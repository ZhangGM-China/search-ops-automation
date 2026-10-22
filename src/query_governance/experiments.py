"""A/B Test orchestration and daily metric persistence for Query governance."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any, Protocol

from sqlalchemy import delete, func, select

from src.governance.query_identity import normalize_query_key, normalize_query_text

from .models import (
    QueryGovernanceExperiment,
    QueryGovernanceExperimentDailyMetric,
    QueryGovernanceTask,
    QueryRunResult,
    QuerySpec,
)


class ExperimentMetricsProvider(Protocol):
    def __call__(
        self,
        spec: QuerySpec,
        payload: Mapping[str, Any],
        *,
        data_date: date | None = None,
    ) -> Mapping[str, Any]:
        ...


class QueryGovernanceExperimentError(ValueError):
    """Raised when a rewrite-owned experiment cannot change state."""


class SqlAlchemyExperimentMetricsProvider:
    """Provide persisted sample metrics when a run has no external metrics."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def __call__(
        self,
        spec: QuerySpec,
        payload: Mapping[str, Any],
        *,
        data_date: date | None = None,
    ) -> Mapping[str, Any]:
        del payload, data_date
        if spec.experiment_id is None:
            return {}
        from .models import QueryGovernanceExperimentSample

        with self._session_factory() as session:
            samples = session.execute(
                select(QueryGovernanceExperimentSample).where(
                    QueryGovernanceExperimentSample.experiment_id
                    == int(spec.experiment_id),
                )
            ).scalars().all()
            query_key = normalize_query_key(spec.query)
            sample = next(
                (
                    item
                    for item in samples
                    if normalize_query_key(item.query_text) == query_key
                ),
                None,
            )
        if sample is None:
            return {}
        return {
            "search_pv": sample.sample_search_pv or 0,
            "ctr": sample.sample_ctr or 0,
            "cvr": sample.sample_cvr or 0,
        }


class QueryExtractorExperimentMetricsProvider:
    """Collect the experiment day's metrics from the read-only data source."""

    def __init__(self, extractor_factory: Callable[[str], Any] | None = None) -> None:
        self._extractor_factory = extractor_factory or self._default_extractor
        self._extractors: dict[str, Any] = {}

    @staticmethod
    def _default_extractor(brand: str) -> Any:
        from src.integrations.query_extractor import QueryExtractor

        brand_type = {
            "riviera": "rl",
            "summit": "rds",
        }.get(brand, "rds")
        return QueryExtractor(brand_type=brand_type)

    def __call__(
        self,
        spec: QuerySpec,
        payload: Mapping[str, Any],
        *,
        data_date: date | None = None,
    ) -> Mapping[str, Any]:
        del payload
        if data_date is None:
            raise ValueError("data_date is required for external experiment metrics")
        extractor = self._extractors.get(spec.brand)
        if extractor is None:
            extractor = self._extractor_factory(spec.brand)
            self._extractors[spec.brand] = extractor
        result = extractor.get_query_daily_metrics_strict(
            spec.brand,
            data_date,
            [spec.query],
        )
        if not isinstance(result, Mapping):
            raise ValueError("experiment metrics source returned an invalid payload")
        if result.get("status") != "complete":
            missing = ",".join(str(item) for item in result.get("missingKeys") or [])
            raise ValueError(
                "experiment metrics source is incomplete"
                + (f": {missing}" if missing else "")
            )
        items = result.get("items")
        if not isinstance(items, Sequence) or not items:
            raise ValueError("experiment metrics source returned no item")
        item = items[0]
        if not isinstance(item, Mapping):
            raise ValueError("experiment metrics source returned an invalid item")
        return {
            "search_pv": int(float(item.get("search_pv") or 0)),
            "ctr": float(item.get("ctr") or 0),
            "cvr": float(item.get("cvr") or 0),
        }


def _number(value: Any, default: int | float = 0) -> int | float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in {float("inf"), float("-inf")}:
        return default
    return int(result) if result.is_integer() else result


class SqlAlchemyExperimentMetricsObserver:
    """Write one idempotent daily row for each treatment/control sample."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        metric_model: type[Any] = QueryGovernanceExperimentDailyMetric,
        task_model: type[Any] = QueryGovernanceTask,
        metrics_provider: ExperimentMetricsProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._metric_model = metric_model
        self._task_model = task_model
        self._metrics_provider = metrics_provider or SqlAlchemyExperimentMetricsProvider(
            session_factory
        )

    def record(
        self,
        spec: QuerySpec,
        run: QueryRunResult,
        *,
        data_date: date | None,
        batch_id: str | None,
    ) -> None:
        if data_date is None:
            return
        payload = {
            "status": run.final_status,
            "nodes": {
                node: {
                    "status": result.status.value,
                    "action": result.action,
                    "error_code": result.error_code,
                }
                for node, result in run.nodes.items()
            },
        }
        self._write(spec, payload, data_date=data_date, batch_id=batch_id)

    def record_observation(
        self,
        spec: QuerySpec,
        products: Sequence[Mapping[str, Any]] | None,
        *,
        data_date: date | None,
        batch_id: str | None,
        error: str | None = None,
    ) -> None:
        if data_date is None:
            return
        payload = {
            "status": "observed" if error is None else "error",
            "result_count": len(products or []),
            "error": error,
        }
        self._write(spec, payload, data_date=data_date, batch_id=batch_id)

    def _write(
        self,
        spec: QuerySpec,
        payload: Mapping[str, Any],
        *,
        data_date: date,
        batch_id: str | None,
    ) -> None:
        if spec.experiment_id is None or not spec.group_type:
            return
        try:
            experiment_id = int(spec.experiment_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("experiment_id must be an integer") from exc
        group_type = str(spec.group_type).strip().lower()
        if group_type not in {"treatment", "control"}:
            raise ValueError("group_type must be treatment or control")

        metrics = {}
        if self._metrics_provider is not None:
            try:
                metrics = dict(
                    self._metrics_provider(
                        spec,
                        payload,
                        data_date=data_date,
                    )
                )
            except Exception as exc:
                # Business governance must remain successful when an optional
                # analytics provider is unavailable; retain the observation
                # and expose the provider failure in its raw payload.
                payload = {
                    **payload,
                    "metrics_error": f"{exc.__class__.__name__}: {exc}",
                }
        with self._session_factory() as session:
            task_id = None
            if group_type == "treatment" and batch_id:
                task_id = session.execute(
                    select(self._task_model.id).where(
                        self._task_model.batch_id == batch_id,
                        self._task_model.brand == spec.brand,
                        self._task_model.query_text == spec.query,
                    )
                ).scalar_one_or_none()
            row = session.execute(
                select(self._metric_model).where(
                    self._metric_model.experiment_id == experiment_id,
                    self._metric_model.query_text == spec.query,
                    self._metric_model.data_date == data_date,
                )
            ).scalar_one_or_none()
            if row is None:
                row = self._metric_model(
                    experiment_id=experiment_id,
                    brand=spec.brand,
                    query_text=spec.query,
                    group_type=group_type,
                    data_date=data_date,
                )
                session.add(row)
            row.group_type = group_type
            row.governance_task_id = task_id
            row.governance_status = (
                "observed"
                if group_type == "control"
                else str(payload.get("status") or "completed")
            )
            row.search_pv = int(_number(metrics.get("search_pv"), 0))
            row.ctr = float(_number(metrics.get("ctr"), 0))
            row.cvr = float(_number(metrics.get("cvr"), 0))
            row.action_count = 0 if group_type == "control" else self._action_count(payload)
            row.token_usage = int(_number(metrics.get("token_usage"), 0))
            row.duration_seconds = float(_number(metrics.get("duration_seconds"), 0))
            row.raw_payload = json.dumps(
                {"observation": dict(payload), "metrics": metrics},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.commit()

    @staticmethod
    def _action_count(payload: Mapping[str, Any]) -> int:
        nodes = payload.get("nodes")
        if not isinstance(nodes, Mapping):
            return 0
        return sum(
            1
            for item in nodes.values()
            if isinstance(item, Mapping) and item.get("action") is not None
        )


class QueryGovernanceExperimentCoordinator:
    """Run treatment samples through governance and observe controls separately."""

    def __init__(self, workflow: Any, observer: SqlAlchemyExperimentMetricsObserver):
        self.workflow = workflow
        self.observer = observer
        workflow.experiment_observer = observer

    def run(
        self,
        treatment_specs: Sequence[QuerySpec],
        control_specs: Sequence[QuerySpec],
        *,
        batch_id: str,
        data_date: date,
    ) -> dict[str, QueryRunResult]:
        treatment = [
            spec
            for spec in treatment_specs
            if spec.group_type in {None, "treatment"}
        ]
        controls = [
            spec
            for spec in control_specs
            if spec.group_type == "control"
        ]
        specs = tuple(treatment) + tuple(controls)
        if not specs:
            return {}
        return self.workflow.run_batch(
            specs,
            batch_id=batch_id,
            data_date=data_date,
        )

    def run_active(
        self,
        service: "QueryGovernanceExperimentService",
        *,
        brand: str,
        experiment_id: int,
        batch_id: str,
        data_date: date,
    ) -> dict[str, QueryRunResult]:
        experiment = service.get_active_experiment_for_brand(
            brand, experiment_id=experiment_id, execution_date=data_date
        )
        if experiment is None:
            raise QueryGovernanceExperimentError("实验不存在或不在运行周期内")
        treatment: list[QuerySpec] = []
        control: list[QuerySpec] = []
        for sample in experiment["samples"]:
            spec = QuerySpec(
                query_id=f"{batch_id}:{brand}:{normalize_query_key(sample['query'])}",
                brand=brand,
                query=sample["query"],
                batch_id=batch_id,
                experiment_id=experiment_id,
                group_type=sample["groupType"],
            )
            (control if sample["groupType"] == "control" else treatment).append(spec)
        return self.run(
            treatment,
            control,
            batch_id=batch_id,
            data_date=data_date,
        )


class QueryGovernanceExperimentService:
    """CRUD and read projections for rewrite-owned A/B Test tables."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _queries(values: Sequence[Any]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values or []:
            text = normalize_query_text(value)
            key = normalize_query_key(text)
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

    def create_or_replace_draft(
        self,
        *,
        brand: str,
        created_by: str,
        experiment_name: str,
        treatment_queries: Sequence[Any],
        control_queries: Sequence[Any],
        today: date | None = None,
        planned_end_date: date | None = None,
    ) -> dict[str, Any]:
        from .models import QueryGovernanceExperiment, QueryGovernanceExperimentSample

        treatment = self._queries(treatment_queries)
        control = self._queries(control_queries)
        overlap = set(map(normalize_query_key, treatment)) & set(
            map(normalize_query_key, control)
        )
        if not treatment or not control:
            raise QueryGovernanceExperimentError("实验组和对照组都必须至少包含一个 Query")
        if overlap:
            raise QueryGovernanceExperimentError("实验组和对照组不能包含相同 Query")
        with self._session_factory() as session:
            experiment = session.execute(
                select(QueryGovernanceExperiment)
                .where(
                    QueryGovernanceExperiment.brand == brand,
                    QueryGovernanceExperiment.status == "draft",
                )
                .order_by(QueryGovernanceExperiment.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if experiment is None:
                experiment = QueryGovernanceExperiment(
                    brand=brand,
                    experiment_name=experiment_name.strip() or f"{brand} A/B Test",
                    status="draft",
                    sample_data_date=today,
                    planned_start_date=today,
                    planned_end_date=planned_end_date,
                    created_by=created_by,
                )
                session.add(experiment)
                session.flush()
            else:
                session.execute(
                    delete(QueryGovernanceExperimentSample).where(
                        QueryGovernanceExperimentSample.experiment_id == experiment.id
                    )
                )
                experiment.experiment_name = experiment_name.strip() or experiment.experiment_name
                experiment.sample_data_date = today
                experiment.planned_start_date = today
                experiment.planned_end_date = planned_end_date
                experiment.created_by = created_by
            for rank, (query, group_type) in enumerate(
                [(item, "treatment") for item in treatment]
                + [(item, "control") for item in control],
                start=1,
            ):
                session.add(
                    QueryGovernanceExperimentSample(
                        experiment_id=experiment.id,
                        brand=brand,
                        query_text=query,
                        group_type=group_type,
                        sample_rank=rank,
                    )
                )
            session.commit()
            return self._experiment_payload(session, experiment.id, include_metrics=False)

    def schedule_experiment(self, experiment_id: int) -> dict[str, Any]:
        return self._transition(experiment_id, {"draft", "scheduled"}, "scheduled")

    def activate_scheduled_experiment(self, experiment_id: int) -> dict[str, Any]:
        return self._transition(experiment_id, {"scheduled"}, "running")

    def end_experiment(self, experiment_id: int) -> dict[str, Any]:
        return self._transition(experiment_id, {"running", "scheduled"}, "ended")

    def cancel_experiment(self, experiment_id: int) -> dict[str, Any]:
        return self._transition(experiment_id, {"draft", "scheduled", "running"}, "cancelled")

    def _transition(
        self, experiment_id: int, allowed: set[str], target: str
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            experiment = session.get(QueryGovernanceExperiment, experiment_id)
            if experiment is None:
                raise QueryGovernanceExperimentError("实验不存在")
            if experiment.status not in allowed:
                raise QueryGovernanceExperimentError(
                    f"实验状态 {experiment.status} 不允许变更为 {target}"
                )
            experiment.status = target
            session.commit()
            return self._experiment_payload(session, experiment.id, include_metrics=False)

    def get_active_experiment_for_brand(
        self,
        brand: str,
        *,
        experiment_id: int | None = None,
        execution_date: date | None = None,
    ) -> dict[str, Any] | None:
        with self._session_factory() as session:
            query = select(QueryGovernanceExperiment).where(
                QueryGovernanceExperiment.brand == brand,
                QueryGovernanceExperiment.status.in_(["scheduled", "running"]),
            )
            if experiment_id is not None:
                query = query.where(QueryGovernanceExperiment.id == experiment_id)
            if execution_date is not None:
                query = query.where(
                    (QueryGovernanceExperiment.planned_start_date.is_(None))
                    | (QueryGovernanceExperiment.planned_start_date <= execution_date),
                    (QueryGovernanceExperiment.planned_end_date.is_(None))
                    | (QueryGovernanceExperiment.planned_end_date >= execution_date),
                )
            experiment = session.execute(
                query.order_by(QueryGovernanceExperiment.id.desc()).limit(1)
            ).scalar_one_or_none()
            return (
                self._experiment_payload(session, experiment.id, include_metrics=False)
                if experiment is not None
                else None
            )

    def list_history_summaries(
        self,
        brand: str,
        *,
        status: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            query = select(QueryGovernanceExperiment).where(QueryGovernanceExperiment.brand == brand)
            if status:
                query = query.where(QueryGovernanceExperiment.status == status)
            if start_date:
                query = query.where(QueryGovernanceExperiment.created_at >= start_date)
            if end_date:
                query = query.where(QueryGovernanceExperiment.created_at < end_date)
            total = session.execute(
                select(func.count()).select_from(query.subquery())
            ).scalar_one()
            rows = session.execute(
                query.order_by(QueryGovernanceExperiment.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).scalars().all()
            items = [self._experiment_summary(session, row) for row in rows]
            current = next((item for item in items if item["status"] in {"scheduled", "running"}), None)
            return {
                "brand": brand,
                "current": current,
                "items": items,
                "total": int(total),
                "page": page,
                "pageSize": page_size,
            }

    def get_sample_pool(self, brand: str, *, today: date | None = None, limit: int = 150) -> dict[str, Any]:
        from .models import QueryGovernanceCandidate, QueryGovernanceSelectionBatch

        with self._session_factory() as session:
            query = (
                select(QueryGovernanceCandidate)
                .join(
                    QueryGovernanceSelectionBatch,
                    QueryGovernanceCandidate.selection_batch_id == QueryGovernanceSelectionBatch.id,
                )
                .where(QueryGovernanceCandidate.brand == brand)
            )
            if today is not None:
                query = query.where(QueryGovernanceCandidate.data_date == today)
            rows = session.execute(
                query.order_by(QueryGovernanceCandidate.selection_rank.asc()).limit(limit)
            ).scalars().all()
            sample_data_date = rows[0].data_date if rows else today

            def metric_value(row: Any, key: str) -> float:
                if not row.metrics_json:
                    return 0.0
                try:
                    payload = json.loads(row.metrics_json)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return 0.0
                value = payload.get(key) if isinstance(payload, Mapping) else 0
                return float(value or 0)

            return {
                "brand": brand,
                "sampleDataDate": (
                    sample_data_date.isoformat() if sample_data_date else None
                ),
                "items": [
                    {
                        "query": row.query_text,
                        "queryText": row.query_text,
                        "queryKey": row.query_key,
                        "searchPv": row.target_day_pv,
                        "ctr": metric_value(row, "ctr"),
                        "cvr": metric_value(row, "cvr"),
                        "rank": row.selection_rank,
                    }
                    for row in rows
                ],
                "total": len(rows),
            }

    def get_experiment_detail(self, experiment_id: int, *, as_of_date: date | None = None) -> dict[str, Any]:
        from .models import QueryGovernanceExperiment

        with self._session_factory() as session:
            experiment = session.get(QueryGovernanceExperiment, experiment_id)
            if experiment is None:
                raise QueryGovernanceExperimentError("实验不存在")
            return self._experiment_payload(session, experiment.id, include_metrics=True, as_of_date=as_of_date)

    def _experiment_summary(self, session: Any, experiment: Any) -> dict[str, Any]:
        samples = self._samples(session, experiment.id)
        return {
            "id": int(experiment.id),
            "experimentId": int(experiment.id),
            "brand": experiment.brand,
            "experimentName": experiment.experiment_name,
            "status": experiment.status,
            "sampleDataDate": experiment.sample_data_date.isoformat() if experiment.sample_data_date else None,
            "plannedStartDate": experiment.planned_start_date.isoformat() if experiment.planned_start_date else None,
            "plannedEndDate": experiment.planned_end_date.isoformat() if experiment.planned_end_date else None,
            "treatmentCount": sum(item["groupType"] == "treatment" for item in samples),
            "controlCount": sum(item["groupType"] == "control" for item in samples),
        }

    def _experiment_payload(
        self,
        session: Any,
        experiment_id: int,
        *,
        include_metrics: bool,
        as_of_date: date | None = None,
    ) -> dict[str, Any]:
        from .models import QueryGovernanceExperimentDailyMetric

        experiment = session.get(QueryGovernanceExperiment, experiment_id)
        payload = self._experiment_summary(session, experiment)
        payload["samples"] = self._samples(session, experiment.id)
        if include_metrics:
            query = select(QueryGovernanceExperimentDailyMetric).where(
                QueryGovernanceExperimentDailyMetric.experiment_id == experiment_id
            )
            if as_of_date is not None:
                query = query.where(QueryGovernanceExperimentDailyMetric.data_date <= as_of_date)
            metrics = session.execute(
                query.order_by(QueryGovernanceExperimentDailyMetric.data_date.asc(), QueryGovernanceExperimentDailyMetric.id.asc())
            ).scalars().all()
            payload["dailyMetrics"] = [
                {
                    "id": int(row.id),
                    "queryText": row.query_text,
                    "groupType": row.group_type,
                    "dataDate": row.data_date.isoformat(),
                    "searchPv": row.search_pv,
                    "ctr": row.ctr,
                    "cvr": row.cvr,
                    "governanceStatus": row.governance_status,
                    "actionCount": row.action_count,
                    "tokenUsage": row.token_usage,
                    "durationSeconds": row.duration_seconds,
                }
                for row in metrics
            ]
            payload["items"] = list(payload["dailyMetrics"])
            payload["groupSummary"] = self._group_summary(metrics)
        else:
            payload["dailyMetrics"] = []
        return payload

    @staticmethod
    def _samples(session: Any, experiment_id: int) -> list[dict[str, Any]]:
        from .models import QueryGovernanceExperimentSample

        rows = session.execute(
            select(QueryGovernanceExperimentSample)
            .where(QueryGovernanceExperimentSample.experiment_id == experiment_id)
            .order_by(QueryGovernanceExperimentSample.sample_rank.asc(), QueryGovernanceExperimentSample.id.asc())
        ).scalars().all()
        return [
            {
                "id": int(row.id),
                "query": row.query_text,
                "queryText": row.query_text,
                "groupType": row.group_type,
                "sampleRank": row.sample_rank,
                "searchPv": row.sample_search_pv,
                "ctr": row.sample_ctr,
                "cvr": row.sample_cvr,
            }
            for row in rows
        ]

    @staticmethod
    def _group_summary(rows: Sequence[Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for group in ("treatment", "control"):
            selected = [row for row in rows if row.group_type == group]
            summary[group] = {
                "queryCount": len(selected),
                "searchPv": sum(int(row.search_pv or 0) for row in selected),
                "actionCount": sum(int(row.action_count or 0) for row in selected),
                "avgCtr": round(sum(float(row.ctr or 0) for row in selected) / len(selected), 6) if selected else 0,
                "avgCvr": round(sum(float(row.cvr or 0) for row in selected) / len(selected), 6) if selected else 0,
            }
        return summary


class QueryGovernanceExperimentScheduler:
    """Scheduler-facing adapter; the deterministic workflow is injected."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        workflow_factory: Callable[[str], Any],
        *,
        observer_factory: Callable[[Callable[[], Any]], SqlAlchemyExperimentMetricsObserver] | None = None,
        metrics_provider_factory: Callable[[str], ExperimentMetricsProvider] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._workflow_factory = workflow_factory
        self._observer_factory = observer_factory or (
            lambda factory: SqlAlchemyExperimentMetricsObserver(factory)
        )
        self._metrics_provider_factory = metrics_provider_factory or (
            lambda _brand: QueryExtractorExperimentMetricsProvider()
        )
        self._custom_observer_factory = observer_factory is not None

    def run_brand(
        self,
        brand: str,
        *,
        data_date: date,
        batch_id: str,
    ) -> dict[str, Any]:
        service = QueryGovernanceExperimentService(self._session_factory)
        active = service.get_active_experiment_for_brand(
            brand, execution_date=data_date
        )
        if active is None:
            return {"brand": brand, "status": "skipped", "reason": "no_active_experiment"}
        if active["status"] == "scheduled":
            try:
                active = service.activate_scheduled_experiment(int(active["id"]))
            except QueryGovernanceExperimentError:
                # Another scheduler worker may have claimed the same experiment.
                active = service.get_active_experiment_for_brand(
                    brand,
                    experiment_id=int(active["id"]),
                    execution_date=data_date,
                )
                if active is None or active["status"] != "running":
                    raise
        workflow = self._workflow_factory(brand)
        should_continue = getattr(workflow, "should_continue", None)
        if callable(should_continue) and not should_continue():
            return {"brand": brand, "status": "skipped", "reason": "scheduler_stopping",
                    "batchId": batch_id, "dataDate": data_date.isoformat()}
        if self._custom_observer_factory:
            observer = self._observer_factory(self._session_factory)
        else:
            observer = SqlAlchemyExperimentMetricsObserver(
                self._session_factory,
                metrics_provider=self._metrics_provider_factory(brand),
            )
        coordinator = QueryGovernanceExperimentCoordinator(workflow, observer)
        results = coordinator.run_active(
            service,
            brand=brand,
            experiment_id=int(active["id"]),
            batch_id=batch_id,
            data_date=data_date,
        )
        treatment = [
            result for result in results.values() if result.spec.group_type == "treatment"
        ]
        control = [
            result for result in results.values() if result.spec.group_type == "control"
        ]
        error_count = sum(result.final_status == "error" for result in results.values())
        checkpoint = getattr(workflow, "checkpoint", None)
        persisted_status = checkpoint.get_checkpoint(batch_id).status if checkpoint is not None else None
        return {
            "brand": brand,
            "status": "running" if persisted_status == "running" else "error" if error_count else "completed",
            "experimentId": int(active["id"]),
            "batchId": batch_id,
            "treatmentCount": len(treatment),
            "controlCount": len(control),
            "errorCount": error_count,
        }


__all__ = [
    "ExperimentMetricsProvider",
    "QueryGovernanceExperimentError",
    "QueryGovernanceExperimentService",
    "QueryGovernanceExperimentScheduler",
    "QueryGovernanceExperimentCoordinator",
    "SqlAlchemyExperimentMetricsProvider",
    "QueryExtractorExperimentMetricsProvider",
    "SqlAlchemyExperimentMetricsObserver",
]
