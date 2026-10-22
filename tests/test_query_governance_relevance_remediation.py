from __future__ import annotations

import pytest

from src.query_governance.relevance_remediation import (
    RelevanceRootCauseAgent,
    build_relevance_remediation_payload,
    decide_blacklist_handling,
)


class _RootCauseRuntime:
    def __init__(self, response):
        self.response = response
        self.messages = []

    def invoke(self, messages, **kwargs):
        self.messages.append(messages)
        return self.response


class _Response:
    def __init__(self, content):
        self.content = content


def test_root_cause_agent_groups_all_candidates_and_keeps_omissions_uncertain():
    runtime = _RootCauseRuntime(
        _Response(
            '{"groups":['
            '{"rootCause":"individual","causeKey":"one-off","productIds":["P1"],"reason":"单商品标签异常","confidence":0.91},'
            '{"rootCause":"kg_issue","causeKey":"wide-relation","productIds":["P2"],"reason":"关系过宽","actions":[{"actionType":"delete_relationship","params":{"tagName":"裙子","responseTagName":"鞋"}}]}'
            ']}'
        )
    )
    agent = RelevanceRootCauseAgent(runtime=runtime)

    result = agent.analyze(
        brand="atlas",
        query_text="裙子",
        evidence={
            "blacklist_candidates": [
                {"product_id": "P1", "reason": "不相关"},
                {"product_id": "P2", "reason": "不相关"},
                {"product_id": "P3", "reason": "不相关"},
            ],
            "queryTags": ["裙子"],
            "kgContext": {"tags": {"裙子": {"id": "tag-1"}}},
        },
    )

    assert [group["rootCause"] for group in result["causeGroups"]] == [
        "individual", "kg_issue", "uncertain"
    ]
    assert result["causeGroups"][0]["conclusion"] == "个例，需拉黑"
    assert result["causeGroups"][1]["actions"][0]["actionType"] == "delete_relationship"
    assert result["causeGroups"][2]["productIds"] == ["P3"]
    assert runtime.messages[0][0]["role"] == "system"


@pytest.mark.parametrize(
    "mode,should_execute,should_persist_pending,record_only",
    [
        ("auto", True, False, False),
        ("human_approval", False, True, False),
        ("disabled", False, False, True),
    ],
)
def test_blacklist_handling_preserves_configured_execution_mode(
    mode, should_execute, should_persist_pending, record_only
):
    decision = decide_blacklist_handling(
        [{"product_id": "P1", "reason": "与 Query 不相关"}],
        execution_mode=mode,
    )

    assert decision == {
        "executionMode": mode,
        "shouldExecute": should_execute,
        "shouldPersistPending": should_persist_pending,
        "recordOnly": record_only,
        "productIds": ["P1"],
        "imageSuspectProductIds": [],
        "imageAutoDowngraded": False,
    }


def test_blacklist_handling_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unsupported product blacklist execution mode"):
        decide_blacklist_handling([{"product_id": "P1"}], execution_mode="manual")


def test_remediation_payload_groups_all_query_products_by_root_cause():
    payload = build_relevance_remediation_payload(
        query="男士牛仔裤",
        candidates=[
            {"product_id": "P1", "root_cause": "individual", "reason": "单商品标签异常"},
            {
                "product_id": "P2",
                "root_cause": "kg_issue",
                "cause_key": "relationship-too-wide",
                "reason": "标签关系过宽",
                "kg_actions": [
                    {
                        "actionType": "delete_relationship",
                        "params": {"tagName": "牛仔裤", "responseTagName": "裙子"},
                    }
                ],
            },
            {
                "product_id": "P3",
                "root_cause": "kg_issue",
                "cause_key": "relationship-too-wide",
                "reason": "标签关系过宽",
            },
            {"product_id": "P4", "reason": "证据不足"},
        ],
        query_tags=[" 牛仔裤 ", "男士", "牛仔裤"],
        kg_context={"tags": {"牛仔裤": {"id": "tag-1"}}},
    )

    assert payload["query"] == "男士牛仔裤"
    assert payload["queryTags"] == ["牛仔裤", "男士"]
    assert payload["kgContext"] == {"tags": {"牛仔裤": {"id": "tag-1"}}}
    assert payload["errorProductIds"] == ["P1", "P2", "P3", "P4"]
    assert [group["rootCause"] for group in payload["causeGroups"]] == [
        "individual",
        "kg_issue",
        "uncertain",
    ]
    individual, kg_issue, uncertain = payload["causeGroups"]
    assert individual["productIds"] == ["P1"]
    assert individual["conclusion"] == "个例，需拉黑"
    assert individual["needKgAdjustment"] is False
    assert kg_issue["productIds"] == ["P2", "P3"]
    assert kg_issue["needKgAdjustment"] is True
    assert kg_issue["actions"] == [
        {
            "step": 1,
            "actionType": "delete_relationship",
            "dependsOn": [],
            "params": {"tagName": "牛仔裤", "responseTagName": "裙子"},
            "riskLevel": "medium",
            "rollbackPlan": [],
        }
    ]
    assert kg_issue["analysisEvidence"] == ["标签关系过宽"]
    assert kg_issue["confidence"] is None
    assert uncertain["productIds"] == ["P4"]
    assert uncertain["conclusion"] == "根因待人工确认"


def test_remediation_payload_rejects_unsupported_kg_action_without_writing():
    with pytest.raises(ValueError, match="unsupported KG remediation action"):
        build_relevance_remediation_payload(
            query="裙子",
            candidates=[
                {
                    "product_id": "P1",
                    "root_cause": "kg_issue",
                    "kg_actions": [{"actionType": "raw_sql", "params": {}}],
                }
            ],
        )


def test_blacklist_handling_downgrades_auto_when_image_is_suspect():
    candidates = [
        {"product_id": "1328942002", "image_checked": False, "image_warning": "IMAGE_URL_SHARED"},
        {"product_id": "1323994001", "image_checked": False, "image_warning": "IMAGE_URL_SHARED"},
    ]
    decision = decide_blacklist_handling(candidates, execution_mode="auto")
    assert decision["executionMode"] == "human_approval"
    assert decision["shouldExecute"] is False
    assert decision["shouldPersistPending"] is True
    assert decision["imageSuspectProductIds"] == ["1328942002", "1323994001"]
    assert decision["imageAutoDowngraded"] is True


def test_blacklist_handling_downgrades_even_when_the_suspect_flag_is_inconsistent():
    decision = decide_blacklist_handling(
        [{"product_id": "P1", "image_checked": True, "image_warning": "IMAGE_URL_SHARED"}],
        execution_mode="auto",
    )
    assert decision["executionMode"] == "human_approval"
    assert decision["shouldExecute"] is False
    assert decision["imageAutoDowngraded"] is True


def test_blacklist_handling_keeps_auto_for_a_product_without_any_image():
    decision = decide_blacklist_handling(
        [{"product_id": "P1", "image_checked": False, "image_warning": "IMAGE_URL_MISSING"}],
        execution_mode="auto",
    )
    assert decision["executionMode"] == "auto"
    assert decision["shouldExecute"] is True
    assert decision["imageSuspectProductIds"] == []
    assert decision["imageAutoDowngraded"] is False


def test_blacklist_handling_keeps_auto_for_image_checked_candidates():
    decision = decide_blacklist_handling(
        [
            {"product_id": "P1", "image_checked": True},
            {"product_id": "P2", "image_checked": True, "image_warning": None},
        ],
        execution_mode="auto",
    )
    assert decision["executionMode"] == "auto"
    assert decision["shouldExecute"] is True
    assert decision["imageSuspectProductIds"] == []
    assert decision["imageAutoDowngraded"] is False


def test_blacklist_handling_keeps_legacy_candidates_executable():
    decision = decide_blacklist_handling([{"product_id": "P1"}], execution_mode="auto")
    assert decision["executionMode"] == "auto"
    assert decision["shouldExecute"] is True


def test_blacklist_handling_respects_human_and_disabled_modes_with_suspect_images():
    candidates = [{"product_id": "P1", "image_warning": "IMAGE_URL_SHARED"}]
    human = decide_blacklist_handling(candidates, execution_mode="human_approval")
    disabled = decide_blacklist_handling(candidates, execution_mode="disabled")
    assert human["executionMode"] == "human_approval"
    assert human["shouldPersistPending"] is True
    assert human["imageAutoDowngraded"] is False
    assert disabled["executionMode"] == "disabled"
    assert disabled["recordOnly"] is True
    assert disabled["shouldExecute"] is False


def test_blacklist_handling_reads_multi_product_candidates():
    decision = decide_blacklist_handling(
        [{"productIds": ["P1", "P2"], "image_warning": "IMAGE_URL_SHARED"}], execution_mode="auto"
    )
    assert decision["productIds"] == ["P1", "P2"]
    assert decision["imageSuspectProductIds"] == ["P1", "P2"]
