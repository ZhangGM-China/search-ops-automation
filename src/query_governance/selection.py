"""Deterministic Query extraction and daily governance selection."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Protocol, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.governance.query_identity import (
    is_non_intent_query,
    normalize_query_key,
    normalize_query_text,
)

from .models import (
    QueryGovernanceCandidate,
    QueryGovernanceSelectionBatch,
    QueryGovernanceTask,
    QuerySpec,
)
from .visibility import CHECK_KEYS, effective_check_modes


RULE_ORDER = (
    "high_pv",
    "low_ctr",
    "low_cvr",
    "ctr_decline",
    "cvr_decline",
    "new_query",
    "rapid_pv_growth",
)

_PLACEHOLDER_QUERIES = frozenset(
    {"null", "none", "undefined", "nan", "unknown", "未知", "无", "空", "(空)"}
)
_VISIBLE_QUERY_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_PURE_NUMBER_RE = re.compile(r"^[0-9]+$")
_PRODUCT_CODE_RE = re.compile(
    r"^(?:[A-Za-z]{1,5}[-_ ]?)?[0-9]{5,}[A-Za-z0-9_-]*$"
)
_LONG_NUMBER_SUFFIX_RE = re.compile(
    r"^[0-9]{4,}\s*(?:男|女|童|儿童|男士|女士|鞋|服|裤|裙|上衣|外套)$"
)
_ATLAS_TRAILING_GENDER_RE = re.compile(
    r"\s*(?:女士|男士|儿童|家居|男装|女装|童装|男|女|童)$"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class QuerySelectionConfig:
    """Immutable configuration loaded once when a governance batch starts."""

    extract_limit: int = 150
    query_min_pv: float = 5
    query_min_metric_type: str = "uv"
    query_min_metric_value: float | None = None
    excluded_keywords: tuple[str, ...] = ()
    high_pv_limit: int = 100
    alert_ctr_gap_pp: float = 5
    alert_cvr_gap_pp: float = 2
    history_days: int = 7
    new_query_pv_threshold: float = 5
    pv_growth_ratio_floor: float = 1.2
    pv_growth_sqrt_multiplier: float = 2
    pv_growth_abs_floor: float = 5
    reorder_enabled: bool = True
    check_modes: Mapping[str, bool] = field(default_factory=lambda: {key: True for key in CHECK_KEYS})

    def __post_init__(self) -> None:
        if self.query_min_metric_value is None:
            object.__setattr__(self, "query_min_metric_value", float(self.query_min_pv))
        if self.query_min_metric_type not in {"uv", "pv"}:
            raise ValueError("query_governance_query_min_metric_type must be uv or pv")
        if not isinstance(self.reorder_enabled, bool):
            raise ValueError("query_governance_reorder_enabled must be boolean")
        if self.extract_limit <= 0:
            raise ValueError("query_governance_extract_limit must be positive")
        if self.query_min_pv < 0 or float(self.query_min_metric_value) < 0:
            raise ValueError("query governance minimum metric cannot be negative")
        if self.high_pv_limit < 0:
            raise ValueError(
                "query_governance_candidate_high_pv_limit cannot be negative"
            )
        if self.alert_ctr_gap_pp < 0 or self.alert_cvr_gap_pp < 0:
            raise ValueError("candidate alert gaps cannot be negative")
        if self.history_days <= 0:
            raise ValueError(
                "query_governance_candidate_history_days must be positive"
            )
        if self.new_query_pv_threshold < 0:
            raise ValueError("new Query PV threshold cannot be negative")
        if self.pv_growth_ratio_floor < 0:
            raise ValueError("PV growth ratio floor cannot be negative")
        if self.pv_growth_sqrt_multiplier < 0:
            raise ValueError("PV growth sqrt multiplier cannot be negative")
        if self.pv_growth_abs_floor < 0:
            raise ValueError("PV growth absolute floor cannot be negative")
        if isinstance(self.excluded_keywords, (str, bytes)) or not isinstance(
            self.excluded_keywords, Sequence
        ):
            raise ValueError(
                "query_governance_excluded_keywords must be a sequence of strings"
            )
        normalized_keywords: list[str] = []
        seen_keywords: set[str] = set()
        for keyword in self.excluded_keywords:
            if not isinstance(keyword, str):
                raise ValueError(
                    "query_governance_excluded_keywords must contain strings"
                )
            cleaned = normalize_query_text(keyword)
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key not in seen_keywords:
                seen_keywords.add(key)
                normalized_keywords.append(cleaned)
        object.__setattr__(self, "excluded_keywords", tuple(normalized_keywords))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "QuerySelectionConfig":
        legacy_minimum = values.get("query_governance_query_min_pv", cls.query_min_pv)
        metric_type = values.get(
            "query_governance_query_min_metric_type",
            "pv" if "query_governance_query_min_pv" in values else cls.query_min_metric_type,
        )
        raw_excluded = values.get(
            "query_governance_excluded_keywords", cls.excluded_keywords
        )
        return cls(
            extract_limit=int(
                values.get("query_governance_extract_limit", cls.extract_limit)
            ),
            query_min_pv=float(legacy_minimum),
            query_min_metric_type=str(metric_type),
            query_min_metric_value=float(values.get("query_governance_query_min_metric_value", legacy_minimum)),
            excluded_keywords=() if raw_excluded is None else raw_excluded,
            high_pv_limit=int(
                values.get(
                    "query_governance_candidate_high_pv_limit",
                    cls.high_pv_limit,
                )
            ),
            alert_ctr_gap_pp=float(
                values.get(
                    "query_governance_candidate_alert_ctr_gap_pp",
                    cls.alert_ctr_gap_pp,
                )
            ),
            alert_cvr_gap_pp=float(
                values.get(
                    "query_governance_candidate_alert_cvr_gap_pp",
                    cls.alert_cvr_gap_pp,
                )
            ),
            history_days=int(
                values.get(
                    "query_governance_candidate_history_days",
                    cls.history_days,
                )
            ),
            new_query_pv_threshold=float(
                values.get(
                    "query_governance_candidate_new_query_pv_threshold",
                    cls.new_query_pv_threshold,
                )
            ),
            pv_growth_ratio_floor=float(
                values.get(
                    "query_governance_candidate_pv_growth_ratio_floor",
                    cls.pv_growth_ratio_floor,
                )
            ),
            pv_growth_sqrt_multiplier=float(
                values.get(
                    "query_governance_candidate_pv_growth_sqrt_multiplier",
                    cls.pv_growth_sqrt_multiplier,
                )
            ),
            pv_growth_abs_floor=float(
                values.get(
                    "query_governance_candidate_pv_growth_abs_floor",
                    cls.pv_growth_abs_floor,
                )
            ),
            reorder_enabled=values.get("query_governance_reorder_enabled", True),
            check_modes=effective_check_modes(values),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "extract_limit": self.extract_limit,
            "query_min_pv": self.query_min_pv,
            "query_min_metric_type": self.query_min_metric_type,
            "query_min_metric_value": float(self.query_min_metric_value),
            "excluded_keywords": list(self.excluded_keywords),
            "high_pv_limit": self.high_pv_limit,
            "alert_ctr_gap_pp": self.alert_ctr_gap_pp,
            "alert_cvr_gap_pp": self.alert_cvr_gap_pp,
            "history_days": self.history_days,
            "new_query_pv_threshold": self.new_query_pv_threshold,
            "pv_growth_ratio_floor": self.pv_growth_ratio_floor,
            "pv_growth_sqrt_multiplier": self.pv_growth_sqrt_multiplier,
            "pv_growth_abs_floor": self.pv_growth_abs_floor,
            "reorder_enabled": self.reorder_enabled,
            "check_modes": {
                key: bool(self.check_modes.get(key, True)) for key in CHECK_KEYS
            },
        }


@dataclass(frozen=True)
class BrandQueryMetrics:
    search_uv: float
    click_uv: float
    purchase_uv: float

    @property
    def ctr(self) -> float:
        return self.click_uv / self.search_uv if self.search_uv > 0 else 0.0

    @property
    def cvr(self) -> float:
        return self.purchase_uv / self.search_uv if self.search_uv > 0 else 0.0


@dataclass(frozen=True)
class QueryCandidateMetrics:
    query: str
    search_pv: float
    search_uv: float
    click_uv: float
    purchase_uv: float
    history_search_uv: float
    history_click_uv: float
    history_purchase_uv: float
    history_average_pv: float
    history_max_daily_pv: float

    @property
    def ctr(self) -> float:
        return self.click_uv / self.search_uv if self.search_uv > 0 else 0.0

    @property
    def cvr(self) -> float:
        return self.purchase_uv / self.search_uv if self.search_uv > 0 else 0.0

    @property
    def history_ctr(self) -> float:
        if self.history_search_uv <= 0:
            return 0.0
        return self.history_click_uv / self.history_search_uv

    @property
    def history_cvr(self) -> float:
        if self.history_search_uv <= 0:
            return 0.0
        return self.history_purchase_uv / self.history_search_uv

    def as_dict(self) -> dict[str, float | str]:
        return {
            "query": self.query,
            "search_pv": self.search_pv,
            "search_uv": self.search_uv,
            "click_uv": self.click_uv,
            "purchase_uv": self.purchase_uv,
            "ctr": self.ctr,
            "cvr": self.cvr,
            "history_search_uv": self.history_search_uv,
            "history_click_uv": self.history_click_uv,
            "history_purchase_uv": self.history_purchase_uv,
            "history_ctr": self.history_ctr,
            "history_cvr": self.history_cvr,
            "history_average_pv": self.history_average_pv,
            "history_max_daily_pv": self.history_max_daily_pv,
        }


@dataclass(frozen=True)
class CandidateDataset:
    brand: BrandQueryMetrics
    queries: tuple[QueryCandidateMetrics, ...]


@dataclass(frozen=True)
class FixedExperimentSample:
    experiment_id: str
    query: str
    group_type: str
    sample_rank: int


@dataclass(frozen=True)
class QuerySelectionDecision:
    query_id: str
    query: str
    rules: tuple[str, ...]
    metrics: Mapping[str, Any]
    source_type: str
    rule_count: int
    ctr_decline_pp: float
    target_day_pv: float
    selection_rank: int
    selected: bool
    experiment_id: int | str | None = None
    group_type: str | None = None

    @property
    def spec(self) -> QuerySpec:
        return QuerySpec(
            self.query_id,
            "",
            self.query,
            experiment_id=self.experiment_id,
            group_type=self.group_type,
        )


@dataclass(frozen=True)
class QuerySelectionBatch:
    batch_id: str
    brand: str
    data_date: date
    config: QuerySelectionConfig
    candidates: tuple[QuerySelectionDecision, ...]

    @property
    def selected(self) -> tuple[QuerySelectionDecision, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.selected)

    @property
    def control_samples(self) -> tuple[QuerySelectionDecision, ...]:
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.group_type == "control"
        )

    @property
    def control_specs(self) -> tuple[QuerySpec, ...]:
        return tuple(
            QuerySpec(
                candidate.query_id,
                self.brand,
                candidate.query,
                batch_id=self.batch_id,
                experiment_id=candidate.experiment_id,
                group_type=candidate.group_type,
            )
            for candidate in self.control_samples
        )

    @property
    def specs(self) -> tuple[QuerySpec, ...]:
        return tuple(
            QuerySpec(
                candidate.query_id,
                self.brand,
                candidate.query,
                batch_id=self.batch_id,
                reorder_eligible=(
                    _reorder_eligibility(candidate, self.config)[0]
                    if self.config.reorder_enabled else False
                ),
                reorder_skip_reason=(
                    _reorder_eligibility(candidate, self.config)[1]
                    if self.config.reorder_enabled else "品牌未启用重排序"
                ),
                recall_enabled=self.config.check_modes.get("recall", True),
                relevance_enabled=self.config.check_modes.get("relevance", True),
                experiment_id=candidate.experiment_id,
                group_type=candidate.group_type,
            )
            for candidate in self.selected
        )


class QueryCandidateSource(Protocol):
    def fetch(
        self,
        brand: str,
        data_date: date,
        config: QuerySelectionConfig,
    ) -> CandidateDataset:
        ...


class QuerySelectionStore(Protocol):
    def save_batch(self, batch: QuerySelectionBatch) -> QuerySelectionBatch:
        ...

    def get_batch(self, batch_id: str, brand: str) -> QuerySelectionBatch | None:
        ...

    def get_latest_batch(
        self, brand: str, *, before: datetime | None = None
    ) -> QuerySelectionBatch | None:
        """Return the newest persisted batch available at ``before``."""
        ...


class BatchWorkflow(Protocol):
    def run_batch(
        self,
        specs: Sequence[QuerySpec],
        *,
        batch_id: str | None = None,
        data_date: date | None = None,
    ) -> Any:
        ...


class InMemoryQuerySelectionStore:
    def __init__(self) -> None:
        self._batches: dict[tuple[str, str], QuerySelectionBatch] = {}

    def save_batch(self, batch: QuerySelectionBatch) -> QuerySelectionBatch:
        return self._batches.setdefault((batch.batch_id, batch.brand), batch)

    def get_batch(self, batch_id: str, brand: str) -> QuerySelectionBatch | None:
        return self._batches.get((batch_id, brand))

    def get_latest_batch(
        self, brand: str, *, before: datetime | None = None
    ) -> QuerySelectionBatch | None:
        cutoff = before.date() if before is not None else None
        batches = [
            batch
            for (_batch_id, batch_brand), batch in self._batches.items()
            if batch_brand == brand
            and (cutoff is None or batch.data_date <= cutoff)
        ]
        return max(batches, key=lambda batch: (batch.data_date, batch.batch_id), default=None)


class SqlAlchemyQuerySelectionStore:
    """Persist one frozen selection batch and its selected Query tasks."""

    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory

    def save_batch(self, batch: QuerySelectionBatch) -> QuerySelectionBatch:
        with self.session_factory() as session:
            existing = self._get_batch(session, batch.batch_id, batch.brand)
            if existing is not None:
                return existing

            stored_batch = QueryGovernanceSelectionBatch(
                batch_id=batch.batch_id,
                brand=batch.brand,
                data_date=batch.data_date,
                config_json=json.dumps(
                    batch.config.as_mapping(), ensure_ascii=False, sort_keys=True
                ),
            )
            session.add(stored_batch)
            session.flush()
            for candidate in batch.candidates:
                session.add(
                    QueryGovernanceCandidate(
                        selection_batch_id=stored_batch.id,
                        batch_id=batch.batch_id,
                        brand=batch.brand,
                        data_date=batch.data_date,
                        query_id=candidate.query_id,
                        query_key=normalize_query_key(candidate.query),
                        query_text=candidate.query,
                        experiment_id=(
                            int(candidate.experiment_id)
                            if candidate.experiment_id is not None
                            else None
                        ),
                        group_type=candidate.group_type,
                        matched_rules_json=json.dumps(
                            list(candidate.rules), ensure_ascii=False
                        ),
                        metrics_json=json.dumps(
                            dict(candidate.metrics), ensure_ascii=False, sort_keys=True
                        ),
                        source_type=candidate.source_type,
                        rule_count=candidate.rule_count,
                        ctr_decline_pp=candidate.ctr_decline_pp,
                        target_day_pv=candidate.target_day_pv,
                        selection_rank=candidate.selection_rank,
                        selected=candidate.selected,
                    )
                )
                if candidate.selected:
                    session.add(
                        QueryGovernanceTask(
                            batch_id=batch.batch_id,
                            brand=batch.brand,
                            data_date=batch.data_date,
                            query_text=candidate.query,
                            query_rank=candidate.selection_rank,
                            status="pending",
                            experiment_id=(
                                int(candidate.experiment_id)
                                if candidate.experiment_id is not None
                                else None
                            ),
                            group_type=candidate.group_type,
                        )
                    )
            session.commit()
            return batch

    def get_batch(self, batch_id: str, brand: str) -> QuerySelectionBatch | None:
        with self.session_factory() as session:
            return self._get_batch(session, batch_id, brand)

    def get_latest_batch(
        self, brand: str, *, before: datetime | None = None
    ) -> QuerySelectionBatch | None:
        with self.session_factory() as session:
            query = session.query(QueryGovernanceSelectionBatch).filter(
                QueryGovernanceSelectionBatch.brand == brand
            )
            if before is not None:
                query = query.filter(
                    QueryGovernanceSelectionBatch.data_date <= before.date()
                )
            stored_batch = query.order_by(
                QueryGovernanceSelectionBatch.data_date.desc(),
                QueryGovernanceSelectionBatch.created_at.desc(),
                QueryGovernanceSelectionBatch.id.desc(),
            ).first()
            if stored_batch is None:
                return None
            return self._get_batch(session, stored_batch.batch_id, brand)

    @staticmethod
    def _get_batch(
        session: Session, batch_id: str, brand: str
    ) -> QuerySelectionBatch | None:
        stored_batch = (
            session.query(QueryGovernanceSelectionBatch)
            .filter_by(batch_id=batch_id, brand=brand)
            .one_or_none()
        )
        if stored_batch is None:
            return None
        rows = (
            session.query(QueryGovernanceCandidate)
            .filter_by(selection_batch_id=stored_batch.id)
            .order_by(QueryGovernanceCandidate.selection_rank)
            .all()
        )
        config_values = json.loads(stored_batch.config_json)
        config = QuerySelectionConfig(
            extract_limit=int(config_values["extract_limit"]),
            query_min_pv=float(config_values.get("query_min_pv", config_values.get("query_min_metric_value", 5))),
            query_min_metric_type=str(config_values.get("query_min_metric_type", "pv")),
            query_min_metric_value=float(config_values.get("query_min_metric_value", config_values.get("query_min_pv", 5))),
            excluded_keywords=tuple(config_values.get("excluded_keywords") or ()),
            high_pv_limit=int(config_values["high_pv_limit"]),
            alert_ctr_gap_pp=float(config_values["alert_ctr_gap_pp"]),
            alert_cvr_gap_pp=float(config_values["alert_cvr_gap_pp"]),
            history_days=int(config_values["history_days"]),
            new_query_pv_threshold=float(config_values["new_query_pv_threshold"]),
            pv_growth_ratio_floor=float(config_values["pv_growth_ratio_floor"]),
            pv_growth_sqrt_multiplier=float(
                config_values["pv_growth_sqrt_multiplier"]
            ),
            pv_growth_abs_floor=float(config_values["pv_growth_abs_floor"]),
            reorder_enabled=bool(config_values.get("reorder_enabled", True)),
            check_modes={
                key: bool(config_values.get("check_modes", {}).get(key, True))
                for key in CHECK_KEYS
            },
        )
        candidates = tuple(
            QuerySelectionDecision(
                query_id=row.query_id,
                query=row.query_text,
                rules=tuple(json.loads(row.matched_rules_json)),
                metrics=json.loads(row.metrics_json),
                source_type=row.source_type,
                rule_count=row.rule_count,
                ctr_decline_pp=row.ctr_decline_pp,
                target_day_pv=row.target_day_pv,
                selection_rank=row.selection_rank,
                selected=bool(row.selected),
                experiment_id=row.experiment_id,
                group_type=row.group_type,
            )
            for row in rows
        )
        return QuerySelectionBatch(
            batch_id=stored_batch.batch_id,
            brand=stored_batch.brand,
            data_date=stored_batch.data_date,
            config=config,
            candidates=candidates,
        )


class QuerySelectionService:
    def __init__(
        self,
        *,
        source: QueryCandidateSource,
        store: QuerySelectionStore,
    ) -> None:
        self.source = source
        self.store = store

    def select_batch(
        self,
        *,
        batch_id: str,
        brand: str,
        data_date: date,
        config: QuerySelectionConfig,
        fixed_samples: Sequence[FixedExperimentSample] = (),
    ) -> QuerySelectionBatch:
        existing = self.store.get_batch(batch_id, brand)
        if existing is not None:
            return existing
        if fixed_samples:
            candidates = self._fixed_sample_candidates(batch_id, brand, fixed_samples)
        else:
            dataset = self.source.fetch(brand, data_date, config)
            candidates = self._dynamic_candidates(batch_id, brand, dataset, config)
        return self.store.save_batch(
            QuerySelectionBatch(
                batch_id=batch_id,
                brand=brand,
                data_date=data_date,
                config=config,
                candidates=candidates,
            )
        )

    def select_and_start(
        self,
        *,
        batch_id: str,
        brand: str,
        data_date: date,
        config: QuerySelectionConfig,
        workflow: BatchWorkflow,
        fixed_samples: Sequence[FixedExperimentSample] = (),
    ) -> tuple[QuerySelectionBatch, Any]:
        batch = self.select_batch(
            batch_id=batch_id,
            brand=brand,
            data_date=data_date,
            config=config,
            fixed_samples=fixed_samples,
        )
        if not fixed_samples:
            if not batch.selected:
                return batch, {}
            specs = batch.specs
        else:
            specs = tuple(batch.specs) + tuple(batch.control_specs)
        return batch, workflow.run_batch(
            specs, batch_id=batch_id, data_date=data_date
        )

    @staticmethod
    def _fixed_sample_candidates(
        batch_id: str,
        brand: str,
        samples: Sequence[FixedExperimentSample],
    ) -> tuple[QuerySelectionDecision, ...]:
        selected = sorted(
            (
                sample
                for sample in samples
                if sample.group_type in {"treatment", "control"}
                and normalize_query_text(sample.query)
            ),
            key=lambda sample: (
                sample.sample_rank,
                normalize_query_key(sample.query),
                sample.group_type,
            ),
        )
        return tuple(
            QuerySelectionDecision(
                query_id=_query_id(batch_id, brand, sample.query),
                query=normalize_query_text(sample.query),
                rules=("fixed_experiment_sample",),
                metrics={
                    "experiment_id": sample.experiment_id,
                    "sample_rank": sample.sample_rank,
                },
                source_type=f"experiment_{sample.group_type}",
                rule_count=1,
                ctr_decline_pp=0.0,
                target_day_pv=0.0,
                selection_rank=index,
                selected=sample.group_type == "treatment",
                experiment_id=sample.experiment_id,
                group_type=sample.group_type,
            )
            for index, sample in enumerate(selected, start=1)
        )

    @staticmethod
    def _dynamic_candidates(
        batch_id: str,
        brand: str,
        dataset: CandidateDataset,
        config: QuerySelectionConfig,
    ) -> tuple[QuerySelectionDecision, ...]:
        valid_rows = [
            row
            for row in dataset.queries
            if is_valid_governance_query(row.query)
            and not is_excluded_governance_query(
                brand, row.query, config.excluded_keywords
            )
            and (row.search_uv if config.query_min_metric_type == "uv" else row.search_pv) >= float(config.query_min_metric_value)
        ]
        high_pv_queries = {
            normalize_query_key(row.query)
            for row in sorted(
                valid_rows,
                key=lambda row: (-row.search_pv, normalize_query_key(row.query)),
            )[: config.high_pv_limit]
        }
        candidates: list[QuerySelectionDecision] = []
        for row in valid_rows:
            rules, ctr_decline_pp = _matched_rules(
                row,
                dataset.brand,
                config,
                normalize_query_key(row.query) in high_pv_queries,
            )
            if not rules:
                continue
            candidates.append(
                QuerySelectionDecision(
                    query_id=_query_id(batch_id, brand, row.query),
                    query=normalize_query_text(row.query),
                    rules=rules,
                    metrics=row.as_dict(),
                    source_type="dynamic",
                    rule_count=len(rules),
                    ctr_decline_pp=ctr_decline_pp,
                    target_day_pv=row.search_pv,
                    selection_rank=0,
                    selected=False,
                )
            )
        ordered = sorted(
            candidates,
            key=lambda item: (
                -item.rule_count,
                -item.ctr_decline_pp,
                -item.target_day_pv,
                normalize_query_key(item.query),
            ),
        )
        return tuple(
            replace(
                candidate,
                selection_rank=index,
                selected=index <= config.extract_limit,
            )
            for index, candidate in enumerate(ordered, start=1)
        )


def _matched_rules(
    row: QueryCandidateMetrics,
    brand: BrandQueryMetrics,
    config: QuerySelectionConfig,
    high_pv: bool,
) -> tuple[tuple[str, ...], float]:
    rules: set[str] = set()
    ctr_brand_gap_pp = max(0.0, (brand.ctr - row.ctr) * 100)
    cvr_brand_gap_pp = max(0.0, (brand.cvr - row.cvr) * 100)
    ctr_decline_pp = (
        max(0.0, (row.history_ctr - row.ctr) * 100)
        if row.history_search_uv > 0
        else 0.0
    )
    cvr_decline_pp = (
        max(0.0, (row.history_cvr - row.cvr) * 100)
        if row.history_search_uv > 0
        else 0.0
    )
    if high_pv:
        rules.add("high_pv")
    if ctr_brand_gap_pp >= config.alert_ctr_gap_pp:
        rules.add("low_ctr")
    if cvr_brand_gap_pp >= config.alert_cvr_gap_pp:
        rules.add("low_cvr")
    if ctr_decline_pp >= config.alert_ctr_gap_pp:
        rules.add("ctr_decline")
    if cvr_decline_pp >= config.alert_cvr_gap_pp:
        rules.add("cvr_decline")
    if (
        row.search_pv >= config.new_query_pv_threshold
        and row.history_max_daily_pv < config.new_query_pv_threshold
    ):
        rules.add("new_query")
    if _is_rapid_pv_growth(row, config):
        rules.add("rapid_pv_growth")
    return tuple(rule for rule in RULE_ORDER if rule in rules), ctr_decline_pp


def _reorder_eligibility(
    candidate: QuerySelectionDecision,
    config: QuerySelectionConfig,
) -> tuple[bool, str | None]:
    """Return the deterministic reorder gate for one selected Query.

    Fixed treatment samples are already explicitly selected by an A/B Test and
    remain in the treatment governance path. Dynamic candidates use only the
    historical CTR/CVR decline metrics; low CTR/CVR versus the brand, PV, and
    other candidate rules are not reorder gates.
    """

    if candidate.source_type == "experiment_treatment":
        return True, None

    metrics = candidate.metrics
    current_cvr = _finite_metric(metrics.get("cvr"))
    history_cvr = _finite_metric(metrics.get("history_cvr"))
    cvr_decline_pp = max(0.0, (history_cvr - current_cvr) * 100)
    eligible = (
        candidate.ctr_decline_pp >= config.alert_ctr_gap_pp
        or cvr_decline_pp >= config.alert_cvr_gap_pp
    )
    return eligible, None if eligible else "CTR/CVR 正常"


def _finite_metric(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _is_rapid_pv_growth(
    row: QueryCandidateMetrics,
    config: QuerySelectionConfig,
) -> bool:
    average = row.history_average_pv
    if average <= 0:
        return False
    ratio_threshold = max(
        config.pv_growth_ratio_floor,
        1 + config.pv_growth_sqrt_multiplier / math.sqrt(average),
    )
    absolute_threshold = max(
        config.pv_growth_abs_floor,
        config.pv_growth_sqrt_multiplier * math.sqrt(average),
    )
    return (
        row.search_pv / average >= ratio_threshold
        and row.search_pv - average >= absolute_threshold
    )


def is_valid_governance_query(value: object) -> bool:
    query = normalize_query_text(value)
    key = normalize_query_key(query)
    if not query or key in _PLACEHOLDER_QUERIES:
        return False
    if not _VISIBLE_QUERY_RE.search(query):
        return False
    # Keep selection aligned with extraction: numeric product-code-like
    # Queries (including Atlas's demographic suffix) are not intent-bearing.
    if is_non_intent_query(query):
        return False
    if _PURE_NUMBER_RE.fullmatch(query):
        return False
    if _PRODUCT_CODE_RE.fullmatch(query):
        return False
    if _LONG_NUMBER_SUFFIX_RE.fullmatch(query):
        return False
    return True


def is_excluded_governance_query(
    brand: object,
    value: object,
    excluded_keywords: Sequence[str] = (),
) -> bool:
    excluded_keys = {
        normalize_query_key(keyword)
        for keyword in (excluded_keywords or ())
        if isinstance(keyword, str) and normalize_query_key(keyword)
    }
    if not excluded_keys:
        return False
    query = normalize_query_text(value)
    key = normalize_query_key(query)
    if not key:
        return False
    if key in excluded_keys:
        return True
    if normalize_query_key(brand) == "atlas":
        stripped = _ATLAS_TRAILING_GENDER_RE.sub("", query).strip()
        if stripped and normalize_query_key(stripped) in excluded_keys:
            return True
    return False


@dataclass(frozen=True)
class QuerySourceSpec:
    table_name: str
    query_column: str
    search_pv_column: str
    search_uv_column: str
    click_uv_column: str
    purchase_uv_column: str


def _quoted_identifier(identifier: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"unsafe SQL identifier: {identifier}")
    return f"`{identifier}`"


def build_candidate_sql(spec: QuerySourceSpec, metric_type: str = "uv") -> str:
    """Build the unbounded SQL query used for deterministic candidate facts.

    history_metrics 必须保持对基表的单层 GROUP BY：MySQL 8（腾讯 CDB 8.0.28 实测）
    对带 utf8mb4 ci collation 的派生表列再做 GROUP BY 时，磁盘临时表 group_key
    索引会产生伪 1062 Duplicate entry。压平后 history_max_daily_pv 由"日汇总的
    MAX"变为"行级 MAX"，仅影响同日大小写变体 Query 的峰值口径。
    """

    if metric_type not in {"uv", "pv"}:
        raise ValueError("metric_type must be uv or pv")

    table = _quoted_identifier(spec.table_name)
    query = _quoted_identifier(spec.query_column)
    uv = _quoted_identifier(spec.search_uv_column)
    pv = _quoted_identifier(spec.search_pv_column)
    click = _quoted_identifier(spec.click_uv_column)
    purchase = _quoted_identifier(spec.purchase_uv_column)
    valid_query = f"""
        {query} IS NOT NULL
        AND TRIM({query}) != ''
        AND LOWER(TRIM({query})) NOT IN ('null', 'none', 'undefined', 'nan', 'unknown', '未知', '无', '空', '(空)')
        AND TRIM({query}) REGEXP '[0-9A-Za-z一-龥]'
        AND TRIM({query}) NOT REGEXP '^[0-9]+$'
        AND TRIM({query}) NOT REGEXP '^([A-Za-z]{{1,5}}[-_ ]?)?[0-9]{{5,}}[A-Za-z0-9_-]*$'
        AND TRIM({query}) NOT REGEXP '^[0-9]{{4,}}[[:space:]]*(男|女|童|儿童|男士|女士|鞋|服|裤|裙|上衣|外套)$'
    """.strip()
    return f"""
WITH current_metrics AS (
    SELECT
        TRIM({query}) AS query_text,
        SUM({pv}) AS search_pv,
        SUM({uv}) AS search_uv,
        SUM({click}) AS click_uv,
        SUM({purchase}) AS purchase_uv
    FROM {table}
    WHERE `date` = :target_date
      AND {valid_query}
    GROUP BY TRIM({query})
        HAVING SUM({uv if metric_type == "uv" else pv}) >= :query_min_metric
),
history_metrics AS (
    SELECT
        TRIM({query}) AS query_text,
        SUM({uv}) AS history_search_uv,
        SUM({click}) AS history_click_uv,
        SUM({purchase}) AS history_purchase_uv,
        SUM({pv}) * 1.0 / :history_days AS history_average_pv,
        MAX({pv}) AS history_max_daily_pv
    FROM {table}
    WHERE `date` BETWEEN :history_start_date AND :history_end_date
      AND {query} IS NOT NULL
      AND TRIM({query}) != ''
    GROUP BY TRIM({query})
),
brand_metrics AS (
    SELECT
        SUM({uv}) AS brand_search_uv,
        SUM({click}) AS brand_click_uv,
        SUM({purchase}) AS brand_purchase_uv
    FROM {table}
    WHERE `date` = :target_date
)
SELECT
    current_metrics.query_text AS query,
    current_metrics.search_pv,
    current_metrics.search_uv,
    current_metrics.click_uv,
    current_metrics.purchase_uv,
    COALESCE(history_metrics.history_search_uv, 0) AS history_search_uv,
    COALESCE(history_metrics.history_click_uv, 0) AS history_click_uv,
    COALESCE(history_metrics.history_purchase_uv, 0) AS history_purchase_uv,
    COALESCE(history_metrics.history_average_pv, 0) AS history_average_pv,
    COALESCE(history_metrics.history_max_daily_pv, 0) AS history_max_daily_pv,
    COALESCE(brand_metrics.brand_search_uv, 0) AS brand_search_uv,
    COALESCE(brand_metrics.brand_click_uv, 0) AS brand_click_uv,
    COALESCE(brand_metrics.brand_purchase_uv, 0) AS brand_purchase_uv
FROM current_metrics
LEFT JOIN history_metrics ON history_metrics.query_text = current_metrics.query_text
CROSS JOIN brand_metrics
ORDER BY current_metrics.search_pv DESC, current_metrics.query_text ASC
""".strip()


class SqlQueryCandidateSource:
    """Read candidate facts from the configured read-only behavior database."""

    def __init__(self, engine: Any, specs: Mapping[str, QuerySourceSpec]) -> None:
        self.engine = engine
        self.specs = dict(specs)

    def fetch(
        self,
        brand: str,
        data_date: date,
        config: QuerySelectionConfig,
    ) -> CandidateDataset:
        try:
            spec = self.specs[brand]
        except KeyError as exc:
            raise ValueError(f"unsupported Query candidate brand: {brand}") from exc
        history_end = data_date - timedelta(days=1)
        history_start = data_date - timedelta(days=config.history_days)
        params = {
            "target_date": data_date.isoformat(),
            "history_start_date": history_start.isoformat(),
            "history_end_date": history_end.isoformat(),
            "history_days": config.history_days,
            "query_min_metric": float(config.query_min_metric_value),
        }
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(build_candidate_sql(spec, config.query_min_metric_type)), params
            ).mappings().all()
        brand_metrics = BrandQueryMetrics(0, 0, 0)
        queries: list[QueryCandidateMetrics] = []
        for row in rows:
            if not queries:
                brand_metrics = BrandQueryMetrics(
                    _number(row.get("brand_search_uv")),
                    _number(row.get("brand_click_uv")),
                    _number(row.get("brand_purchase_uv")),
                )
            queries.append(
                QueryCandidateMetrics(
                    query=normalize_query_text(row.get("query")),
                    search_pv=_number(row.get("search_pv")),
                    search_uv=_number(row.get("search_uv")),
                    click_uv=_number(row.get("click_uv")),
                    purchase_uv=_number(row.get("purchase_uv")),
                    history_search_uv=_number(row.get("history_search_uv")),
                    history_click_uv=_number(row.get("history_click_uv")),
                    history_purchase_uv=_number(row.get("history_purchase_uv")),
                    history_average_pv=_number(row.get("history_average_pv")),
                    history_max_daily_pv=_number(
                        row.get("history_max_daily_pv")
                    ),
                )
            )
        return CandidateDataset(brand=brand_metrics, queries=tuple(queries))


def _number(value: Any) -> float:
    number = float(value or 0)
    if not math.isfinite(number) or number < 0:
        raise ValueError("Query candidate metrics must be finite and non-negative")
    return number


def _query_id(batch_id: str, brand: str, query: str) -> str:
    return f"{batch_id}:{brand}:{normalize_query_key(query)}"


__all__ = [
    "BrandQueryMetrics",
    "CandidateDataset",
    "FixedExperimentSample",
    "InMemoryQuerySelectionStore",
    "QueryCandidateMetrics",
    "QueryCandidateSource",
    "QuerySelectionBatch",
    "QuerySelectionConfig",
    "QuerySelectionDecision",
    "QuerySelectionService",
    "QuerySelectionStore",
    "SqlAlchemyQuerySelectionStore",
    "QuerySourceSpec",
    "SqlQueryCandidateSource",
    "build_candidate_sql",
    "is_excluded_governance_query",
    "is_valid_governance_query",
]
