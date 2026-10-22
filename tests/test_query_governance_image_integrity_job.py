from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.image_integrity import image_storage_key
from src.query_governance.models import QueryGovernanceImageAsset, QueryGovernanceTask
from src.query_governance.scheduler import query_governance_image_integrity_job

COS_PREFIX = "query-governance/images/test/v1"


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _seed_task(session_factory, *, task_id_batch: str, products: list[dict], data_date: date | None = None):
    with session_factory() as session:
        session.add(QueryGovernanceTask(
            batch_id=task_id_batch,
            brand="atlas",
            data_date=data_date or date.today(),
            query_text="外套",
            query_rank=1,
            status="completed",
            s0_snapshot=json.dumps({"products": products}, ensure_ascii=False),
        ))
        session.commit()


def _seed_asset(session_factory, *, product_id: str, source_url: str):
    storage_key = image_storage_key(source_url, prefix=COS_PREFIX)
    with session_factory() as session:
        session.add(QueryGovernanceImageAsset(
            brand="atlas",
            product_id=product_id,
            source_url=source_url,
            source_url_hash="0" * 64,
            storage_key=storage_key,
            cached_url=f"https://cos.example.com/{storage_key}",
            status="ready",
        ))
        session.commit()


def test_image_integrity_job_reports_clean_data_without_notification():
    engine, session_factory = _database()
    try:
        _seed_task(session_factory, task_id_batch="batch-clean", products=[
            {"product_id": "P1", "product_name": "商品一", "image_url": "https://img.test/P1.jpg"},
            {"product_id": "P2", "product_name": "商品二", "image_url": "https://img.test/P2.jpg"},
        ])
        _seed_asset(session_factory, product_id="P1", source_url="https://img.test/P1.jpg")
        _seed_asset(session_factory, product_id="P2", source_url="https://img.test/P2.jpg")
        notified: list[tuple[str, str]] = []

        result = query_governance_image_integrity_job(
            "atlas",
            session_factory=session_factory,
            notifier=lambda content, title: notified.append((content, title)),
        )

        assert result["status"] == "success"
        assert result["brands"]["atlas"]["status"] == "PASS"
        assert result["brands"]["atlas"]["productCount"] == 2
        assert result["brands"]["atlas"]["assetMismatchCount"] == 0
        assert notified == []
    finally:
        engine.dispose()


def test_image_integrity_job_alerts_on_shared_polluted_image():
    engine, session_factory = _database()
    try:
        polluted = "https://img.test/polluted-shared.jpg"
        _seed_task(session_factory, task_id_batch="batch-bad", products=[
            {"product_id": "P1", "product_name": "商品一", "image_url": polluted},
            {"product_id": "P2", "product_name": "商品二", "image_url": polluted},
            {"product_id": "P3", "product_name": "商品三", "image_url": polluted},
            {"product_id": "P4", "product_name": "正常商品", "image_url": "https://img.test/P4.jpg"},
        ])
        notified: list[tuple[str, str]] = []

        result = query_governance_image_integrity_job(
            "atlas",
            session_factory=session_factory,
            notifier=lambda content, title: notified.append((content, title)),
        )

        assert result["status"] == "alert"
        brand = result["brands"]["atlas"]
        assert brand["status"] == "ALERT"
        assert brand["sharedProductCount"] == 3
        alert_codes = {alert["code"] for alert in brand["alerts"]}
        assert "IMAGE_URL_SHARED" in alert_codes
        assert len(notified) == 1
        content, title = notified[0]
        assert "[atlas]" in content
        assert "共用同一张图" in content
        assert title == "Query治理商品图完整性告警"
    finally:
        engine.dispose()


def test_image_integrity_job_skips_tasks_outside_lookback_window():
    engine, session_factory = _database()
    try:
        polluted = "https://img.test/polluted-shared.jpg"
        _seed_task(
            session_factory,
            task_id_batch="batch-old",
            products=[
                {"product_id": "P1", "product_name": "商品一", "image_url": polluted},
                {"product_id": "P2", "product_name": "商品二", "image_url": polluted},
                {"product_id": "P3", "product_name": "商品三", "image_url": polluted},
            ],
            data_date=date.today() - timedelta(days=10),
        )
        notified: list[tuple[str, str]] = []

        result = query_governance_image_integrity_job(
            "atlas",
            session_factory=session_factory,
            notifier=lambda content, title: notified.append((content, title)),
        )

        assert result["status"] == "success"
        assert result["brands"]["atlas"]["productCount"] == 0
        assert notified == []
    finally:
        engine.dispose()


def test_image_integrity_job_alerts_on_asset_cache_pointer_mismatch():
    engine, session_factory = _database()
    try:
        _seed_task(session_factory, task_id_batch="batch-ok", products=[
            {"product_id": "P1", "product_name": "商品一", "image_url": "https://img.test/P1.jpg"},
        ])
        # 资产行指向别人的缓存对象（事故中的 secondary damage 形态）
        with session_factory() as session:
            session.add(QueryGovernanceImageAsset(
                brand="atlas",
                product_id="P1",
                source_url="https://img.test/P1.jpg",
                source_url_hash="0" * 64,
                storage_key=image_storage_key("https://img.test/P1.jpg", prefix=COS_PREFIX),
                cached_url="https://cos.example.com/query-governance/images/test/v1/deadbeef.jpg",
                status="ready",
            ))
            session.commit()
        notified: list[tuple[str, str]] = []

        result = query_governance_image_integrity_job(
            "atlas",
            session_factory=session_factory,
            notifier=lambda content, title: notified.append((content, title)),
        )

        assert result["status"] == "alert"
        brand = result["brands"]["atlas"]
        assert brand["assetMismatchCount"] == 1
        assert brand["status"] == "ALERT"
        assert len(notified) == 1
    finally:
        engine.dispose()
