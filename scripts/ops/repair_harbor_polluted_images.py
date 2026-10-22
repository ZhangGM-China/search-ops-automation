#!/usr/bin/env python3
"""Harbor 污染图片（错图 URL）全量修复工具。

背景：2026-09-29 ~ 2026-10-12 上游搜索索引给 Harbor 商品文档写入了同一条错误
mainImage（.../f/a/fae7537d5904011483cc1bd867f13b0432a5a1c1_xxl-1.jpg，实为
牛仔短裤图），本服务如实透传并落库到三处：

  * query_governance_task.s0_snapshot              （召回快照的 image_url）
  * query_governance_action_plan.request_json      （治理卡片 product_images）
  * query_governance_image_asset                   （+ 对应 COS 对象）

子命令
------
  inventory   只读扫描生产库，产出受影响清单与 dry-run 报告
  mapping     构建正确图片映射（数仓 -> 当日索引 mixed -> product/list 兜底）
  apply       按清单替换（默认 dry-run；--execute 才写库，写前自动备份）
  verify      复核错图残留、共享图分组与映射覆盖率

安全默认：ENV=test 时只动测试库；写生产库必须同时给出
`--env prod --execute --allow-prod-write`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse as urlparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

BAD_MARKER = "fae7537d5904011483cc1bd867f13b0432a5a1c1"
BAD_URL = (
    "https://asset.harbor.example.test/media/catalog/product/f/a/"
    f"{BAD_MARKER}_xxl-1.jpg"
)
TASK_TABLE = "query_governance_task"
PLAN_TABLE = "query_governance_action_plan"
ASSET_TABLE = "query_governance_image_asset"
DW_TABLE = "industry_knowledge_base.dws_brand_query_product_funnel_daily"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# 连接层
# --------------------------------------------------------------------------- #
class Conn:
    """极薄的 pymysql / sqlite3 适配层（占位符统一用 %s）。"""

    def __init__(self, uri: str) -> None:
        self.kind = "sqlite" if uri.startswith("sqlite") else "mysql"
        if self.kind == "sqlite":
            path = uri.split(":///", 1)[-1] if ":///" in uri else uri.split("://", 1)[-1]
            self.raw = sqlite3.connect(path)
            self.raw.row_factory = sqlite3.Row
        else:
            import pymysql

            parsed = urlparse.urlparse(uri)
            self.raw = pymysql.connect(
                host=parsed.hostname,
                port=parsed.port or 3306,
                user=urlparse.unquote(parsed.username or ""),
                password=urlparse.unquote(parsed.password or ""),
                database=parsed.path.lstrip("/"),
                charset="utf8mb4",
                connect_timeout=15,
                read_timeout=300,
                write_timeout=300,
                autocommit=False,
            )

    def _sql(self, sql: str) -> str:
        if self.kind == "mysql":
            return sql
        return sql.replace("%s", "?").replace("NOW()", "CURRENT_TIMESTAMP")

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self.raw.cursor()
        try:
            cur.execute(self._sql(sql), tuple(params))
            cols = [d[0] for d in (cur.description or [])]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self.raw.cursor()
        try:
            return cur.execute(self._sql(sql), tuple(params))
        finally:
            cur.close()

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def resolve_uri(env: str, override: str | None = None) -> str:
    if override:
        return override
    key = "AUTO_DB_URI_PROD" if env == "prod" else "AUTO_DB_URI_TEST"
    value = str(os.getenv(key) or "").strip()
    if not value:
        raise SystemExit(f"{key} 未配置，无法连接 {env} 库")
    return value


# --------------------------------------------------------------------------- #
# 搜索服务（图片取值的权威来源）
# --------------------------------------------------------------------------- #
class SearchClient:
    def __init__(self, brand: str, env: str, timeout: float = 20.0, retries: int = 4) -> None:
        from src.integrations.operational_service import OperationalService

        config = OperationalService.FIXED_BRAND_CONFIGS[brand]
        self.api_alias = config["api_alias"]
        self.base = config["base_urls"][env]
        key = str(os.getenv(config["api_key_env"]) or "").strip()
        if not key:
            raise SystemExit(f"{config['api_key_env']} 未配置，无法读取 {brand} 商品数据")
        self.headers = {
            "Authorization": f"Bearer {key}",
            "tenant-id": str(config["tenantId"]),
            "Content-Type": "application/json",
        }
        self.timeout = timeout
        self.retries = retries

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        import requests

        delay = 0.6
        for attempt in range(self.retries):
            try:
                resp = requests.post(
                    f"{self.base}{path}", headers=self.headers, json=dict(payload), timeout=self.timeout
                )
                if resp.status_code == 200 and resp.text.strip().startswith("{"):
                    return resp.json()
            except Exception:
                pass
            if attempt + 1 < self.retries:
                time.sleep(delay)
                delay *= 2
        return None

    def mixed_page(self, query: str, page: int, page_size: int = 100) -> tuple[list[dict[str, Any]], int | None]:
        body = self._post(
            "/api/search/mixed",
            {"brand": self.api_alias, "keyword": query, "pageSize": page_size, "pageNum": page},
        )
        if not body:
            return [], None
        data = body.get("data") or {}
        return list(data.get("rows") or []), data.get("total")

    def mixed_images(self, query: str, page_size: int = 100, max_pages: int = 12) -> dict[str, str]:
        result: dict[str, str] = {}
        total = None
        for page in range(1, max_pages + 1):
            rows, total = self.mixed_page(query, page, page_size)
            if not rows:
                break
            for row in rows:
                pid = str(row.get("productId") or "").strip()
                image = str(row.get("mainImage") or "").strip()
                if pid and image and pid not in result:
                    result[pid] = image
            if total is not None and page * page_size >= int(total):
                break
        return result

    def product_image(self, product_id: str) -> str | None:
        body = self._post(
            "/admin-api/search/product/list", {"spu": product_id, "pageNum": 1, "pageSize": 20}
        )
        if not body:
            return None
        rows = (body.get("data") or {}).get("list") or []
        if not rows:
            return None
        first = rows[0]
        for field in ("imageUrl", "mainImage", "image_url"):
            value = str(first.get(field) or "").strip()
            if value:
                return value
        return None

    def product_name(self, product_id: str) -> str | None:
        body = self._post(
            "/admin-api/search/product/list", {"spu": product_id, "pageNum": 1, "pageSize": 20}
        )
        rows = (body.get("data") or {}).get("list") or []
        if not rows:
            return None
        for field in ("name", "productName", "title"):
            value = str(rows[0].get(field) or "").strip()
            if value:
                return value
        return None


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def is_usable(url: str | None) -> bool:
    value = str(url or "").strip()
    if not value.lower().startswith(("http://", "https://")):
        return False
    return BAD_MARKER not in value


def shared_urls(mapping: Mapping[str, str]) -> dict[str, list[str]]:
    """被 >=2 个受影响商品映射到同一张图的 URL（疑似新污染或共用图）。"""
    groups: dict[str, list[str]] = defaultdict(list)
    for pid, url in mapping.items():
        groups[url].append(pid)
    return {url: sorted(pids) for url, pids in groups.items() if len(pids) > 1}


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #
def cmd_inventory(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = Conn(resolve_uri(args.env, args.db_uri))
    marker = args.marker
    summary: list[str] = [f"env={args.env} brand={args.brand} marker={marker}"]

    assets = conn.query(
        f"SELECT id, brand, product_id, spu_id, source_url, storage_key, cached_url, status,"
        f" content_type, width, height, image_sha256, created_at, updated_at FROM {ASSET_TABLE}"
        f" WHERE brand=%s AND source_url LIKE %s",
        (args.brand, f"%{marker}%"),
    )
    # 反向污染：错图对象被写进 source_url 正常的资产行（同一 cached_url 服务多个不同 source_url）
    poisoned = [
        row["cached_url"]
        for row in conn.query(
            f"SELECT cached_url, COUNT(DISTINCT source_url) AS sources FROM {ASSET_TABLE}"
            f" WHERE brand=%s AND cached_url IS NOT NULL GROUP BY cached_url HAVING sources > 1",
            (args.brand,),
        )
    ]
    seen_ids = {row["id"] for row in assets}
    extra_rows: list[dict[str, Any]] = []
    if poisoned:
        holders = ",".join(["%s"] * len(poisoned))
        extra_rows = [
            row
            for row in conn.query(
                f"SELECT id, brand, product_id, spu_id, source_url, storage_key, cached_url, status,"
                f" content_type, width, height, image_sha256, created_at, updated_at FROM {ASSET_TABLE}"
                f" WHERE brand=%s AND cached_url IN ({holders})",
                (args.brand, *poisoned),
            )
            if row["id"] not in seen_ids
        ]
        assets = assets + extra_rows
    key_mismatch = conn.query(
        f"SELECT COUNT(*) AS n FROM {ASSET_TABLE} WHERE brand=%s AND cached_url IS NOT NULL"
        f" AND storage_key IS NOT NULL"
        f" AND SUBSTRING_INDEX(cached_url,'/',-1) <> SUBSTRING_INDEX(storage_key,'/',-1)"
        if conn.kind == "mysql"
        else f"SELECT COUNT(*) AS n FROM {ASSET_TABLE} WHERE brand=%s AND cached_url IS NOT NULL"
        f" AND storage_key IS NOT NULL AND cached_url <> storage_key",
        (args.brand,),
    )[0]["n"]
    log(f"受影响资产行：{len(assets)}（错图 URL {len(assets) - len(extra_rows)} + 复用错图对象 {len(extra_rows)}）")
    _dump(out_dir / "assets.json", assets)
    summary.append(f"assets={len(assets)} asset_reused_objects={len(extra_rows)} key_mismatch={key_mismatch}")

    shared = [
        row
        for row in conn.query(
            f"SELECT cached_url, COUNT(DISTINCT product_id) AS products, COUNT(*) AS rows_,"
            f" COUNT(DISTINCT source_url) AS sources, MIN(created_at) AS first_seen,"
            f" MAX(updated_at) AS last_seen FROM {ASSET_TABLE}"
            f" WHERE brand=%s GROUP BY cached_url HAVING products > 1 OR sources > 1"
            f" ORDER BY products DESC",
            (args.brand,),
        )
    ]
    log(f"共享图分组：{len(shared)}（其中 {sum(1 for row in shared if row['sources'] > 1)} 组为错图对象复用）")
    _dump(out_dir / "shared_groups.json", shared)
    summary.append(f"shared_groups={len(shared)}")

    tasks = conn.query(
        f"SELECT id, brand, data_date, query_text, status, s0_snapshot FROM {TASK_TABLE}"
        f" WHERE brand=%s AND s0_snapshot LIKE %s ORDER BY id",
        (args.brand, f"%{marker}%"),
    )
    snapshots: list[dict[str, Any]] = []
    s0_products: set[str] = set()
    for task in tasks:
        try:
            snapshot = json.loads(task["s0_snapshot"])
        except Exception:
            continue
        bad_items = [
            {
                "product_id": str(item.get("product_id")),
                "product_name": item.get("product_name"),
                "rank": item.get("rank"),
                "image_url": item.get("image_url"),
            }
            for item in (snapshot.get("products") or [])
            if marker in str(item.get("image_url") or "")
        ]
        if not bad_items:
            continue
        s0_products.update(item["product_id"] for item in bad_items)
        snapshots.append(
            {
                "task_id": task["id"],
                "data_date": task["data_date"],
                "query_text": task["query_text"],
                "captured_at": snapshot.get("captured_at"),
                "result_count": snapshot.get("result_count"),
                "bad_items": bad_items,
            }
        )
    log(f"受影响任务：{len(snapshots)}（覆盖 {len(s0_products)} 个商品）")
    _dump(out_dir / "snapshots.json", snapshots)
    summary.append(f"tasks={len(snapshots)} s0_products={len(s0_products)}")

    plans_raw = conn.query(
        f"SELECT id, task_id, brand, query_text, action_type, status, request_json, response_json"
        f" FROM {PLAN_TABLE} WHERE brand=%s AND (request_json LIKE %s OR response_json LIKE %s)"
        f" ORDER BY id",
        (args.brand, f"%{marker}%", f"%{marker}%"),
    )
    plans = []
    plan_products: set[str] = set()
    for plan in plans_raw:
        request = _loads(plan.get("request_json"))
        images = request.get("product_images") or {}
        bad_map = {str(pid): url for pid, url in images.items() if marker in str(url or "")}
        extra = _bad_urls_in_tree(plan.get("response_json"), marker)
        names = request.get("product_names") or {}
        reasons = request.get("reasons") or {}
        plan_products.update(bad_map)
        plans.append(
            {
                "plan_id": plan["id"],
                "task_id": plan["task_id"],
                "query_text": plan["query_text"],
                "action_type": plan["action_type"],
                "status": plan["status"],
                "product_ids": [str(pid) for pid in (request.get("product_ids") or [])],
                "bad_product_images": bad_map,
                "bad_elsewhere": extra,
                "product_names": {str(k): v for k, v in names.items()},
                "reasons": {str(k): v for k, v in reasons.items()},
            }
        )
    log(f"受影响治理计划：{len(plans)}（覆盖 {len(plan_products)} 个商品）")
    _dump(out_dir / "plans.json", plans)
    summary.append(f"plans={len(plans)} plan_products={len(plan_products)}")

    products = sorted(set(str(row["product_id"]) for row in assets) | s0_products | plan_products)
    _dump(out_dir / "products.json", products)
    summary.append(f"affected_products={len(products)}")

    inventory = {
        "env": args.env,
        "brand": args.brand,
        "marker": marker,
        "bad_url": BAD_URL,
        "assets": assets,
        "shared_groups": shared,
        "snapshots": snapshots,
        "plans": plans,
        "products": products,
    }
    _dump(out_dir / "inventory.json", inventory)
    (out_dir / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    log(" | ".join(summary))
    log(f"清单目录：{out_dir}")
    conn.close()
    return 0


def _bad_urls_in_tree(payload: Any, marker: str) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and marker in node:
            found.append(node)

    walk(_loads(payload) if isinstance(payload, str) else payload)
    return sorted(set(found))


def _loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else {}
    except Exception:
        return {}


def _default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_default), encoding="utf-8")


# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #
def cmd_mapping(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    inventory = json.loads((directory / "inventory.json").read_text(encoding="utf-8"))
    products: list[str] = inventory["products"]
    candidate: dict[str, dict[str, Any]] = {}

    def offer(pid: str, url: str | None, source: str, tier: int) -> None:
        if not is_usable(url):
            return
        current = candidate.get(pid)
        if current is None or tier < current["tier"]:
            candidate[pid] = {"image_url": str(url).strip(), "source": source, "tier": tier}

    # ① 品牌数仓：一次查询覆盖大部分商品，且与驱动链路一致
    if not args.skip_dw:
        dw = _dw_images(args.dw_brand)
        log(f"数仓映射命中：{len(dw)} 个 Harbor 商品")
        for pid in products:
            offer(pid, dw.get(pid), "dw_main_image_url", 1)

    # ② 当日索引：与线上召回、相关性检测用的字段完全一致
    if not args.skip_index:
        queries = sorted({str(item.get("query_text") or "") for item in inventory["snapshots"] if item.get("query_text")})
        if queries:
            client = SearchClient(args.brand, args.env, timeout=args.timeout)
            index_map: dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for query, images in zip(
                    queries, pool.map(lambda q: client.mixed_images(q, args.page_size), queries)
                ):
                    index_map.update(images)
                    if args.verbose:
                        log(f"  mixed[{query}] -> {len(images)} 个商品图")
            log(f"当日索引映射命中：{len(index_map)} 个商品（覆盖 {len(queries)} 个 Query）")
            for pid in products:
                offer(pid, index_map.get(pid), "index_main_image", 2)

    # ③ product/list 兜底：慢（~2.8s/次），只补齐缺口
    missing = [pid for pid in products if pid not in candidate]
    log(f"待 product/list 兜底的商品：{len(missing)}")
    if missing and not args.skip_product_list:
        client = SearchClient(args.brand, args.env, timeout=args.timeout)
        done = 0
        with ThreadPoolExecutor(max_workers=args.list_workers) as pool:
            for pid, url in zip(missing, pool.map(client.product_image, missing)):
                offer(pid, url, "product_list", 3)
                done += 1
                if args.verbose and done % 100 == 0:
                    log(f"  product/list {done}/{len(missing)}")

    mapping = {pid: {"image_url": item["image_url"], "source": item["source"]} for pid, item in candidate.items()}
    groups = shared_urls({pid: item["image_url"] for pid, item in mapping.items()})
    if groups:
        log(f"警告：{len(groups)} 组图片被多个受影响商品共用，逐商品用 product/list 复核")
        client = SearchClient(args.brand, args.env, timeout=args.timeout)
        still_shared: dict[str, list[str]] = {}
        for url, pids in groups.items():
            kept: list[str] = []
            rechecked: list[str] = []
            dropped: list[str] = []
            for pid in pids:
                catalog = None if args.skip_product_list else client.product_image(pid)
                if catalog == url:
                    kept.append(pid)
                elif is_usable(catalog):
                    mapping[pid] = {"image_url": str(catalog).strip(), "source": "product_list_recheck"}
                    rechecked.append(pid)
                else:
                    dropped.append(pid)
                    mapping.pop(pid, None)
            log(
                f"  {url}：共用 {len(pids)} 个商品 → 目录复核保留 {len(kept)}、"
                f"改用目录图 {len(rechecked)}、剔除 {len(dropped)}"
            )
            if dropped:
                still_shared[url] = dropped
        groups = still_shared

    unresolved = [pid for pid in products if pid not in mapping]
    _dump(directory / "mapping.json", mapping)
    _dump(directory / "unresolved.json", unresolved)
    _dump(directory / "shared_candidates.json", groups)
    by_source = Counter(item["source"] for item in mapping.values())
    log(f"映射完成：{len(mapping)}/{len(products)} 个商品有正确图（{dict(by_source)}），未解析 {len(unresolved)}")
    (directory / "mapping_summary.txt").write_text(
        json.dumps(
            {
                "affected": len(products),
                "mapped": len(mapping),
                "by_source": dict(by_source),
                "unresolved": len(unresolved),
                "shared_groups": {url: pids for url, pids in groups.items()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


def _dw_images(dw_brand: str) -> dict[str, str]:
    import pymysql

    host = os.getenv("RD_RIVIERA_HOST")
    if not host:
        log("RD_RIVIERA_HOST 未配置，跳过数仓映射")
        return {}
    conn = pymysql.connect(
        host=host,
        port=int(os.getenv("RD_RIVIERA_PORT") or 3306),
        user=os.getenv("RD_RIVIERA_USER"),
        password=os.getenv("RD_RIVIERA_PASS"),
        database=os.getenv("RD_RIVIERA_DB"),
        charset="utf8mb4",
        connect_timeout=15,
        read_timeout=300,
    )
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT product_id, MAX(main_image_url) FROM {DW_TABLE}"
            f" WHERE brand=%s AND main_image_url IS NOT NULL GROUP BY product_id",
            (dw_brand,),
        )
        return {str(pid): str(url).strip() for pid, url in cur.fetchall() if url}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def _chunks(values: Sequence[Any], size: int = 500):
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _preflight_backup(
    conn: Conn,
    directory: Path,
    inventory: Mapping[str, Any],
    tables: set[str],
    limit: int | None,
) -> Path:
    """在执行任何写入之前，把将被修改的行原值落盘，保证可回滚。"""

    def _cap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return rows[:limit] if limit else rows

    payload: dict[str, Any] = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "assets": [],
        "snapshots": [],
        "plans": [],
    }
    if "assets" in tables:
        cols = (
            "id", "brand", "product_id", "spu_id", "source_url", "source_url_hash", "storage_key",
            "cached_url", "status", "content_type", "width", "height", "image_sha256",
            "last_success_at", "last_failure_code", "last_failure_message", "created_at", "updated_at",
        )
        ids = [row["id"] for row in _cap(inventory["assets"])]
        for chunk in _chunks(ids):
            marks = ",".join(["%s"] * len(chunk))
            for row in conn.query(
                f"SELECT {', '.join(cols)} FROM {ASSET_TABLE} WHERE id IN ({marks})", tuple(chunk)
            ):
                payload["assets"].append({col: row.get(col) for col in cols})
    if "snapshots" in tables:
        ids = [row["task_id"] for row in _cap(inventory["snapshots"])]
        for chunk in _chunks(ids):
            marks = ",".join(["%s"] * len(chunk))
            for row in conn.query(
                f"SELECT id, s0_snapshot FROM {TASK_TABLE} WHERE id IN ({marks})", tuple(chunk)
            ):
                payload["snapshots"].append({"id": row["id"], "s0_snapshot": row.get("s0_snapshot")})
    if "plans" in tables:
        ids = [row["plan_id"] for row in _cap(inventory["plans"])]
        for chunk in _chunks(ids):
            marks = ",".join(["%s"] * len(chunk))
            for row in conn.query(
                f"SELECT id, request_json, response_json FROM {PLAN_TABLE} WHERE id IN ({marks})",
                tuple(chunk),
            ):
                payload["plans"].append(
                    {
                        "id": row["id"],
                        "request_json": row.get("request_json"),
                        "response_json": row.get("response_json"),
                    }
                )
    path = directory / f"backup-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}.json"
    _dump(path, payload)
    log(
        f"回滚备份已写入：{path}"
        f"（assets={len(payload['assets'])} snapshots={len(payload['snapshots'])} plans={len(payload['plans'])}）"
    )
    return path


def cmd_apply(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    if args.execute and args.env == "prod" and not args.allow_prod_write:
        raise SystemExit("拒绝执行：写生产库需显式加上 --allow-prod-write")
    inventory = json.loads((directory / "inventory.json").read_text(encoding="utf-8"))
    mapping = json.loads((directory / "mapping.json").read_text(encoding="utf-8"))
    marker = inventory["marker"]
    conn = Conn(resolve_uri(args.env, args.db_uri))
    stats: Counter[str] = Counter()

    tables = {name.strip() for name in args.tables.split(",") if name.strip()}

    if args.execute:
        _preflight_backup(conn, directory, inventory, tables, args.limit)

    if "assets" in tables:
        rows = inventory["assets"]

        def _target_url(row: Mapping[str, Any]) -> tuple[str | None, str]:
            entry = mapping.get(str(row["product_id"]))
            if entry and is_usable(entry.get("image_url")):
                return str(entry["image_url"]), str(entry.get("source") or "mapping")
            own = str(row.get("source_url") or "")
            if is_usable(own):
                return own, "own_source_url"
            return None, "unresolved"

        selected = [(row, *_target_url(row)) for row in rows]
        selected = [item for item in selected if item[1]]
        if args.limit:
            selected = selected[: args.limit]
        log(f"资产行待处理：{len(selected)}/{len(rows)}（有可用的正确图）")
        service_cache: dict[str, Any] = {}
        for index, (row, target, source) in enumerate(selected, 1):
            pid = str(row["product_id"])
            prefix = _prefix_of(row.get("storage_key"))
            service = service_cache.get(prefix)
            if service is None:
                service = _image_service(prefix, args)
                service_cache[prefix] = service
            normalized = _normalize(target)
            new_key = service._key_for_url(normalized)
            new_cached = _storage_public_url(new_key, row.get("cached_url"))
            stats[f"assets_source_{source}"] += 1
            if (
                row.get("source_url") == normalized
                and row.get("storage_key") == new_key
                and str(row.get("cached_url") or "").endswith(new_key)
            ):
                stats["assets_skipped"] += 1
                continue
            if not args.execute:
                stats["assets_planned"] += 1
                continue
            asset = service.prepare(target)
            if not asset.available:
                stats["assets_failed"] += 1
                log(f"  图片缓存失败 pid={pid} code={asset.warning_code} url={target}")
                continue
            conn.execute(
                f"UPDATE {ASSET_TABLE} SET source_url=%s, storage_key=%s, cached_url=%s, status=%s,"
                f" content_type=%s, width=%s, height=%s, last_success_at=NOW(),"
                f" last_failure_code=NULL, last_failure_message=NULL, updated_at=NOW() WHERE id=%s",
                (
                    normalized,
                    new_key,
                    asset.url,
                    "ready",
                    asset.content_type,
                    asset.width,
                    asset.height,
                    row["id"],
                ),
            )
            stats["assets_updated"] += 1
            if index % 50 == 0:
                conn.commit()
                log(f"  资产 {index}/{len(selected)} 已提交")
        if args.execute:
            conn.commit()

    if "snapshots" in tables:
        tasks = inventory["snapshots"]
        if args.limit:
            tasks = tasks[: args.limit]
        log(f"任务快照待处理：{len(tasks)}")
        for task in tasks:
            raw = conn.query(f"SELECT s0_snapshot FROM {TASK_TABLE} WHERE id=%s", (task["task_id"],))
            if not raw:
                stats["snapshots_missing"] += 1
                continue
            snapshot = _loads(raw[0]["s0_snapshot"])
            changed = 0
            for item in snapshot.get("products") or []:
                pid = str(item.get("product_id"))
                if marker not in str(item.get("image_url") or ""):
                    continue
                entry = mapping.get(pid)
                if not entry:
                    stats["snapshots_unresolved"] += 1
                    continue
                item["image_url"] = entry["image_url"]
                item.setdefault("image_url_repair", {"from": BAD_URL, "source": entry["source"]})
                changed += 1
            if not changed:
                stats["snapshots_skipped"] += 1
                continue
            if not args.execute:
                stats["snapshots_planned"] += 1
                continue
            conn.execute(
                f"UPDATE {TASK_TABLE} SET s0_snapshot=%s WHERE id=%s",
                (json.dumps(snapshot, ensure_ascii=False), task["task_id"]),
            )
            stats["snapshots_updated"] += 1
        if args.execute:
            conn.commit()

    if "plans" in tables:
        plans = inventory["plans"]
        if args.limit:
            plans = plans[: args.limit]
        log(f"治理计划待处理：{len(plans)}")
        for plan in plans:
            raw = conn.query(f"SELECT request_json, response_json FROM {PLAN_TABLE} WHERE id=%s", (plan["plan_id"],))
            if not raw:
                stats["plans_missing"] += 1
                continue
            request = _loads(raw[0]["request_json"])
            images = request.get("product_images") or {}
            changed = 0
            for pid, url in list(images.items()):
                if marker not in str(url or ""):
                    continue
                entry = mapping.get(str(pid))
                if not entry:
                    stats["plans_unresolved"] += 1
                    continue
                images[pid] = entry["image_url"]
                changed += 1
            if changed:
                request["product_images"] = images
                request["image_repair"] = {
                    "tool": "scripts/ops/repair_harbor_polluted_images.py",
                    "bad_url": BAD_URL,
                    "replaced": changed,
                    "note": "原始图片 URL 由上游索引污染，结论（reason）基于错图产生，需复核",
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            if not changed:
                stats["plans_skipped"] += 1
                continue
            if not args.execute:
                stats["plans_planned"] += 1
                continue
            conn.execute(
                f"UPDATE {PLAN_TABLE} SET request_json=%s WHERE id=%s",
                (json.dumps(request, ensure_ascii=False), plan["plan_id"]),
            )
            stats["plans_updated"] += 1
        if args.execute:
            conn.commit()

    if args.execute:
        conn.commit()
    conn.close()
    mode = "已执行" if args.execute else "dry-run"
    log(f"[{mode}] {dict(stats)}")
    _dump(directory / f"apply-{mode}-{time.strftime('%Y%m%dT%H%M%S')}.json", dict(stats))
    return 0


def _prefix_of(storage_key: str | None) -> str:
    key = str(storage_key or "")
    parts = key.rsplit("/", 1)
    return parts[0] if len(parts) == 2 else "query-governance/images/prod/v1"


def _normalize(url: str) -> str:
    from src.query_governance.relevance import normalize_image_url

    return normalize_image_url(url)


def _storage_public_url(key: str, sample_cached_url: str | None) -> str:
    """按同组已有 cached_url 的形态拼出新的公有地址。"""
    sample = str(sample_cached_url or "")
    if "://" in sample:
        base = sample.split("://", 1)[1]
        host = base.split("/", 1)[0]
        return f"https://{host}/{key.lstrip('/')}"
    try:
        from src.query_governance.composition import _image_storage

        storage = _image_storage()
        url_for = getattr(storage, "url_for", None)
        if callable(url_for):
            return url_for(key)
    except Exception:
        pass
    return key


def _image_service(prefix: str, args: argparse.Namespace) -> Any:
    from src.query_governance.composition import _image_fetcher, _image_storage
    from src.query_governance.relevance import ImageCacheService, PillowImageTransformer

    return ImageCacheService(
        fetcher=_image_fetcher(max(30.0, args.timeout)),
        transformer=PillowImageTransformer(),
        storage=_image_storage(),
        key_prefix=prefix,
        max_workers=1,
    )


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _image_marker_hits(node: Any, marker: str) -> int:
    """统计 JSON 中“图片字段”仍然命中错图特征串的次数（审计注记 image_url_repair 不计入）。"""
    hits = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and "image" in key.lower():
                if "repair" in key.lower() or "note" in key.lower():
                    continue  # 审计注记里保留原错图 URL 是刻意留痕，不计入
                hits += _leaf_marker_hits(value, marker)
            else:
                hits += _image_marker_hits(value, marker)
    elif isinstance(node, list):
        for item in node:
            hits += _image_marker_hits(item, marker)
    return hits


def _leaf_marker_hits(node: Any, marker: str) -> int:
    if isinstance(node, str):
        return 1 if marker in node else 0
    if isinstance(node, list):
        return sum(_leaf_marker_hits(item, marker) for item in node)
    if isinstance(node, dict):
        return sum(_leaf_marker_hits(item, marker) for item in node.values())
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    inventory = json.loads((directory / "inventory.json").read_text(encoding="utf-8"))
    marker = inventory["marker"]
    conn = Conn(resolve_uri(args.env, args.db_uri))
    result: dict[str, Any] = {}

    result["assets_remaining"] = len(
        conn.query(
            f"SELECT id FROM {ASSET_TABLE} WHERE brand=%s AND source_url LIKE %s",
            (args.brand, f"%{marker}%"),
        )
    )
    result["snapshots_marker_anywhere"] = len(
        conn.query(
            f"SELECT id FROM {TASK_TABLE} WHERE brand=%s AND s0_snapshot LIKE %s",
            (args.brand, f"%{marker}%"),
        )
    )
    result["plans_marker_anywhere"] = len(
        conn.query(
            f"SELECT id FROM {PLAN_TABLE} WHERE brand=%s AND (request_json LIKE %s OR response_json LIKE %s)",
            (args.brand, f"%{marker}%", f"%{marker}%"),
        )
    )
    # 关键校验：只看图片字段本身（审计注记 image_url_repair 内含原错图 URL，属正常留痕）
    snap_bad = plan_bad = snap_mismatch = 0
    mapping = {}
    mapping_path = directory / "mapping.json"
    if mapping_path.exists():
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    for row in conn.query(
        f"SELECT id, s0_snapshot FROM {TASK_TABLE} WHERE brand=%s AND s0_snapshot LIKE %s",
        (args.brand, f"%{marker}%"),
    ):
        snapshot = json.loads(row["s0_snapshot"] or "{}")
        snap_bad += 1 if _image_marker_hits(snapshot, marker) else 0
        for product in snapshot.get("products") or []:
            if not isinstance(product, dict) or not product.get("image_url_repair"):
                continue
            expected = (mapping.get(str(product.get("product_id"))) or {}).get("image_url")
            if expected and product.get("image_url") != expected:
                snap_mismatch += 1
    for row in conn.query(
        f"SELECT id, request_json, response_json FROM {PLAN_TABLE}"
        f" WHERE brand=%s AND (request_json LIKE %s OR response_json LIKE %s)",
        (args.brand, f"%{marker}%", f"%{marker}%"),
    ):
        payloads = [json.loads(row["request_json"] or "{}"), json.loads(row["response_json"] or "{}")]
        plan_bad += 1 if any(_image_marker_hits(payload, marker) for payload in payloads) else 0
    result["snapshots_bad_image_field"] = snap_bad
    result["plans_bad_image_field"] = plan_bad
    result["snapshot_image_url_mismatch_vs_mapping"] = snap_mismatch
    result["assets_bad_object_remaining"] = len(
        conn.query(
            f"SELECT id FROM {ASSET_TABLE} WHERE brand=%s AND (cached_url LIKE %s OR storage_key LIKE %s)",
            (args.brand, "%fcfcce9d33d0ba1b6ef7bcffc30b4190550eca5fd281be7ea382f7fb0b65175d%",
             "%fcfcce9d33d0ba1b6ef7bcffc30b4190550eca5fd281be7ea382f7fb0b65175d%"),
        )
    )
    result["shared_groups"] = conn.query(
        f"SELECT cached_url, COUNT(DISTINCT product_id) AS products FROM {ASSET_TABLE}"
        f" WHERE brand=%s GROUP BY cached_url HAVING products > 1 ORDER BY products DESC LIMIT 20",
        (args.brand,),
    )

    rows = conn.query(
        f"SELECT product_id, cached_url FROM {ASSET_TABLE} WHERE brand=%s AND product_id IN"
        f" ({','.join(['%s'] * len(inventory['products']))})",
        (args.brand, *inventory["products"]),
    ) if inventory["products"] else []
    if args.limit:
        rows = rows[: args.limit]

    def _head(row: Mapping[str, Any]) -> tuple[str, bool, str]:
        import requests

        url = str(row.get("cached_url") or "")
        try:
            response = requests.head(url, timeout=20)
            return str(row["product_id"]), response.status_code == 200, url
        except Exception as exc:  # noqa: BLE001 - 网络异常只影响校验结论
            return str(row["product_id"]), False, f"{url} ({exc})"

    unreachable: list[dict[str, str]] = []
    if rows:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for product_id, ok_row, url in pool.map(_head, rows):
                if not ok_row:
                    unreachable.append({"product_id": product_id, "cached_url": url})
    result["checked_cached_urls"] = len(rows)
    result["unreachable_cached_urls"] = unreachable[:20]
    result["unreachable_count"] = len(unreachable)

    ok = (
        result["assets_remaining"] == 0
        and result["snapshots_bad_image_field"] == 0
        and result["plans_bad_image_field"] == 0
        and result["assets_bad_object_remaining"] == 0
        and result["snapshot_image_url_mismatch_vs_mapping"] == 0
        and not unreachable
    )
    result["verdict"] = "PASS" if ok else "PENDING"
    _dump(directory / f"verify-{time.strftime('%Y%m%dT%H%M%S')}.json", result)
    log(json.dumps(result, ensure_ascii=False, indent=2))
    conn.close()
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# restore（按 backup-*.json 回滚，仅在确认修复有误时使用）
# --------------------------------------------------------------------------- #
def _db_datetime(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    return text[:19].replace("T", " ")


def cmd_restore(args: argparse.Namespace) -> int:
    if args.execute and args.env == "prod" and not args.allow_prod_write:
        raise SystemExit("拒绝执行：写生产库需显式加上 --allow-prod-write")
    if not args.backup:
        raise SystemExit("请用 --backup 指定 backup-*.json")
    payload = json.loads(Path(args.backup).read_text(encoding="utf-8"))
    conn = Conn(resolve_uri(args.env, args.db_uri))
    stats: Counter[str] = Counter()

    asset_cols = (
        "source_url", "source_url_hash", "storage_key", "cached_url", "status", "content_type",
        "width", "height", "image_sha256", "last_success_at", "last_failure_code",
        "last_failure_message", "created_at", "updated_at",
    )
    for row in payload.get("assets", []):
        stats["assets_seen"] += 1
        if not args.execute:
            stats["assets_planned"] += 1
            continue
        assignments = ", ".join(f"{col}=%s" for col in asset_cols)
        conn.execute(
            f"UPDATE {ASSET_TABLE} SET {assignments} WHERE id=%s",
            tuple(_db_datetime(row.get(col)) for col in asset_cols) + (row["id"],),
        )
        stats["assets_restored"] += 1
    if args.execute:
        conn.commit()

    for row in payload.get("snapshots", []):
        stats["snapshots_seen"] += 1
        if not args.execute:
            stats["snapshots_planned"] += 1
            continue
        conn.execute(
            f"UPDATE {TASK_TABLE} SET s0_snapshot=%s WHERE id=%s",
            (row.get("s0_snapshot"), row["id"]),
        )
        stats["snapshots_restored"] += 1
    if args.execute:
        conn.commit()

    for row in payload.get("plans", []):
        stats["plans_seen"] += 1
        if not args.execute:
            stats["plans_planned"] += 1
            continue
        conn.execute(
            f"UPDATE {PLAN_TABLE} SET request_json=%s, response_json=%s WHERE id=%s",
            (row.get("request_json"), row.get("response_json"), row["id"]),
        )
        stats["plans_restored"] += 1
    if args.execute:
        conn.commit()

    conn.close()
    mode = "已执行" if args.execute else "dry-run"
    log(f"[{mode}] {dict(stats)}")
    return 0


# --------------------------------------------------------------------------- #
def _load_env(explicit: str | None = None) -> Path | None:
    """从显式路径或当前目录向上查找 .env（worktree 内没有 .env）。"""
    if explicit:
        load_dotenv(explicit)
        return Path(explicit)
    seen: list[Path] = []
    for start in (Path.cwd(), REPO_ROOT):
        for directory in (start, *start.parents):
            if directory in seen:
                continue
            seen.append(directory)
            candidate = directory / ".env"
            if candidate.is_file():
                load_dotenv(candidate)
                return candidate
    load_dotenv()
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("inventory", "mapping", "apply", "verify", "restore"))
    parser.add_argument("--env", choices=("test", "prod"), default=None, help="默认取 .env 的 ENV")
    parser.add_argument("--brand", default="harbor")
    parser.add_argument("--db-uri", default=None, help="覆盖 AUTO_DB_URI_*")
    parser.add_argument("--dir", default=None, help="工作目录（inventory 的 --out 或既有清单目录）")
    parser.add_argument("--out", default=None, help="inventory 输出目录")
    parser.add_argument("--marker", default=BAD_MARKER, help="错图 URL 特征串")
    parser.add_argument("--execute", action="store_true", help="apply 真正写库（默认 dry-run）")
    parser.add_argument("--allow-prod-write", action="store_true", help="确认授权写生产库")
    parser.add_argument("--tables", default="assets,snapshots,plans")
    parser.add_argument("--limit", type=int, default=0, help="apply 每类只处理前 N 条（金丝雀）")
    parser.add_argument("--workers", type=int, default=4, help="索引/数仓并发")
    parser.add_argument("--list-workers", type=int, default=2, help="product/list 并发（服务端限流）")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--dw-brand", default="Harbor")
    parser.add_argument("--skip-dw", action="store_true")
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--skip-product-list", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--env-file", default=None, help="显式指定 .env 路径")
    parser.add_argument("--backup", default=None, help="restore：backup-*.json 路径")
    args = parser.parse_args(argv)
    env_path = _load_env(args.env_file)
    args.env = args.env or str(os.getenv("ENV") or "test").strip().lower() or "test"
    if args.env not in {"test", "prod"}:
        raise SystemExit(f"不支持的 ENV：{args.env}")
    if env_path is not None:
        log(f"环境文件：{env_path} | ENV={args.env}")
    if args.dir is None:
        args.dir = args.out
    if args.out is None and args.command == "inventory":
        args.out = str(Path("var/harbor-image-repair") / time.strftime("%Y%m%dT%H%M%S"))
    if args.command not in {"inventory", "restore"} and not args.dir:
        raise SystemExit("请用 --dir 指定 inventory 产出的清单目录")
    handlers = {
        "inventory": cmd_inventory,
        "mapping": cmd_mapping,
        "apply": cmd_apply,
        "verify": cmd_verify,
        "restore": cmd_restore,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())