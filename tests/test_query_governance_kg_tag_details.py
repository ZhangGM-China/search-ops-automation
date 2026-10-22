import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from src.api import app, get_current_user
from src.api.application import query_governance


@pytest.fixture
def client(monkeypatch):
    user = SimpleNamespace(id=1, username='operator', brand_permissions='["atlas"]', is_admin=False)
    app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def test_tag_details_project_only_requested_attributes_and_select_exact_name(client, monkeypatch):
    calls = []
    def read(self, brand, tag_name):
        calls.append((brand, tag_name))
        return {'data': {'records': [
            {'id': 'wrong', 'name': '羊毛混纺'},
            {'id': 'wool', 'name': '羊毛', 'viewTag': '羊毛', 'sceneId': 'scene',
             'isRoot': '0', 'dimensions': ['材质'], 'weight': 8, 'isEnglish': 1,
             'segmentingAgain': '0', 'isCompose': '1', 'nameAnd': ['羊绒'], 'nameNot': ['羊']},
        ]}}
    monkeypatch.setattr(query_governance.KnowledgeGraphReadTools, 'query_tag', read)
    response = client.get('/api/dashboard/top-query/brands/atlas/kg-tag-details', params={'name': '羊毛'})
    assert response.status_code == 200
    assert response.json() == {'name': '羊毛', 'viewTag': '羊毛', 'isRoot': '0',
        'dimensions': ['材质'], 'weight': 8, 'isEnglish': 1, 'segmentingAgain': '0',
        'isCompose': '1', 'nameAnd': ['羊绒'], 'nameNot': ['羊']}
    assert calls == [('atlas', '羊毛')]


def test_tag_details_keep_brand_permissions(client, monkeypatch):
    monkeypatch.setattr(query_governance.KnowledgeGraphReadTools, 'query_tag', lambda *a, **k: pytest.fail('forbidden read'))
    response = client.get('/api/dashboard/top-query/brands/harbor/kg-tag-details', params={'name': '羊毛'})
    assert response.status_code == 403


def test_tag_details_missing_tag_is_explicit(client, monkeypatch):
    monkeypatch.setattr(query_governance.KnowledgeGraphReadTools, 'query_tag', lambda *a, **k: {'data': {'records': []}})
    response = client.get('/api/dashboard/top-query/brands/atlas/kg-tag-details', params={'name': '羊毛'})
    assert response.status_code == 404


def test_tag_details_read_error_is_not_reported_as_missing(client, monkeypatch):
    def fail(*args):
        raise RuntimeError('unavailable')
    monkeypatch.setattr(query_governance.KnowledgeGraphReadTools, 'query_tag', fail)
    assert client.get('/api/dashboard/top-query/brands/atlas/kg-tag-details', params={'name': '羊毛'}).status_code == 502


def test_tag_details_external_failure_and_blank_name(client, monkeypatch):
    monkeypatch.setattr(query_governance.KnowledgeGraphReadTools, 'query_tag', lambda *args: {'code': -1, 'data': []})
    assert client.get('/api/dashboard/top-query/brands/atlas/kg-tag-details', params={'name': '羊毛'}).status_code == 502
    assert client.get('/api/dashboard/top-query/brands/atlas/kg-tag-details', params={'name': ' '}).status_code == 400


def test_tag_details_support_real_tag_name_field_and_flat_record(client, monkeypatch):
    monkeypatch.setattr(query_governance.KnowledgeGraphReadTools, 'query_tag', lambda *args: {'code': 200, 'data': {'id': 'wool', 'tagName': '羊毛', 'weight': 0}})
    response = client.get('/api/dashboard/top-query/brands/atlas/kg-tag-details', params={'name': '羊毛'})
    assert response.status_code == 200
    assert response.json()['name'] == '羊毛'
    assert response.json()['weight'] == 0


def test_relationship_details_exact_id_and_read_only(client, monkeypatch):
    calls = []
    def snapshot(action):
        calls.append(action)
        return {'data': [{'id': 'wrong', 'tagName': 'wrong'}, {'id': 'r1', 'tagName': '羊毛', 'responseTagName': '羊', 'relationships': ['902227092548485120'], 'sceneId': 'hidden'}]}
    monkeypatch.setattr(query_governance._operational_service.kg_client, 'snapshot', snapshot)
    response = client.get('/api/dashboard/top-query/brands/atlas/kg-relationship-details', params={'id': 'r1'})
    assert response.status_code == 200
    assert response.json() == {'tagName': '羊毛', 'responseTagName': '羊', 'relationships': ['902227092548485120']}
    assert calls == [{'brand': 'atlas', 'actionType': 'delete_relationship', 'params': {'id': 'r1'}}]


def test_relationship_details_missing_failure_and_permissions(client, monkeypatch):
    monkeypatch.setattr(query_governance._operational_service.kg_client, 'snapshot', lambda *args: {'data': []})
    assert client.get('/api/dashboard/top-query/brands/atlas/kg-relationship-details', params={'id': 'r1'}).status_code == 404
    assert client.get('/api/dashboard/top-query/brands/harbor/kg-relationship-details', params={'id': 'r1'}).status_code == 403
    def fail(*args):
        raise RuntimeError('unavailable')
    monkeypatch.setattr(query_governance._operational_service.kg_client, 'snapshot', fail)
    assert client.get('/api/dashboard/top-query/brands/atlas/kg-relationship-details', params={'id': 'r1'}).status_code == 502
