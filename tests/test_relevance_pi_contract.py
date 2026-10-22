from copy import deepcopy
from unittest.mock import Mock

import pytest

from src.query_governance.relevance_kg_contract import RELEVANCE_KG_OUTPUT_SCHEMA, validate_relevance_output


def payload(actions=None, kind='kg_issue'):
    return {'groups': [{'groupId': 'g1', 'rootCause': kind, 'causeKey': 'bad_relation',
        'reason': '错误从属关系', 'productIds': ['p1'], 'confidence': 0.9,
        'actions': actions if actions is not None else [{'actionType': 'delete_relationship', 'params': {'id': 'r1'}, 'reason': '删除错误关系'}]}]}


def test_schema_discriminates_all_six_actions_and_requires_tag_fields():
    schema = RELEVANCE_KG_OUTPUT_SCHEMA
    group = schema['$defs']['StrategyGroup']
    assert len(group['properties']['actions']['items']['oneOf']) == 6
    for name in ('AddTagParams', 'UpdateTagParams'):
        assert {'viewTag', 'weight', 'nameAnd', 'nameNot'} <= set(schema['$defs'][name]['required'])


def test_valid_delete_plan_preserved_without_business_defaults():
    value = payload()
    assert validate_relevance_output(value, {'p1'}) == value


@pytest.mark.parametrize('mutation', [
    lambda p: p['groups'][0].pop('confidence'),
    lambda p: p['groups'][0].update(productIds=['p1', 'p1']),
    lambda p: p['groups'][0].update(productIds=['other']),
    lambda p: p['groups'].append(deepcopy(p['groups'][0])),
    lambda p: p['groups'][0].update(rootCause='individual'),
    lambda p: p['groups'][0]['actions'][0].update(dependsOn=[1]),
])
def test_invalid_coverage_or_action_contract_is_rejected(mutation):
    value = payload()
    mutation(value)
    with pytest.raises(ValueError):
        validate_relevance_output(value, {'p1'})


def test_unresolved_step_reference_and_wrong_type_are_rejected():
    value = payload([{'actionType': 'add_relationship', 'reason': '同义', 'dependsOn': [1], 'params': {
        'sceneId': 's1', 'relationships': '902227138815852544', 'tagId': '@step:1',
        'tagName': '花衬衫', 'dimensionNames': ['品类'], 'responseTagId': 't2', 'responseTagName': '印花衬衫'}}])
    with pytest.raises(ValueError):
        validate_relevance_output(value, {'p1'})


def test_pi_client_uses_server_credentials_and_retains_evidence(monkeypatch):
    from types import SimpleNamespace
    from src.query_governance.pi_governance import PiGovernanceClient
    import src.query_governance.pi_governance as module
    monkeypatch.setenv('RD_LLM_API_KEY', 'test-model-secret')
    monkeypatch.setenv('PI_AGENT_INTERNAL_TOKEN', 'test-internal')
    monkeypatch.setattr(module, 'model_audit_fields', lambda *args: {'modelName': 'configured-model'})
    runtime = SimpleNamespace(model_config=SimpleNamespace(primary_model='configured-model', fallback_model=None), model_config_snapshot=SimpleNamespace(version=3))
    post = Mock(return_value=Mock(json=Mock(return_value={'output': payload(), 'toolResults': [{'name': 'query_relationship', 'args': {'tag_name': '花衬衫', 'side': 'both'}, 'result': {'data': [{'id': 'r1'}], 'complete': True}}], 'usage': {'totalTokens': 100}})))
    result = PiGovernanceClient(post=post).run(runtime=runtime, brand='atlas', scene_id='s1', request={'query': '花衬衫'}, system_prompt='test', output_schema=RELEVANCE_KG_OUTPUT_SCHEMA)
    body = post.call_args.kwargs['json']
    assert body['modelConfig']['apiKey'] == 'test-model-secret'
    assert 'test-model-secret' not in str(result)
    assert result['audit']['tokenUsage'] == 100
    assert body['brand'] == 'atlas' and body['sceneId'] == 's1'
    assert post.call_args.args[0].endswith('/v1/kg/relevance/run')


def test_pi_path_does_not_mark_products_missing_from_legacy_local_cache():
    from src.query_governance.relevance_remediation import RelevanceGovernanceAgent

    kg_tools = Mock()
    kg_tools.scene_id.return_value = 's1'
    pi_client = Mock()
    pi_client.run.return_value = {
        'output': {
            'groups': [{
                'groupId': 'g1', 'rootCause': 'individual', 'causeKey': 'copy',
                'reason': '商品主体不符合', 'productIds': ['p1'],
                'confidence': 0.9, 'actions': [],
            }],
        },
        'toolResults': [{
            'name': 'query_tag', 'args': {'tag_name': '花衬衫'},
            'result': {'data': [], 'complete': True},
        }],
        'audit': {}, 'rawText': '',
    }
    agent = RelevanceGovernanceAgent(
        runtime=Mock(), kg_tools=kg_tools, product_details=None, pi_client=pi_client,
    )

    result = agent.analyze(
        brand='atlas', query_text='花衬衫 女士',
        evidence={'blacklist_candidates': [{'product_id': 'p1', 'reason': '纯色'}]},
    )

    assert result['causeGroups'][0]['rootCause'] == 'individual'
    assert result['causeGroups'][0]['productIds'] == ['p1']
