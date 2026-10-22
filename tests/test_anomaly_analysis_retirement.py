import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module_tree(relative_path: str) -> ast.Module:
    return ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))


def test_retired_anomaly_analysis_agent_is_absent_from_runtime_modules():
    agents_tree = _module_tree("src/llm/agents.py")

    agent_classes = {
        node.name for node in agents_tree.body if isinstance(node, ast.ClassDef)
    }

    assert "AnomalyAnalysisResult" not in agent_classes
    assert "AnomalyAnalysisAgent" not in agent_classes
    assert not (ROOT / "src/workflow.py").exists()
