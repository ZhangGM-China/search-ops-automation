"""Pure protocols for relevance-error remediation.

The module deliberately contains no external writes.  It normalizes the
evidence passed to a future relevance-root-cause Agent and turns its proposed
KG operations into a small, auditable action-step contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .image_integrity import SUSPECT_IMAGE_WARNING_CODES
from .kg_action_contract import ACTION_ENDPOINTS, normalize_kg_actions
from .kg_evidence import graph_context, digest, validate_actions
from .read_only_agent import run_read_only_agent
from .kg_prompts import build_relevance_kg_prompt
from .relevance_kg_contract import RELEVANCE_KG_OUTPUT_SCHEMA, validate_relevance_output

SUPPORTED_BLACKLIST_MODES = frozenset({"auto", "human_approval", "disabled"})
SUPPORTED_KG_ACTIONS = frozenset(ACTION_ENDPOINTS)
SUPPORTED_REMEDIATION_ACTIONS = SUPPORTED_KG_ACTIONS | {"product_blacklist"}


class RelevanceGovernanceAgent:
    """Independent semantic agent for grouping one Query's wrong products.

    The agent only returns a validated analysis payload.  It has no write tool;
    KG plans are generated and executed by the separate governed KG boundary.
    """

    from .kg_prompts import RELEVANCE_KG_PROMPT as _SYSTEM_PROMPT

    def __init__(self, *, runtime: Any, kg_tools: Any | None = None, product_details: Any | None = None, pi_client: Any | None = None, reference_provider: Any | None = None) -> None:
        self.runtime = runtime
        self.observability = None
        self.kg_tools = kg_tools
        self.product_details = product_details
        self.pi_client = pi_client
        self.reference_provider = reference_provider

    def analyze(
        self,
        *,
        brand: str,
        query_text: str,
        evidence: Mapping[str, Any] | None,
        langfuse_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        from src.governance.query_identity import transform_governance_payload_query

        query = transform_governance_payload_query(brand, query_text)
        candidates_input = [
            {"product_id": str(item.get("product_id") or item.get("productId") or ""),
             "detection_reason": str(item.get("reason") or "")}
            for item in (evidence or {}).get("blacklist_candidates", [])
            if isinstance(item, Mapping)
        ]
        request = {"query": query, "wrongProducts": candidates_input}
        reference = self.reference_provider(brand) if callable(self.reference_provider) else ""
        request["reference"] = reference
        tool_results: list[dict[str, Any]] = []
        analysis_warnings: list[str] = []
        missing_detail_ids: set[str] = set()
        group_audits: dict[tuple[str, str], dict[str, Any]] = {}
        output_errors: list[str] = []
        pi_result: dict[str, Any] = {}

        def add_warning(message: str) -> None:
            normalized = str(message or "").strip()
            if normalized and normalized not in analysis_warnings:
                analysis_warnings.append(normalized)

        if self.kg_tools is not None and (self.product_details is not None or self.pi_client is not None):
            allowed_ids = {item["product_id"] for item in candidates_input if item["product_id"]}

            def product_details(*, _agent_observation: Any | None = None) -> Mapping[str, Any]:
                try:
                    rows = self.product_details.fetch(brand, sorted(allowed_ids))
                except Exception as exc:
                    detail = str(exc).strip() or exc.__class__.__name__
                    add_warning(f"商品详情读取失败：{detail}")
                    return {product_id: None for product_id in sorted(allowed_ids)}
                if not isinstance(rows, Mapping):
                    add_warning("商品详情返回结构无效")
                    return {product_id: None for product_id in sorted(allowed_ids)}
                return {
                    product_id: dict(rows[product_id]) if isinstance(rows.get(product_id), Mapping) else None
                    for product_id in sorted(allowed_ids)
                }

            def query_tag(*, tag_name: str, _agent_observation: Any | None = None) -> Mapping[str, Any]:
                return self.kg_tools.query_tag(brand, tag_name, langfuse_config=langfuse_config, parent_observation=_agent_observation)

            def query_relationship(*, tag_name: str, side: str = "both", _agent_observation: Any | None = None) -> Mapping[str, Any]:
                return self.kg_tools.query_relationship(brand, tag_name, side=side, langfuse_config=langfuse_config, parent_observation=_agent_observation)

            def search_products(*, query: str, _agent_observation: Any | None = None) -> Mapping[str, Any]:
                return self.kg_tools.search_products(brand, query, langfuse_config=langfuse_config, parent_observation=_agent_observation)

            # The PI production path reads product details through the
            # isolated MCP tools.  Keep the direct fetch only for the local
            # unit-test seam and the legacy fallback path.
            prepared_details = {} if self.pi_client is not None else product_details()
            request["sceneId"] = self.kg_tools.scene_id(brand)
            request["wrongProducts"] = [
                {**item, **({"product_details": prepared_details.get(item["product_id"])} if self.pi_client is None else {})}
                for item in candidates_input
            ]

            def cached_details(*, _agent_observation=None):
                return prepared_details

            def query_dimension(*, dimension_name: str, _agent_observation=None):
                return self.kg_tools.query_dimension(brand, dimension_name, langfuse_config=langfuse_config, parent_observation=_agent_observation)

            # A deterministic minimum investigation prevents finalizing after only
            # rereading the cached product details. Preserve phrases intact.
            preflight = []
            if self.pi_client is None:
                for name, args, method in (
                    ("query_tag", {"tag_name": query}, query_tag),
                    ("query_relationship", {"tag_name": query, "side": "both"}, query_relationship),
                    ("query_dimension", {"dimension_name": ""}, query_dimension),
                ):
                    try:
                        output = method(**args)
                    except Exception as exc:
                        output = {"error": str(exc).strip() or exc.__class__.__name__}
                    preflight.append({"name": name, "args": args, "result": output})
            request["kgPreflight"] = preflight
            if self.pi_client is not None:
                # Production path: PI owns the multi-turn loop and invokes only
                # the read-only MCP tools registered by its isolated task.
                pi_result = self.pi_client.run(
                    runtime=self.runtime, brand=brand, scene_id=request["sceneId"],
                    request={**request, "kgPreflight": preflight},
                    system_prompt=build_relevance_kg_prompt(reference, RELEVANCE_KG_OUTPUT_SCHEMA),
                    output_schema=RELEVANCE_KG_OUTPUT_SCHEMA,
                    observability=self.observability, langfuse_config=langfuse_config,
                )
                payload = pi_result["output"]
                tool_results = list(pi_result["toolResults"])
                try:
                    payload = validate_relevance_output(payload, set(allowed_ids))
                except ValueError as exc:
                    output_errors.append(str(exc))
                    add_warning("KG 策略输出不符合契约，需人工复核：" + str(exc))
            else:
                payload, tool_results = run_read_only_agent(
                    runtime=self.runtime, system_prompt=self._SYSTEM_PROMPT, user_input=request,
                    tools={"product_details": cached_details, "query_tag": query_tag, "query_dimension": query_dimension,
                           "query_relationship": query_relationship, "search_products": search_products},
                    langfuse_config=langfuse_config, observability=self.observability,
                    agent_name="query-governance.relevance-agent", max_tool_calls=24,
                    tool_observation_names={"product_details"},
                    response_format={"type": "json_object"}, initial_tool_results=preflight,
                )
            details = prepared_details
            missing = (
                []
                if self.pi_client is not None
                else (
                    sorted(allowed_ids - {key for key, value in details.items() if isinstance(value, Mapping)})
                    if isinstance(details, Mapping) else sorted(allowed_ids)
                )
            )
            missing_detail_ids.update(missing)
            if missing and not any(
                warning.startswith(("商品详情读取失败：", "商品详情返回结构无效"))
                for warning in analysis_warnings
            ):
                add_warning("商品详情缺失：" + ", ".join(missing))
        else:
            # Keep the pure unit-test seam; production always supplies tools.
            messages = [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ]
            result = self.runtime.invoke(messages, **dict(langfuse_config or {})) if langfuse_config else self.runtime.invoke(messages)
            content = getattr(getattr(result, "response", result), "content", result)
            payload = json.loads(content) if isinstance(content, str) else {}
        raw_output = payload
        if not isinstance(payload, Mapping) or not isinstance(payload.get("groups"), list):
            if self.pi_client is None:
                raise ValueError("root cause agent groups must be an array")
            payload = {"groups": []}

        candidates = {
            str(item.get("product_id") or item.get("productId") or "").strip(): item
            for item in (evidence or {}).get("blacklist_candidates", [])
            if isinstance(item, Mapping)
            and str(item.get("product_id") or item.get("productId") or "").strip()
        }
        grouped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(payload["groups"], start=1):
            if not isinstance(raw, Mapping):
                continue
            root_cause = str(raw.get("rootCause") or raw.get("root_cause") or "uncertain").strip().lower()
            if root_cause not in {"individual", "kg_issue", "uncertain"}:
                root_cause = "uncertain"
            product_ids = []
            for value in raw.get("productIds") or raw.get("product_ids") or []:
                product_id = str(value or "").strip()
                if product_id and product_id in candidates and product_id not in seen and product_id not in missing_detail_ids:
                    product_ids.append(product_id)
                    seen.add(product_id)
            if not product_ids:
                continue
            cause_key = str(raw.get("causeKey") or raw.get("cause_key") or root_cause).strip() or root_cause
            candidate_rows = [candidates[product_id] for product_id in product_ids]
            reason = str(raw.get("reason") or "").strip()

            normalized_actions: list[dict[str, Any]] = []
            if root_cause == "kg_issue" and self.kg_tools is not None:
                raw_actions = raw.get("actions", [])
                audit = {"rawActions": raw_actions, "validationErrors": [], "repairAttempts": 0}

                def check_actions(values):
                    if output_errors:
                        raise ValueError("；".join(output_errors))
                    actions = normalize_kg_actions(values)
                    if not actions:
                        raise ValueError("KG 问题组未提供可执行动作，需人工复核")
                    context = graph_context(tool_results)
                    if not context["reads"]:
                        raise ValueError("缺少图谱查询证据")
                    validate_actions(actions, context, self.kg_tools.scene_id(brand))
                    return actions

                try:
                    normalized_actions = check_actions(raw_actions)
                except ValueError as exc:
                    audit["validationErrors"] = [str(exc).strip()]
                    normalized_actions = []
                    add_warning(f"KG 动作格式无效（{cause_key}）：" + str(exc).strip())
                group_audits[(root_cause, cause_key)] = audit
            group_candidates = [
                {
                    **candidate,
                    "root_cause": root_cause,
                    "cause_key": cause_key,
                    "reason": reason or candidate.get("reason") or "",
                    "confidence": raw.get("confidence"),
                    "kg_actions": (
                        normalized_actions
                        if root_cause == "kg_issue" and self.kg_tools is not None
                        else (raw.get("actions") or []) if root_cause == "kg_issue" else (
                            []
                        )
                    ),
                }
                for candidate in candidate_rows
            ]
            grouped.extend(group_candidates)
        # Preserve any product that the model omitted as uncertain rather than
        # silently dropping it from the governance record.
        for product_id, candidate in candidates.items():
            if product_id not in seen:
                reason = "商品详情缺失，需人工确认" if product_id in missing_detail_ids else candidate.get("reason")
                grouped.append({**candidate, "root_cause": "uncertain", "cause_key": "uncertain", "reason": reason})
        result_payload = build_relevance_remediation_payload(
            query=query,
            candidates=grouped,
            query_tags=(evidence or {}).get("queryTags") or (evidence or {}).get("query_tags") or [],
            kg_context=(evidence or {}).get("kgContext") or (evidence or {}).get("kg_context") or {},
            warnings=analysis_warnings,
        )
        if self.pi_client is not None:
            result_payload["raw_output"] = raw_output
            result_payload["validation_errors"] = output_errors
            result_payload["model_audit"] = pi_result.get("audit", {})
            result_payload["raw_text"] = pi_result.get("rawText", "")
        if self.kg_tools is not None:
            # Keep valid reads even when another read failed; full tool results
            # retain those failures and validation still checks the full set.
            valid_calls = []
            for call in tool_results:
                try:
                    graph_context([call])
                    valid_calls.append(call)
                except ValueError:
                    pass
            context = graph_context(valid_calls)
            result_payload["graph_context"] = context
            result_payload["tool_evidence"] = {"calls": tool_results}
            for group in result_payload["causeGroups"]:
                group["graphContext"] = context
                group["graphContextDigest"] = digest(context)
                group["coreTags"] = list(dict.fromkeys(read["args"]["tag_name"] for read in context["reads"] if "tag_name" in read["args"]))
                group.update(group_audits.get((group["rootCause"], group["causeKey"]), {}))
        return result_payload



def _candidate_product_ids(item: Mapping[str, Any]) -> list[str]:
    raw_ids = item.get("product_ids") or item.get("productIds")
    if raw_ids is None:
        raw_ids = [item.get("product_id") or item.get("productId")]
    elif isinstance(raw_ids, (str, bytes)):
        raw_ids = [raw_ids]
    ids: list[str] = []
    for raw_id in raw_ids:
        product_id = str(raw_id or "").strip()
        if product_id and product_id not in ids:
            ids.append(product_id)
    return ids


def image_suspect_product_ids(candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    """Products whose image is *itself* suspect (shared / mismatched).

    A product that simply has no image keeps the historical behaviour: the
    relevance model still judges it from text and the configured mode still
    applies.  A product whose image is known to be wrong (``IMAGE_URL_SHARED``
    during the Harbor incident) must never be blacklisted automatically -- that is
    how 542 innocent products almost got written off.
    """

    ids: list[str] = []
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("image_warning") or item.get("imageWarning") or "").strip().upper()
        if code not in SUSPECT_IMAGE_WARNING_CODES:
            continue
        for product_id in _candidate_product_ids(item):
            if product_id not in ids:
                ids.append(product_id)
    return ids


def decide_blacklist_handling(
    candidates: Sequence[Mapping[str, Any]], *, execution_mode: str | None = None
) -> dict[str, Any]:
    """Return the deterministic action decision for the configured mode."""

    mode = str(execution_mode or "auto").strip().lower()
    if mode not in SUPPORTED_BLACKLIST_MODES:
        raise ValueError(f"unsupported product blacklist execution mode: {mode or 'empty'}")
    product_ids: list[str] = []
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        for product_id in _candidate_product_ids(item):
            if product_id not in product_ids:
                product_ids.append(product_id)
    # 治理护栏：图片本身可疑（共用/错配）时禁止自动拉黑，强制人工复核。
    suspect = image_suspect_product_ids(candidates)
    downgraded = mode == "auto" and bool(suspect)
    effective_mode = "human_approval" if downgraded else mode
    return {
        "executionMode": effective_mode,
        "shouldExecute": effective_mode == "auto" and bool(product_ids),
        "shouldPersistPending": effective_mode == "human_approval" and bool(product_ids),
        "recordOnly": effective_mode == "disabled" and bool(product_ids),
        "productIds": product_ids,
        "imageSuspectProductIds": suspect,
        "imageAutoDowngraded": downgraded,
    }


def _normalize_action_steps(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        return []
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(actions, start=1):
        if not isinstance(raw, Mapping):
            continue
        action_type = str(raw.get("actionType") or raw.get("action_type") or "").strip()
        if action_type not in SUPPORTED_REMEDIATION_ACTIONS:
            raise ValueError(f"unsupported KG remediation action: {action_type or 'empty'}")
        depends_on = raw.get("dependsOn") or raw.get("depends_on") or []
        if isinstance(depends_on, (str, bytes)):
            depends_on = [depends_on]
        normalized.append(
            {
                "step": index,
                "actionType": action_type,
                "dependsOn": [int(item) for item in depends_on if str(item).isdigit()],
                "params": dict(raw.get("params") or {}),
                "riskLevel": str(raw.get("riskLevel") or raw.get("risk_level") or "medium"),
                "rollbackPlan": list(raw.get("rollbackPlan") or raw.get("rollback_plan") or []),
            }
        )
    return normalized


def _cause_group_key(candidate: Mapping[str, Any]) -> tuple[str, str]:
    root_cause = str(candidate.get("root_cause") or candidate.get("rootCause") or "uncertain").strip().lower()
    if root_cause not in {"individual", "kg_issue", "uncertain"}:
        root_cause = "uncertain"
    cause_key = str(candidate.get("cause_key") or candidate.get("causeKey") or root_cause).strip()
    return root_cause, cause_key


def build_relevance_remediation_payload(
    *,
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    query_tags: Sequence[str] | None = None,
    kg_context: Mapping[str, Any] | None = None,
    warnings: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Group all wrong products under one Query into independent cause groups."""

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        product_id = str(candidate.get("product_id") or candidate.get("productId") or "").strip()
        if not product_id:
            continue
        root_cause, cause_key = _cause_group_key(candidate)
        group = groups.setdefault(
            (root_cause, cause_key),
            {
                "groupId": f"{root_cause}:{cause_key}",
                "rootCause": root_cause,
                "causeKey": cause_key,
                "productIds": [],
                "reasons": [],
                "actions": [],
                "needKgAdjustment": root_cause == "kg_issue",
                "analysisEvidence": [],
                "confidence": None,
            },
        )
        if product_id not in group["productIds"]:
            group["productIds"].append(product_id)
        reason = str(candidate.get("reason") or "").strip()
        if reason and reason not in group["reasons"]:
            group["reasons"].append(reason)
            group["analysisEvidence"].append(reason)
        confidence = candidate.get("confidence")
        if confidence is not None and group["confidence"] is None:
            group["confidence"] = confidence
        proposed_actions = candidate.get("kg_actions") or candidate.get("kgActions")
        if proposed_actions:
            for step in _normalize_action_steps(proposed_actions):
                if step not in group["actions"]:
                    group["actions"].append(step)

    normalized_groups: list[dict[str, Any]] = []
    for group in groups.values():
        if group["rootCause"] == "individual":
            conclusion = "个例，需拉黑"
        elif group["rootCause"] == "kg_issue":
            conclusion = group["reasons"][0] if group["reasons"] else "知识图谱问题，需调整"
        else:
            conclusion = "根因待人工确认"
        normalized_groups.append(
            {
                "groupId": group["groupId"],
                "rootCause": group["rootCause"],
                "causeKey": group["causeKey"],
                "productIds": group["productIds"],
                "conclusion": conclusion,
                "needKgAdjustment": group["needKgAdjustment"],
                "actions": group["actions"],
                "reasons": group["reasons"],
                "analysisEvidence": group["analysisEvidence"],
                "confidence": group["confidence"],
            }
        )
    payload = {
        "query": str(query or ""),
        "queryTags": list(dict.fromkeys(str(tag).strip() for tag in (query_tags or []) if str(tag).strip())),
        "kgContext": dict(kg_context) if isinstance(kg_context, Mapping) else {},
        "errorProductIds": [
            product_id
            for group in normalized_groups
            for product_id in group["productIds"]
        ],
        "causeGroups": normalized_groups,
    }
    normalized_warnings = list(dict.fromkeys(
        str(item).strip() for item in (warnings or []) if str(item).strip()
    ))
    if normalized_warnings:
        payload["warnings"] = normalized_warnings
    return payload


__all__ = [
    "SUPPORTED_BLACKLIST_MODES",
    "SUPPORTED_KG_ACTIONS",
    "build_relevance_remediation_payload",
    "decide_blacklist_handling",
    "RelevanceGovernanceAgent",
    "RelevanceRootCauseAgent",
]

# Backward-compatible import for persisted worker wiring and callers from the
# previous root-cause-only name.
RelevanceRootCauseAgent = RelevanceGovernanceAgent
