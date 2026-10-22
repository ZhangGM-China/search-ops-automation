#!/usr/bin/env python
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
WATCHDOG_PATH = ROOT / "scripts" / "ops" / "runtime_watchdog.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("runtime_watchdog", WATCHDOG_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load runtime_watchdog module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["runtime_watchdog"] = module
    spec.loader.exec_module(module)
    return module


def verify_runtime_watchdog():
    watchdog = _load_module()
    captured_reports = []
    captured_logs = []

    print("=== 模块验证: 外部运行巡检 watchdog ===")

    with patch.object(watchdog, "write_report", side_effect=lambda payload: captured_reports.append(payload)), \
         patch.object(watchdog, "append_log", side_effect=lambda message: captured_logs.append(message)), \
         patch.object(watchdog, "load_health", return_value={"ok": True, "statusCode": 200, "payload": {"healthy": True, "database": {"connected": True}}}), \
         patch.object(
             watchdog,
             "query_db",
             return_value={
                 "exists": True,
                 "latestAgentLoop": {"status": "success", "start_time": "2026-07-04T00:30:00", "end_time": "2026-07-04T01:20:00"},
                 "latestHealthCheck": {"status": "success"},
                 "recentFailedOperationCount": 0,
                 "recentFailedOperations": [],
                 "latestTopQueryDataDate": "2026-07-03",
                 "latestTopQueryStatusBreakdown": [{"brand": "atlas", "status": "completed", "task_count": 3}],
             },
         ), \
         patch.object(
             watchdog,
             "evaluate_services",
             return_value=(
                 {
                     "api": {"process": True, "portListening": True},
                     "scheduler": {"process": True, "portListening": None},
                     "frontend": {"process": True, "portListening": True},
                     "pi_runtime": {"process": True, "portListening": True},
                 },
                 [],
                 {},
             ),
         ), \
         patch.object(watchdog, "tail_error_lines", return_value=[]):
        result = watchdog.main()
        if result != 0:
            raise AssertionError("健康路径应返回 0")
        if not captured_reports[-1]["ok"]:
            raise AssertionError("健康路径报告应为 ok=true")
    print("✅ 健康路径正常。")

    with patch.object(watchdog, "write_report", side_effect=lambda payload: captured_reports.append(payload)), \
         patch.object(watchdog, "append_log", side_effect=lambda message: captured_logs.append(message)), \
         patch.object(watchdog, "load_health", return_value={"ok": False, "error": "connection refused", "payload": None}), \
         patch.object(
             watchdog,
             "query_db",
             return_value={
                 "exists": True,
                 "latestAgentLoop": {"status": "failed", "start_time": "2026-07-04T00:30:00", "end_time": "2026-07-04T01:20:00"},
                 "latestHealthCheck": {"status": "success"},
                 "recentFailedOperationCount": 25,
                 "recentFailedOperations": [{"module": "OperationalService", "action": "POST mixed"}],
                 "latestTopQueryDataDate": "2026-07-03",
                 "latestTopQueryStatusBreakdown": [{"brand": "atlas", "status": "failed", "task_count": 2}],
             },
         ), \
         patch.object(
             watchdog,
             "evaluate_services",
             return_value=(
                 {
                     "api": {"process": False, "portListening": False},
                     "scheduler": {"process": True, "portListening": None},
                     "frontend": {"process": True, "portListening": True},
                     "pi_runtime": {"process": True, "portListening": True},
                 },
                 ["api_not_running"],
                 {"api": {"attempted": True, "pid": 12345}},
             ),
         ), \
         patch.object(watchdog, "tail_error_lines", side_effect=[["ERROR api down"], [], [], []]):
        result = watchdog.main()
        if result != 1:
            raise AssertionError("异常路径应返回 1")
        issues = captured_reports[-1]["issues"]
        expected = {
            "api_not_running",
            "health_endpoint_unreachable",
            "recent_failed_operations_over_20",
            "latest_top_query_has_failed_tasks",
            "latest_top_query_has_no_completed_tasks",
            "recent_log_errors_detected",
        }
        if not expected.issubset(set(issues)):
            raise AssertionError(f"异常路径 issues 不完整: {json.dumps(issues, ensure_ascii=False)}")
    print("✅ 异常路径正常。")
    print("=== 外部运行巡检 watchdog 验证完成 ===")


if __name__ == "__main__":
    verify_runtime_watchdog()
