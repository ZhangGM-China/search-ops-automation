"""Contract tests for AIOPS-199 inventory retirement boundaries.

These tests intentionally use only the standard library so the retirement
boundary can be checked even when the full application test dependencies are
not installed.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _defined_names(relative_path: str) -> set[str]:
    tree = ast.parse(_source(relative_path), filename=relative_path)
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


class AIOPS199InventoryRetirementTests(unittest.TestCase):
    def test_legacy_inventory_agent_and_executor_are_removed(self) -> None:
        names = _defined_names("src/top_query/executors.py")
        self.assertNotIn("InventoryRiskAgent", names)
        self.assertNotIn("InventoryExecutor", names)
        workflow = _source("src/top_query/workflow.py")
        self.assertNotIn("InventoryRiskAgent", workflow)
        self.assertNotIn("InventoryExecutor", workflow)

    def test_old_inventory_scheduler_entrypoints_are_removed(self) -> None:
        names = _defined_names("src/scheduler/runner.py")
        for name in (
            "_run_broken_rate_monitor",
            "hourly_stock_monitor_job",
            "agent_loop_inventory_recovery_job",
            "broken_rate_monitor_job",
        ):
            self.assertNotIn(name, names)
        self.assertIn("inventory_monitor_job", names)

    def test_mainline_and_model_registry_do_not_reference_inventory_node(self) -> None:
        workflow = _source("src/query_governance/workflow.py")
        self.assertNotIn('"inventory"', workflow)
        registry = _source("src/llm/node_config.py")
        self.assertNotIn('"top_query.inventory_risk"', registry)

    def test_core_size_guard_is_removed_from_runtime_config_surfaces(self) -> None:
        forbidden = "top_query_inventory_core_size_guard_enabled"
        for relative_path in (
            "src/config.py",
            "src/api/shared.py",
            "frontend/src/shared/types/index.ts",
            "frontend/src/features/config/pages/Config/index.tsx",
            "frontend/src/features/config/pages/Config/components/BrandConfigSection.tsx",
        ):
            self.assertNotIn(forbidden, _source(relative_path), relative_path)

    def test_independent_monitor_configuration_remains_available(self) -> None:
        config = _source("src/config.py")
        self.assertIn("top_query_inventory_broken_rate_threshold", config)
        self.assertIn("top_query_inventory_check_interval_minutes", config)
        self.assertIn("top_query_inventory_max_cycles", config)


if __name__ == "__main__":
    unittest.main()
