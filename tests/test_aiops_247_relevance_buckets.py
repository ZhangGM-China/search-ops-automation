from __future__ import annotations

from src.query_governance.relevance_buckets import (
    DEFAULT_RELEVANCE_BUCKETS,
    KNOWN_RELEVANCE_BUCKETS,
    filter_relevance_products,
    normalize_relevance_buckets,
)


def test_default_buckets_are_the_six_l1_l2_leaf_buckets():
    assert DEFAULT_RELEVANCE_BUCKETS == (
        "productIdsByCode",
        "productIdsByProductNameTag",
        "productIdsByCodeSpellCorrections",
        "productIdsByTagsAll",
        "productIdsByWhiteList",
        "productIdsBySubTag",
    )


def test_empty_or_unknown_bucket_config_falls_back_to_defaults_and_deduplicates():
    assert normalize_relevance_buckets([]) == DEFAULT_RELEVANCE_BUCKETS
    assert normalize_relevance_buckets(["unknown"]) == DEFAULT_RELEVANCE_BUCKETS
    assert normalize_relevance_buckets(
        ["productIdsByCode", "productIdsByCode", "unknown"]
    ) == ("productIdsByCode",)


def test_dynamic_bucket_is_a_supported_opt_in_without_changing_global_defaults():
    assert "Dynamic" not in DEFAULT_RELEVANCE_BUCKETS
    assert normalize_relevance_buckets(["Dynamic", "productIdsByCode"]) == (
        "Dynamic",
        "productIdsByCode",
    )


def test_filter_includes_dynamic_products_only_when_selected():
    products = [
        {"product_id": "dynamic", "ext": {"bucket": "Dynamic"}},
        {"product_id": "l1", "ext": {"bucket": "productIdsByCode"}},
    ]

    selected = filter_relevance_products(
        products,
        product_limit=2,
        configured_buckets=["Dynamic"],
    )
    not_selected = filter_relevance_products(
        products,
        product_limit=2,
        configured_buckets=["productIdsByCode"],
    )

    assert [item["product_id"] for item in selected] == ["dynamic"]
    assert [item["product_id"] for item in not_selected] == ["l1"]


def test_filter_takes_top_n_before_bucket_filtering_and_reads_ext_bucket():
    products = [
        {"product_id": "p1", "ext": {"bucket": "productIdsByCode"}},
        {"product_id": "p2", "ext": {"bucket": "productIdsByTagsAll"}},
        {"product_id": "p3", "ext": {"bucket": "productIdsByCode"}},
    ]

    result = filter_relevance_products(
        products,
        product_limit=2,
        configured_buckets=["productIdsByCode"],
    )

    assert [item["product_id"] for item in result] == ["p1"]


def test_filter_supports_legacy_top_level_bucket():
    products = [
        {"product_id": "p1", "bucket": "productIdsBySubTag"},
        {"product_id": "p2", "bucket": "productIdsByCode"},
    ]

    result = filter_relevance_products(
        products,
        product_limit=2,
        configured_buckets=["productIdsByCode"],
    )

    assert [item["product_id"] for item in result] == ["p2"]


def test_selecting_all_known_buckets_disables_bucket_filtering():
    products = [
        {"product_id": "without-bucket"},
        {"product_id": "unknown-bucket", "bucket": "summit-all"},
        {"product_id": "known-bucket", "ext": {"bucket": "productIdsByCode"}},
    ]

    result = filter_relevance_products(
        products,
        product_limit=3,
        configured_buckets=list(KNOWN_RELEVANCE_BUCKETS),
    )

    assert [item["product_id"] for item in result] == [
        "without-bucket",
        "unknown-bucket",
        "known-bucket",
    ]


def test_partial_bucket_selection_keeps_existing_filtering_semantics():
    products = [
        {"product_id": "without-bucket"},
        {"product_id": "known-bucket", "bucket": "productIdsByCode"},
    ]

    result = filter_relevance_products(
        products,
        product_limit=2,
        configured_buckets=["productIdsByCode"],
    )

    assert [item["product_id"] for item in result] == ["known-bucket"]
