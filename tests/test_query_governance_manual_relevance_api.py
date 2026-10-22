"""HTTP contract for the externally callable manual relevance API."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import app, get_current_user
from src.api.application import query_governance


class _FakeManualService:
    def __init__(self) -> None:
        self.submissions = []
        self.runs = []
        self.records = {
            "relchk_existing": {
                "checkId": "relchk_existing",
                "brand": "atlas",
                "status": "completed",
                "queries": [
                    {
                        "query": "白色连衣裙 女士",
                        "status": "completed",
                        "products": [
                            {
                                "sku": "SKU001",
                                "status": "completed",
                                "isRelevant": True,
                                "reason": None,
                            }
                        ],
                    }
                ],
            }
        }

    def get_latest(self, *, brand, query):
        record = self.records.get("relchk_existing")
        if record and record["brand"] == brand and record["queries"][0]["query"] == query:
            return record
        return None

    def submit(self, *, brand, queries, idempotency_key):
        self.submissions.append({
            "brand": brand,
            "queries": queries,
            "idempotency_key": idempotency_key,
        })
        return SimpleNamespace(check_id="relchk_new", status="queued", created=True)

    def run(self, check_id):
        self.runs.append(check_id)

    def get(self, check_id):
        return self.records.get(check_id)


def _user(*brands: str):
    return type(
        "UserStub",
        (),
        {
            "username": "manual-user",
            "id": 7,
            "brand_permissions": str(list(brands)).replace("'", '"'),
            "is_admin": False,
        },
    )()


def test_submit_manual_relevance_check_accepts_only_brand_query_and_skus(monkeypatch):
    service = _FakeManualService()
    monkeypatch.setattr(query_governance, "_manual_relevance_service", lambda: service)
    app.dependency_overrides[get_current_user] = lambda: _user("atlas")
    try:
        response = TestClient(app).post(
            "/api/query-governance/relevance-checks",
            headers={"Idempotency-Key": "manual-api-1"},
            json={
                "brand": "atlas",
                "queries": [
                    {
                        "query": "白色连衣裙 女士",
                        "products": ["SKU001", "SKU002"],
                    }
                ],
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 202
    assert response.json() == {"checkId": "relchk_new", "status": "queued"}
    assert service.submissions == [{
        "brand": "atlas",
        "queries": [{"query": "白色连衣裙 女士", "skus": ["SKU001", "SKU002"]}],
        "idempotency_key": "manual-api-1",
    }]
    assert service.runs == ["relchk_new"]


def test_submit_has_no_query_or_product_count_limit(monkeypatch):
    service = _FakeManualService()
    monkeypatch.setattr(query_governance, "_manual_relevance_service", lambda: service)
    app.dependency_overrides[get_current_user] = lambda: _user("atlas")
    queries = [
        {"query": f"query-{index}", "products": [f"SKU-{index}-{item}" for item in range(241)]}
        for index in range(2)
    ]
    try:
        response = TestClient(app).post(
            "/api/query-governance/relevance-checks",
            headers={"Idempotency-Key": "manual-api-many"},
            json={"brand": "atlas", "queries": queries},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 202
    assert len(service.submissions[0]["queries"]) == 2
    assert len(service.submissions[0]["queries"][0]["skus"]) == 241


def test_submit_rejects_brand_outside_user_permissions(monkeypatch):
    service = _FakeManualService()
    monkeypatch.setattr(query_governance, "_manual_relevance_service", lambda: service)
    app.dependency_overrides[get_current_user] = lambda: _user("harbor")
    try:
        response = TestClient(app).post(
            "/api/query-governance/relevance-checks",
            headers={"Idempotency-Key": "manual-api-forbidden"},
            json={
                "brand": "atlas",
                "queries": [{"query": "裙子", "products": ["SKU001"]}],
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 403
    assert service.submissions == []


def test_submit_rejects_empty_sku_and_unrecognized_input_fields(monkeypatch):
    service = _FakeManualService()
    monkeypatch.setattr(query_governance, "_manual_relevance_service", lambda: service)
    app.dependency_overrides[get_current_user] = lambda: _user("atlas")
    try:
        empty_sku = TestClient(app).post(
            "/api/query-governance/relevance-checks",
            headers={"Idempotency-Key": "manual-api-invalid-sku"},
            json={
                "brand": "atlas",
                "queries": [{"query": "裙子", "products": [" "]}],
            },
        )
        extra_field = TestClient(app).post(
            "/api/query-governance/relevance-checks",
            headers={"Idempotency-Key": "manual-api-extra"},
            json={
                "brand": "atlas",
                "queries": [{"query": "裙子", "products": ["SKU001"]}],
                "detectionTypes": ["Text"],
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert empty_sku.status_code == 422
    assert extra_field.status_code == 422
    assert service.submissions == []


def test_get_manual_relevance_check_returns_minimal_query_shape(monkeypatch):
    service = _FakeManualService()
    monkeypatch.setattr(query_governance, "_manual_relevance_service", lambda: service)
    app.dependency_overrides[get_current_user] = lambda: _user("atlas")
    try:
        response = TestClient(app).get(
            "/api/query-governance/relevance-checks/relchk_existing"
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    body = response.json()
    assert set(body["queries"][0]) == {"query", "status", "products"}
    assert "detectionTypes" not in str(body)


def test_get_manual_relevance_check_returns_404_for_unknown_id(monkeypatch):
    service = _FakeManualService()
    monkeypatch.setattr(query_governance, "_manual_relevance_service", lambda: service)
    app.dependency_overrides[get_current_user] = lambda: _user("atlas")
    try:
        response = TestClient(app).get(
            "/api/query-governance/relevance-checks/missing"
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404


def test_get_latest_manual_relevance_check_scopes_brand_and_query(monkeypatch):
    service = _FakeManualService()
    monkeypatch.setattr(query_governance, "_manual_relevance_service", lambda: service)
    app.dependency_overrides[get_current_user] = lambda: _user("atlas")
    try:
        response = TestClient(app).get(
            "/api/query-governance/relevance-checks/latest",
            params={"brand": "atlas", "query": "白色连衣裙 女士"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.json()["checkId"] == "relchk_existing"


def test_get_latest_manual_relevance_check_returns_null_when_missing(monkeypatch):
    service = _FakeManualService()
    monkeypatch.setattr(query_governance, "_manual_relevance_service", lambda: service)
    app.dependency_overrides[get_current_user] = lambda: _user("atlas")
    try:
        response = TestClient(app).get(
            "/api/query-governance/relevance-checks/latest",
            params={"brand": "atlas", "query": "不存在"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    # 无验证记录是业务常态：200 + null，不再产生 404 噪音。
    assert response.status_code == 200
    assert response.json() is None


def test_external_openapi_exposes_manual_relevance_post_and_get():
    spec = app.openapi()
    submit = spec["paths"]["/api/query-governance/relevance-checks"]["post"]
    result = spec["paths"]["/api/query-governance/relevance-checks/{check_id}"]["get"]
    latest = spec["paths"]["/api/query-governance/relevance-checks/latest"]["get"]

    assert submit["x-exposure"] == "external-compute"
    assert submit["x-side-effects"] == "internal-job-state-only"
    assert submit["security"] == [{"BearerAuth": []}]
    assert result["x-exposure"] == "external-compute"
    assert result["x-side-effects"] == "read-only"
    assert latest["x-exposure"] == "external-compute"
    assert latest["x-side-effects"] == "read-only"
