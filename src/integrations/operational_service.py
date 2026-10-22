import os
import hashlib
import requests
import logging
import time
import json
import threading
import concurrent.futures
import hashlib
from typing import List, Dict, Any, Optional, Mapping
from collections import deque
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

from src.config import GLOBAL_SAFETY_CONFIG
from src.persistence import save_operation_log
from .business_http import (
    BusinessCircuitOpen,
    BusinessRequestGovernor,
    BusinessRequestLimiterUnavailable,
    CircuitRegistry,
)
from .knowledge_graph import KnowledgeGraphClient

DEFAULT_TEST_BASE_URL = "http://staging.example.test"
DEFAULT_PROD_BASE_URL = "http://search.example.test"


def _request_timeout_seconds(timeout: Any) -> float:
    if isinstance(timeout, (tuple, list)):
        values = [float(value) for value in timeout if value is not None]
        return max(values) if values else 15.0
    if timeout is None:
        return 15.0
    return float(timeout)


class _SlidingWindowRateLimiter:
    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        time_func: Any = None,
        sleep_func: Any = None,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.time_func = time_func or time.time
        self.sleep_func = sleep_func or time.sleep
        self._lock = threading.Lock()
        self._windows: Dict[str, deque[float]] = {}

    def acquire(self, key: str) -> None:
        while True:
            with self._lock:
                now = float(self.time_func())
                window = self._windows.setdefault(key, deque())
                self._trim(window, now)
                if len(window) < self.max_requests:
                    window.append(now)
                    return
                wait_seconds = max(0.0, self.window_seconds - (now - window[0]))
            self.sleep_func(wait_seconds)

    def _trim(self, window: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()

class SummitService:
    """Summit专用服务逻辑"""
    
    # 类级别静态变量，确保所有实例共享同一个 Token 缓存并防止并发刷接口
    _shared_token = None
    _token_expires_at = 0
    _token_lock = threading.Lock()

    def __init__(
        self,
        env: str = "test",
        request_governor: Optional[BusinessRequestGovernor] = None,
        circuit_registry: Optional[CircuitRegistry] = None,
    ):
        self.env = env
        self.request_governor = request_governor or BusinessRequestGovernor()
        self.max_retries = int(GLOBAL_SAFETY_CONFIG.get("max_retry_count", 3))
        self.circuit_registry = circuit_registry or CircuitRegistry(
            failure_threshold=int(GLOBAL_SAFETY_CONFIG.get("circuit_breaker_threshold", 10)),
            cooldown_seconds=float(GLOBAL_SAFETY_CONFIG.get("circuit_breaker_cooldown_seconds", 60)),
        )
        if env == "prod":
            self.base_url = "https://api.summit.example.test/search-engine"
            self.token_url = "https://idp.summit.example.test/as/token.oauth2"

        else:
            self.base_url = "https://api-pp.summit.example.test/search-engine"
            self.token_url = "https://preprod.idp.summit.example.test/as/token.oauth2"
        self.auth_basic = os.getenv("SUMMIT_OAUTH_TOKEN", "")

    def get_token(self, force_refresh: bool = False) -> Optional[str]:
        # 1. 乐观锁：无须抢锁直接返回 (增加时间检查)
        now = time.time()
        if not force_refresh and SummitService._shared_token and SummitService._token_expires_at > now + 60:
            return SummitService._shared_token

        if not self.auth_basic:
            raise RuntimeError("SUMMIT_OAUTH_TOKEN is required")
        
        # 2. 抢锁获取/刷新
        with SummitService._token_lock:
            # 二次判定 (Double-checked locking)
            if not force_refresh and SummitService._shared_token and SummitService._token_expires_at > now + 60:
                return SummitService._shared_token
                
            headers = {
                "Authorization": f"Basic {self.auth_basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            credential_hash = hashlib.sha256(
                self.auth_basic.encode("utf-8")
            ).hexdigest()[:16]
            request_key = (
                f"{self.env}|{urlparse(self.token_url).netloc.lower()}|"
                f"client:summit-oauth:{credential_hash}"
            )
            permit = self.circuit_registry.admit(request_key)
            if permit is None:
                logging.error("❌ Summit OAuth 熔断器已打开")
                return None
            outcome_recorded = False

            def record_outcome(outcome: str) -> None:
                nonlocal outcome_recorded
                if outcome_recorded:
                    return
                getattr(self.circuit_registry, outcome)(request_key, permit)
                outcome_recorded = True

            try:
                resp = self.request_governor.request_with_rate_limit_retry(
                    key=request_key,
                    method="POST",
                    url=self.token_url,
                    request_func=lambda method, target_url, **request_kwargs: requests.post(
                        target_url,
                        **request_kwargs,
                    ),
                    max_attempts=self.max_retries,
                    request_timeout=10.0,
                    headers=headers,
                    params={"grant_type": "client_credentials"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    try:
                        res_data = resp.json()
                    except Exception:
                        record_outcome("record_failure")
                        logging.error("❌ 获取Summit Token 失败: 响应不是有效 JSON")
                        return None
                    token = res_data.get("access_token") if isinstance(res_data, dict) else None
                    if not token:
                        record_outcome("record_failure")
                        logging.error("❌ 获取Summit Token 失败: access_token 缺失")
                        return None
                    record_outcome("record_success")
                    SummitService._shared_token = token
                    expires_in = res_data.get("expires_in", 3600)
                    SummitService._token_expires_at = time.time() + expires_in
                    logging.info(f"✅ Summit鉴权 Token 获取/刷新成功 (有效期: {expires_in}s)")
                    return SummitService._shared_token
                if resp.status_code == 429:
                    record_outcome("record_rate_limited")
                elif resp.status_code >= 500:
                    record_outcome("record_failure")
                else:
                    record_outcome("record_success")
                logging.error(f"❌ 获取Summit Token 失败, HTTP {resp.status_code}: {resp.text}")
            except Exception as e:
                record_outcome("record_failure")
                logging.error(f"❌ 获取Summit Token 异常: {e}")
            return None

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        response_validator = kwargs.pop("_response_validator", None)
        max_attempts = kwargs.pop("_max_attempts", self.max_retries)
        token = self.get_token()
        headers = kwargs.get("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        kwargs["headers"] = headers
        
        request_key = f"{self.env}|{urlparse(url).netloc.lower()}|client:summit"
        request_timeout = _request_timeout_seconds(kwargs.get("timeout"))
        kwargs["timeout"] = request_timeout
        permit = self.circuit_registry.admit(request_key)
        if permit is None:
            raise BusinessCircuitOpen(f"business circuit open: {request_key}")

        outcome_recorded = False

        def record_outcome(outcome: str) -> None:
            nonlocal outcome_recorded
            if outcome_recorded:
                return
            getattr(self.circuit_registry, outcome)(request_key, permit)
            outcome_recorded = True

        # 第一次尝试请求业务接口
        try:
            resp = self.request_governor.request_with_rate_limit_retry(
                key=request_key,
                method=method,
                url=url,
                request_func=requests.request,
                max_attempts=max_attempts,
                request_timeout=request_timeout,
                **kwargs,
            )

            # 3. 如果返回 401 Unauthorized，触发强制刷新并原地重试一次
            if resp.status_code == 401:
                logging.warning(f"⚠️ Summit业务接口返回 401，正在触发 Token 强制刷新并重试一次... URL: {url}")
                new_token = self.get_token(force_refresh=True)
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                    kwargs["headers"] = headers
                    resp = self.request_governor.request_with_rate_limit_retry(
                        key=request_key,
                        method=method,
                        url=url,
                        request_func=requests.request,
                        max_attempts=max_attempts,
                        request_timeout=request_timeout,
                        **kwargs,
                    )

            if resp.status_code == 200:
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise ValueError("Summit response JSON must be an object")
                if response_validator is not None and not response_validator(payload):
                    raise ValueError("Summit response JSON failed endpoint validation")

            if resp.status_code >= 500:
                record_outcome("record_failure")
            elif resp.status_code == 429:
                record_outcome("record_rate_limited")
            else:
                record_outcome("record_success")
            return resp
        except Exception:
            record_outcome("record_failure")
            raise

    @staticmethod
    def _search_response_state(payload: Any) -> str:
        if not isinstance(payload, dict):
            return "invalid"
        if "resp_code" in payload:
            response_code = payload.get("resp_code")
            if isinstance(response_code, bool):
                return "invalid"
            if isinstance(response_code, int):
                if response_code != 0:
                    return "business_error"
            elif isinstance(response_code, str) and response_code.strip():
                if response_code.strip() != "0":
                    return "business_error"
            else:
                return "invalid"
        datas = payload.get("datas")
        if not isinstance(datas, dict):
            return "invalid"
        dsm_list = datas.get("dsmList")
        if not isinstance(dsm_list, list):
            return "invalid"
        for item in dsm_list:
            if not isinstance(item, dict):
                return "invalid"
            models = item.get("models", [])
            if not isinstance(models, list) or not all(
                isinstance(model, dict) for model in models
            ):
                return "invalid"
        return "success"

    @classmethod
    def _is_valid_search_response(cls, payload: Any) -> bool:
        return cls._search_response_state(payload) != "invalid"

    def _search_products_or_raise(
        self,
        query: str,
        page_size: int = 10,
        *,
        enrich_details: bool = True,
        request_timeout: float = 15.0,
        max_attempts: int | None = None,
    ) -> List[Dict]:
        url = f"{self.base_url}/searchops/textSearchNew/v2"
        payload = {
            "text": query,
            "pageNum": 1,
            "pageSize": page_size
        }
        try:
            resp = self._request(
                "POST",
                url,
                json=payload,
                timeout=request_timeout,
                _max_attempts=max_attempts or self.max_retries,
                _response_validator=self._is_valid_search_response,
            )
            if resp.status_code == 200:
                data = resp.json()
                response_state = self._search_response_state(data)
                if response_state == "business_error":
                    raise RuntimeError(
                        f"search_business_error:{data.get('resp_code')}"
                    )
                if response_state != "success":
                    raise ValueError("Summit search response structure is invalid")
                logging.info(f"🔍 Summit搜索原始返回 (前1000字符): {json.dumps(data, ensure_ascii=False)[:1000]}")
                # Summit结构映射
                rows = []
                dsm_list = data.get("datas", {}).get("dsmList", [])
                
                for item in dsm_list:
                    models = item.get("models", [])
                    if not models: continue
                    model = models[0]
                    
                    # 提取该商品下所有的 modelCode (款式)
                    model_codes = [str(m.get("modelCode")) for m in models if m.get("modelCode")]
                    
                    # 1. 基础映射：确保不丢弃原始字段
                    row = dict(item) # 保留 dsmCode, brandDisplayName, designedFor, family, productNatureText 等
                    row.update(model) # 保留 webLabel, modelCode, colorLabel, price 等
                    
                    # 2. 补全通用字段名，修复 productName 取值错误
                    price_data = model.get("price")
                    active_price = 0
                    if isinstance(price_data, dict):
                        active_price = price_data.get("activePrice", 0)
                    elif isinstance(price_data, (int, float)):
                        active_price = price_data

                    row.update({
                        "brand": "summit",
                        "skuId": ";".join(model_codes),
                        "spuId": ";".join(model_codes),
                        "productId": ";".join(model_codes), # <--- 核心修改：改为分号分隔的所有款式 ID，无前缀
                        "productName": model.get("webLabel") or model.get("shortLabel") or model.get("productName"),
                        "mainImage": model.get("media", {}).get("images", [""])[0],
                        "price": active_price,
                        "stock": 10, # 初始默认值
                        "description": item.get("designedFor", ""),
                        "category": item.get("family", ""),
                        "mainCategory": item.get("productNatureText", ""),
                        "color": model.get("colorLabel", ""),
                    })
                    
                    # 2.1 增加保底逻辑 (用于详情接口失效或字段缺失时)
                    benefits_list = [b.get("title", "") for b in item.get("benefits", [])]
                    fallback_tag_name = ",".join(filter(None, [
                        item.get("brandDisplayName"), 
                        item.get("family"), 
                        item.get("productNatureText")
                    ] + benefits_list))
                    row["tagName"] = fallback_tag_name
                    
                    base_dimensions = {
                        "品牌": item.get("brandDisplayName"),
                        "大类": item.get("family"),
                        "子类": item.get("productNatureText"),
                        "适用场景": item.get("designedFor"),
                        "颜色": model.get("colorLabel")
                    }
                    row["dimensions"] = base_dimensions

                    # 直接从搜索结果的 models 数组构造明细列表，确保 Agent 获得完整款式信息
                    all_model_details = []
                    for m in models:
                        m_detail = dict(m)
                        # 合并 SPU 级标签和分类信息，供 Agent 提示词模板使用
                        m_detail.update({
                            "tagName": item.get("tagName") or fallback_tag_name,
                            "englishTag": item.get("englishTag"),
                            "family": item.get("family"),
                            "designedFor": item.get("designedFor"),
                            "mainCategory": item.get("productNatureText"),
                            "multiCategory": item.get("multiCategory"),
                            "dimensions": base_dimensions # <--- 修复：将维度信息同步到款式级
                        })
                        all_model_details.append(m_detail)
                    row["summit_models_details"] = all_model_details
                    
                    rows.append(row)
                
                # 3. 可选富化：普通商品搜索需要详情接口的全量字段；治理批次
                # 已经只需要召回快照中的通用字段，跳过逐 modelCode 请求，
                # 避免一个 Query 被放大成数十个串行详情调用。
                def enrich_item(r):
                    try:
                        min_stock = 999999
                        model_codes = r["productId"].split(";")
                        
                        # 获取当前已有的款式明细（包含 search 阶段合并的 SPU 信息）
                        current_models = r.get("summit_models_details", [])
                        model_lookup = {str(m.get("modelCode")): m for m in current_models}
                        
                        # 逐个款式请求详情接口 (product/list)
                        for mc in model_codes:
                            if not mc: continue
                            # 重要：详情接口接收纯 modelCode，不带前缀
                            details = self.get_product_details(mc)
                            if details:
                                # 详情接口返回的 list 项就是款式全量详情
                                m_full = details[0]
                                logging.info(f"🔍 成功获取Summit详情 ({mc}): webLabel={m_full.get('webLabel')}, mainCategory={m_full.get('mainCategory')}")
                                
                                # 更新实时库存 (详情接口中库存字段名为 stock)
                                istock = m_full.get("stock")
                                if istock is not None:
                                    min_stock = min(min_stock, int(istock))

                                # 核心修复：合并详情到现有对象，不覆盖已有的 SPU 级信息 (family, designedFor 等)
                                if mc in model_lookup:
                                    # 保留原始 model 数据中的 SPU 级字段
                                    original = model_lookup[mc]
                                    # 将详情合并进去，但详情中为 None 的字段不覆盖
                                    for k, v in m_full.items():
                                        if v is not None:
                                            original[k] = v
                                else:
                                    # 如果搜索没出但详情出了（基本不会发生），则直接添加
                                    current_models.append(m_full)
                        
                        # 同步第一个款式的高价值字段到外层，供通用逻辑使用
                        if current_models:
                            m0 = current_models[0]
                            # 仅同步非空字段，防止覆盖 search 阶段的保底数据
                            update_map = {}
                            for k in ["webLabel", "tagName", "englishTag", "mainCategory", "multiCategory", "category", "dimensions"]:
                                val = m0.get(k)
                                if val: update_map[k] = val
                            
                            if m0.get("webLabel"):
                                update_map["productName"] = m0.get("webLabel")
                                
                            r.update(update_map)

                        if min_stock != 999999:
                            r["stock"] = min_stock
                            
                    except Exception as e:
                        logging.warning(f"异步富化Summit商品全量详情失败 ({r.get('productId')}): {e}")

                if enrich_details and rows:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                        executor.map(enrich_item, rows)

                return rows
            raise RuntimeError(f"search_http_status:{resp.status_code}")
        except Exception as e:
            logging.error(f"Summit搜索失败: {e}")
            raise

    def search_products_with_status(
        self,
        query: str,
        page_size: int = 10,
        *,
        enrich_details: bool = True,
        request_timeout: float = 15.0,
        max_attempts: int | None = None,
    ) -> Dict[str, Any]:
        try:
            products = self._search_products_or_raise(
                query,
                page_size,
                enrich_details=enrich_details,
                request_timeout=request_timeout,
                max_attempts=max_attempts,
            )
        except Exception as exc:
            return {
                "success": False,
                "products": [],
                "error_reason": f"search_exception:{type(exc).__name__}",
            }
        if not isinstance(products, list) or not all(isinstance(product, dict) for product in products):
            return {
                "success": False,
                "products": [],
                "error_reason": "invalid_search_response",
            }
        return {
            "success": True,
            "products": products,
            "error_reason": None,
        }

    def search_products(
        self, query: str, page_size: int = 10, *, enrich_details: bool = True
    ) -> List[Dict]:
        return self.search_products_with_status(
            query, page_size, enrich_details=enrich_details
        )["products"]

    @staticmethod
    def _product_details_response_state(payload: Any) -> str:
        if not isinstance(payload, dict):
            return "invalid"
        response_code = payload.get("code")
        if isinstance(response_code, bool):
            return "invalid"
        if isinstance(response_code, int):
            if response_code not in {0, 200}:
                return "business_error"
        elif response_code is not None:
            return "invalid"
        data = payload.get("data")
        if not isinstance(data, dict) or "list" not in data:
            return "invalid"
        products = data.get("list")
        if not isinstance(products, list) or not all(
            isinstance(product, dict) for product in products
        ):
            return "invalid"
        total = data.get("total")
        if total is not None and (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total < len(products)
        ):
            return "invalid"
        return "success"

    @classmethod
    def _is_valid_product_details_response(cls, payload: Any) -> bool:
        return cls._product_details_response_state(payload) != "invalid"

    def get_product_details_with_status(self, product_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/searchops-admin/search/product/list"
        payload = {"productId": product_id, "pageNo": 1, "pageSize": 1}
        try:
            resp = self._request(
                "POST",
                url,
                json=payload,
                timeout=10,
                _response_validator=self._is_valid_product_details_response,
            )
        except Exception as e:
            logging.error(f"获取Summit详情异常 (ID: {product_id}): {e}")
            return {
                "success": False,
                "products": [],
                "error_reason": f"detail_exception:{type(e).__name__}",
            }
        if resp.status_code != 200:
            logging.error(f"❌ 获取Summit详情失败 (ID: {product_id}), HTTP {resp.status_code}: {resp.text}")
            return {
                "success": False,
                "products": [],
                "error_reason": f"detail_http_status:{resp.status_code}",
            }
        res_json = resp.json()
        response_state = self._product_details_response_state(res_json)
        if response_state != "success":
            return {
                "success": False,
                "products": [],
                "error_reason": f"detail_{response_state}",
            }
        data_list = res_json["data"]["list"]
        if not data_list:
            logging.warning(f"⚠️ Summit详情接口返回列表为空 (ID: {product_id}), Response: {resp.text}")
        return {
            "success": True,
            "products": data_list,
            "error_reason": None,
        }

    def get_product_details(self, product_id: str) -> List[Dict]:
        result = self.get_product_details_with_status(product_id)
        return result["products"] if result.get("success") else []

    def update_blacklist(
        self,
        raw_id: str,
        words: List[str],
        delete: bool = False,
        external_request_id: Optional[str] = None,
    ) -> bool:
        url = f"{self.base_url}/searchops-admin/new/search/tag/update"
        
        # 详情接口使用纯 modelCode，标签更新接口需要加 summit_ 前缀
        full_product_id = raw_id 
        
        # 1. 先获取当前详情
        detail_result = self.get_product_details_with_status(full_product_id)
        if not detail_result.get("success"):
            logging.error(
                "❌ Summit blacklist update aborted because product detail evidence is unavailable: %s",
                detail_result.get("error_reason"),
            )
            return False
        details = detail_result.get("products")

        def _normalized_product_id(value: Any) -> Optional[str]:
            if isinstance(value, bool) or value is None:
                return None
            normalized = str(value).strip()
            if normalized.startswith("summit_"):
                normalized = normalized[len("summit_"):]
            return normalized or None

        target_id = _normalized_product_id(full_product_id)
        item_detail = next(
            (
                detail
                for detail in details
                if isinstance(detail, dict)
                and target_id is not None
                and any(
                    _normalized_product_id(detail.get(field)) == target_id
                    for field in ("productId", "modelCode", "skuId")
                )
            ),
            None,
        ) if isinstance(details, list) else None
        if item_detail is None:
            logging.error(
                "❌ Summit blacklist update aborted because matching product detail is missing: %s",
                full_product_id,
            )
            return False
        current_blacklist = item_detail.get("blackListAi") or ""
        
        # 2. 合并黑名单
        existing = [w.strip() for w in current_blacklist.split(",") if w.strip()]
        if delete:
            new_list = [w for w in existing if w not in words]
        else:
            new_list = list(set(existing + words))
        
        payload = {
            "productId": f"summit_{full_product_id}", # 修正：添加 summit_ 前缀
            "blackListAi": ",".join(new_list)
        }
        
        headers = {"Content-Type": "application/json"}
        if external_request_id:
            headers["Idempotency-Key"] = external_request_id
            headers["X-Request-ID"] = external_request_id
        start_time = time.perf_counter()
        log_data = {
            "brand": "summit",
            "query": "batch_update",
            "module": "SummitService",
            "action": "PUT tag/update",
            "request": json.dumps(payload, ensure_ascii=False),
        }
        try:
            resp = self._request("PUT", url, json=payload, headers=headers)
            log_data["duration"] = time.perf_counter() - start_time
            log_data["response"] = str(resp.text) if hasattr(resp, "text") else str(resp)
            log_data["status"] = "success" if resp.status_code == 200 else "failed"
            save_operation_log(log_data)
            
            if resp.status_code == 200:
                res_data = resp.json()
                success = res_data.get("code") == 0
                if success:
                    logging.info(f"✅ Summit product {raw_id} blacklist updated successfully.")
                else:
                    logging.error(f"❌ Summit blacklist update failed: {res_data.get('msg')}")
                return success
            return False
        except Exception as e:
            log_data["duration"] = time.perf_counter() - start_time
            log_data["status"] = "failed"
            log_data["error"] = str(e)
            save_operation_log(log_data)
            logging.error(f"Summit blacklist update exception: {e}")
            if external_request_id:
                raise
            return False

    def update_whitelist(
        self,
        raw_id: str,
        words: List[str],
        delete: bool = False,
        external_request_id: Optional[str] = None,
    ) -> bool:
        url = f"{self.base_url}/searchops-admin/new/search/tag/update"
        full_product_id = raw_id
        detail_result = self.get_product_details_with_status(full_product_id)
        if not detail_result.get("success"):
            logging.error(
                "❌ Summit whitelist update aborted because product detail evidence is unavailable: %s",
                detail_result.get("error_reason"),
            )
            return False
        details = detail_result.get("products")

        def _normalized_product_id(value: Any) -> Optional[str]:
            if isinstance(value, bool) or value is None:
                return None
            normalized = str(value).strip()
            if normalized.startswith("summit_"):
                normalized = normalized[len("summit_"):]
            return normalized or None

        target_id = _normalized_product_id(full_product_id)
        item_detail = next(
            (
                detail
                for detail in details
                if isinstance(detail, dict)
                and target_id is not None
                and any(
                    _normalized_product_id(detail.get(field)) == target_id
                    for field in ("productId", "modelCode", "skuId", "spu")
                )
            ),
            None,
        ) if isinstance(details, list) else None
        if item_detail is None:
            logging.error(
                "❌ Summit whitelist update aborted because matching product detail is missing: %s",
                full_product_id,
            )
            return False
        current_whitelist = item_detail.get("whiteListAi") or item_detail.get("whiteList") or ""
        existing = [w.strip() for w in str(current_whitelist).split(",") if w.strip()]
        if delete:
            new_list = [w for w in existing if w not in words]
        else:
            new_list = list(dict.fromkeys(existing + [w.strip() for w in words if str(w).strip()]))

        payload = {
            "productId": f"summit_{target_id}",
            "whiteListAi": ",".join(new_list),
            "whiteList": ",".join(new_list),
        }
        headers = {"Content-Type": "application/json"}
        if external_request_id:
            headers["Idempotency-Key"] = external_request_id
            headers["X-Request-ID"] = external_request_id
        start_time = time.perf_counter()
        log_data = {
            "brand": "summit",
            "query": "batch_update",
            "module": "SummitService",
            "action": "PUT tag/update",
            "request": json.dumps(payload, ensure_ascii=False),
        }
        try:
            resp = self._request("PUT", url, json=payload, headers=headers)
            log_data["duration"] = time.perf_counter() - start_time
            log_data["response"] = str(resp.text) if hasattr(resp, "text") else str(resp)
            log_data["status"] = "success" if resp.status_code == 200 else "failed"
            save_operation_log(log_data)
            if resp.status_code == 200:
                res_data = resp.json()
                return res_data.get("code") == 0
            return False
        except Exception as e:
            log_data["duration"] = time.perf_counter() - start_time
            log_data["status"] = "failed"
            log_data["error"] = str(e)
            save_operation_log(log_data)
            logging.error(f"Summit whitelist update exception: {e}")
            if external_request_id:
                raise
            return False

class OperationalService:
    """对接《运营接口文档.pdf》中的业务接口"""

    # 固定品牌配置：alias / tenantId 固定，敏感 apiKey 只从环境变量读取
    FIXED_BRAND_CONFIGS = {
        "atlas": {
            "api_alias": "alias_atlas",
            "tenantId": 235,
            "api_key_env": "ATLAS_API_KEY",
            "base_urls": {
                "prod": "http://search.example.test",
                "test": "http://staging.example.test",
            },
        },
        "harbor": {
            "api_alias": "vbnmenzb",
            "tenantId": 204,
            "api_key_env": "HARBOR_API_KEY",
            "base_urls": {
                "prod": "http://search.example.test",
                "test": "http://staging.example.test",
            },
        },
        "riviera": {
            "api_alias": "lxptkkhe",
            "tenantId": 178,
            "api_key_env": "RIVIERA_API_KEY",
            "base_urls": {
                "prod": "http://search.example.test",
                "test": "http://staging.example.test",
            },
        },
        "lumen": {
            "api_alias": "alias_lumen",
            "tenantId": 243,
            "api_key_env": "LUMEN_API_KEY",
            "base_urls": {
                "prod": "http://search.example.test",
                "test": "http://staging.example.test",
            },
        },
    }

    _rate_limiter = _SlidingWindowRateLimiter(max_requests=10, window_seconds=1.0)
    _rate_limited_endpoints = {
        ("POST", "/api/search/mixed"): "POST /api/search/mixed",
        ("POST", "/admin-api/search/product/list"): "POST /admin-api/search/product/list",
    }
    brand_configs = {}  # 共享品牌配置，避免每个实例重复刷新

    def __init__(self):
        from src.config import get_env
        # ENV 是唯一来源（进程环境优先于 .env）；缺失时默认 test，未知值 fail fast
        self.env = get_env()
        if self.env == "prod":
            self.base_url = os.getenv("PROD_BASE_URL") or DEFAULT_PROD_BASE_URL
        else:
            self.base_url = os.getenv("TEST_BASE_URL") or DEFAULT_TEST_BASE_URL
        self.failure_threshold = GLOBAL_SAFETY_CONFIG.get("circuit_breaker_threshold", 10)
        self.max_retries = GLOBAL_SAFETY_CONFIG.get("max_retry_count", 3)
        self.delay_threshold = GLOBAL_SAFETY_CONFIG.get("api_delay_threshold", 5)
        self.request_governor = BusinessRequestGovernor()
        self.circuit_registry = CircuitRegistry(
            failure_threshold=self.failure_threshold,
            cooldown_seconds=float(GLOBAL_SAFETY_CONFIG.get("circuit_breaker_cooldown_seconds", 60)),
        )
        self.summit_service = SummitService(
            self.env,
            request_governor=self.request_governor,
            circuit_registry=self.circuit_registry,
        )
        self.kg_client = KnowledgeGraphClient(
            request_governor=self.request_governor,
            circuit_registry=self.circuit_registry,
            environment=self.env,
        )
        
        # 仅在配置为空或环境切换时刷新
        if not self.brand_configs:
            self._refresh_brand_configs()
            
        logging.info(f"Initialized OperationalService in {self.env} environment")

    @classmethod
    def get_runtime_status(cls) -> Dict[str, Any]:
        try:
            governor = BusinessRequestGovernor()
            registry = CircuitRegistry()
            open_keys = registry.open_keys()
            open_circuit_count = len(open_keys)
            limiter = {**governor.snapshot(), **governor.metrics()}
            return {
                "available": True,
                "consecutive_failures": 0,
                "is_circuit_broken": open_circuit_count > 0,
                "limiter": limiter,
                "circuit": {
                    "openCircuitCount": open_circuit_count,
                    "openCircuitIds": [f"sha256:{key[:16]}" for key in open_keys],
                    "lastRecoveryAt": registry.last_recovery_at(),
                },
            }
        except BusinessRequestLimiterUnavailable as exc:
            return {
                "available": False,
                "consecutive_failures": 0,
                "is_circuit_broken": True,
                "limiter": {},
                "circuit": {
                    "openCircuitCount": None,
                    "openCircuitIds": [],
                    "lastRecoveryAt": None,
                },
                "error": str(exc),
            }

    @classmethod
    def clear_brand_config_cache(cls) -> None:
        cls.brand_configs.clear()

    def _get_hardcoded_config(self, brand_alias: str) -> Optional[Dict]:
        """获取固定品牌配置，敏感 apiKey 仅从环境变量读取。"""
        brand_config = self.FIXED_BRAND_CONFIGS.get(brand_alias)
        if not brand_config:
            return None

        base_urls = brand_config.get("base_urls", {})
        return {
            "api_alias": brand_config.get("api_alias"),
            "tenantId": brand_config.get("tenantId"),
            "apiKey": os.getenv(brand_config.get("api_key_env", "")),
            "base_url": base_urls.get(self.env, base_urls.get("test")),
        }

    def _get_base_url(self, brand_alias: str) -> str:
        """获取品牌对应的 Base URL"""
        hardcoded = self._get_hardcoded_config(brand_alias)
        if hardcoded and "base_url" in hardcoded:
            return hardcoded["base_url"]
        return self.base_url

    def _get_kg_config_base_url(self, brand_alias: str) -> str:
        """知识图谱无独立测试环境，sceneId 配置始终从正式业务地址读取。"""
        brand_config = self.FIXED_BRAND_CONFIGS.get(brand_alias) or {}
        production_url = (brand_config.get("base_urls") or {}).get("prod")
        return production_url or os.getenv("PROD_BASE_URL") or DEFAULT_PROD_BASE_URL

    def _is_prod_url(self, url: str) -> bool:
        return "search.example.test" in (url or "")

    def _is_write_method(self, method: str) -> bool:
        return method.upper() in {"POST", "PUT", "PATCH", "DELETE"}

    def _guard_test_write_url(self, method: str, url: str):
        if self.env == "test" and self._is_write_method(method) and self._is_prod_url(url):
            raise RuntimeError(f"ENV=test 禁止向生产地址执行写操作: {method} {url}")

    def _is_set_ops_low_weight_url(self, url: str) -> bool:
        return "/api/search/redis/setOpsLowWeight" in url

    def _is_plain_text_success_response(self, text: str) -> bool:
        normalized = (text or "").strip().lower()
        if not normalized.startswith("ok"):
            return False
        if len(normalized) > 2 and normalized[2].isalnum():
            return False
        return not any(marker in normalized for marker in (
            "error",
            "fail",
            "reject",
            "false",
            "not ok",
            "denied",
            "invalid",
        ))

    @staticmethod
    def _is_success_response_code(response_code: Any) -> bool:
        if response_code is None:
            return True
        if isinstance(response_code, bool):
            return False
        if isinstance(response_code, int):
            return response_code in {0, 200}
        if isinstance(response_code, str):
            return response_code.strip() in {"0", "200"}
        return False

    @classmethod
    def _is_business_error_response_code(cls, response_code: Any) -> bool:
        if response_code is None or isinstance(response_code, bool):
            return False
        if isinstance(response_code, int):
            return not cls._is_success_response_code(response_code)
        if isinstance(response_code, str):
            normalized_code = response_code.strip()
            return bool(normalized_code) and not cls._is_success_response_code(normalized_code)
        return False

    @classmethod
    def _is_valid_search_response(cls, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if not cls._is_success_response_code(payload.get("code")):
            return False
        data = payload.get("data")
        if not isinstance(data, dict) or "rows" not in data:
            return False
        rows = data.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return False
        total = None
        for field in ("total", "totalCount", "count"):
            if field in data:
                total = data.get(field)
                break
        if total is None:
            return True
        return (
            isinstance(total, int)
            and not isinstance(total, bool)
            and total >= len(rows)
        )

    @classmethod
    def _rate_limit_key_for_request(cls, method: str, url: str) -> Optional[str]:
        normalized_method = str(method or "").upper()
        normalized_path = urlparse(url).path or ""
        return cls._rate_limited_endpoints.get((normalized_method, normalized_path))

    @classmethod
    def _acquire_rate_limit_slot(cls, key: str) -> None:
        cls._rate_limiter.acquire(key)

    def _business_request_key(self, brand_alias: str, url: str) -> str:
        config = self._get_hardcoded_config(brand_alias) or self.brand_configs.get(brand_alias, {})
        identity = config.get("tenantId")
        if identity is not None and str(identity).strip():
            identity_key = f"tenant:{identity}"
        else:
            identity_key = f"alias:{config.get('api_alias') or brand_alias}"
        return f"{self.env}|{urlparse(url).netloc.lower()}|{identity_key}"

    def _call_api(
        self,
        method: str,
        url: str,
        brand_alias: str,
        query: str = None,
        *,
        _max_attempts: int | None = None,
        **kwargs,
    ) -> requests.Response:
        """统一接口调用封装，集成 Req 1 的日志记录与异常告警"""
        from src.persistence import save_operation_log

        response_validator = kwargs.pop("_response_validator", None)
        redact_response_log = bool(kwargs.pop("redact_response_log", False))
        external_request_id = str(kwargs.pop("external_request_id", "") or "").strip()
        if external_request_id:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("Idempotency-Key", external_request_id)
            headers.setdefault("X-Request-ID", external_request_id)
            kwargs["headers"] = headers
        
        start_time = time.perf_counter()
        # 提取 Payload
        payload = kwargs.get("json", kwargs.get("params", {}))
        endpoint_path = urlparse(url).path or ""
        log_data = {
            "brand": brand_alias,
            "query": query,
            "module": "OperationalService",
            "action": f"{method} {url.split('/')[-1]}",
            "request": json.dumps(payload, ensure_ascii=False),
            # 只有搜索回读被页面消费；成功写操作只保留结果元数据，失败仍保留有界诊断。
            "payload_policy": (
                "full"
                if endpoint_path in {"/api/search/mixed", "/admin-api/search/product/list"}
                else "failure_only"
            ),
        }
        request_key = None
        permit = None
        outcome_recorded = False

        def record_outcome(outcome: str) -> None:
            nonlocal outcome_recorded
            if outcome_recorded or request_key is None or permit is None:
                return
            getattr(self.circuit_registry, outcome)(request_key, permit)
            outcome_recorded = True
        
        try:
            self._guard_test_write_url(method, url)
            request_key = self._business_request_key(brand_alias, url)
            permit = self.circuit_registry.admit(request_key)
            if permit is None:
                raise BusinessCircuitOpen(f"business circuit open: {request_key}")
            request_timeout = _request_timeout_seconds(kwargs.get("timeout"))
            kwargs["timeout"] = request_timeout
            resp = self.request_governor.request_with_rate_limit_retry(
                key=request_key,
                method=method,
                url=url,
                request_func=requests.request,
                max_attempts=self.max_retries if _max_attempts is None else _max_attempts,
                request_timeout=request_timeout,
                **kwargs,
            )
            duration = time.perf_counter() - start_time
            log_data.update({
                "response": "[REDACTED]" if redact_response_log else resp.text,
                "status": "success" if resp.status_code == 200 else "failed",
                "duration": duration
            })

            # 延迟监测
            if duration > self.delay_threshold:
                msg = f"⚠️ [接口延迟告警] {method} {url.split('/')[-1]} 响应过慢 ({duration:.2f}s)\n品牌: {brand_alias}\n关键词: {query or 'N/A'}"
                from src.runtime.notifier import send_feishu_notification
                send_feishu_notification(msg, "自动化运维系统告警")
            
            # 业务层逻辑判断
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    if self._is_set_ops_low_weight_url(url) and self._is_plain_text_success_response(resp.text):
                        record_outcome("record_success")
                        log_data["status"] = "success"
                    else:
                        record_outcome("record_failure")
                        log_data["status"] = "failed"
                        response_detail = "[REDACTED]" if redact_response_log else resp.text[:100]
                        self._alert_failure(url, brand_alias, query, f"接口返回非 JSON 内容: {response_detail}")
                else:
                    if isinstance(data, dict) and self._is_business_error_response_code(data.get("code")):
                        record_outcome("record_success")
                        log_data["status"] = "failed"
                        if redact_response_log:
                            log_data["error"] = (
                                f"Business Error: code={data.get('code')}; details=[REDACTED]"
                            )
                            alert_reason = f"业务代码异常({data.get('code')}): [REDACTED]"
                        else:
                            log_data["error"] = f"Business Error: {data.get('msg')}"
                            alert_reason = f"业务代码异常({data.get('code')}): {data.get('msg')}"
                        self._alert_failure(url, brand_alias, query, alert_reason)
                        return resp
                    if (
                        isinstance(data, dict)
                        and "code" in data
                        and not self._is_success_response_code(data.get("code"))
                        and not self._is_business_error_response_code(data.get("code"))
                    ):
                        record_outcome("record_failure")
                        log_data["status"] = "failed"
                        log_data["error"] = "Invalid response code contract"
                        self._alert_failure(
                            url,
                            brand_alias,
                            query,
                            "接口响应 code 类型不符合契约",
                        )
                        return resp
                    if response_validator is not None and not response_validator(data):
                        record_outcome("record_failure")
                        log_data["status"] = "failed"
                        log_data["error"] = "Invalid endpoint response structure"
                        self._alert_failure(
                            url,
                            brand_alias,
                            query,
                            "接口返回结构不符合契约",
                        )
                        return resp
                    if "error" in resp.text.lower() and self._is_set_ops_low_weight_url(url):
                        record_outcome("record_success")
                        log_data["status"] = "failed"
                        response_detail = "[REDACTED]" if redact_response_log else resp.text
                        self._alert_failure(url, brand_alias, query, f"分层调整接口返回错误: {response_detail}")
                    else:
                        record_outcome("record_success")
            elif resp.status_code == 429:
                record_outcome("record_rate_limited")
                log_data["status"] = "rate_limited"
                log_data["error"] = "HTTP 429: request rate limited"
            else:
                if resp.status_code >= 500:
                    record_outcome("record_failure")
                else:
                    record_outcome("record_success")
                self._alert_failure(url, brand_alias, query, f"HTTP {resp.status_code}")
                
            return resp
        except BusinessCircuitOpen:
            log_data.update({
                "status": "circuit_breaker_open",
                "duration": time.perf_counter() - start_time,
                "error": "business circuit open",
            })
            raise
        except Exception as e:
            duration = time.perf_counter() - start_time
            if redact_response_log:
                exception_detail = f"{type(e).__name__}: [REDACTED]"
                alert_reason = f"网络异常({type(e).__name__}): [REDACTED]"
            else:
                exception_detail = str(e)
                alert_reason = f"网络异常: {str(e)}"
            log_data.update({
                "status": "failed",
                "duration": duration,
                "error": exception_detail
            })
            if request_key is None:
                raise
            record_outcome("record_failure")
            self._alert_failure(url, brand_alias, query, alert_reason)
            raise
        finally:
            try:
                save_operation_log(log_data)
            except:
                pass # 忽略单元测试中可能的 DB 写入失败

    def _alert_failure(self, url: str, brand: str, query: str, reason: str):
        """发送故障告警到飞书"""
        msg = f"❌ [接口调用失败]\n地址: {url}\n品牌: {brand}\n关键词: {query or 'N/A'}\n原因: {reason}\n环境: {self.env}"
        from src.runtime.notifier import send_feishu_notification
        send_feishu_notification(msg, "自动化运维系统接口告警")

    def _get_api_alias(self, brand_alias: str) -> str:
        """从配置中获取真实的 API 别名，如果没有则返回原值"""
        # 1. 优先从硬编码配置中获取
        hardcoded = self._get_hardcoded_config(brand_alias)
        if hardcoded and "api_alias" in hardcoded:
            return hardcoded["api_alias"]
            
        # 2. 备选从全局配置中获取
        from src.config import BRAND_CONFIGS
        config = BRAND_CONFIGS.get(brand_alias, {})
        return config.get("api_alias", brand_alias)

    def _refresh_brand_configs(self):
        """调用 /public 接口动态刷新所有品牌的 apiKey 和 tenantId"""
        if os.getenv("AIOPS_OFFLINE_TEST") == "1":
            return

        url = f"{self.base_url}/admin-api/common/brands/public"
        try:
            resp = self._call_api("GET", url, "system", "refresh_configs")
            if resp.status_code == 200:
                data = resp.json()
                res_code = data.get("code")
                if res_code in [0, 200] or res_code is None:
                    for brand in data.get("data", []):
                        self.brand_configs[brand["alias"]] = {
                            "apiKey": brand["apiKey"],
                            "tenantId": brand["tenantId"]
                    }
                    logging.info(f"✅ 品牌配置刷新成功: {list(self.brand_configs.keys())}")
        except Exception as e:
            logging.error(f"刷新品牌配置失败: {e}")

    def _get_headers(self, brand_alias: str):
        # 1. 优先从硬编码配置中获取
        hardcoded = self._get_hardcoded_config(brand_alias)
        if hardcoded and "apiKey" in hardcoded and "tenantId" in hardcoded:
            return {
                "Authorization": f"Bearer {hardcoded['apiKey']}",
                "tenant-id": str(hardcoded['tenantId']),
                "Content-Type": "application/json"
            }

        # 2. 备选走动态刷新逻辑
        api_alias = self._get_api_alias(brand_alias)
        config = self.brand_configs.get(api_alias)
        if not config:
            # 如果没找到，尝试刷新一次
            self._refresh_brand_configs()
            config = self.brand_configs.get(api_alias, {"apiKey": "", "tenantId": ""})
        
        return {
            "Authorization": f"Bearer {config['apiKey']}",
            "tenant-id": str(config['tenantId']),
            "Content-Type": "application/json"
        }

    def get_refined_query(self, brand_alias: str, query: str) -> str:
        """根据品牌规则获取精简后的关键词"""
        refined_keyword, _ = self._split_query_by_search_rule(brand_alias, query)
        return refined_keyword

    def _split_query_by_search_rule(self, brand_alias: str, query: str) -> tuple[str, list[dict[str, str]]]:
        """按品牌搜索规则拆分 keyword 和结构化 filters。"""
        from src.config import SEARCH_RULES
        rule = SEARCH_RULES.get(brand_alias)
        if not (rule and rule.get("enable_tag_parsing")):
            return query, []

        tag_map = rule.get("tag_map", {})
        parts = str(query or "").strip().split()
        if not parts:
            return str(query or "").strip(), []
        tag_value = tag_map.get(parts[-1])
        if not tag_value:
            return str(query or "").strip(), []
        refined_keyword = " ".join(parts[:-1]).strip() or str(query or "").strip()
        return refined_keyword, [{
            "field": str(rule.get("filter_dimension", "gender")),
            "value": str(tag_value),
        }]

    @staticmethod
    def _merge_tag_bucket_filters(filters: list[dict[str, Any]], derived_filters: list[dict[str, str]]) -> list[dict[str, str]]:
        merged: list[dict[str, str]] = []
        seen = set()
        for item in [*(filters or []), *(derived_filters or [])]:
            if not isinstance(item, dict):
                continue
            normalized = {
                "field": str(item.get("field") or ""),
                "value": str(item.get("value") or ""),
            }
            if not normalized["field"] or not normalized["value"]:
                continue
            key = (normalized["field"], normalized["value"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)
        return merged

    def _normalize_tag_bucket_payload(self, brand_alias: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        keyword = str(payload.get("keyword") or "").strip()
        refined_keyword, derived_filters = self._split_query_by_search_rule(brand_alias, keyword)
        merged_filters = self._merge_tag_bucket_filters(payload.get("filters") or [], derived_filters)
        normalized = dict(payload)
        normalized["keyword"] = refined_keyword
        normalized["filters"] = merged_filters
        return normalized

    @staticmethod
    def supports_tag_bucket_config(brand_alias: str) -> bool:
        return brand_alias != "summit"

    def search_products(self, brand_alias: str, query: str, page_size: int = None) -> List[Dict]:
        """4. 商品召回接口 - /api/search/mixed"""
        return self.search_products_with_total(brand_alias, query, page_size).get("products", [])

    def _structured_search_failure(self, error_reason: str) -> Dict[str, Any]:
        self.request_governor.record_structured_failure()
        return {
            "success": False,
            "products": [],
            "total_count": None,
            "result_complete": False,
            "error_reason": error_reason,
        }

    def search_products_with_total(
        self,
        brand_alias: str,
        query: str,
        page_size: int = None,
        page_num: int = 1,
        *,
        enrich_details: bool = True,
        request_timeout: float | None = None,
        max_attempts: int | None = None,
    ) -> Dict[str, Any]:
        """商品召回接口，返回指定页商品和真实总数。"""
        if page_size is None:
            page_size = 16
        if not isinstance(page_num, int) or isinstance(page_num, bool) or page_num <= 0:
            return self._structured_search_failure("invalid_page_num")
        try:
            timeout_seconds = 60.0 if request_timeout is None else float(request_timeout)
        except (TypeError, ValueError):
            return self._structured_search_failure("invalid_request_timeout")
        if timeout_seconds <= 0:
            return self._structured_search_failure("invalid_request_timeout")
        try:
            attempt_limit = self.max_retries if max_attempts is None else max(1, int(max_attempts))
        except (TypeError, ValueError):
            return self._structured_search_failure("invalid_max_attempts")

        if brand_alias == "summit":
            search_result = self.summit_service.search_products_with_status(
                query,
                page_size,
                enrich_details=enrich_details,
                request_timeout=timeout_seconds if request_timeout is not None else 15.0,
                max_attempts=attempt_limit,
            )
            products = search_result.get("products") if isinstance(search_result, dict) else None
            success = search_result.get("success") if isinstance(search_result, dict) else None
            error_reason = search_result.get("error_reason") if isinstance(search_result, dict) else None
            if (
                not isinstance(success, bool)
                or not isinstance(products, list)
                or not all(isinstance(product, dict) for product in products)
            ):
                success = False
                products = []
                error_reason = "invalid_search_response"
            if not success:
                return self._structured_search_failure(error_reason or "search_failed")
            return {
                "success": True,
                "products": products,
                "total_count": None,
                "result_complete": False,
                "error_reason": None,
            }

        url = f"{self._get_base_url(brand_alias)}/api/search/mixed"
        refined_keyword, parsed_filters = self._split_query_by_search_rule(brand_alias, query)
        api_alias = self._get_api_alias(brand_alias)

        payload = {
            "brand": api_alias,
            "keyword": refined_keyword,
            "pageSize": page_size,
            "pageNum": page_num,
        }

        from src.config import SEARCH_RULES
        rule = SEARCH_RULES.get(brand_alias)
        if parsed_filters and rule and rule.get("enable_tag_parsing"):
            payload["handletype"] = rule.get("handle_type", "200")
            payload["filters"] = [
                {
                    "dimensionName": parsed_filters[0]["field"],
                    "tagNames": [parsed_filters[0]["value"]],
                }
            ]

        for attempt in range(attempt_limit):
            try:
                resp = self._call_api(
                    "POST",
                    url,
                    brand_alias,
                    query,
                    json=payload,
                    headers=self._get_headers(brand_alias),
                    timeout=60 if request_timeout is None else timeout_seconds,
                    _max_attempts=attempt_limit,
                    _response_validator=self._is_valid_search_response,
                )
                if resp.status_code == 429:
                    return self._structured_search_failure("rate_limited")
                if resp.status_code == 200:
                    res_data = resp.json()
                    if not isinstance(res_data, dict):
                        return self._structured_search_failure("invalid_search_response")
                    res_code = res_data.get("code")
                    if self._is_success_response_code(res_code):
                        data = res_data.get("data")
                        if "data" not in res_data or not isinstance(data, dict):
                            return self._structured_search_failure("invalid_search_response")
                        products = data.get("rows", [])
                        total = data.get("total")
                        if total is None:
                            total = data.get("totalCount")
                        if total is None:
                            total = data.get("count")
                        products_are_valid = (
                            isinstance(products, list)
                            and all(isinstance(product, dict) for product in products)
                        )
                        total_is_valid = (
                            total is None
                            or (isinstance(total, int) and not isinstance(total, bool) and total >= 0)
                        )
                        total_matches_rows = (
                            total_is_valid
                            and (
                                total is None
                                or (products_are_valid and total >= len(products))
                            )
                        )
                        if (
                            not products_are_valid
                            or not total_is_valid
                            or not total_matches_rows
                        ):
                            return self._structured_search_failure("invalid_search_response")
                        trusted_total = total
                        raw_tags = data.get("tags")
                        parsed_tags = (
                            [str(item).strip() for item in raw_tags if str(item or "").strip()]
                            if isinstance(raw_tags, list)
                            else []
                        )
                        raw_exclude_tags = data.get("excludeTags")
                        parsed_exclude_tags = (
                            [str(item).strip() for item in raw_exclude_tags if str(item or "").strip()]
                            if isinstance(raw_exclude_tags, list)
                            else []
                        )
                        return {
                            "success": True,
                            "products": products,
                            "total_count": trusted_total,
                            "result_complete": trusted_total is not None and trusted_total == len(products),
                            "tags": parsed_tags,
                            "exclude_tags": parsed_exclude_tags,
                            "error_reason": None,
                        }
                
                logging.warning(
                    "搜索接口返回异常状态码: %s, 尝试重试 %s/%s",
                    resp.status_code,
                    attempt + 1,
                    attempt_limit,
                )
            except BusinessCircuitOpen:
                return self._structured_search_failure("circuit_breaker_open")
            except Exception as e:
                logging.error("商品召回网络异常 (尝试 %s/%s): %s", attempt + 1, attempt_limit, e)
            # Keep the legacy retry cadence for callers that use the global
            # retry policy. The live Query result path passes an explicit
            # bounded policy, where sleeping after the final attempt would
            # defeat the latency bound.
            if attempt + 1 < attempt_limit or max_attempts is None:
                time.sleep(1)
        return self._structured_search_failure("search_retries_exhausted")

    def search_all_products_with_total(
        self,
        brand_alias: str,
        query: str,
        page_size: int = 16,
    ) -> Dict[str, Any]:
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            return self._structured_search_failure("invalid_page_size")

        products: List[Dict[str, Any]] = []
        total_count: Optional[int] = None
        tags: List[str] = []
        exclude_tags: List[str] = []
        seen_page_signatures: set[str] = set()
        seen_product_ids: set[str] = set()
        page_num = 1

        while True:
            page = self.search_products_with_total(
                brand_alias,
                query,
                page_size=page_size,
                page_num=page_num,
            )
            if not isinstance(page, dict) or page.get("success") is not True:
                error_reason = page.get("error_reason") if isinstance(page, dict) else None
                return self._structured_search_failure(str(error_reason or "search_failed"))
            if page_num == 1:
                tags = list(page.get("tags") or [])
                exclude_tags = list(page.get("exclude_tags") or [])
            page_products = page.get("products")
            page_total = page.get("total_count")
            if (
                not isinstance(page_products, list)
                or not all(isinstance(product, dict) for product in page_products)
                or not isinstance(page_total, int)
                or isinstance(page_total, bool)
                or page_total < 0
            ):
                return self._structured_search_failure("invalid_search_response")
            if total_count is None:
                total_count = page_total
            elif page_total != total_count:
                return self._structured_search_failure("total_changed_between_pages")
            if not page_products and len(products) < total_count:
                return self._structured_search_failure("empty_page_before_total")
            if len(products) + len(page_products) > total_count:
                return self._structured_search_failure("result_count_exceeded_total")

            page_signature = json.dumps(
                page_products,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if page_signature in seen_page_signatures:
                return self._structured_search_failure("duplicate_search_page")
            seen_page_signatures.add(page_signature)

            for product in page_products:
                product_identity = self._search_product_identity(product)
                if product_identity in seen_product_ids:
                    return self._structured_search_failure("duplicate_search_product")
                seen_product_ids.add(product_identity)
                products.append(product)
            if len(products) == total_count:
                return {
                    "success": True,
                    "products": products,
                    "total_count": total_count,
                    "result_complete": True,
                    "tags": tags,
                    "exclude_tags": exclude_tags,
                    "error_reason": None,
                }
            page_num += 1

    @staticmethod
    def _search_product_identity(product: Dict[str, Any]) -> str:
        for key in ("productId", "product_id", "skuId", "sku_id", "taggingUid", "id"):
            value = product.get(key)
            if value is not None and str(value).strip():
                return f"id:{str(value).strip()}"
        return "payload:" + json.dumps(product, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def search_products_with_bucket_snapshot(self, brand_alias: str, keyword: str, page_size: int = 100) -> Dict[str, Any]:
        """读取 Query 当前搜索 bucket 快照，不做 gender/tag 自动拆分。"""
        if brand_alias == "summit":
            return self._structured_search_failure("unsupported")

        url = f"{self._get_base_url(brand_alias)}/api/search/mixed"
        api_alias = self._get_api_alias(brand_alias)
        payload = {
            "brand": api_alias,
            "keyword": keyword,
            "pageSize": page_size,
            "pageNum": 1,
        }

        for attempt in range(self.max_retries):
            try:
                resp = self._call_api("POST", url, brand_alias, keyword, json=payload, headers=self._get_headers(brand_alias), timeout=15)
                if resp.status_code == 429:
                    return self._structured_search_failure("rate_limited")
                if resp.status_code == 200:
                    res_data = resp.json()
                    if not isinstance(res_data, dict):
                        raise ValueError("bucket snapshot response must be an object")
                    res_code = res_data.get("code")
                    if res_code in [0, 200] or res_code is None:
                        data = res_data.get("data")
                        if not isinstance(data, dict):
                            raise ValueError("bucket snapshot data must be an object")
                        rows = data.get("rows") or data.get("searchResult") or data.get("list") or data.get("products") or []
                        if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
                            raise ValueError("bucket snapshot rows must be a list of objects")
                        products = [self._bucket_snapshot_product(item) for item in rows if isinstance(item, dict)]
                        total = data.get("total")
                        if total is None:
                            total = data.get("totalCount")
                        if total is None:
                            total = data.get("count")
                        if total is not None and (
                            not isinstance(total, int)
                            or isinstance(total, bool)
                            or total < len(products)
                        ):
                            raise ValueError("bucket snapshot total is invalid")
                        return {
                            "success": True,
                            "products": products,
                            "total_count": total,
                            "result_complete": total is not None and total == len(products),
                            "error_reason": None,
                        }
                logging.warning(f"搜索 bucket 快照接口返回异常状态码: {resp.status_code}, 尝试重试 {attempt+1}/3")
            except BusinessCircuitOpen:
                return self._structured_search_failure("circuit_breaker_open")
            except Exception as e:
                logging.error(f"搜索 bucket 快照网络异常 (尝试 {attempt+1}/3): {e}")
            time.sleep(1)
        return self._structured_search_failure("search_retries_exhausted")

    def get_current_ops_low_weight_skus(self, brand_alias: str, keyword: str) -> List[str]:
        snapshot = self.search_products_with_bucket_snapshot(brand_alias, keyword)
        if snapshot.get("success") is not True or snapshot.get("result_complete") is not True:
            if snapshot.get("success") is True:
                self.request_governor.record_structured_failure()
            raise RuntimeError(
                "bucket search evidence unavailable: "
                f"{snapshot.get('error_reason') or 'result_incomplete'}"
            )
        skus = []
        for item in snapshot.get("products") or []:
            if item.get("bucket") != "productIdsByOpsLowWeight":
                continue
            sku = item.get("sku_id") or item.get("product_id")
            if sku:
                skus.append(str(sku))
        return skus

    _OPS_LOW_WEIGHT_BUCKET = "productIdsByOpsLowWeight"
    _LEGACY_LOW_WEIGHT_BUCKET = "productIdsByLowWeight"

    @classmethod
    def _select_ops_low_weight_bucket(
        cls,
        available_buckets: set[str],
    ) -> str:
        if cls._OPS_LOW_WEIGHT_BUCKET in available_buckets:
            return cls._OPS_LOW_WEIGHT_BUCKET
        # 生产会按 Query 返回新旧两种显式 bucket；只在元数据明确存在时兼容旧名。
        if cls._LEGACY_LOW_WEIGHT_BUCKET in available_buckets:
            return cls._LEGACY_LOW_WEIGHT_BUCKET
        return cls._OPS_LOW_WEIGHT_BUCKET

    @staticmethod
    def _ops_low_weight_config_version(query: str, product_ids: List[str]) -> str:
        normalized_ids = sorted({str(item).strip() for item in product_ids if str(item).strip()})
        canonical = json.dumps(
            {"query": str(query or "").strip(), "productIds": normalized_ids},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def _request_ops_low_weight_search_page(
        self,
        brand_alias: str,
        normalized_query: str,
        page_num: int,
        page_size: int = 500,
    ) -> Dict[str, Any]:
        response = self._call_api(
            "POST",
            f"{self._get_base_url(brand_alias)}/api/search/mixed",
            brand_alias,
            normalized_query,
            json={
                "brand": self._get_api_alias(brand_alias),
                "keyword": normalized_query,
                "pageSize": page_size,
                "pageNum": page_num,
            },
            headers=self._get_headers(brand_alias),
            timeout=15,
            redact_response_log=True,
        )
        if response.status_code != 200:
            raise RuntimeError("authoritative_config_http_error")
        response_body = response.json()
        if not isinstance(response_body, dict):
            raise ValueError("invalid_authoritative_config_response")
        if response_body.get("code") not in (None, 0, 200):
            raise RuntimeError("authoritative_config_business_error")
        data = response_body.get("data")
        if not isinstance(data, dict):
            raise ValueError("invalid_authoritative_config_response")
        return data

    def observe_query_product_buckets(
        self,
        brand_alias: str,
        query: str,
        product_ids: List[str],
        *,
        page_size: int = 500,
    ) -> Dict[str, Any]:
        from src.governance.query_identity import transform_governance_payload_query

        original_query = str(query or "")
        normalized_query = transform_governance_payload_query(brand_alias, query)
        normalized_target_ids: set[str] = set()
        for product_id in product_ids or []:
            if isinstance(product_id, bool) or not isinstance(product_id, (str, int)):
                continue
            normalized_product_id = str(product_id).strip()
            if normalized_product_id:
                normalized_target_ids.add(normalized_product_id)
        target_ids = sorted(normalized_target_ids)
        target_id_set = set(target_ids)
        total_count: Optional[int] = None
        checked_product_count = 0
        observed: Dict[str, Dict[str, Any]] = {}

        def result(*, success: bool, result_complete: bool) -> Dict[str, Any]:
            observations = {
                product_id: observed[product_id]
                for product_id in target_ids
                if product_id in observed
            }
            return {
                "success": success,
                "query": original_query,
                "normalized_query": normalized_query,
                "total_count": total_count,
                "checked_product_count": checked_product_count,
                "result_complete": result_complete,
                "observations": observations,
                "missing_product_ids": (
                    [product_id for product_id in target_ids if product_id not in observed]
                    if success and result_complete
                    else []
                ),
                "error_reason": None if success else "search_bucket_observation_incomplete",
            }

        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            return result(success=False, result_complete=False)

        seen_page_signatures: set[str] = set()
        page_num = 1
        try:
            while True:
                page = self._request_ops_low_weight_search_page(
                    brand_alias,
                    normalized_query,
                    page_num,
                    page_size,
                )
                page_total = page.get("total")
                page_rows = page.get("rows")
                if (
                    not isinstance(page_total, int)
                    or isinstance(page_total, bool)
                    or page_total < 0
                    or not isinstance(page_rows, list)
                    or not all(isinstance(item, dict) for item in page_rows)
                    or (page_total > 0 and not page_rows)
                ):
                    return result(success=False, result_complete=False)
                if total_count is None:
                    total_count = page_total
                elif page_total != total_count:
                    return result(success=False, result_complete=False)
                if checked_product_count + len(page_rows) > total_count:
                    return result(success=False, result_complete=False)

                page_signature = json.dumps(
                    page_rows,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if page_signature in seen_page_signatures:
                    return result(success=False, result_complete=False)
                seen_page_signatures.add(page_signature)

                rank_offset = checked_product_count
                for index, item in enumerate(page_rows, start=1):
                    snapshot = self._bucket_snapshot_product(item)
                    product_id = snapshot.get("sku_id") or snapshot.get("product_id")
                    normalized_product_id = str(product_id or "").strip()
                    if normalized_product_id not in target_id_set or normalized_product_id in observed:
                        continue
                    observed[normalized_product_id] = {
                        "product_id": normalized_product_id,
                        "bucket": str(snapshot.get("bucket") or "").strip(),
                        "rank": rank_offset + index,
                    }
                checked_product_count += len(page_rows)
                result_complete = checked_product_count == total_count
                if len(observed) == len(target_ids) or result_complete:
                    return result(success=True, result_complete=result_complete)
                page_num += 1
        except Exception:
            return result(success=False, result_complete=False)

    def get_ops_low_weight_config(self, brand_alias: str, query: str) -> Dict[str, Any]:
        from src.governance.query_identity import transform_governance_payload_query

        original_query = str(query or "")
        normalized_query = transform_governance_payload_query(brand_alias, query)
        failure_result = {
            "success": False,
            "query": original_query,
            "normalized_query": normalized_query,
            "product_ids": [],
            "version": None,
            "error_reason": "authoritative_config_unavailable",
        }
        try:
            first_page = self._request_ops_low_weight_search_page(
                brand_alias,
                normalized_query,
                1,
            )
        except ValueError:
            return {**failure_result, "error_reason": "invalid_authoritative_config_response"}
        except RuntimeError as exc:
            error_reason = str(exc).strip()
            if error_reason in {
                "authoritative_config_http_error",
                "authoritative_config_business_error",
            }:
                return {**failure_result, "error_reason": error_reason}
            return {**failure_result, "error_reason": "authoritative_config_request_failed"}
        except Exception:
            return {**failure_result, "error_reason": "authoritative_config_request_failed"}

        def collect_product_ids(
            items: List[Dict[str, Any]],
            selected_bucket: str,
            selected_bucket_offset: int = 0,
            selected_bucket_total: Optional[int] = None,
        ) -> Optional[set[str]]:
            collected: set[str] = set()
            bucket_index = 0
            for item in items:
                snapshot = self._bucket_snapshot_product(item)
                if str(snapshot.get("bucket") or "").strip() != selected_bucket:
                    continue
                current_index = bucket_index
                bucket_index += 1
                if current_index < selected_bucket_offset:
                    continue
                if (
                    selected_bucket_total is not None
                    and current_index >= selected_bucket_offset + selected_bucket_total
                ):
                    continue
                product_id = snapshot.get("sku_id") or snapshot.get("product_id")
                if not product_id or not str(product_id).strip():
                    return None
                collected.add(str(product_id).strip())
            return collected

        def is_authoritative_ops_metadata(item: Dict[str, Any], bucket: str) -> bool:
            if bucket not in {self._OPS_LOW_WEIGHT_BUCKET, self._LEGACY_LOW_WEIGHT_BUCKET}:
                return True
            if item.get("dynamic") is True:
                return False
            action = str(item.get("action") or item.get("title") or item.get("name") or "").strip()
            expected_action = (
                "运营降低权重商品"
                if bucket == self._OPS_LOW_WEIGHT_BUCKET
                else "需要降低权重商品"
            )
            return not action or action == expected_action

        def normalize_bucket_metadata(value: Any) -> Optional[Dict[str, Dict[str, int]]]:
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                return None
            normalized: Dict[str, int] = {}
            offsets: Dict[str, int] = {}
            bucket_seen_counts: Dict[str, int] = {}
            for item in value:
                bucket_value = item.get("bucket")
                if not isinstance(bucket_value, str) or not bucket_value.strip():
                    return None
                bucket = bucket_value.strip()
                bucket_total = item.get("total")
                if (
                    not isinstance(bucket_total, int)
                    or isinstance(bucket_total, bool)
                    or bucket_total < 0
                ):
                    return None
                bucket_offset = bucket_seen_counts.get(bucket, 0)
                bucket_seen_counts[bucket] = bucket_offset + bucket_total
                if not is_authoritative_ops_metadata(item, bucket):
                    continue
                if bucket not in offsets:
                    offsets[bucket] = bucket_offset
                normalized[bucket] = normalized.get(bucket, 0) + bucket_total
            return {"totals": normalized, "offsets": offsets}

        search_links = first_page.get("searchLinks")
        first_bucket_metadata: Optional[Dict[str, Dict[str, int]]] = None
        expected_bucket_total: Optional[int] = None
        selected_bucket = self._OPS_LOW_WEIGHT_BUCKET
        ops_metadata_present = False
        if "searchLinks" not in first_page:
            return {**failure_result, "error_reason": "authoritative_config_incomplete"}
        first_bucket_metadata = normalize_bucket_metadata(search_links)
        if first_bucket_metadata is None:
            return {**failure_result, "error_reason": "invalid_authoritative_config_response"}
        first_bucket_totals = first_bucket_metadata["totals"]
        first_bucket_offsets = first_bucket_metadata["offsets"]
        available_buckets = set(first_bucket_totals)
        ops_metadata_present = self._OPS_LOW_WEIGHT_BUCKET in available_buckets
        selected_bucket = self._select_ops_low_weight_bucket(available_buckets)
        if selected_bucket not in available_buckets:
            # 上游约定：搜索链路未经过运营降权桶时 searchLinks 不返回该桶，
            # 等价于该 query 的降权集合为空。若 rows 中实际出现降权桶商品，
            # 下游 observed 校验仍会拒绝该响应。
            selected_bucket = self._OPS_LOW_WEIGHT_BUCKET
            expected_bucket_total = 0
        else:
            expected_bucket_total = first_bucket_totals[selected_bucket]
        selected_bucket_offset = first_bucket_offsets.get(selected_bucket, 0)

        rows = first_page.get("rows")
        total = first_page.get("total")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
        ):
            return {**failure_result, "error_reason": "authoritative_config_incomplete"}
        if total == 0 and expected_bucket_total not in (None, 0):
            return {**failure_result, "error_reason": "authoritative_config_incomplete"}
        safe_empty = total == 0 or (
            ops_metadata_present
            and selected_bucket == self._OPS_LOW_WEIGHT_BUCKET
            and expected_bucket_total == 0
        )
        if safe_empty:
            if rows is None:
                rows = []
            if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
                return {**failure_result, "error_reason": "invalid_authoritative_config_response"}
            if len(rows) > total:
                return {**failure_result, "error_reason": "authoritative_config_incomplete"}
            if total == 0 and rows:
                return {**failure_result, "error_reason": "authoritative_config_incomplete"}
            first_ids = collect_product_ids(
                rows,
                self._OPS_LOW_WEIGHT_BUCKET,
                selected_bucket_offset if selected_bucket == self._OPS_LOW_WEIGHT_BUCKET else 0,
                None,
            )
            if first_ids is None or first_ids:
                return {**failure_result, "error_reason": "authoritative_config_incomplete"}
            product_ids: List[str] = []
        else:
            if (
                not isinstance(rows, list)
                or not all(isinstance(item, dict) for item in rows)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total <= 0
                or total < len(rows)
                or not rows
            ):
                return {**failure_result, "error_reason": "authoritative_config_incomplete"}
            source_items = list(rows)
            collected_ids = collect_product_ids(
                rows,
                selected_bucket,
                selected_bucket_offset,
                None,
            )
            if collected_ids is None:
                return {**failure_result, "error_reason": "invalid_authoritative_config_response"}
            observed_ops_ids = collect_product_ids(rows, self._OPS_LOW_WEIGHT_BUCKET)
            if observed_ops_ids is None:
                return {**failure_result, "error_reason": "invalid_authoritative_config_response"}
            if not ops_metadata_present and observed_ops_ids:
                return {**failure_result, "error_reason": "authoritative_config_incomplete"}

            seen_page_signatures = {
                json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            }
            page_num = 1
            while len(source_items) < total:
                page_num += 1
                try:
                    page = self._request_ops_low_weight_search_page(
                        brand_alias,
                        normalized_query,
                        page_num,
                    )
                except Exception:
                    return {**failure_result, "error_reason": "authoritative_config_incomplete"}
                if "searchLinks" in page:
                    page_bucket_metadata = normalize_bucket_metadata(page.get("searchLinks"))
                    if (
                        page_bucket_metadata is None
                        or first_bucket_metadata is None
                        or page_bucket_metadata != first_bucket_metadata
                    ):
                        return {**failure_result, "error_reason": "authoritative_config_incomplete"}
                page_rows = page.get("rows")
                page_total = page.get("total")
                if (
                    page_total != total
                    or not isinstance(page_rows, list)
                    or not all(isinstance(item, dict) for item in page_rows)
                    or len(source_items) + len(page_rows) > total
                ):
                    return {**failure_result, "error_reason": "authoritative_config_incomplete"}
                if not page_rows:
                    # 上游 total 计数含被过滤商品时，翻页会提前耗尽（rows 为空）。
                    # 允许在耗尽时结束枚举；降权集合一致性由下方
                    # expected_bucket_total/collected_ids 校验兜底。
                    break
                page_signature = json.dumps(
                    page_rows,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if page_signature in seen_page_signatures:
                    return {**failure_result, "error_reason": "authoritative_config_incomplete"}
                seen_page_signatures.add(page_signature)
                next_source_items = [*source_items, *page_rows]
                next_collected_ids = collect_product_ids(
                    next_source_items,
                    selected_bucket,
                    selected_bucket_offset,
                    None,
                )
                if next_collected_ids is None:
                    return {**failure_result, "error_reason": "invalid_authoritative_config_response"}
                collected_ids = next_collected_ids
                page_ops_ids = collect_product_ids(page_rows, self._OPS_LOW_WEIGHT_BUCKET)
                if page_ops_ids is None:
                    return {**failure_result, "error_reason": "invalid_authoritative_config_response"}
                if not ops_metadata_present and page_ops_ids:
                    return {**failure_result, "error_reason": "authoritative_config_incomplete"}
                source_items = next_source_items

            if expected_bucket_total and not collected_ids:
                return {**failure_result, "error_reason": "authoritative_config_incomplete"}
            product_ids = sorted(collected_ids)
        return {
            "success": True,
            "query": original_query,
            "normalized_query": normalized_query,
            "product_ids": product_ids,
            "version": self._ops_low_weight_config_version(normalized_query, product_ids),
            "error_reason": None,
        }

    @staticmethod
    def supports_ops_low_weight_atomic_compare() -> bool:
        return False

    @staticmethod
    def supports_ops_low_weight_client_preflight() -> bool:
        return True

    def set_ops_low_weight_config(
        self,
        brand_alias: str,
        query: str,
        product_ids: List[str],
        *,
        expected_version: str,
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        from src.governance.query_identity import transform_governance_payload_query

        original_query = str(query or "")
        normalized_query = transform_governance_payload_query(brand_alias, query)
        normalized_product_ids = sorted({str(item).strip() for item in (product_ids or []) if str(item).strip()})

        def result(
            *,
            success: bool = False,
            conflict: bool = False,
            version: Optional[str] = None,
            error_reason: Optional[str],
            http_status: Optional[int] = None,
            tool_response: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            return {
                "success": success,
                "conflict": conflict,
                "query": original_query,
                "normalized_query": normalized_query,
                "product_ids": normalized_product_ids,
                "version": version,
                "error_reason": error_reason,
                "http_status": http_status,
                "tool_response": tool_response or {"code": None, "message": error_reason},
            }

        normalized_expected_version = str(expected_version or "").strip()
        if not normalized_expected_version:
            return result(error_reason="expected_version_required")

        try:
            current = self.get_ops_low_weight_config(brand_alias, normalized_query)
        except Exception as exc:
            return result(
                error_reason="authoritative_config_request_failed",
                tool_response={
                    "code": None,
                    "message": f"{type(exc).__name__}: [REDACTED]",
                },
            )
        if not isinstance(current, dict) or current.get("success") is not True:
            error_reason = (
                str(current.get("error_reason") or "authoritative_config_unavailable")
                if isinstance(current, dict)
                else "invalid_authoritative_config_response"
            )
            return result(error_reason=error_reason)
        current_version = str(current.get("version") or "").strip()
        if not current_version:
            return result(error_reason="invalid_authoritative_config_response")
        if current_version != normalized_expected_version:
            return result(
                conflict=True,
                error_reason="version_conflict",
                http_status=409,
                tool_response={"code": 409, "message": "version_conflict"},
            )

        url = f"{self._get_base_url(brand_alias)}/api/search/redis/setOpsLowWeight"
        payload = {
            self._get_api_alias(brand_alias): {
                normalized_query: normalized_product_ids,
            },
        }
        try:
            response = self._call_api(
                "POST",
                url,
                brand_alias,
                normalized_query,
                json=payload,
                headers=self._get_headers(brand_alias),
                timeout=10,
                external_request_id=external_request_id,
                redact_response_log=True,
            )
        except Exception as exc:
            return result(
                error_reason="conditional_write_request_failed",
                tool_response={
                    "code": None,
                    "message": f"{type(exc).__name__}: [REDACTED]",
                },
            )

        if response.status_code != 200:
            return result(
                error_reason="conditional_write_http_error",
                http_status=response.status_code,
                tool_response={
                    "code": response.status_code,
                    "message": "conditional_write_http_error: [REDACTED]",
                },
            )
        if not self._is_plain_text_success_response(response.text):
            return result(
                error_reason="invalid_conditional_write_response",
                http_status=response.status_code,
                tool_response={
                    "code": response.status_code,
                    "message": "invalid_conditional_write_response: [REDACTED]",
                },
            )

        return result(
            success=True,
            version=self._ops_low_weight_config_version(
                normalized_query,
                normalized_product_ids,
            ),
            error_reason=None,
            http_status=response.status_code,
            tool_response={
                "code": response.status_code,
                "message": "conditional_write_succeeded: [REDACTED]",
            },
        )

    def get_atlas_hot_words(self, limit: int = 15) -> List[Dict[str, Any]]:
        """读取 ATLAS HOT 热词并按品类配置顺序稳定去重。"""
        type_meta = {
            "1": {"category": "女士", "gender_tag": "WOMAN", "suffix": "女士"},
            "2": {"category": "男士", "gender_tag": "MAN", "suffix": "男士"},
            "3": {"category": "儿童", "gender_tag": "KID", "suffix": "儿童"},
            "4": {"category": "家居", "gender_tag": "HOME", "suffix": "家居"},
            "6": {"category": "鞋包", "gender_tag": "WOMAN", "suffix": "女士"},
        }
        hot_words: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for type_value, meta in type_meta.items():
            url = "http://cms.example.test/atlas/queryHotWord"
            try:
                response = self._call_api(
                    "GET",
                    url,
                    "atlas",
                    f"hot-word-type-{type_value}",
                    params={"type": type_value},
                    timeout=10,
                )
                payload = response.json() if response.status_code == 200 else {}
            except Exception:
                payload = {}
            if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
                continue
            groups = payload.get("data") if isinstance(payload.get("data"), list) else []
            details: List[Dict[str, Any]] = []
            for group in groups:
                if isinstance(group, dict) and isinstance(group.get("detail"), list):
                    details.extend(item for item in group["detail"] if isinstance(item, dict))
            details.sort(key=lambda item: self._safe_int(item.get("sort"), 9999))
            for item in details:
                if str(item.get("status") or "").upper() != "HOT":
                    continue
                raw_tag = str(item.get("tag") or "").strip()
                if not raw_tag:
                    continue
                suffix = str(meta["suffix"])
                query_text = raw_tag if raw_tag.endswith(("女士", "男士", "儿童", "家居")) else f"{raw_tag} {suffix}"
                normalized_key = query_text.lower()
                if normalized_key in seen:
                    continue
                seen.add(normalized_key)
                hot_words.append({
                    "query": query_text,
                    "raw_tag": raw_tag,
                    "type": type_value,
                    "category": meta["category"],
                    "gender_tag": meta["gender_tag"],
                    "status": "HOT",
                    "sort": self._safe_int(item.get("sort"), len(hot_words) + 1),
                    "sort_type": item.get("sortType") or "default",
                })
                if len(hot_words) >= max(1, int(limit)):
                    return hot_words
        return hot_words

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalized_top_product_filters(filters: Any) -> List[Dict[str, str]]:
        if not isinstance(filters, list):
            return []
        normalized = []
        for item in filters:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or item.get("dimensionName") or "").strip()
            value = str(item.get("value") or "").strip()
            if field and value:
                normalized.append({"field": field, "value": value})
        return sorted(normalized, key=lambda item: (item["field"], item["value"]))

    # Query 自定义排序（API 文档 2.10）。该接口与 AI 运营专家置顶接口
    # 使用不同的配置空间，不能混用 agent 路径。
    def list_query_sort_configs(self, brand_alias: str, keyword: str = "") -> List[Dict[str, Any]]:
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/search/keyword/page"
        response = self._call_api(
            "POST", url, brand_alias, keyword or "query-sort-list",
            json={"pageNo": 1, "pageSize": 1000, "keyword": keyword},
            headers=self._get_headers(brand_alias), timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
            raise ValueError(f"读取 Query 自定义排序配置失败: {payload}")
        data = payload.get("data")
        items = data.get("list") if isinstance(data, dict) else []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def find_query_sort_config(
        self, brand_alias: str, query: str, filters: Optional[List[Dict[str, str]]] = None,
    ) -> Optional[Dict[str, Any]]:
        keyword, derived_filters = self._split_query_by_search_rule(brand_alias, query)
        expected = self._normalized_top_product_filters(filters if filters is not None else derived_filters)
        for item in self.list_query_sort_configs(brand_alias, keyword):
            if str(item.get("keyword") or "").strip().lower() != keyword.strip().lower():
                continue
            if self._normalized_top_product_filters(item.get("filters")) == expected:
                return item
        return None

    def create_query_sort_config(
        self, brand_alias: str, query: str, filters: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        keyword, derived_filters = self._split_query_by_search_rule(brand_alias, query)
        normalized = self._normalized_top_product_filters(filters if filters is not None else derived_filters)
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/search/keyword/add"
        response = self._call_api(
            "POST", url, brand_alias, query,
            json={"keyword": keyword, "filters": normalized},
            headers=self._get_headers(brand_alias), timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None} or not payload.get("data"):
            raise ValueError(f"创建 Query 自定义排序配置失败: {payload}")
        return str(payload["data"])

    def get_query_sort_products(self, brand_alias: str, config_id: str, query: str = "") -> List[Dict[str, Any]]:
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/search/product/list"
        response = self._call_api(
            "POST", url, brand_alias, query or config_id,
            json={"id": config_id, "channelId": "default"},
            headers=self._get_headers(brand_alias), timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
            raise ValueError(f"读取 Query 自定义排序商品失败: {payload}")
        data = payload.get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def save_query_sort_products(
        self, brand_alias: str, config_id: str, products: List[Mapping[str, Any]], query: str = "",
    ) -> None:
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/search/product/add"
        normalized = []
        for index, product in enumerate(products, 1):
            item = dict(product)
            item["taggingUid"] = str(item.get("taggingUid") or "").strip()
            item["position"] = index
            if not item["taggingUid"]:
                raise ValueError("Query 自定义排序商品缺少 taggingUid")
            normalized.append(item)
        response = self._call_api(
            "POST", url, brand_alias, query or config_id,
            json={"id": config_id, "products": normalized},
            headers=self._get_headers(brand_alias), timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
            raise ValueError(f"保存 Query 自定义排序商品失败: {payload}")

    def delete_query_sort_config(self, brand_alias: str, config_id: str, query: str = "") -> None:
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/search/keyword/delete"
        response = self._call_api(
            "POST", url, brand_alias, query or config_id,
            json={"id": config_id}, headers=self._get_headers(brand_alias), timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
            raise ValueError(f"删除 Query 自定义排序配置失败: {payload}")

    def list_agent_top_product_configs(self, brand_alias: str) -> List[Dict[str, Any]]:
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/agent/keyword/list"
        response = self._call_api(
            "POST",
            url,
            brand_alias,
            "agent-top-products-list",
            headers=self._get_headers(brand_alias),
            timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
            raise ValueError(f"读取置顶商品配置失败: {payload}")
        data = payload.get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def find_agent_top_product_config(
        self,
        brand_alias: str,
        query: str,
        filters: Optional[List[Dict[str, str]]] = None,
    ) -> Optional[Dict[str, Any]]:
        keyword, derived_filters = self._split_query_by_search_rule(brand_alias, query)
        expected_filters = self._normalized_top_product_filters(filters if filters is not None else derived_filters)
        for item in self.list_agent_top_product_configs(brand_alias):
            item_keyword = str(item.get("keyword") or "").strip().lower()
            item_filters = self._normalized_top_product_filters(item.get("filters"))
            if item_keyword == keyword.strip().lower() and item_filters == expected_filters:
                return item
        return None

    def create_agent_top_product_config(
        self,
        brand_alias: str,
        query: str,
        filters: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        keyword, derived_filters = self._split_query_by_search_rule(brand_alias, query)
        normalized_filters = self._normalized_top_product_filters(filters if filters is not None else derived_filters)
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/agent/keyword/add"
        response = self._call_api(
            "POST",
            url,
            brand_alias,
            query,
            json={"keyword": keyword, "filters": normalized_filters, "sinkToBottom": False},
            headers=self._get_headers(brand_alias),
            timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None} or not payload.get("data"):
            raise ValueError(f"创建置顶商品配置失败: {payload}")
        return str(payload["data"])

    def delete_agent_top_product_config(self, brand_alias: str, config_id: str, query: str = "") -> None:
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/agent/keyword/delete"
        response = self._call_api(
            "POST",
            url,
            brand_alias,
            query,
            json={"id": config_id},
            headers=self._get_headers(brand_alias),
            timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
            raise ValueError(f"删除置顶商品配置失败: {payload}")

    def get_agent_top_products(self, brand_alias: str, config_id: str, query: str = "") -> List[Dict[str, Any]]:
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/agent/product/list"
        response = self._call_api(
            "POST",
            url,
            brand_alias,
            query,
            json={"id": config_id},
            headers=self._get_headers(brand_alias),
            timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
            raise ValueError(f"读取置顶商品失败: {payload}")
        data = payload.get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def add_agent_top_products(self, brand_alias: str, config_id: str, product_ids: List[str], query: str = "") -> None:
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/agent/product/add"
        products = [{"taggingUid": product_id, "position": index + 1} for index, product_id in enumerate(product_ids)]
        response = self._call_api(
            "POST",
            url,
            brand_alias,
            query,
            json={"id": config_id, "products": products},
            headers=self._get_headers(brand_alias),
            timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
            raise ValueError(f"写入置顶商品失败: {payload}")

    def delete_agent_top_products(self, brand_alias: str, config_id: str, product_ids: List[str], query: str = "") -> None:
        if not product_ids:
            return
        url = f"{self._get_base_url(brand_alias)}/admin-api/config/top-products/agent/product/delete"
        response = self._call_api(
            "POST",
            url,
            brand_alias,
            query,
            json={"id": config_id, "products": [{"taggingUid": item} for item in product_ids]},
            headers=self._get_headers(brand_alias),
            timeout=15,
        )
        payload = response.json() if response.status_code == 200 else {}
        if not isinstance(payload, dict) or payload.get("code") not in {0, 200, None}:
            raise ValueError(f"删除置顶商品失败: {payload}")

    def replace_agent_top_products(
        self,
        brand_alias: str,
        query: str,
        product_ids: List[str],
        filters: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        desired_ids = list(dict.fromkeys(str(item).strip().upper() for item in product_ids if str(item).strip()))
        if len(desired_ids) != 15:
            raise ValueError("AI运营专家团队必须完整写入 15 件置顶商品")
        existing = self.find_agent_top_product_config(brand_alias, query, filters)
        created = existing is None
        config_id = str(existing.get("id")) if existing else self.create_agent_top_product_config(brand_alias, query, filters)
        before_products = self.get_agent_top_products(brand_alias, config_id, query)
        before_ids = [
            str(item.get("taggingUid") or "").strip().upper()
            for item in before_products
            if str(item.get("taggingUid") or "").strip()
        ]
        rollback_succeeded = False
        try:
            self.delete_agent_top_products(brand_alias, config_id, before_ids, query)
            self.add_agent_top_products(brand_alias, config_id, desired_ids, query)
            after_products = self.get_agent_top_products(brand_alias, config_id, query)
            after_ids = [
                str(item.get("taggingUid") or "").strip().upper()
                for item in after_products
                if str(item.get("taggingUid") or "").strip()
            ]
            if after_ids != desired_ids:
                raise ValueError(f"置顶商品配置校验失败: expected={desired_ids}, actual={after_ids}")
            verified_bucket_ids: List[str] = []
            verified_mixed_ranking: List[str] = []
            for _attempt in range(10):
                snapshot = self.search_products_with_total(brand_alias, query, page_size=15)
                verified_bucket_ids = [
                    str(item.get("taggingUid") or item.get("productId") or item.get("spuId") or item.get("spu") or item.get("skuId") or "").strip().upper()
                    for item in snapshot.get("products") or []
                    if ((item.get("ext") or {}).get("bucket") if isinstance(item.get("ext"), dict) else item.get("bucket")) == "productIdsByAgent"
                ]
                verified_mixed_ranking = [
                    str(item.get("taggingUid") or item.get("productId") or item.get("spuId") or item.get("spu") or item.get("skuId") or "").strip().upper()
                    for item in snapshot.get("products") or []
                ]
                if verified_bucket_ids == desired_ids and verified_mixed_ranking[:15] == desired_ids:
                    break
                time.sleep(1)
            else:
                raise ValueError(
                    "置顶商品顺序验证超时: "
                    f"expected={desired_ids}, agent_bucket={verified_bucket_ids}, mixed={verified_mixed_ranking[:15]}"
                )
            return {
                "config_id": config_id,
                "created_config": created,
                "before_products": before_products,
                "after_products": after_products,
                "product_ids": desired_ids,
                "verified_bucket_ids": verified_bucket_ids,
                "verified_mixed_ranking": verified_mixed_ranking[:15],
            }
        except Exception as execution_error:
            try:
                current_products = self.get_agent_top_products(brand_alias, config_id, query)
                current_ids = [
                    str(item.get("taggingUid") or "").strip().upper()
                    for item in current_products
                    if str(item.get("taggingUid") or "").strip()
                ]
                self.delete_agent_top_products(brand_alias, config_id, current_ids, query)
                if created:
                    self.delete_agent_top_product_config(brand_alias, config_id, query)
                elif before_ids:
                    self.add_agent_top_products(brand_alias, config_id, before_ids, query)
                rollback_succeeded = True
            except Exception as rollback_error:
                logging.error("置顶商品执行失败且回滚失败: %s", rollback_error)
            error = ValueError(str(execution_error))
            error.details = {
                "config_id": config_id,
                "before_products": before_products,
                "requested_product_ids": desired_ids,
                "rolled_back": rollback_succeeded,
            }
            raise error from execution_error

    @staticmethod
    def _bucket_snapshot_product(item: Dict[str, Any]) -> Dict[str, Any]:
        ext = item.get("ext") if isinstance(item.get("ext"), dict) else {}
        product_id = item.get("taggingUid") or item.get("productId") or item.get("spuId") or item.get("spu") or item.get("skuId")
        sku_id = item.get("skuId") or product_id
        return {
            "product_id": product_id,
            "sku_id": sku_id,
            "bucket": ext.get("bucket") or item.get("bucket"),
            "raw": item,
        }

    def retag_products(
        self,
        brand_alias: str,
        external_request_id: Optional[str] = None,
    ) -> bool:
        """触发品牌商品重新打标签。"""
        if brand_alias == "summit":
            logging.warning("Summit 暂未配置重新打标签接口，跳过自动调用。")
            return False

        url = f"{self._get_base_url(brand_alias)}/admin-api/search/tag/retagging"
        try:
            resp = self._call_api(
                "POST",
                url,
                brand_alias,
                "no_result_retagging",
                headers=self._get_headers(brand_alias),
                timeout=30,
                external_request_id=external_request_id,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            ok = data.get("code") in {0, 200} or data.get("code") is None
            if ok:
                return True
            return False
        except Exception as e:
            logging.error(f"重新打标签接口异常 ({brand_alias}): {e}")
            if external_request_id:
                raise
            return False

    def apply_tag_bucket_config(
        self,
        brand_alias: str,
        payload: Dict[str, Any],
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行 Query 级标签权重调整配置。

        默认使用标签权重查询/新增/更新三接口。保留 `TAG_BUCKET_CONFIG_URL`
        或 `TAG_BUCKET_CONFIG_PATH` 作为调试覆盖入口。
        """
        from src.governance.query_policy import assert_query_governance_allowed

        keyword = str(payload.get("keyword") or payload.get("query") or "").strip()
        if keyword:
            assert_query_governance_allowed(brand_alias, keyword)
        if not self.supports_tag_bucket_config(brand_alias):
            return {"code": -1, "message": f"tag bucket config unsupported for brand: {brand_alias}"}

        normalized_payload = self._normalize_tag_bucket_payload(brand_alias, payload)
        if not normalized_payload.get("keyword") or not isinstance(normalized_payload.get("filters"), list) or not isinstance(normalized_payload.get("configs"), list):
            return {"code": -1, "message": "invalid tag bucket config payload"}

        configured_url = os.getenv("TAG_BUCKET_CONFIG_URL")
        configured_path = os.getenv("TAG_BUCKET_CONFIG_PATH")
        if configured_url:
            return self._post_tag_bucket_config(
                brand_alias,
                configured_url,
                normalized_payload,
                external_request_id=external_request_id,
            )
        elif configured_path:
            url = f"{self._get_base_url(brand_alias)}{configured_path if configured_path.startswith('/') else '/' + configured_path}"
            return self._post_tag_bucket_config(
                brand_alias,
                url,
                normalized_payload,
                external_request_id=external_request_id,
            )

        try:
            base_url = self._get_base_url(brand_alias)
            query_url = f"{base_url}/admin-api/config/query-tags/page"
            add_url = f"{base_url}/admin-api/config/query-tags/add"
            update_url = f"{base_url}/admin-api/config/query-tags/update"
            query_payload = {"keyword": str(normalized_payload.get("keyword") or ""), "pageNo": 1, "pageSize": 20}

            query_resp = self._call_api(
                "POST",
                query_url,
                brand_alias,
                str(normalized_payload.get("keyword") or ""),
                json=query_payload,
                headers=self._get_headers(brand_alias),
                timeout=15,
            )
            query_result = self._decode_json_response(query_resp)
            if query_result.get("code") != 0:
                return {"code": -1, "message": f"query-tags/page failed: {query_result.get('message')}", "tool_response": query_result}

            existing_config = self._tag_bucket_existing_config(query_result.get("data"), normalized_payload)
            if existing_config:
                request_payload = self._merge_tag_bucket_config(existing_config, normalized_payload)
                return self._post_tag_bucket_config(
                    brand_alias,
                    update_url,
                    request_payload,
                    external_request_id=external_request_id,
                )
            return self._post_tag_bucket_config(
                brand_alias,
                add_url,
                normalized_payload,
                external_request_id=external_request_id,
            )
        except Exception as e:
            logging.error(f"标签权重调整接口异常 ({brand_alias}): {e}")
            if external_request_id:
                raise
            return {"code": -1, "message": str(e)}

    def _post_tag_bucket_config(
        self,
        brand_alias: str,
        url: str,
        payload: Dict[str, Any],
        external_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        resp = self._call_api(
            "POST",
            url,
            brand_alias,
            str(payload.get("keyword") or ""),
            json=payload,
            headers=self._get_headers(brand_alias),
            timeout=15,
            external_request_id=external_request_id,
        )
        result = self._decode_json_response(resp)
        if result.get("code") != 0:
            return {"code": -1, "message": result.get("message") or "tag bucket config failed", "tool_response": result}
        return result

    def _decode_json_response(self, resp: requests.Response) -> Dict[str, Any]:
        if resp.status_code != 200:
            return {"code": -1, "message": f"http_status={resp.status_code}", "response": resp.text}
        try:
            data = resp.json()
        except Exception:
            return {"code": 0, "message": resp.text} if self._is_plain_text_success_response(resp.text) else {"code": -1, "message": resp.text}
        if isinstance(data, dict):
            if data.get("code") in {0, 200, None}:
                return data
            return {"code": data.get("code"), "message": data.get("msg") or data.get("message") or "business error", "data": data.get("data")}
        return {"code": 0, "data": data, "message": "success"}

    def _tag_bucket_config_exists(self, data: Any, payload: Dict[str, Any]) -> bool:
        return bool(self._tag_bucket_existing_config(data, payload))

    def _tag_bucket_existing_config(self, data: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        items = data.get("list") if isinstance(data, dict) else []
        if not isinstance(items, list):
            return {}
        keyword = str(payload.get("keyword") or "")
        filters = payload.get("filters") or []
        normalized_filters = self._normalize_filters(filters)
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("keyword") or "") == keyword and self._normalize_filters(item.get("filters") or []) == normalized_filters:
                return item
        return {}

    def _merge_tag_bucket_config(self, existing: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        merged_configs = []
        seen = set()
        for config in [*(existing.get("configs") or []), *(payload.get("configs") or [])]:
            if not isinstance(config, dict):
                continue
            normalized = {
                "tags": str(config.get("tags") or "").strip(),
                "afterBucket": str(config.get("afterBucket") or "").strip(),
            }
            if not normalized["tags"] or not normalized["afterBucket"]:
                continue
            key = (normalized["tags"], normalized["afterBucket"])
            if key in seen:
                continue
            seen.add(key)
            merged_configs.append(normalized)
        return {
            "keyword": str(payload.get("keyword") or existing.get("keyword") or ""),
            "filters": payload.get("filters") if isinstance(payload.get("filters"), list) else existing.get("filters") or [],
            "configs": merged_configs,
        }

    @staticmethod
    def _normalize_filters(filters: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        normalized = []
        for item in filters or []:
            if isinstance(item, dict):
                normalized.append({"field": str(item.get("field") or ""), "value": str(item.get("value") or "")})
        return sorted(normalized, key=lambda item: (item["field"], item["value"]))

    def get_tag_bucket_config(self, brand_alias: str, keyword: str, filters: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.supports_tag_bucket_config(brand_alias):
            return {
                "status": "unsupported",
                "matched": False,
                "config": {},
                "tool_response": {"code": -1, "message": f"tag bucket config unsupported for brand: {brand_alias}"},
            }

        normalized_payload = self._normalize_tag_bucket_payload(brand_alias, {"keyword": keyword, "filters": filters or [], "configs": []})
        query_payload = {"keyword": str(normalized_payload.get("keyword") or ""), "pageNo": 1, "pageSize": 20}
        query_url = f"{self._get_base_url(brand_alias)}/admin-api/config/query-tags/page"
        query_resp = self._call_api(
            "POST",
            query_url,
            brand_alias,
            str(normalized_payload.get("keyword") or ""),
            json=query_payload,
            headers=self._get_headers(brand_alias),
            timeout=15,
        )
        query_result = self._decode_json_response(query_resp)
        if query_result.get("code") != 0:
            return {"status": "error", "matched": False, "config": {}, "tool_response": query_result}

        existing = self._tag_bucket_existing_config(query_result.get("data"), normalized_payload)
        return {
            "status": "available" if existing else "not_found",
            "matched": bool(existing),
            "config": existing or {},
            "tool_response": query_result,
        }

    def query_available_tags(self, brand_alias: str, tags: List[str]) -> Dict[str, Any]:
        unique_tags: List[str] = []
        seen = set()
        for tag in tags or []:
            text = str(tag or "").strip()
            if text and text not in seen:
                seen.add(text)
                unique_tags.append(text)
        if not unique_tags:
            return {"status": "no_candidate_tags", "existing_tags": [], "missing_tags": [], "tool_response": []}

        existing_tags = []
        missing_tags = []
        tool_response = []
        for tag in unique_tags:
            response = self.kg_client.execute({
                "brand": brand_alias,
                "actionType": "query_tag",
                "params": {
                    "nameAcc": tag,
                    "pageNum": 1,
                    "pageSize": 10,
                },
            })
            tool_response.append(response)
            data = response.get("data") if isinstance(response, dict) else None
            matched = data[0] if isinstance(data, list) and data else None
            if matched:
                existing_tags.append({
                    "name": tag,
                    "normalized_name": str(matched.get("tagName") or matched.get("viewTag") or tag),
                    "available": True,
                })
            else:
                missing_tags.append({"name": tag, "normalized_name": tag, "available": False})

        return {
            "status": "available" if existing_tags else "not_found",
            "existing_tags": existing_tags,
            "missing_tags": missing_tags,
            "tool_response": tool_response,
        }

    def add_to_blacklist(
        self,
        brand_alias: str,
        product_ids: str,
        blacklist_words: List[str],
        external_request_id: Optional[str] = None,
    ) -> bool:
        """1.1 批量增加黑名单 - /admin-api/search/tag/batch/add/black/list"""
        from src.governance.query_policy import assert_query_governance_allowed
        from src.governance.query_identity import transform_governance_payload_query

        for query_text in blacklist_words:
            assert_query_governance_allowed(brand_alias, query_text)

        if brand_alias == "summit":
            success = True
            for pid in product_ids.split(";"):
                if pid.strip():
                    if external_request_id:
                        updated = self.summit_service.update_blacklist(
                            pid.strip(),
                            blacklist_words,
                            delete=False,
                            external_request_id=self._child_external_request_id(
                                external_request_id,
                                "blacklist_add",
                                pid.strip(),
                            ),
                        )
                    else:
                        updated = self.summit_service.update_blacklist(
                            pid.strip(),
                            blacklist_words,
                            delete=False,
                        )
                    if not updated:
                        success = False
            return success

        url = f"{self._get_base_url(brand_alias)}/admin-api/search/tag/batch/add/black/list"
        
        from src.config import SEARCH_RULES
        rule = SEARCH_RULES.get(brand_alias)
        
        final_blacklist = []
        if rule and rule.get("enable_tag_parsing"):
            tag_map = rule.get("tag_map", {})
            for word in blacklist_words:
                parts = word.strip().split()
                if len(parts) >= 2 and parts[-1] in tag_map:
                    keyword = " ".join(parts[:-1])
                    final_blacklist.append(keyword)
                else:
                    final_blacklist.append(word)
        else:
            final_blacklist = blacklist_words
        final_blacklist = [transform_governance_payload_query(brand_alias, word) for word in final_blacklist]

        payload = {
            "productIds": product_ids,
            "channelId": "default",
            "blackList": final_blacklist
        }
        headers = self._get_headers(brand_alias)
        
        logging.info(f"🚀 [拉黑接口请求] Brand: {brand_alias}, SPU: {product_ids}, Words: {final_blacklist}")

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._call_api(
                    "POST",
                    url,
                    brand_alias,
                    final_blacklist[0] if final_blacklist else None,
                    json=payload,
                    headers=headers,
                    timeout=60,
                    external_request_id=external_request_id,
                )
                if resp.status_code == 200 and resp.json().get("code") in [0, 200]:
                    logging.info(f"✅ [拉黑接口成功] Brand: {brand_alias}, SPU: {product_ids}")
                    return True
                logging.warning(f"❌ [拉黑接口失败] HTTP {resp.status_code}, Resp: {resp.text}, Attempt {attempt+1}/3")
            except Exception as e:
                last_error = e
                logging.error(f"拉黑接口网络异常: {e}, 重试 {attempt+1}")
            time.sleep(1)
        if external_request_id and last_error is not None:
            raise last_error
        return False

    def remove_from_blacklist(
        self,
        brand_alias: str,
        product_ids: str,
        blacklist_words: List[str],
        external_request_id: Optional[str] = None,
    ) -> bool:
        """1.2 批量删除黑名单 - /admin-api/search/tag/batch/remove/black/list"""
        from src.governance.query_policy import assert_query_governance_allowed
        from src.governance.query_identity import transform_governance_payload_query

        for query_text in blacklist_words:
            assert_query_governance_allowed(brand_alias, query_text)

        if brand_alias == "summit":
            success = True
            for pid in product_ids.split(";"):
                if pid.strip():
                    if external_request_id:
                        updated = self.summit_service.update_blacklist(
                            pid.strip(),
                            blacklist_words,
                            delete=True,
                            external_request_id=self._child_external_request_id(
                                external_request_id,
                                "blacklist_remove",
                                pid.strip(),
                            ),
                        )
                    else:
                        updated = self.summit_service.update_blacklist(
                            pid.strip(),
                            blacklist_words,
                            delete=True,
                        )
                    if not updated:
                        success = False
            return success

        url = f"{self._get_base_url(brand_alias)}/admin-api/search/tag/batch/remove/black/list"
        
        from src.config import SEARCH_RULES
        rule = SEARCH_RULES.get(brand_alias)
        
        final_blacklist = []
        if rule and rule.get("enable_tag_parsing"):
            tag_map = rule.get("tag_map", {})
            for word in blacklist_words:
                parts = word.strip().split()
                if len(parts) >= 2 and parts[-1] in tag_map:
                    keyword = " ".join(parts[:-1])
                    final_blacklist.append(keyword)
                else:
                    final_blacklist.append(word)
        else:
            final_blacklist = blacklist_words
        final_blacklist = [transform_governance_payload_query(brand_alias, word) for word in final_blacklist]

        payload = {
            "productIds": product_ids,
            "channelId": "default",
            "blackList": final_blacklist
        }
        headers = self._get_headers(brand_alias)
        
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._call_api(
                    "POST",
                    url,
                    brand_alias,
                    final_blacklist[0] if final_blacklist else None,
                    json=payload,
                    headers=headers,
                    timeout=10,
                    external_request_id=external_request_id,
                )
                if resp.status_code == 200 and resp.json().get("code") in [0, 200]:
                    return True
            except Exception as e:
                last_error = e
                logging.error(f"移出黑名单异常: {e}")
            time.sleep(1)
        if external_request_id and last_error is not None:
            raise last_error
        return False

    def _normalize_whitelist_words(self, brand_alias: str, whitelist_words: List[str]) -> List[str]:
        from src.config import SEARCH_RULES
        from src.governance.query_identity import transform_governance_payload_query

        rule = SEARCH_RULES.get(brand_alias)
        final_whitelist: List[str] = []
        if rule and rule.get("enable_tag_parsing"):
            tag_map = rule.get("tag_map", {})
            for word in whitelist_words:
                parts = str(word or "").strip().split()
                if len(parts) >= 2 and parts[-1] in tag_map:
                    keyword = " ".join(parts[:-1])
                    final_whitelist.append(keyword)
                else:
                    final_whitelist.append(str(word or "").strip())
        else:
            final_whitelist = [str(word or "").strip() for word in whitelist_words]
        return [
            transform_governance_payload_query(brand_alias, word)
            for word in final_whitelist
            if str(word or "").strip()
        ]

    def add_to_whitelist(
        self,
        brand_alias: str,
        product_ids: str,
        whitelist_words: List[str],
        external_request_id: Optional[str] = None,
        channel_id: str = "default",
    ) -> bool:
        """2.5.3 批量增加白名单 - POST /admin-api/search/tag/batch/add/white/list"""
        from src.governance.query_policy import assert_query_governance_allowed

        for query_text in whitelist_words:
            assert_query_governance_allowed(brand_alias, query_text)

        clean_skus = [sku.strip() for sku in str(product_ids or "").split(";") if sku.strip()]
        if not clean_skus:
            return False

        if brand_alias == "summit":
            success = True
            for pid in clean_skus:
                child_id = (
                    self._child_external_request_id(external_request_id, "whitelist_add", pid)
                    if external_request_id
                    else None
                )
                if not self.summit_service.update_whitelist(
                    pid,
                    whitelist_words,
                    delete=False,
                    external_request_id=child_id,
                ):
                    success = False
            return success

        final_whitelist = self._normalize_whitelist_words(brand_alias, whitelist_words)
        if not final_whitelist:
            return False

        url = f"{self._get_base_url(brand_alias)}/admin-api/search/tag/batch/add/white/list"
        headers = self._get_headers(brand_alias)

        for chunk_index in range(0, len(clean_skus), 100):
            chunk = clean_skus[chunk_index : chunk_index + 100]
            payload = {
                "productIds": ";".join(chunk),
                "channelId": channel_id or "default",
                "whiteList": final_whitelist,
            }
            chunk_req_id = (
                external_request_id
                if len(clean_skus) <= 100
                else self._child_external_request_id(external_request_id, "whitelist_add_chunk", str(chunk_index))
                if external_request_id
                else None
            )
            last_error: Optional[Exception] = None
            chunk_ok = False
            for attempt in range(self.max_retries):
                try:
                    resp = self._call_api(
                        "POST",
                        url,
                        brand_alias,
                        final_whitelist[0],
                        json=payload,
                        headers=headers,
                        timeout=60,
                        external_request_id=chunk_req_id,
                    )
                    if resp.status_code == 200:
                        body = resp.json()
                        if isinstance(body, dict) and body.get("code") in [0, 200]:
                            data = body.get("data")
                            if isinstance(data, dict):
                                result_status = str(data.get("resultStatus") or "").upper()
                                if result_status in {"", "ALL_SUCCESS"} or int(data.get("failureCount") or 0) == 0:
                                    chunk_ok = True
                                    break
                            else:
                                chunk_ok = True
                                break
                except Exception as e:
                    last_error = e
                    logging.error(f"白名单接口网络异常: {e}, 重试 {attempt+1}")
                time.sleep(1)
            if not chunk_ok:
                if external_request_id and last_error is not None:
                    raise last_error
                return False
        return True

    def remove_from_whitelist(
        self,
        brand_alias: str,
        product_ids: str,
        whitelist_words: List[str],
        external_request_id: Optional[str] = None,
        channel_id: str = "default",
    ) -> bool:
        """2.5.4 批量移除白名单 - POST /admin-api/search/tag/batch/remove/white/list"""
        from src.governance.query_policy import assert_query_governance_allowed

        for query_text in whitelist_words:
            assert_query_governance_allowed(brand_alias, query_text)

        clean_skus = [sku.strip() for sku in str(product_ids or "").split(";") if sku.strip()]
        if not clean_skus:
            return False

        if brand_alias == "summit":
            success = True
            for pid in clean_skus:
                child_id = (
                    self._child_external_request_id(external_request_id, "whitelist_remove", pid)
                    if external_request_id
                    else None
                )
                if not self.summit_service.update_whitelist(
                    pid,
                    whitelist_words,
                    delete=True,
                    external_request_id=child_id,
                ):
                    success = False
            return success

        final_whitelist = self._normalize_whitelist_words(brand_alias, whitelist_words)
        if not final_whitelist:
            return False

        url = f"{self._get_base_url(brand_alias)}/admin-api/search/tag/batch/remove/white/list"
        headers = self._get_headers(brand_alias)
        payload = {
            "productIds": ";".join(clean_skus[:100]),
            "channelId": channel_id or "default",
            "whiteList": final_whitelist,
        }
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._call_api(
                    "POST",
                    url,
                    brand_alias,
                    final_whitelist[0],
                    json=payload,
                    headers=headers,
                    timeout=15,
                    external_request_id=external_request_id,
                )
                if resp.status_code == 200 and resp.json().get("code") in [0, 200]:
                    return True
            except Exception as e:
                last_error = e
                logging.error(f"移除白名单异常: {e}")
            time.sleep(1)
        if external_request_id and last_error is not None:
            raise last_error
        return False

    def validate_whitelist_tags(
        self,
        brand_alias: str,
        tags: List[str],
    ) -> bool:
        """2.5.7 白名单标签校验接口 - GET /admin-api/search/tag/white/list/tag/validate"""
        clean_tags = [str(tag).strip() for tag in (tags or []) if str(tag or "").strip()]
        if not clean_tags or brand_alias == "summit":
            return False
        url = f"{self._get_base_url(brand_alias)}/admin-api/search/tag/white/list/tag/validate"
        try:
            resp = self._call_api(
                "GET",
                url,
                brand_alias,
                ",".join(clean_tags),
                params=[("tags", tag) for tag in clean_tags],
                headers=self._get_headers(brand_alias),
                timeout=15,
            )
            if resp.status_code == 200:
                body = resp.json()
                return isinstance(body, dict) and body.get("code") in [0, 200] and body.get("data") is True
        except Exception as e:
            logging.error(f"白名单标签校验异常 ({brand_alias}): {e}")
        return False

    def add_whitelist_tags(
        self,
        brand_alias: str,
        product_ids: str,
        whitelist_tags: List[str],
        external_request_id: Optional[str] = None,
        channel_id: str = "default",
        validate_before_write: bool = True,
    ) -> bool:
        """2.5.5 白名单标签批量增加接口 - POST /admin-api/search/tag/batch/add/white/list/tag"""
        clean_skus = list(dict.fromkeys(sku.strip() for sku in str(product_ids or "").split(";") if sku.strip()))
        clean_tags = list(dict.fromkeys(str(tag).strip() for tag in (whitelist_tags or []) if str(tag or "").strip()))
        if not clean_skus or not clean_tags:
            return False
        if brand_alias == "summit":
            return False
        if validate_before_write and not self.validate_whitelist_tags(brand_alias, clean_tags):
            logging.warning(f"白名单标签校验未通过 ({brand_alias}): {clean_tags}")
            return False

        url = f"{self._get_base_url(brand_alias)}/admin-api/search/tag/batch/add/white/list/tag"
        headers = self._get_headers(brand_alias)
        for chunk_index in range(0, len(clean_skus), 1000):
            chunk = clean_skus[chunk_index : chunk_index + 1000]
            payload = {
                "productIds": ";".join(chunk),
                "channelId": channel_id or "default",
                "whiteListTag": clean_tags,
            }
            chunk_req_id = (
                external_request_id
                if len(clean_skus) <= 1000
                else self._child_external_request_id(external_request_id, "whitelist_tag_add_chunk", str(chunk_index))
                if external_request_id
                else None
            )
            last_error: Optional[Exception] = None
            chunk_ok = False
            for attempt in range(self.max_retries):
                try:
                    resp = self._call_api(
                        "POST",
                        url,
                        brand_alias,
                        ",".join(clean_tags),
                        json=payload,
                        headers=headers,
                        timeout=60,
                        external_request_id=chunk_req_id,
                    )
                    if resp.status_code == 200:
                        body = resp.json()
                        if isinstance(body, dict) and body.get("code") in [0, 200]:
                            chunk_ok = True
                            break
                except Exception as e:
                    last_error = e
                    logging.error(f"白名单标签增加接口异常: {e}, 重试 {attempt+1}")
                time.sleep(1)
            if not chunk_ok:
                if external_request_id and last_error is not None:
                    raise last_error
                return False
        return True

    def remove_whitelist_tags(
        self,
        brand_alias: str,
        product_ids: str,
        whitelist_tags: List[str],
        external_request_id: Optional[str] = None,
        channel_id: str = "default",
    ) -> bool:
        """2.5.6 白名单标签批量移除接口 - POST /admin-api/search/tag/batch/remove/white/list/tag"""
        clean_skus = list(dict.fromkeys(sku.strip() for sku in str(product_ids or "").split(";") if sku.strip()))
        clean_tags = list(dict.fromkeys(str(tag).strip() for tag in (whitelist_tags or []) if str(tag or "").strip()))
        if not clean_skus or not clean_tags or brand_alias == "summit":
            return False
        url = f"{self._get_base_url(brand_alias)}/admin-api/search/tag/batch/remove/white/list/tag"
        payload = {
            "productIds": ";".join(clean_skus[:1000]),
            "channelId": channel_id or "default",
            "whiteListTag": clean_tags,
        }
        headers = self._get_headers(brand_alias)
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._call_api(
                    "POST",
                    url,
                    brand_alias,
                    ",".join(clean_tags),
                    json=payload,
                    headers=headers,
                    timeout=30,
                    external_request_id=external_request_id,
                )
                if resp.status_code == 200 and resp.json().get("code") in [0, 200]:
                    return True
            except Exception as e:
                last_error = e
                logging.error(f"白名单标签移除接口异常: {e}")
            time.sleep(1)
        if external_request_id and last_error is not None:
            raise last_error
        return False

    def retag_selected_products(
        self,
        brand_alias: str,
        product_ids: str,
        channel_id: str = "default",
        external_request_id: Optional[str] = None,
    ) -> bool:
        """2.6.3 指定商品重新打标签接口 - POST /admin-api/search/tag/retagging/selected"""
        clean_skus = list(dict.fromkeys(sku.strip() for sku in str(product_ids or "").split(";") if sku.strip()))
        if not clean_skus or brand_alias == "summit":
            return False
        url = f"{self._get_base_url(brand_alias)}/admin-api/search/tag/retagging/selected"
        payload = {
            "productIds": ";".join(clean_skus),
            "channelId": channel_id or "default",
        }
        try:
            resp = self._call_api(
                "POST",
                url,
                brand_alias,
                "retagging_selected",
                json=payload,
                headers=self._get_headers(brand_alias),
                timeout=30,
                external_request_id=external_request_id,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            return isinstance(data, dict) and (data.get("code") in {0, 200} or data.get("code") is None)
        except Exception as e:
            logging.error(f"指定商品重新打标签接口异常 ({brand_alias}): {e}")
            if external_request_id:
                raise
            return False

    def sync_broken_code_to_redis(
        self,
        brand_alias: str,
        add_items: Dict[str, List[str]] = None,
        remove_items: Dict[str, List[str]] = None,
        external_request_id: Optional[str] = None,
        *,
        allow_prod_recovery: bool = False,
    ) -> bool:
        """2. 商品分层调整接口 (核心同步逻辑) - /api/search/redis/setOpsLowWeight
        重要：此接口目前在后端可能返回 404（待下周上线），因此不应计入熔断。
        """
        from src.governance.query_policy import assert_queries_governance_allowed
        from src.governance.query_identity import transform_governance_payload_query

        assert_queries_governance_allowed(
            brand_alias,
            list(set((add_items or {})) | set((remove_items or {}))),
        )

        if brand_alias == "summit":
            logging.info(f"Summit does not support stock demotion, skipping redis sync.")
            return True

        prod_recovery = self.env == "prod" and allow_prod_recovery
        if self.env == "prod":
            has_additions = any(
                str(item).strip()
                for product_ids in (add_items or {}).values()
                for item in (product_ids or [])
            )
            has_removals = any(
                str(item).strip()
                for product_ids in (remove_items or {}).values()
                for item in (product_ids or [])
            )
            if not prod_recovery or has_additions or not has_removals:
                logging.error(
                    "Query 降桶兼容入口已在生产禁用，仅允许显式的历史降桶恢复，brand=%s",
                    brand_alias,
                )
                return False
            logging.warning(
                "Query 降桶正在执行生产恢复兼容路径，仅允许移除并强制回读，brand=%s",
                brand_alias,
            )

        if (
            self.supports_ops_low_weight_atomic_compare() is not True
            and not prod_recovery
        ):
            logging.error(
                "Query 降桶写入被阻断：服务端不支持原子版本比较，brand=%s",
                brand_alias,
            )
            return False

        add_items = add_items or {}
        remove_items = remove_items or {}
        query_changes: Dict[str, Dict[str, set[str]]] = {}
        for operation, source in (("add", add_items), ("remove", remove_items)):
            for raw_query, product_ids in source.items():
                normalized_query = transform_governance_payload_query(
                    brand_alias,
                    raw_query,
                )
                changes = query_changes.setdefault(
                    normalized_query,
                    {"add": set(), "remove": set()},
                )
                changes[operation].update(
                    str(item).strip()
                    for item in (product_ids or [])
                    if str(item).strip()
                )
        if not query_changes:
            return True
        if len(query_changes) != 1:
            logging.error(
                "Query 降桶写入被阻断：兼容接口不支持多 Query 原子提交，brand=%s",
                brand_alias,
            )
            return False

        try:
            for normalized_query, changes in query_changes.items():
                current = self.get_ops_low_weight_config(
                    brand_alias,
                    normalized_query,
                )
                if (
                    not isinstance(current, dict)
                    or current.get("success") is not True
                    or not isinstance(current.get("product_ids"), list)
                    or not isinstance(current.get("version"), str)
                    or not current["version"].strip()
                ):
                    return False
                desired_ids = sorted(
                    ({
                        str(item).strip()
                        for item in current["product_ids"]
                        if str(item).strip()
                    } | changes["add"]) - changes["remove"]
                )
                write_result = self.set_ops_low_weight_config(
                    brand_alias,
                    normalized_query,
                    desired_ids,
                    expected_version=current["version"].strip(),
                    external_request_id=external_request_id,
                )
                write_version = (
                    write_result.get("version")
                    if isinstance(write_result, dict)
                    else None
                )
                if (
                    not isinstance(write_result, dict)
                    or write_result.get("success") is not True
                    or write_result.get("conflict") is not False
                    or not isinstance(write_version, str)
                    or not write_version.strip()
                ):
                    return False
                readback = self.get_ops_low_weight_config(
                    brand_alias,
                    normalized_query,
                )
                if (
                    not isinstance(readback, dict)
                    or readback.get("success") is not True
                    or not isinstance(readback.get("product_ids"), list)
                    or sorted({
                        str(item).strip()
                        for item in readback["product_ids"]
                        if str(item).strip()
                    }) != desired_ids
                    or str(readback.get("version") or "").strip()
                    != write_version.strip()
                ):
                    return False
            return True
        except Exception as e:
            logging.error("权威分层同步失败: %s", type(e).__name__)
            if external_request_id:
                raise
            return False

    def set_broken_code(self, brand_alias: str, query: str, sku_list: List[str]) -> bool:
        """向下兼容或单次调用降权，内部自动走全量同步逻辑"""
        return self.sync_broken_code_to_redis(brand_alias, add_items={query: sku_list})

    def get_top_query_action_status(
        self,
        *,
        action_type: str,
        external_request_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        brand = str(payload.get("brand") or "").strip()
        query = str(payload.get("query") or payload.get("keyword") or "").strip()
        if not brand or not external_request_id:
            return {"status": "unknown", "reason": "missing_action_identity"}
        if action_type == "query_demote":
            product_ids = {
                str(item).strip()
                for item in (payload.get("product_ids") or payload.get("skus") or [])
                if str(item).strip()
            }
            if not query or not product_ids:
                return {"status": "unknown", "reason": "missing_query_or_product_ids"}
            try:
                active = {
                    str(item).strip()
                    for item in self.get_current_ops_low_weight_skus(brand, query)
                    if str(item).strip()
                }
            except Exception as exc:
                return {"status": "unknown", "reason": str(exc)}
            return {
                "status": "completed" if product_ids.issubset(active) else "not_found",
                "response": {"active_product_ids": sorted(active)},
            }
        if action_type == "product_blacklist":
            from src.governance.query_identity import transform_governance_payload_query

            raw_product_ids = payload.get("product_ids") or payload.get("skus") or []
            if not raw_product_ids and payload.get("product_id") is not None:
                raw_product_ids = [payload.get("product_id")]
            product_ids = [
                str(item).strip()
                for item in raw_product_ids
                if str(item).strip()
            ]
            expected_query = transform_governance_payload_query(brand, query)
            if not expected_query or not product_ids:
                return {"status": "unknown", "reason": "missing_query_or_product_ids"}

            # The product list endpoint supports filtering by Query blacklist
            # directly. This is the authoritative readback for product-level
            # blacklist actions; do not issue one detail request per product.
            url = f"{self._get_base_url(brand)}/admin-api/search/product/list"
            rows: list[Dict[str, Any]] = []
            page_no = 1
            page_size = 100
            try:
                while True:
                    response = self._call_api(
                        "POST",
                        url,
                        brand,
                        expected_query,
                        json={
                            "blackList": expected_query,
                            "pageNo": page_no,
                            "pageSize": page_size,
                        },
                        headers=self._get_headers(brand),
                        timeout=60,
                    )
                    if response.status_code != 200:
                        return {
                            "status": "unknown",
                            "reason": f"blacklist_list_readback_http_{response.status_code}",
                        }
                    body = response.json()
                    if not isinstance(body, dict) or body.get("code") not in (None, 0, 200):
                        return {
                            "status": "unknown",
                            "reason": "blacklist_list_readback_business_error",
                        }
                    data = body.get("data")
                    if not isinstance(data, dict):
                        return {
                            "status": "unknown",
                            "reason": "blacklist_list_readback_invalid_response",
                        }
                    page_rows = data.get("list") or data.get("rows") or []
                    if not isinstance(page_rows, list) or not all(isinstance(item, dict) for item in page_rows):
                        return {
                            "status": "unknown",
                            "reason": "blacklist_list_readback_invalid_response",
                        }
                    rows.extend(page_rows)
                    total = data.get("total")
                    if not isinstance(total, int) or total <= len(rows) or len(page_rows) < page_size:
                        break
                    page_no += 1
            except Exception:
                return {
                    "status": "unknown",
                    "reason": "blacklist_list_readback_request_failed",
                }

            available_ids: set[str] = set()
            for row in rows:
                for key in ("productId", "product_id", "spu", "spuId", "skuId", "sku_id", "taggingUid", "attr1", "id"):
                    value = row.get(key)
                    if value is None or not str(value).strip():
                        continue
                    normalized = str(value).strip()
                    available_ids.add(normalized)
                    if "_" in normalized:
                        available_ids.add(normalized.split("_", 1)[1])
            matched_product_ids = [product_id for product_id in product_ids if product_id in available_ids]

            if len(matched_product_ids) == len(product_ids):
                return {
                    "status": "completed",
                    "response": {
                        "readback_source": "product_list_blacklist_filter",
                        "matched_product_ids": matched_product_ids,
                    },
                }
            if not matched_product_ids:
                return {
                    "status": "not_found",
                    "response": {
                        "readback_source": "product_list_blacklist_filter",
                        "matched_product_ids": [],
                    },
                }
            return {
                "status": "unknown",
                "reason": "partial_product_blacklist_readback",
                "response": {
                    "readback_source": "product_list_blacklist_filter",
                    "matched_product_ids": matched_product_ids,
                    "unmatched_product_ids": [
                        product_id for product_id in product_ids if product_id not in matched_product_ids
                    ],
                },
            }
        if action_type == "tag_bucket_config":
            filters = payload.get("filters") if isinstance(payload.get("filters"), list) else []
            expected_configs = payload.get("configs") if isinstance(payload.get("configs"), list) else []
            if not query or not expected_configs:
                return {"status": "unknown", "reason": "missing_query_or_configs"}
            try:
                readback = self.get_tag_bucket_config(brand, query, filters)
            except Exception as exc:
                return {"status": "unknown", "reason": str(exc)}
            if not isinstance(readback, dict):
                return {"status": "unknown", "reason": "invalid_readback"}
            if readback.get("status") == "not_found":
                return {"status": "not_found", "response": readback}
            actual_config = readback.get("config") if isinstance(readback.get("config"), dict) else {}
            actual_configs = actual_config.get("configs") if isinstance(actual_config.get("configs"), list) else []
            expected = json.dumps(expected_configs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            actual = json.dumps(actual_configs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if readback.get("matched") is True and expected == actual:
                return {"status": "completed", "response": readback}
            if readback.get("status") == "available":
                return {"status": "not_found", "response": readback}
            return {"status": "unknown", "response": readback}
        return {"status": "unknown", "reason": f"unsupported_action_type:{action_type}"}

    @staticmethod
    def calculate_broken_rate(size_rows: List[Dict]) -> Optional[float]:
        valid_sizes = [size for size in size_rows if size.get("name")]
        if not valid_sizes:
            return None
        sellable_statuses = {"in_stock", "low_on_stock"}
        broken_count = sum(1 for size in valid_sizes if size.get("availability") not in sellable_statuses)
        return round(broken_count / len(valid_sizes), 4)

    @staticmethod
    def _size_inventory_payload(size_rows: List[Dict], source: str) -> Optional[Dict]:
        valid_rows = []
        for row in size_rows:
            name = str(row.get("name") or row.get("size") or row.get("sizeName") or row.get("code") or "").strip()
            if not name:
                continue
            availability = str(row.get("availability") or "").strip()
            normalized = dict(row)
            normalized["name"] = name
            normalized["availability"] = availability
            valid_rows.append(normalized)
        if not valid_rows:
            return None

        sellable_statuses = {"in_stock", "low_on_stock"}
        available_rows = [row for row in valid_rows if row.get("availability") in sellable_statuses]
        unavailable_rows = [row for row in valid_rows if row.get("availability") not in sellable_statuses]
        low_stock_rows = [row for row in valid_rows if row.get("availability") == "low_on_stock"]
        broken_rate = round(len(unavailable_rows) / len(valid_rows), 4)
        return {
            "total_size_count": len(valid_rows),
            "available_size_count": len(available_rows),
            "broken_size_count": len(unavailable_rows),
            "broken_rate": broken_rate,
            "available_sizes": [row["name"] for row in available_rows],
            "unavailable_sizes": [row["name"] for row in unavailable_rows],
            "low_stock_sizes": [row["name"] for row in low_stock_rows],
            "size_rows": valid_rows,
            "source": source,
        }

    def get_atlas_broken_rate(self, product_id: str) -> Optional[float]:
        """查询 ATLAS 商品断码率，返回 0~1 的比例。"""
        inventory = self.get_atlas_size_inventory(product_id)
        return inventory.get("broken_rate") if inventory else None

    def get_atlas_size_inventory(self, product_id: str) -> Optional[Dict]:
        atlas_product_id = str(product_id).split(',')[0].strip()
        if not atlas_product_id:
            return None

        url = f"https://www.atlas.example.test/atlasrest/4/catalog/store/11716/product/id/{atlas_product_id}"
        headers = {
            "atlas-appid": os.getenv("ATLAS_WECHAT_APP_ID", ""),
            "xweb_xhr": "1",
            "User-Agent": "Mozilla/5.0",
        }
        start_time = time.time()
        try:
            resp = self._call_api(
                "GET",
                url,
                "atlas",
                atlas_product_id,
                headers=headers,
                params={"locale": "zh_CN"},
                timeout=15,
            )
            if resp.status_code != 200:
                try:
                    save_operation_log({
                        "brand": "atlas",
                        "query": atlas_product_id,
                        "module": "OperationalService",
                        "action": "GET atlas_size_detail",
                        "request": json.dumps({"product_id": atlas_product_id, "url": url}, ensure_ascii=False),
                        "response": json.dumps({"status_code": resp.status_code, "body": str(getattr(resp, "text", ""))[:300]}, ensure_ascii=False),
                        "status": "failed",
                        "duration": time.time() - start_time,
                        "error": f"http_status={resp.status_code}",
                    })
                except Exception:
                    logging.exception("记录 ATLAS 尺码接口失败日志时出错")
                logging.warning(f"ATLAS 尺码详情接口返回异常: {resp.status_code}, product_id={atlas_product_id}")
                return None
            colors = resp.json().get("detail", {}).get("colors", [])
            if not colors:
                return None
            return self._size_inventory_payload(colors[0].get("sizes", []), "atlas_detail_api")
        except Exception as e:
            try:
                save_operation_log({
                    "brand": "atlas",
                    "query": atlas_product_id,
                    "module": "OperationalService",
                    "action": "GET atlas_size_detail",
                    "request": json.dumps({"product_id": atlas_product_id, "url": url}, ensure_ascii=False),
                    "status": "failed",
                    "duration": time.time() - start_time,
                    "error": str(e),
                })
            except Exception:
                logging.exception("记录 ATLAS 尺码接口异常日志时出错")
            logging.error(f"获取 ATLAS 尺码明细失败 (ID: {atlas_product_id}): {e}")
            return None

    @staticmethod
    def _log_atlas_size_detail_skip(product: Dict) -> None:
        save_operation_log({
            "brand": "atlas",
            "query": str(product.get("productId") or product.get("skuId") or ""),
            "module": "OperationalService",
            "action": "GET atlas_size_detail",
            "request": json.dumps({
                "productId": product.get("productId"),
                "spuId": product.get("spuId"),
                "skuId": product.get("skuId"),
                "attr1": product.get("attr1"),
                "productName": product.get("productName") or product.get("name"),
            }, ensure_ascii=False),
            "status": "skipped",
            "duration": 0,
            "error": "missing_numeric_atlas_detail_id",
        })

    def get_harbor_broken_rate(self, article_number: str) -> Optional[float]:
        inventory = self.get_harbor_size_inventory(article_number)
        return inventory.get("broken_rate") if inventory else None

    def get_harbor_size_inventory(self, article_number: str) -> Optional[Dict]:
        harbor_article_number = str(article_number).split(',')[0].strip()
        if not harbor_article_number:
            return None

        url = f"https://spapi.harbor.example.test/api/h5app/wxapp/goods/items/{harbor_article_number}"
        params = {
            "type": "sku",
            "showError": "false",
            "company_id": "1",
            "country_code": "zh-CN",
            "store_sn": "001",
        }
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            resp = self._call_api(
                "GET",
                url,
                "harbor",
                harbor_article_number,
                params=params,
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                logging.warning(f"HARBOR 尺码详情接口返回异常: {resp.status_code}, article_number={harbor_article_number}")
                return None
            variants = resp.json().get("data", {}).get("variant", [])
            if not variants:
                return None
            size_rows = []
            for variant in variants:
                stock = variant.get("store") or 0
                try:
                    stock = float(stock)
                except (TypeError, ValueError):
                    stock = 0
                name = (
                    variant.get("sizeName")
                    or variant.get("spec_value_name")
                    or variant.get("size")
                    or variant.get("name")
                    or variant.get("code")
                )
                size_rows.append({
                    **variant,
                    "name": name,
                    "code": variant.get("code"),
                    "availability": "in_stock" if stock > 0 else "out_of_stock",
                    "stock": stock,
                })
            return self._size_inventory_payload(size_rows, "harbor_goods_items_api")
        except Exception as e:
            logging.error(f"获取 HARBOR 尺码明细失败 (ID: {harbor_article_number}): {e}")
            return None

    def get_riviera_broken_rate(self, spu_code: str) -> Optional[float]:
        inventory = self.get_riviera_size_inventory(spu_code)
        return inventory.get("broken_rate") if inventory else None

    def get_riviera_size_inventory(self, spu_code: str) -> Optional[Dict]:
        rl_spu_code = str(spu_code).split(',')[0].strip()
        if not rl_spu_code:
            return None

        search_url = "https://www.riviera.example.test/apis/rl/product/v2/pdps/search"
        inventory_url = "https://www.riviera.example.test/apis/product/inventory/get/netQty.do"
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        try:
            search_resp = self._call_api(
                "GET",
                search_url,
                "riviera",
                rl_spu_code,
                params={"spuCode": rl_spu_code},
                headers=headers,
                timeout=15,
            )
            if search_resp.status_code != 200:
                logging.warning(f"Riviera PDP 接口返回异常: {search_resp.status_code}, spu_code={rl_spu_code}")
                return None
            product_rows = search_resp.json().get("data", {}).get("list", [])
            selected_row = self._select_riviera_product_row(product_rows, rl_spu_code)
            if not selected_row:
                return None
            sku_list = selected_row.get("skuList", [])
            sku_codes = []
            for item in sku_list:
                sku_code = item.get("skuCode") or item.get("code") or item.get("externalSkuCode")
                if sku_code:
                    sku_codes.append(str(sku_code).strip())
            if not sku_codes:
                return None

            inventory_resp = self._call_api(
                "POST",
                inventory_url,
                "riviera",
                rl_spu_code,
                headers=headers,
                json={"skuCodeList": sku_codes},
                timeout=15,
            )
            if inventory_resp.status_code != 200:
                logging.warning(f"Riviera 库存接口返回异常: {inventory_resp.status_code}, spu_code={rl_spu_code}")
                return None
            inventory_data = inventory_resp.json().get("data", {})
            inventory_map = {}
            for item in inventory_data.get("successList") or []:
                sku_code = item.get("extentionCode") or item.get("skuCode")
                if sku_code:
                    inventory_map[str(sku_code).strip()] = item
            size_rows = []
            sku_by_code = {}
            for item in sku_list:
                sku_code = item.get("skuCode") or item.get("code") or item.get("externalSkuCode")
                if sku_code:
                    sku_by_code[str(sku_code).strip()] = item
            for sku_code in sku_codes:
                sku = sku_by_code.get(sku_code, {})
                sku_stock = inventory_map.get(sku_code, inventory_data.get(sku_code, {}))
                net_qty = sku_stock.get("netQty") or 0
                try:
                    net_qty = float(net_qty)
                except (TypeError, ValueError):
                    net_qty = 0
                name = sku.get("sizeName") or sku.get("size") or sku.get("name") or sku_code
                size_rows.append({
                    **sku,
                    "name": name,
                    "code": sku_code,
                    "availability": "in_stock" if net_qty > 0 else "out_of_stock",
                    "stock": net_qty,
                })
            return self._size_inventory_payload(size_rows, "riviera_pdp_inventory_api")
        except Exception as e:
            logging.error(f"获取 Riviera 尺码明细失败 (ID: {rl_spu_code}): {e}")
            return None

    @staticmethod
    def _select_riviera_product_row(product_rows: List[Dict], spu_code: str) -> Optional[Dict]:
        if not product_rows:
            return None

        target = str(spu_code).strip()
        for row in product_rows:
            sku_list = row.get("skuList") or []
            for sku in sku_list:
                item_code = str(sku.get("itemCode") or "").strip()
                sku_code = str(sku.get("skuCode") or sku.get("code") or sku.get("externalSkuCode") or "").strip()
                if item_code == target or sku_code.startswith(target):
                    return row
        return product_rows[0]

    @staticmethod
    def _get_inventory_detail_id(brand_alias: str, product: Dict) -> Optional[str]:
        if brand_alias == "atlas":
            attr1 = product.get("attr1")
            if attr1:
                return str(attr1)
            for field in ("productId", "spuId", "spu", "skuId"):
                value = product.get(field)
                if value and str(value).split(",")[0].strip().isdigit():
                    return str(value)
            return None

        candidate_fields = {
            "harbor": ("productId", "spuId", "skuId", "spu", "attr1"),
            "riviera": ("productId", "spuId", "skuId", "spu", "attr1"),
        }.get(brand_alias, ("productId", "spuId", "skuId", "spu", "attr1"))

        for field in candidate_fields:
            value = product.get(field)
            if value:
                return str(value)
        return None

    def get_product_broken_rate(self, brand_alias: str, product: Dict) -> Optional[float]:
        detail_product_id = self._get_inventory_detail_id(brand_alias, product)
        if not detail_product_id:
            if brand_alias == "atlas":
                self._log_atlas_size_detail_skip(product)
            return None
        if brand_alias == "atlas":
            return self.get_atlas_broken_rate(detail_product_id)
        if brand_alias == "harbor":
            return self.get_harbor_broken_rate(detail_product_id)
        if brand_alias == "riviera":
            return self.get_riviera_broken_rate(detail_product_id)
        return None

    def get_product_size_inventory(self, brand_alias: str, product: Dict) -> Optional[Dict]:
        detail_product_id = self._get_inventory_detail_id(brand_alias, product)
        if not detail_product_id:
            if brand_alias == "atlas":
                self._log_atlas_size_detail_skip(product)
            return None
        if brand_alias == "atlas":
            return self.get_atlas_size_inventory(detail_product_id)
        if brand_alias == "harbor":
            return self.get_harbor_size_inventory(detail_product_id)
        if brand_alias == "riviera":
            return self.get_riviera_size_inventory(detail_product_id)
        return None

    def find_product_broken_rate(self, brand_alias: str, query: str, product_id: str) -> Optional[float]:
        if brand_alias not in {"atlas", "harbor", "riviera"}:
            return None

        products = self.search_products(brand_alias, query)
        for product in products:
            candidates = {
                str(product.get("spuId") or ""),
                str(product.get("spu") or ""),
                str(product.get("productId") or ""),
                str(product.get("skuId") or ""),
                str(product.get("attr1") or ""),
            }
            if str(product_id) in candidates:
                return self.get_product_broken_rate(brand_alias, product)
        return self.get_product_broken_rate(brand_alias, {"productId": product_id})

    def get_query_blacklisted_product_ids(self, brand_alias: str, query: str):
        from src.governance.query_identity import transform_governance_payload_query
        keyword = transform_governance_payload_query(brand_alias, query)
        url = f"{self._get_base_url(brand_alias)}/admin-api/search/product/list"
        identities = set()
        try:
            for page in range(1, 101):
                response = self._call_api("POST", url, brand_alias, keyword,
                    json={"blackList": keyword, "pageNo": page, "pageSize": 100},
                    headers=self._get_headers(brand_alias), timeout=20)
                body = response.json()
                if response.status_code != 200 or body.get("code") not in (None, 0, 200):
                    return None
                data = body.get("data")
                if not isinstance(data, dict):
                    return None
                rows = data.get("list") if "list" in data else data.get("rows")
                if not isinstance(rows, list):
                    return None
                for row in rows:
                    identities.update(self._product_identifiers(row))
                total = data.get("total")
                if isinstance(total, int):
                    if len(identities) >= total:
                        return identities
                    if not rows:
                        return None
                    continue
                if len(rows) < 100:
                    return identities
            return None
        except Exception:
            return None

    def get_product_tag_sources(self, brand_alias: str, record_id: str):
        response = self._call_api("GET", f"{self._get_base_url(brand_alias)}/admin-api/search/product/showTag",
            brand_alias, "kg_product_tags", params={"id": record_id}, headers=self._get_headers(brand_alias), timeout=20)
        body = response.json()
        if response.status_code != 200 or body.get("code") not in (0, 200) or not isinstance(body.get("data"), dict):
            raise RuntimeError("商品标签及推理信息查询失败")
        return body["data"]

    def get_product_status_map(self, brand_alias: str, product_ids: List[str]) -> Optional[Dict[str, Dict[str, Any]]]:
        """Batch query product/list and map every returned product identifier to online status."""
        clean_ids = [str(product_id).strip() for product_id in product_ids if str(product_id).strip()]
        if not clean_ids:
            return {}
        if brand_alias == "summit":
            detail_result = self.summit_service.get_product_details_with_status(
                ";".join(clean_ids)
            )
            if not detail_result.get("success"):
                return None
            return self._build_product_status_map(detail_result["products"])

        url = f"{self._get_base_url(brand_alias)}/admin-api/search/product/list"
        try:
            rows: List[Dict[str, Any]] = []
            chunk_size = 50 if brand_alias == "riviera" else 100
            timeout_seconds = 20 if brand_alias == "riviera" else 10
            for offset in range(0, len(clean_ids), chunk_size):
                chunk = clean_ids[offset: offset + chunk_size]
                payload = {
                    "spu": ";".join(chunk),
                    "pageNum": 1,
                    "pageSize": len(chunk),
                }
                resp = self._call_api(
                    "POST",
                    url,
                    brand_alias,
                    "batch_product_status",
                    json=payload,
                    headers=self._get_headers(brand_alias),
                    timeout=timeout_seconds,
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                res_code = data.get("code")
                if res_code not in [0, 200, None]:
                    return None
                body = data.get("data") or {}
                rows.extend(body.get("list") or body.get("rows") or [])
            return self._build_product_status_map(rows)
        except Exception as e:
            logging.error(f"批量获取 {brand_alias} 商品状态异常: {e}")
            return None

    def _build_product_status_map(self, products: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        status_map: Dict[str, Dict[str, Any]] = {}
        for product in products or []:
            if not isinstance(product, dict):
                continue
            entry = {
                "exists": True,
                "is_online": self._product_is_online(product),
                "raw": product,
            }
            for identifier in self._product_identifiers(product):
                status_map[identifier] = entry
        return status_map

    @staticmethod
    def _product_identifiers(product: Dict[str, Any]) -> List[str]:
        identifiers = []
        for key in ("spu", "spuId", "productId", "product_id", "skuId", "sku_id", "modelCode", "code"):
            value = product.get(key)
            if value is not None and str(value).strip():
                identifiers.append(str(value).strip())
        return list(dict.fromkeys(identifiers))

    @staticmethod
    def _product_is_online(product: Dict[str, Any]) -> bool:
        status_value = product.get("status")
        if status_value is None:
            status_value = product.get("state") or product.get("shelfStatus") or product.get("saleStatus") or product.get("isOnShelf")
        if status_value is None:
            return True
        if isinstance(status_value, bool):
            return status_value
        normalized = str(status_value).strip().lower()
        if normalized in {"0", "false", "off", "offline", "off_shelf", "offshelf", "down", "disabled", "deleted", "sold_out", "下架", "已下架"}:
            return False
        return True

    def get_product_details(self, brand_alias: str, product_id: str) -> List[Dict]:
        """5. 库存查询接口 (通过产品列表获取) - /admin-api/search/product/list"""
        if brand_alias == "summit":
            raw_details = self.summit_service.get_product_details(product_id)
            refined = []
            for item in raw_details:
                # Summit结构中，models 列表中的 modelCode 才是我们要追踪的 ID
                models = item.get("models", [])
                if models:
                    m = models[0]
                    refined.append({
                        "productId": m.get("modelCode"),
                        "skuId": m.get("modelCode"),
                        "inventory": m.get("inventory") or item.get("inventory") or 0,
                        "status": item.get("status")
                    })
            return refined

        url = f"{self._get_base_url(brand_alias)}/admin-api/search/product/list"
        candidates = [item.strip() for item in str(product_id).split(",") if item.strip()]
        for candidate in candidates:
            payload = {
                "spu": candidate,
                "pageNum": 1,
                "pageSize": 20
            }
            try:
                resp = self._call_api("POST", url, brand_alias, candidate, json=payload, headers=self._get_headers(brand_alias), timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    res_code = data.get("code")
                    if res_code in [0, 200] or res_code is None:
                        result = data.get("data", {}).get("list", [])
                        if result:
                            return result
            except Exception as e:
                logging.error(f"获取 {brand_alias} 详情接口异常 (ID: {candidate}): {e}")
        return []

    def get_raw_product_details(self, brand_alias: str, product_id: str) -> List[Dict]:
        """Return the full read-only product payload used by semantic checks.

        ``get_product_details`` intentionally returns a small inventory-oriented
        shape for the existing governance UI.  Manual relevance checks need the
        original name, images, category and descriptive fields, so this method
        keeps the business API response intact while preserving the existing
        brand-specific routing.
        """
        if brand_alias == "summit":
            result = self.summit_service.get_product_details_with_status(product_id)
            return list(result.get("products") or []) if result.get("success") else []
        return self.get_product_details(brand_alias, product_id)

    @staticmethod
    def _child_external_request_id(base_request_id: str, action: str, item: str) -> str:
        material = f"{base_request_id}|{action}|{item}".encode("utf-8")
        return f"child:{hashlib.sha256(material).hexdigest()}"
