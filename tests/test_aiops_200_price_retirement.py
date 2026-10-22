"""Retirement contract for Query price-anomaly governance."""

from pathlib import Path

from src.query_governance.workflow import QueryGovernanceWorkflow


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_price_executor_and_legacy_route_are_retired():
    package = _source("src/top_query/__init__.py")
    executors = _source("src/top_query/executors.py")
    workflow = _source("src/top_query/workflow.py")

    assert "PriceExecutor" not in package
    assert "class PriceExecutor" not in executors
    assert "PriceExecutor" not in workflow
    assert 'run_executor(PriceExecutor' not in workflow
    assert QueryGovernanceWorkflow._RUNNER_KEYS == (
        "recall",
        "reorder",
        "relevance",
    )


def test_price_governance_configuration_is_removed_end_to_end():
    sources = {
        path: _source(path)
        for path in (
            "config.example.yaml",
            "src/config.py",
            "src/api/shared.py",
            "frontend/src/shared/types/index.ts",
            "frontend/src/features/config/pages/Config/components/BrandConfigSection.tsx",
        )
    }

    for source in sources.values():
        assert "top_query_price_exceed_percent" not in source
    assert "{ key: 'price', label: '价格偏高' }" not in sources[
        "frontend/src/features/config/pages/Config/components/BrandConfigSection.tsx"
    ]


def test_api_does_not_publish_price_governance_state_or_plans():
    contracts = _source("src/api/contracts.py")
    api = _source("src/api/application/query_governance_legacy.py")
    support = _source("src/api/application/query_governance_support.py")

    for token in (
        "priceExceedRatioThreshold",
        "priceDemoteSuggestedCount",
        "priceDemoteManualCount",
        "PriceExecutor",
        "价格偏高治理方案",
        "价格治理动作",
    ):
        assert token not in contracts
        assert token not in api
        assert token not in support


def test_frontend_has_no_price_governance_status_but_keeps_price_display():
    query_detail_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "frontend/src/features/query-governance").rglob("*.ts*")
        if not path.name.endswith(".test.ts")
        and not path.name.endswith(".test.tsx")
    )
    for token in (
        "PriceExecutor",
        "priceExceedRatioThreshold",
        "getProductPriceStatus",
        "价格偏高",
        "价格摘要",
        "价格高",
        "step.checkKey === 'price'",
    ):
        assert token not in query_detail_sources

    presentation = _source(
        "frontend/src/features/query-governance/components/QueryDetail/productPresentation.ts"
    )
    comparison = _source(
        "frontend/src/features/query-governance/components/QueryDetail/ProductComparison.tsx"
    )
    assert "getProductPriceDisplay" in presentation
    assert "getProductPriceNumber" in presentation
    assert "ops-current-product-card__price-line" in comparison


def test_s0_and_product_adapter_keep_original_and_discount_prices():
    snapshot = _source("src/query_governance/snapshot.py")
    adapter = _source("src/query_governance/product_adapter.py")

    for token in ("original_price", "discount_price"):
        assert token in snapshot
        assert token in adapter
