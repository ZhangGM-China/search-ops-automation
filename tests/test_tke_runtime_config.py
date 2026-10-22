from copy import deepcopy

import pytest


def test_kubernetes_configmap_mode_reads_runtime_config_from_the_api(monkeypatch):
    import src.config as config_module

    monkeypatch.setenv("AIOPS_CONFIG_PERSISTENCE", "kubernetes_configmap")
    monkeypatch.setattr(
        config_module,
        "_kubernetes_configmap_request",
        lambda *, method, payload=None: {
            "data": {"config.yaml": "safety_config:\n  max_retry_count: 7\n"}
        },
        raising=False,
    )

    loaded = config_module.load_config()

    assert loaded["safety_config"]["max_retry_count"] == 7


def test_kubernetes_configmap_mode_persists_admin_changes_without_writing_container_filesystem(
    monkeypatch,
):
    import src.config as config_module

    captured = []
    original = deepcopy(config_module._raw_config)
    monkeypatch.setenv("AIOPS_CONFIG_PERSISTENCE", "kubernetes_configmap")

    def request(*, method, payload=None):
        captured.append((method, payload))
        return {"metadata": {"name": "aiops-runtime-config"}}

    monkeypatch.setattr(
        config_module,
        "_kubernetes_configmap_request",
        request,
        raising=False,
    )
    try:
        config_module.save_config({"safety_config": {"max_retry_count": 9}})

        assert captured[0][0] == "PATCH"
        assert captured[0][1] == {
            "data": {"config.yaml": "safety_config:\n  max_retry_count: 9\n"}
        }
        assert config_module._raw_config["safety_config"]["max_retry_count"] == 9
    finally:
        config_module._apply_config(original)


def test_unknown_runtime_config_persistence_mode_fails_closed(monkeypatch):
    import src.config as config_module

    monkeypatch.setenv("AIOPS_CONFIG_PERSISTENCE", "not-a-real-store")

    with pytest.raises(config_module.ConfigPersistenceError, match="persistence mode"):
        config_module.load_config()


def test_kubernetes_configmap_mode_rejects_missing_or_non_mapping_runtime_data(monkeypatch):
    import src.config as config_module

    monkeypatch.setenv("AIOPS_CONFIG_PERSISTENCE", "kubernetes_configmap")
    monkeypatch.setattr(
        config_module,
        "_kubernetes_configmap_request",
        lambda *, method, payload=None: {"data": {"config.yaml": "- not-a-mapping\n"}},
        raising=False,
    )

    with pytest.raises(config_module.ConfigPersistenceError, match="runtime config"):
        config_module.load_config()
