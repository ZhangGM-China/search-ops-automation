import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.kg_execution import QueryGovernanceKGActionExecutor
from src.query_governance.models import (
    GovernancePlanStatus,
    QueryGovernanceActionPlan,
    QueryGovernanceTask,
)


class FakeKGClient:
    def __init__(self):
        self.calls = []

    def snapshot(self, action):
        rows = [] if action["actionType"] == "delete_relationship" else [
            {"id": "r-1", **action["params"]}
        ]
        return {"code": 1, "data": rows}

    def execute(self, action, external_request_id=None):
        self.calls.append((action, external_request_id))
        return {"code": 0, "message": "success", "data": {"id": "r-1"}}


def _factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


def test_human_can_execute_one_kg_cause_group_and_persist_verified_fact():
    engine, factory = _factory()
    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="batch-1",
                brand="atlas",
                data_date=__import__("datetime").date(2026, 9, 20),
                query_text="轻薄西装",
                status="completed",
            )
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(
                task_id=task.id,
                batch_id=task.batch_id,
                brand=task.brand,
                query_text=task.query_text,
                node_key="query_governance.kg_agent",
                action_type="kg_plan",
                idempotency_key="kg-plan-1",
                status=GovernancePlanStatus.PENDING.value,
                request_json=json.dumps({"evidence": {}}),
                response_json=json.dumps(
                    {
                        "causeGroups": [
                            {
                                "groupId": "kg-1",
                                "rootCause": "kg_issue",
                                "actions": [
                                    {
                                        "actionType": "add_tag",
                                        "params": {"name": "轻薄西装"},
                                    }
                                ],
                            },
                            {
                                "groupId": "individual-1",
                                "rootCause": "individual",
                                "actions": [],
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(plan)
            session.commit()
            plan_id = plan.id

        client = FakeKGClient()
        executor = QueryGovernanceKGActionExecutor(
            session_factory=factory,
            kg_client=client,
            authorizer=lambda _intent: True,
        )
        result = executor.execute(plan_id, user_id="operator-1", group_id="kg-1")

        assert result["status"] == GovernancePlanStatus.VERIFIED.value
        assert result["groupId"] == "kg-1"
        assert len(client.calls) == 1
        with factory() as session:
            persisted = session.get(QueryGovernanceActionPlan, plan_id)
            assert persisted.status == GovernancePlanStatus.VERIFIED.value
            assert persisted.executor_type == "human"
            assert persisted.executor_user_id == "operator-1"
            assert json.loads(persisted.response_json)["execution"]["groupId"] == "kg-1"
    finally:
        engine.dispose()


def test_human_can_execute_blacklist_tag_through_tag_gateway():
    engine, factory = _factory()
    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="batch-black-tag", brand="atlas",
                data_date=__import__("datetime").date(2026, 9, 20),
                query_text="裙子", status="completed",
            )
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(
                task_id=task.id, batch_id=task.batch_id, brand=task.brand,
                query_text=task.query_text, node_key="query_governance.kg_agent",
                action_type="kg_plan", idempotency_key="kg-black-tag-1",
                status=GovernancePlanStatus.PENDING.value,
                request_json=json.dumps({"evidence": {}}),
                response_json=json.dumps({"actions": [{
                    "actionType": "blacklist_tag",
                    "params": {"id": "node-1", "sceneId": "scene-1", "tagId": "tag-1", "isBlack": 1},
                }]}),
            )
            session.add(plan)
            session.commit()
            plan_id = plan.id
        client = FakeKGClient()
        result = QueryGovernanceKGActionExecutor(session_factory=factory, kg_client=client).execute(
            plan_id, user_id="operator-1")
        assert result["status"] == GovernancePlanStatus.VERIFIED.value
        assert client.calls[0][0]["actionType"] == "blacklist_tag"
        assert client.calls[0][0]["params"]["isBlack"] == 1
    finally:
        engine.dispose()


def test_individual_group_is_not_executable():
    engine, factory = _factory()
    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="batch-2",
                brand="atlas",
                data_date=__import__("datetime").date(2026, 9, 20),
                query_text="单品",
                status="completed",
            )
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(
                task_id=task.id,
                batch_id=task.batch_id,
                brand=task.brand,
                query_text=task.query_text,
                node_key="query_governance.kg_agent",
                action_type="kg_plan",
                idempotency_key="kg-plan-2",
                status=GovernancePlanStatus.PENDING.value,
                response_json=json.dumps(
                    {"causeGroups": [{"groupId": "individual-1", "rootCause": "individual", "actions": []}]}
                ),
            )
            session.add(plan)
            session.commit()
            plan_id = plan.id

        executor = QueryGovernanceKGActionExecutor(
            session_factory=factory,
            kg_client=FakeKGClient(),
            authorizer=lambda _intent: True,
        )
        result = executor.execute(plan_id, user_id="operator-1", group_id="individual-1")
        assert result["status"] == "skipped"
        assert result["reason"] == "individual_requires_blacklist_only"
    finally:
        engine.dispose()


def test_stale_kg_plan_is_rejected_before_any_write():
    engine, factory = _factory()
    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="batch-conflict",
                brand="atlas",
                data_date=__import__("datetime").date(2026, 9, 20),
                query_text="牛仔",
                status="completed",
            )
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(
                task_id=task.id,
                batch_id=task.batch_id,
                brand=task.brand,
                query_text=task.query_text,
                node_key="query_governance.kg_agent",
                action_type="kg_plan",
                idempotency_key="kg-plan-conflict",
                status=GovernancePlanStatus.PENDING.value,
                response_json=json.dumps({
                    "core_tags": ["牛仔"],
                    "graph_context": {"tags": {"牛仔": {"data": []}}},
                    "actions": [{
                        "actionType": "add_relationship",
                        "params": {"tagName": "牛仔裤", "responseTagName": "牛仔"},
                    }],
                }, ensure_ascii=False),
            )
            session.add(plan)
            session.commit()
            plan_id = plan.id

        class ConflictGuard:
            def assert_current(self, _plan):
                error = RuntimeError("KG_PLAN_CONFLICT: knowledge graph changed")
                error.error_code = "KG_PLAN_CONFLICT"
                raise error

        client = FakeKGClient()
        result = QueryGovernanceKGActionExecutor(
            session_factory=factory,
            kg_client=client,
            authorizer=lambda _intent: True,
            plan_guard=ConflictGuard(),
        ).execute(plan_id, user_id="operator-1")

        assert result["status"] == GovernancePlanStatus.FAILED.value
        assert result["errorCode"] == "KG_PLAN_CONFLICT"
        assert client.calls == []
        with factory() as session:
            persisted = session.get(QueryGovernanceActionPlan, plan_id)
            assert persisted.status == GovernancePlanStatus.FAILED.value
            assert persisted.failure_stage == "verification"
    finally:
        engine.dispose()


def test_plan_stays_pending_until_all_kg_cause_groups_are_verified():
    engine, factory = _factory()
    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="batch-3", brand="atlas", data_date=__import__("datetime").date(2026, 9, 20),
                query_text="多原因", status="completed",
            )
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(
                task_id=task.id, batch_id=task.batch_id, brand=task.brand, query_text=task.query_text,
                node_key="query_governance.kg_agent", action_type="kg_plan", idempotency_key="kg-plan-3",
                status=GovernancePlanStatus.PENDING.value,
                response_json=json.dumps({"causeGroups": [
                    {"groupId": "kg-1", "rootCause": "kg_issue", "actions": [{"actionType": "add_tag", "params": {"name": "A"}}]},
                    {"groupId": "kg-2", "rootCause": "kg_issue", "actions": [{"actionType": "add_tag", "params": {"name": "B"}}]},
                ]}),
            )
            session.add(plan)
            session.commit()
            plan_id = plan.id
        executor = QueryGovernanceKGActionExecutor(session_factory=factory, kg_client=FakeKGClient(), authorizer=lambda _intent: True)
        result = executor.execute(plan_id, user_id="operator-1", group_id="kg-1")
        assert result["status"] == GovernancePlanStatus.VERIFIED.value
        assert result["planStatus"] == GovernancePlanStatus.PENDING.value
        with factory() as session:
            assert session.get(QueryGovernanceActionPlan, plan_id).status == GovernancePlanStatus.PENDING.value
    finally:
        engine.dispose()


def test_disabled_or_unconfirmed_kg_plan_cannot_be_written_through_executor():
    engine, factory = _factory()
    try:
        with factory() as session:
            task = QueryGovernanceTask(batch_id='guard', brand='atlas', query_text='裙子',
                data_date=__import__('datetime').date(2026, 10, 7), status='completed')
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(task_id=task.id, batch_id='guard', brand='atlas',
                query_text='裙子', node_key='relevance', action_type='relevance_plan',
                idempotency_key='guard-plan', status='pending', response_json=json.dumps({
                    'causeGroups': [{'groupId': 'kg', 'rootCause': 'kg_issue',
                        'executionMode': 'disabled',
                        'actions': [{'actionType': 'delete_relationship', 'params': {'id': 'r1'}}]}]}))
            session.add(plan)
            session.commit()
            plan_id = plan.id
        client = FakeKGClient()
        executor = QueryGovernanceKGActionExecutor(session_factory=factory, kg_client=client)
        result = executor.execute(plan_id, group_id='kg', user_id='operator')
        assert result['status'] == 'skipped'
        assert result['reason'] == 'execution_disabled'
        assert client.calls == []
        with factory() as session:
            plan = session.get(QueryGovernanceActionPlan, plan_id)
            response = json.loads(plan.response_json)
            response['causeGroups'][0]['executionMode'] = 'human_approval'
            plan.response_json = json.dumps(response)
            session.commit()
        result = executor.execute(plan_id, group_id='kg', executor_type='ai_auto')
        assert result['reason'] == 'human_approval_required'
        assert client.calls == []
        result = executor.execute(plan_id, group_id='kg', user_id='operator')
        assert result['status'] == 'verified'
        assert len(client.calls) == 1
    finally:
        engine.dispose()


@__import__('pytest').mark.parametrize('write_data,expected_status,expected_writes', [
    ({'tagId': 'new-node'}, 'execution_unknown', 1),
    ({}, 'execution_unknown', 1),
])
def test_empty_readback_stops_dependent_write_even_with_write_response_id(write_data, expected_status, expected_writes):
    engine, factory = _factory()
    class Client:
        def __init__(self): self.writes = []
        def execute(self, action, external_request_id=None):
            self.writes.append(action)
            return {'code': 1, 'data': write_data}
        def snapshot(self, action): return {'data': []}
    try:
        with factory() as session:
            task = QueryGovernanceTask(batch_id='alias', brand='atlas', data_date=__import__('datetime').date(2026, 10, 12), query_text='花衬衫', status='completed')
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(task_id=task.id, batch_id='alias', brand='atlas', query_text='花衬衫', node_key='kg_agent', action_type='kg_plan', idempotency_key='alias', status='pending', request_json='{}', response_json=json.dumps({'actions': [
                {'actionType': 'add_tag', 'params': {'name': '花衬衫', 'sceneId': 's1', 'isRoot': 0}},
                {'actionType': 'add_relationship', 'dependsOn': [1], 'params': {'sceneId': 's1', 'tagId': '@step:1', 'tagName': '花衬衫', 'responseTagId': 'root', 'responseTagName': '印花衬衫', 'relationships': '902227138815852544', 'dimensionNames': '品类'}},
            ]}))
            session.add(plan)
            session.commit()
            identity = plan.id
        client = Client()
        executor = QueryGovernanceKGActionExecutor(session_factory=factory, kg_client=client)
        result = executor.execute(identity, user_id='operator')
        assert len(client.writes) == expected_writes
        assert result['status'] == expected_status
        if expected_status == 'verified':
            assert client.writes[1]['params']['tagId'] == 'new-node'
            executor.execute(identity, user_id='operator')
            assert len(client.writes) == 2
    finally:
        engine.dispose()


def test_step_reference_requires_explicit_dependency_before_any_write():
    import pytest
    with pytest.raises(ValueError, match='reference'):
        QueryGovernanceKGActionExecutor._normalize_actions([
            {'actionType': 'add_tag', 'params': {'name': '花衬衫'}},
            {'actionType': 'add_relationship', 'params': {'tagId': '@step:1'}},
        ])


def test_created_tag_reference_uses_independent_tag_query_readback_id():
    """A write response is not authoritative for the next KG step."""
    engine, factory = _factory()

    class Client:
        def __init__(self):
            self.writes = []

        def execute(self, action, external_request_id=None):
            self.writes.append(action)
            if action["actionType"] == "add_tag":
                return {"code": 1, "data": {"id": "write-only-id"}}
            return {"code": 1, "data": {"id": "relation-id"}}

        def snapshot(self, action):
            if action["actionType"] == "add_tag":
                return {"data": [{**action["params"], "id": "kg_tag/readback-id", "tagId": "business-id"}]}
            return {"data": [{**action["params"], "id": "relation-id"}]}

    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="readback", brand="atlas",
                data_date=__import__("datetime").date(2026, 10, 12),
                query_text="花衬衫", status="completed",
            )
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(
                task_id=task.id, batch_id="readback", brand="atlas",
                query_text="花衬衫", node_key="kg_agent", action_type="kg_plan",
                idempotency_key="readback", status="pending", request_json="{}",
                response_json=json.dumps({"actions": [
                    {"actionType": "add_tag", "params": {"name": "花衬衫", "sceneId": "s1", "isRoot": 0}},
                    {"actionType": "add_relationship", "dependsOn": [1], "params": {
                        "sceneId": "s1", "tagId": "@step:1", "tagName": "花衬衫",
                        "responseTagId": "root", "responseTagName": "印花衬衫",
                        "relationships": "902227138815852544", "dimensionNames": "品类",
                    }},
                ]}),
            )
            session.add(plan)
            session.commit()
            plan_id = plan.id
        client = Client()
        result = QueryGovernanceKGActionExecutor(session_factory=factory, kg_client=client).execute(
            plan_id, user_id="operator"
        )
        assert result["status"] == "verified"
        assert client.writes[1]["params"]["tagId"] == "readback-id"
        QueryGovernanceKGActionExecutor(session_factory=factory, kg_client=client).execute(plan_id, user_id="operator")
        assert len(client.writes) == 2
    finally:
        engine.dispose()


@__import__("pytest").mark.parametrize("rows", [[], [{"id": "a", "name": "花衬衫"}, {"id": "b", "name": "花衬衫"}]])
def test_tag_write_does_not_verify_when_readback_is_missing_or_ambiguous(rows):
    engine, factory = _factory()

    class Client:
        def execute(self, action, external_request_id=None):
            return {"code": 1, "data": {"id": "write-only-id"}}

        def snapshot(self, action):
            return {"data": rows}

    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="ambiguous", brand="atlas",
                data_date=__import__("datetime").date(2026, 10, 12),
                query_text="花衬衫", status="completed",
            )
            session.add(task)
            session.flush()
            plan = QueryGovernanceActionPlan(
                task_id=task.id, batch_id="ambiguous", brand="atlas",
                query_text="花衬衫", node_key="kg_agent", action_type="kg_plan",
                idempotency_key="ambiguous", status="pending", request_json="{}",
                response_json=json.dumps({"actions": [{
                    "actionType": "add_tag",
                    "params": {"name": "花衬衫", "sceneId": "s1", "isRoot": 0},
                }]}),
            )
            session.add(plan)
            session.commit()
            plan_id = plan.id
        result = QueryGovernanceKGActionExecutor(session_factory=factory, kg_client=Client()).execute(
            plan_id, user_id="operator"
        )
        assert result["status"] != "verified"
    finally:
        engine.dispose()
