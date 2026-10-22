from typing import Any, Mapping


AUTHORITATIVE_QUERY_DEMOTION_V1 = "authoritative_query_demote_v1"
LEGACY_QUERY_DEMOTION_BLOCK_REASON = "legacy_query_demote_contract"
LEGACY_PENDING_STATUSES = frozenset({
    "approved",
    "pending",
    "pending_confirmation",
    "waiting_human_approval",
})
LEGACY_RECONCILABLE_STATUSES = frozenset({
    "execution_unknown",
    "reconciling",
})


def query_demotion_execution_contract(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return ""
    value = payload.get("execution_contract")
    if not isinstance(value, str):
        return ""
    return value.strip()


def uses_authoritative_query_demotion(payload: Mapping[str, Any] | None) -> bool:
    return query_demotion_execution_contract(payload) == AUTHORITATIVE_QUERY_DEMOTION_V1


def should_block_legacy_query_demotion(
    *,
    env: str,
    status: str,
    payload: Mapping[str, Any] | None,
) -> bool:
    return (
        str(env or "test").strip().lower() == "prod"
        and str(status or "").strip().lower() in LEGACY_PENDING_STATUSES
        and not uses_authoritative_query_demotion(payload)
    )
