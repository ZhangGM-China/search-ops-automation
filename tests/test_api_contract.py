import json
import os
import re
import subprocess
import sys
from pathlib import Path

from src.api import app
from src.api.contracts import build_external_openapi

EXTERNAL_READ_PATHS = (
    "/api/health",
    "/api/dashboard/top-query/overview",
    "/api/dashboard/top-query/brands/{brand}/summary",
    "/api/dashboard/top-query/brands/{brand}/queries",
    "/api/dashboard/top-query/brands/{brand}/trend",
    "/api/dashboard/top-query/tasks",
    "/api/dashboard/top-query/queries/{task_id}",
    "/api/dashboard/top-query/queries/{task_id}/trend",
    "/api/dashboard/top-query/queries/{task_id}/current-result-count",
    "/api/dashboard/top-query/brands/{brand}/products/{product_id}",
    "/api/dashboard/top-query/brands/{brand}/snapshot/validate",
    "/api/dashboard/top-query/experiments/brands/{brand}",
    "/api/dashboard/top-query/experiments/brands/{brand}/sample-pool",
    "/api/dashboard/top-query/experiments/{experiment_id}",
    "/api/dashboard/top-query/experiments/{experiment_id}/report",
    "/api/dashboard/top-query/experiments/{experiment_id}/export",
    "/api/dashboard/no-result-governance",
    "/api/dashboard/no-result-governance/queries/{query_id}/execution",
)

RETIRED_EXTERNAL_READ_PATHS = {
    "/api/dashboard/top-query/queries/{task_id}/expert-ranking",
    "/api/dashboard/top-query/experiments/{experiment_id}/overview",
    "/api/dashboard/top-query/experiments/{experiment_id}/trend",
    "/api/dashboard/top-query/experiments/{experiment_id}/queries",
}

EXTERNAL_COMPUTE_OPERATIONS = (
    (
        "post",
        "/api/query-governance/relevance-checks",
        "202",
        "internal-job-state-only",
    ),
    (
        "get",
        "/api/query-governance/relevance-checks/{check_id}",
        "200",
        "read-only",
    ),
)


def _json_schema(operation):
    response = operation["responses"]["200"]
    content = response.get("content") or {}
    return next(iter(content.values())).get("schema") if content else None


def test_external_read_operations_have_stable_contracts():
    spec = app.openapi()
    operation_ids = []
    for path in EXTERNAL_READ_PATHS:
        operation = spec["paths"][path]["get"]
        operation_ids.append(operation["operationId"])
        schema = _json_schema(operation)
        assert schema and schema != {}, path
        assert operation.get("x-exposure") == "external-read"
        assert operation.get("security") == ([{"BearerAuth": []}] if path != "/api/health" else [])
    assert len(operation_ids) == len(set(operation_ids))


def test_external_contract_declares_client_visible_error_responses():
    spec = app.openapi()
    documented_errors = {"400", "401", "403", "404", "409", "422", "500"}

    for path in EXTERNAL_READ_PATHS:
        responses = spec["paths"][path]["get"]["responses"]
        expected = {"500"} if path == "/api/health" else documented_errors
        assert expected.issubset(responses), path
        if path == "/api/health":
            assert "503" not in responses


def test_external_compute_operations_have_stable_contracts():
    spec = app.openapi()
    for method, path, success_status, side_effects in EXTERNAL_COMPUTE_OPERATIONS:
        operation = spec["paths"][path][method]
        response = operation["responses"][success_status]
        schema = next(iter(response["content"].values()))["schema"]
        assert schema and schema != {}, path
        assert operation["x-exposure"] == "external-compute"
        assert operation["x-side-effects"] == side_effects
        assert operation["security"] == [{"BearerAuth": []}]


def test_api_inventory_marks_active_brand_detail_as_internal():
    inventory = Path("docs/TSD/api/internal-api-inventory.md").read_text(encoding="utf-8")
    row = next(
        line
        for line in inventory.splitlines()
        if "`/api/dashboard/top-query/brands/{brand}`" in line
    )

    assert "| internal |" in row


def test_ab_sample_pool_documentation_uses_sample_pool_path():
    document = Path("docs/TSD/api/09-A-B实验与导出接口.md").read_text(encoding="utf-8")

    assert "experiments/brands/atlas/sample-pool" in document


def test_external_contract_uses_only_actual_bearer_auth_scheme():
    security_schemes = app.openapi()["components"]["securitySchemes"]

    assert set(security_schemes) == {"BearerAuth"}
    assert security_schemes["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Issued JWT. External consumers receive tokens through a controlled account flow.",
    }


def test_external_contract_excludes_mutating_routes():
    spec = app.openapi()
    assert RETIRED_EXTERNAL_READ_PATHS.isdisjoint(spec["paths"])
    for path, methods in spec["paths"].items():
        for method in methods:
            operation = methods[method]
            if operation.get("x-exposure") == "external-read":
                assert method == "get"
                assert path not in {
                    "/api/admin/config",
                    "/api/dashboard/top-query/actions/{action_id}/execute",
                    "/api/dashboard/top-query/experiments/{experiment_id}/schedule",
                }


def test_contract_is_json_serializable():
    json.dumps(app.openapi(), ensure_ascii=False, sort_keys=True)


def test_checked_in_openapi_matches_runtime_contract():
    contract_path = Path("docs/TSD/api/openapi.json")
    assert contract_path.is_file()
    assert json.loads(contract_path.read_text(encoding="utf-8")) == build_external_openapi(app)


def test_release_gate_checks_openapi_drift():
    gate = Path("scripts/ci/test_release_gate.sh").read_text(encoding="utf-8")
    assert "scripts/ci/check_api_contract.py" in gate


def test_release_gate_checks_api_documentation_coverage():
    gate = Path("scripts/ci/test_release_gate.sh").read_text(encoding="utf-8")

    assert "scripts/ci/check_api_documentation.py" in gate


def test_external_openapi_and_inventory_exposure_sets_match():
    inventory = Path("docs/TSD/api/internal-api-inventory.md").read_text(encoding="utf-8")
    inventory_external = {
        (match.group(1), match.group(2), match.group(3))
        for match in re.finditer(
            r"^\| \d+ \| ([A-Z]+) `([^`]+)` \| `[^`]+` \| (external-(?:read|compute)) \|",
            inventory,
            re.MULTILINE,
        )
    }
    spec_external = {
        (method.upper(), path, operation["x-exposure"])
        for path, methods in app.openapi()["paths"].items()
        for method, operation in methods.items()
        if method in {"get", "post", "put", "patch", "delete"}
        and operation.get("x-exposure") in {"external-read", "external-compute"}
    }

    assert inventory_external == spec_external


def test_external_response_models_match_topic_contracts():
    schemas = app.openapi()["components"]["schemas"]

    assert {
        "query",
        "nodes",
        "s0",
        "actions",
        "governancePlans",
        "slots",
    } <= set(schemas["TaskDetailResponse"]["properties"])
    assert {
        "task",
        "plannerPlan",
        "stepStatuses",
        "inventoryBrokenRateThreshold",
        "trend",
        "teamTrace",
    }.isdisjoint(schemas["TaskDetailResponse"]["properties"])
    assert {"brand", "monitoringDate", "dataDate", "source", "local", "queryDiffs"} <= set(
        schemas["SnapshotValidationResponse"]["properties"]
    )
    assert {"brand", "current", "items", "total", "page", "pageSize"} <= set(
        schemas["ExperimentListResponse"]["properties"]
    )
    assert {"brand", "sampleDataDate", "items"} <= set(
        schemas["ExperimentSamplePoolResponse"]["properties"]
    )
    assert "total" not in schemas["ExperimentSamplePoolResponse"]["properties"]
    assert {"queryId", "status", "executedActions", "pendingActions", "actions"} <= set(
        schemas["NoResultExecutionResponse"]["properties"]
    )
    assert "progress" not in schemas["NoResultExecutionResponse"]["properties"]


def _run_contract_checker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/ci/check_api_contract.py", *args],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "ENV": "test",
            "AUTO_DB_URI_TEST": "sqlite:///:memory:",
            "OPENAI_API_KEY": "test-key",
        },
        text=True,
        capture_output=True,
        check=False,
    )


def _run_documentation_checker() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/ci/check_api_documentation.py"],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )


def test_api_contract_checker_accepts_checked_in_contract():
    result = _run_contract_checker()

    assert result.returncode == 0, result.stdout + result.stderr


def test_api_documentation_checker_accepts_registered_routes():
    result = _run_documentation_checker()

    assert result.returncode == 0, result.stdout + result.stderr
    assert re.fullmatch(r"API documentation check passed: \d+ routes covered\n", result.stdout)


def test_api_contract_checker_fails_closed_on_drift(tmp_path):
    drifted_contract = tmp_path / "openapi.json"
    drifted_contract.write_text("{}\n", encoding="utf-8")

    result = _run_contract_checker("--contract-path", str(drifted_contract))

    assert result.returncode != 0
    assert "drift" in (result.stdout + result.stderr).lower()
