import asyncio
import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from importlib import import_module
api_module = import_module("src.api.app")
lifecycle_module = import_module("src.api.lifecycle")
from src.config import get_env


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_get_env_rejects_unknown_runtime_environment(monkeypatch):
    monkeypatch.setenv("ENV", "staging")

    with pytest.raises(ValueError, match="ENV must be one of: prod, test"):
        get_env()


def test_api_uses_lifespan_instead_of_deprecated_startup_events():
    source = inspect.getsource(api_module)

    assert ".on_event(" not in source
    assert "lifespan=" in source
    assert api_module.app.router.on_startup == []


def test_api_has_no_direct_execution_compatibility_entry():
    source = inspect.getsource(api_module)

    assert "import uvicorn" not in source
    assert '__name__ == "__main__"' not in source


def test_api_lifespan_initializes_test_database():
    async def exercise_lifespan():
        with patch.object(lifecycle_module, "get_env", return_value="test"), patch.object(
            lifecycle_module, "init_db"
        ) as mock_init_db:
            async with api_module.api_lifespan(api_module.app):
                pass
        mock_init_db.assert_called_once_with()

    asyncio.run(exercise_lifespan())


def test_api_lifespan_verifies_production_schema_without_initializing():
    async def exercise_lifespan():
        verification = {"schemaRelease": "2026-08-05"}
        with patch.object(lifecycle_module, "get_env", return_value="prod"), patch.object(
            lifecycle_module, "init_db"
        ) as mock_init_db, patch.object(
            lifecycle_module, "verify_release_schema", return_value=verification
        ) as mock_verify, patch.object(
            lifecycle_module, "validate_release_schema_verification"
        ) as mock_validate:
            async with api_module.api_lifespan(api_module.app):
                pass

        mock_init_db.assert_not_called()
        mock_verify.assert_called_once_with(lifecycle_module.engine)
        mock_validate.assert_called_once_with(verification)

    asyncio.run(exercise_lifespan())


def test_api_lifespan_fails_closed_when_production_schema_verification_fails():
    async def exercise_lifespan():
        with patch.object(lifecycle_module, "get_env", return_value="prod"), patch.object(
            lifecycle_module, "verify_release_schema", side_effect=RuntimeError("schema drift")
        ):
            with pytest.raises(
                RuntimeError,
                match="production release schema verification failed",
            ):
                async with api_module.api_lifespan(api_module.app):
                    pass

    asyncio.run(exercise_lifespan())


def test_readme_separates_local_startup_from_the_single_production_entrypoint():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "nohup" not in readme
    assert "from src.persistence import init_db" not in readme
    assert "ENV=test ./venv/bin/python -m uvicorn src.api:app" in readme
    assert "ENV=test ./venv/bin/python main.py" in readme
    assert 'python3 scripts/release/release.py --state-dir "$STATE_DIR" tke-candidate' in readme
    assert "python3 scripts/release/release.py ship" not in readme
    assert "docs/production_deployment_guide.md" in readme


def test_runtime_environment_has_one_authoritative_source():
    config_yaml = (PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8")

    assert "  environment:" not in config_yaml
