import datetime
import concurrent.futures
from typing import Any, Dict, List, Optional

from src.persistence import (
    ActionLog,
    InventoryRecoveryTask,
    SessionLocal,
    TopQueryActionPlan,
    TopQueryTask,
    append_top_query_context_snapshot,
    append_top_query_step_status,
)
from src.persistence.inventory_recovery_dual_write import read_inventory_recovery_tasks
from src.config import get_brand_business_config
from src.integrations.operational_service import OperationalService
from .world_knowledge import WorldKnowledgeBatchSnapshot, WorldKnowledgeService


class SearchEvidenceUnavailable(RuntimeError):
    pass


class TopQueryContextCollector:
    def __init__(
        self,
        check_top_n: int = 16,
        service: Optional[OperationalService] = None,
        world_knowledge_service: Optional[WorldKnowledgeService] = None,
        world_knowledge_snapshot: Optional[WorldKnowledgeBatchSnapshot] = None,
        today_provider: Optional[Any] = None,
    ):
        self.check_top_n = check_top_n
        self.service = service or OperationalService()
        self.world_knowledge_service = world_knowledge_service or WorldKnowledgeService()
        self.world_knowledge_snapshot = world_knowledge_snapshot
        self.today_provider = today_provider or datetime.date.today

    def collect(self, task_id: int, persist: bool = True) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            task = db.get(TopQueryTask, task_id)
            if not task:
                raise ValueError(f"top query task not found: {task_id}")
            if self.world_knowledge_snapshot is not None:
                if self.world_knowledge_snapshot.brand != task.brand:
                    raise ValueError(
                        f"snapshot brand mismatch: expected {task.brand}, "
                        f"got {self.world_knowledge_snapshot.brand}"
                    )
                if self.world_knowledge_snapshot.data_date != task.data_date:
                    raise ValueError(
                        f"snapshot data_date mismatch: expected {task.data_date}, "
                        f"got {self.world_knowledge_snapshot.data_date}"
                    )
            if persist:
                task.status = "collecting_context" # type: ignore
                db.commit()

            business_config = get_brand_business_config(task.brand)
            search_result = getattr(self.service, "search_products_with_total", None)
            if callable(search_result):
                resolved_search_result = search_result(
                    task.brand,
                    task.query_text,
                    page_size=self.check_top_n,
                )
                if not isinstance(resolved_search_result, dict):
                    raise SearchEvidenceUnavailable("invalid_search_response")
                if resolved_search_result.get("success") is not True:
                    if resolved_search_result.get("success") is not False:
                        raise SearchEvidenceUnavailable("invalid_search_response")
                    reason = str(resolved_search_result.get("error_reason") or "search_failed")
                    raise SearchEvidenceUnavailable(reason)
                raw_products = resolved_search_result.get("products")
                if not isinstance(raw_products, list) or not all(
                    isinstance(product, dict) for product in raw_products
                ):
                    raise SearchEvidenceUnavailable("invalid_search_response")
                products = list(raw_products)
                raw_total_count = resolved_search_result.get("total_count")
                if raw_total_count is None:
                    result_count = None
                    result_count_known = False
                elif (
                    isinstance(raw_total_count, int)
                    and not isinstance(raw_total_count, bool)
                    and raw_total_count >= len(products)
                ):
                    result_count = raw_total_count
                    result_count_known = True
                else:
                    raise SearchEvidenceUnavailable("invalid_search_response")
                raw_result_complete = resolved_search_result.get("result_complete")
                if not isinstance(raw_result_complete, bool):
                    raise SearchEvidenceUnavailable("invalid_search_response")
                if raw_result_complete and (
                    raw_total_count is None or raw_total_count != len(products)
                ):
                    raise SearchEvidenceUnavailable("invalid_search_response")
                result_complete = raw_result_complete
                search_failure_reason = resolved_search_result.get("error_reason")
            else:
                products = self.service.search_products(task.brand, task.query_text, page_size=self.check_top_n)
                if not isinstance(products, list) or not all(
                    isinstance(product, dict) for product in products
                ):
                    raise SearchEvidenceUnavailable("invalid_search_response")
                result_count = None
                result_count_known = False
                result_complete = False
                search_failure_reason = None
            observed_result_count = len(products)
            summarized_products = self._summarize_products(
                task.brand,
                task.query_text,
                products[:self.check_top_n],
            )
            result_count_baseline = self._result_count_baseline(db, task)
            previous_result_count = result_count_baseline["previous_result_count"]
            seven_day_average = result_count_baseline["seven_day_average"]
            history_sample_count = result_count_baseline["history_sample_count"]
            result_count_history = result_count_baseline["result_count_history"]
            snapshot = {
                "success": True,
                "result_count": result_count,
                "result_count_known": result_count_known,
                "observed_result_count": observed_result_count,
                "result_complete": result_complete,
                "failure_reason": search_failure_reason,
                "previous_result_count": previous_result_count,
                "seven_day_average": seven_day_average,
                "history_sample_count": history_sample_count,
                "result_count_history": result_count_history,
                "products": summarized_products,
            }
            if persist:
                append_top_query_context_snapshot(task_id, "before_search", snapshot)
            world_knowledge_args = {
                "brand": task.brand,
                "query": task.query_text,
                "today": self.today_provider(),
                "products": summarized_products,
                "config": business_config.get("top_query_world_knowledge_config") or {},
            }
            if self.world_knowledge_snapshot is not None:
                world_knowledge_args.update({
                    "today": self.world_knowledge_snapshot.knowledge_date,
                    "config": self.world_knowledge_snapshot.effective_config(),
                    "snapshot": self.world_knowledge_snapshot,
                })
            open_factors = self.world_knowledge_service.collect(**world_knowledge_args)
            tag_bucket_api_supported = bool(self.service.supports_tag_bucket_config(task.brand))
            tag_bucket_payload = open_factors.get("tag_bucket_config") if isinstance(open_factors, dict) else None
            tag_bucket_target = open_factors.get("tag_bucket_target") if isinstance(open_factors, dict) else None
            tag_bucket_lookup = tag_bucket_target if isinstance(tag_bucket_target, dict) else tag_bucket_payload
            tag_bucket_filters = tag_bucket_lookup.get("filters") if isinstance(tag_bucket_lookup, dict) else []
            tag_bucket_keyword = str((tag_bucket_lookup or {}).get("keyword") or task.query_text or "").strip()
            tag_bucket_query_result = (
                self.service.get_tag_bucket_config(task.brand, tag_bucket_keyword, tag_bucket_filters)
                if tag_bucket_api_supported and tag_bucket_keyword and isinstance(tag_bucket_filters, list)
                else {
                    "status": "unsupported" if not tag_bucket_api_supported else "not_found",
                    "matched": False,
                    "config": {},
                    "tool_response": {} if tag_bucket_api_supported else {"code": -1, "message": f"tag bucket config unsupported for brand: {task.brand}"},
                }
            )
            query_semantic_tag_validation = (
                self.service.query_available_tags(task.brand, [tag_bucket_keyword])
                if tag_bucket_keyword else {"status": "not_found", "existing_tags": [], "missing_tags": [], "tool_response": []}
            )
            tag_catalog_validation = self.service.query_available_tags(
                task.brand,
                open_factors.get("suggested_tags") or [],
            ) if isinstance(open_factors, dict) else {"status": "no_candidate_tags", "existing_tags": [], "missing_tags": [], "tool_response": []}
            context = {
                "task": self._task_payload(task),
                "search": {
                    "success": True,
                    "result_count": result_count,
                    "result_count_known": result_count_known,
                    "observed_result_count": observed_result_count,
                    "result_complete": result_complete,
                    "failure_reason": search_failure_reason,
                    "previous_result_count": previous_result_count,
                    "seven_day_average": seven_day_average,
                    "history_sample_count": history_sample_count,
                    "result_count_history": result_count_history,
                    "check_top_n": self.check_top_n,
                },
                "previous_result_count": previous_result_count,
                "seven_day_average": seven_day_average,
                "history_sample_count": history_sample_count,
                "result_count_history": result_count_history,
                "products": summarized_products,
                "history": self._load_history(db, task),
                "open_factors": open_factors,
                "tag_bucket_target": tag_bucket_target,
                "tag_bucket_config": open_factors.get("tag_bucket_config"),
                "tag_bucket_api_supported": tag_bucket_api_supported,
                "tag_bucket_current_config": tag_bucket_query_result,
                "tag_bucket_query_result": tag_bucket_query_result,
                "query_semantic_tag_validation": query_semantic_tag_validation,
                "tag_catalog_validation": tag_catalog_validation,
            }
            if persist:
                task.status = "planning" # type: ignore
                task.result_count = result_count # type: ignore
                db.commit()
            return context
        except Exception as exc:
            db.rollback()
            if persist:
                append_top_query_step_status(task_id, {
                    "check_key": "context",
                    "status": "check_failed",
                    "display_status": "still_problematic",
                    "summary": f"上下文采集失败: {exc}",
                    "evidence": {"error": str(exc)},
                })
            raise
        finally:
            db.close()

    def _summarize_products(self, brand: str, query: str, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not products:
            return []
        summarized: List[Optional[Dict[str, Any]]] = [None] * len(products)
        worker_count = min(len(products), 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_index = {
                executor.submit(
                    self._summarize_single_product,
                    brand,
                    query,
                    index,
                    product,
                ): index - 1
                for index, product in enumerate(products, start=1)
            }
            for future in concurrent.futures.as_completed(future_to_index):
                summarized[future_to_index[future]] = future.result()
        return [item for item in summarized if item is not None]

    def _summarize_single_product(
        self,
        brand: str,
        query: str,
        rank: int,
        product: Dict[str, Any],
    ) -> Dict[str, Any]:
        product_payload = {
            "rank": rank,
            "product_id": product.get("productId") or product.get("spuId") or product.get("spu") or product.get("skuId"),
            "sku_id": product.get("skuId"),
            "name": product.get("productName") or product.get("name") or product.get("webLabel"),
            "price": self._safe_float(product.get("price")),
            "originalPrice": self._first_positive_float(
                product.get("originalPrice"),
                product.get("original_price"),
                product.get("price"),
            ),
            "discountPrice": self._first_positive_float(
                product.get("discountPrice"),
                product.get("discount_price"),
                product.get("salePrice"),
                product.get("sale_price"),
            ),
            "discount": self._first_positive_float(
                product.get("discount"),
                product.get("discountRate"),
                product.get("discount_rate"),
            ),
            "tag_name": product.get("tagName"),
            "main_image": product.get("mainImage") or product.get("imageUrl"),
            "mainImage": product.get("mainImage") or product.get("imageUrl"),
            "bucket": (product.get("ext") or {}).get("bucket") if isinstance(product.get("ext"), dict) else product.get("bucket"),
        }
        return product_payload

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _first_positive_float(cls, *values) -> Optional[float]:
        for value in values:
            numeric = cls._safe_float(value)
            if numeric is not None and numeric > 0:
                return numeric
        return None

    @staticmethod
    def _previous_result_count(db, task: TopQueryTask) -> Optional[int]:
        previous = db.query(TopQueryTask).filter(
            TopQueryTask.brand == task.brand,
            TopQueryTask.query_text == task.query_text,
            TopQueryTask.data_date < task.data_date if task.data_date else TopQueryTask.id != task.id,
            TopQueryTask.result_count.isnot(None),
        ).order_by(TopQueryTask.data_date.desc(), TopQueryTask.id.desc()).first()
        return int(previous.result_count) if previous and previous.result_count is not None else None

    @staticmethod
    def _result_count_baseline(db, task: TopQueryTask) -> Dict[str, Any]:
        query = db.query(TopQueryTask).filter(
            TopQueryTask.brand == task.brand,
            TopQueryTask.query_text == task.query_text,
            TopQueryTask.result_count.isnot(None),
        )
        if task.data_date:
            query = query.filter(TopQueryTask.data_date < task.data_date)
        else:
            query = query.filter(TopQueryTask.id != task.id)
        rows = query.order_by(TopQueryTask.data_date.desc(), TopQueryTask.id.desc()).all()
        result_count_history: List[int] = []
        seen_dates = set()
        for row in rows:
            date_key = row.data_date or f"id:{row.id}"
            if date_key in seen_dates:
                continue
            seen_dates.add(date_key)
            result_count_history.append(int(row.result_count or 0))
            if len(result_count_history) >= 7:
                break
        sample_count = len(result_count_history)
        average = round(sum(result_count_history) / sample_count, 2) if sample_count else None
        return {
            "previous_result_count": result_count_history[0] if result_count_history else None,
            "seven_day_average": average,
            "history_sample_count": sample_count,
            "result_count_history": result_count_history,
        }


    @staticmethod
    def _query_tokens(query: str) -> List[str]:
        query = str(query or "").strip()
        if not query:
            return []
        parts = [part.strip() for part in query.replace("，", " ").replace(",", " ").split() if part.strip()]
        return parts or [query]

    @staticmethod
    def _task_payload(task: TopQueryTask) -> Dict[str, Any]:
        return {
            "id": task.id,
            "brand": task.brand,
            "data_date": task.data_date.isoformat() if task.data_date else None,
            "query_text": task.query_text,
            "query_rank": task.query_rank,
            "search_uv": task.search_uv,
            "search_pv": task.search_pv,
            "ctr": task.ctr,
            "cvr": task.cvr,
            "avg_ctr": task.avg_ctr,
            "avg_cvr": task.avg_cvr,
            "query_pv_share": task.query_pv_share,
        }

    @staticmethod
    def _load_history(db, task: TopQueryTask) -> Dict[str, Any]:
        recent_actions = db.query(ActionLog).filter(
            ActionLog.brand == task.brand,
            ActionLog.query_text == task.query_text,
        ).order_by(ActionLog.created_at.desc()).limit(20).all()
        inventory_tasks = read_inventory_recovery_tasks(
            db,
            brand=task.brand,
            query_text=task.query_text,
            limit=20,
        )
        top_query_actions = db.query(TopQueryActionPlan).filter(
            TopQueryActionPlan.brand == task.brand,
            TopQueryActionPlan.query_text == task.query_text,
        ).order_by(TopQueryActionPlan.created_at.desc()).limit(20).all()
        return {
            "action_logs": [
                {
                    "action_type": action.action_type,
                    "product_id": action.product_id,
                    "reason": action.reason,
                    "created_at": action.created_at.isoformat() if action.created_at else None,
                }
                for action in recent_actions
            ],
            "inventory_recovery_tasks": [
                {
                    "product_id": item.product_id,
                    "status": item.status,
                    "last_broken_rate_percent": item.last_check_stock,
                }
                for item in inventory_tasks
            ],
            "top_query_actions": [
                {
                    "tool_key": action.tool_key,
                    "status": action.status,
                    "execution_mode": action.execution_mode,
                    "reason": action.reason,
                }
                for action in top_query_actions
            ],
        }
