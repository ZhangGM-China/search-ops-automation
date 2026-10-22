"""Regressions for evidence-backed relevance KG proposals; no external writes."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.query_governance.behavior_evidence import OperationalProductDetailGateway
from src.query_governance.kg_agent import _compact_action_plan
from src.query_governance.kg_action_contract import ACTION_ENDPOINTS
from src.query_governance.kg_action_contract import normalize_kg_action
from src.query_governance.read_api import QueryGovernanceReadService, _cause_groups_for_plan
from src.query_governance.relevance_remediation import RelevanceGovernanceAgent, SUPPORTED_KG_ACTIONS


class Runtime:
    def __init__(self, *outputs):
        self.outputs = iter(outputs)
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append((list(messages), kwargs))
        return json.dumps(next(self.outputs), ensure_ascii=False)


def group(actions):
    return {'groups': [{'rootCause': 'kg_issue', 'causeKey': 'phrase', 'productIds': ['p1'], 'actions': actions}]}


def setup_agent(*outputs, failed=False):
    tools = Mock()
    tools.scene_id.return_value = 's1'
    tools.query_tag.return_value = {'data': [], 'complete': True}
    tools.query_relationship.return_value = {'data': [{'id': 'r1', 'tagName': '花衬衫', 'responseTagName': '衬衫'}], 'complete': True}
    tools.query_dimension.return_value = {'data': [{'id': 'd1', 'name': '品类'}], 'complete': True}
    if failed:
        tools.query_tag.side_effect = RuntimeError('KG unavailable')
    runtime = Runtime({'toolCall': {'name': 'product_details', 'args': {}}}, *outputs)
    agent = RelevanceGovernanceAgent(runtime=runtime, kg_tools=tools, product_details=Mock(fetch=Mock(return_value={'p1': {'id': 'internal-1', 'name': '纯色衬衫'}})))
    return agent, runtime, tools


def analyze(agent):
    return agent.analyze(brand='atlas', query_text='花衬衫 女士', evidence={'blacklist_candidates': [{'product_id': 'p1', 'reason': '纯色'}]})


def test_preflight_and_json_mode_before_model_sees_products():
    agent, runtime, tools = setup_agent({'groups': [{'rootCause': 'individual', 'productIds': ['p1']}]})
    analyze(agent)
    tools.query_tag.assert_any_call('atlas', '花衬衫', langfuse_config=None, parent_observation=None)
    tools.query_relationship.assert_any_call('atlas', '花衬衫', side='both', langfuse_config=None, parent_observation=None)
    assert tools.query_dimension.called
    assert all(kwargs['response_format'] == {'type': 'json_object'} for _, kwargs in runtime.calls)
    assert json.loads(runtime.calls[0][0][1]['content'])['kgPreflight']


def test_invalid_action_is_not_repaired_by_a_second_model_call():
    bad = [{'actionType': '', 'params': {'id': 'r1'}}]
    agent, runtime, _ = setup_agent(group(bad))
    result = analyze(agent)
    g = result['causeGroups'][0]
    assert g['actions'] == []
    assert g['rawActions'] == bad
    assert g['repairAttempts'] == 0
    assert g['validationErrors']
    assert len(runtime.calls) == 2
    persisted = _compact_action_plan(result)
    assert persisted['causeGroups'][0]['rawActions'] == bad
    assert persisted['tool_evidence']['calls']


@pytest.mark.parametrize('actions', [[], [{'actionType': ''}], [{'actionType': 'delete_relationship', 'params': {'id': 'invented'}}]])
def test_invalid_or_empty_proposal_stays_reviewable_with_evidence(actions):
    agent, runtime, _ = setup_agent(group(actions), {'actions': actions})
    result = analyze(agent)
    g = result['causeGroups'][0]
    assert g['actions'] == []
    assert g['validationErrors']
    assert g['graphContext']['reads']
    assert g['rawActions'] == actions
    assert len(runtime.calls) <= 3
    persisted = _compact_action_plan(_compact_action_plan(result))
    assert persisted['causeGroups'][0]['validationErrors'] == g['validationErrors']
    assert persisted['tool_evidence']['calls']
    displayed = _cause_groups_for_plan([g], action_id=10, status='pending', status_label='待执行')[0]
    assert displayed['statusLabel'] == '需人工复核'
    assert displayed['executable'] is False


def test_failed_graph_read_cannot_be_repaired_into_executable_action():
    good = [{'actionType': 'delete_relationship', 'params': {'id': 'r1'}}]
    agent, _, _ = setup_agent(group(good), {'actions': good}, failed=True)
    result = analyze(agent)
    g = result['causeGroups'][0]
    assert not g['actions']
    assert g['validationErrors']
    assert any(call['result'].get('error') for call in result['tool_evidence']['calls'])


def test_composite_ids_with_tag_sources_keep_internal_record_identity():
    service = Mock()
    service.get_product_status_map.return_value = {'sku2': {'raw': {'id': 'record-2', 'productId': 'sku2'}}}
    service.get_product_tag_sources.return_value = {'tags': ['衬衫']}
    result = OperationalProductDetailGateway(service, include_tag_sources=True).fetch('atlas', ['sku1,sku2'])
    assert result['sku1,sku2']['id'] == 'record-2'
    assert service.get_product_status_map.call_args.args[1] == ['sku1', 'sku2']
    service.get_product_tag_sources.assert_called_once_with('atlas', 'record-2')


def test_missing_kg_plan_is_not_reported_as_no_work():
    result = QueryGovernanceReadService._relevance_remediation_payload(task=SimpleNamespace(brand='atlas', data_date='2026-10-12'), relevance={'causeGroups': [{'rootCause': 'kg_issue', 'actions': []}]}, actions=[])
    assert result['kgStrategyStatus'] == '需人工复核'


def test_generated_action_vocabulary_matches_executor_api_contract():
    assert SUPPORTED_KG_ACTIONS == frozenset(ACTION_ENDPOINTS)


def test_singleton_dimension_arrays_from_model_are_canonicalized_for_api():
    action = normalize_kg_action({
        'actionType': 'add_tag',
        'params': {
            'name': '花衬衫', 'viewTag': '花衬衫', 'sceneId': 's1',
            'dimensions': ['d1'], 'isEnglish': 0, 'isRoot': 0,
            'segmentingAgain': 0, 'isCompose': 0, 'weight': 400,
        },
    })
    assert action['params']['dimensions'] == 'd1'


def test_phrase_alias_strategy_passes_after_exact_candidate_reads():
    new_tag = dict(name='花衬衫', viewTag='花衬衫', sceneId='s1', dimensions='d1', isEnglish=0, isRoot=0, segmentingAgain=0, isCompose=0, weight=400)
    existing = {**new_tag, 'id': 'root1', 'name': '印花衬衫', 'isRoot': 1, 'dimensions': ['d1']}
    actions = [
        {'actionType': 'add_tag', 'params': new_tag},
        {'actionType': 'add_relationship', 'dependsOn': [1], 'params': {
            'sceneId': 's1', 'relationships': '902227138815852544', 'tagId': '@step:1',
            'tagName': '花衬衫', 'dimensionNames': '品类', 'responseTagId': 'root1', 'responseTagName': '印花衬衫',
        }},
    ]
    agent, runtime, tools = setup_agent(
        {'toolCall': {'name': 'query_tag', 'args': {'tag_name': '印花衬衫'}}},
        {'toolCall': {'name': 'query_relationship', 'args': {'tag_name': '印花衬衫', 'side': 'both'}}},
        group(actions),
    )
    tools.query_tag.side_effect = lambda brand, name, **kwargs: {'data': [existing] if name == '印花衬衫' else [], 'complete': True}
    tools.query_relationship.return_value = {'data': [], 'complete': True}
    result = analyze(agent)
    g = _compact_action_plan(result)['causeGroups'][0]
    assert len(g['actions']) == 2
    assert g['actions'][1]['params']['tagId'] == '@step:1'
    assert g['actions'][1]['dependsOn'] == [1]
    assert g['repairAttempts'] == 0
    assert not g['validationErrors']


def test_empty_group_cannot_fall_back_to_another_groups_actions():
    from src.query_governance.kg_execution import QueryGovernanceKGActionExecutor
    other = [{'actionType': 'delete_relationship', 'params': {'id': 'other'}}]
    assert QueryGovernanceKGActionExecutor._actions_for_group({'actions': other}, {'actions': []}) == []
