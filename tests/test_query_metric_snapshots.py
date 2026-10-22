import datetime as dt
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.metric_snapshots import materialize_candidate_metrics
from src.query_governance.models import QueryGovernanceCandidate, QueryGovernanceSelectionBatch, QueryMetricSnapshot


def test_materialize_candidate_metrics_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QueryGovernanceSelectionBatch.__table__, QueryGovernanceCandidate.__table__, QueryMetricSnapshot.__table__])
    Session = sessionmaker(bind=engine)
    with Session() as session:
        batch = QueryGovernanceSelectionBatch(batch_id="b", brand="atlas", data_date=dt.date(2026, 9, 5), config_json="{}")
        session.add(batch); session.flush()
        session.add(QueryGovernanceCandidate(selection_batch_id=batch.id, batch_id="b", brand="atlas", data_date=batch.data_date, query_id="q", query_key="q", query_text="dress", matched_rules_json="[]", metrics_json=json.dumps({"search_pv": 100, "search_uv": 80, "click_uv": 20, "add_to_cart_uv": 8, "purchase_uv": 4}), source_type="test", selection_rank=1, selected=True))
        session.commit()
        assert materialize_candidate_metrics(session) == 1
        assert materialize_candidate_metrics(session) == 1
        assert session.query(QueryMetricSnapshot).count() == 1
        row = session.query(QueryMetricSnapshot).one()
        assert row.pv == 100 and row.add_to_cart_uv == 8 and row.ctr == 0.25 and row.cvr == 0.05


def test_materialize_candidate_metrics_uses_configured_window_provider_and_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QueryGovernanceSelectionBatch.__table__, QueryGovernanceCandidate.__table__, QueryMetricSnapshot.__table__])
    Session = sessionmaker(bind=engine)
    with Session() as session:
        batch = QueryGovernanceSelectionBatch(batch_id="window", brand="atlas", data_date=dt.date(2026, 9, 5), config_json=json.dumps({"history_days": 2}))
        session.add(batch); session.flush()
        session.add(QueryGovernanceCandidate(selection_batch_id=batch.id, batch_id="window", brand="atlas", data_date=batch.data_date, query_id="q", query_key="q", query_text="dress", matched_rules_json="[]", metrics_json=json.dumps({"search_pv": 100, "search_uv": 80, "click_uv": 20, "add_to_cart_uv": 8, "purchase_uv": 4}), source_type="test", selection_rank=1, selected=True))
        session.commit()

        def provider(brand, query, metric_date):
            return {"search_pv": 50, "search_uv": 25, "click_uv": 5, "add_to_cart_uv": 3, "purchase_uv": 2}

        from src.query_governance.metric_snapshots import materialize_candidate_metrics
        assert materialize_candidate_metrics(session, metric_provider=provider) == 5
        assert materialize_candidate_metrics(session, metric_provider=provider) == 5
        rows = session.query(QueryMetricSnapshot).order_by(QueryMetricSnapshot.data_date).all()
        assert [row.data_date.isoformat() for row in rows] == ["2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07"]
        assert session.query(QueryMetricSnapshot).count() == 5
        assert rows[0].add_to_cart_uv == 3


def test_materialize_candidate_metrics_does_not_persist_missing_dates():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QueryGovernanceSelectionBatch.__table__, QueryGovernanceCandidate.__table__, QueryMetricSnapshot.__table__])
    Session = sessionmaker(bind=engine)
    with Session() as session:
        batch = QueryGovernanceSelectionBatch(batch_id="missing", brand="atlas", data_date=dt.date(2026, 9, 5), config_json=json.dumps({"history_days": 1}))
        session.add(batch); session.flush()
        session.add(QueryGovernanceCandidate(selection_batch_id=batch.id, batch_id="missing", brand="atlas", data_date=batch.data_date, query_id="q", query_key="q", query_text="dress", matched_rules_json="[]", metrics_json="{}", source_type="test", selection_rank=1, selected=True))
        session.commit()

        def provider(_brand, _query, metric_date):
            return {"status": "missing"} if metric_date == dt.date(2026, 9, 4) else {"search_pv": 1, "search_uv": 1}

        materialize_candidate_metrics(session, metric_provider=provider)
        assert [row.data_date for row in session.query(QueryMetricSnapshot).all()] == [dt.date(2026, 9, 5), dt.date(2026, 9, 6)]


def test_materialize_candidate_metrics_batches_real_source_reads_by_brand_and_date():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QueryGovernanceSelectionBatch.__table__, QueryGovernanceCandidate.__table__, QueryMetricSnapshot.__table__])
    Session = sessionmaker(bind=engine)
    with Session() as session:
        batch = QueryGovernanceSelectionBatch(batch_id="batch-source", brand="atlas", data_date=dt.date(2026, 9, 5), config_json=json.dumps({"history_days": 1}))
        session.add(batch); session.flush()
        for rank, query_text in enumerate(("dress", "shirt"), start=1):
            session.add(QueryGovernanceCandidate(selection_batch_id=batch.id, batch_id=batch.batch_id, brand="atlas", data_date=batch.data_date, query_id=query_text, query_key=query_text, query_text=query_text, matched_rules_json="[]", metrics_json="{}", source_type="test", selection_rank=rank, selected=True))
        session.commit()

        calls = []

        def batch_provider(brand, queries, metric_date):
            calls.append((brand, tuple(queries), metric_date))
            return {
                query: {
                    "status": "complete",
                    "search_pv": 100 + index,
                    "search_uv": 50,
                    "click_uv": 10,
                    "add_to_cart_uv": 5,
                    "purchase_uv": 2,
                }
                for index, query in enumerate(queries)
            }

        written = materialize_candidate_metrics(session, metric_batch_provider=batch_provider)

        assert written == 6
        assert calls == [
            ("atlas", ("dress", "shirt"), dt.date(2026, 9, 4)),
            ("atlas", ("dress", "shirt"), dt.date(2026, 9, 5)),
            ("atlas", ("dress", "shirt"), dt.date(2026, 9, 6)),
        ]
        rows = session.query(QueryMetricSnapshot).order_by(QueryMetricSnapshot.data_date, QueryMetricSnapshot.query_text).all()
        assert len(rows) == 6
        assert all(row.add_to_cart_uv == 5 for row in rows)


def test_materialize_tracked_query_metrics_scopes_to_execution_date_candidates():
    from src.query_governance.scheduler import _materialize_tracked_query_metrics

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            QueryGovernanceSelectionBatch.__table__,
            QueryGovernanceCandidate.__table__,
            QueryMetricSnapshot.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    with Session() as session:
        batch_old = QueryGovernanceSelectionBatch(
            batch_id="b-old",
            brand="atlas",
            data_date=dt.date(2026, 9, 1),
            config_json=json.dumps({"history_days": 1}),
        )
        batch_new = QueryGovernanceSelectionBatch(
            batch_id="b-new",
            brand="atlas",
            data_date=dt.date(2026, 9, 2),
            config_json=json.dumps({"history_days": 1}),
        )
        session.add_all([batch_old, batch_new])
        session.flush()
        session.add_all([
            QueryGovernanceCandidate(
                selection_batch_id=batch_old.id,
                batch_id=batch_old.batch_id,
                brand="atlas",
                data_date=batch_old.data_date,
                query_id="q-old",
                query_key="old_dress",
                query_text="old_dress",
                matched_rules_json="[]",
                metrics_json="{}",
                source_type="test",
                selection_rank=1,
                selected=True,
            ),
            QueryGovernanceCandidate(
                selection_batch_id=batch_new.id,
                batch_id=batch_new.batch_id,
                brand="atlas",
                data_date=batch_new.data_date,
                query_id="q-new",
                query_key="new_skirt",
                query_text="new_skirt",
                matched_rules_json="[]",
                metrics_json="{}",
                source_type="test",
                selection_rank=1,
                selected=True,
            ),
        ])
        session.commit()

    called_queries = []

    def provider(brand, query, metric_date):
        called_queries.append((query, metric_date))
        return {"search_pv": 10, "search_uv": 5}

    written = _materialize_tracked_query_metrics(
        "atlas",
        dt.date(2026, 9, 2),
        Session,
        metric_provider=provider,
    )

    assert written == 2  # 9-1 and 9-2 for new_skirt (as_of_date <= 9-2)
    assert all(query == "new_skirt" for query, _ in called_queries)
    assert "old_dress" not in [query for query, _ in called_queries]

    with Session() as session:
        rows = session.query(QueryMetricSnapshot).all()
        assert len(rows) == 2
        assert all(row.query_text == "new_skirt" for row in rows)
        assert sorted(row.data_date.isoformat() for row in rows) == ["2026-09-01", "2026-09-02"]
