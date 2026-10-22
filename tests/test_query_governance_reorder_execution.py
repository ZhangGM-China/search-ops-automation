from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.governance.action_gateway import ActionGateway as FactActionGateway
from src.persistence.models import (
    ActionAttempt,
    ActionIntent,
    ActionReadback,
    ActionTerminalFact,
    Base,
)
from src.query_governance.action_execution import QueryReorderActionGateway
from src.query_governance.models import (
    NodeResult,
    NodeStatus,
    QueryGovernanceActionPlan,
    QueryGovernanceTask,
    QuerySpec,
)
from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
from src.query_governance.ports import NodeRunner
from src.query_governance.workflow import QueryGovernanceWorkflow
from src.llm.node_config import capture_snapshot
from src.query_governance.reorder_actions import (
    QueryReorderActionExecutor,
    QueryReorderActionVerifier,
)
from src.query_governance.vector_reorder import (
    VectorReorderClient,
    VectorReorderConfig,
    VectorReorderNodeRunner,
)


@dataclass
class _Operational:
    configs: dict[tuple[str, str], dict] = field(default_factory=dict)
    calls: list[tuple[str, object]] = field(default_factory=list)

    def find_agent_top_product_config(self, brand, query, filters=None):
        return self.configs.get((brand, query))

    def create_agent_top_product_config(self, brand, query, filters=None):
        config = {"id": f"config-{len(self.configs) + 1}", "keyword": query}
        self.configs[(brand, query)] = {**config, "products": []}
        self.calls.append(("create", config["id"]))
        return config["id"]

    def get_agent_top_products(self, brand, config_id, query=""):
        for config in self.configs.values():
            if config["id"] == config_id:
                return [{"taggingUid": item} for item in config["products"]]
        return []

    def delete_agent_top_products(self, brand, config_id, product_ids, query=""):
        self.calls.append(("delete", list(product_ids)))
        for config in self.configs.values():
            if config["id"] == config_id:
                config["products"] = [
                    item for item in config["products"] if item not in set(product_ids)
                ]

    def add_agent_top_products(self, brand, config_id, product_ids, query=""):
        self.calls.append(("add", list(product_ids)))
        for config in self.configs.values():
            if config["id"] == config_id:
                config["products"] = list(product_ids)

    def delete_agent_top_product_config(self, brand, config_id, query=""):
        self.calls.append(("delete_config", config_id))
        self.configs = {
            key: value
            for key, value in self.configs.items()
            if value["id"] != config_id
        }

    # 2.10 Query 自定义排序接口的测试适配器
    find_query_sort_config = find_agent_top_product_config
    create_query_sort_config = create_agent_top_product_config
    get_query_sort_products = get_agent_top_products
    delete_query_sort_config = delete_agent_top_product_config

    def save_query_sort_products(self, brand, config_id, products, query=""):
        self.calls.append(("add", [item["taggingUid"] for item in products]))
        for config in self.configs.values():
            if config["id"] == config_id:
                config["products"] = [item["taggingUid"] for item in products]


@dataclass
class _Search:
    operational: _Operational
    fallback: dict[str, list[str]] = field(default_factory=dict)
    failures: set[str] = field(default_factory=set)
    forced: dict[str, list[str]] = field(default_factory=dict)

    def mixed(self, brand, query):
        if query in self.failures:
            raise RuntimeError("Mixed unavailable")
        if query in self.forced:
            ids = self.forced[query]
        else:
            config = self.operational.configs.get((brand, query))
            ids = config["products"] if config is not None and config["products"] else self.fallback.get(query, [])
        return [{"productId": product_id} for product_id in ids]


def _action():
    return {
        "type": "query_reorder",
        "query_id": "q1",
        "expected_product_ids": ["P-2", "P-1"],
        "preference_description": "偏好通勤",
        "request": {"query": "裙子"},
        "batch_id": "batch-1",
    }


def _persist_task(session_factory, *, batch_id="batch-1", query="裙子"):
    with session_factory() as session:
        session.add(
            QueryGovernanceTask(
                batch_id=batch_id,
                brand="atlas",
                data_date=date(2026, 8, 31),
                query_text=query,
                query_rank=1,
                status="running",
            )
        )
        session.commit()


def test_vector_node_emits_automatic_reorder_action():
    class _Response:
        status_code = 200

        def json(self):
            return {"result": [{"taggingUid": "P-2"}, {"taggingUid": "P-1"}]}

    runner = VectorReorderNodeRunner(
        client=VectorReorderClient(request_func=lambda *args, **kwargs: _Response()),
        config=VectorReorderConfig(product_limit=2, concurrency=1),
    )

    result = runner.run(
        QuerySpec("q1", "atlas", "裙子", batch_id="batch-1"),
        {"preference_result": {"should_reorder": True, "preference_description": "偏好通勤"}},
    )

    assert result.status is NodeStatus.EXECUTED
    assert result.action == {
        "type": "query_reorder",
        "query_id": "q1",
        "expected_product_ids": ["P-2", "P-1"],
        "preference_description": "偏好通勤",
        "request": result.result["request"],
        "batch_id": "batch-1",
    }


def test_reorder_executor_writes_configured_n_and_restores_before_state():
    operational = _Operational(
        configs={("atlas", "裙子"): {"id": "config-1", "products": ["P-0"]}}
    )
    executor = QueryReorderActionExecutor(operational)
    spec = QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")

    execution = executor.execute(spec, _action(), {"external_request_id": "req-1"})
    assert execution["request_id"] == "req-1"
    assert execution["before_product_ids"] == ["P-0"]
    assert operational.configs[("atlas", "裙子")]["products"] == ["P-2", "P-1"]

    rollback = executor.rollback(spec, _action(), execution, {})
    assert rollback["status"] == "success"
    assert operational.configs[("atlas", "裙子")]["products"] == ["P-0"]


def test_reorder_executor_uses_query_custom_sort_api_not_agent_api():
    class _QuerySortOnly(_Operational):
        find_agent_top_product_config = None
        create_agent_top_product_config = None
        get_agent_top_products = None
        delete_agent_top_products = None
        add_agent_top_products = None
        delete_agent_top_product_config = None

        def find_query_sort_config(self, brand, query, filters=None):
            return self.configs.get((brand, query))

        def create_query_sort_config(self, brand, query, filters=None):
            config = {"id": "query-config-1", "keyword": query, "products": []}
            self.configs[(brand, query)] = config
            return config["id"]

        def get_query_sort_products(self, brand, config_id, query=""):
            return [{"taggingUid": item, "position": index + 1} for index, item in enumerate(self.configs[(brand, query)]["products"])]

        def save_query_sort_products(self, brand, config_id, products, query=""):
            self.configs[(brand, query)]["products"] = [item["taggingUid"] for item in products]

        def delete_query_sort_config(self, brand, config_id, query=""):
            self.configs.pop((brand, query), None)

    operational = _QuerySortOnly()
    execution = QueryReorderActionExecutor(operational).execute(
        QuerySpec("q1", "atlas", "裙子", batch_id="batch-1"), _action(), {}
    )
    assert execution["config_id"] == "query-config-1"
    assert operational.configs[("atlas", "裙子")]["products"] == ["P-2", "P-1"]


def test_reorder_gateway_persists_facts_and_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    operational = _Operational()
    search = _Search(operational, fallback={"裙子": ["P-2", "P-1"]})
    gateway = QueryReorderActionGateway(
        search=search,
        executor=QueryReorderActionExecutor(operational),
        verifier=QueryReorderActionVerifier(),
        fact_gateway=FactActionGateway(
            session_factory=session_factory,
            authorizer=lambda _: True,
        ),
    )
    spec = QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")
    try:
        first = gateway.execute_and_verify(spec, _action(), {})
        second = gateway.execute_and_verify(spec, _action(), {})

        assert first.status is NodeStatus.EXECUTED
        assert second.status is NodeStatus.EXECUTED
        assert [call[0] for call in operational.calls].count("add") == 1
        with Session(engine) as db:
            intent = db.scalars(select(ActionIntent)).one()
            attempt = db.scalars(select(ActionAttempt)).one()
            readback = db.scalars(select(ActionReadback)).one()
            terminal = db.scalars(select(ActionTerminalFact)).one()
        assert intent.action_type == "query_reorder"
        assert attempt.external_request_id == intent.idempotency_key
        assert readback.authoritative == 1
        assert json.loads(readback.observed_payload)["status"] == "success"
        assert terminal.terminal_code == "authoritative_readback_success"
    finally:
        engine.dispose()


def test_reorder_gateway_rolls_back_when_mixed_does_not_match():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    _persist_task(session_factory)
    operational = _Operational(
        configs={("atlas", "裙子"): {"id": "config-1", "products": ["P-0"]}}
    )
    search = _Search(operational, forced={"裙子": ["P-0"]})
    gateway = QueryReorderActionGateway(
        search=search,
        executor=QueryReorderActionExecutor(operational),
        verifier=QueryReorderActionVerifier(),
        fact_gateway=FactActionGateway(
            session_factory=session_factory,
            authorizer=lambda _: True,
        ),
    )
    spec = QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")
    try:
        result = gateway.execute_and_verify(spec, _action(), {})
        assert result.status is NodeStatus.ERROR
        assert result.error_code == "ACTION_READBACK_FAILED"
        assert operational.configs[("atlas", "裙子")]["products"] == ["P-0"]
        with Session(engine) as db:
            plan = db.scalars(select(QueryGovernanceActionPlan)).one()
        assert plan.status == "rolled_back"
        assert plan.executor_type == "ai_auto"
        assert plan.failure_stage == "verification"
        assert plan.error_code == "ACTION_READBACK_FAILED"
        response = json.loads(plan.response_json)
        assert response["verification"]["rollback"]["status"] == "success"
    finally:
        engine.dispose()


def test_reorder_gateway_keeps_partial_position_match_without_rollback():
    operational = _Operational()
    gateway = QueryReorderActionGateway(
        search=_Search(operational, forced={"裙子": ["P-2", "P-other"]}),
        executor=QueryReorderActionExecutor(operational),
        verifier=QueryReorderActionVerifier(),
        fact_gateway=FactActionGateway(authorizer=lambda _: True),
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子", batch_id="batch-1"), _action(), {}
    )

    assert result.status is NodeStatus.EXECUTED
    assert operational.configs[("atlas", "裙子")]["products"] == ["P-2", "P-1"]
    assert result.result["verification"]["verification"]["matched_count"] == 1


def test_reorder_verifier_compares_only_expected_top_n_in_order():
    verification = QueryReorderActionVerifier()(
        QuerySpec("q1", "atlas", "裙子"),
        _action(),
        [{"productId": "P-2"}, {"productId": "P-1"}, {"productId": "P-9"}],
        {"request_id": "req-1"},
    )
    assert verification["matched"] is True
    assert verification["actual_product_ids"] == ["P-2", "P-1", "P-9"]


def test_reorder_verifier_accepts_partial_position_matches():
    verification = QueryReorderActionVerifier()(
        QuerySpec("q1", "atlas", "裙子"),
        {"expected_product_ids": ["P-1", "P-2", "P-3"]},
        [
            {"productId": "P-1"},
            {"productId": "P-other"},
            {"productId": "P-3"},
        ],
        None,
    )

    assert verification["matched"] is True
    assert verification["partial"] is True
    assert verification["matched_count"] == 2
    assert verification["expected_count"] == 3
    assert verification["matched_positions"] == [1, 3]
    assert verification["unmatched_positions"] == [2]


def test_reorder_verifier_rejects_when_no_position_matches():
    verification = QueryReorderActionVerifier()(
        QuerySpec("q1", "atlas", "裙子"),
        {"expected_product_ids": ["P-1", "P-2"]},
        [{"productId": "P-other-1"}, {"productId": "P-other-2"}],
        None,
    )

    assert verification["matched"] is False
    assert verification["partial"] is False
    assert verification["matched_count"] == 0
    assert verification["matched_positions"] == []
    assert verification["unmatched_positions"] == [1, 2]


def test_reorder_gateway_marks_mixed_unavailable_inconclusive_without_rollback():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    _persist_task(session_factory)
    operational = _Operational()
    gateway = QueryReorderActionGateway(
        search=_Search(operational, failures={"裙子"}),
        executor=QueryReorderActionExecutor(operational),
        verifier=QueryReorderActionVerifier(),
        fact_gateway=FactActionGateway(session_factory=session_factory, authorizer=lambda _: True),
    )

    try:
        result = gateway.execute_and_verify(
            QuerySpec("q1", "atlas", "裙子", batch_id="batch-1"), _action(), {}
        )

        assert result.status is NodeStatus.ERROR
        assert result.error_code == "ACTION_READBACK_INCONCLUSIVE"
        assert operational.configs[("atlas", "裙子")]["products"] == ["P-2", "P-1"]
        with Session(engine) as db:
            plan = db.scalars(select(QueryGovernanceActionPlan)).one()
            readback = db.scalars(select(ActionReadback)).one()
            terminal = db.scalars(select(ActionTerminalFact)).one()
        assert plan.status == "execution_unknown"
        assert plan.error_code == "ACTION_READBACK_INCONCLUSIVE"
        assert json.loads(plan.response_json)["verification"]["reason"] == "readback_unavailable"
        assert readback.status == "failed"
        assert terminal.status == "inconclusive"
    finally:
        engine.dispose()


def test_reorder_executor_removes_new_config_when_write_fails():
    class _FailingOperational(_Operational):
        def save_query_sort_products(self, brand, config_id, products, query=""):
            for config in self.configs.values():
                if config["id"] == config_id:
                    config["products"] = [item["taggingUid"] for item in products]
            raise RuntimeError("write failed")

    operational = _FailingOperational()
    executor = QueryReorderActionExecutor(operational)

    with pytest.raises(RuntimeError, match="write failed"):
        executor.execute(
            QuerySpec("q1", "atlas", "裙子", batch_id="batch-1"),
            _action(),
            {"external_request_id": "req-1"},
        )

    assert operational.configs == {}


def test_reorder_executor_restores_existing_empty_config_when_write_fails():
    class _FailingOperational(_Operational):
        def save_query_sort_products(self, brand, config_id, products, query=""):
            for config in self.configs.values():
                if config["id"] == config_id:
                    config["products"] = [item["taggingUid"] for item in products]
            raise RuntimeError("write failed")

    operational = _FailingOperational(
        configs={("atlas", "裙子"): {"id": "config-1", "products": []}}
    )
    with pytest.raises(RuntimeError, match="write failed"):
        QueryReorderActionExecutor(operational).execute(
            QuerySpec("q1", "atlas", "裙子", batch_id="batch-1"),
            _action(),
            {},
        )
    assert operational.configs[("atlas", "裙子")]["products"] == []


def test_reorder_gateway_retries_mixed_until_expected_order_is_visible():
    class _DelayedSearch:
        def __init__(self):
            self.calls = 0

        def mixed(self, brand, query):
            self.calls += 1
            ids = ["P-0"] if self.calls == 1 else ["P-2", "P-1"]
            return [{"productId": item} for item in ids]

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    operational = _Operational()
    search = _DelayedSearch()
    sleeps: list[float] = []
    gateway = QueryReorderActionGateway(
        search=search,
        executor=QueryReorderActionExecutor(operational),
        verifier=QueryReorderActionVerifier(),
        fact_gateway=FactActionGateway(
            session_factory=session_factory,
            authorizer=lambda _: True,
        ),
        readback_attempts=2,
        readback_interval_seconds=0.25,
        sleeper=sleeps.append,
    )
    try:
        result = gateway.execute_and_verify(
            QuerySpec("q1", "atlas", "裙子", batch_id="batch-1"),
            _action(),
            {},
        )
        assert result.status is NodeStatus.EXECUTED
        assert search.calls == 2
        assert sleeps == [0.25]
        assert result.result["verification"]["attempt"] == 2
        assert not any(call[0] == "delete_config" for call in operational.calls)
    finally:
        engine.dispose()


def test_reorder_gateway_recovery_uses_mixed_before_retrying():
    operational = _Operational()
    spec = QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")
    gateway = QueryReorderActionGateway(
        search=_Search(operational, forced={"裙子": ["P-2", "P-1"]}),
        executor=QueryReorderActionExecutor(operational),
        verifier=QueryReorderActionVerifier(),
        fact_gateway=FactActionGateway(authorizer=lambda _: True),
    )
    completed = gateway.recover_interrupted(spec, "reorder", _action(), {})
    assert completed.result is not None
    assert completed.result.status is NodeStatus.EXECUTED

    gateway.search = _Search(operational, forced={"裙子": ["P-0"]})
    retry = gateway.recover_interrupted(spec, "reorder", _action(), {})
    assert retry.status.value == "safe_to_retry"

    gateway.search = _Search(operational, failures={"裙子"})
    unresolved = gateway.recover_interrupted(spec, "reorder", _action(), {})
    assert unresolved.status.value == "unresolved"
    assert unresolved.error_code == "ACTION_RECOVERY_READBACK_FAILED"


def test_reorder_gateway_rejects_invalid_runtime_limits_and_empty_action():
    operational = _Operational()
    fact_gateway = FactActionGateway(authorizer=lambda _: True)
    with pytest.raises(ValueError, match="readback_attempts"):
        QueryReorderActionGateway(
            search=_Search(operational),
            executor=QueryReorderActionExecutor(operational),
            verifier=QueryReorderActionVerifier(),
            fact_gateway=fact_gateway,
            readback_attempts=0,
        )
    gateway = QueryReorderActionGateway(
        search=_Search(operational),
        executor=QueryReorderActionExecutor(operational),
        verifier=QueryReorderActionVerifier(),
        fact_gateway=fact_gateway,
    )
    with pytest.raises(Exception, match="至少需要一个商品"):
        gateway.execute_and_verify(
            QuerySpec("q1", "atlas", "裙子"),
            {"type": "query_reorder", "expected_product_ids": []},
            {},
        )


def test_workflow_runs_relevance_for_other_queries_when_one_reorder_fails():
    class _Response:
        status_code = 200

        def __init__(self, query):
            self.query = query

        def json(self):
            if self.query == "外套":
                raise RuntimeError("vector timeout")
            return {"result": [{"taggingUid": "P-2"}, {"taggingUid": "P-1"}]}

    class _Post:
        def __call__(self, url, *, json, headers, timeout):
            return _Response(json["query"])

    class _Runner(NodeRunner):
        def __init__(self, node, events):
            self.node = node
            self.events = events

        def run(self, spec, context):
            self.events.append(f"{self.node}:{spec.query}")
            return NodeResult.executed({"node": self.node})

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    operational = _Operational()
    search = _Search(
        operational,
        fallback={"裙子": ["P-0"], "外套": ["Q-0"]},
    )
    action_gateway = QueryReorderActionGateway(
        search=search,
        executor=QueryReorderActionExecutor(operational),
        verifier=QueryReorderActionVerifier(),
        fact_gateway=FactActionGateway(
            session_factory=session_factory,
            authorizer=lambda _: True,
        ),
    )
    events: list[str] = []
    workflow = QueryGovernanceWorkflow(
        search=search,
        store=SqlAlchemyCheckpointStore(session_factory),
        checkpoint=SqlAlchemyCheckpointStore(session_factory),
        runners={
            "recall": _Runner("recall", events),
            "reorder": VectorReorderNodeRunner(
                client=VectorReorderClient(request_func=_Post()),
                config=VectorReorderConfig(product_limit=2, concurrency=1),
            ),
            "relevance": _Runner("relevance", events),
        },
        action_gateway=action_gateway,
        model_config_snapshot=capture_snapshot(
            node_keys=(
                "query_governance.kg_agent",
                "query_governance.preference_generation",
                "query_governance.relevance_multimodal",
                "query_governance.relevance_rules",
            )
        ),
    )
    specs = [
        QuerySpec("q1", "atlas", "裙子", batch_id="batch-1"),
        QuerySpec("q2", "atlas", "外套", batch_id="batch-1"),
    ]
    try:
        results = workflow.run_batch(
            specs,
            batch_id="batch-1",
            data_date=date(2026, 8, 31),
            preference_results={
                "q1": {"should_reorder": True, "preference_description": "偏好通勤"},
                "q2": {"should_reorder": True, "preference_description": "偏好通勤"},
            },
        )
        assert results["q1"].nodes["reorder"].status is NodeStatus.EXECUTED
        assert results["q2"].nodes["reorder"].status is NodeStatus.ERROR
        assert "relevance:裙子" in events
        assert "relevance:外套" in events
        with Session(engine) as db:
            assert db.scalars(select(ActionIntent)).all()
            action_plan = db.scalars(select(QueryGovernanceActionPlan)).one()
        assert action_plan.status == "verified"
        assert action_plan.executor_type == "ai_auto"
        assert action_plan.executed_at is not None
        assert json.loads(action_plan.request_json)["expected_product_ids"] == [
            "P-2",
            "P-1",
        ]
        assert json.loads(action_plan.response_json)["verification"]["status"] == "success"
    finally:
        engine.dispose()
