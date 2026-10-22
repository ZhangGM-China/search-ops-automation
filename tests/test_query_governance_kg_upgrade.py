"""Approved KG business contracts, exercised without external writes."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.query_governance.kg_agent import KGAgent, KnowledgeGraphReadTools, KGPlanConflictChecker, _compact_action_plan
from src.query_governance.kg_action_contract import normalize_kg_action
from src.query_governance.recall import RecallCheckRunner, RecallHistoryBaseline
from src.query_governance.models import QuerySpec
from src.integrations.operational_service import OperationalService


class Runtime:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def invoke(self, messages, **kwargs):
        self.messages.append(list(messages))
        return json.dumps(next(self.responses))


def tag(name='裙子', identity='t1', root='1'):
    return dict(id=identity, tagId='biz-'+identity, name=name, sceneId='s1', dimensions=['d1'],
                dimensionNames=['品类'], isRoot=root, isEnglish=0, segmentingAgain='0',
                isCompose='0', nameAnd=[], nameNot=[], weight=200, viewTag=name, isBlack=0)


class Client:
    def __init__(self):
        self.row = tag()
        self.calls = []

    def get_scene_id(self, brand):
        return 's1'

    def execute(self, action):
        self.calls.append(action)
        if action['actionType'] == 'query_tag':
            return {'code': 1, 'data': [dict(self.row)]}
        return {'code': 1, 'data': []}


class Search:
    def mixed_with_total(self, *args, **kwargs):
        return {'success': True, 'total_count': 2, 'products': [{'productId': 'p1'}]}


def generate(client=None):
    client = client or Client()
    tools = KnowledgeGraphReadTools(client, search=Search())
    runtime = Runtime([
        {'toolCall': {'name': 'query_tag', 'args': {'tag_name': '裙子'}}},
        {'toolCall': {'name': 'search_products', 'args': {'query': '裙子'}}},
        {'decision': 'adjust', 'reason': '标签错误影响召回', 'actions': [
            {'actionType': 'blacklist_tag', 'params': {'id': 't1', 'tagId': 'biz-t1', 'sceneId': 's1', 'isBlack': 1}}
        ]},
    ])
    return KGAgent(runtime=runtime, tools=tools).generate(brand='atlas', query_text='裙子', trigger_source='recall_anomaly', evidence={}), tools


def test_plan_survives_compaction_and_guard_ignores_search_count_changes():
    result, tools = generate()
    assert result.plan['decision'] == 'adjust'
    persisted = _compact_action_plan(_compact_action_plan(result.plan))
    assert persisted['tool_evidence']['calls']
    tools._search = Mock(mixed_with_total=Mock(return_value={'success': True, 'total_count': 300, 'products': []}))
    KGPlanConflictChecker(tools).assert_current(persisted)
    tools._client.row['isBlack'] = 1
    with pytest.raises(Exception, match='KG_PLAN_CONFLICT'):
        KGPlanConflictChecker(tools).assert_current(persisted)


def test_large_graph_keeps_replay_queries_and_full_digest():
    client = Client()
    client.row['description'] = '大图谱证据' * 15000
    result, tools = generate(client)
    original = result.plan
    original_digest = original['graph_context_digest']
    original['causeGroups'] = [{'groupId': 'g1', 'rootCause': 'kg_issue',
                               'graphContext': original['graph_context'],
                               'graphContextDigest': original_digest,
                               'actions': original['actions']}]
    compacted = _compact_action_plan(original)
    persisted = _compact_action_plan(compacted)
    assert persisted == compacted
    assert len(json.dumps(persisted, ensure_ascii=False).encode()) < 60000
    assert persisted['graph_context_digest'] == original_digest
    assert persisted['graph_context']['reads'][0]['args'] == original['graph_context']['reads'][0]['args']
    assert persisted['causeGroups'][0]['graphContext']['resultsOmitted'] is True
    assert original['graph_context']['reads'][0]['result']['data']
    KGPlanConflictChecker(tools).assert_current(persisted)
    client.row['isBlack'] = 1
    with pytest.raises(Exception, match='KG_PLAN_CONFLICT'):
        KGPlanConflictChecker(tools).assert_current(persisted)


def test_unqueried_relationship_id_cannot_become_executable():
    runtime = Runtime([
        {'toolCall': {'name': 'search_products', 'args': {'query': '裙子'}}},
        {'decision': 'adjust', 'reason': 'test', 'actions': [{'actionType': 'delete_relationship', 'params': {'id': 'invented'}}]},
    ])
    result = KGAgent(runtime=runtime, tools=KnowledgeGraphReadTools(Client(), search=Search())).generate(
        brand='atlas', query_text='裙子', trigger_source='recall_anomaly', evidence={})
    assert result.plan['decision'] == 'manual_review'
    assert result.plan['actions'] == []


def test_target_relationship_is_exact_and_paginates():
    class Paged:
        def execute(self, action):
            page = action['params']['pageNum']
            return {'code': 1, 'pages': 2, 'data': [{'id': str(page), 'tagName': 'A', 'responseTagName': '裙子' if page == 2 else '短裙子'}]}
    tools = KnowledgeGraphReadTools(Paged())
    assert [row['id'] for row in tools.query_relationship('atlas', '裙子', side='target')['data']] == ['2']


def test_graph_business_error_is_not_empty_evidence():
    tools = KnowledgeGraphReadTools(Mock(execute=Mock(return_value={'code': -1, 'data': []})))
    with pytest.raises(Exception):
        tools.query_tag('atlas', '裙子')


def test_agent_cannot_propose_unblacklist_as_unapproved_strategy():
    with pytest.raises(ValueError):
        normalize_kg_action({'actionType': 'unblacklist_tag', 'params': {'id': 't1', 'sceneId': 's1', 'tagId': 'b1', 'isBlack': 0}})


def test_drop_excludes_confirmed_offline_and_query_blacklist_before_agent():
    gate = Mock()
    gate.assess.return_value = {'status': 'normal', 'missing_count': 5, 'normal_count': 5, 'unexplained_products': []}
    runner = RecallCheckRunner(drop_assessor=gate, config_provider=lambda b: {})
    tasks = Mock()
    result = runner.run(QuerySpec(brand='atlas', query='裙子', query_id='q1', batch_id='b1'), {
        's0': {'result_count': 5, 'products': [], 'result_complete': True},
        'history_baseline': RecallHistoryBaseline(previous_result_count=10),
        'async_tasks': tasks, 'batch_id': 'b1',
    })
    assert result.result['conclusion'] == '正常消失'
    tasks.enqueue_for_query.assert_not_called()


def test_drop_snapshot_unknown_does_not_go_to_kg_as_confirmed_problem():
    gate = Mock()
    gate.assess.return_value = {'status': 'manual_review', 'reason': '历史仅前N件'}
    runner = RecallCheckRunner(drop_assessor=gate, config_provider=lambda b: {})
    tasks = Mock()
    result = runner.run(QuerySpec(brand='atlas', query='裙子', query_id='q1', batch_id='b1'), {
        's0': {'result_count': 5, 'products': [], 'result_complete': True},
        'history_baseline': RecallHistoryBaseline(previous_result_count=10),
        'async_tasks': tasks, 'batch_id': 'b1',
    })
    assert result.result['conclusion'] == '需人工复核'
    tasks.enqueue_for_query.assert_not_called()


def test_query_blacklist_lookup_accepts_empty_business_code_and_pages():
    service = OperationalService.__new__(OperationalService)
    service._get_base_url = Mock(return_value="https://example.test")
    service._get_headers = Mock(return_value={"x-test": "1"})
    service._call_api = Mock(side_effect=[
        Mock(status_code=200, json=lambda: {"data": {"total": 2, "list": [{"productId": "p1"}]}}),
        Mock(status_code=200, json=lambda: {"data": {"total": 2, "list": [{"spu": "p2"}]}}),
    ])

    assert service.get_query_blacklisted_product_ids("atlas", "裙子") == {"p1", "p2"}
    first_payload = service._call_api.call_args_list[0].kwargs["json"]
    second_payload = service._call_api.call_args_list[1].kwargs["json"]
    assert first_payload["pageNo"] == 1
    assert second_payload["pageNo"] == 2


def test_product_tag_source_lookup_requires_object_data():
    service = OperationalService.__new__(OperationalService)
    service._get_base_url = Mock(return_value="https://example.test")
    service._get_headers = Mock(return_value={})
    service._call_api = Mock(return_value=Mock(status_code=200, json=lambda: {
        "code": 0, "data": {"tags": [{"name": "裙子"}], "reasoning": [{"source": "商品描述"}]}
    }))

    assert service.get_product_tag_sources("atlas", "record-1")["tags"][0]["name"] == "裙子"
    assert service._call_api.call_args.kwargs["params"] == {"id": "record-1"}
