"""Shared rules for date-scoped Query governance check visibility."""

from __future__ import annotations

from collections.abc import Mapping, Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

CHECK_KEYS = ("recall", "open_factor", "relevance", "inventory")
_DISABLED = {"disabled", "off", "false", "0", "none"}


def strictest_execution_mode(*modes: str) -> str:
    """An Agent and each involved tool must all permit an automatic write."""
    priority = {"auto": 0, "human_approval": 1, "disabled": 2}
    normalized = []
    for mode in modes:
        value = str(mode or "human_approval").strip().lower()
        if value == "dry_run":
            value = "human_approval"
        if value not in priority:
            raise ValueError(f"unsupported execution mode: {value}")
        normalized.append(value)
    return max(normalized or ["human_approval"], key=priority.__getitem__)


def governance_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def effective_check_modes(config: Mapping[str, Any] | None) -> dict[str, bool]:
    """Resolve the four user-facing checks from the existing brand switches."""
    values = config if isinstance(config, Mapping) else {}
    modes = values.get("top_query_check_modes")
    modes = modes if isinstance(modes, Mapping) else {}

    def enabled_mode(key: str) -> bool:
        value = modes.get(key)
        return str(value).strip().lower() not in _DISABLED if value is not None else True

    tool_modes = values.get("top_query_tool_modes")
    tool_modes = tool_modes if isinstance(tool_modes, Mapping) else {}

    return {
        "recall": enabled_mode("recall"),
        "open_factor": (
            values.get("query_governance_reorder_enabled") is not False
            and enabled_mode("open_factor")
            and str(tool_modes.get("query_reorder") or "").strip().lower() not in _DISABLED
        ),
        "relevance": values.get("query_governance_relevance_enabled") is not False and enabled_mode("relevance"),
        "inventory": enabled_mode("inventory"),
    }


def visible_check_keys(
    configured: Mapping[str, Any] | None,
    *,
    observed: Iterable[str] = (),
) -> set[str]:
    """Show enabled checks, plus checks with an actual historical fact."""
    configured_keys = {key for key in CHECK_KEYS if bool((configured or {}).get(key))}
    observed_keys = {str(key) for key in observed if str(key) in CHECK_KEYS}
    return configured_keys | observed_keys


def result_has_disabled_reason(result: Mapping[str, Any] | None) -> bool:
    """Identify a persisted skip caused by a feature switch being disabled."""
    if not isinstance(result, Mapping):
        return False
    reason = result.get("reason") or result.get("errorMessage") or result.get("error_message")
    return "品牌未启用" in str(reason or "") or "disabled" in str(reason or "").lower()


def record_check_mode_changes(
    candidate: dict[str, Any], previous: Mapping[str, Any], *, changed_on: date,
) -> None:
    """Append internal effective-date records only when one of the four switches changes."""
    old_brands = previous.get("brand_configs") or {}
    new_brands = candidate.get("brand_configs") or {}
    history = candidate.setdefault("query_governance_check_history", {})
    for brand, raw_config in new_brands.items():
        if not isinstance(raw_config, Mapping):
            continue
        old_config = old_brands.get(brand) if isinstance(old_brands, Mapping) else None
        if not isinstance(old_config, Mapping):
            continue
        before = effective_check_modes(old_config)
        after = effective_check_modes(raw_config)
        if before == after:
            continue
        history.setdefault(brand, []).append({
            "changedOn": changed_on.isoformat(),
            "effectiveDate": (changed_on + timedelta(days=1)).isoformat(),
            "before": before,
            "after": after,
        })


def check_modes_for_date(
    raw_config: Mapping[str, Any], brand: str, data_date: date, *,
    for_display: bool = False, today: date | None = None,
) -> dict[str, bool] | None:
    """Resolve one brand/day without applying today's new switches to old records."""
    brands = raw_config.get("brand_configs") or {}
    current = effective_check_modes(brands.get(brand) if isinstance(brands, Mapping) else None)
    history = raw_config.get("query_governance_check_history") or {}
    entries = history.get(brand) if isinstance(history, Mapping) else None
    entries = [entry for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []
    if entries:
        ordered = sorted(entries, key=lambda entry: (str(entry.get("effectiveDate") or ""), str(entry.get("changedOn") or "")))
        applicable = [entry for entry in ordered if str(entry.get("effectiveDate") or "") <= data_date.isoformat()]
        if applicable:
            return {key: bool(applicable[-1].get("after", {}).get(key, True)) for key in CHECK_KEYS}
        first = ordered[0]
        if data_date.isoformat() >= str(first.get("changedOn") or "") or not for_display:
            return {key: bool(first.get("before", {}).get(key, True)) for key in CHECK_KEYS}
        return None
    if for_display and data_date < (today or governance_today()):
        return None
    return current
