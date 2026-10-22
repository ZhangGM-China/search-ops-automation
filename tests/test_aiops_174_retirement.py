"""Retirement contract for AIOPS-174.

These checks deliberately inspect the new runtime boundary instead of trying to
preserve the legacy Ops Team and score implementation through compatibility code.
"""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


def test_score_and_ops_models_are_retired_from_persistence():
    source = _source("src/persistence/models.py")
    tree = ast.parse(source)
    model_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }

    assert "TopQueryScore" not in model_names
    assert {
        "OpsTeamRun",
        "OpsTeamMessage",
        "OpsExpertRanking",
        "OpsAgentPromptVersion",
        "OpsAgentDecisionAudit",
        "OpsIntelligenceSnapshot",
        "OpsNotificationRecord",
    }.isdisjoint(model_names)
    assert "top_query_score" not in source
    assert "ops_team_" not in source


def test_top_query_task_has_no_ops_team_or_score_fields():
    source = _source("src/persistence/models.py")
    task_source = source.split("class TopQueryTask", 1)[1].split("class TopQueryStepStatus", 1)[0]
    for field in ("ops_team_run_id", "ops_team_status", "ops_priority_mode", "ops_generation"):
        assert field not in task_source


def test_schema_has_no_retired_score_or_ops_tables():
    source = _source("src/persistence/schema.py")
    for token in (
        "top_query_score",
        "ops_team_run",
        "ops_team_message",
        "ops_expert_ranking",
        "ops_agent_prompt_version",
        "ops_agent_decision_audit",
        "ops_intelligence_snapshot",
        "ops_notification_record",
    ):
        assert token not in source


def test_top_query_api_exposes_no_ops_team_routes_or_score_summary():
    source = _source("src/api/application/query_governance_legacy.py")
    assert "OpsTeamService" not in source
    assert "/ops-team/" not in source
    for field in ("healthyCount", "averageScore", "governanceScore", "opsTeamRunId", "expertRankings"):
        assert f'"{field}"' not in source


def test_top_query_support_does_not_read_retired_scores_or_health_summary():
    source = _source("src/api/application/query_governance_support.py")
    assert "TopQueryScore" not in source
    assert "top_query_score" not in source
    assert "_is_top_query_healthy" not in source


def test_legacy_ops_modules_and_frontend_surfaces_are_removed():
    retired_paths = (
        "src/top_query/ops_team.py",
        "src/top_query/ops_ranking.py",
        "src/top_query/ops_chat.py",
        "src/top_query/ops_intelligence.py",
        "src/top_query/ops_prompts.py",
        "frontend/src/features/query-governance/components/OpsTeamTraceGraph.tsx",
        "frontend/src/features/query-governance/components/OpsRankingComparison.tsx",
        "frontend/src/features/query-governance/components/OpsExpertChatPanel.tsx",
        "frontend/src/features/query-governance/components/OpsPromptDrawer.tsx",
        "frontend/src/features/query-governance/components/QueryDetail/HealthEvaluation.tsx",
        "src/top_query/evaluator.py",
    )
    assert [path for path in retired_paths if (ROOT / path).exists()] == []


def test_top_query_scheduler_has_no_ops_team_exports():
    source = _source("src/scheduler/runner.py")
    for token in ("OpsTeam", "ops_team", "expert_ranking", "ExpertRanking"):
        assert token not in source


def test_evaluator_and_health_score_are_not_runtime_dependencies():
    workflow = _source("src/top_query/workflow.py")
    node_config = _source("src/llm/node_config.py")
    for token in (
        "append_top_query_score",
        "from .evaluator import",
        "EvaluatorAgent",
        "health_evaluated",
        "top_query.evaluator",
    ):
        assert token not in workflow
    assert "top_query.evaluator" not in node_config


def test_formal_rebuild_plan_lists_all_retired_score_and_ops_tables():
    plan = _source("docs/Query 治理重构计划.md")
    for table_name in (
        "top_query_budget_account",
        "top_query_budget_ledger",
        "top_query_score",
        "ops_team_*",
        "ops_expert_ranking",
        "ops_agent_prompt_version",
        "ops_agent_decision_audit",
        "ops_intelligence_snapshot",
        "ops_visual_review_cache",
        "agent_experience_entry",
        "ops_notification_record",
    ):
        assert table_name in plan


def test_top_query_frontend_has_no_retired_health_or_ranking_entrypoints():
    frontend_sources = "\n".join(
        path.read_text()
        for path in (ROOT / "frontend/src").rglob("*.ts*")
        if "node_modules" not in path.parts
    )
    for token in (
        "OpsTeamTraceGraph",
        "OpsRankingComparison",
        "OpsPromptDrawer",
        "HealthEvaluation",
        "governanceScore",
        "healthyCount",
        "averageScore",
        "expertRankings",
        "opsTeamRunId",
    ):
        assert token not in frontend_sources


def test_ab_test_and_inventory_recovery_surfaces_remain_present():
    assert (ROOT / "src/top_query/experiment_service.py").exists()
    assert (ROOT / "src/persistence/models.py").read_text().find("class InventoryRecoveryTask") >= 0
