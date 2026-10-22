"""Offline coverage for the image-integrity audit helpers.

The Harbor incident (2026-09-29~10-12) wrote one denim-shorts photo onto thousands
of product documents.  The application now refuses to judge products on a shared
image, but the same pollution must also be *discoverable* after the fact: given
snapshot/asset observations the audit has to flag a shared image, and it must not
cry wolf when the catalog legitimately reuses one photo (e.g. two colourways of
one shoe).
"""

from __future__ import annotations

from src.query_governance.image_integrity import (
    asset_records_mismatch,
    image_storage_key,
    IMAGE_URL_MISSING,
    IMAGE_URL_SHARED,
    SHARED_LEGITIMATE,
    SHARED_SUSPECT,
    SHARED_UNKNOWN,
    VERDICT_ALERT,
    INTEGRITY_OK,
    classify_shared_group,
    image_health_report,
    normalize_image_key,
    render_alert_lines,
    shared_image_groups,
)

BAD_URL = "https://asset.harbor.example.test/media/catalog/product/f/a/fae7537d5904011483cc1bd867f13b0432a5a1c1_xxl-1.jpg"


def test_normalize_image_key_ignores_case_and_query():
    assert normalize_image_key(" HTTPS://Asset.HARBOR.EXAMPLE.TEST/A.jpg?x=1#frag ") == "https://asset.harbor.example.test/A.jpg"
    assert normalize_image_key("//asset.harbor.example.test/a.jpg") == "https://asset.harbor.example.test/a.jpg"
    assert normalize_image_key("") == ""
    assert normalize_image_key(None) == ""


def test_shared_image_groups_finds_one_photo_on_many_products():
    observations = [
        {"product_id": "P1", "image_url": BAD_URL},
        {"product_id": "P2", "image_url": BAD_URL + "?v=2"},
        {"product_id": "P3", "image_url": "https://asset.harbor.example.test/other.jpg"},
        {"product_id": "P4", "image_url": ""},
    ]
    groups = shared_image_groups(observations)
    assert set(groups) == {normalize_image_key(BAD_URL)}
    assert groups[normalize_image_key(BAD_URL)] == ["P1", "P2"]


def test_shared_image_groups_ignores_repeats_of_one_product_and_snapshots():
    observations = [
        {"product_id": "P1", "imageUrl": BAD_URL},
        {"product_id": "P1", "imageUrl": BAD_URL},
    ]
    assert shared_image_groups(observations) == {}


def test_classify_shared_group_distinguishes_catalog_truth():
    ids = ["P1", "P2"]
    same = {pid: {"catalog_url": BAD_URL, "url_match": True} for pid in ids}
    assert classify_shared_group(ids, same) == (SHARED_LEGITIMATE, 2)
    mixed = {"P1": {"catalog_url": BAD_URL, "url_match": True}, "P2": {"catalog_url": "https://asset.harbor.example.test/right.jpg", "url_match": False}}
    assert classify_shared_group(ids, mixed) == (SHARED_SUSPECT, 2)
    assert classify_shared_group(ids, None) == (SHARED_UNKNOWN, 0)


def test_health_report_passes_on_clean_observations():
    observations = [
        {"product_id": "P1", "image_url": "https://asset.harbor.example.test/a.jpg"},
        {"product_id": "P2", "image_url": "https://asset.harbor.example.test/b.jpg"},
    ]
    report = image_health_report(observations)
    assert report["verdict"] == INTEGRITY_OK
    assert report["alerts"] == []
    assert report["shared_group_count"] == 0
    assert report["missing_image_count"] == 0


def test_health_report_alerts_on_shared_suspect_image():
    observations = [{"product_id": f"P{i}", "image_url": BAD_URL} for i in range(1, 6)]
    observations.append({"product_id": "P9", "image_url": "https://asset.harbor.example.test/healthy.jpg"})
    checks = {
        "P1": {"catalog_url": BAD_URL, "url_match": True},
        "P2": {"catalog_url": "https://asset.harbor.example.test/right.jpg", "url_match": False},
    }
    report = image_health_report(observations, catalog_checks=checks)
    assert report["verdict"] == VERDICT_ALERT
    assert report["shared_product_count"] == 5
    assert report["catalog_mismatch_count"] == 1
    codes = [alert["code"] for alert in report["alerts"]]
    assert IMAGE_URL_SHARED in codes
    assert "IMAGE_CATALOG_MISMATCH" in codes
    shared_alert = next(alert for alert in report["alerts"] if alert["code"] == IMAGE_URL_SHARED)
    assert "5 个商品共用同一张图" in shared_alert["message"]
    assert report["shared_groups"][0]["catalog_status"] == SHARED_SUSPECT


def test_health_report_keeps_legitimate_catalog_reuse_quiet():
    observations = [
        {"product_id": "1318025001", "image_url": "https://asset.harbor.example.test/loafer.jpg"},
        {"product_id": "1318025002", "image_url": "https://asset.harbor.example.test/loafer.jpg"},
    ]
    checks = {
        pid: {"catalog_url": "https://asset.harbor.example.test/loafer.jpg", "url_match": True}
        for pid in ("1318025001", "1318025002")
    }
    report = image_health_report(observations, catalog_checks=checks, shared_alert_min_products=2)
    assert report["verdict"] == INTEGRITY_OK
    assert report["shared_group_count"] == 1
    assert report["shared_groups"][0]["catalog_status"] == SHARED_LEGITIMATE


def test_health_report_alerts_on_missing_images_only_above_threshold():
    observations = [{"product_id": f"P{i}", "image_url": ""} for i in range(1, 4)] + [
        {"product_id": "P9", "image_url": "https://asset.harbor.example.test/a.jpg"}
    ]
    quiet = image_health_report(observations, missing_alert_ratio=0.8)
    noisy = image_health_report(observations, missing_alert_ratio=0.1)
    assert quiet["verdict"] == INTEGRITY_OK
    assert noisy["verdict"] == VERDICT_ALERT
    assert any(alert["code"] == IMAGE_URL_MISSING for alert in noisy["alerts"])


def test_render_alert_lines_surfaces_verdict_and_findings():
    report = image_health_report([{"product_id": f"P{i}", "image_url": BAD_URL} for i in range(1, 4)])
    lines = render_alert_lines(report)
    assert lines[0].startswith("IMAGE-INTEGRITY ")
    assert "verdict=ALERT" in lines[0]
    assert any(line.startswith("[ALERT] IMAGE_URL_SHARED:") for line in lines)
    assert any(line.startswith("[INFO] IMAGE_URL_SHARED:") for line in lines)

def test_asset_records_mismatch_flags_foreign_cache_pointer():
    prefix = "query-governance/images/prod/v1"
    good_source = "https://asset.harbor.example.test/right.jpg"
    good = {
        "product_id": "P1",
        "source_url": good_source,
        "storage_key": image_storage_key(good_source, prefix=prefix),
        "cached_url": f"https://bucket.cos.example.com/{image_storage_key(good_source, prefix=prefix)}",
    }
    foreign = {
        "product_id": "P2",
        "source_url": good_source,
        "storage_key": image_storage_key(good_source, prefix=prefix),
        "cached_url": "https://bucket.cos.example.com/query-governance/images/prod/v1/ffffffff.jpg",
    }
    findings = asset_records_mismatch([good, foreign])
    assert [item["product_id"] for item in findings] == ["P2"]
    assert findings[0]["issues"] == ["cached_url_points_elsewhere"]


def test_asset_records_mismatch_flags_storage_key_not_from_source_url():
    row = {
        "product_id": "P3",
        "source_url": "https://asset.harbor.example.test/right.jpg",
        "storage_key": "query-governance/images/prod/v1/00000000.jpg",
        "cached_url": "https://bucket.cos.example.com/query-governance/images/prod/v1/00000000.jpg",
    }
    findings = asset_records_mismatch([row])
    assert findings[0]["issues"] == ["storage_key_not_derived_from_source_url"]


def test_health_report_raises_alert_for_foreign_asset_rows():
    report = image_health_report(
        [{"product_id": "P1", "image_url": "https://asset.harbor.example.test/a.jpg"}],
        asset_mismatches=[{"product_id": "P2", "issues": ["cached_url_points_elsewhere"]}],
    )
    assert report["verdict"] == VERDICT_ALERT
    assert report["asset_mismatch_count"] == 1
    assert any(alert["code"] == "IMAGE_STORAGE_KEY_FOREIGN" for alert in report["alerts"])
