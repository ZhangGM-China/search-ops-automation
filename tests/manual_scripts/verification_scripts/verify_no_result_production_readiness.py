import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parents[3]
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

os.environ.setdefault("ENV", "test")

from src.config import BRAND_CONFIGS, GLOBAL_SAFETY_CONFIG, get_env, is_brand_enabled
from src.persistence import (
    NoResultGovernanceAction,
    NoResultGovernanceBatch,
    NoResultQuery,
    NoResultRetagTask,
    SessionLocal,
    get_latest_task_session,
    get_recent_operation_metrics,
)


SUPPORTED_BRANDS = ["harbor", "riviera", "summit"]
PRODUCTION_ENDPOINT_MARKERS = ("kg.example.test", "search.example.test", "prod", "production")
REQUIRED_ACTION_TYPES = {"add_tag", "add_relationship"}
MUTATION_ACTION_TYPES = {
    "update_tag",
    "blacklist_tag",
    "unblacklist_tag",
    "update_relationship",
    "delete_relationship",
}


def _truthy(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes"}


def _has_text(value: str | None) -> bool:
    return bool(str(value or "").strip())


def _has_meaningful_json(value: str | None) -> bool:
    if not _has_text(value):
        return False
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return True
    return parsed not in ({}, [], None, "")


def _env_list(name: str) -> List[str]:
    return sorted({
        item.strip()
        for item in str(os.getenv(name) or "").split(",")
        if item.strip()
    })


def _collect_environment() -> Dict[str, Any]:
    return {
        "env": get_env(),
        "kgBaseUrl": os.getenv("KG_BASE_URL") or "",
        "noResultKgRealWrite": os.getenv("NO_RESULT_KG_REAL_WRITE") or "",
        "allowProductionKg": os.getenv("NO_RESULT_ALLOW_PRODUCTION_KG") or "",
        "kgApiTokenPresent": _has_text(os.getenv("KG_API_TOKEN")),
    }


def _collect_brand_decision() -> Dict[str, Any]:
    excluded = [brand for brand in _env_list("NO_RESULT_PRODUCTION_EXCLUDED_BRANDS") if brand in SUPPORTED_BRANDS]
    invalid = [brand for brand in _env_list("NO_RESULT_PRODUCTION_EXCLUDED_BRANDS") if brand not in SUPPORTED_BRANDS]
    still_enabled = []
    for brand in excluded:
        if bool(BRAND_CONFIGS.get(brand, {}).get("enabled")) and bool(is_brand_enabled(brand)):
            still_enabled.append(brand)
    return {
        "excludedBrands": excluded,
        "invalidExcludedBrands": invalid,
        "excludedBrandsStillEnabled": sorted(still_enabled),
    }


def _collect_write_authorization(environment: Dict[str, Any]) -> Dict[str, Any]:
    kg_base_url = str(environment["kgBaseUrl"])
    production_like_endpoint = any(marker in kg_base_url.lower() for marker in PRODUCTION_ENDPOINT_MARKERS)
    production_kg_allowed = _truthy(str(environment.get("allowProductionKg")))
    authorized = (
        environment["env"] == "test"
        and _truthy(str(environment["noResultKgRealWrite"]))
        and bool(environment["kgApiTokenPresent"])
        and _has_text(kg_base_url)
        and (not production_like_endpoint or production_kg_allowed)
    )
    missing = []
    if environment["env"] != "test":
        missing.append("ENV must be test for STG write readiness")
    if not _truthy(str(environment["noResultKgRealWrite"])):
        missing.append("NO_RESULT_KG_REAL_WRITE=true")
    if not environment["kgApiTokenPresent"]:
        missing.append("KG_API_TOKEN")
    if not _has_text(kg_base_url):
        missing.append("KG_BASE_URL")
    if production_like_endpoint and not production_kg_allowed:
        missing.append("KG_BASE_URL points to production KG without NO_RESULT_ALLOW_PRODUCTION_KG=true")
    return {
        "authorized": authorized,
        "missing": missing,
        "productionLikeEndpoint": production_like_endpoint,
        "productionKgExplicitlyAllowed": production_kg_allowed,
    }


def _collect_brand_coverage(db, brand_decision: Dict[str, Any]) -> Dict[str, Any]:
    brands = {}
    missing = []
    excluded_unverified = []
    excluded = set(brand_decision["excludedBrands"])
    excluded_still_enabled = set(brand_decision["excludedBrandsStillEnabled"])
    for brand in SUPPORTED_BRANDS:
        batch_count = db.query(NoResultGovernanceBatch).filter(NoResultGovernanceBatch.brand == brand).count()
        query_count = db.query(NoResultQuery).filter(NoResultQuery.brand == brand).count()
        latest_batch = db.query(NoResultGovernanceBatch).filter(
            NoResultGovernanceBatch.brand == brand
        ).order_by(NoResultGovernanceBatch.batch_date.desc(), NoResultGovernanceBatch.id.desc()).first()
        enabled = bool(BRAND_CONFIGS.get(brand, {}).get("enabled")) and bool(is_brand_enabled(brand))
        brands[brand] = {
            "enabled": enabled,
            "batchCount": batch_count,
            "queryCount": query_count,
            "latestBatchDate": latest_batch.batch_date.isoformat() if latest_batch and latest_batch.batch_date else None,
            "latestBatchStatus": latest_batch.status if latest_batch else None,
        }
        if brand in excluded and brand not in excluded_still_enabled:
            excluded_unverified.append(brand)
        elif brand in excluded_still_enabled:
            missing.append(brand)
        elif query_count == 0:
            missing.append(brand)
    return {"brands": brands, "missingBrands": missing, "excludedUnverifiedBrands": excluded_unverified}


def _collect_action_evidence(db, brand_decision: Dict[str, Any]) -> Dict[str, Any]:
    actions = db.query(NoResultGovernanceAction).all()
    successful = [action for action in actions if action.status == "success"]
    brands_covered = sorted({str(action.brand) for action in successful if action.brand})
    excluded = set(brand_decision["excludedBrands"]) - set(brand_decision["excludedBrandsStillEnabled"])
    required_brands = set(SUPPORTED_BRANDS) - excluded
    return {
        "totalActions": len(actions),
        "successfulActions": len(successful),
        "pendingExecuteActions": sum(1 for action in actions if action.status == "pending_execute"),
        "manualReviewActions": sum(1 for action in actions if action.status == "manual_review"),
        "failedActions": sum(1 for action in actions if action.status == "failed"),
        "actionTypesCovered": sorted({str(action.action_type) for action in successful if action.action_type}),
        "brandsCovered": brands_covered,
        "brandsMissingSuccessfulAction": sorted(required_brands - set(brands_covered)),
    }


def _collect_retag_review_evidence(db) -> Dict[str, Any]:
    return {
        "successfulRetagTasks": db.query(NoResultRetagTask).filter(NoResultRetagTask.status == "success").count(),
        "failedRetagTasks": db.query(NoResultRetagTask).filter(NoResultRetagTask.status == "failed").count(),
        "pendingRetagTasks": db.query(NoResultRetagTask).filter(NoResultRetagTask.status == "pending").count(),
        "completedReviewedQueries": db.query(NoResultQuery).filter(
            NoResultQuery.status == "completed",
            NoResultQuery.reviewed_at.isnot(None),
            NoResultQuery.after_result_count > 0,
        ).count(),
        "manualRequiredReviewedQueries": db.query(NoResultQuery).filter(
            NoResultQuery.status == "manual_required",
            NoResultQuery.reviewed_at.isnot(None),
        ).count(),
    }


def _collect_rollback_evidence(db) -> Dict[str, Any]:
    actions = db.query(NoResultGovernanceAction).all()
    return {
        "actionsWithBeforeSnapshot": sum(1 for action in actions if _has_meaningful_json(action.before_snapshot)),
        "actionsWithRollbackPayload": sum(1 for action in actions if _has_meaningful_json(action.rollback_payload)),
        "rollbackPendingActions": sum(1 for action in actions if action.rollback_status == "pending"),
        "rollbackSuccessActions": sum(1 for action in actions if action.rollback_status == "success"),
        "rollbackConflictActions": sum(1 for action in actions if action.rollback_status == "conflict"),
    }


def _collect_scheduler_health() -> Dict[str, Any]:
    health_session = get_latest_task_session("service_health_check_job")
    process_session = get_latest_task_session("no_result_governance_process_job")
    metrics = get_recent_operation_metrics(
        window_minutes=60,
        slow_threshold=float(GLOBAL_SAFETY_CONFIG.get("api_delay_threshold", 5)),
        modules=["OperationalService", "SummitService"],
    )
    return {
        "healthCheck": {
            "status": health_session.status if health_session else None,
            "phase": health_session.current_phase if health_session else None,
            "startTime": health_session.start_time.isoformat() if health_session and health_session.start_time else None,
            "endTime": health_session.end_time.isoformat() if health_session and health_session.end_time else None,
        },
        "noResultProcess": {
            "status": process_session.status if process_session else None,
            "phase": process_session.current_phase if process_session else None,
            "startTime": process_session.start_time.isoformat() if process_session and process_session.start_time else None,
            "endTime": process_session.end_time.isoformat() if process_session and process_session.end_time else None,
        },
        "operationMetrics": metrics,
    }


def _derive_blockers(report: Dict[str, Any]) -> List[str]:
    blockers = []
    if report["environment"]["env"] != "test":
        blockers.append("env_not_test")
    if not report["writeAuthorization"]["authorized"]:
        blockers.append("stg_kg_write_not_authorized")
    for brand in report["brandDecision"]["invalidExcludedBrands"]:
        blockers.append(f"invalid_excluded_brand:{brand}")
    for brand in report["brandDecision"]["excludedBrandsStillEnabled"]:
        blockers.append(f"excluded_brand_still_enabled:{brand}")
    for brand in report["brandCoverage"]["missingBrands"]:
        blockers.append(f"brand_not_verified:{brand}")
    if report["actionEvidence"]["successfulActions"] == 0:
        blockers.append("real_kg_action_success_missing")
    for brand in report["actionEvidence"]["brandsMissingSuccessfulAction"]:
        blockers.append(f"successful_action_missing_for_brand:{brand}")
    if report["retagReviewEvidence"]["successfulRetagTasks"] == 0:
        blockers.append("real_retag_success_missing")
    if report["retagReviewEvidence"]["completedReviewedQueries"] == 0:
        blockers.append("real_review_success_missing")
    if report["rollbackEvidence"]["actionsWithRollbackPayload"] == 0:
        blockers.append("rollback_payload_evidence_missing")
    return blockers


def _derive_warnings(report: Dict[str, Any]) -> List[str]:
    warnings = []
    for brand in report["brandCoverage"]["excludedUnverifiedBrands"]:
        warnings.append(f"production_excluded_brand:{brand}")
    action_types = set(report["actionEvidence"]["actionTypesCovered"])
    missing_action_types = sorted(REQUIRED_ACTION_TYPES - action_types)
    if missing_action_types:
        warnings.append(f"missing_action_type_coverage:{','.join(missing_action_types)}")
    if action_types.isdisjoint(MUTATION_ACTION_TYPES):
        warnings.append("missing_mutation_action_coverage")
    metrics = report["schedulerHealth"]["operationMetrics"]
    failure_threshold = float(GLOBAL_SAFETY_CONFIG.get("health_check_error_rate_threshold_percent", 20))
    p95_threshold = float(GLOBAL_SAFETY_CONFIG.get("health_check_api_p95_threshold_seconds", 5))
    if metrics["failure_rate_percent"] > failure_threshold:
        warnings.append(f"operation_failure_rate_high:{metrics['failure_rate_percent']}")
    if metrics["p95_duration_seconds"] > p95_threshold:
        warnings.append(f"operation_p95_high:{metrics['p95_duration_seconds']}")
    if report["schedulerHealth"]["healthCheck"]["status"] != "success":
        warnings.append("latest_health_check_not_success")
    if report["schedulerHealth"]["noResultProcess"]["status"] in {"running", "failed"}:
        warnings.append(f"latest_no_result_process_status:{report['schedulerHealth']['noResultProcess']['status']}")
    return warnings


def _derive_conclusion(blockers: List[str], warnings: List[str]) -> str:
    if blockers:
        return "仍不能上生产"
    if warnings:
        return "有条件可上生产"
    return "有条件可上生产"


def build_readiness_report() -> Dict[str, Any]:
    db = SessionLocal()
    try:
        report: Dict[str, Any] = {
            "environment": _collect_environment(),
            "writeAuthorization": {},
            "brandDecision": _collect_brand_decision(),
            "brandCoverage": {},
            "actionEvidence": {},
            "retagReviewEvidence": _collect_retag_review_evidence(db),
            "rollbackEvidence": _collect_rollback_evidence(db),
            "schedulerHealth": _collect_scheduler_health(),
            "blockers": [],
            "warnings": [],
            "conclusion": "",
        }
        report["writeAuthorization"] = _collect_write_authorization(report["environment"])
        report["brandCoverage"] = _collect_brand_coverage(db, report["brandDecision"])
        report["actionEvidence"] = _collect_action_evidence(db, report["brandDecision"])
        report["blockers"] = _derive_blockers(report)
        report["warnings"] = _derive_warnings(report)
        report["conclusion"] = _derive_conclusion(report["blockers"], report["warnings"])
        return report
    finally:
        db.close()


def verify_no_result_production_readiness() -> Dict[str, Any]:
    print("=== 无结果词治理生产上线准入验证 ===")
    report = build_readiness_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"=== 准入结论: {report['conclusion']} ===")
    if report["blockers"]:
        print("阻塞项:")
        for blocker in report["blockers"]:
            print(f"- {blocker}")
    if report["warnings"]:
        print("风险/待确认项:")
        for warning in report["warnings"]:
            print(f"- {warning}")
    return report


def main() -> int:
    report = verify_no_result_production_readiness()
    return 1 if report.get("blockers") else 0


if __name__ == "__main__":
    raise SystemExit(main())
