import datetime
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import event, text

from src.persistence import database as database_module
from src.persistence import (
    ActionLog,
    Base,
    OperationLog,
    SessionLocal,
    purge_expired_operation_logs,
    reset_engine_for_testing,
    save_operation_log,
)


def test_operation_log_payloads_are_redacted_and_bounded():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)
    try:
        save_operation_log({
            "brand": "atlas",
            "query": "连衣裙",
            "module": "OperationalService",
            "action": "POST mixed",
            "status": "success",
            "duration": 0.4,
            "request": '{"access_token":"secret-token","body":"' + "x" * 9000 + '"}',
            "response": "y" * 9000,
        })

        db = SessionLocal()
        try:
            log = db.query(OperationLog).one()
        finally:
            db.close()

        assert log.brand == "atlas"
        assert log.query_text == "连衣裙"
        assert log.module == "OperationalService"
        assert log.action == "POST mixed"
        assert log.status == "success"
        assert log.duration == 0.4
        assert "secret-token" not in log.request_payload
        assert "[REDACTED]" in log.request_payload
        assert len(log.request_payload) <= database_module.OPERATION_LOG_PAYLOAD_MAX_CHARS
        assert len(log.response_payload) <= database_module.OPERATION_LOG_PAYLOAD_MAX_CHARS
        assert "[truncated" in log.response_payload
    finally:
        Base.metadata.drop_all(bind=engine)


def test_successful_non_read_operation_log_drops_payloads_but_keeps_outcome_metadata():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)
    try:
        save_operation_log({
            "brand": "atlas",
            "query": "连衣裙",
            "module": "OperationalService",
            "action": "POST setOpsLowWeight",
            "status": "success",
            "duration": 0.4,
            "request": '{"productIds":["SKU-1"],"keyword":"连衣裙"}',
            "response": '{"code":200,"message":"ok"}',
        })

        db = SessionLocal()
        try:
            log = db.query(OperationLog).one()
        finally:
            db.close()

        assert log.status == "success"
        assert log.duration == 0.4
        assert log.request_payload is None
        assert log.response_payload is None
    finally:
        Base.metadata.drop_all(bind=engine)


def test_failed_operation_log_keeps_bounded_diagnostics_including_error():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)
    try:
        save_operation_log({
            "brand": "atlas",
            "query": "连衣裙",
            "module": "OperationalService",
            "action": "POST setOpsLowWeight",
            "status": "failed",
            "request": '{"authorization":"secret-token","productIds":["SKU-1"]}',
            "response": "r" * 9000,
            "error": "e" * 9000,
        })

        db = SessionLocal()
        try:
            log = db.query(OperationLog).one()
        finally:
            db.close()

        assert log.request_payload is not None
        assert "secret-token" not in log.request_payload
        assert log.response_payload is not None
        assert log.error_message is not None
        assert len(log.error_message) <= database_module.OPERATION_LOG_ERROR_MAX_CHARS
        assert "[truncated" in log.error_message
    finally:
        Base.metadata.drop_all(bind=engine)


def test_operation_log_retention_deletes_only_expired_rows_in_batches():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)
    now = datetime.datetime(2026, 3, 31, 12, 0, 0)
    db = SessionLocal()
    try:
        stale_logs = [
            OperationLog(created_at=datetime.datetime(2026, 2, 27, 23, 59), action=f"stale-{index}")
            for index in range(3)
        ]
        current_month_log = OperationLog(created_at=datetime.datetime(2026, 3, 1, 0, 0), action="current-month")
        rollback_evidence = ActionLog(created_at=datetime.datetime(2026, 2, 1), action_type=2)
        db.add_all([*stale_logs, current_month_log, rollback_evidence])
        db.commit()
        db.execute(text(
            "CREATE INDEX ix_operation_log_retention ON operation_log (created_at, id)"
        ))
        db.commit()
    finally:
        db.close()

    statements = []

    def capture_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with patch.dict(os.environ, {"ENV": "prod"}):
            assert purge_expired_operation_logs(now=now, batch_size=2) == 2
            assert purge_expired_operation_logs(now=now, batch_size=2) == 1
            assert purge_expired_operation_logs(now=now, batch_size=2) == 0
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    retention_selects = [
        statement
        for statement in statements
        if statement.startswith("select operation_log.id")
        and "where operation_log.created_at <" in statement
    ]
    assert retention_selects
    assert all(
        "order by operation_log.created_at asc, operation_log.id asc" in statement
        for statement in retention_selects
    )

    db = SessionLocal()
    try:
        assert [log.action for log in db.query(OperationLog).order_by(OperationLog.id)] == ["current-month"]
        assert db.query(ActionLog).count() == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_operation_log_retention_blocks_without_a_created_at_id_index():
    engine = reset_engine_for_testing()
    Base.metadata.create_all(bind=engine)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP INDEX ix_operation_log_retention_created_id"))
        with pytest.raises(database_module.OperationLogRetentionIndexMissing):
            purge_expired_operation_logs(now=datetime.datetime(2026, 3, 31, 12, 0, 0))
    finally:
        Base.metadata.drop_all(bind=engine)


def test_test_environment_operation_log_retention_uses_seven_days():
    now = datetime.datetime(2026, 8, 13, 18, 0, 0)

    with patch.dict(os.environ, {"ENV": "test"}):
        assert database_module._operation_log_retention_cutoff(now) == datetime.datetime(
            2026,
            8,
            6,
            18,
            0,
            0,
        )


def test_production_operation_log_retention_keeps_thirty_days_and_current_month():
    now = datetime.datetime(2026, 8, 31, 18, 0, 0)

    with patch.dict(os.environ, {"ENV": "prod"}):
        assert database_module._operation_log_retention_cutoff(now) == datetime.datetime(
            2026,
            8,
            1,
            0,
            0,
            0,
        )


def test_local_runtime_logs_are_kept_under_logs_directory():
    project_root = Path(__file__).resolve().parents[1]
    start_local = (project_root / "scripts" / "start_local.sh").read_text(encoding="utf-8")
    watchdog = (project_root / "scripts" / "ops" / "runtime_watchdog.py").read_text(
        encoding="utf-8"
    )

    assert 'LOG_DIR="$PROJECT_ROOT/logs"' in start_local
    assert 'API_PORT="${API_PORT:-8014}"' in start_local
    assert '> "$LOG_DIR/api.log" 2>&1 &' in start_local
    assert '> "$LOG_DIR/scheduler.log" 2>&1 &' in start_local
    assert '> "$LOG_DIR/frontend.log" 2>&1 &' in start_local
    assert '> "$LOG_DIR/pi-runtime.log" 2>&1 &' in start_local
    assert '--env-file-if-exists="$PROJECT_ROOT/.env"' in start_local
    assert 'PI_RUNTIME_PORT="$PI_RUNTIME_PORT"' in start_local
    assert 'LOG_DIR = PROJECT_ROOT / "logs"' in watchdog
    assert 'API_LOG_PATH = LOG_DIR / "api.log"' in watchdog
    assert 'SCHEDULER_LOG_PATH = LOG_DIR / "scheduler.log"' in watchdog
    assert 'FRONTEND_LOG_PATH = LOG_DIR / "frontend.log"' in watchdog
    assert 'PI_RUNTIME_LOG_PATH = LOG_DIR / "pi-runtime.log"' in watchdog


def test_runtime_watchdog_cron_does_not_append_to_an_unbounded_log_file():
    installer = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "ops"
        / "install_runtime_watchdog_cron.sh"
    ).read_text(encoding="utf-8")

    assert "runtime_watchdog_cron.log" not in installer
    assert "> /dev/null 2>&1" in installer
