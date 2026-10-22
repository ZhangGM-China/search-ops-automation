#!/usr/bin/env python3
"""Harbor 商品图完整性巡检（只读，可用于定时任务）。

背景：2026-09-29 ~ 2026-10-12 上游搜索索引把同一条错误 mainImage（牛仔短裤图）
写进了 Harbor 商品文档，本服务如实透传，导致 4000+ 商品被拿错图判定相关性。修复
已完成，本脚本负责“以后能第一时间发现”：

  1. 共享图检测：同一张图被 >= N 个不同商品使用（快照 / 治理卡片 / 资产表）。
  2. 与商品库对图：抽样调用 /admin-api/search/product/list，比对目录图是否一致
     （目录图一致的同图共用是上游合法复用，例如同一双鞋的两个颜色；不一致才是错图）。
  3. 资产行自洽性：cached_url 指向与自身 source_url 不符的缓存对象。

只读：脚本不写任何业务表（只在 --json 指定的本地目录落盘报告）。
退出码：默认 0；带 --fail-on-alert 且判定为 ALERT 时返回 1，便于定时任务告警。

用法
----
  # 近 3 天的生产巡检（联网读搜索服务 + 只读查库）
  venv/bin/python scripts/ops/audit_harbor_image_integrity.py --env prod --brands harbor --days 3

  # 离线回放：用修复前的备份文件复算，验证巡检能抓到原事故
  venv/bin/python scripts/ops/audit_harbor_image_integrity.py \\
      --replay-backup .worktrees/harbor-image-repair/var/harbor-image-repair/p1/backup-20261013T191316-11213.json

  # 定时任务：只查库+对图，告警时退出 1
  ... --days 2 --sample-catalog 8 --fail-on-alert --webhook-url https://example.com/hook
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
for entry in (str(REPO_ROOT), str(SCRIPT_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from src.query_governance.image_integrity import (  # noqa: E402
    IMAGE_STORAGE_KEY_FOREIGN,
    asset_records_mismatch,
    image_health_report,
    normalize_image_key,
    render_alert_lines,
    shared_image_groups,
)
from repair_harbor_polluted_images import Conn, SearchClient, _load_env, resolve_uri  # noqa: E402

TASK_TABLE = "query_governance_task"
ASSET_TABLE = "query_governance_image_asset"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _product_rows(snapshot: Any) -> list[Mapping[str, Any]]:
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            return []
    if not isinstance(snapshot, Mapping):
        return []
    products = snapshot.get("products") or snapshot.get("items") or []
    return [item for item in products if isinstance(item, Mapping)]


def collect_db_scope(
    conn: Conn,
    brands: Sequence[str],
    *,
    days: int,
    task_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Read recent S0 snapshots plus the asset table for the given brands."""

    since = (date.today() - timedelta(days=max(1, days))).isoformat()
    marks = ",".join(["%s"] * len(brands))
    tasks = conn.query(
        f"SELECT id, brand, data_date, query_text, status, s0_snapshot FROM {TASK_TABLE}"
        f" WHERE brand IN ({marks}) AND data_date >= %s ORDER BY id DESC LIMIT %s",
        (*brands, since, task_limit),
    )
    observations: list[dict[str, Any]] = []
    for task in tasks:
        for product in _product_rows(task.get("s0_snapshot")):
            observations.append(
                {
                    "source": "snapshot",
                    "brand": task.get("brand"),
                    "task_id": task.get("id"),
                    "data_date": str(task.get("data_date") or ""),
                    "query_text": task.get("query_text"),
                    "product_id": product.get("product_id") or product.get("productId"),
                    "image_url": product.get("image_url") or product.get("imageUrl"),
                }
            )
    asset_rows = conn.query(
        f"SELECT id, brand, product_id, source_url, storage_key, cached_url, status FROM {ASSET_TABLE}"
        f" WHERE brand IN ({marks})",
        tuple(brands),
    )
    scope = {
        "mode": "db",
        "brands": list(brands),
        "since": since,
        "tasks_scanned": len(tasks),
        "observations": len(observations),
        "asset_rows_scanned": len(asset_rows),
    }
    return observations, asset_rows, scope


def collect_replay_scope(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Replay a repair pre-flight backup (pre-repair values) through the same checks."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    observations: list[dict[str, Any]] = []
    asset_rows: list[dict[str, Any]] = []
    for snapshot in payload.get("snapshots") or []:
        if not isinstance(snapshot, Mapping):
            continue
        for product in _product_rows(snapshot.get("s0_snapshot")):
            observations.append(
                {
                    "source": "snapshot_backup",
                    "task_id": snapshot.get("id"),
                    "product_id": product.get("product_id") or product.get("productId"),
                    "image_url": product.get("image_url") or product.get("imageUrl"),
                }
            )
    for plan in payload.get("plans") or []:
        if not isinstance(plan, Mapping):
            continue
        request = plan.get("request_json")
        if isinstance(request, str):
            try:
                request = json.loads(request)
            except (TypeError, ValueError):
                continue
        if not isinstance(request, Mapping):
            continue
        for product_id, image_url in (request.get("product_images") or {}).items():
            observations.append(
                {
                    "source": "plan_backup",
                    "plan_id": plan.get("id"),
                    "product_id": product_id,
                    "image_url": image_url,
                }
            )
    for row in payload.get("assets") or []:
        if isinstance(row, Mapping):
            asset_rows.append(
                {
                    "product_id": row.get("product_id"),
                    "source_url": row.get("source_url"),
                    "storage_key": row.get("storage_key"),
                    "cached_url": row.get("cached_url"),
                }
            )
    scope = {
        "mode": "replay",
        "backup": str(path),
        "created_at": payload.get("created_at"),
        "observations": len(observations),
        "asset_rows_scanned": len(asset_rows),
    }
    return observations, asset_rows, scope


def _download(url: str, *, timeout: float = 20.0) -> bytes | None:
    import requests

    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.harbor.example.test/"},
        )
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        return None
    return None


def _correlation(left: bytes, right: bytes) -> float | None:
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:
        return None
    try:
        import numpy as np

        images = []
        for payload in (left, right):
            image = Image.open(BytesIO(payload)).convert("L").resize((64, 64))
            images.append(np.asarray(image, dtype="float64").ravel())
        a, b = images
        a = a - a.mean()
        b = b - b.mean()
        denominator = float((a @ a) ** 0.5 * (b @ b) ** 0.5)
        if not denominator:
            return None
        return round(float(a @ b) / denominator, 6)
    except Exception:
        return None


def catalog_checks(
    search: SearchClient,
    observations: Sequence[Mapping[str, Any]],
    *,
    sample: int,
    groups: Mapping[str, list[str]],
    group_sample: int,
    check_pixels: bool,
) -> dict[str, dict[str, Any]]:
    """Compare recorded images against the product catalog (product/list)."""

    wanted: list[str] = []
    for product_ids in groups.values():
        for product_id in product_ids[: max(1, group_sample)]:
            if product_id not in wanted:
                wanted.append(product_id)
    recorded: dict[str, str] = {}
    for item in observations:
        product_id = str(item.get("product_id") or "").strip()
        url = str(item.get("image_url") or "").strip()
        if product_id and url and product_id not in recorded:
            recorded[product_id] = url
    for product_id in recorded:
        if len(wanted) >= sample:
            break
        if product_id not in wanted:
            wanted.append(product_id)

    checks: dict[str, dict[str, Any]] = {}
    for product_id in wanted[:sample]:
        catalog_url = search.product_image(product_id)
        if not catalog_url:
            continue
        check: dict[str, Any] = {
            "product_id": product_id,
            "recorded_url": recorded.get(product_id, ""),
            "catalog_url": catalog_url,
            "url_match": normalize_image_key(recorded.get(product_id, "")) == normalize_image_key(catalog_url),
        }
        if check_pixels and check["recorded_url"]:
            left = _download(check["recorded_url"])
            right = _download(catalog_url)
            if left and right:
                check["correlation"] = _correlation(left, right)
        checks[product_id] = check
    return checks


def _notify(url: str, report: Mapping[str, Any]) -> None:
    import requests

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": "**Harbor 商品图完整性巡检**\n" + "\n".join(render_alert_lines(report))
        },
        "report": dict(report),
    }
    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            log(f"告警已推送：http={resp.status_code}")
            return
        except Exception as exc:  # noqa: BLE001
            log(f"告警推送失败（第 {attempt + 1} 次）：{exc}")
            time.sleep(1.5)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=["test", "prod"], default=None, help="库环境，默认取 .env 的 ENV")
    parser.add_argument("--db-uri", default=None, help="覆盖数据库连接串")
    parser.add_argument("--brands", default="harbor", help="逗号分隔的品牌，默认 harbor")
    parser.add_argument("--days", type=int, default=3, help="扫描最近多少天的任务快照（默认 3）")
    parser.add_argument("--task-limit", type=int, default=200, help="最多扫描多少个任务（默认 200）")
    parser.add_argument("--sample-catalog", type=int, default=8, help="抽样对图的商品数（默认 8）")
    parser.add_argument("--group-sample", type=int, default=3, help="每个共享图分组抽样对图的商品数（默认 3）")
    parser.add_argument("--shared-alert-min", type=int, default=3, help="共享图告警阈值：同图商品数下限（默认 3）")
    parser.add_argument("--missing-alert-ratio", type=float, default=0.05, help="缺图比例告警阈值（默认 0.05）")
    parser.add_argument("--replay-backup", default=None, help="离线回放备份 JSON（不查库、默认不联网）")
    parser.add_argument("--check-pixels", action="store_true", help="抽样下载图片做像素相关性复核")
    parser.add_argument("--no-catalog", action="store_true", help="跳过商品库对图（纯离线）")
    parser.add_argument("--json", dest="json_path", default=None, help="报告输出路径")
    parser.add_argument("--fail-on-alert", action="store_true", help="判定 ALERT 时退出码为 1")
    parser.add_argument("--webhook-url", default=None, help="可选告警推送地址")
    args = parser.parse_args(argv)

    _load_env()
    env = args.env or (str(__import__("os").getenv("ENV") or "test").strip() or "test")
    brands = [item.strip() for item in str(args.brands).split(",") if item.strip()]

    if args.replay_backup:
        observations, asset_rows, scope = collect_replay_scope(Path(args.replay_backup))
    else:
        conn = Conn(resolve_uri(env, args.db_uri))
        try:
            observations, asset_rows, scope = collect_db_scope(
                conn, brands, days=args.days, task_limit=args.task_limit
            )
        finally:
            conn.close()
    scope["env"] = env

    groups = shared_image_groups(observations)
    checks: dict[str, dict[str, Any]] = {}
    if not args.no_catalog and not args.replay_backup:
        search = SearchClient(brands[0], env)
        checks = catalog_checks(
            search,
            observations,
            sample=args.sample_catalog,
            groups=groups,
            group_sample=args.group_sample,
            check_pixels=args.check_pixels,
        )
    elif not args.no_catalog and args.replay_backup:
        log("回放模式：默认跳过商品库对图（如需联网复核，加 --check-pixels 需自行传入 --brands 与索引环境）")

    report = image_health_report(
        observations,
        catalog_checks=checks,
        asset_mismatches=asset_records_mismatch(asset_rows),
        shared_alert_min_products=args.shared_alert_min,
        missing_alert_ratio=args.missing_alert_ratio,
    )
    report["scope"] = scope
    report["shared_groups_by_source"] = _group_sources(observations)
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    for line in render_alert_lines(report):
        print(line)
    if args.json_path:
        target = Path(args.json_path)
    else:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        target = REPO_ROOT / "var" / "harbor-image-integrity" / f"audit-{env}-{stamp}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log(f"报告已写入 {target}")

    if args.webhook_url and report["verdict"] != "PASS":
        _notify(args.webhook_url, report)
    if args.fail_on_alert and report["verdict"] != "PASS":
        return 1
    return 0


def _group_sources(observations: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    buckets: defaultdict[str, set[str]] = defaultdict(set)
    for item in observations:
        url = normalize_image_key(item.get("image_url"))
        if not url:
            continue
        buckets[url].add(str(item.get("product_id")))
    for url, product_ids in buckets.items():
        if len(product_ids) >= 2:
            sources = {
                str(item.get("source") or "")
                for item in observations
                if normalize_image_key(item.get("image_url")) == url
            }
            counts[",".join(sorted(value for value in sources if value))] += 1
    return dict(counts)


if __name__ == "__main__":
    raise SystemExit(main())