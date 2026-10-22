from src.api.shared import BRAND_CONFIG_EDITABLE_KEYS
from src.config import DEFAULT_BRAND_BUSINESS_CONFIG
from src.query_governance.behavior_evidence import BehaviorEvidenceConfig
from src.query_governance.relevance_config import DEFAULT_RELEVANCE_RUNTIME_CONFIG
from src.query_governance.vector_reorder import DEFAULT_VECTOR_REORDER_TIMEOUT_SECONDS


def test_brand_defaults_use_uv_and_unified_agent_timeouts():
    assert DEFAULT_BRAND_BUSINESS_CONFIG["query_governance_query_min_metric_type"] == "uv"
    assert DEFAULT_BRAND_BUSINESS_CONFIG["query_governance_query_min_metric_value"] == 5
    assert DEFAULT_BRAND_BUSINESS_CONFIG["query_governance_preference_generation_timeout_seconds"] == 1200
    assert DEFAULT_RELEVANCE_RUNTIME_CONFIG["query_governance_relevance_model_timeout_seconds"] == 1200
    assert DEFAULT_VECTOR_REORDER_TIMEOUT_SECONDS == 10


def test_retired_runtime_limits_are_not_exposed_as_brand_configuration():
    retired = {
        "query_governance_preference_behavior_evidence_fallback_days",
        "query_governance_preference_generation_max_completion_tokens",
        "query_governance_vector_reorder_timeout_seconds",
        "query_governance_relevance_request_max_bytes",
        "query_governance_relevance_max_completion_tokens",
    }
    assert retired.isdisjoint(BRAND_CONFIG_EDITABLE_KEYS)
