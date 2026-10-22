"""Persistent relevance correction rules and their operator audit trail."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import select

from src.governance.query_identity import transform_governance_payload_query

from .models import (
    QueryGovernanceRelevanceGuidance,
    QueryGovernanceRelevanceGuidanceReason,
)
from .models import QuerySpec


class GuidanceNotFoundError(LookupError):
    pass


def _identity(brand: str, query: str) -> tuple[str, str]:
    normalized_brand = str(brand or "").strip()
    normalized_query = transform_governance_payload_query(normalized_brand, query)
    if not normalized_brand or not normalized_query:
        raise ValueError("brand and query are required")
    return normalized_brand, normalized_query


def _rule_payload(row: QueryGovernanceRelevanceGuidance) -> dict[str, Any]:
    return {
        "id": row.id,
        "brand": row.brand,
        "query": row.query_text,
        "rules": row.rules_text or "",
        "status": row.status,
        "updatedBy": row.updated_by,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def _beijing_wall_clock(value: datetime | None) -> str | None:
    """Render a naive-UTC instant stored in the database as Beijing wall-clock time."""
    if value is None:
        return None
    instant = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return instant.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def _reason_payload(row: QueryGovernanceRelevanceGuidanceReason) -> dict[str, Any]:
    return {
        "id": row.id,
        "productId": row.product_id,
        "reason": row.reason_text,
        "operator": row.operator,
        "createdAt": _beijing_wall_clock(row.created_at),
    }


def _reason_history(session: Any, brand: str, query: str) -> list[dict[str, Any]]:
    reasons = session.execute(
        select(QueryGovernanceRelevanceGuidanceReason).where(
            QueryGovernanceRelevanceGuidanceReason.brand == brand,
            QueryGovernanceRelevanceGuidanceReason.query_text == query,
        ).order_by(QueryGovernanceRelevanceGuidanceReason.id)
    ).scalars().all()
    return [_reason_payload(reason) for reason in reasons]


def _guidance_payload(session: Any, row: QueryGovernanceRelevanceGuidance) -> dict[str, Any]:
    """Rule payload plus its reason history, so every write response keeps the audit trail."""
    payload = _rule_payload(row)
    payload["reasonHistory"] = _reason_history(session, row.brand, row.query_text)
    return payload


def _response_finish_reason(response: Any) -> str:
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, Mapping):
        return str(metadata.get("finish_reason") or "")
    return ""


def _strip_mysql_utf8_unsupported_chars(value: str) -> str:
    """Keep text encodable by MySQL's three-byte ``utf8`` character set."""
    return "".join(
        char for char in value
        if ord(char) <= 0xFFFF and not 0xD800 <= ord(char) <= 0xDFFF
    )


def _rule_lines(rules: str) -> list[str]:
    return [
        re.sub(r"^(?:[-*•]|\d+[.、)])\s*", "", line.strip()).strip()
        for line in str(rules or "").splitlines()
        if line.strip()
    ]


def _append_rule_text(existing: str, addition: str) -> tuple[str, bool]:
    lines = _rule_lines(existing)
    additions = _rule_lines(addition)
    if not additions:
        raise ValueError("rule is required")
    unique = [line for line in additions if line not in lines]
    updated = "\n".join(f"{index}. {line}" for index, line in enumerate([*lines, *unique], 1))
    if len(updated) > 20000:
        raise ValueError("rules exceed 20000 characters")
    return updated, bool(unique)


@dataclass
class RelevanceGuidanceService:
    session_factory: Callable[[], Any]
    organizer_runtime: Any | None = None
    organizer_runtime_factory: Callable[[], Any] | None = None
    observability: Any | None = None

    def get(self, brand: str, query: str) -> dict[str, Any]:
        brand, query = _identity(brand, query)
        with self.session_factory() as session:
            row = session.execute(
                select(QueryGovernanceRelevanceGuidance).where(
                    QueryGovernanceRelevanceGuidance.brand == brand,
                    QueryGovernanceRelevanceGuidance.query_text == query,
                )
            ).scalar_one_or_none()
            reason_history = _reason_history(session, brand, query)
            if row is None:
                return {
                    "id": None,
                    "brand": brand,
                    "query": query,
                    "rules": "",
                    "status": "inactive",
                    "updatedBy": None,
                    "updatedAt": None,
                    "reasonHistory": reason_history,
                }
            payload = _rule_payload(row)
            payload["reasonHistory"] = reason_history
            return payload

    def list(self, brand: str, *, status: str | None = None) -> list[dict[str, Any]]:
        normalized_brand = str(brand or "").strip()
        if not normalized_brand:
            raise ValueError("brand is required")
        with self.session_factory() as session:
            statement = select(QueryGovernanceRelevanceGuidance).where(
                QueryGovernanceRelevanceGuidance.brand == normalized_brand
            )
            if status:
                statement = statement.where(QueryGovernanceRelevanceGuidance.status == status)
            rows = session.execute(statement.order_by(QueryGovernanceRelevanceGuidance.query_text)).scalars().all()
            return [_rule_payload(row) for row in rows]

    def save(self, brand: str, query: str, rules: str, *, operator: str) -> dict[str, Any]:
        brand, query = _identity(brand, query)
        normalized_rules = _strip_mysql_utf8_unsupported_chars(str(rules or "")).strip()
        if len(normalized_rules) > 20000:
            raise ValueError("rules exceed 20000 characters")
        with self.session_factory() as session:
            row = self._get_row(session, brand, query)
            if row is None:
                row = QueryGovernanceRelevanceGuidance(
                    brand=brand,
                    query_text=query,
                    rules_text=normalized_rules,
                    status="active",
                    updated_by=str(operator or "unknown"),
                )
                session.add(row)
            else:
                row.rules_text = normalized_rules
                row.status = "active"
                row.updated_by = str(operator or "unknown")
            session.commit()
            session.refresh(row)
            return _guidance_payload(session, row)

    def append_rule(
        self,
        brand: str,
        query: str,
        rule: str,
        *,
        operator: str,
        session: Any | None = None,
    ) -> dict[str, Any]:
        """Append an operator-written rule.

        ``session`` lets a guarded action write the rule in its own local
        transaction; the rule text always comes from the caller's input and is
        never derived from a cancellation reason.
        """
        brand, query = _identity(brand, query)
        normalized_rule = _strip_mysql_utf8_unsupported_chars(str(rule or ""))
        if session is not None:
            return self._append_rule_with_session(session, brand, query, normalized_rule, operator)
        with self.session_factory() as session:
            payload = self._append_rule_with_session(session, brand, query, normalized_rule, operator)
            session.commit()
            session.refresh(payload["_row"])
            return _guidance_payload(session, payload["_row"])

    def _append_rule_with_session(
        self,
        session: Any,
        brand: str,
        query: str,
        normalized_rule: str,
        operator: str,
    ) -> dict[str, Any]:
        row = self._get_row(session, brand, query, lock=True)
        updated, appended = _append_rule_text(row.rules_text if row else "", normalized_rule)
        if row is None:
            row = QueryGovernanceRelevanceGuidance(
                brand=brand, query_text=query, rules_text=updated,
                status="active", updated_by=str(operator or "unknown"),
            )
            session.add(row)
        elif appended:
            row.rules_text = updated
            row.status = "active"
            row.updated_by = str(operator or "unknown")
        session.flush()
        return {"_row": row, "appended": appended}

    def deactivate(self, brand: str, query: str, *, operator: str) -> dict[str, Any]:
        brand, query = _identity(brand, query)
        with self.session_factory() as session:
            row = self._get_row(session, brand, query)
            if row is None:
                raise GuidanceNotFoundError("relevance guidance not found")
            row.status = "inactive"
            row.updated_by = str(operator or "unknown")
            session.commit()
            session.refresh(row)
            return _guidance_payload(session, row)

    def record_cancel_reason(
        self,
        brand: str,
        query: str,
        reason: str,
        *,
        product_id: str,
        operator: str,
        session: Any | None = None,
    ) -> dict[str, Any]:
        """Persist a cancellation reason for auditing only.

        A cancellation reason never becomes a relevance rule: only text the
        operator writes (rule editor, append API or the explicit
        ``correctionRule`` field) may change ``rules_text``.
        """
        brand, query = _identity(brand, query)
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            return {"reasonRecorded": False}
        normalized_product = str(product_id or "").strip()
        if not normalized_product:
            raise ValueError("product_id is required")
        operator = str(operator or "unknown")
        if session is not None:
            return self._record_cancel_reason_with_session(
                session,
                brand,
                query,
                normalized_reason,
                normalized_product,
                operator,
            )
        with self.session_factory() as session:
            result = self._record_cancel_reason_with_session(
                session,
                brand,
                query,
                normalized_reason,
                normalized_product,
                operator,
            )
            session.commit()
            return result

    def preview_organize(self, brand: str, query: str, *, operator: str) -> dict[str, Any]:
        runtime = self.organizer_runtime
        if runtime is None and self.organizer_runtime_factory is not None:
            runtime = self.organizer_runtime_factory()
        if runtime is None:
            raise ValueError("相关性规则整理模型未配置")
        current = self.get(brand, query)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是相关性纠正规则整理 Agent。只整理已有规则，去重、合并并压缩表达，"
                    "不得添加输入中没有的业务约束，不执行任何外部写入。只输出 JSON：{\"rules\":\"...\"}。"
                ),
            },
            {
                "role": "user",
                "content": f"Query：{current['query']}\n已有相关性纠正规则：\n{current['rules']}",
            },
        ]
        response: Any = None
        try:
            invoke_kwargs: dict[str, Any] = {
                "run_name": "query-governance.relevance-guidance",
                "metadata": {"brand": current["brand"], "query": current["query"]},
            }
            if self.observability is not None:
                query_id = f"guidance:{current['brand']}:{current['query']}"
                trace = self.observability.start_batch(
                    query_id,
                    [QuerySpec(query_id, current["brand"], current["query"])],
                    metadata={"operation": "relevance-guidance-organize"},
                )
                invoke_kwargs = trace.query_config(query_id, "relevance-guidance")
            invocation = runtime.invoke(messages, **invoke_kwargs)
            response = getattr(invocation, "response", invocation)
            content = getattr(response, "content", response)
            text = str(content).strip()
            if text.startswith("```"):
                text = text.strip("`").strip()
                if text.lower().startswith("json"):
                    text = text[4:].strip()
            payload = json.loads(text)
            rules = str(payload.get("rules") or "").strip() if isinstance(payload, Mapping) else ""
        except Exception as exc:
            if _response_finish_reason(response) == "length":
                raise ValueError("相关性规则整理结果被截断，请减少规则条数后重试") from exc
            raise ValueError(f"相关性规则整理结果无效: {exc}") from exc
        if not rules:
            raise ValueError("相关性规则整理结果为空")
        return {"brand": current["brand"], "query": current["query"], "rules": rules, "status": "preview"}

    def confirm_organize(self, brand: str, query: str, rules: str, *, operator: str) -> dict[str, Any]:
        return self.save(brand, query, rules, operator=operator)

    def _record_cancel_reason_with_session(
        self,
        session: Any,
        brand: str,
        query: str,
        reason: str,
        product_id: str,
        operator: str,
    ) -> dict[str, Any]:
        session.add(
            QueryGovernanceRelevanceGuidanceReason(
                brand=brand,
                query_text=query,
                product_id=product_id,
                reason_text=reason,
                operator=operator,
            )
        )
        session.flush()
        return {"reasonRecorded": True}

    @staticmethod
    def _get_row(session: Any, brand: str, query: str, *, lock: bool = False) -> QueryGovernanceRelevanceGuidance | None:
        statement = select(QueryGovernanceRelevanceGuidance).where(
            QueryGovernanceRelevanceGuidance.brand == brand,
            QueryGovernanceRelevanceGuidance.query_text == query,
        )
        return session.execute(statement.with_for_update() if lock else statement).scalar_one_or_none()


__all__ = ["GuidanceNotFoundError", "RelevanceGuidanceService"]
