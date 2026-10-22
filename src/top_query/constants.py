EXECUTION_MODE_DISABLED = "disabled"
EXECUTION_MODE_HUMAN_APPROVAL = "human_approval"
EXECUTION_MODE_AUTO = "auto"

EXECUTION_MODES = (
    EXECUTION_MODE_DISABLED,
    EXECUTION_MODE_HUMAN_APPROVAL,
    EXECUTION_MODE_AUTO,
)

QUERY_STATUSES = {
    "pending",
    "collecting_context",
    "executing",
    "completed",
    "manual_required",
    "skipped",
    "failed",
}

STEP_STATUSES = {
    "pass",
    "fixed",
    "failed",
    "warning",
    "check_failed",
    "waiting_human_approval",
}

DISPLAY_STATUSES = {
    "normal": "正常",
    "fixed": "已处理",
    "waiting_human_approval": "需处理待确认",
    "still_problematic": "处理后仍有问题",
}

PLANNER_EXECUTOR_KEYS = (
    "recall",
    "relevance",
    "open_factor",
    "inventory",
)

CHECK_ITEM_KEYS = PLANNER_EXECUTOR_KEYS

PLANNER_TOOL_KEYS = (
    "product_blacklist",
    "query_demote",
    "tag_bucket_config",
    "kg_tag",
    "kg_relation",
)

TOOL_KEYS = (
    *PLANNER_TOOL_KEYS,
    "query_reorder",
)

DEFAULT_TOP_QUERY_CHECK_MODES = {
    "recall": EXECUTION_MODE_HUMAN_APPROVAL,
    "relevance": EXECUTION_MODE_AUTO,
    "open_factor": EXECUTION_MODE_AUTO,
    "inventory": EXECUTION_MODE_AUTO,
}

DEFAULT_TOP_QUERY_TOOL_MODES = {
    "product_blacklist": EXECUTION_MODE_AUTO,
    "query_demote": EXECUTION_MODE_AUTO,
    "tag_bucket_config": EXECUTION_MODE_AUTO,
    "kg_tag": EXECUTION_MODE_HUMAN_APPROVAL,
    "kg_relation": EXECUTION_MODE_HUMAN_APPROVAL,
    "query_reorder": EXECUTION_MODE_AUTO,
    "whitelist_tag": EXECUTION_MODE_AUTO,
    "query_whitelist": EXECUTION_MODE_AUTO,
}


QUERY_DEMOTION_BUCKET_ORDER = (
    "top",
    "productIdsByAgent",
    "productIdsByCode",
    "productIdsByProductNameTag",
    "productIdsByCodeSpellCorrections",
    "productIdsByTagsAll",
    "productIdsByWhiteList",
    "productIdsBySubTag",
    "productIdsByHighLowGroup",
    "productIdsByMultiCategory",
    "productIdsByTagNameDowngrade",
    "productIdsByOpsLowWeight",
    "productIdsByLowWeight",
    "productIdsByFuzzy",
    "productIdsByProductNameFuzzy",
    "productIdsByTagFuzzy",
    "productIdsByFuzzySpellCorrections",
)

QUERY_DEMOTION_LOW_WEIGHT_BUCKETS = frozenset({
    "productIdsByOpsLowWeight",
    "productIdsByLowWeight",
})

QUERY_DEMOTION_PROTECTED_SOURCE_BUCKETS = frozenset({
    "top",
    "productIdsByOpsLowWeight",
    "productIdsByLowWeight",
    "productIdsByFuzzy",
    "productIdsByProductNameFuzzy",
    "productIdsByTagFuzzy",
    "productIdsByFuzzySpellCorrections",
    "productIdsByNoStock",
})


def query_demotion_source_bucket_is_eligible(bucket: str) -> bool:
    return str(bucket or "").strip() not in QUERY_DEMOTION_PROTECTED_SOURCE_BUCKETS
