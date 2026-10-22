"""Materialize local Query metric snapshots from persisted candidates.

Production execution belongs to the release runbook; this command only operates
on the configured application database and is safe to rerun.
"""
from __future__ import annotations

import argparse
from datetime import date

from src.persistence.schema import init_db
from src.persistence.session import SessionLocal
from src.integrations.query_extractor import QueryExtractor
from src.query_governance.metric_snapshots import materialize_candidate_metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brand")
    parser.add_argument("--date", dest="data_date")
    parser.add_argument("--from-source", action="store_true")
    args = parser.parse_args()
    init_db()
    extractor = QueryExtractor() if args.from_source else None
    provider = extractor.get_query_performance if extractor else None
    batch_provider = extractor.get_queries_performance_batch if extractor else None
    with SessionLocal() as session:
        count = materialize_candidate_metrics(
            session, brand=args.brand,
            data_date=date.fromisoformat(args.data_date) if args.data_date else None,
            metric_provider=provider,
            metric_batch_provider=batch_provider,
            as_of_date=date.today(),
        )
    print(f"materialized={count}")


if __name__ == "__main__":
    main()
