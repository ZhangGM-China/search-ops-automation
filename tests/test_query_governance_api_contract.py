import datetime
import inspect

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.api import app
from src.api import get_current_user
from src.api.application import query_governance
from src.api.contracts import BrandOverviewResponse, BrandQueryListResponse, BrandSummaryResponse, TaskDetailResponse


def test_current_user_dependency_runs_outside_event_loop():
    assert not inspect.iscoroutinefunction(get_current_user)


def test_query_governance_contract_has_explicit_new_state_models():
    overview = BrandOverviewResponse.model_json_schema()
    summary = BrandSummaryResponse.model_json_schema()
    query_list = BrandQueryListResponse.model_json_schema()
    detail = TaskDetailResponse.model_json_schema()

    assert overview["properties"]["brands"]["items"]["$ref"].endswith("QueryGovernanceBrandOverview")
    assert any(
        item.get("$ref", "").endswith("QueryGovernanceBatch")
        for item in summary["properties"]["batch"].get("anyOf", [])
    )
    assert query_list["properties"]["queries"]["items"]["$ref"].endswith("QueryGovernanceQueryListItem")
    assert detail["properties"]["nodes"]["items"]["$ref"].endswith("QueryGovernanceNode")
    assert detail["properties"]["slots"]["$ref"].endswith("QueryGovernanceDetailSlots")
    assert detail["properties"]["productDisplayLimit"]["type"] == "integer"
    assert {
        "kgTasks", "sharedFacts", "preference", "relevance", "inventory", "modelConfig", "abTest",
    } == set(detail["$defs"]["QueryGovernanceDetailSlots"]["properties"])
    assert "governancePlans" in detail["properties"]
    assert "timeline" not in detail["properties"]
    assert {"plannerPlan", "inventoryBrokenRateThreshold"}.isdisjoint(detail["properties"])
    batch_telemetry = overview["$defs"]["QueryGovernanceBatchTelemetry"]
    assert "irrelevantQueryCount" in batch_telemetry["properties"]
    assert "topQueryLimit" in batch_telemetry["properties"]
    assert "rapidPvGrowthQueryCount" in batch_telemetry["properties"]


def test_rewritten_read_routes_replace_legacy_handlers_without_duplicate_paths():
    def _walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "original_router"):
                yield from _walk(route.original_router.routes)

    top_query_routes = [
        route
        for route in _walk(app.routes)
        if route.path.startswith("/api/dashboard/top-query/")
    ]
    route_keys = [
        (method, route.path)
        for route in top_query_routes
        for method in route.methods
    ]

    assert len(route_keys) == len(set(route_keys))
    handler = next(
        route.endpoint
        for route in top_query_routes
        if route.path == "/api/dashboard/top-query/queries/{task_id}"
        and "GET" in route.methods
    )
    assert handler.__module__ == "src.api.application.query_governance"


def test_structured_kg_execution_route_is_owned_by_query_governance():
    def _walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "original_router"):
                yield from _walk(route.original_router.routes)

    route = next(
        route
        for route in _walk(app.routes)
        if isinstance(route, APIRoute)
        and route.path == "/api/query-governance/actions/{action_id}/execute"
        and "POST" in route.methods
    )
    assert route.endpoint.__module__ == "src.api.application.query_governance"


def test_database_backed_query_governance_reads_run_in_fastapi_threadpool():
    handlers = (
        query_governance.get_query_governance_overview,
        query_governance.get_query_governance_brand_detail,
        query_governance.get_query_governance_brand_summary,
        query_governance.get_query_governance_brand_queries,
        query_governance.get_query_governance_brand_trend,
        query_governance.get_query_governance_tasks,
        query_governance.get_query_governance_detail,
    )

    assert all(not inspect.iscoroutinefunction(handler) for handler in handlers)


def test_api_responses_expose_server_timing_for_latency_diagnosis():
    response = TestClient(app).get("/api/health/live")

    assert response.status_code == 200
    assert response.headers["server-timing"].startswith("app;dur=")


def test_empty_selection_reason_reaches_brand_queries_route(monkeypatch):
    class FakeReadService:
        def brand_detail(self, brand, *, data_date):
            return {"batch": {"dataDate": "2026-09-07", "reason": "no_candidates"}, "queries": []}

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/brands/atlas/queries?date=2026-09-07")
        assert response.status_code == 200
        assert response.json()["reason"] == "no_candidates"
        assert response.json()["total"] == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_omitted_monitoring_date_defaults_to_server_today_and_reads_yesterday(monkeypatch):
    class FakeReadService:
        def overview(self, brands, *, data_date):
            assert data_date == datetime.date(2026, 9, 9)
            return {"brands": []}

    class FixedDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 10)

    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    monkeypatch.setattr(query_governance.datetime, "date", FixedDate)
    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/overview")
        assert response.status_code == 200
        assert response.json() == {"brands": []}
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_overview_maps_monitoring_date_to_previous_business_date(monkeypatch):
    class FakeReadService:
        def overview(self, brands, *, data_date):
            assert data_date == datetime.date(2026, 9, 13)
            return {"brands": []}

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/overview?date=2026-09-14")
        assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_overview_excludes_disabled_brands(monkeypatch):
    seen_brands = []

    class FakeReadService:
        def overview(self, brands, *, data_date):
            seen_brands.append(list(brands))
            return {"brands": []}

    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    monkeypatch.setattr(query_governance, "is_brand_enabled", lambda brand: brand != "meadow")
    user = type("UserStub", (), {"brand_permissions": '["atlas", "meadow"]', "is_admin": False})()
    app.dependency_overrides[get_current_user] = lambda: user
    query_governance._clear_top_query_endpoint_caches()
    try:
        response = TestClient(app).get("/api/dashboard/top-query/overview?date=2026-09-14")
        assert response.status_code == 200
        assert seen_brands == [["atlas"]]
    finally:
        query_governance._clear_top_query_endpoint_caches()
        app.dependency_overrides.pop(get_current_user, None)


def test_overview_reuses_short_ttl_cache_for_same_permissions_and_date(monkeypatch):
    calls = 0

    class FakeReadService:
        def overview(self, brands, *, data_date):
            nonlocal calls
            calls += 1
            return {"brands": [{"brand": brands[0], "dataDate": data_date.isoformat()}]}

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    query_governance._clear_top_query_endpoint_caches()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        first = client.get("/api/dashboard/top-query/overview?date=2026-09-14")
        second = client.get("/api/dashboard/top-query/overview?date=2026-09-14")
        assert first.status_code == 200
        assert second.json() == first.json()
        assert calls == 1
    finally:
        query_governance._clear_top_query_endpoint_caches()
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard/top-query/brands/atlas",
        "/api/dashboard/top-query/brands/atlas/summary",
        "/api/dashboard/top-query/brands/atlas/queries",
        "/api/dashboard/top-query/brands/atlas/trend",
    ],
)
def test_brand_reads_map_monitoring_date_to_previous_business_date(monkeypatch, path):
    class FakeReadService:
        def brand_detail(self, brand, *, data_date):
            assert brand == "atlas"
            assert data_date == datetime.date(2026, 9, 13)
            return {
                "brand": brand,
                "batch": {"dataDate": "2026-09-13", "reason": None},
                "summary": {},
                "queries": [],
                "trend": [],
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(f"{path}?date=2026-09-14")
        assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_brand_trend_route_returns_persisted_business_metrics(monkeypatch):
    class FakeReadService:
        def brand_detail(self, brand, *, data_date):
            return {
                "batch": {"dataDate": "2026-09-07"},
                "trend": [
                    {"date": "2026-08-10", "uv": 1000, "pv": 2000, "ctr": 0.2},
                    {"date": "2026-09-07", "uv": 1290, "pv": 2580, "ctr": 0.2},
                ],
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/brands/atlas/trend?date=2026-09-08")
        assert response.status_code == 200
        assert response.json()["dataDate"] == "2026-09-07"
        assert [point["date"] for point in response.json()["trend"]] == ["2026-08-10", "2026-09-07"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_brand_summary_and_trend_routes_use_lightweight_read_methods(monkeypatch):
    calls: list[str] = []

    class FakeReadService:
        def brand_summary(self, brand, *, data_date):
            calls.append(f"summary:{brand}:{data_date}")
            return {"brand": brand, "batch": None, "summary": {"queryCount": 0}, "stats": {}}

        def brand_trend(self, brand, *, data_date):
            calls.append(f"trend:{brand}:{data_date}")
            return {"brand": brand, "dataDate": "2026-09-07", "trend": []}

        def brand_detail(self, brand, *, data_date):
            raise AssertionError("full brand detail must not be used by overview reads")

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        assert client.get("/api/dashboard/top-query/brands/atlas/summary?date=2026-09-08").status_code == 200
        assert client.get("/api/dashboard/top-query/brands/atlas/trend?date=2026-09-08").status_code == 200
        assert calls == [
            "summary:atlas:2026-09-07",
            "trend:atlas:2026-09-07",
        ]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_brand_summary_reuses_short_ttl_cache_for_same_brand_and_date(monkeypatch):
    calls = 0

    class FakeReadService:
        def brand_summary(self, brand, *, data_date):
            nonlocal calls
            calls += 1
            return {
                "brand": brand,
                "batch": {"dataDate": data_date.isoformat()},
                "summary": {"queryCount": 10},
                "stats": {},
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    query_governance._clear_top_query_endpoint_caches()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        first = client.get("/api/dashboard/top-query/brands/atlas/summary?date=2026-09-14")
        second = client.get("/api/dashboard/top-query/brands/atlas/summary?date=2026-09-14")
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json()
        assert calls == 1
    finally:
        query_governance._clear_top_query_endpoint_caches()
        app.dependency_overrides.pop(get_current_user, None)


def test_brand_queries_route_uses_lightweight_brand_queries_read(monkeypatch):
    calls: list[str] = []

    class FakeReadService:
        def brand_queries(self, brand, *, data_date, page, page_size, filter_key):
            calls.append(f"queries:{brand}:{data_date}:{page}:{page_size}:{filter_key}")
            return {
                "brand": brand,
                "batch": {"dataDate": "2026-09-07", "reason": None},
                "queries": [{
                    "id": 1, "batchId": "batch-1", "brand": "atlas", "dataDate": "2026-09-07",
                    "queryText": "帽子", "status": "executed",
                }],
                "stats": {"topQueryCount": 1},
                "total": 1,
                "page": page,
                "pageSize": page_size,
                "hasNext": False,
            }

        def brand_detail(self, brand, *, data_date):
            raise AssertionError("full brand detail must not be used by Query list reads")

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(
            "/api/dashboard/top-query/brands/atlas/queries?date=2026-09-08&page=2&pageSize=30&filter=recall"
        )
        assert response.status_code == 200
        assert response.json()["queries"][0]["id"] == 1
        assert response.json()["page"] == 2
        assert response.json()["pageSize"] == 30
        assert response.json()["hasNext"] is False
        assert calls == ["queries:atlas:2026-09-07:2:30:recall"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_query_workspace_route_reads_detail_trend_and_events_once(monkeypatch):
    calls: list[int] = []

    class FakeReadService:
        def query_workspace(self, task_id, *, include_trend=True):
            assert include_trend is True
            calls.append(task_id)
            return {
                "query": {"brand": "atlas", "queryText": "帽子"},
                "trend": [{"date": "2026-09-05", "pv": 114}],
                "events": [{"id": 7}],
            }

        def query_detail(self, task_id):
            raise AssertionError("workspace route must not call query_detail directly")

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/queries/42/workspace")
        assert response.status_code == 200
        assert response.json()["trend"] == [{"date": "2026-09-05", "pv": 114}]
        assert response.json()["events"] == [{"id": 7}]
        assert calls == [42]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_query_workspace_route_omits_trend_when_disabled(monkeypatch):
    class FakeReadService:
        def query_workspace(self, task_id, *, include_trend=True):
            assert task_id == 42
            assert include_trend is False
            return {
                "query": {"brand": "atlas", "queryText": "帽子"},
                "trend": [],
                "events": [{"id": 7}],
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(
            "/api/dashboard/top-query/queries/42/workspace?includeTrend=false"
        )
        assert response.status_code == 200
        assert response.json()["trend"] == []
        assert response.json()["events"] == [{"id": 7}]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_brand_summary_route_exposes_query_issue_stats(monkeypatch):
    class FakeReadService:
        def brand_detail(self, brand, *, data_date):
            return {
                "batch": {"dataDate": "2026-09-13"},
                "summary": {"queryCount": 3},
                "stats": {
                    "topQueryCount": 3,
                    "stepIssueCounts": {"relevance": 1},
                    "relevanceIssueCount": 1,
                },
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/brands/atlas/summary?date=2026-09-13")
        assert response.status_code == 200
        assert response.json()["stats"]["stepIssueCounts"]["relevance"] == 1
        assert response.json()["stats"]["relevanceIssueCount"] == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_query_detail_handler_reads_rewritten_service_and_enforces_brand_permission(monkeypatch):
    class FakeReadService:
        def query_detail(self, task_id):
            return {
                "query": {"brand": "atlas"},
                "nodes": [],
                "s0": None,
                "actions": [],
                "slots": {},
            }

    user = type(
        "UserStub",
        (),
        {"brand_permissions": '["atlas"]', "is_admin": False},
    )()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/queries/42")
        assert response.status_code == 200
        assert response.json()["query"]["brand"] == "atlas"

        forbidden_user = type(
            "UserStub",
            (),
            {"brand_permissions": '["harbor"]', "is_admin": False},
        )()
        app.dependency_overrides[get_current_user] = lambda: forbidden_user
        forbidden = TestClient(app).get("/api/dashboard/top-query/queries/42")
        assert forbidden.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_query_detail_and_live_result_use_brand_product_display_limit(monkeypatch):
    observed_limits = []

    class FakeReadService:
        def query_detail(self, task_id):
            return {
                "query": {"brand": "atlas"},
                "nodes": [],
                "s0": None,
                "actions": [],
                "slots": {},
            }

        def live_result(self, task_id, *, limit):
            observed_limits.append(limit)
            return {
                "taskId": task_id,
                "status": "executed",
                "resultCount": 24,
                "resultCountKnown": True,
                "products": [],
                "errorCode": None,
                "errorMessage": None,
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    monkeypatch.setattr(
        query_governance,
        "get_brand_business_config",
        lambda brand: {"top_query_check_product_limit": 24},
    )
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        detail = TestClient(app).get("/api/dashboard/top-query/queries/42")
        assert detail.status_code == 200
        assert detail.json()["productDisplayLimit"] == 24

        live = TestClient(app).get("/api/dashboard/top-query/queries/42/current-result-count")
        assert live.status_code == 200
        assert live.json()["resultCount"] == 24
        assert live.json()["productDisplayLimit"] == 24
        assert observed_limits == [24]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_query_detail_and_live_result_stay_on_the_same_configured_limit(monkeypatch):
    observed_limits = []

    class FakeReadService:
        def query_detail(self, task_id):
            return {
                "query": {"brand": "atlas"},
                "nodes": [],
                "s0": None,
                "actions": [],
                "slots": {},
            }

        def live_result(self, task_id, *, limit):
            observed_limits.append(limit)
            return {
                "taskId": task_id,
                "status": "executed",
                "resultCount": 16,
                "resultCountKnown": True,
                "products": [{} for _ in range(limit)],
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    monkeypatch.setattr(
        query_governance,
        "get_brand_business_config",
        lambda brand: {"top_query_check_product_limit": 16},
    )
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        detail = TestClient(app).get("/api/dashboard/top-query/queries/42")
        live = TestClient(app).get("/api/dashboard/top-query/queries/42/current-result-count")

        assert detail.status_code == live.status_code == 200
        assert detail.json()["productDisplayLimit"] == 16
        assert live.json()["productDisplayLimit"] == 16
        assert len(live.json()["products"]) == 16
        assert observed_limits == [16]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_current_result_count_uses_shared_cache_and_non_blocking_route(monkeypatch):
    observed_limits = []

    class FakeReadService:
        def query_detail(self, task_id):
            return {"query": {"brand": "atlas"}}

        def live_result(self, task_id, *, limit):
            observed_limits.append(limit)
            return {
                "taskId": task_id,
                "status": "executed",
                "resultCount": 41,
                "resultCountKnown": True,
                "products": [],
                "errorCode": None,
                "errorMessage": None,
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    monkeypatch.setattr(
        query_governance,
        "get_brand_business_config",
        lambda brand: {"top_query_check_product_limit": 16},
    )
    query_governance._clear_top_query_endpoint_caches()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        live_route = next(
            route
            for route in query_governance.router.routes
            if isinstance(route, APIRoute)
            and route.path == "/api/dashboard/top-query/queries/{task_id}/current-result-count"
            and "GET" in route.methods
        )
        assert not inspect.iscoroutinefunction(live_route.endpoint)

        client = TestClient(app)
        first = client.get("/api/dashboard/top-query/queries/42/current-result-count")
        second = client.get("/api/dashboard/top-query/queries/42/current-result-count")
        assert first.status_code == second.status_code == 200
        assert first.json()["resultCount"] == second.json()["resultCount"] == 41
        assert observed_limits == [16]
    finally:
        query_governance._clear_top_query_endpoint_caches()
        app.dependency_overrides.pop(get_current_user, None)


def test_query_governance_service_bounds_live_search_requests(monkeypatch):
    observed = {}

    class FakeOperationalService:
        def search_products_with_total(self, brand, query, **kwargs):
            observed.update({"brand": brand, "query": query, **kwargs})
            return {"success": True, "products": [], "total_count": 0, "result_complete": True}

    monkeypatch.setenv("AIOPS_TOP_QUERY_LIVE_RESULT_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setattr(query_governance, "_operational_service", FakeOperationalService())

    service = query_governance._service()
    result = service._live_search("atlas", "连衣裙", 16)

    assert result["success"] is True
    assert observed == {
        "brand": "atlas",
        "query": "连衣裙",
        "page_size": 16,
        "enrich_details": False,
        "request_timeout": 2.5,
        "max_attempts": 1,
    }


def test_query_trend_route_returns_persisted_query_metrics(monkeypatch):
    class FakeReadService:
        def query_detail(self, task_id):
            return {
                "query": {"brand": "atlas", "queryText": "帽子 女士"},
                "trend": [{"date": "2026-09-05", "pv": 114, "ctr": 0.42, "cvr": 0}],
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/queries/42/trend")
        assert response.status_code == 200
        assert response.json()["trend"] == [{"date": "2026-09-05", "pv": 114, "ctr": 0.42, "cvr": 0}]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_query_governance_events_route_returns_rewritten_events(monkeypatch):
    class FakeReadService:
        def query_detail(self, task_id):
            return {
                "query": {"brand": "atlas"},
                "events": [{"id": 7, "agentName": "QueryPreferenceAgent"}],
            }

    user = type(
        "UserStub",
        (),
        {"brand_permissions": '["atlas"]', "is_admin": False},
    )()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/queries/42/events")
        assert response.status_code == 200
        assert response.json() == {"items": [{"id": 7, "agentName": "QueryPreferenceAgent"}]}
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_product_detail_route_enriches_s0_snapshot_with_live_product_detail(monkeypatch):
    class FakeReadService:
        def query_detail(self, task_id):
            return {
                "query": {"brand": "atlas"},
                "s0": {
                    "products": [
                        {
                            "productId": "p1",
                            "productName": "快照名称",
                            "imageUrl": "https://snapshot/image.jpg",
                            "discountPrice": 329,
                        }
                    ]
                },
            }

    class FakeOperationalService:
        def get_product_details(self, brand, product_id):
            assert (brand, product_id) == ("atlas", "p1")
            return [{
                "productId": "p1",
                "productName": "实时商品名称",
                "mainImage": "https://live/image.jpg",
                "price": 399,
                "stock": 8,
                "category": "女装>针织衫",
            }]

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    monkeypatch.setattr(query_governance, "_operational_service", FakeOperationalService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(
            "/api/dashboard/top-query/brands/atlas/products/p1?taskId=42"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["product"]["productName"] == "实时商品名称"
        assert body["product"]["stock"] == 8
        assert body["product"]["discountPrice"] == 329
        assert body["detailMeta"] == {
            "detailSource": "live_with_snapshot",
            "capturedAt": None,
            "isSnapshot": False,
            "taskId": 42,
        }
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize(
    "description,record_id,brand,tag_result,expected,calls",
    [
        (None, "record-p1", "atlas", {"id": "record-p1", "description": "POLO衫领上衣，短袖。"}, "POLO衫领上衣，短袖。", 1),
        ("  ", "record-p1", "atlas", {"id": "record-p1", "description": "原始商品描述"}, "原始商品描述", 1),
        ("已有描述", "record-p1", "atlas", {}, "已有描述", 0),
        (None, None, "atlas", {}, None, 0),
        (None, "record-p1", "summit", {}, None, 0),
        (None, "record-p1", "atlas", RuntimeError("unavailable"), None, 1),
        (None, "record-p1", "atlas", {"id": "record-p1", "description": "  "}, None, 1),
        (None, "record-p1", "atlas", {"id": "other", "description": "其他商品"}, None, 1),
        (None, "record-p1", "atlas", {"id": "record-p1", "description": {"invalid": True}}, None, 1),
    ],
)
def test_product_detail_enriches_missing_description_from_record_id(
    monkeypatch, description, record_id, brand, tag_result, expected, calls
):
    from unittest.mock import Mock

    live = {"id": record_id, "productId": "p1", "productName": "商品", "description": description, "stock": 20}
    service = Mock()
    service.get_product_details.return_value = [live]
    if isinstance(tag_result, Exception):
        service.get_product_tag_sources.side_effect = tag_result
    else:
        service.get_product_tag_sources.return_value = {"stock": 999, **tag_result}
    monkeypatch.setattr(query_governance, "_operational_service", service)
    user = type("UserStub", (), {"brand_permissions": f'["{brand}"]', "is_admin": False})()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(f"/api/dashboard/top-query/brands/{brand}/products/p1")
        assert response.status_code == 200
        product = response.json()["product"]
        assert product.get("description") == expected
        assert product["stock"] == 20
        assert live["description"] == description
        assert service.get_product_tag_sources.call_count == calls
        if calls:
            service.get_product_tag_sources.assert_called_once_with(brand, "record-p1")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_product_detail_route_returns_live_product_without_task_id(monkeypatch):
    class FakeReadService:
        def query_detail(self, task_id):
            raise AssertionError("task snapshot must not be required for operations logs")

    class FakeOperationalService:
        def get_product_details(self, brand, product_id):
            assert (brand, product_id) == ("atlas", "p1")
            return [{"productId": "p1", "productName": "猫跟凉鞋", "mainImage": "https://img.test/p1.jpg"}]

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    monkeypatch.setattr(query_governance, "_operational_service", FakeOperationalService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/brands/atlas/products/p1")
        assert response.status_code == 200
        assert response.json()["product"]["productName"] == "猫跟凉鞋"
        assert response.json()["detailMeta"] == {
            "detailSource": "live",
            "capturedAt": None,
            "isSnapshot": False,
            "taskId": None,
        }
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_product_detail_route_uses_same_query_mixed_row_when_detail_api_is_unavailable(monkeypatch):
    class FakeReadService:
        def query_detail(self, task_id):
            return {
                "query": {"brand": "atlas", "queryText": "牛仔裤 女士"},
                "s0": {
                    "products": [{
                        "productId": "C08228021250,8228121250,7949131250,8228226250,8228246250,8228236250",
                        "rank": 1,
                        "productName": "Z1975 高腰长版常规牛仔裤",
                        "discountPrice": 299,
                    }]
                },
            }

    class FakeOperationalService:
        def get_product_details(self, brand, product_id):
            return []

        def search_products_with_total(self, brand, query, **kwargs):
            assert (brand, query) == ("atlas", "牛仔裤 女士")
            assert kwargs["enrich_details"] is False
            return {
                "products": [{
                    "productId": "C08228021250,8228121250,7949131250,8228226250,8228246250,8228236250",
                    "productName": "Z1975 高腰长版常规牛仔裤",
                    "description": "高腰牛仔裤，五口袋。常规长裤腿。",
                    "mainImage": "https://img.test/jeans.jpg",
                    "stock": 7,
                }]
            }

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    monkeypatch.setattr(query_governance, "_operational_service", FakeOperationalService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get(
            "/api/dashboard/top-query/brands/atlas/products/"
            "C08228021250%2C8228121250%2C7949131250%2C8228226250%2C8228246250%2C8228236250?taskId=1131"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["product"]["description"] == "高腰牛仔裤，五口袋。常规长裤腿。"
        assert body["product"]["mainImage"] == "https://img.test/jeans.jpg"
        assert body["product"]["stock"] == 7
        assert body["detailMeta"]["detailSource"] == "live_with_snapshot"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("recall_result,expected", [
    ({"products": [{"productId": "p1", "description": "从召回结果补齐的描述"}]}, "从召回结果补齐的描述"),
    ({"products": [{"productId": "p1,p2", "description": "别名描述"}, {"productId": "p1", "description": "精确描述"}]}, "精确描述"),
    ({"products": [{"productId": "p1;p2", "description": "别名描述"}]}, "别名描述"),
    ({"products": [{"productId": "other", "description": "不能串商品"}]}, None),
    ({"success": False, "products": [{"productId": "p1", "description": "不可用"}]}, None),
    ({"products": None}, None),
    (RuntimeError("search unavailable"), None),
])
def test_product_detail_route_enriches_existing_live_row_description_from_query_recall(monkeypatch, recall_result, expected):
    class FakeReadService:
        def query_detail(self, task_id):
            return {
                "query": {"brand": "atlas", "queryText": "牛仔裤 女士"},
                "s0": {"products": [{"productId": "p1", "productName": "快照名称", "discountPrice": 299}]},
            }

    class FakeOperationalService:
        def get_product_details(self, brand, product_id):
            return [{"id": "record-p1", "productId": "p1", "productName": "实时名称", "stock": 5}]

        def get_product_tag_sources(self, brand, record_id):
            return {"id": record_id}

        def search_products_with_total(self, brand, query, **kwargs):
            if isinstance(recall_result, Exception):
                raise recall_result
            return recall_result

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_service", lambda: FakeReadService())
    monkeypatch.setattr(query_governance, "_operational_service", FakeOperationalService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/brands/atlas/products/p1?taskId=42")
        assert response.status_code == 200
        assert response.json()["product"].get("description") == expected
        assert response.json()["product"]["stock"] == 5
        assert response.json()["product"]["productName"] == "实时名称"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_product_detail_route_falls_back_without_error_when_live_detail_is_unavailable(monkeypatch):
    class FakeOperationalService:
        def get_product_details(self, brand, product_id):
            return []

    user = type("UserStub", (), {"brand_permissions": '["atlas"]', "is_admin": False})()
    monkeypatch.setattr(query_governance, "_operational_service", FakeOperationalService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/brands/atlas/products/p1")
        assert response.status_code == 200
        assert response.json() == {
            "product": {"productId": "p1", "skuId": "p1"},
            "detailMeta": {
                "detailSource": "operations_log_fallback",
                "capturedAt": None,
                "isSnapshot": False,
                "taskId": None,
            },
        }
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_experiment_history_route_reads_rewrite_owned_service(monkeypatch):
    class FakeExperimentService:
        def list_history_summaries(self, brand, **kwargs):
            assert brand == "atlas"
            assert kwargs["page"] == 1
            return {"brand": brand, "current": None, "items": [], "total": 0, "page": 1, "pageSize": 20}

    user = type(
        "UserStub",
        (),
        {"brand_permissions": '["atlas"]', "is_admin": False},
    )()
    monkeypatch.setattr(query_governance, "_experiment_service", lambda: FakeExperimentService())
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).get("/api/dashboard/top-query/experiments/brands/atlas")
        assert response.status_code == 200
        assert response.json()["brand"] == "atlas"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
