"""Materialize Query metrics locally so detail reads never hit the source DB."""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .models import QueryGovernanceCandidate, QueryGovernanceSelectionBatch, QueryMetricSnapshot


def materialize_candidate_metrics(
    session: Any,
    *,
    brand: str | None = None,
    data_date: date | None = None,
    metric_provider: Any | None = None,
    metric_batch_provider: Any | None = None,
    as_of_date: date | None = None,
) -> int:
    """Idempotently upsert candidate metric inputs into the local snapshot table."""
    query = select(QueryGovernanceCandidate)
    if brand is not None:
        query = query.where(QueryGovernanceCandidate.brand == brand)
    if data_date is not None:
        query = query.where(QueryGovernanceCandidate.data_date == data_date)
    query = query.where(QueryGovernanceCandidate.selected.is_(True))
    candidates = session.execute(query.order_by(QueryGovernanceCandidate.id.asc())).scalars().all()
    batch_metrics: dict[tuple[str, str, date], Mapping[str, Any]] = {}
    batch_configs: dict[int, dict[str, Any]] = {}

    def get_batch_config(selection_batch_id: int) -> dict[str, Any]:
        if selection_batch_id in batch_configs:
            return batch_configs[selection_batch_id]
        config_row = session.execute(
            select(QueryGovernanceSelectionBatch.config_json).where(
                QueryGovernanceSelectionBatch.id == selection_batch_id
            )
        ).scalar_one_or_none()
        try:
            cfg = json.loads(config_row or "{}")
        except (TypeError, ValueError):
            cfg = {}
        batch_configs[selection_batch_id] = cfg if isinstance(cfg, Mapping) else {}
        return batch_configs[selection_batch_id]

    if metric_batch_provider is not None:
        batch_queries: dict[tuple[str, date], set[str]] = {}
        for candidate in candidates:
            config = get_batch_config(candidate.selection_batch_id)
            history_days = max(1, int(config.get("history_days", 7))) if isinstance(config, Mapping) else 7
            dates = [candidate.data_date + timedelta(days=offset) for offset in range(-history_days, history_days + 1)]
            if as_of_date is not None:
                dates = [metric_date for metric_date in dates if metric_date <= as_of_date]
            for metric_date in dates:
                batch_queries.setdefault((candidate.brand, metric_date), set()).add(candidate.query_text)
        for (candidate_brand, metric_date), queries in sorted(batch_queries.items(), key=lambda item: (item[0][0], item[0][1])):
            response = metric_batch_provider(candidate_brand, sorted(queries), metric_date) or {}
            for query_text in queries:
                metric = response.get(query_text) if isinstance(response, Mapping) else None
                batch_metrics[(candidate_brand, query_text, metric_date)] = metric if isinstance(metric, Mapping) else {"status": "missing"}
    written = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for candidate in candidates:
        raw = json.loads(candidate.metrics_json or "{}")
        if not isinstance(raw, Mapping):
            continue
        config = get_batch_config(candidate.selection_batch_id)
        history_days = max(1, int(config.get("history_days", 7))) if isinstance(config, Mapping) else 7
        dates = (
            [candidate.data_date]
            if metric_provider is None and metric_batch_provider is None
            else [candidate.data_date + timedelta(days=offset) for offset in range(-history_days, history_days + 1)]
        )
        if as_of_date is not None:
            dates = [metric_date for metric_date in dates if metric_date <= as_of_date]
        for metric_date in dates:
            if metric_provider is None and metric_batch_provider is None:
                metric = raw
            elif metric_batch_provider is not None:
                metric = batch_metrics.get((candidate.brand, candidate.query_text, metric_date), {"status": "missing"})
            else:
                metric = metric_provider(candidate.brand, candidate.query_text, metric_date) or {}
            if metric.get("status") in {"missing", "failed"}:
                continue
            uv = float(metric.get("search_uv", metric.get("uv", 0)) or 0)
            pv = float(metric.get("search_pv", metric.get("pv", candidate.target_day_pv if metric_date == candidate.data_date else 0)) or 0)
            click_uv = float(metric.get("click_uv", 0) or 0)
            add_to_cart_uv = float(metric.get("add_to_cart_uv", metric.get("atc_uv", 0)) or 0)
            purchase_uv = float(metric.get("purchase_uv", 0) or 0)
            row = session.execute(select(QueryMetricSnapshot).where(
            QueryMetricSnapshot.brand == candidate.brand,
            QueryMetricSnapshot.query_text == candidate.query_text,
            QueryMetricSnapshot.data_date == metric_date,
            )).scalar_one_or_none()
            values = dict(pv=pv, uv=uv, click_uv=click_uv, add_to_cart_uv=add_to_cart_uv, purchase_uv=purchase_uv,
                      ctr=float(metric.get("ctr") or (click_uv / uv if uv else 0)),
                      cvr=float(metric.get("cvr") or (purchase_uv / uv if uv else 0)),
                      query_rank=candidate.selection_rank, updated_at=now)
            if row is None:
                session.add(QueryMetricSnapshot(brand=candidate.brand, query_text=candidate.query_text,
                                                 data_date=metric_date, created_at=now, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            written += 1
    session.commit()
    return written
