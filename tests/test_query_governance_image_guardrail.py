"""Offline coverage for the image-warning guard shown on governance cards.

A product judged while its image was unusable (missing or shared by many
products) must stay visible on the card, but the card must not keep the
automatic execution mode: the operator confirms it by hand.
"""

from __future__ import annotations

from src.query_governance.read_api import (
    _apply_image_warning_guard,
    _image_warning_count,
    _plan_products,
)

BAD_URL = "https://asset.harbor.example.test/media/catalog/product/f/a/fae7537d5904011483cc1bd867f13b0432a5a1c1_xxl-1.jpg"


def test_plan_products_merges_request_level_image_warnings():
    request = {
        "product_ids": ["P1", "P2"],
        "product_images": {"P1": BAD_URL},
        "image_warnings": {"P1": "IMAGE_URL_SHARED"},
        "image_warning_messages": {"P1": "同一图片被 868 个商品共用，已跳过图片判断"},
    }
    products = _plan_products(request, {}, {"products": []})
    by_id = {item["productId"]: item for item in products}
    assert by_id["P1"]["imageUrl"] == BAD_URL
    assert by_id["P1"]["imageWarning"] == "IMAGE_URL_SHARED"
    assert "868" in by_id["P1"]["imageWarningMessage"]
    assert "imageWarning" not in by_id["P2"]


def test_plan_products_reads_candidate_level_warnings():
    candidates = [
        {"product_id": "P1", "image_url": BAD_URL, "image_warning": "IMAGE_URL_SHARED"},
        {"product_id": "P2", "image_url": "https://asset.harbor.example.test/ok.jpg"},
    ]
    products = _plan_products({"products": candidates}, {}, {"products": []})
    by_id = {item["productId"]: item for item in products}
    assert by_id["P1"]["imageWarning"] == "IMAGE_URL_SHARED"
    assert "imageWarning" not in by_id["P2"]
    assert _image_warning_count(products) == 1


def test_image_warning_guard_downgrades_auto_execution_mode():
    plan = {"executionMode": "auto", "cancelable": True}
    products = [{"productId": "P1", "imageWarning": "IMAGE_URL_SHARED"}]
    _apply_image_warning_guard(plan, products)
    assert plan["executionMode"] == "human_approval"
    assert plan["cancelable"] is False
    assert plan["imageWarningCount"] == 1


def test_image_warning_guard_keeps_manual_and_disabled_modes():
    for mode in ("human_approval", "disabled"):
        plan = {"executionMode": mode, "cancelable": False}
        _apply_image_warning_guard(plan, [{"productId": "P1", "imageWarning": "IMAGE_URL_MISSING"}])
        assert plan["executionMode"] == mode
        assert plan["imageWarningCount"] == 1


def test_image_warning_guard_is_noop_without_warnings():
    plan = {"executionMode": "auto", "cancelable": True}
    _apply_image_warning_guard(plan, [{"productId": "P1"}])
    assert plan == {"executionMode": "auto", "cancelable": True}

def test_image_warning_guard_keeps_auto_for_missing_images():
    plan = {"executionMode": "auto", "cancelable": True}
    _apply_image_warning_guard(plan, [{"productId": "P1", "imageWarning": "IMAGE_URL_MISSING"}])
    assert plan["executionMode"] == "auto"
    assert plan["imageWarningCount"] == 1
    assert "imageSuspectCount" not in plan


def test_image_warning_guard_counts_suspect_products():
    plan = {"executionMode": "auto", "cancelable": True}
    _apply_image_warning_guard(
        plan,
        [
            {"productId": "P1", "imageWarning": "IMAGE_URL_SHARED"},
            {"productId": "P2", "imageWarning": "IMAGE_URL_MISSING"},
        ],
    )
    assert plan["executionMode"] == "human_approval"
    assert plan["imageWarningCount"] == 2
    assert plan["imageSuspectCount"] == 1
