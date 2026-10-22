import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "script_name",
    ("init_local_db.py", "seed_test_db.py"),
)
def test_local_database_scripts_reject_prod_before_import(tmp_path, script_name):
    would_be_prod_db = tmp_path / "would-be-prod.sqlite"
    env = {
        **os.environ,
        "ENV": "prod",
        "AUTO_DB_URI_PROD": f"sqlite:///{would_be_prod_db}",
        "INITIAL_ADMIN_PASSWORD": "test-password",
        "OPENAI_API_KEY": "test-key",
        "PYTHONPATH": str(PROJECT_ROOT),
    }

    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "db" / script_name)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 2
    assert f"blocked: {script_name} only runs with ENV=test" in completed.stderr
    assert not would_be_prod_db.exists()
