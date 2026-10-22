"""Pure helpers for detecting suspect product-image assignments.

The 2026-09/10 Harbor incident proved that a polluted upstream image field can be
carried through the whole pipeline unnoticed: 4k+ products were judged against a
single denim-shorts photo.  The application cannot repair the upstream index,
but it can (a) refuse to judge products on a shared image and (b) keep auditing
what it stored.

This module holds the deterministic part of that audit: URL grouping, catalog
comparison classification and threshold evaluation.  It performs no I/O so it
can be unit-tested offline; the read-only runner lives in
``scripts/ops/audit_harbor_image_integrity.py``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

IMAGE_URL_MISSING = "IMAGE_URL_MISSING"
IMAGE_URL_SHARED = "IMAGE_URL_SHARED"
IMAGE_URL_MISMATCH = "IMAGE_URL_MISMATCH"
IMAGE_STORAGE_KEY_FOREIGN = "IMAGE_STORAGE_KEY_FOREIGN"

# Warning codes that mean "the image itself cannot be trusted", as opposed to a
# product that simply has no image.  Only these may block an automatic
# blacklist write.
SUSPECT_IMAGE_WARNING_CODES = frozenset({IMAGE_URL_SHARED, IMAGE_URL_MISMATCH})

CATALOG_MATCH = "match"
CATALOG_MISMATCH = "mismatch"
CATALOG_UNKNOWN = "unknown"
SHARED_LEGITIMATE = "legitimate_upstream_reuse"
SHARED_SUSPECT = "suspect_upstream_mismatch"
SHARED_UNKNOWN = "unknown"

INTEGRITY_OK = "PASS"
VERDICT_ALERT = "ALERT"


def is_suspect_image_warning(code: Any) -> bool:
    """True when the warning code means the image itself is untrustworthy."""

    return str(code or "").strip().upper() in SUSPECT_IMAGE_WARNING_CODES


def normalize_image_key(value: Any) -> str:
    """Return a comparison key for one image URL (scheme/host lowercased)."""

    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = f"https:{text}"
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return text
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def shared_image_groups(
    items: Sequence[Mapping[str, Any]] | Sequence[Any],
    *,
    url_fields: Sequence[str] = ("image_url", "imageUrl", "main_image", "mainImage"),
    id_fields: Sequence[str] = ("product_id", "productId"),
    min_products: int = 2,
) -> dict[str, list[str]]:
    """Group the products that share one image URL.

    Only groups reaching ``min_products`` distinct product ids are returned, so
    a legitimately reused catalog image shows up exactly like a polluted one --
    the caller distinguishes them through :func:`classify_shared_group`.
    """

    buckets: dict[str, list[str]] = {}
    for item in items:
        product_id = _first_value(item, id_fields)
        if not product_id:
            continue
        key = normalize_image_key(_first_value(item, url_fields))
        if not key:
            continue
        ids = buckets.setdefault(key, [])
        product_id = str(product_id)
        if product_id not in ids:
            ids.append(product_id)
    return {key: ids for key, ids in buckets.items() if len(ids) >= min_products}


def classify_shared_group(
    product_ids: Sequence[str],
    catalog_checks: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]] | None,
) -> tuple[str, int]:
    """Classify a shared-image group from per-product catalog comparisons.

    ``catalog_checks`` maps a product id to ``{"catalog_url": ..., "url_match":
    bool, "correlation": float | None}``.  When the catalog returns the same
    image for every sampled product of the group the upstream reuse is real
    (Harbor does that for e.g. two colourways of one shoe); when the catalog
    disagrees for any of them the shared image is suspect.
    """

    checks = _check_index(catalog_checks)
    sampled = 0
    mismatched = 0
    for product_id in product_ids:
        check = checks.get(str(product_id))
        if not isinstance(check, Mapping):
            continue
        sampled += 1
        if not _check_matches(check):
            mismatched += 1
    if not sampled:
        return SHARED_UNKNOWN, 0
    if mismatched:
        return SHARED_SUSPECT, sampled
    return SHARED_LEGITIMATE, sampled


def image_health_report(
    observations: Sequence[Mapping[str, Any]],
    *,
    catalog_checks: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]] | None = None,
    asset_mismatches: Sequence[Mapping[str, Any]] | None = None,
    shared_alert_min_products: int = 3,
    missing_alert_ratio: float = 0.05,
    mismatch_alert_count: int = 1,
) -> dict[str, Any]:
    """Evaluate one audit scope and return findings plus an overall verdict."""

    product_count = 0
    missing = 0
    for item in observations:
        if not isinstance(item, Mapping):
            continue
        product_count += 1
        if not normalize_image_key(_first_value(item, ("image_url", "imageUrl", "main_image", "mainImage"))):
            missing += 1
    groups = shared_image_groups(observations)
    checks = _check_index(catalog_checks)
    findings: list[dict[str, Any]] = []
    shared_product_count = 0
    for key, product_ids in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        status, sampled = classify_shared_group(product_ids, checks)
        shared_product_count += len(product_ids)
        findings.append(
            {
                "code": IMAGE_URL_SHARED,
                "image_key": key,
                "product_count": len(product_ids),
                "product_ids": list(product_ids),
                "catalog_status": status,
                "catalog_sampled": sampled,
            }
        )
    missing_ratio = round(missing / product_count, 6) if product_count else 0.0
    mismatch_checks = 0
    for check in checks.values():
        if isinstance(check, Mapping) and not _check_matches(check):
            mismatch_checks += 1
    alerts: list[dict[str, Any]] = []
    for finding in findings:
        if finding["product_count"] < shared_alert_min_products:
            continue
        if finding["catalog_status"] == SHARED_LEGITIMATE:
            continue
        alerts.append(
            {
                "severity": VERDICT_ALERT,
                "code": IMAGE_URL_SHARED,
                "message": (
                    f"{finding['product_count']} 个商品共用同一张图 "
                    f"({finding['image_key']})，目录复核={finding['catalog_status']}"
                ),
                "product_ids": finding["product_ids"][:20],
            }
        )
    if product_count and missing_ratio > missing_alert_ratio:
        alerts.append(
            {
                "severity": VERDICT_ALERT,
                "code": IMAGE_URL_MISSING,
                "message": f"{missing}/{product_count} 个商品没有图片地址（比例 {missing_ratio:.2%}）",
                "product_ids": [],
            }
        )
    if mismatch_checks >= mismatch_alert_count:
        alerts.append(
            {
                "severity": VERDICT_ALERT,
                "code": "IMAGE_CATALOG_MISMATCH",
                "message": f"{mismatch_checks} 个商品的图片与商品库不一致",
                "product_ids": [
                    str(check.get("product_id"))
                    for check in checks.values()
                    if isinstance(check, Mapping) and not _check_matches(check) and check.get("product_id")
                ][:20],
            }
        )
    foreign_assets = [dict(item) for item in (asset_mismatches or []) if isinstance(item, Mapping)]
    if foreign_assets:
        alerts.append(
            {
                "severity": VERDICT_ALERT,
                "code": IMAGE_STORAGE_KEY_FOREIGN,
                "message": f"{len(foreign_assets)} 行资产记录的缓存对象与自身图片地址不一致",
                "product_ids": [str(item.get("product_id") or "") for item in foreign_assets][:20],
            }
        )
    return {
        "product_count": product_count,
        "missing_image_count": missing,
        "missing_image_ratio": missing_ratio,
        "shared_group_count": len(findings),
        "shared_product_count": shared_product_count,
        "shared_groups": findings,
        "catalog_checked_count": len(checks),
        "catalog_mismatch_count": mismatch_checks,
        "asset_mismatch_count": len(foreign_assets),
        "asset_mismatches": foreign_assets[:20],
        "alerts": alerts,
        "verdict": VERDICT_ALERT if alerts else INTEGRITY_OK,
    }


def image_storage_key(source_url: Any, *, prefix: str) -> str:
    """Return the cache key the pipeline derives from one image URL."""

    key = normalize_image_key(source_url)
    if not key:
        return ""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{str(prefix).rstrip('/')}/{digest}.jpg"


def asset_records_mismatch(
    rows: Sequence[Mapping[str, Any]],
    *,
    default_prefix: str = "query-governance/images/prod/v1",
) -> list[dict[str, Any]]:
    """Find asset rows whose cache pointer disagrees with their own source URL.

    A polluted row that is internally consistent (source_url, storage_key and
    cached_url all describing the *same wrong* photo) is invisible here -- that
    case is caught by :func:`shared_image_groups`.  What this detects is the
    secondary damage: a row whose ``cached_url`` points at a foreign object, i.e.
    another product's cached photo.  In the Harbor incident that was 46 extra rows.
    """

    findings: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        storage_key = str(row.get("storage_key") or "").strip()
        cached_url = str(row.get("cached_url") or "").strip()
        product_id = str(row.get("product_id") or row.get("productId") or "").strip()
        prefix = storage_key.rsplit("/", 1)[0] if "/" in storage_key else default_prefix
        expected = image_storage_key(row.get("source_url") or row.get("sourceUrl"), prefix=prefix)
        issues: list[str] = []
        if storage_key and expected and normalize_image_key(storage_key) != normalize_image_key(expected):
            issues.append("storage_key_not_derived_from_source_url")
        if cached_url and storage_key and _basename(cached_url) != _basename(storage_key):
            issues.append("cached_url_points_elsewhere")
        if issues:
            findings.append(
                {
                    "product_id": product_id,
                    "source_url": str(row.get("source_url") or ""),
                    "storage_key": storage_key,
                    "cached_url": cached_url,
                    "issues": issues,
                }
            )
    return findings


def render_alert_lines(report: Mapping[str, Any]) -> list[str]:
    """Render one report as greppable monitor lines."""

    lines = [
        "IMAGE-INTEGRITY "
        f"products={report.get('product_count', 0)} "
        f"missing={report.get('missing_image_count', 0)} "
        f"shared_groups={report.get('shared_group_count', 0)} "
        f"shared_products={report.get('shared_product_count', 0)} "
        f"catalog_checked={report.get('catalog_checked_count', 0)} "
        f"catalog_mismatch={report.get('catalog_mismatch_count', 0)} "
        f"verdict={report.get('verdict', INTEGRITY_OK)}"
    ]
    for alert in report.get("alerts") or []:
        if isinstance(alert, Mapping):
            lines.append(f"[{alert.get('severity', VERDICT_ALERT)}] {alert.get('code')}: {alert.get('message')}")
    for finding in report.get("shared_groups") or []:
        if not isinstance(finding, Mapping):
            continue
        lines.append(
            f"[INFO] {IMAGE_URL_SHARED}: {finding.get('product_count')} 个商品共用 {finding.get('image_key')} "
            f"(目录复核={finding.get('catalog_status')}, 抽样={finding.get('catalog_sampled')})"
        )
    return lines


def _basename(value: Any) -> str:
    text = str(value or "").strip()
    return text.rsplit("/", 1)[-1].split("?", 1)[0]


def _first_value(item: Any, fields: Sequence[str]) -> Any:
    if isinstance(item, Mapping):
        for field in fields:
            value = item.get(field)
            if value:
                return value
        return None
    for field in fields:
        value = getattr(item, field, None)
        if value:
            return value
    return None


def _check_index(catalog_checks: Any) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    if isinstance(catalog_checks, Mapping):
        for product_id, check in catalog_checks.items():
            if isinstance(check, Mapping):
                merged = dict(check)
                merged.setdefault("product_id", str(product_id))
                index[str(product_id)] = merged
    elif isinstance(catalog_checks, Sequence) and not isinstance(catalog_checks, (str, bytes)):
        for check in catalog_checks:
            if isinstance(check, Mapping) and check.get("product_id"):
                index[str(check["product_id"])] = check
    return index


def _check_matches(check: Mapping[str, Any]) -> bool:
    if "url_match" in check:
        return bool(check.get("url_match"))
    if "urlMatch" in check:
        return bool(check.get("urlMatch"))
    correlation = check.get("correlation")
    if isinstance(correlation, (int, float)):
        return float(correlation) >= 0.98
    catalog_url = normalize_image_key(check.get("catalog_url") or check.get("catalogUrl"))
    recorded_url = normalize_image_key(check.get("recorded_url") or check.get("recordedUrl"))
    if not catalog_url or not recorded_url:
        return True
    return catalog_url == recorded_url