from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from src.llm.node_config import QUERY_GOVERNANCE_NODE_KEYS, capture_snapshot
from src.persistence.models import Base
from src.query_governance.async_tasks import (
    QueryGovernanceAsyncTaskRepository,
    QueryGovernanceAsyncTaskService,
)
from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
from src.query_governance.models import (
    NodeStatus,
    NodeResult,
    QueryGovernanceAsyncTask,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QuerySpec,
)
from src.query_governance.recall import (
    RecallCheckRunner,
    RecallHistoryBaseline,
    SqlAlchemyRecallHistoryGateway,
    build_recall_assessment,
)
from src.query_governance.workflow import QueryGovernanceWorkflow


@dataclass
class _AsyncSubmission:
    task_id: int | None
    status: str = "pending"
    error_code: str | None = None
    error_message: str | None = None


class _AsyncTasks:
    def __init__(self, submission: _AsyncSubmission):
        self.submission = submission
        self.calls: list[dict] = []

    def enqueue_for_query(self, **kwargs):
        self.calls.append(kwargs)
        return self.submission


def _context(*, count: int, baseline: RecallHistoryBaseline | None = None, async_tasks=None):
    return {
        "s0": {"result_count": count, "result_complete": True},
        "data_date": date(2026, 8, 29),
        "history_baseline": baseline or RecallHistoryBaseline(),
        "async_tasks": async_tasks,
        "batch_id": "batch-1",
    }


def test_recall_assessment_marks_empty_results_as_anomaly():
    assessment = build_recall_assessment(
        result_count=0,
        low_result_threshold=5,
        drop_ratio_threshold=0.3,
        previous_result_count=10,
    )

    assert assessment["status"] == "needs_attention"
    assert assessment["reasons"] == ["no_result"]
    assert assessment["summary"] == "本次未召回商品，需要交由 KG Agent 生成治理策略。"


def test_recall_runner_records_healthy_result_without_async_task():
    runner = RecallCheckRunner(
        low_result_threshold=5,
        result_drop_threshold=0.3,
    )

    result = runner.run(
        QuerySpec("q1", "atlas", "裙子"),
        _context(
            count=10,
            baseline=RecallHistoryBaseline(
                previous_result_count=10,
            ),
        ),
    )

    assert result.status is NodeStatus.EXECUTED
    assert result.result["status"] == "healthy"
    assert result.result["async_task_id"] is None


def test_recall_runner_enqueues_anomaly_with_evidence_and_idempotency_key():
    async_tasks = _AsyncTasks(_AsyncSubmission(task_id=42))
    runner = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3)

    result = runner.run(
        QuerySpec("q1", "atlas", "裙子"),
        _context(
            count=2,
            baseline=RecallHistoryBaseline(
                previous_result_count=10,
            ),
            async_tasks=async_tasks,
        ),
    )

    assert result.status is NodeStatus.EXECUTED
    assert result.result["status"] == "needs_attention"
    assert result.result["async_task_id"] == 42
    assert result.result["reasons"] == ["low_result"]
    assert async_tasks.calls[0]["trigger_source"] == "recall_anomaly"
    assert async_tasks.calls[0]["evidence"]["result_count"] == 2
    assert async_tasks.calls[0]["idempotency_key"] == "kg:batch-1:atlas:裙子:recall"


def test_recall_runner_persists_langfuse_context_for_async_worker():
    async_tasks = _AsyncTasks(_AsyncSubmission(task_id=42))
    runner = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3)

    result = runner.run(
        QuerySpec("q1", "atlas", "裙子"),
        {
            **_context(count=0, async_tasks=async_tasks),
            "langfuse_trace_context": {
                "batch_trace_id": "batch-trace-1",
                "query_trace_id": "query-trace-1",
            },
        },
    )

    assert result.status is NodeStatus.EXECUTED
    assert async_tasks.calls[0]["langfuse_context"] == {
        "batch_trace_id": "batch-trace-1",
        "query_trace_id": "query-trace-1",
    }


def test_recall_runner_surfaces_async_persistence_failure_as_node_error():
    async_tasks = _AsyncTasks(
        _AsyncSubmission(
            task_id=None,
            status="error",
            error_code="ASYNC_TASK_PERSIST_FAILED",
            error_message="database unavailable",
        )
    )
    runner = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3)

    result = runner.run(
        QuerySpec("q1", "atlas", "裙子"),
        _context(count=0, async_tasks=async_tasks),
    )

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "ASYNC_TASK_PERSIST_FAILED"
    assert "database unavailable" in result.error_message


def test_recall_runner_accepts_previous_result_drop_from_injected_baseline():
    runner = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3)
    result = runner.run(
        QuerySpec("q1", "atlas", "裙子"),
        _context(
            count=5,
            baseline=RecallHistoryBaseline(
                previous_result_count=10,
            ),
        ),
    )

    assert result.result["status"] == "needs_attention"
    assert result.result["reasons"] == ["result_count_drop"]
    assert result.result["summary"] == "本次召回 5 件，较前一日召回数 10 件明显下降，需要交由 KG Agent 生成治理策略。"


def test_recall_runner_uses_previous_day_only_and_does_not_show_history_sample_warning():
    result = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3).run(
        QuerySpec("q1", "atlas", "裙子"),
        _context(
            count=8,
            baseline=RecallHistoryBaseline(
                previous_result_count=10,
                seven_day_average=1,
                history_sample_count=1,
            ),
        ),
    )

    assert result.result["status"] == "healthy"
    assert result.result["summary"] == "本次召回 8 件，数量充足，无需调整。"
    assert result.result["evidence"]["comparison_status"] == "normal"
    assert "历史样本" not in result.result["summary"]


def test_recall_runner_missing_previous_day_is_not_comparable_and_is_healthy():
    result = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3).run(
        QuerySpec("q1", "atlas", "裙子"),
        _context(
            count=8,
            baseline=RecallHistoryBaseline(
                previous_result_count=None,
                seven_day_average=1,
                history_sample_count=1,
            ),
        ),
    )

    assert result.result["status"] == "healthy"
    assert result.result["summary"] == "本次召回 8 件，数量充足，无需调整。"
    assert result.result["evidence"]["comparison_status"] == "not_comparable"
    assert "历史样本" not in result.result["summary"]


def test_recall_assessment_low_result_copy_does_not_expose_threshold():
    assessment = build_recall_assessment(
        result_count=2,
        low_result_threshold=5,
        drop_ratio_threshold=0.3,
        previous_result_count=None,
    )

    assert assessment["summary"] == "本次召回 2 件，数量不足，需要交由 KG Agent 生成治理策略。"
    assert "少结果阈值" not in assessment["summary"]


def test_recall_runner_compares_only_with_previous_result_and_exposes_fixed_conclusion():
    runner = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3)
    result = runner.run(
        QuerySpec("q1", "atlas", "裙子"),
        _context(
            count=8,
            baseline=RecallHistoryBaseline(
                previous_result_count=20,
                seven_day_average=8,
                history_sample_count=7,
            ),
        ),
    )

    assert result.result["status"] == "needs_attention"
    assert result.result["conclusion"] == "结果骤降"
    assert result.result["evidence"]["comparison_baseline"] == "previous_result"
    assert result.result["evidence"]["previous_result_count"] == 20
    assert result.result["evidence"]["seven_day_average"] is None


def test_sqlalchemy_history_gateway_uses_previous_distinct_dates():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QueryGovernanceTask.__table__])
    factory = sessionmaker(bind=engine)
    with Session(engine) as db:
        for index, count in enumerate((10, 8, 12, 10)):
            task = QueryGovernanceTask(
                batch_id=f"old-{index}",
                brand="atlas",
                data_date=date(2026, 8, 25 + index),
                query_text="裙子",
                status="completed",
                s0_snapshot='{"result_count":%d}' % count,
            )
            db.add(task)
        db.add(
            QueryGovernanceTask(
                batch_id="current",
                brand="atlas",
                data_date=date(2026, 8, 29),
                query_text="裙子",
                status="running",
                s0_snapshot='{"result_count":2}',
            )
        )
        db.commit()

    baseline = SqlAlchemyRecallHistoryGateway(factory).get_baseline(
        QuerySpec("q1", "atlas", "裙子"),
        data_date=date(2026, 8, 29),
        batch_id="current",
    )

    assert baseline.previous_result_count == 10
    assert baseline.seven_day_average == 10
    assert baseline.history_sample_count == 4
    engine.dispose()


def test_sqlalchemy_history_gateway_does_not_treat_older_gap_as_previous_day():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[QueryGovernanceTask.__table__])
    factory = sessionmaker(bind=engine)
    with Session(engine) as db:
        db.add(QueryGovernanceTask(
            batch_id="old",
            brand="atlas",
            data_date=date(2026, 8, 27),
            query_text="裙子",
            status="completed",
            s0_snapshot='{"result_count":10}',
        ))
        db.commit()

    baseline = SqlAlchemyRecallHistoryGateway(factory).get_baseline(
        QuerySpec("q1", "atlas", "裙子"),
        data_date=date(2026, 8, 29),
        batch_id="current",
    )

    assert baseline.previous_result_count is None
    assert baseline.result_count_history == (10,)
    engine.dispose()


def test_repeat_anomaly_submission_reuses_one_async_task():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[QueryGovernanceTask.__table__, QueryGovernanceAsyncTask.__table__],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as db:
        db.add(
            QueryGovernanceTask(
                batch_id="batch-1",
                brand="atlas",
                data_date=date(2026, 8, 29),
                query_text="裙子",
                status="running",
            )
        )
        db.commit()
    async_tasks = QueryGovernanceAsyncTaskService(
        QueryGovernanceAsyncTaskRepository(factory)
    )
    runner = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3)
    context = _context(count=0, async_tasks=async_tasks)
    spec = QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")

    first = runner.run(spec, context)
    second = runner.run(spec, context)

    assert first.result["async_task_id"] == second.result["async_task_id"]
    assert first.result["async_task_created"] is True
    assert second.result["async_task_created"] is False
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(QueryGovernanceAsyncTask)) == 1
    engine.dispose()


def test_workflow_persists_recall_anomaly_and_continues_mainline():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    checkpoint = SqlAlchemyCheckpointStore(factory)
    async_tasks = QueryGovernanceAsyncTaskService(
        QueryGovernanceAsyncTaskRepository(factory)
    )
    events: list[str] = []

    class EmptySearch:
        def mixed(self, brand, query):
            return []

    class RecordingRunner:
        def __init__(self, node):
            self.node = node

        def run(self, spec, context):
            events.append(self.node)
            return NodeResult.executed({"node": self.node})

    class ActionGateway:
        def execute_and_verify(self, spec, action, context):
            raise AssertionError("recall check must not execute a governance action")

    workflow = QueryGovernanceWorkflow(
        search=EmptySearch(),
        store=checkpoint,
        checkpoint=checkpoint,
        async_tasks=async_tasks,
        recall_history=SqlAlchemyRecallHistoryGateway(factory),
        runners={
            "recall": RecallCheckRunner(
                low_result_threshold=5,
                result_drop_threshold=0.3,
            ),
            "reorder": RecordingRunner("reorder"),
            "relevance": RecordingRunner("relevance"),
        },
        action_gateway=ActionGateway(),
        model_config_snapshot=capture_snapshot(node_keys=QUERY_GOVERNANCE_NODE_KEYS),
    )

    result = workflow.run_batch(
        [QuerySpec("q1", "atlas", "裙子", batch_id="batch-1")],
        batch_id="batch-1",
        data_date=date(2026, 8, 29),
    )["q1"]

    assert result.nodes["recall"].status is NodeStatus.EXECUTED
    assert result.nodes["recall"].result["reasons"] == ["no_result"]
    assert events == ["reorder", "relevance"]
    with Session(engine) as db:
        async_row = db.scalars(select(QueryGovernanceAsyncTask)).one()
        recall_step = db.scalars(
            select(QueryGovernanceStepStatus).where(
                QueryGovernanceStepStatus.node_key == "recall"
            )
        ).one()
    assert async_row.trigger_source == "recall_anomaly"
    assert async_row.status == "pending"
    assert recall_step.status == "executed"
    engine.dispose()


def test_recall_runner_rejects_missing_or_incomplete_s0_evidence():
    runner = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3)
    spec = QuerySpec("q1", "atlas", "裙子")

    missing = runner.run(spec, {})
    incomplete = runner.run(
        spec,
        {"s0": {"result_count": 0, "result_complete": False}},
    )

    assert missing.error_code == "RECALL_EVIDENCE_UNAVAILABLE"
    assert incomplete.error_code == "RECALL_EVIDENCE_UNAVAILABLE"


def test_recall_runner_reports_history_provider_failure_without_async_write():
    runner = RecallCheckRunner(low_result_threshold=5, result_drop_threshold=0.3)
    result = runner.run(
        QuerySpec("q1", "atlas", "裙子"),
        {
            "s0": {"result_count": 10, "result_complete": True},
            "history_error": "history database unavailable",
            "async_tasks": _AsyncTasks(_AsyncSubmission(task_id=1)),
        },
    )

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "RECALL_HISTORY_FAILED"


def test_recall_runner_anomaly_without_async_service_keeps_check_evidence():
    result = RecallCheckRunner(
        low_result_threshold=5,
        result_drop_threshold=0.3,
    ).run(
        QuerySpec("q1", "atlas", "裙子"),
        {"s0": {"result_count": 0, "result_complete": True}},
    )

    assert result.status is NodeStatus.EXECUTED
    assert result.result["async_task_status"] == "not_configured"
    assert result.result["async_task_id"] is None


def test_recall_runner_requires_batch_id_when_async_service_is_configured():
    result = RecallCheckRunner(
        low_result_threshold=5,
        result_drop_threshold=0.3,
    ).run(
        QuerySpec("q1", "atlas", "裙子"),
        {
            "s0": {"result_count": 0, "result_complete": True},
            "async_tasks": _AsyncTasks(_AsyncSubmission(task_id=1)),
        },
    )

    assert result.status is NodeStatus.ERROR
    assert result.error_code == "ASYNC_TASK_CONTEXT_MISSING"


def test_recall_rate_evaluator_reuses_relevance_bidirectionally_and_verifies_100_percent():
    import json
    from src.query_governance.models import (
        QueryGovernanceImageAsset,
        QueryGovernanceRelevanceGuidance,
        QueryGovernanceRelevanceRecord,
    )
    from src.query_governance.recall_rate import (
        CatalogProduct,
        RecallRateEvaluator,
    )
    from src.query_governance.relevance import (
        ImageCacheService,
        InMemoryObjectStorage,
        PillowImageTransformer,
        RelevanceCheckRunner,
        SqlAlchemyRelevanceStore,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            QueryGovernanceRelevanceRecord.__table__,
            QueryGovernanceRelevanceGuidance.__table__,
            QueryGovernanceImageAsset.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    relevance_store = SqlAlchemyRelevanceStore(factory)
    # Pre-populate P2 as already checked & relevant in the relevance module
    relevance_store.save_relevant("atlas", "连衣裙 女士", "P2", model_name="seed")

    class FakeImageFetcher:
        def fetch(self, url: str) -> tuple[bytes, str]:
            raise RuntimeError("no image needed in text fallback test")

    image_cache = ImageCacheService(
        fetcher=FakeImageFetcher(),
        transformer=PillowImageTransformer(),
        storage=InMemoryObjectStorage(),
    )

    model_calls: list[list[dict]] = []

    class FakeRelevanceRuntime:
        def invoke(self, messages, **kwargs):
            model_calls.append(list(messages))
            # Only P3 and P4 should be sent to the relevance agent (P1 is in intersection, P2 is reused from DB)
            return json.dumps(
                {
                    "results": [
                        {
                            "query": "连衣裙 女士",
                            "relevant": [1, 0],
                            "image_checked": [0, 0],
                            "reasons": {"2": "不是连衣裙，是长裤"},
                        }
                    ]
                },
                ensure_ascii=False,
            )

    relevance_runner = RelevanceCheckRunner(
        runtime=FakeRelevanceRuntime(),
        record_store=relevance_store,
        image_cache=image_cache,
    )

    class FakeOperational:
        def __init__(self):
            self.search_calls = 0
            self.whitelist_tag_calls: list[tuple[str, str, list[str]]] = []
            self.whitelist_calls: list[tuple[str, str, list[str]]] = []
            self.retag_calls: list[tuple[str, str]] = []

        def search_all_products_with_total(self, brand: str, query: str, page_size: int = 100):
            self.search_calls += 1
            if self.search_calls == 1:
                # Initial Mixed search recalls only P1, and returns segmented KG tag ["连衣裙"]
                return {
                    "success": True,
                    "products": [{"productId": "P1", "productName": "黑色连衣裙"}],
                    "total_count": 1,
                    "result_complete": True,
                    "tags": ["连衣裙"],
                }
            # Post-tagging Mixed search recalls P1, P2, P3 -> 100% recall rate!
            return {
                "success": True,
                "products": [
                    {"productId": "P1", "productName": "黑色连衣裙"},
                    {"productId": "P2", "productName": "白色连衣裙"},
                    {"productId": "P3", "productName": "红色连衣裙"},
                ],
                "total_count": 3,
                "result_complete": True,
                "tags": ["连衣裙"],
            }

        def add_whitelist_tags(self, brand: str, product_ids: str, tags: list[str]) -> bool:
            self.whitelist_tag_calls.append((brand, product_ids, list(tags)))
            return True

        def add_to_whitelist(self, brand: str, product_ids: str, words: list[str]) -> bool:
            self.whitelist_calls.append((brand, product_ids, list(words)))
            return True

        def retag_selected_products(self, brand: str, product_ids: str) -> bool:
            self.retag_calls.append((brand, product_ids))
            return True

    class FakeLLMRecall:
        def recall_for_query(self, brand: str, query: str):
            return [
                CatalogProduct(brand=brand, spu="P1", recall_product_id="P1", product_name="黑色连衣裙"),
                CatalogProduct(brand=brand, spu="P2", recall_product_id="P2", product_name="白色连衣裙"),
                CatalogProduct(brand=brand, spu="P3", recall_product_id="P3", product_name="红色连衣裙"),
                CatalogProduct(brand=brand, spu="P4", recall_product_id="P4", product_name="牛仔长裤"),
            ]

    ops = FakeOperational()
    evaluator = RecallRateEvaluator(
        search=ops,
        llm_recall=FakeLLMRecall(),
        relevance_runner=relevance_runner,
        relevance_store=relevance_store,
        operational_service=ops,
    )
    spec = QuerySpec("q1", "atlas", "连衣裙 女士", batch_id="batch-1")
    report = evaluator.evaluate(spec, {})

    # 1. P1 (intersection), P2 (reused), P3 (agent-verified) are now all in relevance_store for bidirectional reuse
    reused_for_next_run = relevance_store.get_relevant_product_ids(
        "atlas", "连衣裙 女士", ["P1", "P2", "P3", "P4"]
    )
    assert reused_for_next_run == {"P1", "P2", "P3"}
    assert len(model_calls) == 1
    assert report["reused_relevance_product_ids"] == ["P2"]
    assert report["valid_llm_only_product_ids"] == ["P2", "P3"]
    assert report["full_valid_product_ids"] == ["P1", "P2", "P3"]
    assert report["initial_search_recall_rate"] == round(1 / 3, 4)
    assert report["llm_recall_rate"] == 1.0
    assert report["post_search_recall_rate"] == 1.0
    assert report["is_full_recall"] is True
    assert report["operator_alert"] is None
    assert ops.whitelist_tag_calls == [("atlas", "P2;P3", ["连衣裙"])]
    assert ops.whitelist_calls == []
    assert ops.retag_calls == [("atlas", "P2;P3")]
    engine.dispose()


def test_recall_rate_evaluator_uses_jev_for_multi_tags_and_alerts_when_post_recall_below_100():
    from src.query_governance.models import (
        QueryGovernanceImageAsset,
        QueryGovernanceRelevanceGuidance,
        QueryGovernanceRelevanceRecord,
    )
    from src.query_governance.recall_rate import (
        CatalogProduct,
        JevTagSelector,
        RecallRateEvaluator,
    )
    from src.query_governance.relevance import (
        ImageCacheService,
        InMemoryObjectStorage,
        PillowImageTransformer,
        RelevanceCheckRunner,
        SqlAlchemyRelevanceStore,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            QueryGovernanceRelevanceRecord.__table__,
            QueryGovernanceRelevanceGuidance.__table__,
            QueryGovernanceImageAsset.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    relevance_store = SqlAlchemyRelevanceStore(factory)
    # Mark P2 and P3 as already relevant so we focus on JEV + post-check alert
    relevance_store.save_relevant("atlas", "红色丝绸连衣裙", "P2")
    relevance_store.save_relevant("atlas", "红色丝绸连衣裙", "P3")

    class FakeImageFetcher:
        def fetch(self, url: str) -> tuple[bytes, str]:
            raise RuntimeError("unused")

    relevance_runner = RelevanceCheckRunner(
        runtime=None,  # type: ignore[arg-type]
        record_store=relevance_store,
        image_cache=ImageCacheService(
            fetcher=FakeImageFetcher(),
            transformer=PillowImageTransformer(),
            storage=InMemoryObjectStorage(),
        ),
    )

    jev_requests: list[dict] = []

    class FakeJevResponse:
        status_code = 200

        def __init__(self, payload: dict):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_jev_post(url, *, headers, json, timeout):
        jev_requests.append(json)
        pid = json["state"]["candidate"]["productId"]
        if pid == "P2":
            return FakeJevResponse(
                {
                    "model": "jev-1.13.0",
                    "answers": {
                        "tag_1": {"type": "noul", "noul": 0.92},
                        "tag_2": {"type": "noul", "noul": 0.85},
                    },
                }
            )
        # P3 has no matching tag >= 0.7 -> should fallback to query whitelist
        return FakeJevResponse(
            {
                "model": "jev-1.13.0",
                "answers": {
                    "tag_1": {"type": "noul", "noul": 0.20},
                    "tag_2": {"type": "noul", "noul": 0.30},
                },
            }
        )

    class FakeOperational:
        def __init__(self):
            self.calls = 0
            self.tag_writes: list[tuple[str, list[str]]] = []
            self.wl_writes: list[tuple[str, list[str]]] = []

        def search_all_products_with_total(self, brand: str, query: str, page_size: int = 100):
            self.calls += 1
            if self.calls == 1:
                return {
                    "success": True,
                    "products": [{"productId": "P1"}],
                    "total_count": 1,
                    "result_complete": True,
                    "tags": ["红色", "连衣裙"],
                }
            # After tagging, P2 is recalled, but P3 is still missing!
            return {
                "success": True,
                "products": [{"productId": "P1"}, {"productId": "P2"}],
                "total_count": 2,
                "result_complete": True,
                "tags": ["红色", "连衣裙"],
            }

        def add_whitelist_tags(self, brand: str, product_ids: str, tags: list[str]) -> bool:
            self.tag_writes.append((product_ids, list(tags)))
            return True

        def add_to_whitelist(self, brand: str, product_ids: str, words: list[str]) -> bool:
            self.wl_writes.append((product_ids, list(words)))
            return True

        def retag_selected_products(self, brand: str, product_ids: str) -> bool:
            return True

    class FakeLLMRecall:
        def recall_for_query(self, brand: str, query: str):
            return [
                CatalogProduct(brand=brand, spu="P2", recall_product_id="P2", product_name="红色真丝裙"),
                CatalogProduct(brand=brand, spu="P3", recall_product_id="P3", product_name="酒红礼服裙"),
            ]

    ops = FakeOperational()
    evaluator = RecallRateEvaluator(
        search=ops,
        llm_recall=FakeLLMRecall(),
        relevance_runner=relevance_runner,
        relevance_store=relevance_store,
        operational_service=ops,
        jev_selector=JevTagSelector(api_key="test-key", request_post=fake_jev_post),
    )
    async_tasks = _AsyncTasks(_AsyncSubmission(task_id=99))
    runner = RecallCheckRunner(
        low_result_threshold=5,
        result_drop_threshold=0.3,
        recall_rate_evaluator=evaluator,
        config_provider=lambda brand: {"query_governance_recall_rate_enabled": True},
    )
    spec = QuerySpec("q1", "atlas", "红色丝绸连衣裙", batch_id="batch-1")
    node_result = runner.run(spec, _context(count=1, async_tasks=async_tasks))

    assert len(jev_requests) == 2
    assert ops.tag_writes == [("P2", ["红色"]), ("P2", ["连衣裙"])]
    assert ops.wl_writes == [("P3", ["红色丝绸连衣裙"])]
    assert node_result.status is NodeStatus.EXECUTED
    assert node_result.result["status"] == "needs_attention"
    assert "recall_rate_incomplete" in node_result.result["reasons"]
    assert "low_result" in node_result.result["reasons"]
    # Post-tagging result count is 2 (< low_result_threshold 5), so KG Agent task is also enqueued!
    assert node_result.result["async_task_id"] == 99
    assert async_tasks.calls[0]["evidence"]["result_count"] == 2
    recall_rate = node_result.result["recall_rate"]
    assert recall_rate["is_full_recall"] is False
    assert recall_rate["unrecalled_product_ids"] == ["P3"]
    assert recall_rate["post_search_recall_rate"] == round(2 / 3, 4)
    assert "P3" in recall_rate["operator_alert"]
    assert "66.7%" in recall_rate["operator_alert"]
    engine.dispose()

