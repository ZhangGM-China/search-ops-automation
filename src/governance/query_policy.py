"""Cross-domain policy checks for governed query writes."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy.exc import OperationalError

from src.persistence import SessionLocal, TopQueryExperiment, TopQueryExperimentSample

from .query_identity import normalize_governance_query_key


class ExperimentValidationError(ValueError):
    """A governed write conflicts with an active experiment contract."""


def assert_query_governance_allowed(brand: str, query_text: str) -> None:
    assert_queries_governance_allowed(brand, [query_text])


def assert_queries_governance_allowed(brand: str, query_texts: Sequence[str]) -> None:
    db = SessionLocal()
    try:
        assert_queries_governance_allowed_in_session(db, brand, query_texts)
    except OperationalError as exc:
        if "no such table: top_query_experiment" not in str(exc).lower():
            raise
    finally:
        db.close()


def assert_queries_governance_allowed_in_session(
    db,
    brand: str,
    query_texts: Sequence[str],
) -> None:
    experiment = db.query(TopQueryExperiment).filter(
        TopQueryExperiment.brand == brand,
        TopQueryExperiment.status == "running",
    ).order_by(TopQueryExperiment.id.desc()).first()
    if not experiment:
        return

    query_keys = {
        normalize_governance_query_key(brand, query_text)
        for query_text in query_texts
    }
    samples = db.query(TopQueryExperimentSample).filter(
        TopQueryExperimentSample.experiment_id == experiment.id,
        TopQueryExperimentSample.group_type == "control",
    ).all()
    if any(
        normalize_governance_query_key(brand, sample.query_text) in query_keys
        for sample in samples
    ):
        raise ExperimentValidationError("运行中的 A/B Test 对照组禁止治理")
