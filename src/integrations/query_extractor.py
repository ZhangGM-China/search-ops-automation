import os
import urllib.parse
import datetime
import logging
import math
from sqlalchemy import bindparam, create_engine, text
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

from src.persistence.mysql_timeouts import mysql_timeout_connect_args
from src.governance.query_identity import is_non_intent_query, normalize_query_key, normalize_query_text

load_dotenv()

class QueryExtractor:
    """对接《自动化运营数据指标及SQL文档.pdf》，从生产数据源数据库提取异常Query"""

    @staticmethod
    def _require_connection_env(prefix: str) -> Dict[str, str]:
        keys = {
            "user": f"{prefix}_USER",
            "password": f"{prefix}_PASS",
            "host": f"{prefix}_HOST",
            "port": f"{prefix}_PORT",
            "db": f"{prefix}_DB",
        }
        values = {name: os.getenv(env_key) for name, env_key in keys.items()}
        missing = [env_key for name, env_key in keys.items() if not values.get(name)]
        if missing:
            raise ValueError(f"Missing production data source env vars: {', '.join(missing)}")
        return values  # type: ignore[return-value]
    
    def __init__(self, brand_type: str = "rds"):
        from src.config import get_env
        env = get_env()
        logging.info(f"📊 [Extractor] 正在以 {env.upper()} 环境模式启动数据提取器...")
        
        # Ensure latest environment variables are loaded
        from dotenv import load_dotenv
        load_dotenv(override=True)
        
        if brand_type == "rds":
            values = self._require_connection_env("RD_RDS")
            user = values["user"]
            safe_password = urllib.parse.quote_plus(values["password"])
            host = values["host"]
            port = values["port"]
            db = values["db"]
            self.uri = f"mysql+pymysql://{user}:{safe_password}@{host}:{port}/{db}?charset=utf8mb4"
        elif brand_type == "summit":
            values = self._require_connection_env("RD_SUMMIT")
            user = values["user"]
            safe_password = urllib.parse.quote_plus(values["password"])
            host = values["host"]
            port = values["port"]
            db = values["db"]
            self.uri = f"mysql+pymysql://{user}:{safe_password}@{host}:{port}/{db}?charset=utf8mb4"
        else:
            values = self._require_connection_env("RD_RIVIERA")
            user = values["user"]
            safe_password = urllib.parse.quote_plus(values["password"])
            host = values["host"]
            port = values["port"]
            db = values["db"]
            self.uri = f"mysql+pymysql://{user}:{safe_password}@{host}:{port}/{db}?charset=utf8mb4"
            
        self.engine = create_engine(
            self.uri,
            pool_pre_ping=True,
            connect_args=mysql_timeout_connect_args("RD_DB"),
        )

    def _get_query_field_mapping(self, brand: str):
        query_col = "search_term" if brand == "riviera" else "`query`"
        if brand == "harbor":
            uv_col = "query_search_uv"
            click_col = "query_search_click_uv"
            pay_col = "query_search_pay_uv"
            pv_col = "query_search_pv"
            atc_col = "query_search_atc_uv"
        else:
            uv_col = "search_uv" if brand == "riviera" else "query_search_uv"
            click_col = "click_uv" if brand == "riviera" else "query_search_click_uv"
            pay_col = "purchase_uv" if brand == "riviera" else "query_search_pay_uv"
            pv_col = "search_uv" if brand == "riviera" else "query_search_pv"
            atc_col = "add_to_cart_uv" if brand == "riviera" else "query_search_atc_uv"
        return query_col, uv_col, click_col, pay_col, pv_col, atc_col

    @staticmethod
    def _get_brand_user_behavior_table(brand: str) -> str:
        tables = {
            "atlas": "dwd_user_behavior_daily",
            "meadow": "dwd_meadow_user_behavior_daily",
            "summit": "dwd_summit_user_behavior_daily",
            "harbor": "dwd_harbor_user_behavior_daily",
        }
        return tables[brand]

    @staticmethod
    def _normalize_rows(result) -> List[Dict[str, Any]]:
        final_data = []
        for row in result:
            d = dict(row._mapping)
            d['search_uv'] = float(d['search_uv']) if d.get('search_uv') else 0
            d['ctr'] = float(d['ctr']) if d.get('ctr') else 0
            d['cvr'] = float(d['cvr']) if d.get('cvr') else 0
            d['ctr_avg'] = float(d['ctr_avg']) if d.get('ctr_avg') else 0
            d['cvr_avg'] = float(d['cvr_avg']) if d.get('cvr_avg') else 0
            d['ctr_gap'] = float(d['ctr_gap']) if d.get('ctr_gap') else 0
            d['cvr_gap'] = float(d['cvr_gap']) if d.get('cvr_gap') else 0
            final_data.append(d)
        return final_data

    @staticmethod
    def _rows_to_dicts(result) -> List[Dict[str, Any]]:
        rows = []
        for row in result:
            if hasattr(row, "_mapping"):
                rows.append(dict(row._mapping))
            elif isinstance(row, dict):
                rows.append(dict(row))
        return rows

    @staticmethod
    def _normalize_top_query_rows(result) -> List[Dict[str, Any]]:
        rows = []
        numeric_fields = [
            "search_uv", "search_pv", "ctr", "cvr", "avg_ctr", "avg_cvr", "query_pv_share",
            "current_click_uv", "current_purchase_uv", "history_search_uv", "history_click_uv",
            "history_purchase_uv",
        ]
        for row in result:
            item = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
            for field in numeric_fields:
                if field in item:
                    item[field] = float(item[field]) if item.get(field) is not None else 0
            item["query_rank"] = int(item.get("query_rank") or 0)
            if not is_non_intent_query(item.get("search_text") or item.get("keyword") or item.get("query")):
                rows.append(item)
        return rows

    @staticmethod
    def _build_exclusion_clause(column_name: str, excluded_keywords: List[str], params: Dict[str, Any]) -> str:
        placeholders = []
        for index, keyword in enumerate(excluded_keywords or []):
            if not keyword:
                continue
            key = f"excluded_keyword_{index}"
            placeholders.append(f":{key}")
            params[key] = keyword
        if not placeholders:
            return ""
        return f" AND {column_name} NOT IN ({', '.join(placeholders)})"

    def get_no_result_queries(
        self,
        brand: str,
        target_date: datetime.date,
        limit: int = 100,
        excluded_keywords: List[str] = None,
        raise_on_error: bool = False,
    ) -> List[Dict[str, Any]]:
        """按品牌拉取指定日期的无结果 Query。"""
        if brand in {"atlas", "meadow"}:
            return []

        safe_limit = max(1, min(int(limit or 100), 1000))
        target_date_str = target_date.strftime("%Y-%m-%d")
        params: Dict[str, Any] = {"target_date": target_date_str}

        if brand == "harbor":
            exclusion_clause = self._build_exclusion_clause("search_text", excluded_keywords or [], params)
            sql = f"""
            SELECT
                search_text,
                COUNT(*) AS search_pv
            FROM harbor_parsed_search_log
            WHERE DATE(search_time) = :target_date
                AND brand = 'harbor'
                AND recall_count = 0
                AND TRIM(search_text) NOT REGEXP '^[0-9]{{6,}}$'
                AND search_text IS NOT NULL
                AND TRIM(search_text) != ''
                {exclusion_clause}
            GROUP BY search_text
            ORDER BY search_pv DESC
            LIMIT {safe_limit}
            """
        elif brand == "riviera":
            exclusion_clause = self._build_exclusion_clause("search_text", excluded_keywords or [], params)
            sql = f"""
            SELECT
                search_text,
                COUNT(*) AS search_pv
            FROM moe_parsed_search_log
            WHERE DATE(search_time) = :target_date
                AND brand = 'Riviera'
                AND recall_count = 0
                AND TRIM(search_text) NOT REGEXP '^[0-9]{{6,}}$'
                AND TRIM(search_text) != '123'
                AND search_text IS NOT NULL
                AND TRIM(search_text) != ''
                {exclusion_clause}
            GROUP BY search_text
            ORDER BY search_pv DESC
            LIMIT {safe_limit}
            """
        elif brand == "summit":
            start_at = datetime.datetime.combine(target_date, datetime.time.min)
            end_at = start_at + datetime.timedelta(days=1)
            params.update({"start_at": start_at, "end_at": end_at})
            exclusion_clause = self._build_exclusion_clause("searchTerm", excluded_keywords or [], params)
            sql = f"""
            SELECT
                searchTerm AS keyword,
                COUNT(*) AS no_result_count
            FROM behavior
            WHERE event = 'SearchNoResultsProductExposure'
                AND searchTerm IS NOT NULL
                AND TRIM(searchTerm) != ''
                AND NOT regexp_like(TRIM(searchTerm), '^[0-9]{{5,}}$')
                AND searchTerm NOT LIKE '%二手%'
                AND searchTerm NOT LIKE '%防晒%'
                AND searchTerm NOT LIKE '%租%'
                AND searchTerm NOT LIKE '%钛%'
                AND searchTerm NOT LIKE '%盐丸%'
                AND searchTerm NOT LIKE '%门店%'
                AND searchTerm NOT LIKE '%回收%'
                AND searchTerm NOT LIKE '%优惠券%'
                AND searchTerm NOT LIKE '%穿线%'
                AND searchTerm NOT LIKE '%抽奖%'
                AND searchTerm NOT LIKE '%冬日温泉季%'
                AND __timestamp__ >= :start_at
                AND __timestamp__ < :end_at
                {exclusion_clause}
            GROUP BY keyword
            ORDER BY no_result_count DESC
            LIMIT {safe_limit}
            """
        else:
            return []

        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql), params)
                return self._rows_to_dicts(result)
        except Exception as e:
            logging.error(f"提取无结果 Query 失败 ({brand}). 错误详情: {e}")
            if raise_on_error:
                raise
            return []

    def get_top_query_rows(
        self,
        brand: str,
        target_date: datetime.date = None,
        limit: int = None,
    ) -> List[Dict[str, Any]]:
        """拉取指定品牌指定日期的全量 Top Query，不按 CTR/CVR 异常过滤。"""
        from src.config import get_brand_business_config, get_brand_config
        config = get_brand_config(brand)
        business_config = get_brand_business_config(brand)

        table_name = config["table_name"]
        safe_limit = max(1, min(int(limit or business_config.get("query_governance_extract_limit") or 150), 1000))
        query_col, uv_col, click_col, pay_col, pv_col, atc_col = self._get_query_field_mapping(brand)

        target_date = target_date or (datetime.date.today() - datetime.timedelta(days=1))
        past_end_date = target_date - datetime.timedelta(days=1)
        past_start_date = target_date - datetime.timedelta(days=7)
        min_history_days = 2 if brand == "riviera" else 5

        sql = f"""
        WITH query_metrics AS (
            SELECT
                {query_col} AS raw_query,
                SUM({uv_col}) AS search_uv,
                SUM({pv_col}) AS search_pv,
                SUM({click_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS ctr,
                SUM({pay_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS cvr
            FROM {table_name}
            WHERE `date` = :target_date
                AND {query_col} IS NOT NULL
                AND TRIM({query_col}) != ''
                AND {query_col} NOT LIKE '%折扣%'
                AND {query_col} NOT LIKE '%特价%'
                AND {query_col} NOT LIKE '%打折%'
                AND {query_col} NOT LIKE '%特惠%'
                AND (CHAR_LENGTH({query_col}) - CHAR_LENGTH(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({query_col}, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''))) < 5
            GROUP BY 1
        ),
        ranked AS (
            SELECT
                raw_query,
                search_uv,
                search_pv,
                ctr,
                cvr,
                ROW_NUMBER() OVER (ORDER BY search_pv DESC) AS query_rank,
                search_pv * 1.0 / NULLIF(SUM(search_pv) OVER (), 0) AS query_pv_share
            FROM query_metrics
        ),
        past_avg AS (
            SELECT
                {query_col} AS raw_query,
                SUM({click_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS avg_ctr,
                SUM({pay_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS avg_cvr
            FROM {table_name}
            WHERE `date` BETWEEN :past_start_date AND :past_end_date
                AND {query_col} IS NOT NULL
                AND TRIM({query_col}) != ''
            GROUP BY 1
            HAVING COUNT(DISTINCT `date`) >= {min_history_days}
        )
        SELECT
            r.raw_query AS `query`,
            r.query_rank,
            r.search_uv,
            r.search_pv,
            r.ctr,
            r.cvr,
            p.avg_ctr,
            p.avg_cvr,
            r.query_pv_share
        FROM ranked r
        LEFT JOIN past_avg p ON r.raw_query = p.raw_query
        ORDER BY r.query_rank ASC
        LIMIT {safe_limit}
        """

        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql), {
                    "target_date": target_date.strftime("%Y-%m-%d"),
                    "past_start_date": past_start_date.strftime("%Y-%m-%d"),
                    "past_end_date": past_end_date.strftime("%Y-%m-%d"),
                })
                return self._normalize_top_query_rows(result)
        except Exception as e:
            logging.error(f"提取全量 Top Query 失败 ({brand}). 错误详情: {e}")
            return []

    def get_brand_daily_metric(self, brand: str, target_date: datetime.date) -> Dict[str, Any]:
        """拉取品牌日汇总指标，作为 Top Query Planner 的品牌级上下文。"""
        from src.config import get_brand_config
        config = get_brand_config(brand)
        table_name = config["table_name"]
        _query_col, _uv_col, _click_col, _pay_col, pv_col, _atc_col = self._get_query_field_mapping(brand)

        if brand == "riviera":
            sql = """
            SELECT
                SUM(search_uv) AS brand_uv,
                SUM(search_uv) AS brand_pv,
                SUM(click_uv) AS click_uv,
                SUM(add_to_cart_uv) AS add_to_cart_uv,
                SUM(purchase_uv) AS purchase_uv,
                0 AS purchase_amount,
                ROUND(SUM(click_uv) * 1.0 / NULLIF(SUM(search_uv), 0), 4) AS ctr,
                ROUND(SUM(add_to_cart_uv) * 1.0 / NULLIF(SUM(search_uv), 0), 4) AS atc,
                ROUND(SUM(purchase_uv) * 1.0 / NULLIF(SUM(search_uv), 0), 4) AS cvr
            FROM rl_daily
            WHERE `date` = :target_date
            """
        else:
            user_table_name = self._get_brand_user_behavior_table(brand)
            sql = f"""
            WITH SearchMetrics AS (
                SELECT
                    user_date,
                    COUNT(DISTINCT user_uid) AS brand_uv
                FROM {user_table_name}
                WHERE user_search_pv > 0
                    AND user_date = :target_date
                GROUP BY user_date
            ),
            ClickMetrics AS (
                SELECT
                    user_date,
                    COUNT(DISTINCT CASE WHEN user_search_pv_pdp_view > 0 OR user_search_pv_click > 0 THEN user_uid END) AS click_uv
                FROM {user_table_name}
                WHERE (user_search_pv_pdp_view > 0 OR user_search_pv_click > 0)
                    AND user_date = :target_date
                GROUP BY user_date
            ),
            ATCMetrics AS (
                SELECT
                    user_date,
                    COUNT(DISTINCT CASE WHEN user_search_pv_atc > 0 THEN user_uid END) AS add_to_cart_uv
                FROM {user_table_name}
                WHERE user_search_pv_atc > 0
                    AND user_date = :target_date
                GROUP BY user_date
            ),
            PayMetrics AS (
                SELECT
                    user_date,
                    COUNT(DISTINCT CASE WHEN user_search_pv_pay > 0 THEN user_uid END) AS purchase_uv,
                    SUM(user_pay_amount) AS purchase_amount
                FROM {user_table_name}
                WHERE user_search_pv_pay > 0
                    AND user_date = :target_date
                GROUP BY user_date
            )
            SELECT
                T1.user_date,
                IFNULL(T1.brand_uv, 0) AS brand_uv,
                (
                    SELECT IFNULL(SUM({pv_col}), 0)
                    FROM {table_name}
                    WHERE `date` = :target_date
                ) AS brand_pv,
                IFNULL(T2.click_uv, 0) AS click_uv,
                IFNULL(T3.add_to_cart_uv, 0) AS add_to_cart_uv,
                IFNULL(T4.purchase_uv, 0) AS purchase_uv,
                IFNULL(T4.purchase_amount, 0) AS purchase_amount,
                IFNULL(T2.click_uv, 0) / NULLIF(IFNULL(T1.brand_uv, 0), 0) AS ctr,
                IFNULL(T3.add_to_cart_uv, 0) / NULLIF(IFNULL(T1.brand_uv, 0), 0) AS atc,
                IFNULL(T4.purchase_uv, 0) / NULLIF(IFNULL(T1.brand_uv, 0), 0) AS cvr
            FROM SearchMetrics T1
            LEFT JOIN ClickMetrics T2 ON T1.user_date = T2.user_date
            LEFT JOIN ATCMetrics T3 ON T1.user_date = T3.user_date
            LEFT JOIN PayMetrics T4 ON T1.user_date = T4.user_date
            """

        empty = {
            "brand": brand,
            "data_date": target_date.strftime("%Y-%m-%d"),
            "brand_uv": 0,
            "brand_pv": 0,
            "click_uv": 0,
            "add_to_cart_uv": 0,
            "purchase_uv": 0,
            "purchase_amount": 0,
            "ctr": 0,
            "atc": 0,
            "cvr": 0,
            "metric_source": "user_behavior",
        }
        try:
            with self.engine.connect() as conn:
                row = conn.execute(text(sql), {"target_date": target_date.strftime("%Y-%m-%d")}).fetchone()
                if not row:
                    return empty
                data = dict(row._mapping) if hasattr(row, "_mapping") else {
                    "brand_uv": row[0],
                    "brand_pv": row[1],
                    "click_uv": row[2],
                    "add_to_cart_uv": row[3],
                    "purchase_uv": row[4],
                    "purchase_amount": row[5],
                    "ctr": row[6],
                    "atc": row[7],
                    "cvr": row[8],
                }
                return {
                    **empty,
                    "brand_uv": float(data.get("brand_uv") or 0),
                    "brand_pv": float(data.get("brand_pv") or 0),
                    "click_uv": float(data.get("click_uv") or 0),
                    "add_to_cart_uv": float(data.get("add_to_cart_uv") or 0),
                    "purchase_uv": float(data.get("purchase_uv") or 0),
                    "purchase_amount": float(data.get("purchase_amount") or 0),
                    "ctr": float(data.get("ctr") or 0),
                    "atc": float(data.get("atc") or 0),
                    "cvr": float(data.get("cvr") or 0),
                }
        except Exception as e:
            logging.error(f"提取品牌日指标失败 ({brand}). 错误详情: {e}")
            return empty

    def get_query_daily_metrics(
        self,
        brand: str,
        target_date: datetime.date,
        queries: List[str],
    ) -> List[Dict[str, Any]]:
        normalized_queries = [str(query or "").strip() for query in queries if str(query or "").strip()]
        if not normalized_queries:
            return []
        sql, params = self._build_query_daily_metrics_query(
            brand,
            target_date,
            filtered_queries=normalized_queries,
        )
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql), params)
                return self._normalize_top_query_rows(result)
        except Exception as e:
            logging.error(f"提取固定 Query 指标失败 ({brand}). 错误详情: {e}")
            return []

    def get_query_daily_metrics_strict(
        self,
        brand: str,
        target_date: datetime.date,
        queries: List[str],
    ) -> Dict[str, Any]:
        requested_by_key: Dict[str, str] = {}
        for query in queries:
            display_query = normalize_query_text(query)
            query_key = normalize_query_key(display_query)
            if query_key and query_key not in requested_by_key:
                requested_by_key[query_key] = display_query
        requested_keys = list(requested_by_key)
        if not requested_keys:
            return {
                "status": "complete",
                "requestedKeys": [],
                "matchedKeys": [],
                "missingKeys": [],
                "duplicateKeys": [],
                "items": [],
                "error": None,
            }

        snapshot = self.get_query_daily_snapshot_strict(brand, target_date)
        if snapshot["status"] == "failed":
            return {
                "status": "failed",
                "requestedKeys": requested_keys,
                "matchedKeys": [],
                "missingKeys": requested_keys,
                "duplicateKeys": [],
                "items": [],
                "error": snapshot["error"],
            }
        canonical_items = {normalize_query_key(item["query"]): item for item in snapshot["items"]}
        items = []
        for query_key in requested_keys:
            if query_key in canonical_items:
                items.append({**canonical_items[query_key], "query": requested_by_key[query_key]})

        matched_keys = [query_key for query_key in requested_keys if query_key in canonical_items]
        missing_keys = [query_key for query_key in requested_keys if query_key not in canonical_items]
        return {
            "status": "complete" if not missing_keys else "partial",
            "requestedKeys": requested_keys,
            "matchedKeys": matched_keys,
            "missingKeys": missing_keys,
            "duplicateKeys": [query_key for query_key in requested_keys if query_key in snapshot["duplicateKeys"]],
            "items": items,
            "error": None,
        }

    def get_query_baseline_window_strict(
        self,
        brand: str,
        planned_start_date: datetime.date,
        requested_queries: List[str],
    ) -> Dict[str, Any]:
        baseline_start_date = planned_start_date - datetime.timedelta(days=7)
        baseline_end_date = planned_start_date - datetime.timedelta(days=1)
        requested_by_key: Dict[str, str] = {}
        for query in requested_queries:
            display_query = normalize_query_text(query)
            query_key = normalize_query_key(display_query)
            if query_key and query_key not in requested_by_key:
                requested_by_key[query_key] = display_query
        requested_keys = list(requested_by_key)
        result_base = {
            "baselineStartDate": baseline_start_date.isoformat(),
            "baselineEndDate": baseline_end_date.isoformat(),
            "expectedDays": 7,
            "requestedKeys": requested_keys,
        }
        if not requested_keys:
            return {
                **result_base,
                "status": "failed",
                "matchedKeys": [],
                "missingKeys": [],
                "items": [],
                "error": "baseline_requested_queries_empty",
            }

        sql, params = self._build_query_baseline_window_query(
            brand,
            baseline_start_date,
            baseline_end_date,
        )
        try:
            with self.engine.connect() as conn:
                rows = self._rows_to_dicts(conn.execute(text(sql), params))
        except Exception as e:
            logging.error(
                "baseline_source_query_failed exception=%s",
                type(e).__name__,
            )
            return {
                **result_base,
                "status": "failed",
                "matchedKeys": [],
                "missingKeys": requested_keys,
                "items": [],
                "error": "baseline_source_query_failed",
            }

        expected_dates = {
            (baseline_start_date + datetime.timedelta(days=offset)).isoformat()
            for offset in range(7)
        }
        grouped: Dict[str, Dict[str, Any]] = {}
        invalid_keys = set()
        for row in rows:
            query_key = normalize_query_key(row.get("query"))
            if query_key not in requested_by_key:
                continue
            raw_date = row.get("data_date")
            if isinstance(raw_date, datetime.datetime):
                data_date = raw_date.date().isoformat()
            elif isinstance(raw_date, datetime.date):
                data_date = raw_date.isoformat()
            else:
                data_date = str(raw_date or "")[:10]
            if data_date not in expected_dates:
                invalid_keys.add(query_key)
                continue

            values: Dict[str, float] = {}
            for field in ("search_uv", "search_pv"):
                try:
                    value = float(row.get(field))
                except (TypeError, ValueError):
                    invalid_keys.add(query_key)
                    break
                if not math.isfinite(value) or value < 0 or not value.is_integer():
                    invalid_keys.add(query_key)
                    break
                values[field] = int(value)
            if len(values) != 2:
                continue
            for field in ("click_uv", "purchase_uv"):
                try:
                    value = float(row.get(field))
                except (TypeError, ValueError):
                    invalid_keys.add(query_key)
                    break
                if not math.isfinite(value) or value < 0:
                    invalid_keys.add(query_key)
                    break
                values[field] = value
            if len(values) != 4:
                continue

            aggregate = grouped.setdefault(query_key, {
                "dates": set(),
                "searchUv": 0,
                "searchPv": 0,
                "clickUv": 0.0,
                "purchaseUv": 0.0,
            })
            aggregate["dates"].add(data_date)
            aggregate["searchUv"] += values["search_uv"]
            aggregate["searchPv"] += values["search_pv"]
            aggregate["clickUv"] += values["click_uv"]
            aggregate["purchaseUv"] += values["purchase_uv"]

        matched_keys = [
            key
            for key in requested_keys
            if key in grouped or key in invalid_keys
        ]
        missing_keys = [key for key in requested_keys if key not in matched_keys]
        items = []
        incomplete_keys = set(missing_keys)
        zero_denominator_keys = set()
        for query_key in requested_keys:
            aggregate = grouped.get(query_key)
            if aggregate is None:
                continue
            observed_days = len(aggregate["dates"])
            search_uv = aggregate["searchUv"]
            if observed_days != 7:
                incomplete_keys.add(query_key)
            if search_uv == 0:
                zero_denominator_keys.add(query_key)
            items.append({
                "query": requested_by_key[query_key],
                "observedDays": observed_days,
                "searchUv": search_uv,
                "searchPv": aggregate["searchPv"],
                "clickUv": aggregate["clickUv"],
                "purchaseUv": aggregate["purchaseUv"],
                "ctr": aggregate["clickUv"] / search_uv if search_uv else None,
                "cvr": aggregate["purchaseUv"] / search_uv if search_uv else None,
            })

        error = None
        if invalid_keys:
            error = "baseline_invalid_metric"
        elif zero_denominator_keys:
            error = "baseline_zero_search_uv"
        elif incomplete_keys:
            error = "baseline_window_incomplete"
        return {
            **result_base,
            "status": "complete" if error is None else "partial",
            "matchedKeys": matched_keys,
            "missingKeys": missing_keys,
            "items": items,
            "error": error,
        }

    def _build_query_baseline_window_query(
        self,
        brand: str,
        baseline_start_date: datetime.date,
        baseline_end_date: datetime.date,
    ):
        from src.config import get_brand_config

        table_name = get_brand_config(brand)["table_name"]
        query_col, uv_col, click_col, pay_col, pv_col, atc_col = (
            self._get_query_field_mapping(brand)
        )
        sql = f"""
        SELECT `date` AS data_date, {query_col} AS `query`,
               SUM({uv_col}) AS search_uv, SUM({pv_col}) AS search_pv,
               SUM({click_col}) AS click_uv, SUM({pay_col}) AS purchase_uv
        FROM {table_name}
        WHERE `date` BETWEEN :baseline_start_date AND :baseline_end_date
        GROUP BY `date`, {query_col}
        """
        return sql, {
            "baseline_start_date": baseline_start_date.isoformat(),
            "baseline_end_date": baseline_end_date.isoformat(),
        }

    def get_query_daily_snapshot_strict(self, brand: str, target_date: datetime.date) -> Dict[str, Any]:
        sql, params = self._build_strict_query_daily_snapshot_query(brand, target_date)
        try:
            with self.engine.connect() as conn:
                rows = self._normalize_top_query_rows(conn.execute(text(sql), params))
        except Exception as e:
            logging.error(f"提取实验 Query 全日快照失败 ({brand}). 错误详情: {e}")
            return {"status": "failed", "items": [], "duplicateKeys": [], "error": str(e)}

        current_by_key: Dict[str, List[Dict[str, Any]]] = {}
        history_by_key: Dict[str, List[Dict[str, Any]]] = {}
        first_index: Dict[str, int] = {}
        for index, row in enumerate(rows):
            key = normalize_query_key(row.get("query"))
            if not key:
                continue
            first_index.setdefault(key, index)
            row_type = row.get("row_type")
            if row_type in {None, "current"}:
                current_by_key.setdefault(key, []).append(row)
            if row_type in {None, "history"}:
                history_by_key.setdefault(key, []).append(row)

        min_history_days = 2 if brand == "riviera" else 5
        canonical_items: Dict[str, Dict[str, Any]] = {}
        for key, current_rows in current_by_key.items():
            total_uv = sum(float(row.get("search_uv") or 0) for row in current_rows)
            total_pv = sum(float(row.get("search_pv") or 0) for row in current_rows)
            click_uv = sum(float(row.get("current_click_uv") if row.get("current_click_uv") is not None else float(row.get("ctr") or 0) * float(row.get("search_uv") or 0)) for row in current_rows)
            purchase_uv = sum(float(row.get("current_purchase_uv") if row.get("current_purchase_uv") is not None else float(row.get("cvr") or 0) * float(row.get("search_uv") or 0)) for row in current_rows)
            history_rows = history_by_key.get(key, [])
            history_uv = sum(float(row.get("history_search_uv") or 0) for row in history_rows)
            history_click = sum(float(row.get("history_click_uv") or 0) for row in history_rows)
            history_purchase = sum(float(row.get("history_purchase_uv") or 0) for row in history_rows)
            history_dates = {date for row in history_rows for date in str(row.get("history_dates") or "").split(",") if date}
            has_history = len(history_dates) >= min_history_days and history_uv > 0
            canonical_items[key] = {
                "query": normalize_query_text(current_rows[0].get("query")),
                "search_uv": total_uv, "search_pv": total_pv,
                "ctr": click_uv / total_uv if total_uv else 0.0,
                "cvr": purchase_uv / total_uv if total_uv else 0.0,
                "avg_ctr": history_click / history_uv if has_history else 0.0,
                "avg_cvr": history_purchase / history_uv if has_history else 0.0,
            }
        ranked = sorted(canonical_items, key=lambda key: (-canonical_items[key]["search_pv"], first_index[key]))
        total_pv = sum(canonical_items[key]["search_pv"] for key in ranked)
        for rank, key in enumerate(ranked, 1):
            canonical_items[key]["query_rank"] = rank
            canonical_items[key]["query_pv_share"] = canonical_items[key]["search_pv"] / total_pv if total_pv else 0.0
        return {
            "status": "complete", "items": [canonical_items[key] for key in ranked],
            "duplicateKeys": [key for key, values in current_by_key.items() if len(values) > 1], "error": None,
        }

    def _build_strict_query_daily_snapshot_query(self, brand: str, target_date: datetime.date):
        from src.config import get_brand_config
        table_name = get_brand_config(brand)["table_name"]
        query_col, uv_col, click_col, pay_col, pv_col, atc_col = self._get_query_field_mapping(brand)
        sql = f"""
        SELECT 'current' AS row_type, {query_col} AS `query`,
               SUM({uv_col}) AS search_uv, SUM({pv_col}) AS search_pv,
               SUM({click_col}) AS current_click_uv, SUM({pay_col}) AS current_purchase_uv,
               NULL AS history_search_uv, NULL AS history_click_uv, NULL AS history_purchase_uv, NULL AS history_dates
        FROM {table_name} WHERE `date` = :target_date GROUP BY {query_col}
        UNION ALL
        SELECT 'history' AS row_type, {query_col} AS `query`,
               NULL, NULL, NULL, NULL,
               SUM({uv_col}), SUM({click_col}), SUM({pay_col}),
               GROUP_CONCAT(DISTINCT DATE_FORMAT(`date`, '%Y-%m-%d') ORDER BY `date`)
        FROM {table_name} WHERE `date` BETWEEN :past_start_date AND :past_end_date GROUP BY {query_col}
        """
        return sql, {
            "target_date": target_date.strftime("%Y-%m-%d"),
            "past_start_date": (target_date - datetime.timedelta(days=7)).strftime("%Y-%m-%d"),
            "past_end_date": (target_date - datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
        }

    def _build_query_daily_metrics_query(
        self,
        brand: str,
        target_date: datetime.date,
        filtered_queries: Optional[List[str]] = None,
        strict_snapshot: bool = False,
    ):
        from src.config import get_brand_config

        config = get_brand_config(brand)
        table_name = config["table_name"]
        query_col, uv_col, click_col, pay_col, pv_col, atc_col = self._get_query_field_mapping(brand)
        past_end_date = target_date - datetime.timedelta(days=1)
        past_start_date = target_date - datetime.timedelta(days=7)
        min_history_days = 2 if brand == "riviera" else 5
        query_filter = ""
        query_params: Dict[str, Any] = {}
        if filtered_queries:
            query_params = {f"query_{index}": query for index, query in enumerate(filtered_queries)}
            in_clause = ", ".join(f":query_{index}" for index, _query in enumerate(filtered_queries))
            query_filter = f"AND {query_col} IN ({in_clause})"
        current_numerators = ""
        history_metrics = f"""
                SUM({click_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS avg_ctr,
                SUM({pay_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS avg_cvr"""
        history_having = f"HAVING COUNT(DISTINCT `date`) >= {min_history_days}"
        history_select = "p.avg_ctr,\n            p.avg_cvr,"
        if strict_snapshot:
            current_numerators = f""",
                SUM({click_col}) AS current_click_uv,
                SUM({pay_col}) AS current_purchase_uv"""
            history_metrics = f"""
                SUM({uv_col}) AS history_search_uv,
                SUM({click_col}) AS history_click_uv,
                SUM({pay_col}) AS history_purchase_uv,
                GROUP_CONCAT(DISTINCT DATE_FORMAT(`date`, '%Y-%m-%d') ORDER BY `date`) AS history_dates"""
            history_having = ""
            history_select = "p.history_search_uv,\n            p.history_click_uv,\n            p.history_purchase_uv,\n            p.history_dates,"

        sql = f"""
        WITH current_metrics AS (
            SELECT
                {query_col} AS raw_query,
                SUM({uv_col}) AS search_uv,
                SUM({pv_col}) AS search_pv,
                SUM({click_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS ctr,
                SUM({pay_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS cvr
                {current_numerators}
            FROM {table_name}
            WHERE `date` = :target_date
              {query_filter}
            GROUP BY 1
        ),
        ranked AS (
            SELECT
                raw_query,
                search_uv,
                search_pv,
                ctr,
                cvr,
                ROW_NUMBER() OVER (ORDER BY search_pv DESC) AS query_rank,
                search_pv * 1.0 / NULLIF(SUM(search_pv) OVER (), 0) AS query_pv_share
            FROM current_metrics
        ),
        past_avg AS (
            SELECT
                {query_col} AS raw_query,
                {history_metrics}
            FROM {table_name}
            WHERE `date` BETWEEN :past_start_date AND :past_end_date
              {query_filter}
            GROUP BY 1
            {history_having}
        )
        SELECT
            r.raw_query AS `query`,
            r.query_rank,
            r.search_uv,
            r.search_pv,
            r.ctr,
            r.cvr,
            {history_select}
            r.query_pv_share
        FROM ranked r
        LEFT JOIN past_avg p ON p.raw_query = r.raw_query
        ORDER BY r.query_rank ASC
        """

        return sql, {
            "target_date": target_date.strftime("%Y-%m-%d"),
            "past_start_date": past_start_date.strftime("%Y-%m-%d"),
            "past_end_date": past_end_date.strftime("%Y-%m-%d"),
            **query_params,
        }

    def get_top_queries(self, brand: str) -> List[Dict[str, Any]]:
        from src.config import get_brand_business_config, get_brand_config
        config = get_brand_config(brand)
        business_config = get_brand_business_config(brand)

        table_name = config["table_name"]
        top_n = business_config.get("query_governance_extract_limit", 150)
        query_col, uv_col, click_col, pay_col, pv_col, atc_col = self._get_query_field_mapping(brand)

        today = datetime.date.today()
        t1_date = today - datetime.timedelta(days=1)
        p_end_date = today - datetime.timedelta(days=2)
        p_start_days = 8 if brand != "riviera" else 4
        p_start_date = today - datetime.timedelta(days=p_start_days)

        sql = f"""
        WITH top_keywords AS (
            SELECT
                {query_col} as raw_query,
                SUM({uv_col}) AS search_uv,
                SUM({click_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS ctr,
                SUM({pay_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS cvr
            FROM {table_name}
            WHERE `date` = :t1_date
                AND {query_col} NOT LIKE '%折扣%'
                AND {query_col} NOT LIKE '%特价%'
                AND {query_col} NOT LIKE '%打折%'
                AND {query_col} NOT LIKE '%特惠%'
                AND (CHAR_LENGTH({query_col}) - CHAR_LENGTH(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({query_col}, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''))) < 5
            GROUP BY 1
            ORDER BY SUM({pv_col}) DESC
            LIMIT {top_n}
        ),
        past_avg AS (
            SELECT
                {query_col} as raw_query,
                SUM({click_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS avg_ctr,
                SUM({pay_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS avg_cvr
            FROM {table_name}
            WHERE `date` BETWEEN :p_start_date AND :p_end_date
                AND {query_col} NOT LIKE '%折扣%'
                AND {query_col} NOT LIKE '%特价%'
                AND {query_col} NOT LIKE '%打折%'
                AND {query_col} NOT LIKE '%特惠%'
                AND (CHAR_LENGTH({query_col}) - CHAR_LENGTH(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({query_col}, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''))) < 5
            GROUP BY 1
            HAVING COUNT(DISTINCT `date`) >= {5 if brand != "riviera" else 2}
        )
        SELECT
            t.raw_query as `query`,
            t.search_uv,
            t.ctr,
            t.cvr,
            p.avg_ctr as ctr_avg,
            p.avg_cvr as cvr_avg,
            CASE
                {
                f"WHEN (t.ctr - p.avg_ctr) * 100 < -5 AND (t.cvr - p.avg_cvr) * 100 < -2 THEN 'both'" if brand == "harbor" else
                f"WHEN (t.ctr - p.avg_ctr) * 100 < -10 AND (t.cvr - p.avg_cvr) * 100 < -3 THEN 'both'" if brand != 'riviera' else
                f"WHEN (t.ctr - p.avg_ctr) / NULLIF(p.avg_ctr, 0) * 100 < -10 AND (t.cvr - p.avg_cvr) / NULLIF(p.avg_cvr, 0) * 100 < -10 THEN 'both'"
                }
                {
                f"WHEN (t.ctr - p.avg_ctr) * 100 < -5 THEN 'low_ctr'" if brand == "harbor" else
                f"WHEN (t.ctr - p.avg_ctr) * 100 < -10 THEN 'low_ctr'" if brand != 'riviera' else
                f"WHEN (t.ctr - p.avg_ctr) / NULLIF(p.avg_ctr, 0) * 100 < -10 THEN 'low_ctr'"
                }
                {
                f"WHEN (t.cvr - p.avg_cvr) * 100 < -2 THEN 'low_cvr'" if brand == "harbor" else
                f"WHEN (t.cvr - p.avg_cvr) * 100 < -3 THEN 'low_cvr'" if brand != 'riviera' else
                f"WHEN (t.cvr - p.avg_cvr) / NULLIF(p.avg_cvr, 0) * 100 < -10 THEN 'low_cvr'"
                }
                ELSE 'normal'
            END as `type`,
            ROUND({
            f"(t.ctr - p.avg_ctr) * 100" if brand != "riviera" else
            "(t.ctr - p.avg_ctr) / NULLIF(p.avg_ctr, 0) * 100"
            }, 2) as ctr_gap,
            ROUND({
            f"(t.cvr - p.avg_cvr) * 100" if brand != "riviera" else
            "(t.cvr - p.avg_cvr) / NULLIF(p.avg_cvr, 0) * 100"
            }, 2) as cvr_gap
        FROM top_keywords t
        JOIN past_avg p ON t.raw_query = p.raw_query
        ORDER BY t.search_uv DESC
        """

        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql), {
                    "t1_date": t1_date.strftime("%Y-%m-%d"),
                    "p_start_date": p_start_date.strftime("%Y-%m-%d"),
                    "p_end_date": p_end_date.strftime("%Y-%m-%d")
                })
                return self._normalize_rows(result)
        except Exception as e:
            logging.error(f"提取 Top Query 失败 ({brand}). 错误详情: {e}")
            logging.error(f"使用的数据库 URI: {self.uri.split('@')[-1]}")
            return []

    def get_abnormal_queries(self, brand: str) -> List[Dict[str, Any]]:
        from src.config import get_brand_business_config, get_brand_config
        config = get_brand_config(brand)
        business_config = get_brand_business_config(brand)
        
        table_name = config["table_name"]
        top_n = business_config.get("query_governance_extract_limit", 150)
        query_col, uv_col, click_col, pay_col, pv_col, atc_col = self._get_query_field_mapping(brand)

        # 使用 Python 本地计算日期，避免数据库 CURRENT_DATE 受到时区 (UTC/DST) 影响导致的 24 小时错位
        today = datetime.date.today()
        t1_date = today - datetime.timedelta(days=1)
        p_end_date = today - datetime.timedelta(days=2)
        p_start_days = 8 if brand != "riviera" else 4
        p_start_date = today - datetime.timedelta(days=p_start_days)

        sql = f"""
        WITH top_keywords AS (
            SELECT
                {query_col} as raw_query,
                SUM({uv_col}) AS search_uv,
                SUM({click_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS ctr,
                SUM({pay_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS cvr
            FROM {table_name}
            WHERE `date` = :t1_date
                AND {query_col} NOT LIKE '%折扣%'
                AND {query_col} NOT LIKE '%特价%'
                AND {query_col} NOT LIKE '%打折%'
                AND {query_col} NOT LIKE '%特惠%'
                AND (CHAR_LENGTH({query_col}) - CHAR_LENGTH(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({query_col}, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''))) < 5
            GROUP BY 1
            ORDER BY SUM({pv_col}) DESC
            LIMIT {top_n}
        ),
        past_avg AS (
            SELECT
                {query_col} as raw_query,
                SUM({click_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS avg_ctr,
                SUM({pay_col}) * 1.0 / NULLIF(SUM({uv_col}), 0) AS avg_cvr
            FROM {table_name}
            WHERE `date` BETWEEN :p_start_date AND :p_end_date
                AND {query_col} NOT LIKE '%折扣%'
                AND {query_col} NOT LIKE '%特价%'
                AND {query_col} NOT LIKE '%打折%'
                AND {query_col} NOT LIKE '%特惠%'
                AND (CHAR_LENGTH({query_col}) - CHAR_LENGTH(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({query_col}, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''))) < 5
            GROUP BY 1
            HAVING COUNT(DISTINCT `date`) >= {5 if brand != "riviera" else 2}
        )
        SELECT 
            t.raw_query as `query`, 
            t.search_uv, 
            t.ctr, 
            t.cvr, 
            p.avg_ctr as ctr_avg, 
            p.avg_cvr as cvr_avg,
            CASE 
                {
                f"WHEN (t.ctr - p.avg_ctr) * 100 < -5 AND (t.cvr - p.avg_cvr) * 100 < -2 THEN 'both'" if brand == "harbor" else
                f"WHEN (t.ctr - p.avg_ctr) * 100 < -10 AND (t.cvr - p.avg_cvr) * 100 < -3 THEN 'both'" if brand != "riviera" else
                f"WHEN (t.ctr - p.avg_ctr) / NULLIF(p.avg_ctr, 0) * 100 < -10 AND (t.cvr - p.avg_cvr) / NULLIF(p.avg_cvr, 0) * 100 < -10 THEN 'both'"
                }
                {
                f"WHEN (t.ctr - p.avg_ctr) * 100 < -5 THEN 'low_ctr'" if brand == "harbor" else
                f"WHEN (t.ctr - p.avg_ctr) * 100 < -10 THEN 'low_ctr'" if brand != "riviera" else
                f"WHEN (t.ctr - p.avg_ctr) / NULLIF(p.avg_ctr, 0) * 100 < -10 THEN 'low_ctr'"
                }
                {
                f"WHEN (t.cvr - p.avg_cvr) * 100 < -2 THEN 'low_cvr'" if brand == "harbor" else
                f"WHEN (t.cvr - p.avg_cvr) * 100 < -3 THEN 'low_cvr'" if brand != "riviera" else
                f"WHEN (t.cvr - p.avg_cvr) / NULLIF(p.avg_cvr, 0) * 100 < -10 THEN 'low_cvr'"
                }
            END as `type`,
            ROUND({
            f"(t.ctr - p.avg_ctr) * 100" if brand != "riviera" else
            "(t.ctr - p.avg_ctr) / NULLIF(p.avg_ctr, 0) * 100"
            }, 2) as ctr_gap,
            ROUND({
            f"(t.cvr - p.avg_cvr) * 100" if brand != "riviera" else
            "(t.cvr - p.avg_cvr) / NULLIF(p.avg_cvr, 0) * 100"
            }, 2) as cvr_gap
        FROM top_keywords t
        JOIN past_avg p ON t.raw_query = p.raw_query
        WHERE 
            {
            f"(t.ctr - p.avg_ctr) * 100 < -10 OR (t.cvr - p.avg_cvr) * 100 < -3" if brand != "riviera" and brand != "harbor" else
            f"(t.ctr - p.avg_ctr) * 100 < -5 OR (t.cvr - p.avg_cvr) * 100 < -2" if brand == "harbor" else
            f"(t.ctr - p.avg_ctr) / NULLIF(p.avg_ctr, 0) * 100 < -10 OR (t.cvr - p.avg_cvr) / NULLIF(p.avg_cvr, 0) * 100 < -10"
            }
        ORDER BY t.search_uv DESC
        """
        
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql), {
                    "t1_date": t1_date.strftime("%Y-%m-%d"),
                    "p_start_date": p_start_date.strftime("%Y-%m-%d"),
                    "p_end_date": p_end_date.strftime("%Y-%m-%d")
                })
                return self._normalize_rows(result)
        except Exception as e:
            logging.error(f"提取异常Query失败 ({brand}). 错误详情: {e}")
            logging.error(f"使用的数据库 URI: {self.uri.split('@')[-1]}") # 仅打印 Host 部分
            return []

    def get_query_performance(self, brand: str, query: str, target_date: datetime.date) -> Dict[str, Any]:
        """获取特定日期、特定 Query 的真实按日表现指标。"""
        from src.config import get_brand_config
        config = get_brand_config(brand)
        table_name = config["table_name"]
        query_col, uv_col, click_col, pay_col, pv_col, atc_col = self._get_query_field_mapping(brand)

        sql = f"""
        SELECT
            SUM({pv_col}) AS pv,
            SUM({uv_col}) AS uv,
            SUM({click_col}) AS click_uv,
            SUM({atc_col}) AS add_to_cart_uv,
            SUM({pay_col}) AS purchase_uv
        FROM {table_name}
        WHERE {query_col} = :q_text AND `date` = :q_date
        """
        date_text = target_date.isoformat()
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql), {"q_text": query, "q_date": target_date.strftime('%Y-%m-%d')}).fetchone()
                if result is not None and result[1] is not None:
                    uv = float(result[1] or 0)
                    click_uv = float(result[2] or 0)
                    add_to_cart_uv = float(result[3] or 0)
                    purchase_uv = float(result[4] or 0)
                    return {
                        "date": date_text,
                        "status": "complete",
                        "pv": int(result[0] or 0),
                        "uv": int(uv),
                        "search_pv": int(result[0] or 0),
                        "search_uv": int(uv),
                        "click_uv": click_uv,
                        "add_to_cart_uv": add_to_cart_uv,
                        "purchase_uv": purchase_uv,
                        "ctr": click_uv / uv if uv else None,
                        "atc": add_to_cart_uv / uv if uv else None,
                        "cvr": purchase_uv / uv if uv else None,
                    }
        except Exception as exc:
            logging.error("query_metric_source_failed exception=%s", type(exc).__name__)
            return {"date": date_text, "status": "failed"}
        return {"date": date_text, "status": "missing"}

    def get_queries_performance_batch(self, brand: str, query_list: List[str], target_date: datetime.date) -> Dict[str, Dict[str, Any]]:
        """批量获取多个 Query 在特定日期的表现指标 (优化 N+1 问题)"""
        if not query_list: return {}
        
        from src.config import get_brand_config
        config = get_brand_config(brand)
        table_name = config["table_name"]
        
        query_col, uv_col, click_col, pay_col, pv_col, atc_col = self._get_query_field_mapping(brand)

        # 使用 SQLAlchemy 的 tuple 处理 IN 子句
        sql = text(f"""
        SELECT 
            {query_col} as q_text,
            SUM({pv_col}) AS pv,
            SUM({uv_col}) AS uv,
            SUM({click_col}) AS click_uv,
            SUM({atc_col}) AS add_to_cart_uv,
            SUM({pay_col}) AS purchase_uv
        FROM {table_name}
        WHERE {query_col} IN :q_list AND `date` = :q_date
        GROUP BY 1
        """).bindparams(bindparam("q_list", expanding=True))
        
        results_map = {}
        try:
            with self.engine.connect() as conn:
                res = conn.execute(sql, {"q_list": query_list, "q_date": target_date.strftime('%Y-%m-%d')})
                for row in res:
                    uv = float(row[2] or 0)
                    click_uv = float(row[3] or 0)
                    add_to_cart_uv = float(row[4] or 0)
                    purchase_uv = float(row[5] or 0)
                    results_map[row[0]] = {
                        "date": target_date.strftime('%Y-%m-%d'),
                        "status": "complete",
                        "pv": int(row[1] or 0),
                        "uv": int(uv),
                        "search_pv": int(row[1] or 0),
                        "search_uv": int(uv),
                        "click_uv": click_uv,
                        "add_to_cart_uv": add_to_cart_uv,
                        "purchase_uv": purchase_uv,
                        "ctr": click_uv / uv if uv else None,
                        "atc": add_to_cart_uv / uv if uv else None,
                        "cvr": purchase_uv / uv if uv else None,
                    }
        except Exception:
            logging.error(f"批量追踪指标查询失败 ({brand}).")
        
        return results_map
