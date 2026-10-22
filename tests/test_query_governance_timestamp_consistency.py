from datetime import date, datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.query_governance.models import QueryGovernanceTask, QueryGovernanceWorkflowRun


def test_query_governance_lifecycle_defaults_are_utc_naive():
    engine = create_engine("sqlite:///:memory:")
    QueryGovernanceTask.__table__.create(engine)
    QueryGovernanceWorkflowRun.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as session:
        task = QueryGovernanceTask(
            batch_id="batch-timestamp",
            brand="atlas",
            data_date=date(2026, 9, 2),
            query_text="裙子",
        )
        run = QueryGovernanceWorkflowRun(batch_id="batch-timestamp")
        session.add_all([task, run])
        session.commit()

        expected = datetime.now(timezone.utc).replace(tzinfo=None)
        for value in (
            task.created_at,
            task.updated_at,
            run.started_at,
            run.created_at,
            run.updated_at,
        ):
            assert value is not None
            assert value.tzinfo is None
            assert abs(value - expected).total_seconds() < 2

