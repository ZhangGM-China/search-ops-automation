"""Runtime retirement contract for AIOPS-172.

The legacy implementation remains available as a read-only migration reference
until the new query_governance workflow owns the replacement behavior. It must
not be startable by the scheduler or operations CLI.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_scheduler_does_not_register_legacy_agent_loop_job():
    source = _source("src/scheduler/runner.py")
    assert 'id=\'agent_loop_governance_job\'' not in source
    assert 'fenced("agent_loop_governance_job", agent_loop_governance_job)' not in source


def test_top_query_cli_does_not_expose_legacy_run_or_resume_commands():
    source = _source("scripts/ops/top_query.py")
    assert '"run":' not in source
    assert '"resume":' not in source


def test_legacy_rerun_scripts_fail_closed_with_retirement_marker():
    for relative_path in (
        "scripts/ops/run_agent_loop_rerun.py",
        "scripts/ops/resume_agent_loop_rerun.py",
    ):
        source = _source(relative_path)
        assert "top_query_agent_loop_retired" in source


def test_scheduler_legacy_entrypoint_is_fail_closed():
    source = _source("src/scheduler/runner.py")
    assert "LegacyAgentLoopRetiredError" in source
    assert "raise LegacyAgentLoopRetiredError()" in source


def test_legacy_loop_state_and_configuration_are_not_exposed():
    constants = _source("src/top_query/constants.py")
    config = _source("src/config.py")
    api_shared = _source("src/api/shared.py")
    frontend_types = _source("frontend/src/shared/types/index.ts")
    frontend_config = _source(
        "frontend/src/features/config/pages/Config/components/BrandConfigSection.tsx"
    )
    for value in ("planning", "planner_reviewing", "evaluating"):
        assert f'"{value}"' not in constants
    for value in (
        "top_query_agent_max_rounds",
        "top_query_max_consecutive_planner_failures",
    ):
        assert value not in config
        assert value not in api_shared
        assert value not in frontend_types
        assert value not in frontend_config


def test_health_checks_do_not_depend_on_retired_agent_loop():
    lifecycle = _source("src/api/lifecycle.py")
    runner = _source("src/scheduler/runner.py")
    watchdog = _source("scripts/ops/runtime_watchdog.py")
    assert "get_agent_loop_run_freshness" not in lifecycle
    assert 'get_latest_task_session("agent_loop_governance_job")' not in lifecycle
    assert "get_agent_loop_run_freshness" not in runner
    assert 'get_latest_task_session("agent_loop_governance_job")' not in runner
    assert "agent_loop_governance_job" not in watchdog


def test_service_does_not_read_legacy_checkpoint_to_start_a_run():
    source = _source("src/top_query/service.py")
    assert "WorkflowCheckpointCodec.decode" not in source
    assert "active_checkpoint" not in source
