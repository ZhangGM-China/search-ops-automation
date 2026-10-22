import datetime
import json
import os
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from src.persistence import (
    Base,
    NoResultGovernanceAction,
    NoResultGovernanceBatch,
    NoResultQuery,
    NoResultRetagTask,
    SessionLocal,
)


def _setup_memory_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine


def _seed_ready_no_result_data():
    now = datetime.datetime(2026, 6, 26, 9, 0, 0)
    db = SessionLocal()
    try:
        for brand in ["harbor", "riviera", "summit"]:
            batch = NoResultGovernanceBatch(
                brand=brand,
                batch_date=now - datetime.timedelta(days=1),
                status="completed",
                total_queries=1,
                changed_queries=1,
                retag_status="success",
                rollback_status="pending",
                started_at=now,
                completed_at=now,
            )
            db.add(batch)
            db.flush()
            query = NoResultQuery(
                batch_id=batch.id,
                brand=brand,
                query_text=f"{brand}_ready_query",
                data_date=now - datetime.timedelta(days=1),
                status="completed",
                before_result_count=0,
                after_result_count=3,
                search_pv=10,
                reviewed_at=now,
            )
            db.add(query)
            db.flush()
            db.add(NoResultGovernanceAction(
                query_id=query.id,
                batch_id=batch.id,
                brand=brand,
                query_text=query.query_text,
                action_type="add_tag",
                target_interface="/admin/tag/addTag",
                request_payload=json.dumps({"actionType": "add_tag"}),
                response_payload=json.dumps({"code": 1, "message": "success"}),
                before_snapshot=json.dumps({"target": query.query_text}),
                rollback_payload=json.dumps({"actionType": "blacklist_tag"}),
                status="success",
                rollback_status="pending",
                risk_level="low",
                error_message="executed by readiness-test",
                created_at=now,
                updated_at=now,
            ))
            db.add(NoResultRetagTask(
                brand=brand,
                batch_ids=json.dumps([str(batch.id)]),
                status="success",
                earliest_run_at=now,
                triggered_at=now,
                response_payload=json.dumps({"success": True}),
                updated_at=now,
            ))
        db.commit()
    finally:
        db.close()


def _seed_ready_data_with_partial_action_coverage():
    now = datetime.datetime(2026, 6, 26, 9, 0, 0)
    db = SessionLocal()
    try:
        for brand in ["harbor", "riviera", "summit"]:
            batch = NoResultGovernanceBatch(
                brand=brand,
                batch_date=now - datetime.timedelta(days=1),
                status="completed",
                total_queries=1,
                changed_queries=1,
                retag_status="success",
                rollback_status="pending",
                started_at=now,
                completed_at=now,
            )
            db.add(batch)
            db.flush()
            query = NoResultQuery(
                batch_id=batch.id,
                brand=brand,
                query_text=f"{brand}_partial_query",
                data_date=now - datetime.timedelta(days=1),
                status="completed",
                before_result_count=0,
                after_result_count=3,
                search_pv=10,
                reviewed_at=now,
            )
            db.add(query)
            db.flush()
            if brand == "harbor":
                db.add(NoResultGovernanceAction(
                    query_id=query.id,
                    batch_id=batch.id,
                    brand=brand,
                    query_text=query.query_text,
                    action_type="add_tag",
                    target_interface="/admin/tag/addTag",
                    request_payload=json.dumps({"actionType": "add_tag"}),
                    response_payload=json.dumps({"code": 1, "message": "success"}),
                    before_snapshot=json.dumps({"target": query.query_text}),
                    rollback_payload=json.dumps({"actionType": "blacklist_tag"}),
                    status="success",
                    rollback_status="pending",
                    risk_level="low",
                    created_at=now,
                    updated_at=now,
                ))
            db.add(NoResultRetagTask(
                brand=brand,
                batch_ids=json.dumps([str(batch.id)]),
                status="success",
                earliest_run_at=now,
                triggered_at=now,
                response_payload=json.dumps({"success": True}),
                updated_at=now,
            ))
        db.commit()
    finally:
        db.close()


def test_readiness_report_blocks_when_stg_write_is_not_authorized():
    from tests.manual_scripts.verification_scripts.verify_no_result_production_readiness import (
        build_readiness_report,
    )

    engine = _setup_memory_db()
    try:
        _seed_ready_no_result_data()
        with patch.dict(os.environ, {
            "ENV": "test",
            "NO_RESULT_KG_REAL_WRITE": "",
            "KG_API_TOKEN": "",
        }, clear=False):
            report = build_readiness_report()

        assert report["environment"]["env"] == "test"
        assert report["writeAuthorization"]["authorized"] is False
        assert "stg_kg_write_not_authorized" in report["blockers"]
        assert report["conclusion"] == "仍不能上生产"
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()

def test_readiness_report_can_be_conditionally_ready_with_full_real_write_evidence():
    from tests.manual_scripts.verification_scripts.verify_no_result_production_readiness import (
        build_readiness_report,
    )

    engine = _setup_memory_db()
    try:
        _seed_ready_no_result_data()
        with patch.dict(os.environ, {
            "ENV": "test",
            "NO_RESULT_KG_REAL_WRITE": "true",
            "KG_API_TOKEN": "stg-token",
            "KG_BASE_URL": "https://kg-stg.example.test",
        }, clear=False):
            report = build_readiness_report()

        assert report["writeAuthorization"]["authorized"] is True
        assert report["brandCoverage"]["missingBrands"] == []
        assert report["actionEvidence"]["successfulActions"] == 3
        assert report["retagReviewEvidence"]["successfulRetagTasks"] == 3
        assert report["retagReviewEvidence"]["completedReviewedQueries"] == 3
        assert report["rollbackEvidence"]["actionsWithRollbackPayload"] == 3
        assert report["conclusion"] == "有条件可上生产"
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_readiness_report_blocks_production_kg_without_explicit_authorization():
    from tests.manual_scripts.verification_scripts.verify_no_result_production_readiness import (
        build_readiness_report,
    )

    engine = _setup_memory_db()
    try:
        _seed_ready_no_result_data()
        with patch.dict(os.environ, {
            "ENV": "test",
            "NO_RESULT_KG_REAL_WRITE": "true",
            "KG_API_TOKEN": "prod-token",
            "KG_BASE_URL": "https://kg.example.test",
            "NO_RESULT_ALLOW_PRODUCTION_KG": "",
        }, clear=False):
            report = build_readiness_report()

        assert report["writeAuthorization"]["authorized"] is False
        assert "KG_BASE_URL points to production KG without NO_RESULT_ALLOW_PRODUCTION_KG=true" in report["writeAuthorization"]["missing"]
        assert "stg_kg_write_not_authorized" in report["blockers"]
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_readiness_report_allows_production_kg_with_explicit_authorization():
    from tests.manual_scripts.verification_scripts.verify_no_result_production_readiness import (
        build_readiness_report,
    )

    engine = _setup_memory_db()
    try:
        _seed_ready_no_result_data()
        with patch.dict(os.environ, {
            "ENV": "test",
            "NO_RESULT_KG_REAL_WRITE": "true",
            "KG_API_TOKEN": "prod-token",
            "KG_BASE_URL": "https://kg.example.test",
            "NO_RESULT_ALLOW_PRODUCTION_KG": "true",
        }, clear=False):
            report = build_readiness_report()

        assert report["writeAuthorization"]["authorized"] is True
        assert report["writeAuthorization"]["productionLikeEndpoint"] is True
        assert report["writeAuthorization"]["productionKgExplicitlyAllowed"] is True
        assert "stg_kg_write_not_authorized" not in report["blockers"]
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_readiness_report_flags_missing_brand_and_mutation_action_coverage():
    from tests.manual_scripts.verification_scripts.verify_no_result_production_readiness import (
        build_readiness_report,
    )

    engine = _setup_memory_db()
    try:
        _seed_ready_data_with_partial_action_coverage()
        with patch.dict(os.environ, {
            "ENV": "test",
            "NO_RESULT_KG_REAL_WRITE": "true",
            "KG_API_TOKEN": "stg-token",
            "KG_BASE_URL": "https://kg-stg.example.test",
        }, clear=False):
            report = build_readiness_report()

        assert report["actionEvidence"]["brandsMissingSuccessfulAction"] == ["riviera", "summit"]
        assert "successful_action_missing_for_brand:summit" in report["blockers"]
        assert "successful_action_missing_for_brand:riviera" in report["blockers"]
        assert "missing_action_type_coverage:add_relationship" in report["warnings"]
        assert "missing_mutation_action_coverage" in report["warnings"]
        assert report["conclusion"] == "仍不能上生产"
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_readiness_report_accepts_explicitly_excluded_disabled_brand():
    from tests.manual_scripts.verification_scripts.verify_no_result_production_readiness import (
        build_readiness_report,
    )

    engine = _setup_memory_db()
    try:
        _seed_ready_data_with_partial_action_coverage()
        with patch.dict(os.environ, {
            "ENV": "test",
            "NO_RESULT_KG_REAL_WRITE": "true",
            "KG_API_TOKEN": "stg-token",
            "KG_BASE_URL": "https://kg-stg.example.test",
            "NO_RESULT_PRODUCTION_EXCLUDED_BRANDS": "summit",
        }, clear=False), patch(
            "tests.manual_scripts.verification_scripts.verify_no_result_production_readiness.BRAND_CONFIGS",
            {
                "harbor": {"enabled": True},
                "riviera": {"enabled": True},
                "summit": {"enabled": False},
            },
        ), patch(
            "tests.manual_scripts.verification_scripts.verify_no_result_production_readiness.is_brand_enabled",
            side_effect=lambda brand: brand != "summit",
        ):
            report = build_readiness_report()

        assert report["brandDecision"]["excludedBrands"] == ["summit"]
        assert report["brandCoverage"]["excludedUnverifiedBrands"] == ["summit"]
        assert "brand_not_verified:summit" not in report["blockers"]
        assert "successful_action_missing_for_brand:summit" not in report["blockers"]
        assert "production_excluded_brand:summit" in report["warnings"]
        assert "successful_action_missing_for_brand:riviera" in report["blockers"]
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_readiness_report_blocks_excluded_brand_when_config_still_enabled():
    from tests.manual_scripts.verification_scripts.verify_no_result_production_readiness import (
        build_readiness_report,
    )

    engine = _setup_memory_db()
    try:
        _seed_ready_data_with_partial_action_coverage()
        with patch.dict(os.environ, {
            "ENV": "test",
            "NO_RESULT_KG_REAL_WRITE": "true",
            "KG_API_TOKEN": "stg-token",
            "KG_BASE_URL": "https://kg-stg.example.test",
            "NO_RESULT_PRODUCTION_EXCLUDED_BRANDS": "summit",
        }, clear=False), patch(
            "tests.manual_scripts.verification_scripts.verify_no_result_production_readiness.BRAND_CONFIGS",
            {
                "harbor": {"enabled": True},
                "riviera": {"enabled": True},
                "summit": {"enabled": True},
            },
        ), patch(
            "tests.manual_scripts.verification_scripts.verify_no_result_production_readiness.is_brand_enabled",
            return_value=True,
        ):
            report = build_readiness_report()

        assert "excluded_brand_still_enabled:summit" in report["blockers"]
        assert "brand_not_verified:summit" in report["blockers"]
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_readiness_main_returns_nonzero_when_blockers_exist(monkeypatch):
    from tests.manual_scripts.verification_scripts import verify_no_result_production_readiness as script

    monkeypatch.setattr(
        script,
        "verify_no_result_production_readiness",
        lambda: {"blockers": ["missing_evidence"]},
    )

    assert script.main() == 1
