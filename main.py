import json

from src.config import get_env
from src.persistence import engine
from src.migrations.release_schema import (
    validate_release_schema_verification,
    verify_release_schema,
)
from src.runtime.release_info import get_release_info, scheduler_config_fingerprint
from src.scheduler import scheduler_mode, start_scheduler, write_scheduler_status
from src.query_governance.composition import build_query_governance_workflow


def _log_scheduler_startup_failure(error_type: str) -> None:
    print(json.dumps({
        "event": "scheduler_startup_failed",
        "errorType": error_type,
    }, ensure_ascii=False, sort_keys=True))


def main() -> None:
    try:
        mode = scheduler_mode()
    except Exception:
        _log_scheduler_startup_failure("scheduler_mode_invalid")
        raise SystemExit(1) from None
    if get_env() == "prod":
        try:
            validate_release_schema_verification(verify_release_schema(engine))
        except Exception:
            _log_scheduler_startup_failure("schema_verification_failed")
            raise SystemExit(1) from None

    try:
        release_info = get_release_info()
    except Exception:
        _log_scheduler_startup_failure("release_identity_failed")
        raise SystemExit(1) from None

    startup_logged = False

    def log_scheduler_started() -> None:
        nonlocal startup_logged
        if startup_logged:
            return
        startup_logged = True
        print(json.dumps({
            "event": "scheduler_startup",
            **release_info,
        }, ensure_ascii=False, sort_keys=True))

    try:
        started = start_scheduler(
            singleton_name="main_scheduler",
            on_started=log_scheduler_started,
            release=release_info["releaseSha"],
            config_fingerprint=scheduler_config_fingerprint(release_info),
            query_governance_workflow_factory=build_query_governance_workflow,
            leader_wait_seconds=90,
        )
    except Exception:
        try:
            write_scheduler_status(ready=False, mode=mode, state="failed")
        except Exception:
            pass
        _log_scheduler_startup_failure("startup_exception")
        raise SystemExit(1) from None

    if started is not True:
        try:
            write_scheduler_status(ready=False, mode=mode, state="rejected")
        except Exception:
            pass
        _log_scheduler_startup_failure("startup_rejected")
        raise SystemExit(1)
    if not startup_logged:
        _log_scheduler_startup_failure("startup_not_confirmed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
