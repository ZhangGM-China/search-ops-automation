from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.persistence.models import Base
from src.query_governance.models import (
    QueryGovernanceRelevanceGuidance,
    QueryGovernanceRelevanceGuidanceReason,
)
from src.query_governance.relevance_guidance import (
    GuidanceNotFoundError,
    RelevanceGuidanceService,
)


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


@dataclass
class _Invocation:
    content: str
    response_metadata: dict | None = None


@dataclass
class _Runtime:
    response: str
    finish_reason: str | None = None
    calls: list[tuple[list[dict], dict]] = field(default_factory=list)

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        metadata = {"finish_reason": self.finish_reason} if self.finish_reason else None
        return _Invocation(self.response, metadata)


def test_rule_crud_uses_one_brand_query_and_deactivation(session_factory):
    service = RelevanceGuidanceService(session_factory)

    created = service.save("ATLAS", "  连衣裙  ", "只接受女装连衣裙", operator="alice")
    assert created["brand"] == "ATLAS"
    assert created["query"] == "连衣裙"
    assert created["status"] == "active"
    assert service.get("ATLAS", "连衣裙")["rules"] == "只接受女装连衣裙"

    updated = service.save("ATLAS", "连衣裙", "只接受女装连衣裙\n排除童装", operator="alice")
    assert updated["id"] == created["id"]
    assert service.deactivate("ATLAS", "连衣裙", operator="alice")["status"] == "inactive"
    assert service.get("ATLAS", "连衣裙")["status"] == "inactive"


def test_cancel_reason_is_audited_without_becoming_a_rule(session_factory):
    service = RelevanceGuidanceService(session_factory)
    service.save("atlas", "裙子", "1. 排除男装", operator="alice")

    first = service.record_cancel_reason(
        "atlas", "裙子", "商品是男装，不应被召回", product_id="SKU-1", operator="alice"
    )
    second = service.record_cancel_reason(
        "atlas", "裙子", "商品是男装，不应被召回", product_id="SKU-2", operator="bob"
    )
    empty = service.record_cancel_reason(
        "atlas", "裙子", "  ", product_id="SKU-3", operator="bob"
    )

    assert first["reasonRecorded"] is True
    assert second["reasonRecorded"] is True
    assert empty["reasonRecorded"] is False
    with Session(session_factory.kw["bind"]) as db:
        guidance = db.scalars(select(QueryGovernanceRelevanceGuidance)).one()
        reasons = db.scalars(
            select(QueryGovernanceRelevanceGuidanceReason).order_by(QueryGovernanceRelevanceGuidanceReason.id)
        ).all()
    assert guidance.rules_text == "1. 排除男装"
    assert guidance.status == "active"
    assert len(reasons) == 2
    assert reasons[0].reason_text == "商品是男装，不应被召回"
    assert reasons[0].product_id == "SKU-1"
    assert reasons[0].operator == "alice"
    assert reasons[1].product_id == "SKU-2"
    assert reasons[1].operator == "bob"
    assert [item["reason"] for item in service.get("atlas", "裙子")["reasonHistory"]] == [
        "商品是男装，不应被召回",
        "商品是男装，不应被召回",
    ]


def test_cancel_reason_never_creates_guidance_rules(session_factory):
    service = RelevanceGuidanceService(session_factory)

    service.record_cancel_reason(
        "atlas", "裙子", "商品是童装", product_id="SKU-9", operator="alice"
    )

    with Session(session_factory.kw["bind"]) as db:
        assert db.scalars(select(QueryGovernanceRelevanceGuidance)).all() == []
    current = service.get("atlas", "裙子")
    assert current["rules"] == ""
    assert current["status"] == "inactive"
    assert [item["reason"] for item in current["reasonHistory"]] == ["商品是童装"]


def test_record_cancel_reason_requires_product_id(session_factory):
    service = RelevanceGuidanceService(session_factory)
    with pytest.raises(ValueError, match="product_id is required"):
        service.record_cancel_reason("atlas", "裙子", "商品是童装", product_id=" ", operator="alice")


def test_append_rule_uses_the_caller_transaction(session_factory):
    service = RelevanceGuidanceService(session_factory)
    service.save("atlas", "裙子", "排除男装", operator="alice")

    with Session(session_factory.kw["bind"]) as db:
        service.append_rule("atlas", "裙子", "泡泡袖等同于灯笼袖", operator="bob", session=db)
        pending = db.scalars(select(QueryGovernanceRelevanceGuidance)).one()
        assert pending.rules_text == "1. 排除男装\n2. 泡泡袖等同于灯笼袖"
        db.rollback()

    assert service.get("atlas", "裙子")["rules"] == "排除男装"


def test_append_rule_numbers_existing_lines_and_keeps_new_rule_at_end(session_factory):
    service = RelevanceGuidanceService(session_factory)
    service.save("atlas", "夹克", "排除童装\n- 排除鞋履", operator="alice")

    appended = service.append_rule("atlas", "夹克", "夹克外套属于该 Query", operator="bob")
    duplicate = service.append_rule("atlas", "夹克", "夹克外套属于该 Query", operator="bob")

    assert appended["rules"] == "1. 排除童装\n2. 排除鞋履\n3. 夹克外套属于该 Query"
    assert duplicate["rules"] == appended["rules"]


def test_append_rule_rejects_empty_text(session_factory):
    service = RelevanceGuidanceService(session_factory)
    with pytest.raises(ValueError, match="rule is required"):
        service.append_rule("atlas", "夹克", " ", operator="alice")


def test_rule_save_filters_four_byte_characters(session_factory):
    service = RelevanceGuidanceService(session_factory)

    saved = service.save("atlas", "乐福鞋男士", "🥿 乐福鞋穿脱方便", operator="alice")

    assert saved["rules"] == "乐福鞋穿脱方便"
    assert service.get("atlas", "乐福鞋男士")["rules"] == "乐福鞋穿脱方便"


def test_ai_organize_previews_without_write_and_confirm_replaces_rule(session_factory):
    runtime = _Runtime('{"rules":"只保留女装连衣裙，排除男装和童装"}')
    service = RelevanceGuidanceService(session_factory, organizer_runtime=runtime)
    service.save("atlas", "连衣裙", "排除男装\n排除童装", operator="alice")

    preview = service.preview_organize("atlas", "连衣裙", operator="alice")
    assert preview["rules"] == "只保留女装连衣裙，排除男装和童装"
    assert service.get("atlas", "连衣裙")["rules"] == "排除男装\n排除童装"
    assert runtime.calls
    assert "已有规则" in runtime.calls[0][0][0]["content"]

    confirmed = service.confirm_organize(
        "atlas", "连衣裙", preview["rules"], operator="alice"
    )
    assert confirmed["rules"] == preview["rules"]
    assert service.get("atlas", "连衣裙")["rules"] == preview["rules"]


def test_ai_organize_requires_json_rules_and_reports_model_failure(session_factory):
    service = RelevanceGuidanceService(
        session_factory, organizer_runtime=_Runtime("not-json")
    )
    service.save("atlas", "裙子", "排除男装", operator="alice")
    with pytest.raises(ValueError, match="整理结果"):
        service.preview_organize("atlas", "裙子", operator="alice")


def test_ai_organize_reports_truncated_model_output(session_factory):
    service = RelevanceGuidanceService(
        session_factory,
        organizer_runtime=_Runtime('{"rules":"慢跑裤', finish_reason="length"),
    )
    service.save("atlas", "运动裤", "1. 慢跑裤同义卫裤", operator="alice")

    with pytest.raises(ValueError, match="截断"):
        service.preview_organize("atlas", "运动裤", operator="alice")


def _add_reason_row(session_factory, *, created_at: datetime) -> None:
    with Session(session_factory.kw["bind"]) as db:
        db.add(
            QueryGovernanceRelevanceGuidanceReason(
                brand="atlas",
                query_text="运动裤",
                product_id="SKU-1",
                reason_text="商品是休闲短裤",
                operator="joy",
                created_at=created_at,
            )
        )
        db.commit()


def test_cancel_reason_history_is_reported_in_beijing_time(session_factory):
    service = RelevanceGuidanceService(session_factory)
    _add_reason_row(session_factory, created_at=datetime(2026, 10, 9, 3, 5, 14))

    item = service.get("atlas", "运动裤")["reasonHistory"][0]

    assert item["createdAt"] == "2026-10-09 11:05:14"
    assert "T" not in item["createdAt"]


def test_every_write_response_keeps_the_reason_history(session_factory):
    service = RelevanceGuidanceService(session_factory)

    created = service.save("atlas", "运动裤", "排除休闲短裤", operator="alice")
    assert created["reasonHistory"] == []

    _add_reason_row(session_factory, created_at=datetime(2026, 10, 9, 3, 5, 14))

    responses = [
        service.save("atlas", "运动裤", "排除休闲短裤\n包含运动短裤", operator="alice"),
        service.append_rule("atlas", "运动裤", "训练短裤也是运动裤", operator="bob"),
        service.deactivate("atlas", "运动裤", operator="alice"),
    ]

    for payload in responses:
        assert [item["reason"] for item in payload["reasonHistory"]] == ["商品是休闲短裤"]
        assert payload["reasonHistory"][0]["productId"] == "SKU-1"
        assert payload["reasonHistory"][0]["operator"] == "joy"
        assert payload["reasonHistory"][0]["createdAt"] == "2026-10-09 11:05:14"
