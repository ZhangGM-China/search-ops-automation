"""Real executor/gateway/SQLite/HTTP chain; external KG is an isolated simulator."""
import json
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.integrations.knowledge_graph import KnowledgeGraphClient
from src.persistence.models import Base
from src.query_governance.kg_execution import QueryGovernanceKGActionExecutor, _verify_readback
from src.query_governance.models import QueryGovernanceActionPlan, QueryGovernanceTask


@pytest.mark.parametrize("fault", [None, "missing_tag", "duplicate_tag", "wrong_flags", "query_error", "missing_relation"])
def test_create_tag_then_relationship_over_http_and_replay(fault):
    state = {"tag": None, "relation": None, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            params = json.loads(self.rfile.read(int(self.headers["Content-Length"])))['params']
            state['requests'].append((self.path, params, self.headers.get('Idempotency-Key')))
            if self.path == '/admin/tag/addTag':
                state['tag'] = {**params, 'id': 'kg_tag/node-from-query', 'tagId': 'business-id'}
                body = {'code': 1, 'data': {'id': 'different-write-receipt'}}
            elif self.path == '/admin/tag/query':
                rows = [state['tag']] if state['tag'] else []
                if fault == 'missing_tag':
                    rows = []
                elif fault == 'duplicate_tag':
                    rows += [{**state['tag'], 'id': 'kg_tag/another-node'}]
                elif fault == 'wrong_flags':
                    rows = [{**state['tag'], 'isRoot': 1}]
                body = {'code': -1 if fault == 'query_error' else 1, 'data': rows, 'count': len(rows)}
            elif self.path == '/admin/tagRelationship/addRelationship':
                state['relation'] = {**params, 'id': 'relation-node',
                                     'parent': 'kg_tag/' + params['tagId'],
                                     'child': 'kg_tag/' + params['responseTagId'],
                                     'relationships': [params['relationships']],
                                     'tagDimensions': [params['dimensionNames']]}
                for field in ('tagId', 'responseTagId', 'dimensionNames'):
                    del state['relation'][field]
                body = {'code': 1, 'data': '标签关系插入成功'}
            elif self.path == '/admin/tagRelationship/query':
                rows = [] if fault == 'missing_relation' else [state['relation']]
                body = {'code': 1, 'data': rows, 'count': len(rows)}
            else:
                body = {'code': -1, 'data': []}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            task = QueryGovernanceTask(batch_id='http', brand='atlas', data_date=date(2026, 10, 12), query_text='花衬衫', status='completed')
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(
                task_id=task.id, batch_id='http', brand='atlas', query_text=task.query_text,
                node_key='kg_agent', action_type='kg_plan', idempotency_key='http', status='pending',
                response_json=json.dumps({'actions': [
                    {'actionType': 'add_tag', 'params': {
                        'name': '花衬衫', 'viewTag': '花衬衫', 'sceneId': 'scene',
                        'dimensions': 'category', 'isEnglish': 0, 'isRoot': 0,
                        'segmentingAgain': 0, 'isCompose': 0, 'weight': 400}},
                    {'actionType': 'add_relationship', 'dependsOn': [1], 'params': {
                        'sceneId': 'scene', 'tagId': '@step:1', 'tagName': '花衬衫',
                        'responseTagId': 'existing-root', 'responseTagName': '印花衬衫',
                        'relationships': KnowledgeGraphClient.RELATION_SYNONYM, 'dimensionNames': '品类'}},
                ]}),
            )
            session.add(plan)
            session.commit()
            plan_id = plan.id
        client = KnowledgeGraphClient(base_url=f'http://127.0.0.1:{server.server_port}', token='test-only', environment='test')
        executor = QueryGovernanceKGActionExecutor(session_factory=factory, kg_client=client)
        result = executor.execute(plan_id, user_id='test-operator')
        assert result['status'] == ('execution_unknown' if fault else 'verified')
        writes = [row for row in state['requests'] if not row[0].endswith('/query')]
        assert len(writes) == (1 if fault and fault != 'missing_relation' else 2)
        assert all(row[2] for row in writes)
        assert state['requests'][1][0] == '/admin/tag/query'
        assert state['requests'][1][1]['nameAcc'] == '花衬衫'
        if not fault:
            assert writes[1][1]['tagId'] == 'node-from-query'
            assert state['requests'][-1][0] == '/admin/tagRelationship/query'
            executor.execute(plan_id, user_id='test-operator')
            assert len(state['requests']) == 4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        engine.dispose()


@pytest.mark.parametrize('observed', [
    {'snapshotSource': 'not_required'}, {'code': -1, 'data': []},
    {'code': 1, 'data': [{'id': 'edge'}]}, {'code': 1, 'data': [], 'count': 1},
])
def test_delete_is_unverified_without_complete_proof_of_absence(observed):
    with pytest.raises(ValueError):
        _verify_readback({'actionType': 'delete_relationship', 'params': {'id': 'edge'}}, observed)


@pytest.mark.parametrize('reference', ['@step:abc', '@step:0', '@step:-1'])
def test_invalid_step_references_are_rejected_before_writes(reference):
    with pytest.raises(ValueError, match='reference'):
        QueryGovernanceKGActionExecutor._normalize_actions([
            {'actionType': 'add_tag', 'params': {'name': '花衬衫'}},
            {'actionType': 'add_relationship', 'params': {'tagId': reference}, 'dependsOn': [1]},
        ])


def test_step_reference_cannot_point_to_relationship_write():
    with pytest.raises(ValueError, match='reference'):
        QueryGovernanceKGActionExecutor._normalize_actions([
            {'actionType': 'delete_relationship', 'params': {'id': 'edge'}},
            {'actionType': 'add_relationship', 'params': {'tagId': '@step:1'}, 'dependsOn': [1]},
        ])
