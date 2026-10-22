from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.persistence.models import ActionIntent, ActionLog, ActionTerminalFact, BrandDailyMetric, InventoryRecoveryTask, NodeModelConfigEntry, NodeModelConfigVersion, SchedulerJobRun
from src.query_governance.models import (
    QueryGovernanceAgentEvent,
    QueryGovernanceActionPlan,
    QueryGovernanceAsyncTask,
    QueryGovernanceBatchSummary,
    QueryGovernanceCandidate,
    QueryGovernanceContextSnapshot,
    QueryGovernanceExperiment,
    QueryGovernanceExperimentSample,
    QueryGovernanceRelevanceRecord,
    QueryGovernanceSelectionBatch,
    QueryGovernanceStepStatus,
    QueryGovernanceTask,
    QueryGovernanceWorkflowRun,
    QueryMetricSnapshot,
)
from src.query_governance.read_api import QueryGovernanceReadService, _governance_plans, _governance_states
from src.query_governance.selection import QuerySelectionBatch, QuerySelectionConfig, QuerySelectionDecision, SqlAlchemyQuerySelectionStore


def test_governance_states_prioritize_async_generation_failure_over_node_issue():
    states = _governance_states(
        [],
        [SimpleNamespace(
            trigger_source="recall_anomaly",
            status="error",
            plan_json=None,
            error_code="KG_AGENT_INVALID_JSON",
            error_message="agent response must be JSON",
        )],
    )

    assert states == {
        "recall": {
            "status": "failed",
            "label": "生成失败",
            "errorCode": "KG_AGENT_INVALID_JSON",
            "errorMessage": "agent response must be JSON",
        },
    }


def test_governance_states_follow_visible_plan_source_and_hide_empty_pending_task():
    action = SimpleNamespace(
        action_type="kg_plan", status="verified", failure_stage=None,
        executor_type="ai_auto", error_code=None, error_message=None,
        request_json=json.dumps({"trigger_source": "recall_anomaly"}),
    )
    states = _governance_states(
        [action],
        [SimpleNamespace(
            trigger_source="recall_anomaly", status="error", plan_json=None,
            error_code="OLD_ERROR", error_message="old failure",
        )],
    )
    assert states["recall"]["status"] == "verified"
    assert states["recall"]["label"] == "AI 已执行"

    hidden_pending = _governance_states(
        [SimpleNamespace(
            action_type="product_blacklist", status="verified", failure_stage=None,
            executor_type="ai_auto", error_code=None, error_message=None,
            request_json="{}",
        )],
        [SimpleNamespace(
            trigger_source="relevance_root_cause", status="pending", plan_json=None,
            error_code=None, error_message=None,
        )],
    )
    assert hidden_pending["relevance"]["status"] == "verified"


def test_governance_plans_project_all_five_strategy_types_with_business_evidence():
    task = SimpleNamespace(
        id=42,
        s0_snapshot=json.dumps({
            "products": [
                {"product_id": "P1", "product_name": "轻薄外套"},
                {"product_id": "P2", "product_name": "错误召回商品"},
                {"product_id": "P3", "product_name": "断码商品"},
            ],
        }, ensure_ascii=False),
    )
    actions = [
        SimpleNamespace(
            id=1, action_type="kg_plan", status="pending", error_message=None,
            request_json=json.dumps({
                "trigger_source": "recall_anomaly",
                "evidence": {
                    "reason": "low_result",
                    "current_result_count": 2,
                    "previous_result_count": 8,
                    "products": [{"product_id": "P2", "reason": "商品被错误召回"}],
                },
            }, ensure_ascii=False),
            response_json=json.dumps({
                "reason": "召回数量不足，需要补充标签关系",
                "actions": [
                    {"actionType": "create_tag", "title": "创建标签", "description": "创建“轻薄”标签"},
                    {"actionType": "create_relation", "title": "创建标签关系", "description": "将“轻薄”关联到“外套”"},
                ],
            }, ensure_ascii=False),
        ),
        SimpleNamespace(
            id=2, action_type="query_reorder", status="verified", error_message=None,
            executor_type="ai_auto",
            request_json=json.dumps({
                "preference_description": "优先展示轻薄款",
                "products": [{"product_id": "P1"}, {"product_id": "P2"}],
            }, ensure_ascii=False),
            response_json=json.dumps({"verified": True}),
        ),
        SimpleNamespace(
            id=3, action_type="product_blacklist", status="verified", error_message=None,
            executor_type="ai_auto",
            request_json=json.dumps({
                "execution_mode": "auto",
                "products": [{"product_id": "P2", "reason": "与 Query 意图不相关"}],
                "product_images": {"P2": "https://img/P2.jpg"},
            }, ensure_ascii=False),
            response_json=json.dumps({"verified": True}),
        ),
        SimpleNamespace(
            id=4, action_type="query_demote", status="verified", error_message=None,
            executor_type="ai_auto",
            request_json=json.dumps({
                "execution_mode": "auto",
                "source": "inventory_monitor",
                "products": [{"product_id": "P3", "broken_rate_percent": 72}],
            }, ensure_ascii=False),
            response_json=json.dumps({"verified": True}),
        ),
        SimpleNamespace(
            id=5, action_type="query_demote", status="failed", error_message="恢复回读失败",
            executor_type="ai_auto",
            request_json=json.dumps({
                "execution_mode": "auto",
                "operation": "restore",
                "products": [{
                    "product_id": "P3",
                    "executed_broken_rate": 72,
                    "restored_broken_rate": 18,
                }],
            }, ensure_ascii=False),
            response_json=json.dumps({}),
        ),
    ]

    plans = _governance_plans(task, [], actions, [])

    assert [plan["type"] for plan in plans] == [
        "kg", "reorder", "blacklist", "demotion", "demotion_restore",
    ]
    kg, reorder, blacklist, demotion, restore = plans
    assert kg["reason"] == "召回数量不足：当前 2 件，上一次 8 件"
    assert [step["title"] for step in kg["steps"]] == ["创建标签", "创建标签关系"]
    assert kg["products"][0]["productId"] == "P2"
    assert reorder["summary"] == "优先展示轻薄款"
    assert [product["productId"] for product in reorder["products"]] == ["P1", "P2"]
    assert blacklist["products"][0]["reason"] == "与 Query 意图不相关"
    assert blacklist["products"][0]["imageUrl"] == "https://img/P2.jpg"
    assert blacklist["cancelable"] is True
    assert demotion["metrics"]["fromRate"] == 72
    assert demotion["cancelable"] is True
    assert restore["metrics"] == {"fromRate": 72, "toRate": 18}
    assert restore["statusLabel"] == "AI 执行失败"
    assert restore["cancelable"] is False


def test_plan_product_images_override_stale_snapshot_image():
    task = SimpleNamespace(id=99, s0_snapshot=json.dumps({
        "products": [
            {"product_id": "P1", "product_name": "阔腿牛仔慢跑裤", "rank": 26,
             "image_url": "https://img.test/polluted-legacy.jpg"},
            {"product_id": "P2", "product_name": "无计划图商品", "rank": 27,
             "image_url": "https://img.test/snapshot-only.jpg"},
        ],
    }, ensure_ascii=False))
    action = SimpleNamespace(
        id=7, action_type="product_blacklist", status="pending", error_message=None,
        executor_type=None,
        request_json=json.dumps({
            "execution_mode": "human_approval",
            "product_ids": ["P1", "P2"],
            "product_images": {"P1": "https://img.test/correct.jpg"},
            "product_names": {"P1": "阔腿牛仔慢跑裤", "P2": "无计划图商品"},
        }, ensure_ascii=False),
        response_json=json.dumps({}),
    )

    plans = _governance_plans(task, [], [action], [])
    products = {p["productId"]: p for p in plans[0]["products"]}

    # 计划里的正确图必须覆盖快照回带的历史错图
    assert products["P1"]["imageUrl"] == "https://img.test/correct.jpg"
    assert products["P1"]["image_url"] == "https://img.test/correct.jpg"
    # 计划没有图的商品保持回落快照图的行为
    assert products["P2"]["image_url"] == "https://img.test/snapshot-only.jpg"


def test_grouped_error_products_use_latest_blacklist_toggle_status():
    task = SimpleNamespace(id=43, s0_snapshot=json.dumps({"products": []}))
    action = SimpleNamespace(
        id=6, action_type="relevance_plan", status="pending", error_message=None,
        executor_type=None,
        request_json=json.dumps({"trigger_source": "relevance_root_cause", "evidence": {"products": [{"product_id": "P1"}]}}),
        response_json=json.dumps({"causeGroups": [{"groupId": "one", "rootCause": "individual", "products": [{"productId": "P1", "status": "pending"}]}]}),
    )

    plan = _governance_plans(task, [], [action], [], blacklist_product_statuses={"P1": "blacklisted"})[0]

    assert plan["causeGroups"][0]["products"][0]["blacklistStatus"] == "blacklisted"


def test_governance_plans_coalesce_blacklist_execution_rounds_without_losing_product_facts():
    task = SimpleNamespace(id=1135, s0_snapshot=json.dumps({"products": []}))
    actions = [
        SimpleNamespace(
            id=908, action_type="product_blacklist", status="verified", executor_type="ai_auto",
            error_message=None, response_json="{}",
            request_json=json.dumps({
                "product_ids": ["P1", "P2"],
                "product_names": {"P1": "短袖外套", "P2": "拉链外套"},
                "reasons": {"P1": "短袖不符", "P2": "拉链不符"},
                "source": "historical_action_intent",
            }, ensure_ascii=False),
        ),
        SimpleNamespace(
            id=909, action_type="product_blacklist", status="verified", executor_type="ai_auto",
            error_message=None, response_json="{}",
            request_json=json.dumps({
                "product_ids": ["P3"],
                "product_names": {"P3": "拼接外套"},
                "reasons": {"P3": "材质不符"},
                "source": "historical_action_intent",
            }, ensure_ascii=False),
        ),
    ]

    plans = _governance_plans(task, [], actions, [])

    assert len(plans) == 1
    assert plans[0]["title"] == "错误商品"
    assert plans[0]["actionIds"] == [908, 909]
    assert plans[0]["statusLabel"] == "AI 已执行"
    assert [(item["productId"], item["productName"], item["reason"], item["status"]) for item in plans[0]["products"]] == [
        ("P1", "短袖外套", "短袖不符", "blacklisted"),
        ("P2", "拉链外套", "拉链不符", "blacklisted"),
        ("P3", "拼接外套", "材质不符", "blacklisted"),
    ]


def test_governance_plans_show_kg_decision_and_tool_evidence():
    task = SimpleNamespace(id=7, s0_snapshot=json.dumps({"products": []}, ensure_ascii=False))
    action = SimpleNamespace(
        id=9,
        action_type="kg_plan",
        status="pending",
        executor_type=None,
        error_message=None,
        request_json=json.dumps({"trigger_source": "recall_anomaly", "evidence": {"result_count": 0}}),
        response_json=json.dumps({
            "decision": "no_change",
            "reason": "牛仔裤已有召回且关系完整",
            "tool_evidence": {
                "tags": {"牛仔": {"data": [{"name": "牛仔裤"}]}},
                "relationships": {"牛仔": {"data": [{"responseTagName": "牛仔裤"}]}},
                "searches": {"牛仔": {"result_count": 0}, "牛仔裤": {"result_count": 18}},
            },
            "actions": [],
        }, ensure_ascii=False),
    )

    plans = _governance_plans(task, [], [action], [])

    assert plans[0]["decision"] == "no_change"
    assert plans[0]["statusLabel"] == "无需调整"
    assert plans[0]["executable"] is False
    assert {item["label"] for item in plans[0]["evidence"]} >= {"搜索「牛仔裤」结果", "KG 标签查询", "KG 关系查询"}


def test_governance_plans_hide_async_kg_generation_error_details():
    task = SimpleNamespace(id=8, s0_snapshot=json.dumps({"products": []}))
    async_task = SimpleNamespace(
        id=12,
        status="error",
        plan_json=None,
        evidence_json=json.dumps({"reason": "low_result", "result_count": 2}),
        trigger_source="recall_anomaly",
        error_code="KG_AGENT_PERSIST_FAILED",
        error_message=(
            "(pymysql.err.DataError) (1406, Data too long for column "
            "'response_json') INSERT INTO query_governance_action_plan ..."
        ),
    )

    plan = _governance_plans(task, [], [], [async_task])[0]

    assert plan["status"] == "failed"
    assert plan["title"] == "召回数量治理策略"
    assert plan["statusLabel"] == "生成失败"
    assert plan["summary"] == "生成失败"
    assert plan["reason"] == "生成失败"
    assert plan["failureStage"] is None
    assert plan["evidence"] == []
    assert plan["steps"] == []
    assert plan["products"] == []
    assert "pymysql" not in json.dumps(plan, ensure_ascii=False)
    assert "response_json" not in json.dumps(plan, ensure_ascii=False)


def test_governance_plans_hide_persisted_kg_action_error_details():
    task = SimpleNamespace(id=9, s0_snapshot=json.dumps({"products": []}))
    action = SimpleNamespace(
        id=13,
        action_type="kg_plan",
        status="failed",
        executor_type="ai_auto",
        executor_user_id=None,
        executed_at=None,
        failure_stage="generation",
        error_message=(
            "(pymysql.err.DataError) (1406, Data too long for column "
            "'response_json')"
        ),
        request_json=json.dumps({"trigger_source": "recall_anomaly", "evidence": {"result_count": 0}}),
        response_json=json.dumps({"reason": "原始方案详情", "actions": [{"actionType": "add_tag"}]}),
    )

    plan = _governance_plans(task, [], [action], [])[0]

    assert plan["statusLabel"] == "生成失败"
    assert plan["summary"] == "生成失败"
    assert plan["reason"] == "生成失败"
    assert plan["failureStage"] is None
    assert plan["steps"] == []
    assert plan["evidence"] == []
    assert plan["products"] == []
    assert "pymysql" not in json.dumps(plan, ensure_ascii=False)


def test_governance_plans_do_not_offer_cancel_for_successful_reorder_or_manual_action():
    task = SimpleNamespace(id=7, s0_snapshot=json.dumps({"products": []}))
    actions = [
        SimpleNamespace(
            id=10, action_type="query_reorder", status="verified", error_message=None,
            executor_type="ai_auto",
            request_json=json.dumps({"execution_mode": "auto", "preference": "展示新品"}),
            response_json=json.dumps({}),
        ),
        SimpleNamespace(
            id=11, action_type="query_demote", status="verified", error_message=None,
            executor_type="human",
            request_json=json.dumps({"execution_mode": "human_approval", "product_ids": ["P1"]}),
            response_json=json.dumps({}),
        ),
    ]

    plans = _governance_plans(task, [], actions, [])

    assert all(plan["cancelable"] is False for plan in plans)


def test_governance_plans_translate_reorder_readback_failure_for_operators():
    task = SimpleNamespace(id=9, s0_snapshot=json.dumps({"products": []}))
    actions = [
        SimpleNamespace(
            id=12,
            action_type="query_reorder",
            status="execution_unknown",
            executor_type="ai_auto",
            error_code="ACTION_READBACK_FAILED",
            error_message="排序动作权威回读未确认成功",
            request_json=json.dumps({"preference": "展示新品"}),
            response_json=json.dumps({}),
        ),
    ]

    plans = _governance_plans(task, [], actions, [])

    assert plans[0]["reason"] == "排序已提交，但系统未能确认新排序是否已生效。"


def test_governance_plans_project_inventory_demotion_and_restore_rates_without_fake_action_ids():
    task = SimpleNamespace(id=8, s0_snapshot=json.dumps({"products": []}))
    records = [
        SimpleNamespace(
            id=21, product_id="P1", product_name="断码外套", last_check_stock=72,
            status="monitoring", last_action_request_id="demote-21",
        ),
        SimpleNamespace(
            id=22, product_id="P2", product_name="已恢复外套", last_check_stock=18,
            status="recovered", last_action_request_id="restore-22",
        ),
    ]
    checks = {
        22: [
            {"status": "monitoring", "broken_rate_percent": 76},
            {"status": "recovered", "broken_rate_percent": 18},
        ],
    }

    plans = _governance_plans(task, [], [], [], records, checks)

    demotion, restore = plans
    assert demotion["type"] == "demotion"
    assert demotion["actionId"] is None
    assert demotion["executable"] is False
    assert demotion["cancelable"] is True
    assert demotion["metrics"] == {"fromRate": 72}
    assert restore["type"] == "demotion_restore"
    assert restore["metrics"] == {"fromRate": 76, "toRate": 18}
    assert restore["products"][0]["executedBrokenRate"] == 76
    assert restore["products"][0]["restoredBrokenRate"] == 18


def test_empty_selection_is_visible_without_fabricated_query_tasks():
    engine, factory = _database()
    SqlAlchemyQuerySelectionStore(factory).save_batch(QuerySelectionBatch(
        batch_id="query-governance:atlas:2026-09-07", brand="atlas",
        data_date=date(2026, 9, 7), config=QuerySelectionConfig(), candidates=(),
    ))
    service = QueryGovernanceReadService(factory)
    overview = service.overview(["atlas"], data_date=date(2026, 9, 7))["brands"]
    assert len(overview) == 1
    assert overview[0]["status"] == "skipped"
    assert overview[0]["reason"] == "no_candidates"
    assert overview[0]["dataDate"] == "2026-09-07"
    detail = service.brand_detail("atlas", data_date=date(2026, 9, 7))
    assert detail["batch"]["reason"] == "no_candidates"
    assert detail["queries"] == []
    assert detail["batch"]["telemetry"]["detectedProductCount"] == 0
    assert detail["batch"]["telemetry"]["relevanceAccuracy"] is None
    assert service.brand_detail("harbor")["batch"] is None
    engine.dispose()


def test_requested_date_without_batch_keeps_brand_visible_as_no_data():
    engine, factory = _database()
    try:
        overview = QueryGovernanceReadService(factory).overview(
            ["atlas", "harbor"], data_date=date(2026, 9, 9)
        )["brands"]
        assert overview == [
            {
                "brand": "atlas",
                "batchId": "",
                "dataDate": None,
                "status": "skipped",
                "reason": "no_data",
                "queryCount": 0,
                "nodeStatusCounts": {
                    "pending": 0,
                    "running": 0,
                    "executed": 0,
                    "skipped": 0,
                    "error": 0,
                },
                "durationSeconds": None,
                "startedAt": None,
                "completedAt": None,
                "telemetry": {},
            },
            {
                "brand": "harbor",
                "batchId": "",
                "dataDate": None,
                "status": "skipped",
                "reason": "no_data",
                "queryCount": 0,
                "nodeStatusCounts": {
                    "pending": 0,
                    "running": 0,
                    "executed": 0,
                    "skipped": 0,
                    "error": 0,
                },
                "durationSeconds": None,
                "startedAt": None,
                "completedAt": None,
                "telemetry": {},
            },
        ]
    finally:
        engine.dispose()


def test_brand_detail_keeps_business_trend_when_requested_day_has_no_governance_batch():
    engine, factory = _database()
    try:
        with factory() as session:
            session.add_all([
                BrandDailyMetric(
                    brand="atlas",
                    data_date=date(2026, 8, 11),
                    brand_uv=800,
                    brand_pv=2100,
                    ctr=0.08,
                    cvr=0.01,
                    raw_payload=json.dumps({"click_uv": 64, "add_to_cart_uv": 20, "purchase_uv": 8, "atc": 0.025}),
                ),
                BrandDailyMetric(
                    brand="atlas",
                    data_date=date(2026, 9, 9),
                    brand_uv=1200,
                    brand_pv=3400,
                    ctr=0.12,
                    cvr=0.03,
                    raw_payload=json.dumps({"click_uv": 144, "add_to_cart_uv": 48, "purchase_uv": 36, "atc": 0.04}),
                ),
            ])
            session.commit()

        detail = QueryGovernanceReadService(factory).brand_detail(
            "atlas", data_date=date(2026, 9, 9)
        )

        assert detail["batch"] is None
        assert detail["summary"]["queryCount"] == 0
        assert detail["stats"] == {
            "topQueryCount": 0,
            "manualRequiredCount": 0,
            "completedCount": 0,
            "stepIssueCounts": {"recall": 0, "relevance": 0, "open_factor": 0, "inventory": 0},
            "noResultOrLowResultCount": 0,
            "resultDropCount": 0,
            "relevanceIssueCount": 0,
            "openFactorIssueCount": 0,
            "inventoryProductIssueCount": 0,
            "brandUv": 1200,
            "brandPv": 3400,
            "clickUv": 144,
            "addToCartUv": 48,
            "purchaseUv": 36,
            "ctr": 0.12,
            "atc": 0.04,
            "cvr": 0.03,
        }
        assert [point["date"] for point in detail["trend"]] == ["2026-08-11", "2026-09-09"]
    finally:
        engine.dispose()


def test_brand_detail_restores_query_business_metrics_and_pv_rank_from_candidates():
    engine, factory = _database()
    try:
        batch = QuerySelectionBatch(
            batch_id="query-governance:atlas:2026-09-08",
            brand="atlas",
            data_date=date(2026, 9, 8),
            config=QuerySelectionConfig(extract_limit=2),
            candidates=(
                QuerySelectionDecision(
                    query_id="q1", query="连衣裙", rules=("high_pv",),
                    metrics={"search_pv": 1200, "search_uv": 300, "click_uv": 60, "purchase_uv": 12, "history_ctr": 0.18, "history_cvr": 0.03},
                    source_type="dynamic", rule_count=1, ctr_decline_pp=0, target_day_pv=1200, selection_rank=1, selected=True,
                ),
                QuerySelectionDecision(
                    query_id="q2", query="外套", rules=("high_pv",),
                    metrics={"search_pv": 800, "search_uv": 200, "click_uv": 20, "purchase_uv": 4, "history_ctr": 0.16, "history_cvr": 0.02},
                    source_type="dynamic", rule_count=1, ctr_decline_pp=0, target_day_pv=800, selection_rank=2, selected=True,
                ),
            ),
        )
        SqlAlchemyQuerySelectionStore(factory).save_batch(batch)
        queries = QueryGovernanceReadService(factory).brand_detail("atlas", data_date=date(2026, 9, 8))["queries"]
        assert [(item["queryText"], item["searchPv"], item["ctr"], item["cvr"], item["queryRank"]) for item in queries] == [
            ("连衣裙", 1200, 0.2, 0.04, 1),
            ("外套", 800, 0.1, 0.02, 2),
        ]
        assert queries[0]["avgCtr"] == 0.18
        assert queries[0]["avgCvr"] == 0.03
    finally:
        engine.dispose()


def test_brand_queries_pages_selected_queries_by_global_pv_order():
    engine, factory = _database()
    try:
        batch = QuerySelectionBatch(
            batch_id="query-governance:atlas:2026-09-14",
            brand="atlas",
            data_date=date(2026, 9, 14),
            config=QuerySelectionConfig(extract_limit=3),
            candidates=(
                QuerySelectionDecision(
                    query_id="q-low", query="低 PV 词", rules=("test",),
                    metrics={"search_pv": 120, "search_uv": 60},
                    source_type="test", rule_count=1, ctr_decline_pp=0,
                    target_day_pv=120, selection_rank=1, selected=True,
                ),
                QuerySelectionDecision(
                    query_id="q-middle", query="中 PV 词", rules=("test",),
                    metrics={"search_pv": 643, "search_uv": 240},
                    source_type="test", rule_count=1, ctr_decline_pp=0,
                    target_day_pv=643, selection_rank=50, selected=True,
                ),
                QuerySelectionDecision(
                    query_id="q-high", query="香水", rules=("test",),
                    metrics={"search_pv": 797, "search_uv": 344},
                    source_type="test", rule_count=1, ctr_decline_pp=0,
                    target_day_pv=797, selection_rank=223, selected=True,
                ),
            ),
        )
        SqlAlchemyQuerySelectionStore(factory).save_batch(batch)
        service = QueryGovernanceReadService(factory)

        first_page = service.brand_queries(
            "atlas", data_date=date(2026, 9, 14), page=1, page_size=2
        )
        second_page = service.brand_queries(
            "atlas", data_date=date(2026, 9, 14), page=2, page_size=2
        )

        assert [
            (item["queryText"], item["searchPv"], item["queryRank"])
            for item in first_page["queries"]
        ] == [("香水", 797, 223), ("中 PV 词", 643, 50)]
        assert [
            (item["queryText"], item["searchPv"], item["queryRank"])
            for item in second_page["queries"]
        ] == [("低 PV 词", 120, 1)]
        assert first_page["total"] == 3
        assert first_page["hasNext"] is True
        assert second_page["hasNext"] is False
    finally:
        engine.dispose()


def test_brand_query_list_and_detail_share_downstream_governance_status():
    engine, factory = _database()
    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="query-governance:atlas:2026-09-25",
                brand="atlas", data_date=date(2026, 9, 25),
                query_text="拿破仑外套 女士", status="completed",
                s0_snapshot=json.dumps({"result_count": 4, "products": []}),
            )
            session.add(task)
            session.flush()
            session.add(QueryGovernanceStepStatus(
                task_id=task.id, node_key="recall", status="executed",
                result_json=json.dumps({"result": {
                    "status": "needs_attention", "reasons": ["result_count_drop"],
                    "evidence": {"result_count": 4, "previous_result_count": 20},
                }}),
            ))
            session.add(QueryGovernanceAsyncTask(
                task_id=task.id, batch_id=task.batch_id, brand=task.brand,
                query_text=task.query_text, trigger_source="recall_anomaly",
                idempotency_key="kg:test:napoleon", status="error",
                error_code="KG_AGENT_INVALID_JSON",
                error_message="agent response must be JSON",
            ))
            session.commit()
            task_id = int(task.id)

        service = QueryGovernanceReadService(factory)
        rail = service.brand_queries("atlas", data_date=date(2026, 9, 25))
        detail = service.query_workspace(task_id)
        rail_recall = next(step for step in rail["queries"][0]["stepStatuses"] if step["checkKey"] == "recall")
        detail_recall = next(step for step in detail["query"]["stepStatuses"] if step["checkKey"] == "recall")

        assert rail_recall["status"] == detail_recall["status"] == "executed"
        assert rail_recall["evidence"]["issueTypes"] == ["result_count_drop"]
        assert rail_recall["governanceStatus"] == detail_recall["governanceStatus"]
        assert rail_recall["governanceStatus"]["label"] == "生成失败"
        assert detail["governancePlans"][0]["statusLabel"] == "生成失败"
    finally:
        engine.dispose()



def test_brand_queries_all_filter_uses_sql_page_for_task_cards():
    engine, factory = _database()
    try:
        batch = QuerySelectionBatch(
            batch_id="query-governance:atlas:2026-09-14",
            brand="atlas",
            data_date=date(2026, 9, 14),
            config=QuerySelectionConfig(extract_limit=3),
            candidates=tuple(
                QuerySelectionDecision(
                    query_id=f"q-{index}", query=f"词-{index}", rules=("test",),
                    metrics={"search_pv": 100 + index, "search_uv": 10},
                    source_type="test", rule_count=1, ctr_decline_pp=0,
                    target_day_pv=100 + index, selection_rank=index, selected=True,
                )
                for index in range(3)
            ),
        )
        SqlAlchemyQuerySelectionStore(factory).save_batch(batch)
        QueryGovernanceReadService(factory).materialize_overview_summary(
            "atlas", data_date=date(2026, 9, 14)
        )
        statements: list[str] = []

        def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(" ".join(statement.lower().split()))

        event.listen(engine, "before_cursor_execute", capture_sql)
        try:
            QueryGovernanceReadService(factory).brand_queries(
                "atlas", data_date=date(2026, 9, 14), page=1, page_size=1
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture_sql)

        task_reads = [
            statement for statement in statements
            if " from query_governance_task " in statement
        ]
        assert any(" limit " in statement for statement in task_reads)
        page_reads = [
            statement for statement in task_reads
            if "query_governance_candidate" in statement and " limit " in statement
        ]
        assert len(page_reads) == 1
        assert "query_governance_candidate.batch_id =" in page_reads[0]
        assert "query_governance_candidate.brand =" in page_reads[0]
        batch_step_reads = [
            statement for statement in statements
            if "join query_governance_task" in statement
            and "query_governance_step_status" in statement
        ]
        assert batch_step_reads
        assert all("result_json" not in statement for statement in batch_step_reads)
    finally:
        engine.dispose()


def test_brand_queries_filtered_path_uses_materialized_task_ids():
    engine, factory = _database()
    _seed(factory)
    service = QueryGovernanceReadService(factory)
    try:
        service.materialize_overview_summary("atlas", data_date=date(2026, 8, 28))
        statements: list[str] = []

        def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(" ".join(statement.lower().split()))

        event.listen(engine, "before_cursor_execute", capture_sql)
        try:
            listing = service.brand_queries(
                "atlas", data_date=date(2026, 8, 28), page=1, page_size=1, filter_key="still_problematic"
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture_sql)

        assert listing["total"] == 1
        batch_step_reads = [
            statement for statement in statements
            if "join query_governance_task" in statement
            and "query_governance_step_status" in statement
        ]
        assert batch_step_reads
        assert all("result_json" not in statement for statement in batch_step_reads)
    finally:
        engine.dispose()


def test_overview_reads_only_the_lightweight_s0_snapshot_projection_for_metrics():
    engine, factory = _database()
    _seed(factory)
    service = QueryGovernanceReadService(factory)
    statements: list[str] = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        if " from query_governance_task " in normalized:
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        service.overview(["atlas"], data_date=date(2026, 8, 28))
        service.brand_queries("atlas", data_date=date(2026, 8, 28), page=1, page_size=1)
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        engine.dispose()

    assert statements
    snapshot_reads = [statement for statement in statements if "s0_snapshot" in statement]
    assert snapshot_reads
    assert all(
        "query_governance_task.id" in statement and "query_governance_task.s0_snapshot" in statement
        for statement in snapshot_reads
    )


def test_steps_for_tasks_uses_one_indexed_read_for_large_task_id_sets():
    engine, factory = _database()
    try:
        with factory() as session:
            session.add_all([
                QueryGovernanceStepStatus(
                    task_id=task_id,
                    node_key="recall",
                    status="completed",
                    result_json="{}",
                )
                for task_id in range(1, 31)
            ])
            session.commit()

        statements: list[str] = []

        def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
            normalized = " ".join(statement.lower().split())
            if " from query_governance_step_status " in normalized:
                statements.append(normalized)

        event.listen(engine, "before_cursor_execute", capture_sql)
        try:
            with factory() as session:
                rows = QueryGovernanceReadService._steps_for_tasks(
                    session,
                    [SimpleNamespace(id=task_id) for task_id in range(1, 31)],
                )
        finally:
            event.remove(engine, "before_cursor_execute", capture_sql)

        assert len(rows) == 30
        assert len(statements) == 1
    finally:
        engine.dispose()


def test_overview_step_projection_omits_s0_result_json():
    engine, factory = _database()
    try:
        with factory() as session:
            session.add_all([
                QueryGovernanceStepStatus(
                    task_id=1,
                    node_key="s0",
                    status="completed",
                    result_json='{"products":[1]}',
                ),
                QueryGovernanceStepStatus(
                    task_id=1,
                    node_key="recall",
                    status="completed",
                    result_json='{"result_count":0}',
                ),
            ])
            session.commit()

        statements: list[str] = []

        def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
            if "query_governance_step_status" in statement.lower():
                statements.append(" ".join(statement.lower().split()))

        event.listen(engine, "before_cursor_execute", capture_sql)
        try:
            with factory() as session:
                rows = QueryGovernanceReadService._steps_for_tasks(
                    session,
                    [SimpleNamespace(id=1)],
                    include_s0_result=False,
                )
        finally:
            event.remove(engine, "before_cursor_execute", capture_sql)

        by_node = {row.node_key: row for row in rows}
        assert by_node["s0"].result_json is None
        assert by_node["recall"].result_json == '{"result_count":0}'
        assert len(statements) == 2
        assert "result_json" not in statements[0]
        assert "node_key !=" in statements[1]
    finally:
        engine.dispose()


def test_latest_empty_day_does_not_fall_back_to_old_tasks_or_hide_failure():
    engine, factory = _database()
    _seed(factory)
    batch_id = "query-governance:atlas:2026-09-07"
    SqlAlchemyQuerySelectionStore(factory).save_batch(QuerySelectionBatch(
        batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 7),
        config=QuerySelectionConfig(), candidates=(),
    ))
    service = QueryGovernanceReadService(factory)
    assert service.brand_detail("atlas")["batch"]["batchId"] == batch_id
    with factory() as session:
        session.add(QueryGovernanceWorkflowRun(batch_id=batch_id, status="error"))
        session.commit()
    payload = service.brand_detail("atlas")["batch"]
    assert payload["status"] == "error"
    assert payload["reason"] is None
    engine.dispose()


def test_query_task_ordering_compiles_without_mysql_nulls_last():
    statement = select(QueryGovernanceTask).order_by(
        *QueryGovernanceReadService._query_task_ordering()
    )

    compiled = str(statement.compile(dialect=mysql.dialect()))

    assert "NULLS LAST" not in compiled
    assert "query_rank IS NULL ASC" in compiled


def test_query_tasks_sort_null_ranks_after_ranked_tasks():
    engine, factory = _database()
    try:
        with factory() as session:
            session.add_all([
                QueryGovernanceTask(
                    id=2, batch_id="batch-order", brand="atlas",
                    data_date=date(2026, 9, 18), query_text="unranked",
                    query_rank=None, status="completed",
                ),
                QueryGovernanceTask(
                    id=1, batch_id="batch-order", brand="atlas",
                    data_date=date(2026, 9, 18), query_text="ranked",
                    query_rank=1, status="completed",
                ),
            ])
            session.commit()
            tasks = QueryGovernanceReadService._tasks(
                session, "batch-order", brand="atlas"
            )

        assert [task.query_text for task in tasks] == ["ranked", "unranked"]
    finally:
        engine.dispose()


def test_overview_reuses_one_step_and_workflow_read_per_brand():
    engine, factory = _database()
    _seed(factory)
    with factory() as session:
        batch = QueryGovernanceSelectionBatch(
            batch_id="batch-atlas-20260829",
            brand="atlas",
            data_date=date(2026, 8, 28),
            config_json="{}",
        )
        session.add(batch)
        session.flush()
        session.add(QueryGovernanceCandidate(
            selection_batch_id=batch.id,
            batch_id=batch.batch_id,
            brand="atlas",
            data_date=batch.data_date,
            query_id="q-overview",
            query_key="q-overview",
            query_text="连衣裙",
            matched_rules_json='["high_pv"]',
            metrics_json="{}",
            source_type="test",
            selection_rank=1,
            selected=True,
        ))
        session.commit()
    statements: list[str] = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        payload = QueryGovernanceReadService(factory).overview(["atlas"])
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        engine.dispose()

    assert payload["brands"][0]["brand"] == "atlas"
    assert sum(" from query_governance_step_status " in statement for statement in statements) == 2
    assert sum(" from query_governance_workflow_run " in statement for statement in statements) == 1
    candidate_reads = [
        statement for statement in statements
        if " from query_governance_candidate " in statement
    ]
    assert len(candidate_reads) == 1
    assert "selection_batch_id" in candidate_reads[0]
    assert "query_governance_candidate.batch_id =" not in candidate_reads[0]


def test_materialized_overview_summary_avoids_detail_table_reads():
    engine, factory = _database()
    _seed(factory)
    service = QueryGovernanceReadService(factory)
    try:
        summary = service.materialize_overview_summary(
            "atlas", data_date=date(2026, 8, 28)
        )
        with factory() as session:
            stored = session.execute(
                select(QueryGovernanceBatchSummary).where(
                    QueryGovernanceBatchSummary.batch_id == summary["batchId"],
                    QueryGovernanceBatchSummary.brand == "atlas",
                )
            ).scalar_one()
            assert json.loads(stored.payload_json)["queryCount"] == summary["queryCount"]

        statements: list[str] = []

        def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(" ".join(statement.lower().split()))

        event.listen(engine, "before_cursor_execute", capture_sql)
        try:
            overview = service.overview(["atlas"], data_date=date(2026, 8, 28))
        finally:
            event.remove(engine, "before_cursor_execute", capture_sql)

        expected = {
            **summary,
            "telemetry": {
                key: value for key, value in summary["telemetry"].items()
                if key != "irrelevantProductPairs"
            },
        }
        # 相关性检测准确率的分子是读时投影：0 个不相关组合时为 0/无比率。
        expected["telemetry"]["blacklistedProductCount"] = 0
        expected["telemetry"]["relevanceDetectionAccuracy"] = None
        assert overview["brands"] == [expected]
        assert any(" from query_governance_batch_summary " in statement for statement in statements)
        assert not any(" from query_governance_step_status " in statement for statement in statements)
        assert not any(" from query_governance_candidate " in statement for statement in statements)
        assert not any(" from query_governance_agent_event " in statement for statement in statements)
    finally:
        engine.dispose()


def test_overview_rebuilds_legacy_summary_before_returning_pass_rate():
    engine, factory = _database()
    _seed(factory)
    service = QueryGovernanceReadService(factory)
    try:
        summary = service.materialize_overview_summary("atlas", data_date=date(2026, 8, 28))
        with factory() as session:
            stored = session.execute(
                select(QueryGovernanceBatchSummary).where(
                    QueryGovernanceBatchSummary.batch_id == summary["batchId"],
                    QueryGovernanceBatchSummary.brand == "atlas",
                )
            ).scalar_one()
            payload = json.loads(stored.payload_json)
            payload["telemetry"].pop("qualityMetricsVersion", None)
            payload["telemetry"]["passCount"] = 1
            payload["telemetry"]["passRate"] = 1.0
            stored.payload_json = json.dumps(payload)
            session.commit()

        overview = service.overview(["atlas"], data_date=date(2026, 8, 28))

        assert overview["brands"][0]["telemetry"]["passCount"] == 0
        assert overview["brands"][0]["telemetry"]["passRate"] == 0.0
        assert overview["brands"][0]["telemetry"]["qualityMetricsVersion"] == 6
    finally:
        engine.dispose()


def test_overview_reads_materialized_summaries_for_all_brands_in_one_query():
    engine, factory = _database()
    _seed(factory)
    with factory() as session:
        session.add(QueryGovernanceBatchSummary(
            batch_id="batch-harbor-20260829",
            brand="harbor",
            data_date=date(2026, 8, 28),
            payload_json=json.dumps({
                "brand": "harbor",
                "batchId": "batch-harbor-20260829",
                "dataDate": "2026-08-28",
                "status": "completed",
                "reason": None,
                "queryCount": 2,
                "nodeStatusCounts": {},
                "durationSeconds": 1,
                "startedAt": None,
                "completedAt": None,
                "telemetry": {},
            }),
        ))
        session.commit()
    service = QueryGovernanceReadService(factory)
    service.materialize_overview_summary("atlas", data_date=date(2026, 8, 28))
    statements: list[str] = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        payload = service.overview(["atlas", "harbor"], data_date=date(2026, 8, 28))
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)
        engine.dispose()

    assert {item["brand"] for item in payload["brands"]} == {"atlas", "harbor"}
    summary_reads = [
        statement for statement in statements
        if " from query_governance_batch_summary " in statement
    ]
    assert len(summary_reads) == 1
    assert " in (" in summary_reads[0]


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def test_check_visibility_matches_brand_filters_query_cards_and_detail():
    engine, session_factory = _database()
    try:
        batch_id = "query-governance:atlas:2026-10-02"
        with session_factory() as session:
            session.add_all([
                QueryGovernanceSelectionBatch(
                    batch_id=batch_id, brand="atlas", data_date=date(2026, 10, 2),
                    config_json=json.dumps({"check_modes": {
                        "recall": True, "open_factor": False,
                        "relevance": False, "inventory": False,
                    }}),
                ),
                QueryGovernanceWorkflowRun(
                    batch_id=batch_id, status="completed",
                    started_at=datetime(2026, 10, 3, 0, 30),
                ),
            ])
            executed = QueryGovernanceTask(
                batch_id=batch_id, brand="atlas", data_date=date(2026, 10, 2),
                query_text="有历史相关性检测", query_rank=1, status="completed",
            )
            disabled = QueryGovernanceTask(
                batch_id=batch_id, brand="atlas", data_date=date(2026, 10, 2),
                query_text="未检测", query_rank=2, status="completed",
            )
            session.add_all([executed, disabled])
            session.flush()
            session.add_all([
                QueryGovernanceStepStatus(
                    task_id=executed.id, node_key="relevance", status="executed",
                    result_json='{"assessment":"healthy"}',
                ),
                QueryGovernanceStepStatus(
                    task_id=disabled.id, node_key="relevance", status="skipped",
                    result_json=json.dumps({"reason": "品牌未启用相关性检测"}, ensure_ascii=False),
                ),
            ])
            executed_id, disabled_id = executed.id, disabled.id
            session.commit()

        service = QueryGovernanceReadService(session_factory)
        listing = service.brand_queries("atlas", data_date=date(2026, 10, 2))
        brand_detail = service.brand_detail("atlas", data_date=date(2026, 10, 2))
        executed_detail = service.query_detail(executed_id)
        disabled_detail = service.query_detail(disabled_id)

        assert listing["visibleCheckKeys"] == ["recall", "relevance"]
        assert brand_detail["visibleCheckKeys"] == listing["visibleCheckKeys"]
        by_id = {item["id"]: item for item in listing["queries"]}
        assert by_id[executed_id]["visibleCheckKeys"] == ["recall", "relevance"]
        assert by_id[disabled_id]["visibleCheckKeys"] == ["recall"]
        assert executed_detail["visibleCheckKeys"] == by_id[executed_id]["visibleCheckKeys"]
        assert disabled_detail["visibleCheckKeys"] == by_id[disabled_id]["visibleCheckKeys"]
    finally:
        engine.dispose()


def test_independent_inventory_activity_after_utc_midnight_is_visible_on_run_day():
    engine, session_factory = _database()
    try:
        batch_id = "query-governance:atlas:2026-09-28"
        with session_factory() as session:
            session.add_all([
                QueryGovernanceSelectionBatch(
                    batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 28),
                    config_json=json.dumps({"check_modes": {
                        "recall": True, "open_factor": False,
                        "relevance": True, "inventory": False,
                    }}),
                ),
                QueryGovernanceWorkflowRun(
                    batch_id=batch_id, status="completed",
                    started_at=datetime(2026, 9, 28, 16, 30),
                ),
            ])
            affected = QueryGovernanceTask(
                batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 28),
                query_text="短外套 女士", query_rank=1, status="completed",
            )
            unaffected = QueryGovernanceTask(
                batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 28),
                query_text="牛仔裤 女士", query_rank=2, status="completed",
            )
            session.add_all([affected, unaffected])
            session.flush()
            affected_id, unaffected_id = affected.id, unaffected.id
            session.add(InventoryRecoveryTask(
                brand="atlas", product_id="C05536064401", product_name="排扣外套",
                original_query="短外套 女士", last_check_stock=75, status="monitoring",
                created_at=datetime(2026, 9, 29, 6, 5),
                updated_at=datetime(2026, 9, 29, 6, 5),
            ))
            session.commit()

        service = QueryGovernanceReadService(session_factory)
        listing = service.brand_queries("atlas")
        detail = service.brand_detail("atlas")
        affected_detail = service.query_detail(affected_id)
        unaffected_detail = service.query_detail(unaffected_id)

        assert listing["total"] == 2
        assert listing["dataDate"] == "2026-09-28"
        listed = {item["id"]: item for item in listing["queries"]}
        assert "inventory" in listing["visibleCheckKeys"]
        assert "inventory" in detail["visibleCheckKeys"]
        assert "inventory" in listed[affected_id]["visibleCheckKeys"]
        assert "inventory" not in listed[unaffected_id]["visibleCheckKeys"]
        assert "inventory" in affected_detail["visibleCheckKeys"]
        assert "inventory" not in unaffected_detail["visibleCheckKeys"]
    finally:
        engine.dispose()


def test_completed_inventory_scan_shows_zero_issue_query_in_list_and_detail():
    engine, session_factory = _database()
    try:
        batch_id = "query-governance:atlas:2026-09-28"
        with session_factory() as session:
            session.add_all([
                QueryGovernanceSelectionBatch(
                    batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 28),
                    config_json=json.dumps({"check_modes": {"recall": True, "inventory": True}}),
                ),
                QueryGovernanceWorkflowRun(
                    batch_id=batch_id, status="completed", started_at=datetime(2026, 9, 28, 16, 30),
                ),
                SchedulerJobRun(
                    job_id="inventory_monitor_job:atlas", scheduled_for=datetime(2026, 9, 29, 6),
                    idempotency_key="inventory-scan-test", status="succeeded", attempt_no=1,
                    claim_owner="test", claim_token="test", release="test", config_fingerprint="a" * 64,
                    claimed_at=datetime(2026, 9, 29, 6), started_at=datetime(2026, 9, 29, 6),
                    heartbeat_at=datetime(2026, 9, 29, 6), terminal_at=datetime(2026, 9, 29, 6, 5),
                    terminal_summary="scheduler job succeeded duration_ms=100",
                ),
            ])
            checked = QueryGovernanceTask(
                batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 28),
                query_text="牛仔裤 女士", query_rank=1, status="completed",
            )
            unchecked = QueryGovernanceTask(
                batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 28),
                query_text="衬衫 女士", query_rank=2, status="completed",
            )
            session.add_all([checked, unchecked])
            session.flush()
            checked_id, unchecked_id = checked.id, unchecked.id
            session.commit()

        service = QueryGovernanceReadService(session_factory)
        listing = service.brand_queries("atlas")
        checked_card = next(item for item in listing["queries"] if item["id"] == checked_id)
        unchecked_card = next(item for item in listing["queries"] if item["id"] == unchecked_id)
        for card in (checked_card, unchecked_card):
            step = next(step for step in card["stepStatuses"] if step["checkKey"] == "inventory")
            assert step["status"] == "executed"
            assert step["evidence"]["issueTypes"] == []
            assert step["evidence"]["checkedAt"] == "2026-09-29T06:05:00"
        brand_detail = service.brand_detail("atlas")
        assert all(
            any(step["checkKey"] == "inventory" for step in item["stepStatuses"])
            for item in brand_detail["queries"]
        )
        detail = service.query_detail(checked_id)
        assert detail["query"]["stepStatuses"][-1]["checkKey"] == "inventory"
        assert detail["inventory"] == []
        assert detail["inventoryExecution"]["status"] == "success"

        with session_factory() as session:
            scan = session.execute(select(SchedulerJobRun)).scalar_one()
            scan.status = "failed"
            session.commit()
        failed = service.brand_queries("atlas")
        assert all(
            not any(step["checkKey"] == "inventory" for step in item["stepStatuses"])
            for item in failed["queries"]
        )

        with session_factory() as session:
            scan = session.execute(select(SchedulerJobRun)).scalar_one()
            scan.status = "succeeded"
            scan.started_at = datetime(2026, 9, 28, 15)
            session.commit()
        # Calendar-day fallback: a succeeded scan within the data date's
        # calendar day is still surfaced even when it predates the run.
        stale = service.brand_queries("atlas")
        assert all(
            any(step["checkKey"] == "inventory" for step in item["stepStatuses"])
            for item in stale["queries"]
        )
    finally:
        engine.dispose()


def _seed(session_factory):
    with session_factory() as session:
        run = QueryGovernanceWorkflowRun(
            batch_id="batch-atlas-20260829",
            status="completed",
            current_node=None,
            model_config_version=7,
            started_at=datetime(2026, 8, 29, 0, 30),
            finished_at=datetime(2026, 8, 29, 0, 42),
        )
        task = QueryGovernanceTask(
            batch_id=run.batch_id,
            brand="atlas",
            data_date=date(2026, 8, 28),
            query_text="连衣裙",
            query_rank=1,
            status="completed",
            s0_snapshot=json.dumps(
                {
                    "result_count": 2,
                    "products": [
                        {"product_id": "P1", "rank": 1, "product_name": "黑色连衣裙"},
                        {"product_id": "P2", "rank": 2, "product_name": "白色连衣裙"},
                    ],
                },
                ensure_ascii=False,
            ),
            started_at=datetime(2026, 8, 29, 0, 31),
            completed_at=datetime(2026, 8, 29, 0, 40),
        )
        session.add_all([run, task])
        session.flush()
        session.add_all(
            [
                QueryGovernanceStepStatus(
                    task_id=task.id,
                    node_key="s0",
                    status="executed",
                    result_json=json.dumps({"result_count": 2}),
                    duration_seconds=1.2,
                ),
                QueryGovernanceStepStatus(
                    task_id=task.id,
                    node_key="recall",
                    status="executed",
                    result_json=json.dumps({"assessment": "healthy"}),
                    duration_seconds=2.3,
                ),
                QueryGovernanceStepStatus(
                    task_id=task.id,
                    node_key="reorder",
                    status="skipped",
                    result_json=json.dumps({"reason": "CTR/CVR 正常"}, ensure_ascii=False),
                    duration_seconds=0.1,
                ),
                QueryGovernanceStepStatus(
                    task_id=task.id,
                    node_key="relevance",
                    status="error",
                    error_code="MODEL_TIMEOUT",
                    error_message="相关性模型超时",
                    duration_seconds=30,
                ),
                QueryGovernanceActionPlan(
                    task_id=task.id,
                    batch_id=run.batch_id,
                    brand="atlas",
                    query_text="连衣裙",
                    node_key="reorder",
                    action_type="query_reorder",
                    idempotency_key="reorder-1",
                    status="verified",
                    executor_type="ai_auto",
                    request_json=json.dumps({"preference": "夏季"}, ensure_ascii=False),
                    response_json=json.dumps({"verified": True}),
                ),
                QueryGovernanceAsyncTask(
                    task_id=task.id,
                    batch_id=run.batch_id,
                    brand="atlas",
                    query_text="连衣裙",
                    trigger_source="recall_anomaly",
                    idempotency_key="kg-1",
                    status="pending",
                    evidence_json=json.dumps({"reason": "low_result"}),
                ),
                QueryGovernanceContextSnapshot(
                    batch_id=run.batch_id,
                    brand="atlas",
                    context_type="brand_context",
                    payload=json.dumps({"social_trends": ["度假"]}, ensure_ascii=False),
                ),
                QueryGovernanceAgentEvent(
                    task_id=task.id,
                    brand="atlas",
                    query_text="连衣裙",
                    agent_name="QueryPreferenceAgent",
                    node_key="reorder",
                    event_type="completed",
                    input_json=json.dumps({"query": "连衣裙"}, ensure_ascii=False),
                    output_json=json.dumps({"preference": "夏季", "batchQueryCount": 2}, ensure_ascii=False),
                    duration_seconds=2.5,
                    model_name="qwen3.6-plus",
                    model_role="primary",
                    fallback_used=False,
                    token_usage=120,
                ),
                InventoryRecoveryTask(
                    brand="atlas",
                    product_id="P1",
                    product_name="黑色连衣裙",
                    original_query="连衣裙",
                    last_check_stock=75,
                    status="monitoring",
                    created_at=datetime(2026, 8, 29, 1, 0),
                    updated_at=datetime(2026, 8, 29, 1, 5),
                ),
            ]
        )
        config_version = NodeModelConfigVersion(
            version=7,
            status="published",
            active_slot="active",
            change_note="治理节点默认配置",
        )
        config_version.entries.append(
            NodeModelConfigEntry(
                node_key="query_governance.preference_generation",
                channel="rd_llm",
                primary_model="qwen3.6-plus",
                fallback_model="qwen3.7-max",
            )
        )
        experiment = QueryGovernanceExperiment(
            brand="atlas",
            experiment_name="夏季查询实验",
            status="running",
            sample_data_date=date(2026, 8, 28),
        )
        session.add_all([config_version, experiment])
        session.flush()
        session.add(
            QueryGovernanceExperimentSample(
                experiment_id=experiment.id,
                brand="atlas",
                query_text="连衣裙",
                group_type="treatment",
                sample_rank=1,
            )
        )
        task.experiment_id = experiment.id
        task.group_type = "treatment"
        session.commit()
        return task.id


def test_read_service_uses_only_new_query_governance_tables_and_five_states():
    engine, session_factory = _database()
    try:
        task_id = _seed(session_factory)
        service = QueryGovernanceReadService(session_factory)

        overview = service.overview(["atlas"])
        overview_brand = overview["brands"][0]
        assert {
            key: overview_brand[key]
            for key in (
                "brand",
                "batchId",
                "dataDate",
                "status",
                "queryCount",
                "nodeStatusCounts",
                "durationSeconds",
                "startedAt",
                "completedAt",
            )
        } == {
            "brand": "atlas",
            "batchId": "batch-atlas-20260829",
            "dataDate": "2026-08-28",
            "status": "executed",
            "queryCount": 1,
            "nodeStatusCounts": {
                "pending": 0,
                "running": 0,
                "executed": 2,
                "skipped": 1,
                "error": 1,
            },
            "durationSeconds": 720.0,
            "startedAt": "2026-08-29T00:30:00",
            "completedAt": "2026-08-29T00:42:00",
        }
        assert overview_brand["telemetry"]["queryCount"] == 1

        detail = service.query_detail(task_id)
        assert detail["query"]["status"] == "error"
        assert [node["nodeKey"] for node in detail["nodes"]] == [
            "s0",
            "reorder",
            "relevance",
            "recall",
        ]
        assert {node["status"] for node in detail["nodes"]} <= {
            "pending",
            "running",
            "executed",
            "skipped",
            "error",
        }
        relevance_node = next(
            node for node in detail["nodes"] if node["nodeKey"] == "relevance"
        )
        assert relevance_node["errorCode"] == "MODEL_TIMEOUT"
        assert relevance_node["errorMessage"] == "相关性模型超时"
        assert relevance_node["durationSeconds"] == 30
        assert relevance_node["tokenUsage"] is None
        assert relevance_node["tokenUsageMissing"] is True
        assert detail["s0"]["products"][0]["productId"] == "P1"
        assert detail["actions"][0]["actionType"] == "query_reorder"
        assert detail["slots"]["kgTasks"][0]["triggerSource"] == "recall_anomaly"
        assert detail["slots"]["sharedFacts"][0]["contextType"] == "brand_context"
        assert detail["slots"]["relevance"] == {"available": True}
        assert detail["slots"]["inventory"] == {"available": True, "source": "independent_job"}
        assert detail["relevance"]["blacklistCandidates"] == []
        assert detail["inventory"][0]["productId"] == "P1"
        assert detail["events"][0]["agentName"] == "QueryPreferenceAgent"
        assert "timeline" not in detail
        assert [plan["type"] for plan in detail["governancePlans"]] == ["reorder"]
        assert detail["governancePlans"][0]["statusLabel"] == "AI 已执行"
        assert detail["governancePlans"][0]["summary"] == "夏季"
        assert detail["experiment"]["groupType"] == "treatment"
        assert detail["modelConfig"]["nodes"][0]["primaryModel"] == "qwen3.6-plus"
        forbidden = {"plannerPlan", "inventoryBrokenRateThreshold", "price"}
        assert forbidden.isdisjoint(detail)
    finally:
        engine.dispose()


def test_brand_detail_exposes_query_issue_counts_and_step_evidence():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            batch_id = "batch-atlas-20260913"
            session.add(QueryGovernanceWorkflowRun(
                batch_id=batch_id,
                status="completed",
                started_at=datetime(2026, 9, 13, 0, 0, 0),
                finished_at=datetime(2026, 9, 13, 1, 0, 0),
            ))
            tasks = [
                QueryGovernanceTask(batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 13), query_text="少召回", status="completed"),
                QueryGovernanceTask(batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 13), query_text="不相关", status="completed"),
                QueryGovernanceTask(batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 13), query_text="重排", status="completed"),
                QueryGovernanceTask(batch_id=batch_id, brand="atlas", data_date=date(2026, 9, 13), query_text="骤降", status="completed"),
            ]
            session.add_all(tasks)
            session.flush()
            for task in tasks:
                session.add(QueryGovernanceStepStatus(
                    task_id=task.id, node_key="s0", status="executed",
                    result_json=json.dumps({"result_count": 2}),
                ))
            session.add_all([
                QueryGovernanceStepStatus(
                    task_id=tasks[0].id, node_key="recall", status="executed",
                    result_json=json.dumps({"result": {"status": "needs_attention", "reasons": ["low_result"], "evidence": {"result_count": 2}}}),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[1].id, node_key="recall", status="executed",
                    result_json=json.dumps({"result": {"status": "healthy", "reasons": []}}),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[2].id, node_key="recall", status="executed",
                    result_json=json.dumps({"result": {"status": "healthy", "reasons": []}}),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[3].id, node_key="recall", status="executed",
                    result_json=json.dumps({"result": {"status": "needs_attention", "reasons": ["result_count_drop"]}}),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[0].id, node_key="reorder", status="skipped",
                    result_json=json.dumps({"result": {"should_reorder": False}}),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[1].id, node_key="reorder", status="executed",
                    result_json=json.dumps({"result": {"should_reorder": False}}),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[2].id, node_key="reorder", status="error",
                    result_json=json.dumps({"result": {"should_reorder": True}}),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[0].id, node_key="relevance", status="executed",
                    result_json=json.dumps({"result": {"blacklist_candidates": [{"product_id": "p1"}]}}),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[1].id, node_key="relevance", status="executed",
                    result_json=json.dumps({"result": {"blacklist_candidates": []}}),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[2].id, node_key="relevance", status="executed",
                    result_json=json.dumps({"result": {"blacklist_candidates": [{"product_id": "p2"}, {"product_id": "p3"}]}}),
                ),
                InventoryRecoveryTask(
                    brand="atlas", product_id="p4", product_name="断码商品", original_query="重排",
                    last_check_stock=80, status="monitoring", updated_at=datetime(2026, 9, 13, 1, 0, 0),
                ),
                InventoryRecoveryTask(
                    brand="atlas", product_id="p-old", product_name="历史断码商品", original_query="少召回",
                    last_check_stock=80, status="monitoring", updated_at=datetime(2026, 9, 12, 23, 59, 59),
                ),
            ])
            session.commit()

        detail = QueryGovernanceReadService(session_factory).brand_detail("atlas", data_date=date(2026, 9, 13))
        assert detail["stats"]["stepIssueCounts"] == {
            "recall": 2, "relevance": 2, "open_factor": 1, "inventory": 1,
        }
        assert detail["stats"]["noResultOrLowResultCount"] == 1
        assert detail["stats"]["resultDropCount"] == 1
        assert detail["stats"]["relevanceIssueCount"] == 2
        assert detail["stats"]["openFactorIssueCount"] == 1
        assert detail["stats"]["inventoryProductIssueCount"] == 1
        by_query = {item["queryText"]: item for item in detail["queries"]}
        step_evidence = {
            item["checkKey"]: item["evidence"]
            for item in by_query["少召回"]["stepStatuses"]
        }
        assert step_evidence["recall"]["issueTypes"] == ["low_result"]
        assert step_evidence["relevance"]["issueTypes"] == ["blacklist_candidates"]
        step_evidence = {
            item["checkKey"]: item["evidence"]
            for item in by_query["重排"]["stepStatuses"]
        }
        assert step_evidence["open_factor"]["issueTypes"] == ["should_reorder"]

        recall_page = QueryGovernanceReadService(session_factory).brand_queries(
            "atlas", data_date=date(2026, 9, 13), page=1, page_size=1, filter_key="recall"
        )
        assert recall_page["total"] == 2
        assert recall_page["page"] == 1
        assert recall_page["pageSize"] == 1
        assert recall_page["hasNext"] is True
        assert [item["queryText"] for item in recall_page["queries"]] == ["少召回"]
        assert "nodes" not in recall_page["queries"][0]
        assert "telemetry" not in recall_page["queries"][0]
        recall_step_evidence = next(
            step
            for step in recall_page["queries"][0]["stepStatuses"]
            if step["checkKey"] == "recall"
        )
        assert "blacklistCandidates" not in recall_step_evidence.get("evidence", {})

        second_page = QueryGovernanceReadService(session_factory).brand_queries(
            "atlas", data_date=date(2026, 9, 13), page=2, page_size=1, filter_key="recall"
        )
        assert [item["queryText"] for item in second_page["queries"]] == ["骤降"]
    finally:
        engine.dispose()


def test_brand_queries_empty_filter_returns_empty_page_with_normalized_date():
    engine, session_factory = _database()
    try:
        batch_id = "query-governance:atlas:2026-09-13"
        with session_factory() as session:
            session.add(QueryGovernanceBatchSummary(
                batch_id=batch_id,
                brand="atlas",
                data_date=date(2026, 9, 13),
                payload_json=json.dumps({"dataDate": "2026-09-13"}),
            ))
            task = QueryGovernanceTask(
                batch_id=batch_id,
                brand="atlas",
                data_date=date(2026, 9, 13),
                query_text="正常 Query",
                status="completed",
            )
            session.add(task)
            session.flush()
            session.add(QueryGovernanceStepStatus(
                task_id=task.id,
                node_key="reorder",
                status="executed",
                result_json=json.dumps({"result": {"should_reorder": False}}),
            ))
            session.commit()

        listing = QueryGovernanceReadService(session_factory).brand_queries(
            "atlas",
            data_date=date(2026, 9, 13),
            filter_key="open_factor",
        )

        assert listing["dataDate"] == "2026-09-13"
        assert listing["batch"]["dataDate"] == "2026-09-13"
        assert listing["queries"] == []
        assert listing["total"] == 0
        assert listing["hasNext"] is False
    finally:
        engine.dispose()


def test_read_service_exposes_persisted_recall_conclusion_and_kg_strategy_with_mixed_tag_counts():
    engine, factory = _database()
    try:
        with factory() as session:
            task = QueryGovernanceTask(
                batch_id="batch-recall-display",
                brand="atlas",
                data_date=date(2026, 9, 7),
                query_text="轻薄外套",
                status="completed",
                s0_snapshot=json.dumps({"result_count": 2, "products": []}, ensure_ascii=False),
            )
            session.add(task)
            session.flush()
            session.add(QueryGovernanceStepStatus(
                task_id=task.id,
                node_key="recall",
                status="executed",
                result_json=json.dumps({
                    "summary": "本次召回 2 件，数量不足，需要交由 KG Agent 生成治理策略。",
                    "evidence": {
                        "result_count": 2,
                        "previous_result_count": 8,
                        "comparison_status": "normal",
                    },
                }, ensure_ascii=False),
            ))
            session.add(QueryGovernanceAsyncTask(
                task_id=task.id,
                batch_id=task.batch_id,
                brand=task.brand,
                query_text=task.query_text,
                trigger_source="recall_anomaly",
                idempotency_key="kg-display",
                status="plan_generated",
                plan_json=json.dumps({
                    "actions": [
                        {"actionType": "add_tag", "params": {"tagName": "轻薄"}, "reason": "补充标签"},
                        {"actionType": "add_relationship", "params": {"tagName": "轻薄", "responseTagName": "外套", "relationships": "902227092548485120"}, "reason": "建立从属关系"},
                    ],
                }, ensure_ascii=False),
            ))
            session.commit()
            task_id = task.id

        def mixed(_brand, query, _limit):
            return {"success": True, "products": [{"product_id": f"{query}-1"}], "total_count": 1, "result_complete": True}

        detail = QueryGovernanceReadService(factory, live_search=mixed).query_detail(task_id)
        assert detail["recall"]["conclusion"] == "本次召回 2 件，数量不足，需要交由 KG Agent 生成治理策略。"
        assert detail["recall"]["previousResultCount"] == 8
        assert detail["recall"]["kgStrategy"]["items"] == [
            "新增标签：轻薄",
            "将轻薄与外套建立从属关系",
        ]
        assert detail["recall"]["kgStrategy"]["tags"] == [
            {"name": "轻薄", "status": "pending_create", "resultCount": None},
            {"name": "外套", "status": "exists", "resultCount": 1},
        ]
    finally:
        engine.dispose()


def test_relevance_payload_uses_only_final_plan_groups_not_detection_evidence():
    payload = QueryGovernanceReadService._relevance_payload(
        nodes=[],
        query_text="轻薄外套",
        async_tasks=[SimpleNamespace(
            trigger_source="relevance_root_cause",
            plan_json="null",
            evidence_json="null",
        )],
    )

    assert payload["query"] == "轻薄外套"
    assert payload["causeGroups"] == []

    evidence_payload = QueryGovernanceReadService._relevance_payload(
        nodes=[],
        query_text="轻薄外套",
        async_tasks=[SimpleNamespace(
            trigger_source="relevance_root_cause",
            plan_json=None,
            evidence_json=json.dumps({"causeGroups": [{"groupId": "g-1"}]}),
        )],
    )

    assert evidence_payload["causeGroups"] == []


def test_query_detail_exposes_structured_reorder_evidence_and_configured_product_limit(monkeypatch):
    monkeypatch.setattr(
        "src.query_governance.read_api.get_brand_business_config",
        lambda brand: {"top_query_check_product_limit": 8} if brand == "atlas" else {},
    )
    engine, factory = _database()
    try:
        with factory() as session:
            batch = QueryGovernanceSelectionBatch(
                batch_id="batch-reorder-evidence",
                brand="atlas",
                data_date=date(2026, 9, 7),
                config_json=json.dumps({"alert_ctr_gap_pp": 5, "alert_cvr_gap_pp": 2, "history_days": 7}),
            )
            task = QueryGovernanceTask(
                batch_id=batch.batch_id,
                brand="atlas",
                data_date=batch.data_date,
                query_text="轻薄外套",
                status="completed",
                s0_snapshot=json.dumps({"result_count": 24, "products": []}),
            )
            session.add(batch)
            session.add(task)
            session.flush()
            session.add(QueryGovernanceCandidate(
                selection_batch_id=batch.id,
                batch_id=batch.batch_id,
                brand="atlas",
                data_date=batch.data_date,
                query_id="q-reorder-evidence",
                query_key="轻薄外套",
                query_text="轻薄外套",
                matched_rules_json=json.dumps(["ctr_decline", "cvr_decline"]),
                metrics_json=json.dumps({
                    "search_pv": 100,
                    "search_uv": 100,
                    "click_uv": 6.2,
                    "purchase_uv": 1.1,
                    "ctr": 0.062,
                    "cvr": 0.011,
                    "history_ctr": 0.124,
                    "history_cvr": 0.038,
                }),
                source_type="dynamic",
                selection_rank=1,
                selected=True,
            ))
            session.commit()
            task_id = task.id

        detail = QueryGovernanceReadService(factory).query_detail(task_id)
        assert detail["reorder"] == {
            "currentCtr": 0.062,
            "recentCtr": 0.124,
            "ctrChange": -6.2,
            "ctrAlertThreshold": 5.0,
            "currentCvr": 0.011,
            "recentCvr": 0.038,
            "cvrChange": -2.7,
            "cvrAlertThreshold": 2.0,
            "recallCount": 24,
            "lowRecallThreshold": 4,
            "productsLimit": 8,
            "requiresReorder": True,
            "strategyStatus": "pending",
            "executionStatus": "not_run",
            "conclusion": "当前 CTR、CVR 较近期分别下降 6.2、2.7 个百分点，均已达到告警阈值，触发重排序。AI 重排序策略生成中，重排序未执行。",
        }
    finally:
        engine.dispose()


def test_inventory_payload_uses_check_history_and_preserves_image_and_recovery_transition():
    record = SimpleNamespace(
        id=7,
        product_id="SKU-7",
        product_name="恢复商品",
        original_query="连衣裙",
        last_check_stock=13,
        status="recovered",
        created_at=datetime(2026, 9, 19, 0, 0),
        updated_at=datetime(2026, 9, 21, 12, 0),
    )

    payload = QueryGovernanceReadService._inventory_payload(
        record,
        checks=[
            {"checked_at": datetime(2026, 9, 20, 3, 0), "broken_rate_percent": 83, "status": "monitoring"},
            {"checked_at": datetime(2026, 9, 21, 12, 0), "broken_rate_percent": 13, "status": "recovered"},
        ],
        image_url="https://img.test/recovered.jpg",
    )

    assert payload["checkedAt"] == "2026-09-21T12:00:00"
    assert payload["imageUrl"] == "https://img.test/recovered.jpg"
    assert payload["brokenRateBefore"] == 83
    assert payload["recoveredAt"] == "2026-09-21T12:00:00"


def test_query_detail_exposes_failed_independent_inventory_action():
    engine, session_factory = _database()
    try:
        task_id = _seed(session_factory)
        with session_factory() as session:
            intent = ActionIntent(
                action_type="query_demote",
                idempotency_key="inventory-monitor:failed-read-model",
                payload_hash="a" * 64,
                payload_json=json.dumps({
                    "source": "inventory_monitor",
                    "brand": "atlas",
                    "query": "连衣裙",
                    "add_product_ids": ["P-failed"],
                    "remove_product_ids": [],
                }, ensure_ascii=False),
                operator="scheduler:inventory_monitor",
                brand="atlas",
                release="test",
                permission="execute_governance_actions",
                query_text="连衣裙",
                target="query_demotion",
                status="failed",
            )
            session.add(intent)
            session.flush()
            session.add(ActionTerminalFact(
                intent_id=intent.id,
                status="failed",
                terminal_code="executor_failed",
                result_json=json.dumps({
                    "status": "failed",
                    "error_code": "INVENTORY_ACTION_FAILED",
                    "error": "库存系统读回失败",
                }),
                replay_result_json=json.dumps({
                    "status": "failed",
                    "error_code": "INVENTORY_ACTION_FAILED",
                    "error": "库存系统读回失败",
                }),
                fact_hash="b" * 64,
            ))
            session.commit()

        detail = QueryGovernanceReadService(session_factory).query_detail(task_id)

        assert detail["inventoryExecution"] == {
            "available": False,
            "source": "independent_job",
            "status": "failed",
            "errorCode": "INVENTORY_ACTION_FAILED",
            "errorMessage": "库存系统读回失败",
        }
        assert detail["slots"]["inventory"] == {
            "available": False,
            "source": "independent_job",
            "status": "failed",
        }
    finally:
        engine.dispose()


def test_batch_telemetry_exposes_configured_top_query_limit_and_rapid_growth_count():
    engine, factory = _database()
    try:
        with factory() as session:
            batch = QueryGovernanceSelectionBatch(
                batch_id="batch-rapid-growth-telemetry",
                brand="atlas",
                data_date=date(2026, 9, 7),
                config_json=json.dumps({"extract_limit": 500}),
            )
            task = QueryGovernanceTask(
                batch_id=batch.batch_id,
                brand=batch.brand,
                data_date=batch.data_date,
                query_text="快速增长 Query",
                status="completed",
            )
            ignored_task = QueryGovernanceTask(
                batch_id=batch.batch_id,
                brand=batch.brand,
                data_date=batch.data_date,
                query_text="未选中 Query",
                status="completed",
            )
            session.add_all([batch, task, ignored_task])
            session.flush()
            session.add_all([
                QueryGovernanceCandidate(
                    selection_batch_id=batch.id,
                    batch_id=batch.batch_id,
                    brand=batch.brand,
                    data_date=batch.data_date,
                    query_id="rapid-growth",
                    query_key="快速增长 Query",
                    query_text="快速增长 Query",
                    matched_rules_json=json.dumps(["rapid_pv_growth", "high_pv"]),
                    metrics_json="{}",
                    source_type="dynamic",
                    selection_rank=1,
                    selected=True,
                ),
                QueryGovernanceCandidate(
                    selection_batch_id=batch.id,
                    batch_id=batch.batch_id,
                    brand=batch.brand,
                    data_date=batch.data_date,
                    query_id="ignored",
                    query_key="未选中 Query",
                    query_text="未选中 Query",
                    matched_rules_json=json.dumps(["rapid_pv_growth"]),
                    metrics_json="{}",
                    source_type="dynamic",
                    selection_rank=2,
                    selected=False,
                ),
            ])
            session.commit()

        telemetry = QueryGovernanceReadService(factory).brand_detail(
            "atlas", data_date=date(2026, 9, 7)
        )["batch"]["telemetry"]

        assert telemetry["topQueryLimit"] == 500
        assert telemetry["rapidPvGrowthQueryCount"] == 1
        assert telemetry["highPvQueryCount"] == 1
    finally:
        engine.dispose()


def test_reorder_display_uses_five_conclusion_templates(monkeypatch):
    monkeypatch.setattr(
        "src.query_governance.read_api.get_brand_business_config",
        lambda _brand: {"top_query_check_product_limit": 16},
    )
    service = QueryGovernanceReadService(lambda: None)
    cases = [
        (
            4, 0.04, 0.10, 0.01, 0.04, False,
            "当前召回商品数为 4 件，候选集过小，无需进行重排序。",
        ),
        (
            24, 0.04, 0.10, 0.02, 0.02, True,
            "当前 CTR 较近期下降 6 个百分点，已达到告警阈值，触发重排序。",
        ),
        (
            24, 0.10, 0.10, 0.01, 0.035, True,
            "当前 CVR 较近期下降 2.5 个百分点，已达到告警阈值，触发重排序。",
        ),
        (
            24, 0.04, 0.10, 0.01, 0.04, True,
            "当前 CTR、CVR 较近期分别下降 6、3 个百分点，均已达到告警阈值，触发重排序。",
        ),
        (
            24, 0.08, 0.10, 0.03, 0.04, False,
            "CTR、CVR 均在正常范围，无需进行重排序。",
        ),
    ]

    for recall_count, current_ctr, recent_ctr, current_cvr, recent_cvr, expected_requires, expected_conclusion in cases:
        result = service._reorder_display(
            SimpleNamespace(brand="atlas", s0_snapshot=json.dumps({"result_count": recall_count})),
            [],
            {"ctr": current_ctr, "avgCtr": recent_ctr, "cvr": current_cvr, "avgCvr": recent_cvr},
            {"alert_ctr_gap_pp": 5, "alert_cvr_gap_pp": 2},
        )
        assert result["requiresReorder"] is expected_requires
        expected_final = expected_conclusion
        if expected_requires:
            expected_final = f"{expected_conclusion}AI 重排序策略生成失败，重排序未执行。"
        assert result["conclusion"] == expected_final


def test_reorder_display_keeps_strategy_and_execution_statuses_independent(monkeypatch):
    monkeypatch.setattr(
        "src.query_governance.read_api.get_brand_business_config",
        lambda _brand: {"top_query_check_product_limit": 16},
    )
    service = QueryGovernanceReadService(lambda: None)
    task = SimpleNamespace(brand="atlas", s0_snapshot=json.dumps({"result_count": 24}))
    query = {"ctr": 0.04, "avgCtr": 0.10, "cvr": 0.02, "avgCvr": 0.02}
    config = {"alert_ctr_gap_pp": 5, "alert_cvr_gap_pp": 2}
    cases = [
        (
            {"nodeKey": "reorder", "status": "executed", "result": {"preference_description": "优先展示轻薄款"}},
            [SimpleNamespace(action_type="query_reorder", status="verified")],
            "success", "success", "AI 重排序策略已生成，重排序执行成功。",
        ),
        (
            {"nodeKey": "reorder", "status": "error", "result": {"preference_description": "优先展示轻薄款"}},
            [SimpleNamespace(action_type="query_reorder", status="verification_failed")],
            "success", "failed", "AI 重排序策略已生成，但重排序执行失败。",
        ),
        (
            {"nodeKey": "reorder", "status": "error", "result": {"preference_description": "优先展示轻薄款"}},
            [SimpleNamespace(action_type="query_reorder", status="execution_unknown")],
            "success", "inconclusive", "AI 重排序策略已生成，重排序结果待确认。",
        ),
        (
            {"nodeKey": "reorder", "status": "error", "result": {}},
            [],
            "failed", "not_run", "AI 重排序策略生成失败，重排序未执行。",
        ),
        (
            {"nodeKey": "reorder", "status": "error", "result": {}},
            [SimpleNamespace(action_type="query_reorder", status="error")],
            "failed", "failed", "AI 重排序策略生成失败，重排序执行失败。",
        ),
    ]

    for reorder_node, actions, expected_strategy, expected_execution, expected_suffix in cases:
        result = service._reorder_display(task, [reorder_node], query, config, actions)
        assert result["strategyStatus"] == expected_strategy
        assert result["executionStatus"] == expected_execution
        assert result["conclusion"].endswith(expected_suffix)

def test_query_detail_falls_back_to_successful_blacklist_fact_for_legacy_task():
    engine, session_factory = _database()
    try:
        task = QueryGovernanceTask(
            batch_id="batch-legacy", brand="atlas", data_date=date(2026, 9, 14),
            query_text="牛仔裤 男士", status="completed",
            s0_snapshot=json.dumps({"products": [{"product_id": "P1", "product_name": "基本款修身牛仔裤"}]}),
        )
        with session_factory() as session:
            session.add(task)
            session.flush()
            session.add(QueryGovernanceStepStatus(
                task_id=task.id, node_key="relevance", status="executed",
                result_json=json.dumps({"result": {"blacklist_candidates": [{"product_id": "P1", "product_name": "基本款修身牛仔裤", "reason": "不相关"}]}}),
            ))
            session.add(ActionIntent(
                action_type="product_blacklist", idempotency_key="legacy-blacklist-1",
                payload_hash="a" * 64, payload_json=json.dumps({"product_ids": ["P1"], "product_names": {"P1": "基本款修身牛仔裤"}}),
                operator="scheduler:query_governance", brand="atlas", release="test", permission="execute_governance_actions",
                query_text="牛仔裤 男士", target="atlas:牛仔裤 男士", status="success", version=0,
            ))
            session.commit()
            task_id = task.id

        detail = QueryGovernanceReadService(session_factory).query_detail(task_id)
        blacklist = next(plan for plan in detail["governancePlans"] if plan["type"] == "blacklist")
        assert blacklist["title"] == "错误商品"
        assert blacklist["products"][0]["status"] == "blacklisted"
        assert detail["actions"][0]["status"] == "verified"
    finally:
        engine.dispose()


def test_query_detail_keeps_historical_blacklist_fact_when_reorder_action_exists():
    engine, session_factory = _database()
    try:
        task = QueryGovernanceTask(
            batch_id="batch-reorder-and-blacklist",
            brand="atlas",
            data_date=date(2026, 9, 14),
            query_text="男士牛仔裤",
            status="completed",
            s0_snapshot=json.dumps(
                {"products": [{"product_id": "P1", "product_name": "错误召回商品"}]},
                ensure_ascii=False,
            ),
        )
        with session_factory() as session:
            session.add(task)
            session.flush()
            session.add_all(
                [
                    QueryGovernanceStepStatus(
                        task_id=task.id,
                        node_key="relevance",
                        status="executed",
                        result_json=json.dumps(
                            {
                                "result": {
                                    "blacklist_candidates": [
                                        {
                                            "product_id": "P1",
                                            "product_name": "错误召回商品",
                                            "reason": "与 Query 不相关",
                                        }
                                    ]
                                }
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    QueryGovernanceActionPlan(
                        task_id=task.id,
                        batch_id=task.batch_id,
                        brand=task.brand,
                        query_text=task.query_text,
                        node_key="reorder",
                        action_type="query_reorder",
                        idempotency_key="reorder-with-blacklist-fact",
                        status="verified",
                        executor_type="ai_auto",
                        request_json=json.dumps({"preference_description": "优先轻薄款"}, ensure_ascii=False),
                    ),
                    ActionIntent(
                        action_type="product_blacklist",
                        idempotency_key="historical-blacklist-alongside-reorder",
                        payload_hash="b" * 64,
                        payload_json=json.dumps(
                            {"product_ids": ["P1"], "product_names": {"P1": "错误召回商品"}},
                            ensure_ascii=False,
                        ),
                        operator="scheduler:query_governance",
                        brand="atlas",
                        release="test",
                        permission="execute_governance_actions",
                        query_text="男士牛仔裤",
                        target="atlas:男士牛仔裤",
                        status="success",
                        version=0,
                    ),
                ]
            )
            session.commit()
            task_id = task.id

        detail = QueryGovernanceReadService(session_factory).query_detail(task_id)

        assert [plan["type"] for plan in detail["governancePlans"]] == [
            "reorder",
            "blacklist",
        ]
        assert detail["governancePlans"][1]["title"] == "错误商品"
        assert detail["governancePlans"][1]["executionMode"] == "disabled"
        assert detail["governancePlans"][1]["products"][0]["status"] == "blacklisted"
    finally:
        engine.dispose()


def test_read_service_marks_batch_error_only_when_all_query_results_fail():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            run = QueryGovernanceWorkflowRun(
                batch_id="batch-all-error",
                status="error",
                started_at=datetime(2026, 9, 7, 0, 0),
                finished_at=datetime(2026, 9, 7, 0, 1),
            )
            task = QueryGovernanceTask(
                batch_id=run.batch_id,
                brand="atlas",
                data_date=date(2026, 9, 7),
                query_text="裙子",
                query_rank=1,
                status="error",
            )
            session.add_all([run, task])
            session.flush()
            session.add_all([
                QueryGovernanceStepStatus(
                    task_id=task.id,
                    node_key=node,
                    status="error",
                    error_code="NODE_FAILED",
                    error_message="failed",
                )
                for node in ("s0", "recall", "reorder", "relevance")
            ])
            session.commit()

        overview = QueryGovernanceReadService(session_factory).overview(["atlas"])
        assert overview["brands"][0]["status"] == "error"
    finally:
        engine.dispose()


def test_read_service_exposes_query_and_batch_telemetry_without_reinterpreting_legacy_duration():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            run = QueryGovernanceWorkflowRun(
                batch_id="batch-telemetry",
                status="completed",
                started_at=datetime(2026, 8, 29, 0, 0),
                finished_at=datetime(2026, 8, 29, 0, 1, 40),
            )
            task = QueryGovernanceTask(
                batch_id=run.batch_id,
                brand="atlas",
                data_date=date(2026, 8, 29),
                query_text="鞋",
                query_rank=1,
                status="completed",
                started_at=datetime(2026, 8, 29, 0, 0, 10),
                completed_at=datetime(2026, 8, 29, 0, 1, 0),
            )
            session.add_all([run, task])
            session.flush()
            session.add(
                QueryGovernanceStepStatus(
                    task_id=task.id,
                    node_key="s0",
                    status="executed",
                    duration_seconds=50,
                    result_json=json.dumps(
                        {
                            "result": {"result_count": 1},
                            "telemetry": {"executionSeconds": 1.25},
                        }
                    ),
                )
            )
            session.add(
                QueryGovernanceStepStatus(
                    task_id=task.id,
                    node_key="relevance",
                    status="executed",
                    duration_seconds=20,
                    result_json=json.dumps(
                        {
                            "result": {
                                "product_count": 2,
                                "rounds": [
                                    {"round": 1, "telemetry": {"roundSeconds": 3.5}},
                                    {"round": 2, "telemetry": {"roundSeconds": 1.5}},
                                ],
                            },
                            "telemetry": {
                                "executionSeconds": 5.0,
                                "modelSeconds": 4.0,
                            "modelCallCount": 2,
                            "retryCount": 1,
                            "truncationCount": 1,
                            "batchKey": "model:1",
                            },
                        }
                    ),
                )
            )
            session.add_all([
                QueryGovernanceAgentEvent(
                    task_id=task.id,
                    brand="atlas",
                    query_text="鞋",
                    agent_name="relevance",
                    node_key="relevance",
                    event_type="model_call",
                    token_usage=120,
                ),
                QueryGovernanceAgentEvent(
                    task_id=task.id,
                    brand="atlas",
                    query_text="鞋",
                    agent_name="relevance",
                    node_key="relevance",
                    event_type="model_call",
                    token_usage=80,
                ),
            ])
            session.commit()

        detail = QueryGovernanceReadService(session_factory).brand_detail("atlas")
        query = detail["queries"][0]
        assert query["durationSeconds"] == 70.0
        assert query["telemetry"] == {
            "endToEndSeconds": 50.0,
            "nodeWallClockSeconds": 70.0,
            "independentProcessingSeconds": 6.25,
            "stageDurations": {"s0": 1.25, "relevance": 5.0},
            "roundDurations": [3.5, 1.5],
            "tokenUsage": 0,
            "tokenUsageMissing": True,
            "relevanceTokenUsage": None,
            "relevanceTokenUsageMissing": True,
        }
        assert detail["batch"]["telemetry"]["queryEndToEnd"] == {
            "averageSeconds": 50.0,
            "p50Seconds": 50.0,
            "p95Seconds": 50.0,
        }
        assert detail["batch"]["telemetry"]["productCount"] == 2
        assert detail["batch"]["telemetry"]["roundCount"] == 2
        assert detail["batch"]["telemetry"]["modelCallCount"] == 1
        assert detail["batch"]["telemetry"]["tokenUsage"] == 200
        assert detail["batch"]["telemetry"]["retryCount"] == 1
        assert detail["batch"]["telemetry"]["truncationCount"] == 1
        assert detail["batch"]["telemetry"]["detectedProductCount"] == 0
        assert detail["batch"]["telemetry"]["irrelevantProductCount"] == 0
        assert detail["batch"]["telemetry"]["relevanceAccuracy"] is None
        assert detail["batch"]["telemetry"]["preferenceAdjustedQueryCount"] == 0
    finally:
        engine.dispose()


def test_batch_telemetry_does_not_attribute_shared_model_time_to_each_query():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            run = QueryGovernanceWorkflowRun(
                batch_id="batch-shared-model",
                status="completed",
                started_at=datetime(2026, 8, 29, 0, 0),
                finished_at=datetime(2026, 8, 29, 0, 0, 20),
            )
            session.add(run)
            session.flush()
            for index, query in enumerate(("q1", "q2"), start=1):
                task = QueryGovernanceTask(
                    batch_id=run.batch_id,
                    brand="atlas",
                    data_date=date(2026, 8, 29),
                    query_text=query,
                    query_rank=index,
                    status="completed",
                    started_at=datetime(2026, 8, 29, 0, 0, index),
                    completed_at=datetime(2026, 8, 29, 0, 0, index + 10),
                )
                session.add(task)
                session.flush()
                session.add(
                    QueryGovernanceStepStatus(
                        task_id=task.id,
                        node_key="relevance",
                        status="executed",
                        duration_seconds=10,
                        result_json=json.dumps(
                            {
                                "result": {"product_count": 1},
                                "telemetry": {
                                    "batchKey": "model:1",
                                    "batchQueryCount": 2,
                                    "batchModelSeconds": 5.0,
                                    "modelCallCount": 1,
                                    "truncationCount": 1,
                                    "truncationKeys": ["model:1", f"retry:{query}:1"],
                                    "independentExecutionSeconds": 1.0,
                                    "executionSeconds": 5.0,
                                },
                            }
                        ),
                    )
                )
            session.commit()

        telemetry = QueryGovernanceReadService(session_factory).brand_detail("atlas")["batch"]["telemetry"]
        assert telemetry["independentProcessing"] == {
            "averageSeconds": 1.0,
            "p50Seconds": 1.0,
            "p95Seconds": 1.0,
        }
        assert telemetry["modelSeconds"] == 5.0
        assert telemetry["modelCallCount"] == 1
        assert telemetry["truncationCount"] == 3
    finally:
        engine.dispose()

def test_brand_detail_filters_by_data_date_and_live_result_is_explicit():
    engine, session_factory = _database()
    try:
        task_id = _seed(session_factory)
        service = QueryGovernanceReadService(
            session_factory,
            live_search=lambda brand, query, limit: {
                "success": True,
                "total_count": 1,
                "result_complete": True,
                "products": [{"productId": "P9", "rank": 1}],
            },
        )

        brand = service.brand_detail("atlas", data_date=date(2026, 8, 28))
        assert brand["batch"]["batchId"] == "batch-atlas-20260829"
        assert brand["summary"]["queryCount"] == 1
        assert brand["queries"][0]["id"] == task_id
        assert brand["slots"] == {
            "kgAgent": {"available": True},
            "sharedFacts": {"available": True},
            "preference": {"available": True},
            "relevance": {"available": True},
            "inventory": {"available": True, "source": "independent_job"},
            "modelConfig": {"available": True},
            "abTest": {"available": True},
        }

        live = service.live_result(task_id, limit=16)
        assert live == {
            "taskId": task_id,
            "status": "executed",
            "resultCount": 1,
            "resultCountKnown": True,
            "products": [{"productId": "P9", "rank": 1}],
            "errorCode": None,
            "errorMessage": None,
        }
    finally:
        engine.dispose()


def test_read_service_prefers_formal_batch_over_retest_batch_for_same_date():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            for batch_id, count in (("query-governance:atlas:2026-09-06", 2), ("query-governance:atlas:2026-09-06:retest-a235", 1)):
                for rank in range(count):
                    session.add(QueryGovernanceTask(
                        batch_id=batch_id,
                        brand="atlas",
                        data_date=date(2026, 9, 6),
                        query_text=f"q-{batch_id}-{rank}",
                        query_rank=rank + 1,
                        status="completed",
                    ))
            session.commit()
        overview = QueryGovernanceReadService(session_factory).overview(["atlas"], data_date=date(2026, 9, 6))
        assert overview["brands"][0]["batchId"] == "query-governance:atlas:2026-09-06"
        assert overview["brands"][0]["queryCount"] == 2
    finally:
        engine.dispose()


def test_brand_detail_reports_query_duration_percentiles_and_reorder_telemetry():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            run = QueryGovernanceWorkflowRun(
                batch_id="batch-reorder-telemetry",
                status="completed",
                started_at=datetime(2026, 9, 6, 0, 0),
                finished_at=datetime(2026, 9, 6, 0, 1),
            )
            session.add(run)
            session.flush()
            tasks = []
            for rank, seconds in enumerate((10, 20), start=1):
                task = QueryGovernanceTask(
                    batch_id=run.batch_id,
                    brand="atlas",
                    data_date=date(2026, 9, 5),
                    query_text=f"q{rank}",
                    query_rank=rank,
                    status="completed",
                    started_at=datetime(2026, 9, 6, 0, 0),
                    completed_at=datetime(2026, 9, 6, 0, 0, seconds),
                )
                tasks.append(task)
            session.add_all(tasks)
            session.flush()
            for task in tasks:
                session.add(
                    QueryGovernanceStepStatus(
                        task_id=task.id,
                        node_key="reorder",
                        status="executed",
                        duration_seconds=task.query_rank,
                        result_json=json.dumps(
                            {
                                "result": {"query": task.query_text},
                                "telemetry": {
                                    "executionSeconds": float(task.query_rank),
                                    "maxConcurrency": 2,
                                },
                            }
                        ),
                    )
                )
            session.commit()

        detail = QueryGovernanceReadService(session_factory).brand_detail("atlas")
        assert detail["batch"]["telemetry"]["queryEndToEnd"] == {
            "averageSeconds": 15.0,
            "p50Seconds": 10.0,
            "p95Seconds": 20.0,
        }
        assert detail["batch"]["telemetry"]["maxConcurrency"] == 2
        assert detail["queries"][0]["telemetry"]["stageDurations"] == {"reorder": 1.0}
    finally:
        engine.dispose()


def test_batch_telemetry_reads_token_usage_from_nested_node_result_audit():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            run = QueryGovernanceWorkflowRun(
                batch_id="batch-token-audit",
                status="completed",
                started_at=datetime(2026, 9, 6, 0, 0),
                finished_at=datetime(2026, 9, 6, 0, 1),
            )
            session.add(run)
            session.flush()
            task = QueryGovernanceTask(
                batch_id=run.batch_id,
                brand="atlas",
                data_date=date(2026, 9, 6),
                query_text="q1",
                query_rank=1,
                status="completed",
            )
            session.add(task)
            session.flush()
            session.add(QueryGovernanceStepStatus(
                task_id=task.id,
                node_key="relevance",
                status="executed",
                result_json=json.dumps({
                    "result": {
                        "detected_product_ids": ["p1"],
                        "audit": {"tokenUsage": 321},
                    },
                    "telemetry": {},
                }),
            ))
            session.commit()

        detail = QueryGovernanceReadService(session_factory).brand_detail(
            "atlas", data_date=date(2026, 9, 6)
        )
        assert detail["batch"]["telemetry"]["tokenUsage"] == 321
    finally:
        engine.dispose()


def test_batch_telemetry_allocates_shared_relevance_time_and_exposes_query_token():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            run = QueryGovernanceWorkflowRun(
                batch_id="batch-shared-timing",
                status="completed",
                started_at=datetime(2026, 9, 6, 0, 0),
                finished_at=datetime(2026, 9, 6, 0, 0, 20),
            )
            session.add(run)
            session.flush()
            for index, query in enumerate(("q1", "q2"), start=1):
                task = QueryGovernanceTask(
                    batch_id=run.batch_id,
                    brand="atlas",
                    data_date=date(2026, 9, 6),
                    query_text=query,
                    query_rank=index,
                    status="completed",
                    started_at=datetime(2026, 9, 6, 0, 0, index),
                    completed_at=datetime(2026, 9, 6, 0, 0, index + 10),
                )
                session.add(task)
                session.flush()
                session.add(QueryGovernanceStepStatus(
                    task_id=task.id,
                    node_key="relevance",
                    status="executed",
                    duration_seconds=10,
                    result_json=json.dumps({
                        "result": {
                            "detected_product_ids": [f"p{index}"],
                            "audit": {"tokenUsage": 203},
                        },
                        "telemetry": {
                            "batchKey": "model:shared",
                            "batchQueryCount": 2,
                            "batchModelSeconds": 6.0,
                            "executionSeconds": 7.0,
                            "independentExecutionSeconds": 1.0,
                        },
                    }),
                ))
            session.commit()

        detail = QueryGovernanceReadService(session_factory).brand_detail(
            "atlas", data_date=date(2026, 9, 6)
        )
        assert detail["batch"]["telemetry"]["relevanceAverageSeconds"] == 3.5
        assert detail["batch"]["telemetry"]["tokenUsage"] == 203
        assert detail["queries"][0]["telemetry"]["tokenUsage"] == 102
    finally:
        engine.dispose()


def test_batch_telemetry_deduplicates_shared_relevance_tokens_and_reports_accuracy():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            run = QueryGovernanceWorkflowRun(
                batch_id="batch-shared-tokens",
                status="completed",
                started_at=datetime(2026, 9, 6, 0, 0),
                finished_at=datetime(2026, 9, 6, 0, 1),
            )
            session.add(run)
            session.flush()
            for index, query in enumerate(("q1", "q2"), start=1):
                task = QueryGovernanceTask(
                    batch_id=run.batch_id,
                    brand="atlas",
                    data_date=date(2026, 9, 6),
                    query_text=query,
                    query_rank=index,
                    status="completed",
                )
                session.add(task)
                session.flush()
                session.add(QueryGovernanceStepStatus(
                    task_id=task.id,
                    node_key="relevance",
                    status="executed",
                    result_json=json.dumps({
                        "result": {
                            "detected_product_ids": [f"p{index}"],
                            "relevant_product_ids": [f"p{index}"],
                            "blacklist_candidates": [],
                            "audit": {"tokenUsage": 200},
                        },
                        "telemetry": {
                            "batchKey": "model:shared",
                            "batchQueryCount": 2,
                            "executionSeconds": 4.0,
                        },
                    }),
                ))
            session.commit()

        detail = QueryGovernanceReadService(session_factory).brand_detail(
            "atlas", data_date=date(2026, 9, 6)
        )
        telemetry = detail["batch"]["telemetry"]
        assert telemetry["tokenUsage"] == 200
        assert telemetry["relevanceTokenUsage"] == 200
        assert telemetry["relevanceAccuracy"] == 1.0
        assert detail["queries"][0]["telemetry"]["tokenUsage"] == 100
    finally:
        engine.dispose()


def test_brand_detail_exposes_persisted_business_metrics_and_trend():
    engine, session_factory = _database()
    try:
        _seed(session_factory)
        with session_factory() as session:
            session.add_all([
                BrandDailyMetric(
                    brand="atlas",
                    data_date=date(2026, 8, 27),
                    brand_uv=900,
                    brand_pv=2500,
                    ctr=0.10,
                    cvr=0.02,
                    raw_payload=json.dumps({"click_uv": 90, "add_to_cart_uv": 30, "purchase_uv": 18, "atc": 0.033}),
                ),
                BrandDailyMetric(
                    brand="atlas",
                    data_date=date(2026, 8, 28),
                    brand_uv=1200,
                    brand_pv=3400,
                    ctr=0.12,
                    cvr=0.03,
                    raw_payload=json.dumps({"click_uv": 144, "add_to_cart_uv": 48, "purchase_uv": 36, "atc": 0.04}),
                ),
            ])
            session.commit()

        detail = QueryGovernanceReadService(session_factory).brand_detail(
            "atlas", data_date=date(2026, 8, 28)
        )

        assert detail["dataDate"] == "2026-08-28"
        assert detail["stats"] == {
            "topQueryCount": 1,
            "manualRequiredCount": 0,
            "completedCount": 1,
            "stepIssueCounts": {"recall": 0, "relevance": 0, "open_factor": 0, "inventory": 1},
            "noResultOrLowResultCount": 0,
            "resultDropCount": 0,
            "relevanceIssueCount": 0,
            "openFactorIssueCount": 0,
            "inventoryProductIssueCount": 1,
            "brandUv": 1200,
            "brandPv": 3400,
            "clickUv": 144,
            "addToCartUv": 48,
            "purchaseUv": 36,
            "ctr": 0.12,
            "atc": 0.04,
            "cvr": 0.03,
        }
        assert detail["trend"] == [
            {"date": "2026-08-27", "uv": 900, "pv": 2500, "clickUv": 90, "addToCartUv": 30, "purchaseUv": 18, "ctr": 0.1, "atc": 0.033, "cvr": 0.02},
            {"date": "2026-08-28", "uv": 1200, "pv": 3400, "clickUv": 144, "addToCartUv": 48, "purchaseUv": 36, "ctr": 0.12, "atc": 0.04, "cvr": 0.03},
        ]
    finally:
        engine.dispose()


def test_brand_detail_counts_queries_with_irrelevant_products_once_each():
    engine, session_factory = _database()
    try:
        batch_id = "query-governance:atlas:2026-09-13"
        with session_factory() as session:
            tasks = [
                QueryGovernanceTask(
                    batch_id=batch_id,
                    brand="atlas",
                    data_date=date(2026, 9, 13),
                    query_text=query_text,
                    query_rank=index,
                    status="completed",
                )
                for index, query_text in enumerate(("多个不相关商品", "全部相关", "检测失败"), start=1)
            ]
            session.add_all(tasks)
            session.flush()
            session.add_all([
                QueryGovernanceStepStatus(
                    task_id=tasks[0].id,
                    node_key="relevance",
                    status="executed",
                    result_json=json.dumps({
                        "result": {
                            "detected_product_ids": ["p1", "p2", "p3"],
                            "relevant_product_ids": ["p3"],
                            "blacklist_candidates": [
                                {"product_id": "p1", "is_relevant": False},
                                {"product_id": "p2", "is_relevant": False},
                            ],
                        },
                    }),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[1].id,
                    node_key="relevance",
                    status="executed",
                    result_json=json.dumps({
                        "result": {
                            "detected_product_ids": ["p4"],
                            "relevant_product_ids": ["p4"],
                            "blacklist_candidates": [],
                        },
                    }),
                ),
                QueryGovernanceStepStatus(
                    task_id=tasks[2].id,
                    node_key="relevance",
                    status="error",
                    error_code="MODEL_TIMEOUT",
                ),
            ])
            session.commit()

        detail = QueryGovernanceReadService(session_factory).brand_detail(
            "atlas", data_date=date(2026, 9, 13)
        )

        assert detail["batch"]["telemetry"]["irrelevantQueryCount"] == 1
        assert detail["batch"]["telemetry"]["irrelevantProductCount"] == 2
        overview = QueryGovernanceReadService(session_factory).overview(
            ["atlas"], data_date=date(2026, 9, 13)
        )
        assert overview["brands"][0]["telemetry"]["irrelevantQueryCount"] == 1
        assert detail["stats"]["stepIssueCounts"]["relevance"] == 1
        assert detail["stats"]["relevanceIssueCount"] == 1
        relevance_steps = {
            query["queryText"]: next(
                step for step in query["stepStatuses"] if step["checkKey"] == "relevance"
            )
            for query in detail["queries"]
        }
        assert relevance_steps["多个不相关商品"]["evidence"]["irrelevantProductCount"] == 2
        assert relevance_steps["多个不相关商品"]["evidence"]["blacklistCandidates"] == [
            {"product_id": "p1", "is_relevant": False},
            {"product_id": "p2", "is_relevant": False},
        ]
        assert relevance_steps["全部相关"]["evidence"]["irrelevantProductCount"] == 0
        assert relevance_steps["检测失败"]["evidence"]["irrelevantProductCount"] == 0
    finally:
        engine.dispose()


def test_query_detail_exposes_historical_query_metric_trend_from_candidates():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            batch = QueryGovernanceSelectionBatch(
                batch_id="batch-atlas-20260905",
                brand="atlas",
                data_date=date(2026, 9, 5),
                config_json="{}",
            )
            previous_batch = QueryGovernanceSelectionBatch(
                batch_id="batch-atlas-20260904",
                brand="atlas",
                data_date=date(2026, 9, 4),
                config_json="{}",
            )
            task = QueryGovernanceTask(
                batch_id=batch.batch_id,
                brand="atlas",
                data_date=batch.data_date,
                query_text="帽子 女士",
                status="completed",
            )
            session.add_all([batch, previous_batch, task])
            session.flush()
            session.add_all([
                QueryGovernanceCandidate(
                    selection_batch_id=previous_batch.id,
                    batch_id=previous_batch.batch_id,
                    brand="atlas",
                    data_date=date(2026, 9, 4),
                    query_id="q-0904",
                    query_key="帽子 女士",
                    query_text="帽子 女士",
                    matched_rules_json="[]",
                    metrics_json=json.dumps({
                        "search_pv": 90, "search_uv": 60, "click_uv": 30,
                        "purchase_uv": 3, "ctr": 0.5, "cvr": 0.05,
                    }),
                    source_type="top_query",
                    selection_rank=1,
                    selected=True,
                ),
                QueryGovernanceCandidate(
                    selection_batch_id=batch.id,
                    batch_id=batch.batch_id,
                    brand="atlas",
                    data_date=date(2026, 9, 5),
                    query_id="q-0905",
                    query_key="帽子 女士",
                    query_text="帽子 女士",
                    matched_rules_json="[]",
                    metrics_json=json.dumps({
                        "search_pv": 114, "search_uv": 81, "click_uv": 34,
                        "purchase_uv": 0, "ctr": 34 / 81, "cvr": 0,
                    }),
                    source_type="top_query",
                    selection_rank=1,
                    selected=True,
                ),
            ])
            session.commit()
            task_id = task.id

        detail = QueryGovernanceReadService(session_factory).query_detail(task_id)
        assert detail["trend"] == [
            {"date": "2026-09-04", "uv": 60, "pv": 90, "clickUv": 30, "addToCartUv": 0, "purchaseUv": 3, "ctr": 0.5, "atc": 0, "cvr": 0.05},
            {"date": "2026-09-05", "uv": 81, "pv": 114, "clickUv": 34, "addToCartUv": 0, "purchaseUv": 0, "ctr": 34 / 81, "atc": 0, "cvr": 0},
        ]
    finally:
        engine.dispose()


def test_query_workspace_skips_metric_trend_when_disabled(monkeypatch):
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            task = QueryGovernanceTask(
                batch_id="batch-atlas-no-trend",
                brand="atlas",
                data_date=date(2026, 9, 5),
                query_text="帽子 女士",
                status="completed",
            )
            session.add(task)
            session.commit()
            task_id = task.id

        def unexpected_trend_read(session, task):
            raise AssertionError("includeTrend=false must skip trend database read")

        monkeypatch.setattr(QueryGovernanceReadService, "_query_metric_trend", unexpected_trend_read)
        detail = QueryGovernanceReadService(session_factory).query_workspace(task_id, include_trend=False)
        assert detail["query"]["id"] == task_id
        assert detail["trend"] == []
    finally:
        engine.dispose()


def test_query_detail_exposes_all_persisted_metric_dates_including_follow_up_days():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            batch = QueryGovernanceSelectionBatch(
                batch_id="batch-atlas-20260905",
                brand="atlas",
                data_date=date(2026, 9, 5),
                config_json=json.dumps({"history_days": 3}),
            )
            session.add(batch)
            task = QueryGovernanceTask(
                batch_id="batch-atlas-20260905",
                brand="atlas",
                data_date=date(2026, 9, 5),
                query_text="帽子 女士",
                status="completed",
            )
            session.add(task)
            session.flush()
            session.add_all([
                QueryMetricSnapshot(
                    brand="atlas", query_text="帽子 女士", data_date=date(2026, 9, 4),
                    pv=90, uv=60, click_uv=30, add_to_cart_uv=12, purchase_uv=3,
                    ctr=0.5, cvr=0.05,
                ),
                QueryMetricSnapshot(
                    brand="atlas", query_text="帽子 女士", data_date=date(2026, 9, 7),
                    pv=120, uv=80, click_uv=32, add_to_cart_uv=8, purchase_uv=4,
                    ctr=0.4, cvr=0.05,
                ),
            ])
            session.commit()
            task_id = task.id

        detail = QueryGovernanceReadService(session_factory).query_detail(task_id)
        assert [point["date"] for point in detail["trend"]] == ["2026-09-04", "2026-09-07"]
        assert detail["trend"][1]["addToCartUv"] == 8
        assert detail["trend"][1]["atc"] == 0.1
        assert detail["historyDays"] == 3
    finally:
        engine.dispose()


def test_query_detail_merges_stale_snapshots_with_fresh_candidates():
    engine, session_factory = _database()
    try:
        with session_factory() as session:
            batch_sep = QueryGovernanceSelectionBatch(
                batch_id="batch-atlas-20260918",
                brand="atlas",
                data_date=date(2026, 9, 18),
                config_json="{}",
            )
            batch_oct14 = QueryGovernanceSelectionBatch(
                batch_id="batch-atlas-20261014",
                brand="atlas",
                data_date=date(2026, 10, 14),
                config_json="{}",
            )
            batch_oct15 = QueryGovernanceSelectionBatch(
                batch_id="batch-atlas-20261015",
                brand="atlas",
                data_date=date(2026, 10, 15),
                config_json="{}",
            )
            task = QueryGovernanceTask(
                batch_id=batch_oct15.batch_id,
                brand="atlas",
                data_date=batch_oct15.data_date,
                query_text="豹纹 女士",
                status="completed",
            )
            session.add_all([batch_sep, batch_oct14, batch_oct15, task])
            session.flush()
            session.add_all([
                QueryMetricSnapshot(
                    brand="atlas", query_text="豹纹 女士", data_date=date(2026, 9, 16),
                    pv=80, uv=50, click_uv=20, add_to_cart_uv=5, purchase_uv=2,
                    ctr=0.4, cvr=0.04,
                ),
                QueryMetricSnapshot(
                    brand="atlas", query_text="豹纹 女士", data_date=date(2026, 9, 18),
                    pv=145, uv=98, click_uv=43, add_to_cart_uv=10, purchase_uv=1,
                    ctr=0.438776, cvr=0.010204,
                ),
                QueryGovernanceCandidate(
                    selection_batch_id=batch_sep.id,
                    batch_id=batch_sep.batch_id,
                    brand="atlas",
                    data_date=date(2026, 9, 18),
                    query_id="q-0918",
                    query_key="豹纹 女士",
                    query_text="豹纹 女士",
                    matched_rules_json="[]",
                    metrics_json=json.dumps({
                        "search_pv": 145, "search_uv": 98, "click_uv": 43,
                        "purchase_uv": 1, "ctr": 0.438776, "cvr": 0.010204,
                    }),
                    source_type="top_query",
                    selection_rank=1,
                    selected=True,
                ),
                QueryGovernanceCandidate(
                    selection_batch_id=batch_oct14.id,
                    batch_id=batch_oct14.batch_id,
                    brand="atlas",
                    data_date=date(2026, 10, 14),
                    query_id="q-1014",
                    query_key="豹纹 女士",
                    query_text="豹纹 女士",
                    matched_rules_json="[]",
                    metrics_json=json.dumps({
                        "search_pv": 105, "search_uv": 74, "click_uv": 40,
                        "purchase_uv": 0, "ctr": 40 / 74, "cvr": 0,
                    }),
                    source_type="top_query",
                    selection_rank=1,
                    selected=True,
                ),
                QueryGovernanceCandidate(
                    selection_batch_id=batch_oct15.id,
                    batch_id=batch_oct15.batch_id,
                    brand="atlas",
                    data_date=date(2026, 10, 15),
                    query_id="q-1015",
                    query_key="豹纹 女士",
                    query_text="豹纹 女士",
                    matched_rules_json="[]",
                    metrics_json=json.dumps({
                        "search_pv": 208, "search_uv": 146, "click_uv": 68,
                        "purchase_uv": 0, "ctr": 68 / 146, "cvr": 0,
                    }),
                    source_type="top_query",
                    selection_rank=1,
                    selected=True,
                ),
            ])
            session.commit()
            task_id = task.id

        detail = QueryGovernanceReadService(session_factory).query_detail(task_id)
        trend_dates = [point["date"] for point in detail["trend"]]
        assert trend_dates == ["2026-09-16", "2026-09-18", "2026-10-14", "2026-10-15"]
        assert detail["trend"][-1]["date"] == "2026-10-15"
        assert detail["trend"][-1]["uv"] == 146
        assert detail["trend"][-1]["clickUv"] == 68
        assert detail["trend"][1]["addToCartUv"] == 10
    finally:
        engine.dispose()


def test_relevance_remediation_projects_brand_day_retag_verification():
    task = SimpleNamespace(brand="atlas", data_date=date(2026, 9, 14))
    actions = [
        SimpleNamespace(
            action_type="product_blacklist",
            status="verified",
            executor_type="ai_auto",
            response_json="{}",
        ),
        SimpleNamespace(
            action_type="kg_plan",
            status="verified",
            executor_type="human",
            response_json="{}",
        ),
        SimpleNamespace(
            action_type="product_retag",
            idempotency_key="query-governance:retag:atlas:2026-09-14",
            status="verified",
            executor_type="ai_auto",
            response_json=json.dumps(
                {
                    "retag": {"status": "verified"},
                    "remediationSummary": {
                        "status": "verified",
                        "conclusion": "passed",
                        "verificationTime": "2026-09-15T01:00:00",
                        "beforeRecallCount": 16,
                        "afterRecallCount": 13,
                        "errorProductsDisappeared": True,
                        "correctProductsHarmed": False,
                    },
                }
            ),
        ),
    ]

    payload = QueryGovernanceReadService._relevance_remediation_payload(
        task=task,
        relevance={"causeGroups": [{"groupId": "g-1"}, {"groupId": "g-2"}]},
        actions=actions,
    )

    assert payload == {
        "blacklistStatus": "AI 已执行",
        "rootCauseGroupCount": 2,
        "kgStrategyStatus": "手动已执行",
        "retagStatus": "AI 已执行",
        "verifiedAt": "2026-09-15T01:00:00",
        "incorrectProductsDisappeared": True,
        "correctProductsAffected": False,
        "beforeRecallCount": 16,
        "afterRecallCount": 13,
        "finalConclusion": "passed",
    }


def test_query_quality_metrics_averages_post_recall_rate_over_reported_queries():
    from src.query_governance.read_api import _query_quality_metrics

    tasks = [
        SimpleNamespace(id=1, status="completed", query_text="a"),
        SimpleNamespace(id=2, status="completed", query_text="b"),
        SimpleNamespace(id=3, status="completed", query_text="c"),
    ]

    def recall_step(task_id, result):
        return SimpleNamespace(
            task_id=task_id,
            node_key="recall",
            status="executed",
            result_json=json.dumps({"result": result}),
        )

    steps = [
        recall_step(1, {"result_count": 5, "evidence": {"recall_rate": {"post_search_recall_rate": 1.0}}}),
        recall_step(2, {"result_count": 5, "evidence": {"recall_rate": {"post_search_recall_rate": 0.5}}}),
        # Query 3 跑了数量检查但没有召回率报告（未启用/未跑检测），不进分母。
        recall_step(3, {"result_count": 5}),
    ]
    metrics = _query_quality_metrics(tasks, steps, visible_checks={"recall"})
    assert metrics["avgSearchRecallRate"] == 0.75
    assert metrics["recallRateQueryCount"] == 2


def test_overview_telemetry_contains_query_quality_and_processing_metrics():
    from src.query_governance.read_api import _query_quality_metrics


    class Task:
        def __init__(self, task_id, status):
            self.id = task_id
            self.status = status

    tasks = [Task(1, "completed"), Task(2, "manual_required"), Task(3, "completed")]
    steps = [
        type("Step", (), {"task_id": 1, "node_key": "recall", "result_json": json.dumps({"result": {"result_count": 0}})})(),
        type("Step", (), {"task_id": 2, "node_key": "recall", "result_json": json.dumps({"result": {"result_count": 2}})})(),
        type("Step", (), {"task_id": 3, "node_key": "recall", "result_json": json.dumps({"result": {"result_count": 1}})})(),
    ]

    metrics = _query_quality_metrics(tasks, steps, visible_checks={"recall"})
    assert metrics == {
        "noRecallCount": 1,
        "noRecallRate": 1 / 3,
        "passCount": None,
        "passRate": None,
        "aiProcessedCount": 2,
        "pendingCount": 1,
        "avgSearchRecallRate": None,
        "recallRateQueryCount": 0,
        "searchRecallRateSum": None,
    }


def test_overview_pass_rate_only_counts_relevance_issues():
    from src.query_governance.read_api import _query_quality_metrics

    tasks = [
        SimpleNamespace(id=1, status="completed", query_text="正常"),
        SimpleNamespace(id=2, status="completed", query_text="召回异常"),
        SimpleNamespace(id=3, status="completed", query_text="相关性异常"),
        SimpleNamespace(id=4, status="manual_required", query_text="待处理"),
    ]

    def step(task_id, node_key, result, status="executed"):
        return SimpleNamespace(
            task_id=task_id,
            node_key=node_key,
            status=status,
            result_json=json.dumps({"result": result}),
        )

    steps = []
    for task_id in range(1, 5):
        steps.extend([
            step(task_id, "recall", {"reasons": []}),
            step(task_id, "reorder", {"should_reorder": False}),
            step(task_id, "relevance", {"blacklist_candidates": []}),
            step(task_id, "inventory", {}),
        ])
    steps[1] = step(1, "reorder", {"reason": "CTR/CVR 正常"}, status="skipped")
    steps[5] = step(2, "recall", {"reasons": ["low_result"]})
    steps[10] = step(3, "relevance", {"blacklist_candidates": [{"product_id": "p-1"}]})

    metrics = _query_quality_metrics(
        tasks,
        steps,
        visible_checks={"recall", "open_factor", "relevance", "inventory"},
    )

    assert metrics["passCount"] == 3
    assert metrics["passRate"] == 3 / 4
    assert metrics["aiProcessedCount"] == 3
    assert metrics["pendingCount"] == 1


def test_overview_pass_rate_ignores_inventory_and_reorder_issues():
    from src.query_governance.read_api import _query_quality_metrics

    tasks = [
        SimpleNamespace(id=1, status="completed", query_text="断码异常"),
        SimpleNamespace(id=2, status="completed", query_text="正常"),
    ]
    steps = [
        SimpleNamespace(task_id=task.id, node_key=node_key, status="executed", result_json=json.dumps({"result": result}))
        for task in tasks
        for node_key, result in (
            ("recall", {"reasons": ["low_result"]}),
            ("reorder", {"should_reorder": True}),
            ("relevance", {"blacklist_candidates": []}),
            ("inventory", {}),
        )
    ]

    metrics = _query_quality_metrics(
        tasks,
        steps,
        inventory_records=[SimpleNamespace(original_query="断码异常", status="monitoring")],
        visible_checks={"recall", "open_factor", "relevance", "inventory"},
    )

    assert metrics["passCount"] == 2
    assert metrics["passRate"] == 1.0


def test_overview_pass_rate_requires_a_successful_relevance_step():
    from src.query_governance.read_api import _query_quality_metrics

    def step(task_id, node_key, result, status="executed"):
        return SimpleNamespace(
            task_id=task_id,
            node_key=node_key,
            status=status,
            result_json=json.dumps({"result": result}),
        )

    tasks = [
        SimpleNamespace(id=1, status="completed", query_text="缺少相关性记录"),
        SimpleNamespace(id=2, status="completed", query_text="相关性执行失败"),
        SimpleNamespace(id=3, status="completed", query_text="正常"),
    ]
    steps = [
        step(1, "recall", {"reasons": []}),
        step(2, "relevance", {"blacklist_candidates": []}, status="error"),
        step(3, "relevance", {"blacklist_candidates": []}),
    ]

    metrics = _query_quality_metrics(tasks, steps, visible_checks={"recall", "relevance"})

    assert metrics["passCount"] == 1
    assert metrics["passRate"] == 1 / 3


def test_overview_pass_rate_is_null_when_relevance_check_is_disabled():
    from src.query_governance.read_api import _query_quality_metrics

    tasks = [SimpleNamespace(id=1, status="completed", query_text="正常")]
    steps = [
        SimpleNamespace(
            task_id=1,
            node_key="recall",
            status="executed",
            result_json=json.dumps({"result": {"reasons": []}}),
        ),
    ]

    metrics = _query_quality_metrics(tasks, steps, visible_checks={"recall", "open_factor"})

    assert metrics["passCount"] is None
    assert metrics["passRate"] is None


def test_batch_payload_relevance_accuracy_includes_reused_products():
    from src.query_governance.read_api import QueryGovernanceReadService

    engine, session_factory = _database()
    try:
        with session_factory() as session:
            task = QueryGovernanceTask(
                id=101,
                batch_id="batch-reused-1",
                brand="atlas",
                query_text="连衣裙",
                data_date=date(2026, 9, 7),
                status="completed",
            )
            run = QueryGovernanceWorkflowRun(
                batch_id="batch-reused-1",
                status="completed",
            )
            step = QueryGovernanceStepStatus(
                task_id=101,
                node_key="relevance",
                status="executed",
                result_json=json.dumps({
                    "result": {
                        "detected_product_ids": ["p1", "p2"],
                        "reused_product_ids": ["p3", "p4", "p5"],
                        "blacklist_candidates": [{"productId": "p1"}],
                    }
                }),
            )
            session.add_all([task, run, step])
            session.commit()

        detail = QueryGovernanceReadService(session_factory).brand_detail("atlas", data_date=date(2026, 9, 7))
        telem = detail["batch"]["telemetry"]
        # 2 detected + 3 reused = 5 inspected, 1 irrelevant
        assert telem["detectedProductCount"] == 2
        assert telem["reusedProductCount"] == 3
        assert telem["inspectedProductCount"] == 5
        assert telem["irrelevantProductCount"] == 1
        # Accuracy = (5 - 1) / 5 = 0.8
        assert telem["relevanceAccuracy"] == 0.8
        assert telem["irrelevantRate"] == 0.2
    finally:
        engine.dispose()


def test_metric_trend_metrics_includes_inspected_products():
    from src.query_governance.read_api import _metric_trend_metrics

    payload = {
        "telemetry": {
            "queryCount": 10,
            "passCount": 9,
            "detectedProductCount": 100,
            "reusedProductCount": 100,
            "inspectedProductCount": 200,
            "irrelevantProductCount": 20,
        }
    }
    metrics = _metric_trend_metrics(payload)
    assert metrics["detected_product_count"] == 100
    assert metrics["reused_product_count"] == 100
    assert metrics["inspected_product_count"] == 200
    assert metrics["irrelevant_product_count"] == 20
    # (200 - 20) / 200 = 0.9
    assert metrics["relevance_accuracy"] == 0.9


def test_overview_no_recall_uses_l0_to_l2_buckets_in_s0_evidence():
    from src.query_governance.read_api import _query_quality_metrics

    tasks = [
        SimpleNamespace(id=1, status="completed", query_text="裙子"),
        SimpleNamespace(id=2, status="completed", query_text="外套"),
    ]
    steps = [
        SimpleNamespace(
            task_id=task.id,
            node_key="recall",
            status="executed",
            result_json=json.dumps({"result": {"result_count": 2, "reasons": []}}),
        )
        for task in tasks
    ]

    metrics = _query_quality_metrics(
        tasks,
        steps,
        visible_checks={"recall"},
        s0_snapshots={
            1: {"products": [{"product_id": "p1", "bucket": "productIdsByFuzzy"}]},
            2: {"products": [{"product_id": "p2", "bucket": "productIdsByCode"}]},
        },
    )

    assert metrics["noRecallCount"] == 1
    assert metrics["noRecallRate"] == 1 / 2


def test_overview_pending_counts_governance_manual_review_and_excludes_it_from_ai_processed():
    from src.query_governance.read_api import _query_quality_metrics

    tasks = [
        SimpleNamespace(id=1, status="completed", query_text="裙子"),
        SimpleNamespace(id=2, status="completed", query_text="外套"),
    ]
    steps = [
        SimpleNamespace(
            task_id=task.id,
            node_key="recall",
            status="executed",
            result_json=json.dumps({"result": {"result_count": 2, "reasons": []}}),
        )
        for task in tasks
    ]
    governance_states = {
        1: {"recall": {"status": "manual_review"}},
        2: {"recall": {"status": "verified"}},
    }

    metrics = _query_quality_metrics(
        tasks,
        steps,
        visible_checks={"recall"},
        governance_states_by_task=governance_states,
    )

    assert metrics["pendingCount"] == 1
    assert metrics["aiProcessedCount"] == 1


def test_overview_ai_processed_excludes_human_verified_governance():
    from src.query_governance.read_api import _query_quality_metrics

    task = SimpleNamespace(id=1, status="completed", query_text="人工已执行")
    metrics = _query_quality_metrics(
        [task],
        [],
        visible_checks=set(),
        governance_states_by_task={
            1: {"recall": {"status": "verified", "executionMode": "human_approval", "executorType": "human"}}
        },
    )

    assert metrics["aiProcessedCount"] == 0
    assert metrics["pendingCount"] == 0


def test_overview_pending_ignores_generation_and_auto_actions():
    from src.query_governance.read_api import _query_quality_metrics

    tasks = [
        SimpleNamespace(id=1, status="completed", query_text="生成中"),
        SimpleNamespace(id=2, status="completed", query_text="自动执行中"),
        SimpleNamespace(id=3, status="completed", query_text="人工确认"),
        SimpleNamespace(id=4, status="completed", query_text="已完成"),
    ]
    states = {
        1: {"recall": {"status": "pending", "label": "生成中", "executionMode": "human_approval"}},
        2: {"open_factor": {"status": "pending", "label": "待执行", "executionMode": "auto"}},
        3: {"recall": {"status": "pending", "label": "待执行", "executionMode": "human_approval"}},
    }

    metrics = _query_quality_metrics(
        tasks,
        [],
        visible_checks=set(),
        governance_states_by_task=states,
    )

    assert metrics["pendingCount"] == 1
    assert metrics["aiProcessedCount"] == 1


def test_overview_pending_does_not_lose_human_action_behind_same_stage_auto_action():
    from src.query_governance.read_api import _governance_states, _query_quality_metrics

    governance_states = _governance_states(
        [
            SimpleNamespace(
                action_type="product_blacklist", status="pending", failure_stage=None,
                executor_type=None, error_code=None, error_message=None,
                request_json=json.dumps({"execution_mode": "auto"}),
            ),
            SimpleNamespace(
                action_type="kg_plan", status="pending", failure_stage=None,
                executor_type=None, error_code=None, error_message=None,
                request_json=json.dumps({"execution_mode": "human_approval"}),
            ),
        ],
        [],
        include_execution_mode=True,
    )
    metrics = _query_quality_metrics(
        [SimpleNamespace(id=1, status="completed", query_text="同一阶段多动作")],
        [],
        visible_checks=set(),
        governance_states_by_task={1: governance_states},
    )

    assert metrics["pendingCount"] == 1
    assert metrics["aiProcessedCount"] == 0


def test_materialized_overview_uses_s0_buckets_and_governance_projection():
    engine, factory = _database()
    try:
        _seed(factory)
        with factory() as session:
            task = session.execute(select(QueryGovernanceTask)).scalar_one()
            task.s0_snapshot = json.dumps({
                "result_count": 2,
                "result_complete": True,
                "products": [
                    {"product_id": "P1", "bucket": "productIdsByFuzzy"},
                    {"product_id": "P2", "bucket": "productIdsByNoStock"},
                ],
            })
            session.add(QueryGovernanceActionPlan(
                task_id=task.id,
                batch_id=task.batch_id,
                brand=task.brand,
                query_text=task.query_text,
                node_key="recall",
                action_type="kg_plan",
                idempotency_key="overview-pending-kg",
                status="pending",
                request_json=json.dumps({
                    "execution_mode": "human_approval",
                    "trigger_source": "recall_anomaly",
                }),
            ))
            session.commit()

        overview = QueryGovernanceReadService(factory).materialize_overview_summary(
            "atlas", data_date=date(2026, 8, 28)
        )
        telemetry = overview["telemetry"]
        assert telemetry["noRecallCount"] == 1
        assert telemetry["pendingCount"] == 1
        assert telemetry["aiProcessedCount"] == 0
    finally:
        engine.dispose()


def test_governance_candidate_groups_follow_final_relevance_strategy():
    task = SimpleNamespace(id=43, s0_snapshot='{"products": []}')
    products = [{"product_id": f"P{i}", "reason": "非羊绒"} for i in range(50)]
    nodes = [{"nodeKey": "relevance", "result": {"blacklist_candidates": products, "cause_groups": [{"root_cause": "uncertain", "products": products}]}}]
    action = SimpleNamespace(
        id=60, task_id=43, action_type="relevance_plan", status="pending", executor_type=None,
        request_json=json.dumps({"trigger_source": "relevance_root_cause", "evidence": {"products": products}}),
        response_json=json.dumps({"causeGroups": [{"groupId": "kg", "rootCause": "kg_issue", "productIds": [p["product_id"] for p in products]}]}),
    )
    plans = _governance_plans(task, nodes, [action], [])
    assert plans[0]["causeGroups"][0]["rootCause"] == "kg_issue"
    assert not any(plan.get("causeGroups") for plan in plans if plan["type"] == "blacklist")
    assert {p["productId"] for plan in plans for p in plan["products"]} == {f"P{i}" for i in range(50)}


def test_relevance_groups_share_latest_task_source_and_persisted_action_identity():
    task = SimpleNamespace(id=43, s0_snapshot='{"products": []}')
    def analysis(analysis_id, root, key):
        return SimpleNamespace(id=analysis_id, task_id=43, idempotency_key=key, trigger_source="relevance_root_cause", status="completed", evidence_json='{}', plan_json=json.dumps({"causeGroups": [{"groupId": key, "rootCause": root, "productIds": ["P1"]}]}))
    old = analysis(1, "uncertain", "old")
    new = analysis(2, "kg_issue", "new")
    unrelated = analysis(99, "uncertain", "foreign")
    unrelated.task_id = 44
    action = SimpleNamespace(id=60, task_id=43, idempotency_key="new:plan", action_type="relevance_plan", status="pending", executor_type=None, request_json=json.dumps({"trigger_source": "relevance_root_cause"}), response_json=new.plan_json)
    plans = _governance_plans(task, [], [action], [old, new, unrelated])
    payload = QueryGovernanceReadService._relevance_payload([], "羊绒", async_tasks=[old, new])
    assert payload["causeGroups"] == [{"groupId": "new", "rootCause": "kg_issue", "productIds": ["P1"]}]
    assert [plan["actionId"] for plan in plans] == [60]


def test_relevance_new_async_result_replaces_older_action_without_evidence_groups():
    task = SimpleNamespace(id=43, s0_snapshot='{"products": []}')
    old = SimpleNamespace(id=1, task_id=43, idempotency_key="old", trigger_source="relevance_root_cause", status="completed", evidence_json='{}', plan_json='{"causeGroups": [{"rootCause": "individual", "productIds": ["P1"]}]}')
    new = SimpleNamespace(id=2, task_id=43, idempotency_key="new", trigger_source="relevance_root_cause", status="completed", evidence_json='{"causeGroups": [{"rootCause": "uncertain"}]}', plan_json='{"causeGroups": [{"rootCause": "kg_issue", "productIds": ["P1"]}]}')
    action = SimpleNamespace(id=60, task_id=43, idempotency_key="old:plan", action_type="relevance_plan", status="pending", executor_type=None, request_json='{"trigger_source": "relevance_root_cause"}', response_json=old.plan_json)
    plans = _governance_plans(task, [], [action], [old, new])
    assert len(plans) == 1
    assert plans[0]["id"] == "kg-async-2"
    assert plans[0]["causeGroups"][0]["rootCause"] == "kg_issue"
    new.plan_json = '{}'
    assert QueryGovernanceReadService._relevance_payload([], "羊绒", async_tasks=[new])["causeGroups"] == []


def test_metric_trend_returns_daily_snapshots_and_keeps_missing_days_as_gaps():
    engine, factory = _database()
    _seed(factory)
    service = QueryGovernanceReadService(factory)
    try:
        service.materialize_overview_summary("atlas", data_date=date(2026, 8, 28))

        payload = service.metric_trend(
            "atlas",
            start_date=date(2026, 8, 27),
            end_date=date(2026, 8, 29),
        )

        assert payload["brand"] == "atlas"
        assert payload["startDate"] == "2026-08-27"
        assert payload["endDate"] == "2026-08-29"
        assert [day["date"] for day in payload["days"]] == ["2026-08-27", "2026-08-28", "2026-08-29"]
        assert payload["days"][0] == {"date": "2026-08-27", "batchId": None, "metrics": None}
        assert payload["days"][2] == {"date": "2026-08-29", "batchId": None, "metrics": None}

        summary_day = payload["days"][1]
        assert summary_day["batchId"] == "batch-atlas-20260829"
        metrics = summary_day["metrics"]
        assert metrics["query_count"] == 1
        assert metrics["pass_count"] == 0
        assert metrics["pass_rate"] == 0.0
        assert metrics["no_recall_count"] == 0
        assert metrics["no_recall_rate"] == 0.0
        assert metrics["irrelevant_product_count"] == 0
        assert metrics["relevance_accuracy"] is None
        assert metrics["token_usage"] is not None
    finally:
        engine.dispose()


def _seed_relevance_flagged_batch(session_factory, *, batch_id="batch-atlas-20260828", data_date=date(2026, 8, 28)):
    """Seed one batch whose relevance steps flagged (query, product) pairs.

    The same product under different queries counts as separate pairs, while
    the repeated P1 under 连衣裙 emulates a duplicate round entry and must
    collapse into one pair.
    """
    flagged = [("连衣裙", ["P1", "P2", "P1"]), ("衬衫", ["P1"])]
    with session_factory() as session:
        session.add(QueryGovernanceWorkflowRun(
            batch_id=batch_id, status="completed",
            started_at=datetime(2026, 8, 29, 0, 30), finished_at=datetime(2026, 8, 29, 0, 42),
        ))
        for rank, (query_text, product_ids) in enumerate(flagged, start=1):
            task = QueryGovernanceTask(
                batch_id=batch_id, brand="atlas", data_date=data_date,
                query_text=query_text, query_rank=rank, status="completed",
                started_at=datetime(2026, 8, 29, 0, 31), completed_at=datetime(2026, 8, 29, 0, 40),
            )
            session.add(task)
            session.flush()
            session.add(QueryGovernanceStepStatus(
                task_id=task.id, node_key="relevance", status="executed",
                result_json=json.dumps({
                    "detected_product_ids": list(product_ids),
                    "blacklist_candidates": [
                        {"productId": product_id} for product_id in product_ids
                    ],
                }),
                duration_seconds=3.0,
            ))
        session.commit()


def _add_action_log(session_factory, *, query, product_id, action_type, created_at):
    with session_factory() as session:
        session.add(ActionLog(
            brand="atlas", query_text=query, product_id=product_id,
            product_name=f"商品{product_id}", action_type=action_type,
            reason="测试动作",
            created_at=created_at, updated_at=created_at,
        ))
        session.commit()


def test_relevance_detection_accuracy_auto_mode_starts_full_and_drops_after_cancel():
    engine, factory = _database()
    _seed_relevance_flagged_batch(factory)
    service = QueryGovernanceReadService(factory)
    try:
        for query, product_id in (("连衣裙", "P1"), ("连衣裙", "P2"), ("衬衫", "P1")):
            _add_action_log(
                factory, query=query, product_id=product_id, action_type=1,
                created_at=datetime(2026, 8, 29, 1, 0),
            )

        telemetry = service.overview(["atlas"])["brands"][0]["telemetry"]
        assert telemetry["irrelevantProductPairCount"] == 3
        assert telemetry["blacklistedProductCount"] == 3
        assert telemetry["relevanceDetectionAccuracy"] == 1.0
        assert "irrelevantProductPairs" not in telemetry

        # 次日运营取消一条误判拉黑：同一 (query, product) 的最新动作生效。
        _add_action_log(
            factory, query="连衣裙", product_id="P1", action_type=5,
            created_at=datetime(2026, 8, 30, 9, 0),
        )

        telemetry = service.overview(["atlas"])["brands"][0]["telemetry"]
        assert telemetry["blacklistedProductCount"] == 2
        assert telemetry["relevanceDetectionAccuracy"] == 0.6667
    finally:
        engine.dispose()


def test_relevance_detection_accuracy_manual_mode_starts_zero_and_rises_after_approval():
    engine, factory = _database()
    _seed_relevance_flagged_batch(factory)
    service = QueryGovernanceReadService(factory)
    try:
        service.materialize_overview_summary("atlas", data_date=date(2026, 8, 28))
        # 人工审批模式：判定后尚未拉黑，准确率为 0 而非缺口。
        telemetry = service.brand_summary("atlas", data_date=date(2026, 8, 28))["batch"]["telemetry"]
        assert telemetry["irrelevantProductPairCount"] == 3
        assert telemetry["blacklistedProductCount"] == 0
        assert telemetry["relevanceDetectionAccuracy"] == 0.0

        # 运营确认拉黑其中一个组合。
        _add_action_log(
            factory, query="衬衫", product_id="P1", action_type=1,
            created_at=datetime(2026, 8, 30, 9, 0),
        )

        telemetry = service.brand_summary("atlas", data_date=date(2026, 8, 28))["batch"]["telemetry"]
        assert telemetry["blacklistedProductCount"] == 1
        assert telemetry["relevanceDetectionAccuracy"] == 0.3333
    finally:
        engine.dispose()


def test_metric_trend_projects_current_blacklist_state_per_day():
    engine, factory = _database()
    _seed_relevance_flagged_batch(factory)
    service = QueryGovernanceReadService(factory)
    try:
        _add_action_log(factory, query="连衣裙", product_id="P1", action_type=1, created_at=datetime(2026, 8, 29, 1, 0))
        _add_action_log(factory, query="连衣裙", product_id="P2", action_type=1, created_at=datetime(2026, 8, 29, 1, 0))
        _add_action_log(factory, query="连衣裙", product_id="P2", action_type=5, created_at=datetime(2026, 8, 30, 9, 0))
        service.materialize_overview_summary("atlas", data_date=date(2026, 8, 28))

        payload = service.metric_trend("atlas", start_date=date(2026, 8, 28), end_date=date(2026, 8, 28))

        metrics = payload["days"][0]["metrics"]
        assert metrics["irrelevant_product_count"] == 4
        assert metrics["irrelevant_product_pair_count"] == 3
        assert metrics["blacklisted_product_count"] == 1
        assert metrics["relevance_detection_accuracy"] == 0.3333

        # 旧格式批次没有对清单：新指标留缺口，既有指标不受影响。
        with factory() as session:
            stored = session.execute(select(QueryGovernanceBatchSummary)).scalar_one()
            persisted = json.loads(stored.payload_json)
            persisted["telemetry"].pop("irrelevantProductPairs", None)
            persisted["telemetry"].pop("irrelevantProductPairCount", None)
            stored.payload_json = json.dumps(persisted)
            session.commit()

        payload = service.metric_trend("atlas", start_date=date(2026, 8, 28), end_date=date(2026, 8, 28))
        metrics = payload["days"][0]["metrics"]
        assert metrics["irrelevant_product_count"] == 4
        assert metrics["blacklisted_product_count"] is None
        assert metrics["relevance_detection_accuracy"] is None
    finally:
        engine.dispose()


def test_metric_trend_treats_stale_quality_version_days_as_gaps():
    engine, factory = _database()
    _seed(factory)
    service = QueryGovernanceReadService(factory)
    try:
        summary = service.materialize_overview_summary("atlas", data_date=date(2026, 8, 28))
        with factory() as session:
            stored = session.execute(
                select(QueryGovernanceBatchSummary).where(
                    QueryGovernanceBatchSummary.batch_id == summary["batchId"],
                    QueryGovernanceBatchSummary.brand == "atlas",
                )
            ).scalar_one()
            payload_json = json.loads(stored.payload_json)
            payload_json["telemetry"].pop("qualityMetricsVersion", None)
            stored.payload_json = json.dumps(payload_json)
            session.commit()

        payload = service.metric_trend(
            "atlas",
            start_date=date(2026, 8, 28),
            end_date=date(2026, 8, 28),
        )

        assert payload["days"] == [
            {"date": "2026-08-28", "batchId": "batch-atlas-20260829", "metrics": None}
        ]
    finally:
        engine.dispose()


def test_metric_trend_clamps_the_window_to_the_180_day_server_limit():
    engine, factory = _database()
    _seed(factory)
    service = QueryGovernanceReadService(factory)
    try:
        payload = service.metric_trend(
            "atlas",
            start_date=date(2025, 1, 1),
            end_date=date(2026, 8, 28),
        )

        assert payload["endDate"] == "2026-08-28"
        assert payload["startDate"] == "2026-03-02"
        assert len(payload["days"]) == 180
    finally:
        engine.dispose()


def test_query_detail_inventory_scans_read_events_within_today_and_never_fabricate():
    """当天巡检记录只来自真实 inventory_scan 事件；无事件时不得伪造兜底行。"""

    engine, session_factory = _database()
    try:
        batch_id = "query-governance:atlas:2026-10-14"
        now = datetime.now()
        # 锚定到当天固定时刻，避免测试在凌晨运行时事件日期跨天
        day_base = datetime.combine(now.date(), datetime.min.time())
        latest_scan_at = day_base + timedelta(hours=10, minutes=8)
        earlier_scan_at = day_base + timedelta(hours=9, minutes=8)
        yesterday_scan_at = day_base + timedelta(days=-1, hours=11)
        with session_factory() as session:
            session.add_all([
                QueryGovernanceSelectionBatch(
                    batch_id=batch_id, brand="atlas", data_date=now.date(),
                    config_json=json.dumps({"check_modes": {"recall": True, "inventory": True}}),
                ),
                QueryGovernanceWorkflowRun(
                    batch_id=batch_id, status="completed", started_at=now - timedelta(hours=2),
                ),
                SchedulerJobRun(
                    job_id="inventory_monitor_job:atlas", scheduled_for=now - timedelta(hours=1),
                    idempotency_key="scan-events-test", status="succeeded", attempt_no=1,
                    claim_owner="test", claim_token="test", release="test", config_fingerprint="b" * 64,
                    claimed_at=now - timedelta(hours=1), started_at=now - timedelta(hours=1),
                    heartbeat_at=now - timedelta(hours=1), terminal_at=now - timedelta(minutes=50),
                    terminal_summary="scheduler job succeeded duration_ms=100",
                ),
            ])
            scanned = QueryGovernanceTask(
                batch_id=batch_id, brand="atlas", data_date=now.date(),
                query_text="牛仔裤 女士", query_rank=1, status="completed",
            )
            unscanned = QueryGovernanceTask(
                batch_id=batch_id, brand="atlas", data_date=now.date(),
                query_text="衬衫 女士", query_rank=2, status="completed",
            )
            session.add_all([scanned, unscanned])
            session.flush()
            session.add_all([
                QueryGovernanceAgentEvent(
                    task_id=scanned.id, brand="atlas", query_text="牛仔裤 女士",
                    agent_name="inventory_monitor", node_key="inventory",
                    event_type="inventory_scan",
                    output_json=json.dumps({
                        "checkedAt": latest_scan_at.strftime("%Y-%m-%d %H:%M"),
                        "brokenProductCount": 1,
                        "normalProductCount": 3,
                        "recoveredProductCount": 0,
                    }),
                    created_at=latest_scan_at,
                ),
                QueryGovernanceAgentEvent(
                    task_id=scanned.id, brand="atlas", query_text="牛仔裤 女士",
                    agent_name="inventory_monitor", node_key="inventory",
                    event_type="inventory_scan",
                    output_json=json.dumps({
                        "checkedAt": earlier_scan_at.strftime("%Y-%m-%d %H:%M"),
                        "brokenProductCount": 0,
                        "normalProductCount": 4,
                        "recoveredProductCount": 1,
                    }),
                    created_at=earlier_scan_at,
                ),
                QueryGovernanceAgentEvent(
                    task_id=scanned.id, brand="atlas", query_text="牛仔裤 女士",
                    agent_name="inventory_monitor", node_key="inventory",
                    event_type="inventory_scan",
                    output_json=json.dumps({
                        "checkedAt": yesterday_scan_at.strftime("%Y-%m-%d %H:%M"),
                        "brokenProductCount": 2,
                        "normalProductCount": 2,
                        "recoveredProductCount": 0,
                    }),
                    created_at=yesterday_scan_at,
                ),
            ])
            session.commit()
            scanned_id, unscanned_id = scanned.id, unscanned.id

        service = QueryGovernanceReadService(session_factory)
        detail = service.query_detail(scanned_id)
        assert detail["inventoryScans"] == [
            {
                "checkedAt": latest_scan_at.strftime("%Y-%m-%d %H:%M"),
                "brokenProductCount": 1,
                "normalProductCount": 3,
                "recoveredProductCount": 0,
            },
            {
                "checkedAt": earlier_scan_at.strftime("%Y-%m-%d %H:%M"),
                "brokenProductCount": 0,
                "normalProductCount": 4,
                "recoveredProductCount": 1,
            },
        ]

        empty = service.query_detail(unscanned_id)
        assert empty["inventoryScans"] == []
    finally:
        engine.dispose()
