import ast
from pathlib import Path

from src.governance.write_entry_registry import PRODUCTION_WRITE_ENTRY_MATRIX


API_APPLICATION_DIR = Path(__file__).parents[1] / "src" / "api" / "application"


def _api_mutation_routes():
    routes = set()
    for path in sorted(API_APPLICATION_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                target = decorator.func
                if not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "router"
                    and target.attr in {"post", "put", "patch", "delete"}
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    continue
                routes.add((target.attr.upper(), decorator.args[0].value))
    return routes


def test_every_api_mutation_route_is_classified_in_production_write_matrix():
    registered = {(entry.method, entry.route) for entry in PRODUCTION_WRITE_ENTRY_MATRIX}

    assert registered == _api_mutation_routes()


def test_matrix_records_request_readback_and_recovery_contract_for_every_entry():
    required_text_fields = (
        "category",
        "gateway_status",
        "stable_request_id",
        "authoritative_readback",
        "rollback_or_manual",
        "disposition",
    )
    for entry in PRODUCTION_WRITE_ENTRY_MATRIX:
        for field in required_text_fields:
            assert str(getattr(entry, field) or "").strip(), (entry.route, field)
        if entry.gateway_status == "integrated":
            assert entry.stable_request_id != "none"
            assert entry.authoritative_readback != "none"
            assert entry.rollback_or_manual != "none"


def test_inventory_and_admin_entries_are_claimed_as_gateway_integrated():
    integrated = {
        (entry.method, entry.route)
        for entry in PRODUCTION_WRITE_ENTRY_MATRIX
        if entry.gateway_status == "integrated"
    }

    assert integrated == {
        ("POST", "/api/dashboard/stock-tasks/{task_id}/toggle-status"),
        ("POST", "/api/dashboard/actions/cancel-blacklist"),
        ("POST", "/api/dashboard/actions/cancel-blacklist/batch"),
        ("POST", "/api/dashboard/actions/blacklist"),
        ("POST", "/api/dashboard/actions/blacklist/batch"),
        ("POST", "/api/dashboard/actions/cancel-demotion"),
        ("POST", "/api/dashboard/top-query/actions/{action_id}/approve"),
        ("POST", "/api/dashboard/top-query/actions/{action_id}/reject"),
        ("POST", "/api/dashboard/top-query/tasks/{task_id}/mark-processed"),
        ("POST", "/api/dashboard/no-result-governance/actions/{action_id}/reject"),
        ("POST", "/api/dashboard/no-result-governance/actions/{action_id}/execute"),
        ("POST", "/api/dashboard/no-result-governance/actions/{action_id}/approve"),
        ("POST", "/api/dashboard/no-result-governance/batches/{batch_id}/rollback"),
        ("POST", "/api/dashboard/no-result-governance/queries/{query_id}/rollback"),
        ("POST", "/api/dashboard/no-result-governance/brands/{brand}/dates/{date}/rollback"),
        ("POST", "/api/dashboard/no-result-governance/queries/{query_id}/execution/start"),
        ("POST", "/api/dashboard/no-result-governance/queries/{query_id}/execution/pause"),
        ("POST", "/api/dashboard/no-result-governance/queries/{query_id}/execution/resume"),
        ("POST", "/api/admin/users"),
        ("PUT", "/api/admin/users/{user_id}"),
        ("POST", "/api/admin/users/{user_id}/reset-password"),
        ("POST", "/api/admin/users/{user_id}/revoke-sessions"),
        ("DELETE", "/api/admin/users/{user_id}"),
        ("POST", "/api/admin/assistant/access-config"),
        ("POST", "/api/admin/config"),
        ("POST", "/api/admin/page-config"),
        ("POST", "/api/admin/node-model-config/drafts"),
        ("POST", "/api/admin/node-model-config/versions/{version}/publish"),
        ("POST", "/api/admin/node-model-config/rollback"),
        ("POST", "/api/dashboard/top-query/brands/{brand}/snapshot/rebuild"),
        ("POST", "/api/query-governance/actions/{action_id}/execute"),
        ("POST", "/api/query-governance/kg-plans/{plan_id}/execute"),
    }


def test_top_query_snapshot_rebuild_documents_its_gateway_contract():
    entries = {
        (entry.method, entry.route): entry
        for entry in PRODUCTION_WRITE_ENTRY_MATRIX
    }
    rebuild = entries[(
        "POST",
        "/api/dashboard/top-query/brands/{brand}/snapshot/rebuild",
    )]

    assert rebuild.gateway_status == "integrated"
    assert rebuild.stable_request_id == (
        "ActionIntent.idempotency_key from requestId/idempotencyKey/externalRequestId "
        "or deterministic brand/monitoring-date/limit facts"
    )
    assert rebuild.authoritative_readback == (
        "persisted active TopQueryTask IDs, brand, data date, and exact task count "
        "match the rebuild result before cache invalidation"
    )
    assert rebuild.rollback_or_manual == (
        "failed or inconclusive readback is terminal fail-closed; inspect the persisted "
        "snapshot before issuing a new governed request"
    )
    assert rebuild.disposition == (
        "ActionGateway persisted attempt with authoritative database readback, terminal "
        "replay, payload conflict protection, and post-success cache invalidation"
    )


def test_no_result_execution_controls_document_their_exact_gateway_contract():
    entries = {
        (entry.method, entry.route): entry
        for entry in PRODUCTION_WRITE_ENTRY_MATRIX
    }
    start = entries[(
        "POST",
        "/api/dashboard/no-result-governance/queries/{query_id}/execution/start",
    )]
    resume = entries[(
        "POST",
        "/api/dashboard/no-result-governance/queries/{query_id}/execution/resume",
    )]
    pause = entries[(
        "POST",
        "/api/dashboard/no-result-governance/queries/{query_id}/execution/pause",
    )]

    self_managed_contract = {
        "gateway_status": "integrated",
        "stable_request_id": "ActionIntent.idempotency_key from requestId/idempotencyKey/externalRequestId or deterministic request facts; propagated to NoResultGovernanceAction.externalRequestId",
        "authoritative_readback": "persisted query scope and gatewayRequestId marker plus every NoResultGovernanceAction state; pending/executing/execution_unknown is never success",
        "rollback_or_manual": "expired pending actions are fenced to manual_review; executing/execution_unknown external writes remain fail-closed for manual investigation",
        "disposition": "ActionGateway persisted attempt, stale-fence evidence, authoritative readback, and terminal replay",
    }
    pause_contract = {
        "gateway_status": "integrated",
        "stable_request_id": "ActionIntent.idempotency_key from requestId/idempotencyKey or deterministic request facts",
        "authoritative_readback": "same-transaction query execution_paused state plus operator/reason marker",
        "rollback_or_manual": "failed or inconclusive readback rolls back the local pause; otherwise resume or manual investigation",
        "disposition": "ActionGateway local transaction with authoritative readback and terminal replay",
    }

    for entry in (start, resume):
        assert entry.gateway_status == self_managed_contract["gateway_status"]
        assert entry.stable_request_id == self_managed_contract["stable_request_id"]
        assert entry.authoritative_readback == self_managed_contract["authoritative_readback"]
        assert entry.rollback_or_manual == self_managed_contract["rollback_or_manual"]
        assert entry.disposition == self_managed_contract["disposition"]
    assert pause.gateway_status == pause_contract["gateway_status"]
    assert pause.stable_request_id == pause_contract["stable_request_id"]
    assert pause.authoritative_readback == pause_contract["authoritative_readback"]
    assert pause.rollback_or_manual == pause_contract["rollback_or_manual"]
    assert pause.disposition == pause_contract["disposition"]
