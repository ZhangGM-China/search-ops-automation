import importlib.util
import os
import socket
import sqlite3
import subprocess
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG_PATH = ROOT / "scripts" / "ops" / "runtime_watchdog.py"
FRONTEND_START_PATH = ROOT / "scripts" / "dev" / "start_query_governance_frontend.sh"


def _load_watchdog_module():
    spec = importlib.util.spec_from_file_location("runtime_watchdog_under_test", WATCHDOG_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load runtime_watchdog module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["runtime_watchdog_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_watchdog_runtime_log_rotation_has_a_fixed_disk_bound():
    watchdog = _load_watchdog_module()

    assert watchdog.RUNTIME_LOG_MAX_BYTES == 5 * 1024 * 1024
    assert watchdog.RUNTIME_LOG_BACKUP_COUNT == 6


def test_managed_service_logs_are_copy_truncated_with_bounded_backups(tmp_path):
    watchdog = _load_watchdog_module()
    log_path = tmp_path / "api.log"
    log_path.write_bytes(b"x" * (watchdog.RUNTIME_LOG_MAX_BYTES + 1))

    watchdog.rotate_managed_log(log_path)

    assert log_path.exists()
    assert log_path.stat().st_size == 0
    backup = Path(f"{log_path}.1")
    assert backup.exists()
    assert backup.stat().st_size <= watchdog.RUNTIME_LOG_MAX_BYTES


def _pick_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_frontend_start_honors_explicit_npm_bin_when_path_is_restricted(tmp_path):
    npm_args_path = tmp_path / "npm-args.txt"
    fake_npm_path = tmp_path / "npm"
    fake_npm_path.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$NPM_ARGS_PATH"\n',
        encoding="utf-8",
    )
    fake_npm_path.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "FRONTEND_PORT": str(_pick_unused_port()),
            "NPM_ARGS_PATH": str(npm_args_path),
            "NPM_BIN": str(fake_npm_path),
            "PATH": "/usr/bin:/bin",
        }
    )

    result = subprocess.run(
        [str(FRONTEND_START_PATH)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert npm_args_path.read_text(encoding="utf-8").splitlines() == [
        "run",
        "dev",
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        env["FRONTEND_PORT"],
        "--strictPort",
    ]


def test_port_in_use_falls_back_when_lsof_missing(monkeypatch):
    watchdog = _load_watchdog_module()
    port = _pick_unused_port()

    def raising_run(*args, **kwargs):
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(watchdog.subprocess, "run", raising_run)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        assert watchdog.port_in_use(port) is True

    assert watchdog.port_in_use(port) is False


def test_query_db_ignores_agent_fallback_failures(tmp_path, monkeypatch):
    watchdog = _load_watchdog_module()
    db_path = tmp_path / "watchdog.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            create table task_run_session (
                id integer primary key,
                start_time datetime,
                end_time datetime,
                task_name varchar(100),
                current_phase varchar(50),
                status varchar(20),
                summary_data text,
                error_log text
            );
            create table operation_log (
                id integer primary key,
                created_at datetime,
                module varchar(100),
                action varchar(100),
                status varchar(20),
                error_message text
            );
            create table top_query_task (
                id integer primary key,
                brand varchar(50),
                status varchar(20),
                data_date date
            );
            """
        )
        conn.executemany(
            "insert into operation_log (created_at, module, action, status, error_message) values (?, ?, ?, ?, ?)",
            [
                ("2099-01-01 00:00:00", "NoResultQueryIntentAgent", "No_Result_Query_Intent", "failed", "Request timed out."),
                ("2099-01-01 00:01:00", "NoResultDiagnosisAgent", "No_Result_Diagnosis", "failed", "Request timed out."),
                ("2099-01-01 00:02:00", "OperationalService", "POST list", "failed", "business error"),
            ],
        )
        conn.execute(
            "insert into top_query_task (brand, status, data_date) values (?, ?, ?)",
            ("atlas", "completed", "2099-01-01"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(watchdog, "DB_PATH", db_path)
    monkeypatch.setattr(watchdog, "WINDOW_HOURS", 999999)

    report = watchdog.query_db()

    assert report["recentFailedOperationCount"] == 1
    assert report["recentFailedOperations"] == [
        {
            "created_at": "2099-01-01 00:02:00",
            "module": "OperationalService",
            "action": "POST list",
            "error_message": "business error",
        }
    ]


def test_query_db_ignores_future_mock_top_query_dates(tmp_path, monkeypatch):
    watchdog = _load_watchdog_module()
    db_path = tmp_path / "watchdog.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            create table task_run_session (
                id integer primary key,
                start_time datetime,
                end_time datetime,
                task_name varchar(100),
                current_phase varchar(50),
                status varchar(20),
                summary_data text,
                error_log text
            );
            create table operation_log (
                id integer primary key,
                created_at datetime,
                module varchar(100),
                action varchar(100),
                status varchar(20),
                error_message text
            );
            create table top_query_task (
                id integer primary key,
                brand varchar(50),
                status varchar(20),
                data_date date
            );
            """
        )
        conn.executemany(
            "insert into top_query_task (brand, status, data_date) values (?, ?, ?)",
            [
                ("atlas", "completed", "2026-07-03"),
                ("atlas", "completed", "2099-01-01"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(watchdog, "DB_PATH", db_path)

    report = watchdog.query_db()

    assert report["latestTopQueryDataDate"] == "2026-07-03"
    assert report["latestTopQueryStatusBreakdown"] == [
        {
            "brand": "atlas",
            "status": "completed",
            "task_count": 1,
        }
    ]


def test_query_db_scopes_service_failures_to_latest_watchdog_baseline_when_no_loop_running(tmp_path, monkeypatch):
    watchdog = _load_watchdog_module()
    db_path = tmp_path / "watchdog.db"
    now = dt.datetime.now().replace(microsecond=0)
    old_failure = (now - dt.timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    baseline = (now - dt.timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    new_failure = (now - dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            create table task_run_session (
                id integer primary key,
                start_time datetime,
                end_time datetime,
                task_name varchar(100),
                current_phase varchar(50),
                status varchar(20),
                summary_data text,
                error_log text
            );
            create table operation_log (
                id integer primary key,
                created_at datetime,
                module varchar(100),
                action varchar(100),
                status varchar(20),
                error_message text
            );
            create table top_query_task (
                id integer primary key,
                brand varchar(50),
                status varchar(20),
                data_date date
            );
            """
        )
        conn.executemany(
            "insert into operation_log (created_at, module, action, status, error_message) values (?, ?, ?, ?, ?)",
            [
                (old_failure, "OperationalService", "POST page", "failed", "old failure"),
                (new_failure, "OperationalService", "POST list", "failed", "current failure"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    api_log = tmp_path / "api.log"
    api_log.write_text(f"[{baseline}] watchdog_log_baseline\n", encoding="utf-8")

    monkeypatch.setattr(watchdog, "DB_PATH", db_path)
    monkeypatch.setattr(watchdog, "WINDOW_HOURS", 999999)
    monkeypatch.setattr(watchdog, "API_LOG_PATH", api_log)
    monkeypatch.setattr(watchdog, "SCHEDULER_LOG_PATH", tmp_path / "scheduler.log")
    monkeypatch.setattr(watchdog, "FRONTEND_LOG_PATH", tmp_path / "frontend.log")

    report = watchdog.query_db()

    assert report["recentFailedOperationSince"] == baseline
    assert report["recentFailedOperationCount"] == 1
    assert report["recentFailedOperations"] == [
        {
            "created_at": new_failure,
            "module": "OperationalService",
            "action": "POST list",
            "error_message": "current failure",
        }
    ]


def test_watchdog_defaults_match_the_local_assistant_runtime_ports():
    watchdog = _load_watchdog_module()

    assert watchdog.API_PORT == 8014
    assert watchdog.FRONTEND_PORT == 5174
    assert watchdog.PI_RUNTIME_PORT == 8124
    assert watchdog.PI_RUNTIME_LOG_PATH == watchdog.LOG_DIR / "pi-runtime.log"


def test_watchdog_restarts_pi_runtime_when_its_process_and_port_are_missing(monkeypatch):
    watchdog = _load_watchdog_module()
    repaired = []

    monkeypatch.setattr(
        watchdog,
        "process_matches",
        lambda pattern: "pi-runtime/src/server" not in pattern,
    )
    monkeypatch.setattr(
        watchdog,
        "port_in_use",
        lambda port: port != watchdog.PI_RUNTIME_PORT,
    )
    monkeypatch.setattr(
        watchdog,
        "maybe_start_service",
        lambda name: repaired.append(name) or {"attempted": True, "pid": 12345},
    )

    services, issues, repairs = watchdog.evaluate_services()

    assert services["pi_runtime"] == {
        "process": False,
        "portListening": False,
    }
    assert "pi_runtime_not_running" in issues
    assert repairs["pi_runtime"] == {"attempted": True, "pid": 12345}
    assert repaired == ["pi_runtime"]


def test_restart_pi_runtime_uses_project_env_and_managed_log(monkeypatch):
    watchdog = _load_watchdog_module()
    captured = {}

    monkeypatch.setattr(watchdog, "port_in_use", lambda _port: False)

    def fake_spawn(cmd, stdout_path):
        captured["cmd"] = cmd
        captured["stdout_path"] = stdout_path
        return {"attempted": True, "pid": 45678}

    monkeypatch.setattr(watchdog, "spawn_background", fake_spawn)

    result = watchdog.restart_pi_runtime()

    assert result == {"attempted": True, "pid": 45678}
    assert captured["cmd"] == [
        "env",
        f"PI_RUNTIME_PORT={watchdog.PI_RUNTIME_PORT}",
        "PI_AGENT_INTERNAL_TOKEN=local-pi-runtime",
        "KG_AGENT_MCP_URL=https://mcphub.example.test/mcp/kg_agent_mcp",
        "KG_AGENT_MCP_ENVIRONMENT=production",
        watchdog.NODE_BIN,
        f"--env-file-if-exists={watchdog.PROJECT_ROOT / '.env'}",
        str(watchdog.PROJECT_ROOT / "pi-runtime" / "src" / "server.mjs"),
    ]
    assert captured["stdout_path"] == watchdog.PI_RUNTIME_LOG_PATH


def test_query_db_fails_open_for_a_partially_initialized_local_database(tmp_path, monkeypatch):
    watchdog = _load_watchdog_module()
    db_path = tmp_path / "watchdog.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(watchdog, "DB_PATH", db_path)

    report = watchdog.query_db()

    assert report == {
        "exists": True,
        "schemaReady": False,
        "latestHealthCheck": None,
        "recentFailedOperationSince": None,
        "recentFailedOperations": [],
        "recentFailedOperationCount": 0,
        "latestTopQueryDataDate": None,
        "latestTopQueryStatusBreakdown": [],
    }
