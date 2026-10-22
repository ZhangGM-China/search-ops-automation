import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _FakeLangfuse:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_relevance_langfuse_uses_environment_variables_before_launchctl(monkeypatch):
    module = _load_module(
        "benchmark_relevance_under_test",
        ROOT / "tests/manual_scripts/benchmark_relevance.py",
    )
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example.test")
    monkeypatch.setitem(sys.modules, "langfuse", types.SimpleNamespace(Langfuse=_FakeLangfuse))

    def launchctl_must_not_be_called(*args, **kwargs):
        raise AssertionError("launchctl must not be used when explicit env vars exist")

    monkeypatch.setattr(module.subprocess, "check_output", launchctl_must_not_be_called)
    client = module._langfuse_client()

    assert client is not None
    assert client.kwargs["public_key"] == "pk-test"
    assert client.kwargs["secret_key"] == "sk-test"
    assert client.kwargs["base_url"] == "https://langfuse.example.test"


def test_preference_langfuse_uses_environment_variables_before_launchctl(monkeypatch):
    module = _load_module(
        "benchmark_preference_generation_under_test",
        ROOT / "tests/manual_scripts/benchmark_preference_generation.py",
    )
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example.test")
    monkeypatch.setitem(sys.modules, "langfuse", types.SimpleNamespace(Langfuse=_FakeLangfuse))

    def launchctl_must_not_be_called(*args, **kwargs):
        raise AssertionError("launchctl must not be used when explicit env vars exist")

    monkeypatch.setattr(module.subprocess, "check_output", launchctl_must_not_be_called)
    client = module._langfuse_client()

    assert client is not None
    assert client.kwargs["public_key"] == "pk-test"
    assert client.kwargs["secret_key"] == "sk-test"
    assert client.kwargs["base_url"] == "https://langfuse.example.test"
