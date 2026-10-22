"""Small shared state for API application handlers."""

from __future__ import annotations

import copy
import threading
import time
from typing import Dict, Any
from src.integrations.operational_service import OperationalService
from src.api.redis_cache import RedisCache

_top_query_endpoint_cache_lock = threading.Lock()
_redis_cache = RedisCache()

_top_query_endpoint_cache: Dict[str, Dict[tuple, Dict[str, Any]]] = {
    "query_governance_overview": {},
    "current_result_count": {},
    "product_detail": {},
    "snapshot_validate": {},
    "query_governance_brand_queries": {},
    "query_governance_brand_summary": {},
}


def _top_query_cache_get(namespace: str, key: tuple):
    remote_value = _redis_cache.get(namespace, key)
    if remote_value is not None:
        return remote_value
    now = time.monotonic()
    with _top_query_endpoint_cache_lock:
        record = _top_query_endpoint_cache.get(namespace, {}).get(key)
        if not record:
            return None
        if float(record.get("expires_at") or 0) <= now:
            _top_query_endpoint_cache.get(namespace, {}).pop(key, None)
            return None
        return copy.deepcopy(record.get("value"))


def _top_query_cache_set(
    namespace: str,
    key: tuple,
    value: Any,
    ttl_seconds: float,
):
    _redis_cache.set(namespace, key, value, ttl_seconds=max(1, int(ttl_seconds)))
    with _top_query_endpoint_cache_lock:
        bucket = _top_query_endpoint_cache.setdefault(namespace, {})
        bucket[key] = {
            "value": copy.deepcopy(value),
            "expires_at": time.monotonic() + ttl_seconds,
        }


def _top_query_cache_invalidate(namespace: str, predicate):
    _redis_cache.invalidate_namespace(namespace)
    with _top_query_endpoint_cache_lock:
        bucket = _top_query_endpoint_cache.get(namespace, {})
        for key in list(bucket.keys()):
            if predicate(key):
                bucket.pop(key, None)


def _clear_top_query_endpoint_caches():
    for namespace in tuple(_top_query_endpoint_cache):
        invalidate = getattr(_redis_cache, "invalidate_namespace", None)
        if invalidate is not None:
            invalidate(namespace)
    with _top_query_endpoint_cache_lock:
        for bucket in _top_query_endpoint_cache.values():
            bucket.clear()


NO_RESULT_CONFIG_KEYS = [
    "no_result_governance_enabled",
    "no_result_execution_mode",
    "no_result_extract_limit",
    "no_result_search_pv_threshold",
    "no_result_search_uv_threshold",
    "no_result_excluded_keywords",
    "no_result_auto_max_risk",
    "no_result_retag_cooldown_hours",
    "no_result_review_wait_minutes",
    "no_result_top_n",
    "no_result_agent_max_rounds",
    "no_result_ignore_ttl_days",
]

TOP_QUERY_CONFIG_KEYS = [
    "top_query_governance_enabled",
    "query_governance_extract_limit",
    "query_governance_query_concurrency",
    "query_governance_query_min_metric_type",
    "query_governance_query_min_metric_value",
    "query_governance_excluded_keywords",
    "query_governance_candidate_high_pv_limit",
    "query_governance_candidate_alert_ctr_gap_pp",
    "query_governance_candidate_alert_cvr_gap_pp",
    "query_governance_candidate_history_days",
    "query_governance_candidate_new_query_pv_threshold",
    "query_governance_candidate_pv_growth_ratio_floor",
    "query_governance_candidate_pv_growth_sqrt_multiplier",
    "query_governance_candidate_pv_growth_abs_floor",
    "query_governance_preference_top_purchase_product_limit",
    "query_governance_preference_top_click_product_limit",
    "query_governance_preference_generation_batch_query_limit",
    "query_governance_preference_generation_concurrency",
    "query_governance_preference_generation_timeout_seconds",
    "query_governance_kg_agent_timeout_seconds",
    "query_governance_reorder_enabled",
    "query_governance_vector_reorder_concurrency",
    "query_governance_vector_reorder_retry_attempts",
    "query_governance_vector_reorder_retry_backoff_seconds",
    "query_governance_relevance_enabled",
    "query_governance_relevance_multimodal_concurrency",
    "query_governance_relevance_batch_size",
    "query_governance_relevance_model_timeout_seconds",
    "query_governance_relevance_image_download_timeout_seconds",
    "query_governance_relevance_blacklist_ratio_threshold",
    "query_governance_relevance_max_rounds",
    "query_governance_relevance_buckets",
    "query_governance_recall_rate_enabled",
    "query_governance_recall_rate_product_batch_size",
    "query_governance_recall_rate_score_threshold",
    "query_governance_recall_rate_jev_threshold",
    "top_query_check_product_limit",
    "top_query_low_result_threshold",
    "top_query_result_drop_threshold",
    "top_query_inventory_broken_rate_threshold",
    "top_query_inventory_check_interval_minutes",
    "top_query_inventory_max_cycles",
    "top_query_token_limit",
    "top_query_world_knowledge_config",
    "query_governance_kg_reference",
    "top_query_check_modes",
    "top_query_tool_modes",
]

BRAND_CONFIG_EDITABLE_KEYS = [
    "enabled",
    *NO_RESULT_CONFIG_KEYS,
    *TOP_QUERY_CONFIG_KEYS,
]

SAFETY_CONFIG_EDITABLE_KEYS = (
    "circuit_breaker_threshold",
    "circuit_breaker_cooldown_seconds",
    "max_retry_count",
    "api_delay_threshold",
    "health_check_stale_minutes",
    "health_check_error_rate_threshold_percent",
    "health_check_api_p95_threshold_seconds",
)

service = OperationalService()

__all__ = tuple(
    name for name in globals()
    if not name.startswith("__") and name != "router"
)
