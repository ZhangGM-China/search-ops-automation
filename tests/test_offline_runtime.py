import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFLINE_RUNTIME = PROJECT_ROOT / "tests" / "offline_runtime"
PYTHON_BIN = Path(sys.executable)


def _run_offline_script(source: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AIOPS_OFFLINE_TEST"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([
        str(OFFLINE_RUNTIME),
        str(PROJECT_ROOT),
        env.get("PYTHONPATH", ""),
    ])
    return subprocess.run(
        [str(PYTHON_BIN), "-c", source],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_offline_runtime_installs_before_application_imports_and_blocks_external_dns():
    result = _run_offline_script(
        """
import socket
if not getattr(socket.getaddrinfo, '_aiops_offline_guard', False):
    raise SystemExit('offline guard not installed')
try:
    socket.getaddrinfo('example.com', 443)
except RuntimeError as exc:
    assert 'OfflineNetworkBlockedError' in str(exc)
else:
    raise SystemExit('external DNS was not blocked')
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_offline_runtime_allows_loopback_connections():
    result = _run_offline_script(
        """
import socket
if not getattr(socket.socket.connect, '_aiops_offline_guard', False):
    raise SystemExit('offline guard not installed')
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(1)
client = socket.create_connection(listener.getsockname(), timeout=1)
server, _ = listener.accept()
client.close()
server.close()
listener.close()
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_explicit_test_database_uri_wins_over_dotenv_during_offline_runs():
    env = os.environ.copy()
    env["ENV"] = "test"
    env["AUTO_DB_URI_TEST"] = "sqlite:////tmp/aiops-offline-contract.db"
    result = subprocess.run(
        [
            str(PYTHON_BIN),
            "-c",
            "import os; import src.config; print(os.environ['AUTO_DB_URI_TEST'])",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sqlite:////tmp/aiops-offline-contract.db"


def test_offline_runtime_does_not_attempt_feishu_notification(monkeypatch):
    from src.runtime.notifier import send_feishu_notification

    monkeypatch.setenv("AIOPS_OFFLINE_TEST", "1")
    with patch("src.runtime.notifier.requests.post") as post:
        assert send_feishu_notification("offline verification") is False
    post.assert_not_called()


def test_offline_runtime_does_not_refresh_remote_brand_config(monkeypatch):
    from src.integrations.operational_service import OperationalService

    monkeypatch.setenv("AIOPS_OFFLINE_TEST", "1")
    OperationalService.brand_configs = {}
    with patch.object(OperationalService, "_call_api") as call_api:
        OperationalService()
    call_api.assert_not_called()
