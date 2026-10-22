"""Exercise detection, blacklist readback, KG persistence and manual execution."""
from datetime import date
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.governance.action_gateway import ActionGateway
from src.persistence.models import Base
from src.query_governance.async_tasks import QueryGovernanceAsyncTaskRepository, QueryGovernanceAsyncTaskService
from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
from src.query_governance.kg_agent import QueryGovernanceActionPlanRepository, QueryGovernanceKGWorker
from src.query_governance.kg_execution import QueryGovernanceKGActionExecutor
from src.query_governance.models import NodeResult, NodeStatus, QuerySpec, QueryGovernanceActionPlan, QueryGovernanceAsyncTask
from src.query_governance.relevance_actions import RelevanceBlacklistActionGateway
from src.query_governance.workflow import QueryGovernanceWorkflow


@pytest.mark.parametrize('kg_analysis_fails', [False, True])
def test_all_detection_errors_are_blacklisted_before_kg_analysis_and_kg_waits_for_confirmation(monkeypatch, kg_analysis_fails):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    log = []
    monkeypatch.setattr('src.query_governance.relevance_actions.save_action_log', log.append)
    events = []

    class Operational:
        def __init__(self): self.blacklisted = set()
        def add_to_blacklist(self, brand, product_ids, queries, external_request_id=None):
            events.append('blacklist_write')
            self.blacklisted.update(product_ids.split(";"))
            return True
        def get_top_query_action_status(self, **kwargs):
            events.append('blacklist_readback')
            assert set(kwargs['payload']['product_ids']) <= self.blacklisted
            return {'status': 'completed'}

    class Search:
        def mixed(self, brand, query):
            return [{'product_id': 'P1', 'ext': {'bucket': 'productIdsByCode'}},
                    {'product_id': 'P2', 'ext': {'bucket': 'productIdsByCode'}}]

    class Node:
        def run(self, spec, context): return NodeResult.executed({})

    class Detection:
        def run(self, spec, context):
            events.append('detection')
            return NodeResult.executed({'detected_product_ids': ['P1', 'P2'],
                'blacklist_candidates': [{'product_id': 'P1', 'reason': '类目错误'},
                                         {'product_id': 'P2', 'reason': '标签关系错误'}]})

    class RootAgent:
        def analyze(self, **kwargs):
            events.append('root_analysis')
            assert operational.blacklisted == {'P1', 'P2'}
            if kg_analysis_fails:
                raise ValueError('KG proposal invalid')
            return {'queryTags': [], 'kgContext': {}, 'causeGroups': [
                {'groupId': 'kg:wide', 'rootCause': 'kg_issue', 'productIds': ['P1', 'P2'],
                 'actions': [{'actionType': 'delete_relationship', 'params': {'id': 'R1'}}]},
            ]}

    class KGClient:
        # 权威回读契约：snapshot 返回图谱查询记录列表；delete_relationship 期望关系已不存在
        def snapshot(self, action): return {'code': 1, 'data': [], 'count': 0}
        def execute(self, action, external_request_id=None):
            events.append('kg_write')
            return {'code': 0, 'message': 'success'}

    operational = Operational()
    tasks = QueryGovernanceAsyncTaskService(QueryGovernanceAsyncTaskRepository(factory))
    store = SqlAlchemyCheckpointStore(factory)
    blacklist = RelevanceBlacklistActionGateway(operational_service=operational,
        fact_gateway=ActionGateway(session_factory=factory, authorizer=lambda intent: True))
    workflow = QueryGovernanceWorkflow(search=Search(), store=store, checkpoint=store,
        runners={'recall': Node(), 'reorder': Node(), 'relevance': Detection()},
        action_gateway=blacklist, async_tasks=tasks, relevance_independent_actions=True,
        relevance_blacklist_execution_mode='auto')
    try:
        run = workflow.run_batch([QuerySpec('q1', 'atlas', '裙子', batch_id='priority-e2e')],
                                 batch_id='priority-e2e', data_date=date(2026, 10, 7))['q1']
        assert run.nodes['relevance'].status is NodeStatus.EXECUTED
        assert operational.blacklisted == {'P1', 'P2'}
        assert [row['spu'] for row in log] == ['P1', 'P2']
        with factory() as session:
            blacklist_plan = session.scalars(select(QueryGovernanceActionPlan)).one()
            assert blacklist_plan.status == 'verified'
            assert blacklist_plan.executor_type == 'ai_auto'
            task_id = session.scalars(select(QueryGovernanceAsyncTask)).one().id
        kg_executor = QueryGovernanceKGActionExecutor(session_factory=factory,
            kg_client=KGClient(), authorizer=lambda intent: True)
        worker = QueryGovernanceKGWorker(async_service=tasks, agent=None,
            relevance_root_cause_agent=RootAgent(), agent_execution_modes={'relevance': 'auto'},
            kg_execution_modes={'kg_tag': 'human_approval', 'kg_relation': 'human_approval'},
            kg_action_executor=kg_executor,
            action_plan_repository=QueryGovernanceActionPlanRepository(factory))
        worker.process(task_id)
        assert events.index('blacklist_readback') < events.index('root_analysis')
        assert 'kg_write' not in events
        assert operational.blacklisted == {'P1', 'P2'}
        with factory() as session:
            async_task = session.get(QueryGovernanceAsyncTask, task_id)
            assert async_task.status == ('error' if kg_analysis_fails else 'plan_generated')
            plans = session.scalars(select(QueryGovernanceActionPlan).where(
                QueryGovernanceActionPlan.action_type == 'relevance_plan')).all()
            if kg_analysis_fails:
                assert plans == []
                return
            assert len(plans) == 1
            plan = plans[0]
            assert plan.status == 'pending'
            group = json.loads(plan.response_json)['causeGroups'][0]
            assert group['executionMode'] == 'human_approval'
            assert group['executable'] is True
            plan_id = plan.id
        result = kg_executor.execute(plan_id, group_id='kg:wide', user_id='operator')
        assert result['status'] == 'verified'
        assert events.count('kg_write') == 1
        kg_executor.execute(plan_id, group_id='kg:wide', user_id='operator')
        assert events.count('kg_write') == 1
        with factory() as session:
            plan = session.get(QueryGovernanceActionPlan, plan_id)
            assert plan.executor_type == 'human'
            assert plan.executor_user_id == 'operator'
    finally:
        engine.dispose()
