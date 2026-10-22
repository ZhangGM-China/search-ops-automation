from __future__ import annotations

from datetime import date

from src.query_governance.models import (
    ActionRecoveryStatus,
    NodeStatus,
    QueryGovernanceActionPlan,
    QueryGovernanceTask,
    QuerySpec,
)
from src.query_governance.relevance_actions import (
    RelevanceActionPlanRepository,
    RelevanceBlacklistActionGateway,
)
from src.query_governance.action_execution import RoutingActionGateway
from src.integrations.operational_service import OperationalService
from src.governance.action_gateway import ActionGateway as FactActionGateway
from src.persistence import ActionIntent, Base
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


class _Operational:
    def __init__(self, status: str):
        self.status = status
        self.calls: list[dict] = []

    def get_top_query_action_status(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": self.status}

    def add_to_blacklist(self, brand, product_ids, queries, external_request_id=None):
        self.calls.append({
            "brand": brand,
            "product_ids": product_ids,
            "queries": queries,
            "external_request_id": external_request_id,
        })
        return True


class _FactGateway:
    def __init__(self, readback_status: str = "completed"):
        self.readback_status = readback_status
        self.intent = None

    def execute(self, intent, executor, *, readback, authoritative_readback_on_success):
        self.intent = intent
        context = type("Context", (), {"external_request_id": intent.idempotency_key})()
        execution = executor(context)
        observed = readback(context)
        assert authoritative_readback_on_success is True
        if self.readback_status != "completed":
            return {"status": self.readback_status}
        return {
            "status": "success",
            "execution": execution,
            "readback": observed,
        }


def test_relevance_blacklist_interrupted_action_recovers_from_authoritative_readback():
    operational = _Operational("completed")
    gateway = RelevanceBlacklistActionGateway(operational_service=operational)
    spec = QuerySpec("q1", "atlas", "裙子")
    action = {"type": "product_blacklist", "product_ids": ["p1"]}

    recovered = gateway.recover_interrupted(spec, "relevance", action, {})

    assert recovered.status is ActionRecoveryStatus.COMPLETED
    assert recovered.result is not None
    assert recovered.result.status is NodeStatus.EXECUTED
    assert recovered.result.result["recovered"] is True
    assert operational.calls[-1]["action_type"] == "product_blacklist"
    assert operational.calls[0]["payload"]["product_ids"] == ["p1"]


def test_relevance_blacklist_action_persists_product_action_log_after_readback(monkeypatch):
    operational = _Operational("completed")
    saved = []
    monkeypatch.setattr("src.query_governance.relevance_actions.save_action_log", saved.append)
    gateway = RelevanceBlacklistActionGateway(operational_service=operational, fact_gateway=_FactGateway())
    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子"),
        {"type": "product_blacklist", "product_ids": ["p1"], "product_names": {"p1": "测试裙子"}, "reasons": {"p1": "不相关"}},
        {},
    )
    assert result.status is NodeStatus.EXECUTED
    assert saved == [{"brand": "atlas", "query": "裙子", "spu": "p1", "name": "测试裙子", "image_url": "", "type": 1, "reason": "不相关"}]


def test_relevance_blacklist_action_plan_is_recorded_and_marked_executed():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        task = QueryGovernanceTask(
            batch_id="batch-1", brand="atlas", data_date=date(2026, 9, 15), query_text="裙子", status="running"
        )
        session.add(task)
        session.commit()

    gateway = RelevanceBlacklistActionGateway(
        operational_service=_Operational("completed"),
        fact_gateway=FactActionGateway(session_factory=session_factory, authorizer=lambda _: True),
    )
    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子", batch_id="batch-1"),
        {"type": "product_blacklist", "product_ids": ["p1"], "reasons": {"p1": "不相关"}},
        {},
    )

    assert result.status is NodeStatus.EXECUTED
    with session_factory() as session:
        plans = session.query(QueryGovernanceActionPlan).all()
        assert len(plans) == 1
        assert plans[0].action_type == "product_blacklist"
        assert plans[0].node_key == "relevance"
        assert plans[0].status == "verified"
        assert plans[0].executor_type == "ai_auto"
        assert plans[0].executed_at is not None


def test_human_approval_blacklist_is_routed_to_pending_plan_without_external_write():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        session.add(
            QueryGovernanceTask(
                batch_id="batch-human",
                brand="atlas",
                data_date=date(2026, 9, 20),
                query_text="男士牛仔裤",
                status="running",
            )
        )
        session.commit()
    operational = _Operational("completed")
    relevance_gateway = RelevanceBlacklistActionGateway(
        operational_service=operational,
        fact_gateway=FactActionGateway(session_factory=session_factory, authorizer=lambda _: True),
    )
    routing = RoutingActionGateway({"product_blacklist": relevance_gateway})

    result = routing.defer(
        QuerySpec("q-human", "atlas", "男士牛仔裤", batch_id="batch-human"),
        {
            "type": "product_blacklist",
            "product_ids": ["P1"],
            "execution_mode": "human_approval",
            "reasons": {"P1": "与 Query 不相关"},
        },
        {},
    )

    assert result.status is NodeStatus.EXECUTED
    assert operational.calls == []
    with session_factory() as session:
        plan = session.scalars(select(QueryGovernanceActionPlan)).one()
        assert plan.status == "pending"
        assert plan.executor_type == "human"
        assert '"execution_mode":"human_approval"' in plan.request_json
        assert plan.executed_at is None


def test_relevance_blacklist_action_persists_product_image_after_readback(monkeypatch):
    operational = _Operational("completed")
    saved = []
    monkeypatch.setattr("src.query_governance.relevance_actions.save_action_log", saved.append)
    gateway = RelevanceBlacklistActionGateway(operational_service=operational, fact_gateway=_FactGateway())

    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子"),
        {
            "type": "product_blacklist",
            "product_ids": ["p1"],
            "product_names": {"p1": "测试裙子"},
            "product_images": {"p1": "https://img.test/p1.jpg"},
            "reasons": {"p1": "不相关"},
        },
        {},
    )

    assert result.status is NodeStatus.EXECUTED
    assert saved[0]["image_url"] == "https://img.test/p1.jpg"


def test_relevance_blacklist_interrupted_action_is_safe_to_retry_when_not_found():
    operational = _Operational("not_found")
    gateway = RelevanceBlacklistActionGateway(operational_service=operational)

    recovered = gateway.recover_interrupted(
        QuerySpec("q1", "atlas", "裙子"),
        "relevance",
        {"type": "product_blacklist", "product_ids": ["p1"]},
        {},
    )

    assert recovered.status is ActionRecoveryStatus.SAFE_TO_RETRY


def test_relevance_blacklist_interrupted_action_stays_unresolved_on_unknown_readback():
    gateway = RelevanceBlacklistActionGateway(
        operational_service=_Operational("unknown")
    )

    recovered = gateway.recover_interrupted(
        QuerySpec("q1", "atlas", "裙子"),
        "relevance",
        {"type": "product_blacklist", "product_ids": ["p1"]},
        {},
    )

    assert recovered.status is ActionRecoveryStatus.UNRESOLVED
    assert recovered.error_code == "RELEVANCE_BLACKLIST_RECOVERY_UNKNOWN"


def test_relevance_blacklist_action_uses_authorized_gateway_and_readback():
    operational = _Operational("completed")
    facts = _FactGateway()
    gateway = RelevanceBlacklistActionGateway(
        operational_service=operational,
        fact_gateway=facts,
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子"),
        {"type": "product_blacklist", "product_ids": ["p1"], "reasons": {"p1": "不相关"}},
        {},
    )

    assert result.status is NodeStatus.EXECUTED
    assert facts.intent.action_type == "product_blacklist"
    assert facts.intent.payload["product_ids"] == ["p1"]
    assert operational.calls[-1]["action_type"] == "product_blacklist"


def test_relevance_blacklist_retries_across_batches_but_is_idempotent_within_batch():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    operational = _Operational("completed")
    gateway = RelevanceBlacklistActionGateway(
        operational_service=operational,
        fact_gateway=FactActionGateway(
            session_factory=session_factory,
            authorizer=lambda _: True,
        ),
    )
    action_one = {
        "type": "product_blacklist",
        "product_ids": ["p1"],
        "reasons": {"p1": "图片显示商品为运动鞋，不是单鞋"},
    }
    action_two = {
        "type": "product_blacklist",
        "product_ids": ["p1"],
        "reasons": {"p1": "商品为运动鞋，不属于单鞋品类"},
    }
    try:
        first = gateway.execute_and_verify(
            QuerySpec("q1", "atlas", "单鞋 女士", batch_id="batch-1"), action_one, {}
        )
        same_batch = gateway.execute_and_verify(
            QuerySpec("q1", "atlas", "单鞋 女士", batch_id="batch-1"), action_two, {}
        )
        next_batch = gateway.execute_and_verify(
            QuerySpec("q1", "atlas", "单鞋 女士", batch_id="batch-2"), action_two, {}
        )

        assert first.status is NodeStatus.EXECUTED
        assert same_batch.status is NodeStatus.EXECUTED
        assert next_batch.status is NodeStatus.EXECUTED
        assert len([call for call in operational.calls if "queries" in call]) == 2
        with Session(engine) as db:
            intents = db.scalars(select(ActionIntent)).all()
            assert len(intents) == 2
            assert all("reasons" in intent.payload_json for intent in intents)
    finally:
        engine.dispose()


def test_relevance_blacklist_partial_readback_persists_action_logs_for_matched_products_only(monkeypatch):
    class _PartialGateway:
        def execute(self, intent, executor, *, readback, authoritative_readback_on_success):
            return {
                "status": "failed",
                "readback": {
                    "status": "unknown",
                    "reason": "partial_product_blacklist_readback",
                    "response": {
                        "readback_source": "product_list_blacklist_filter",
                        "matched_product_ids": ["p1"],
                        "unmatched_product_ids": ["p2"],
                    },
                },
            }

    saved = []
    monkeypatch.setattr("src.query_governance.relevance_actions.save_action_log", saved.append)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        task = QueryGovernanceTask(
            batch_id="batch-partial", brand="atlas", data_date=date(2026, 9, 15), query_text="裙子", status="running"
        )
        session.add(task)
        session.commit()

    gateway = RelevanceBlacklistActionGateway(
        operational_service=_Operational("completed"),
        fact_gateway=_PartialGateway(),
        action_plan_repository=RelevanceActionPlanRepository(session_factory),
    )
    result = gateway.execute_and_verify(
        QuerySpec("q1", "atlas", "裙子", batch_id="batch-partial"),
        {
            "type": "product_blacklist",
            "product_ids": ["p1", "p2"],
            "product_names": {"p1": "匹配商品", "p2": "未匹配商品"},
            "reasons": {"p1": "不相关1", "p2": "不相关2"},
        },
        {},
    )

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "RELEVANCE_BLACKLIST_READBACK_FAILED"
    # 验证仅为匹配成功的 p1 记录了 ActionLog，而未匹配成功的 p2 没有保存
    assert len(saved) == 1
    assert saved[0]["spu"] == "p1"
    assert saved[0]["name"] == "匹配商品"
    assert saved[0]["type"] == 1

    with session_factory() as session:
        plans = session.query(QueryGovernanceActionPlan).all()
        assert len(plans) == 1
        assert plans[0].status == "execution_unknown"
        assert plans[0].error_code == "RELEVANCE_BLACKLIST_READBACK_FAILED"


class _Response:
    status_code = 200
    text = "{}"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _service_for_blacklist_readback(response_payload):
    service = OperationalService.__new__(OperationalService)
    service._get_base_url = lambda _brand: "https://example.test"
    service._get_headers = lambda _brand: {"Authorization": "Bearer test"}
    calls = []

    def call_api(method, url, brand, query=None, **kwargs):
        calls.append((method, url, brand, query, kwargs))
        return _Response(response_payload)

    service._call_api = call_api
    return service, calls


def test_blacklist_readback_uses_query_list_filter_and_confirms_all_targets():
    service, calls = _service_for_blacklist_readback(
        {
            "code": 0,
            "data": {
                "list": [
                    {"productId": "p1", "status": 0, "blackList": "宫廷"},
                    {"productId": "p2", "status": 0, "blackList": "宫廷"},
                ],
                "total": 2,
            },
        }
    )

    result = service.get_top_query_action_status(
        action_type="product_blacklist",
        external_request_id="req-1",
        payload={"brand": "atlas", "query": "宫廷 女士", "product_ids": ["p1", "p2"]},
    )

    assert result["status"] == "completed"
    assert calls[0][4]["json"] == {
        "blackList": "宫廷",
        "pageNo": 1,
        "pageSize": 100,
    }


def test_blacklist_readback_reports_partial_targets_from_query_list():
    service, _ = _service_for_blacklist_readback(
        {"code": 0, "data": {"list": [{"productId": "p1"}], "total": 1}}
    )

    result = service.get_top_query_action_status(
        action_type="product_blacklist",
        external_request_id="req-1",
        payload={"brand": "harbor", "query": "瑜伽裤", "product_ids": ["p1", "p2"]},
    )

    assert result["status"] == "unknown"
    assert result["reason"] == "partial_product_blacklist_readback"
    assert result["response"] == {
        "readback_source": "product_list_blacklist_filter",
        "matched_product_ids": ["p1"],
        "unmatched_product_ids": ["p2"],
    }


def test_blacklist_readback_reports_list_api_failure():
    service, _ = _service_for_blacklist_readback(
        {"code": 500, "msg": "temporary failure", "data": {}}
    )

    result = service.get_top_query_action_status(
        action_type="product_blacklist",
        external_request_id="req-1",
        payload={"brand": "harbor", "query": "瑜伽裤", "product_ids": ["p1"]},
    )

    assert result["status"] == "unknown"
    assert result["reason"] == "blacklist_list_readback_business_error"


class _TopSortOperational(_Operational):
    def __init__(
        self,
        *,
        status: str = "completed",
        config: dict | None = None,
        pinned_products: list[dict] | None = None,
        blacklist_succeeds: bool = True,
    ):
        super().__init__(status)
        self.config = config
        self.pinned_products = list(pinned_products or [])
        self.blacklist_succeeds = blacklist_succeeds

    def find_query_sort_config(self, brand, query, filters=None):
        self.calls.append({"op": "find_query_sort_config", "brand": brand, "query": query, "filters": filters})
        return self.config

    def get_query_sort_products(self, brand, config_id, query=""):
        self.calls.append({"op": "get_query_sort_products", "brand": brand, "config_id": config_id, "query": query})
        return list(self.pinned_products)

    def save_query_sort_products(self, brand, config_id, products, query=""):
        self.calls.append({
            "op": "save_query_sort_products",
            "brand": brand,
            "config_id": config_id,
            "products": [dict(item) for item in products],
            "query": query,
        })

    def delete_query_sort_config(self, brand, config_id, query=""):
        self.calls.append({
            "op": "delete_query_sort_config",
            "brand": brand,
            "config_id": config_id,
            "query": query,
        })

    def create_query_sort_config(self, brand, query, filters=None):
        self.calls.append({
            "op": "create_query_sort_config",
            "brand": brand,
            "query": query,
            "filters": filters,
        })
        return "cfg-restored"

    def add_to_blacklist(self, brand, product_ids, queries, external_request_id=None):
        self.calls.append({
            "op": "add_to_blacklist",
            "brand": brand,
            "product_ids": product_ids,
            "queries": queries,
            "external_request_id": external_request_id,
        })
        return self.blacklist_succeeds


def test_relevance_blacklist_unpins_top_bucket_product_and_reindexes_remaining_before_blacklist(monkeypatch):
    monkeypatch.setattr("src.query_governance.relevance_actions.save_action_log", lambda _row: None)
    operational = _TopSortOperational(
        config={"id": "cfg-101", "keyword": "卫衣", "filters": []},
        pinned_products=[
            {"taggingUid": "p1", "position": 1, "productName": "错误置顶商品"},
            {"taggingUid": "p2", "position": 2, "productName": "正常置顶商品2"},
            {"taggingUid": "p3", "position": 3, "productName": "正常置顶商品3"},
        ],
    )
    gateway = RelevanceBlacklistActionGateway(
        operational_service=operational,
        fact_gateway=_FactGateway(),
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "harbor", "卫衣"),
        {
            "type": "product_blacklist",
            "product_ids": ["p1"],
            "product_buckets": {"p1": "top"},
            "reasons": {"p1": "与卫衣无关"},
        },
        {},
    )

    assert result.status is NodeStatus.EXECUTED
    ops = [call.get("op") for call in operational.calls if "op" in call]
    assert ops == [
        "find_query_sort_config",
        "get_query_sort_products",
        "save_query_sort_products",
        "add_to_blacklist",
    ]
    save_call = next(call for call in operational.calls if call.get("op") == "save_query_sort_products")
    assert save_call["config_id"] == "cfg-101"
    assert save_call["products"] == [
        {"taggingUid": "p2", "position": 1, "productName": "正常置顶商品2"},
        {"taggingUid": "p3", "position": 2, "productName": "正常置顶商品3"},
    ]
    execution = result.result["gateway"]["execution"]
    assert execution["unpinned_product_ids"] == ["p1"]
    assert execution["deleted_sort_config"] is False


def test_relevance_blacklist_deletes_sort_config_when_all_pinned_products_are_unpinned(monkeypatch):
    monkeypatch.setattr("src.query_governance.relevance_actions.save_action_log", lambda _row: None)
    operational = _TopSortOperational(
        config={"id": "cfg-102", "keyword": "亚麻", "filters": []},
        pinned_products=[
            {"taggingUid": "p1", "position": 1},
        ],
    )
    gateway = RelevanceBlacklistActionGateway(
        operational_service=operational,
        fact_gateway=_FactGateway(),
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "harbor", "亚麻"),
        {
            "type": "product_blacklist",
            "product_ids": ["p1"],
            "product_buckets": {"p1": "top"},
            "reasons": {"p1": "非亚麻材质"},
        },
        {},
    )

    assert result.status is NodeStatus.EXECUTED
    ops = [call.get("op") for call in operational.calls if "op" in call]
    assert ops == [
        "find_query_sort_config",
        "get_query_sort_products",
        "delete_query_sort_config",
        "add_to_blacklist",
    ]
    execution = result.result["gateway"]["execution"]
    assert execution["unpinned_product_ids"] == ["p1"]
    assert execution["deleted_sort_config"] is True


def test_relevance_blacklist_rolls_back_unpin_when_blacklist_write_fails(monkeypatch):
    monkeypatch.setattr("src.query_governance.relevance_actions.save_action_log", lambda _row: None)
    before_pinned = [
        {"taggingUid": "p1", "position": 1},
        {"taggingUid": "p2", "position": 2},
    ]
    operational = _TopSortOperational(
        config={"id": "cfg-103", "keyword": "T恤", "filters": []},
        pinned_products=before_pinned,
        blacklist_succeeds=False,
    )
    gateway = RelevanceBlacklistActionGateway(
        operational_service=operational,
        fact_gateway=FactActionGateway(authorizer=lambda _: True),
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "harbor", "T恤"),
        {
            "type": "product_blacklist",
            "product_ids": ["p1"],
            "product_buckets": {"p1": "top"},
            "reasons": {"p1": "不相关"},
        },
        {},
    )

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "RELEVANCE_BLACKLIST_FAILED"
    save_calls = [call for call in operational.calls if call.get("op") == "save_query_sort_products"]
    assert len(save_calls) == 2
    assert save_calls[0]["products"] == [{"taggingUid": "p2", "position": 1}]
    assert save_calls[1]["products"] == before_pinned


def test_relevance_blacklist_skips_sort_config_lookup_for_non_top_buckets(monkeypatch):
    monkeypatch.setattr("src.query_governance.relevance_actions.save_action_log", lambda _row: None)
    operational = _TopSortOperational(
        config={"id": "cfg-104", "keyword": "连衣裙", "filters": []},
        pinned_products=[{"taggingUid": "p1", "position": 1}],
    )
    gateway = RelevanceBlacklistActionGateway(
        operational_service=operational,
        fact_gateway=_FactGateway(),
    )

    result = gateway.execute_and_verify(
        QuerySpec("q1", "harbor", "连衣裙"),
        {
            "type": "product_blacklist",
            "product_ids": ["p1"],
            "product_buckets": {"p1": "productIdsByProductNameTag"},
            "reasons": {"p1": "不相关"},
        },
        {},
    )

    assert result.status is NodeStatus.EXECUTED
    ops = [call.get("op") for call in operational.calls if "op" in call]
    assert ops == ["add_to_blacklist"]


def test_relevance_runner_and_workflow_propagate_top_bucket_to_blacklist_gateway(monkeypatch):
    from src.query_governance.relevance import RelevanceCheckRunner
    from src.query_governance.workflow import QueryGovernanceWorkflow

    monkeypatch.setattr("src.query_governance.relevance_actions.save_action_log", lambda _row: None)

    class _Runtime:
        def invoke(self, messages, **kwargs):
            return type(
                "Invocation",
                (),
                {
                    "response": type(
                        "Response",
                        (),
                        {
                            "content": '{"results":[{"query":"卫衣","products":[{"product_id":"p1","is_relevant":false,"reason":"配饰非卫衣"}]}]}'
                        },
                    )(),
                    "audit": {"modelName": "test-model"},
                },
            )()

    class _RecordStore:
        def get_relevant_product_ids(self, brand, query, product_ids):
            return set()

        def save_relevant(self, *args, **kwargs):
            return None

        def get_guidance(self, brand, query):
            return ""

    class _ImageCache:
        def prepare_many(self, urls, **kwargs):
            return {}

    runner = RelevanceCheckRunner(
        runtime=_Runtime(),
        record_store=_RecordStore(),
        image_cache=_ImageCache(),
    )
    spec = QuerySpec("q1", "harbor", "卫衣")
    node_result = runner.run(
        spec,
        {
            "current_products": [
                {
                    "productId": "p1",
                    "productName": "发圈配饰",
                    "ext": {"bucket": "top"},
                }
            ]
        },
    )
    assert node_result.status is NodeStatus.EXECUTED
    assert node_result.action["product_buckets"] == {"p1": "top"}

    wf = QueryGovernanceWorkflow.__new__(QueryGovernanceWorkflow)
    wf._relevance_blacklist_execution_mode = "auto"
    wf_action = wf._relevance_blacklist_action(spec, node_result.result)
    assert wf_action["product_buckets"] == {"p1": "top"}

    operational = _TopSortOperational(
        config={"id": "cfg-201", "keyword": "卫衣", "filters": []},
        pinned_products=[
            {"taggingUid": "p1", "position": 1},
            {"taggingUid": "p2", "position": 2},
        ],
    )
    gateway = RelevanceBlacklistActionGateway(
        operational_service=operational,
        fact_gateway=_FactGateway(),
    )
    executed = gateway.execute_and_verify(spec, wf_action, {})
    assert executed.status is NodeStatus.EXECUTED
    ops = [call.get("op") for call in operational.calls if "op" in call]
    assert ops == [
        "find_query_sort_config",
        "get_query_sort_products",
        "save_query_sort_products",
        "add_to_blacklist",
    ]


