#!/usr/bin/env python3
import datetime as dt
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / "logs"
DB_PATH = PROJECT_ROOT / "data" / "local_db" / "search_ops_automation_test.db"
REPORT_PATH = LOG_DIR / "runtime_watchdog_latest.json"
LOG_PATH = LOG_DIR / "runtime_watchdog.log"
API_LOG_PATH = LOG_DIR / "api.log"
SCHEDULER_LOG_PATH = LOG_DIR / "scheduler.log"
FRONTEND_LOG_PATH = LOG_DIR / "frontend.log"
PI_RUNTIME_LOG_PATH = LOG_DIR / "pi-runtime.log"

API_PORT = int(os.getenv("API_PORT", "8014"))
FRONTEND_PORT = int(os.getenv("FRONTEND_PORT", "5174"))
PI_RUNTIME_PORT = int(os.getenv("PI_RUNTIME_PORT", "8124"))
NODE_BIN = os.getenv("NODE_BIN") or shutil.which("node") or next(
    (
        candidate
        for candidate in ("/usr/local/bin/node", "/opt/homebrew/bin/node")
        if Path(candidate).is_file()
    ),
    "node",
)
HEALTH_URL = os.getenv("WATCHDOG_HEALTH_URL", f"http://127.0.0.1:{API_PORT}/api/health")
WINDOW_HOURS = int(os.getenv("WATCHDOG_WINDOW_HOURS", "3"))
ERROR_KEYWORDS = ("traceback", "error", "exception", "failed", "timeout")
KNOWN_WARNING_PATTERNS = (
    "获取Summit Token 失败, HTTP 403",
    "preprod.idp.summit.example.test",
    "cloudflare",
)
LOG_BASELINE_MARKERS = (
    "调度器启动成功",
    "Uvicorn running on",
    "VITE v",
    "PI SDK runtime listening on",
    "watchdog_log_baseline",
)
SERVICE_FAILURE_MODULES = ("OperationalService", "SummitService")
MOCK_TOP_QUERY_CUTOFF_DATE = dt.date(2090, 1, 1)
RUNTIME_LOG_MAX_BYTES = 5 * 1024 * 1024
RUNTIME_LOG_BACKUP_COUNT = 6


def now_local() -> dt.datetime:
    return dt.datetime.now()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def _parse_sqlite_dt(value: Any) -> dt.datetime | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def append_log(message: str) -> None:
    ensure_dirs()
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=RUNTIME_LOG_MAX_BYTES,
        backupCount=RUNTIME_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.emit(logging.makeLogRecord({"msg": f"[{iso_now()}] {message}"}))
    finally:
        handler.close()


def rotate_managed_log(
    path: Path,
    *,
    max_bytes: int = RUNTIME_LOG_MAX_BYTES,
    backup_count: int = RUNTIME_LOG_BACKUP_COUNT,
) -> None:
    """Bound a log that may still be open by a long-running local service."""
    if max_bytes < 1 or backup_count < 1 or not path.exists():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return

    for index in range(backup_count, 1, -1):
        previous = path.with_name(f"{path.name}.{index - 1}")
        destination = path.with_name(f"{path.name}.{index}")
        if previous.exists():
            try:
                previous.replace(destination)
            except OSError:
                continue

    backup = path.with_name(f"{path.name}.1")
    try:
        with path.open("rb") as source:
            source.seek(max(0, size - max_bytes))
            tail = source.read(max_bytes)
        with backup.open("wb") as target:
            target.write(tail)
        # Truncate in place so a running child process keeps a valid fd.
        with path.open("r+b") as current:
            current.truncate(0)
    except OSError:
        return


def rotate_managed_service_logs() -> None:
    for path in (API_LOG_PATH, SCHEDULER_LOG_PATH, FRONTEND_LOG_PATH, PI_RUNTIME_LOG_PATH):
        rotate_managed_log(path)


def write_report(payload: dict[str, Any]) -> None:
    ensure_dirs()
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_shell(cmd: list[str], stdout_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    stdout_handle = None
    stderr_target: Any = subprocess.PIPE
    stdout_target: Any = subprocess.PIPE
    if stdout_path is not None:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stdout_target = stdout_handle
        stderr_target = subprocess.STDOUT
    try:
        return subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            text=True,
            stdout=stdout_target,
            stderr=stderr_target,
            check=False,
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()


def spawn_background(cmd: list[str], stdout_path: Path) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    handle = stdout_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        handle.close()
    return {"attempted": True, "pid": process.pid}


def port_in_use(port: int) -> bool:
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return _port_in_use_via_socket(port)
    return result.returncode == 0 and bool(result.stdout.strip())


def _port_in_use_via_socket(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.3)
                if sock.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def process_matches(pattern: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-f", pattern],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def restart_api() -> dict[str, Any]:
    if port_in_use(API_PORT):
        return {"attempted": False, "reason": f"port_{API_PORT}_busy"}
    cmd = [
        str(PROJECT_ROOT / "venv" / "bin" / "python"),
        "-m",
        "uvicorn",
        "src.api:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(API_PORT),
        "--no-access-log",
    ]
    return spawn_background(cmd, stdout_path=API_LOG_PATH)


def restart_scheduler() -> dict[str, Any]:
    if process_matches(r"/python(\d+(\.\d+)*)? main\.py"):
        return {"attempted": False, "reason": "scheduler_already_running"}
    cmd = [str(PROJECT_ROOT / "venv" / "bin" / "python"), "main.py"]
    return spawn_background(cmd, stdout_path=SCHEDULER_LOG_PATH)


def restart_frontend() -> dict[str, Any]:
    if port_in_use(FRONTEND_PORT):
        return {"attempted": False, "reason": f"port_{FRONTEND_PORT}_busy"}
    cmd = [
        "env",
        f"FRONTEND_PORT={FRONTEND_PORT}",
        f"VITE_API_BASE=http://127.0.0.1:{API_PORT}/api",
        str(PROJECT_ROOT / "scripts" / "dev" / "start_query_governance_frontend.sh"),
    ]
    return spawn_background(cmd, stdout_path=FRONTEND_LOG_PATH)


def restart_pi_runtime() -> dict[str, Any]:
    if port_in_use(PI_RUNTIME_PORT):
        return {"attempted": False, "reason": f"port_{PI_RUNTIME_PORT}_busy"}
    cmd = [
        "env",
        f"PI_RUNTIME_PORT={PI_RUNTIME_PORT}",
        f"PI_AGENT_INTERNAL_TOKEN={os.getenv('PI_AGENT_INTERNAL_TOKEN', 'local-pi-runtime')}",
        f"KG_AGENT_MCP_URL={os.getenv('KG_AGENT_MCP_URL', 'https://mcphub.example.test/mcp/kg_agent_mcp')}",
        f"KG_AGENT_MCP_ENVIRONMENT={os.getenv('KG_AGENT_MCP_ENVIRONMENT', 'production')}",
        NODE_BIN,
        f"--env-file-if-exists={PROJECT_ROOT / '.env'}",
        str(PROJECT_ROOT / "pi-runtime" / "src" / "server.mjs"),
    ]
    return spawn_background(cmd, stdout_path=PI_RUNTIME_LOG_PATH)


def maybe_start_service(name: str) -> dict[str, Any]:
    if name == "api":
        return restart_api()
    if name == "scheduler":
        return restart_scheduler()
    if name == "frontend":
        return restart_frontend()
    if name == "pi_runtime":
        return restart_pi_runtime()
    raise ValueError(f"unsupported service: {name}")


def load_health() -> dict[str, Any]:
    try:
        response = requests.get(HEALTH_URL, timeout=8)
        return {
            "ok": response.status_code == 200,
            "statusCode": response.status_code,
            "payload": response.json() if response.headers.get("content-type", "").startswith("application/json") else None,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "payload": None}


def tail_error_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    baseline_index = -1
    for idx, line in enumerate(lines):
        if any(marker in line for marker in LOG_BASELINE_MARKERS):
            baseline_index = idx
    if baseline_index >= 0:
        lines = lines[baseline_index + 1 :]
    lines = lines[-limit:]
    return [
        line.strip() for line in lines
        if any(keyword in line.lower() for keyword in ERROR_KEYWORDS)
    ][:20]


def split_known_warnings(lines: list[str]) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    for line in lines:
        lowered = line.lower()
        if any(pattern.lower() in lowered for pattern in KNOWN_WARNING_PATTERNS):
            warnings.append(line)
        else:
            errors.append(line)
    return warnings, errors


def _extract_timestamped_baseline(line: str) -> dt.datetime | None:
    stripped = str(line or "").strip()
    if "watchdog_log_baseline" not in stripped:
        return None
    if not stripped.startswith("["):
        return None
    closing = stripped.find("]")
    if closing <= 1:
        return None
    return _parse_sqlite_dt(stripped[1:closing].strip())


def latest_watchdog_baseline_time(paths: list[Path] | None = None) -> dt.datetime | None:
    latest: dt.datetime | None = None
    for path in paths or [API_LOG_PATH, SCHEDULER_LOG_PATH, FRONTEND_LOG_PATH, PI_RUNTIME_LOG_PATH]:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            parsed = _extract_timestamped_baseline(line)
            if parsed is None:
                continue
            if latest is None or parsed > latest:
                latest = parsed
            break
    return latest


def query_db() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"exists": False}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        required_tables = {"task_run_session", "operation_log", "top_query_task"}
        available_tables = {
            str(row[0])
            for row in conn.execute("select name from sqlite_master where type = 'table'").fetchall()
        }
        if not required_tables.issubset(available_tables):
            return {
                "exists": True,
                "schemaReady": False,
                "latestHealthCheck": None,
                "recentFailedOperationSince": None,
                "recentFailedOperations": [],
                "recentFailedOperationCount": 0,
                "latestTopQueryDataDate": None,
                "latestTopQueryStatusBreakdown": [],
            }
        latest_health = conn.execute(
            """
            select id, task_name, status, current_phase, start_time, end_time, summary_data, error_log
            from task_run_session
            where task_name = 'service_health_check_job'
            order by id desc
            limit 1
            """
        ).fetchone()
        window_start = now_local() - dt.timedelta(hours=WINDOW_HOURS)
        latest_baseline = latest_watchdog_baseline_time()
        effective_failure_since = max(
            window_start,
            latest_baseline or window_start,
        )
        effective_failure_since_text = effective_failure_since.strftime("%Y-%m-%d %H:%M:%S")
        error_count = conn.execute(
            """
            select count(*) from operation_log
            where status = 'failed'
              and module in ({placeholders})
              and created_at >= ?
            """.format(placeholders=",".join("?" for _ in SERVICE_FAILURE_MODULES)),
            (*SERVICE_FAILURE_MODULES, effective_failure_since_text),
        ).fetchone()[0]
        recent_failures = conn.execute(
            """
            select created_at, module, action, substr(coalesce(error_message, ''), 1, 180) as error_message
            from operation_log
            where status = 'failed'
              and module in ({placeholders})
              and created_at >= ?
            order by id desc
            limit 10
            """.format(placeholders=",".join("?" for _ in SERVICE_FAILURE_MODULES)),
            (*SERVICE_FAILURE_MODULES, effective_failure_since_text),
        ).fetchall()
        latest_data_date = conn.execute(
            """
            select max(data_date) from top_query_task
            where data_date < ?
            """,
            (MOCK_TOP_QUERY_CUTOFF_DATE.isoformat(),),
        ).fetchone()[0]
        if not latest_data_date:
            latest_data_date = conn.execute("select max(data_date) from top_query_task").fetchone()[0]
        quality_rows = []
        if latest_data_date:
            quality_rows = conn.execute(
                """
                select brand, status, count(*) as task_count
                from top_query_task
                where data_date = ?
                group by brand, status
                order by brand, task_count desc
                """,
                (latest_data_date,),
            ).fetchall()
        return {
            "exists": True,
            "schemaReady": True,
            "latestHealthCheck": row_to_dict(latest_health),
            "recentFailedOperationSince": effective_failure_since_text,
            "recentFailedOperations": [row_to_dict(row) for row in recent_failures],
            "recentFailedOperationCount": int(error_count or 0),
            "latestTopQueryDataDate": latest_data_date,
            "latestTopQueryStatusBreakdown": [row_to_dict(row) for row in quality_rows],
        }
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    summary_data = payload.get("summary_data")
    if summary_data:
        try:
            payload["summary_data"] = json.loads(summary_data)
        except json.JSONDecodeError:
            pass
    return payload


def evaluate_services() -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    services = {
        "api": {
            "process": process_matches(rf"uvicorn src\.api:app .*--port {API_PORT}"),
            "portListening": port_in_use(API_PORT),
        },
        "scheduler": {
            "process": process_matches(r"python(\d+(\.\d+)*)?.* main\.py|Python main\.py"),
            "portListening": None,
        },
        "frontend": {
            "process": process_matches(rf"start_query_governance_frontend\.sh|vite .*--port {FRONTEND_PORT}|node .*vite --host 127\.0\.0\.1 --port {FRONTEND_PORT}"),
            "portListening": port_in_use(FRONTEND_PORT),
        },
        "pi_runtime": {
            "process": process_matches(r"pi-runtime/src/server\.mjs"),
            "portListening": port_in_use(PI_RUNTIME_PORT),
        },
    }
    issues: list[str] = []
    repairs: dict[str, Any] = {}
    for name, state in services.items():
        unhealthy = not state["process"] if name == "scheduler" else (not state["process"] or not state["portListening"])
        if unhealthy:
            issues.append(f"{name}_not_running")
            repairs[name] = maybe_start_service(name)
    return services, issues, repairs


def evaluate_quality(db_report: dict[str, Any], health_report: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    health_payload = health_report.get("payload") or {}
    if not health_report.get("ok"):
        issues.append("health_endpoint_unreachable")
    elif not health_payload.get("database", {}).get("connected"):
        issues.append("health_database_disconnected")
    elif health_payload.get("healthy") is False:
        issues.append("health_endpoint_unhealthy")

    if db_report.get("recentFailedOperationCount", 0) > 20:
        issues.append("recent_failed_operations_over_20")

    breakdown = db_report.get("latestTopQueryStatusBreakdown") or []
    completed = sum(row["task_count"] for row in breakdown if row.get("status") == "completed")
    failed = sum(row["task_count"] for row in breakdown if row.get("status") == "failed")
    if failed > 0:
        issues.append("latest_top_query_has_failed_tasks")
    if completed == 0 and breakdown:
        issues.append("latest_top_query_has_no_completed_tasks")
    return issues


def main() -> int:
    ensure_dirs()
    health_report = load_health()
    db_report = query_db()
    services, service_issues, repairs = evaluate_services()
    quality_issues = evaluate_quality(db_report, health_report)
    log_issues = {
        "api": tail_error_lines(API_LOG_PATH),
        "scheduler": tail_error_lines(SCHEDULER_LOG_PATH),
        "frontend": tail_error_lines(FRONTEND_LOG_PATH),
        "pi_runtime": tail_error_lines(PI_RUNTIME_LOG_PATH),
    }
    log_warnings = {name: [] for name in log_issues}
    filtered_log_issues = {}
    for name, lines in log_issues.items():
        warnings, errors = split_known_warnings(lines)
        log_warnings[name] = warnings
        filtered_log_issues[name] = errors
    rotate_managed_service_logs()
    all_issues = service_issues + quality_issues
    if any(filtered_log_issues.values()):
        all_issues.append("recent_log_errors_detected")

    report = {
        "checkedAt": iso_now(),
        "windowHours": WINDOW_HOURS,
        "health": health_report,
        "services": services,
        "repairs": repairs,
        "database": db_report,
        "logIssues": filtered_log_issues,
        "logWarnings": log_warnings,
        "issues": all_issues,
        "ok": not all_issues,
    }
    write_report(report)
    append_log(
        f"ok={report['ok']} issues={','.join(all_issues) if all_issues else 'none'} "
        f"repairs={json.dumps(repairs, ensure_ascii=False)}"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
