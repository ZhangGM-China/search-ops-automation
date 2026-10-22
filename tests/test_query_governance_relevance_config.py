from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from src.query_governance.relevance_config import (
    RelevanceRuntimeConfig,
    RelevanceConcurrencyController,
    get_relevance_runtime_config,
)


def test_relevance_runtime_config_has_planned_defaults_and_reads_brand_overrides(monkeypatch):
    assert RelevanceRuntimeConfig.from_mapping({}) == RelevanceRuntimeConfig(
        multimodal_concurrency=5,
        batch_size=240,
        request_max_bytes=20 * 1024 * 1024,
        model_timeout_seconds=1200,
        image_download_timeout_seconds=30,
        max_completion_tokens=None,
    )

    monkeypatch.setattr(
        "src.query_governance.relevance_config.get_brand_business_config",
        lambda _brand: {
            "query_governance_relevance_multimodal_concurrency": 3,
            "query_governance_relevance_batch_size": 24,
            "query_governance_relevance_model_timeout_seconds": 30,
            "query_governance_relevance_image_download_timeout_seconds": 9,
        },
    )
    assert get_relevance_runtime_config("atlas") == RelevanceRuntimeConfig(
        multimodal_concurrency=3,
        batch_size=24,
        request_max_bytes=20 * 1024 * 1024,
        model_timeout_seconds=30,
        image_download_timeout_seconds=9,
        max_completion_tokens=None,
    )


@pytest.mark.parametrize(
    "field",
    [
        "query_governance_relevance_multimodal_concurrency",
        "query_governance_relevance_batch_size",
        "query_governance_relevance_model_timeout_seconds",
        "query_governance_relevance_image_download_timeout_seconds",
    ],
)
def test_relevance_runtime_config_requires_positive_integers(field):
    with pytest.raises(ValueError, match="positive integer"):
        RelevanceRuntimeConfig.from_mapping({field: 0})
    with pytest.raises(ValueError, match="positive integer"):
        RelevanceRuntimeConfig.from_mapping({field: -1})
    with pytest.raises(ValueError, match="positive integer"):
        RelevanceRuntimeConfig.from_mapping({field: True})


def test_relevance_concurrency_controller_enforces_configured_limit():
    controller = RelevanceConcurrencyController(2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def work():
        nonlocal active, peak
        with controller:
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: work(), range(6)))

    assert peak == 2
