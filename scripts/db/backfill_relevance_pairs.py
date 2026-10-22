"""Backfill materialized overview summaries with the irrelevant pair list.

相关性检测准确率在读时用 (query, product) 对清单投影当前拉黑状态。该字段
上线前已物化的批次快照只存了计数；本命令对这些批次重新物化，把对清单补进
payload。仅做本地只读重算 + 汇总表 upsert：不调用模型、不写任何外部接口，
可安全重复执行。

安全保护：重建后 queryCount / detectedProductCount / irrelevantProductCount
任一与原快照不一致时，立即还原原 payload 并跳过该批次（说明其底层明细
已不完整，重算会丢数据）。
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from sqlalchemy import select

from src.persistence.schema import init_db
from src.persistence.session import SessionLocal
from src.query_governance.models import QueryGovernanceBatchSummary
from src.query_governance.read_api import QueryGovernanceReadService


def _telemetry(payload: dict) -> dict:
    telemetry = payload.get("telemetry")
    return telemetry if isinstance(telemetry, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser(description="补齐已物化批次汇总的不相关商品对清单")
    parser.add_argument("--brand", help="只处理指定品牌")
    parser.add_argument("--date", dest="data_date", help="只处理指定日期 (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=30, help="处理最近 N 天的批次，默认 30")
    args = parser.parse_args()
    init_db()
    service = QueryGovernanceReadService(SessionLocal)

    with SessionLocal() as session:
        query = select(
            QueryGovernanceBatchSummary.brand,
            QueryGovernanceBatchSummary.data_date,
            QueryGovernanceBatchSummary.batch_id,
        )
        if args.brand:
            query = query.where(QueryGovernanceBatchSummary.brand == args.brand)
        if args.data_date:
            query = query.where(QueryGovernanceBatchSummary.data_date == date.fromisoformat(args.data_date))
        elif args.days:
            cutoff = date.today() - timedelta(days=args.days - 1)
            query = query.where(QueryGovernanceBatchSummary.data_date >= cutoff)
        rows = session.execute(
            query.order_by(QueryGovernanceBatchSummary.data_date.asc())
        ).all()

    updated = already = reverted = 0
    for brand, data_date_value, batch_id in rows:
        with SessionLocal() as session:
            stored = session.execute(
                select(QueryGovernanceBatchSummary).where(
                    QueryGovernanceBatchSummary.batch_id == batch_id,
                    QueryGovernanceBatchSummary.brand == brand,
                )
            ).scalar_one()
            old_payload_json = stored.payload_json
        if isinstance(_telemetry(json.loads(old_payload_json)).get("irrelevantProductPairs"), list):
            already += 1
            continue

        rebuilt = service.materialize_overview_summary(brand, data_date=data_date_value, batch_id=batch_id)
        old_payload = json.loads(old_payload_json)
        old_telemetry = _telemetry(old_payload)
        new_telemetry = _telemetry(rebuilt)
        diverged = (
            rebuilt.get("queryCount") != old_payload.get("queryCount")
            or new_telemetry.get("detectedProductCount") != old_telemetry.get("detectedProductCount")
            or new_telemetry.get("irrelevantProductCount") != old_telemetry.get("irrelevantProductCount")
        )
        if diverged or not isinstance(new_telemetry.get("irrelevantProductPairs"), list):
            # 底层明细已不完整：还原原快照，保持该批次为新指标缺口。
            with SessionLocal() as session:
                stored = session.execute(
                    select(QueryGovernanceBatchSummary).where(
                        QueryGovernanceBatchSummary.batch_id == batch_id,
                        QueryGovernanceBatchSummary.brand == brand,
                    )
                ).scalar_one()
                stored.payload_json = old_payload_json
                session.commit()
            reverted += 1
            print(f"reverted {brand} {data_date_value} {batch_id}: rebuilt summary diverges from stored counts")
            continue
        updated += 1
        print(
            f"updated {brand} {data_date_value} {batch_id}: "
            f"pairs={len(new_telemetry['irrelevantProductPairs'])}"
        )

    print(f"updated={updated} already_had_pairs={already} reverted={reverted}")


if __name__ == "__main__":
    main()
