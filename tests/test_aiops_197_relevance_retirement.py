"""Retirement contract for the pre-rewrite relevance pipeline."""

from pathlib import Path

from src.query_governance.workflow import QueryGovernanceWorkflow
from src.top_query.kg_planner import TopQueryKnowledgeGraphPlanner


ROOT = Path(__file__).resolve().parents[1]
LEGACY_RUNTIME_TOKENS = (
    "IntentCheckAgent",
    "TextIntentAgent",
    "ImageIntentAgent",
    "RelevanceExecutor",
    "fix_relation",
    "top_query.relevance.image",
)


def _runtime_sources() -> list[Path]:
    return [
        *sorted((ROOT / "src").rglob("*.py")),
        *sorted(
            path
            for path in (ROOT / "frontend" / "src").rglob("*.ts")
            if not path.name.endswith(".test.ts")
        ),
        *sorted(
            path
            for path in (ROOT / "frontend" / "src").rglob("*.tsx")
            if not path.name.endswith(".test.tsx")
        ),
    ]


def _contract_sources() -> list[Path]:
    return [
        *_runtime_sources(),
        *sorted(
            path
            for path in (ROOT / "tests").rglob("*.py")
            if path.name != Path(__file__).name
        ),
    ]


def test_legacy_relevance_runtime_symbols_are_retired():
    matches = {
        token: [
            path.relative_to(ROOT).as_posix()
            for path in _contract_sources()
            if token in path.read_text(encoding="utf-8")
        ]
        for token in LEGACY_RUNTIME_TOKENS
    }

    assert matches == {token: [] for token in LEGACY_RUNTIME_TOKENS}


def test_relevance_stage_is_owned_by_the_new_deterministic_workflow():
    workflow_source = (
        ROOT / "src" / "query_governance" / "workflow.py"
    ).read_text(encoding="utf-8")

    assert QueryGovernanceWorkflow._RUNNER_KEYS == (
        "recall",
        "reorder",
        "relevance",
    )
    assert "src.top_query" not in workflow_source
    assert "IntentCheckAgent" not in workflow_source
    assert "RelevanceExecutor" not in workflow_source


def test_legacy_relevance_kg_intent_is_rejected_without_tool_calls():
    class FailingKnowledgeGraphClient:
        def execute(self, _payload):
            raise AssertionError("retired relevance intent must not call KG tools")

    actions, diagnostics = TopQueryKnowledgeGraphPlanner(
        kg_client=FailingKnowledgeGraphClient()
    ).plan(
        {"intent_type": "fix_" + "relation", "query": "黑色 连衣裙"},
        {"brand": "atlas", "query": "黑色 连衣裙"},
    )

    assert actions == []
    assert diagnostics["queryToolsCalled"] == 0
    assert diagnostics["skippedReasons"] == ["unsupported_legacy_intent"]
