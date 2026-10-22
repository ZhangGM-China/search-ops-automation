"""Independent, rule-based broken-rate monitoring for Query Governance.

This module is deliberately separate from the Query governance mainline.  It
reads the last persisted selection batch and performs only the periodic
inventory checks; it never extracts or selects Top Queries itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from collections import defaultdict
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from src.config import (
    BRAND_CONFIGS,
    get_brand_business_config,
    is_brand_enabled,
    supports_broken_rate_monitoring,
    _raw_config,
)
from src.governance.query_identity import normalize_governance_query_key
from src.governance.query_policy import assert_query_governance_allowed
from src.query_governance.selection import (
    QuerySelectionBatch,
    SqlAlchemyQuerySelectionStore,
)
from src.top_query.constants import query_demotion_source_bucket_is_eligible
from .visibility import check_modes_for_date, effective_check_modes


class SqlAlchemyLatestQueryBatchReader:
    """Read the newest persisted selection batch for one brand."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._store = SqlAlchemyQuerySelectionStore(session_factory)

    def get_latest(
        self, brand: str, *, before: dt.datetime | None = None
    ) -> QuerySelectionBatch | None:
        return self._store.get_latest_batch(brand, before=before)


def resolve_latest_persisted_queries(
    reader: Any,
    *,
    brand: str,
    now: dt.datetime | None = None,
) -> QuerySelectionBatch | None:
    """Resolve a batch as-of ``now``; before 00:30 this naturally is prior data."""

    return reader.get_latest(brand, before=now or dt.datetime.now())


def _product_id(product: Mapping[str, Any]) -> str:
    for key in ("skuId", "spuId", "productId", "spu", "attr1"):
        value = product.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _bucket(product: Mapping[str, Any]) -> str:
    value = product.get("bucket")
    if value:
        return str(value).strip()
    ext = product.get("ext")
    return str(ext.get("bucket") or "").strip() if isinstance(ext, Mapping) else ""


def _read_control_query_keys(brand: str) -> set[str]:
    """Read active control-group Queries from the rewrite-owned experiment tables."""

    from sqlalchemy import select
    from src.persistence import SessionLocal
    from .models import QueryGovernanceExperiment, QueryGovernanceExperimentSample

    try:
        with SessionLocal() as session:
            experiment = session.execute(
                select(QueryGovernanceExperiment)
                .where(
                    QueryGovernanceExperiment.brand == brand,
                    QueryGovernanceExperiment.status == "running",
                )
                .order_by(QueryGovernanceExperiment.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if experiment is None:
                return set()
            rows = session.execute(
                select(QueryGovernanceExperimentSample.query_text).where(
                    QueryGovernanceExperimentSample.experiment_id == experiment.id,
                    QueryGovernanceExperimentSample.group_type == "control",
                )
            ).all()
            return {
                normalize_governance_query_key(brand, row[0])
                for row in rows
                if row[0]
            }
    except Exception as exc:
        logging.error("读取断码 A/B Test 对照组失败，brand=%s error=%s", brand, exc)
        return set()


class InventoryActionService:
    """Execute one inventory demotion/recovery through shared action facts."""

    def __init__(self, operational_service: Any) -> None:
        self.operational = operational_service

    def execute_inventory_monitor_demotion(
        self,
        brand: str,
        query: str,
        candidates: list[Mapping[str, Any]],
        recovery_candidates: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from src.governance.action_gateway import ActionGateway, ActionIntent

        add_ids = sorted(
            {
                str(item.get("product_id") or "").strip()
                for item in candidates or []
                if str(item.get("product_id") or "").strip()
            }
        )
        remove_ids = sorted(
            {
                str(item.get("product_id") or "").strip()
                for item in recovery_candidates or []
                if str(item.get("product_id") or "").strip()
            }
        )
        if not add_ids and not remove_ids:
            return {
                "status": "verifying",
                "applied_product_count": 0,
                "recovered_product_count": 0,
            }
        assert_query_governance_allowed(brand, query)
        payload = {
            "source": "inventory_monitor",
            "brand": brand,
            "query": query,
            "add_product_ids": add_ids,
            "remove_product_ids": remove_ids,
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        intent = ActionIntent(
            action_type="query_demote",
            idempotency_key=f"inventory-monitor:{digest}",
            payload=payload,
            operator="scheduler:inventory_monitor",
            brand=brand,
            release=str(os.getenv("AIOPS_RELEASE") or "dev"),
            permission="execute_governance_actions",
            query_text=query,
            target="query_demotion",
        )
        gateway = ActionGateway(
            authorizer=lambda _intent: True,
        )

        def execute(_context: Any) -> dict[str, Any]:
            success = self.operational.sync_broken_code_to_redis(
                brand,
                add_items={query: add_ids} if add_ids else {},
                remove_items={query: remove_ids} if remove_ids else {},
                allow_prod_recovery=True,
            )
            if success is not True:
                raise RuntimeError("inventory demotion write or readback failed")
            return {
                "status": "success",
                "product_ids": add_ids,
                "remove_product_ids": remove_ids,
            }

        try:
            gateway_result = gateway.execute(intent, execute)
        except Exception as exc:
            return {
                "status": "error",
                "error_code": "INVENTORY_ACTION_FAILED",
                "error_message": str(exc).strip() or exc.__class__.__name__,
                "applied_product_count": 0,
                "recovered_product_count": 0,
            }
        return {
            "status": "verifying",
            "action_result": gateway_result,
            "applied_product_count": len(add_ids),
            "recovered_product_count": len(remove_ids),
        }


class InventoryMonitorService:
    """Run one independent inventory monitor cycle for a brand."""

    def __init__(
        self,
        *,
        reader: Any | None = None,
        operational_service: Any | None = None,
        governance_service: Any | None = None,
        session_factory: Callable[[], Session] | None = None,
        task_reader: Callable[[], list[Any]] | None = None,
        task_saver: Callable[[dict[str, Any]], Any] | None = None,
        task_stock_updater: Callable[[int, int], Any] | None = None,
        task_status_updater: Callable[[int, str], Any] | None = None,
        task_recoverer: Callable[[int, int], Any] | None = None,
        action_logger: Callable[[dict[str, Any]], Any] | None = None,
        config_getter: Callable[[str], Mapping[str, Any]] = get_brand_business_config,
        enabled_checker: Callable[[str], bool] = is_brand_enabled,
        supported_checker: Callable[[str], bool] = supports_broken_rate_monitoring,
        control_query_reader: Callable[[str], set[str]] | None = None,
    ) -> None:
        self.reader = reader
        self.operational = operational_service
        self.governance = governance_service
        self.session_factory = session_factory
        self.task_reader = task_reader
        self.task_saver = task_saver
        self.task_stock_updater = task_stock_updater
        self.task_status_updater = task_status_updater
        self.task_recoverer = task_recoverer
        self.action_logger = action_logger
        self.config_getter = config_getter
        self.enabled_checker = enabled_checker
        self.supported_checker = supported_checker
        self.control_query_reader = control_query_reader or _read_control_query_keys

    def _wire_defaults(self) -> None:
        if self.reader is None or self.operational is None:
            from src.persistence import SessionLocal
            from src.integrations.operational_service import OperationalService

            self.session_factory = self.session_factory or SessionLocal
            self.reader = self.reader or SqlAlchemyLatestQueryBatchReader(
                self.session_factory
            )
            self.operational = self.operational or OperationalService()
        if self.governance is None:
            # Inventory actions must use the same authoritative, audited
            # query-demotion workflow as the main governance service. The old
            # InventoryActionService calls the legacy Redis compatibility
            # endpoint, which is intentionally disabled for production adds.
            from src.top_query.service import TopQueryGovernanceService

            self.governance = TopQueryGovernanceService(
                operational_service=self.operational,
            )
        if self.task_reader is None or self.task_saver is None:
            from src.persistence import (
                get_active_inventory_tasks,
                save_inventory_task,
                update_inventory_task_stock,
                update_inventory_task_status,
                mark_inventory_task_recovered,
                save_action_log,
            )

            self.task_reader = self.task_reader or get_active_inventory_tasks
            self.task_saver = self.task_saver or save_inventory_task
            self.task_stock_updater = self.task_stock_updater or update_inventory_task_stock
            self.task_status_updater = self.task_status_updater or update_inventory_task_status
            self.task_recoverer = self.task_recoverer or mark_inventory_task_recovered
            self.action_logger = self.action_logger or save_action_log

    def run(self, brand_filter: str | None = None) -> dict[str, Any]:
        self._wire_defaults()
        brands = [brand_filter] if brand_filter else list(BRAND_CONFIGS.keys())
        summary: dict[str, Any] = {
            "status": "success",
            "brands": {},
            "added_count": 0,
            "recovered_count": 0,
            "auto_stopped_count": 0,
            "skipped_unsupported": 0,
            "action_failures": [],
        }
        tasks = list(self.task_reader() or [])
        by_brand: dict[str, list[Any]] = defaultdict(list)
        for task in tasks:
            by_brand[str(task.brand)].append(task)

        for brand in brands:
            if not brand or not self.enabled_checker(brand):
                continue
            if not self.supported_checker(brand):
                summary["skipped_unsupported"] += len(by_brand.get(brand, []))
                continue
            config = dict(self.config_getter(brand) or {})
            history = (_raw_config.get("query_governance_check_history") or {}).get(brand)
            if history:
                dated_modes = check_modes_for_date(_raw_config, brand, dt.date.today()) or {}
                check_modes = dict(config.get("top_query_check_modes") or {})
                check_modes["inventory"] = "auto" if dated_modes.get("inventory", True) else "disabled"
                config["top_query_check_modes"] = check_modes
            batch = resolve_latest_persisted_queries(self.reader, brand=brand)
            result = self._run_brand(brand, batch, by_brand.get(brand, []), config)
            summary["brands"][brand] = result
            for key in ("added_count", "recovered_count", "auto_stopped_count"):
                summary[key] += int(result.get(key) or 0)
            summary["action_failures"].extend(result.get("action_failures") or [])
        if summary["action_failures"]:
            summary["status"] = "error"
            first_failure = summary["action_failures"][0]
            summary["error_code"] = "INVENTORY_ACTION_FAILED"
            summary["error"] = (
                f"断码动作失败 {len(summary['action_failures'])} 项; "
                f"首项 query={first_failure.get('query')} "
                f"reason={first_failure.get('error_message')}"
            )
        return summary

    def _run_brand(
        self,
        brand: str,
        batch: QuerySelectionBatch | None,
        active_tasks: list[Any],
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        threshold = (
            float(config.get("top_query_inventory_broken_rate_threshold", 50)) / 100
        )
        limit = max(1, int(config.get("top_query_check_product_limit", 16)))
        max_cycles = max(1, int(config.get("top_query_inventory_max_cycles", 3)))
        control_query_keys = self.control_query_reader(brand)
        active_by_key = {
            (normalize_governance_query_key(brand, task.original_query), str(task.product_id)): task
            for task in active_tasks
        }
        status_map = None
        if active_tasks:
            ids = list(
                dict.fromkeys(
                    str(task.product_id) for task in active_tasks if task.product_id
                )
            )
            status_map = self.operational.get_product_status_map(brand, ids)

        added = recovered = auto_stopped = 0
        protected_bucket_skip_count = 0
        action_failures: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        if active_tasks and status_map is None:
            warnings.append(
                {
                    "code": "PRODUCT_STATUS_LOOKUP_FAILED",
                    "message": "商品状态批量查询失败，监测任务保持原状态",
                }
            )
        for task in active_tasks:
            if status_map is None:
                continue
            state = status_map.get(str(task.product_id))
            if not state or not state.get("exists") or not state.get("is_online"):
                self.task_status_updater(task.id, "auto_stopped")
                auto_stopped += 1

        processed: set[tuple[str, str]] = set()
        removals: dict[str, list[Any]] = defaultdict(list)
        for task in active_tasks:
            if status_map is not None:
                state = status_map.get(str(task.product_id))
                if not state or not state.get("exists") or not state.get("is_online"):
                    continue
            query_key = normalize_governance_query_key(brand, task.original_query)
            try:
                rate = self.operational.find_product_broken_rate(
                    brand, task.original_query, str(task.product_id)
                )
            except Exception:
                rate = None
            if rate is None:
                continue
            rate_percent = int(round(float(rate) * 100))
            processed.add((query_key, str(task.product_id)))
            if float(rate) < threshold:
                removals[task.original_query].append((task, rate_percent))
            else:
                self.task_stock_updater(task.id, rate_percent)

        added = recovered = 0
        query_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"broken": 0, "normal": 0, "recovered": 0})
        specs = batch.specs if batch is not None and effective_check_modes(config)["inventory"] else ()
        for _cycle in range(max_cycles):
            additions: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for spec in specs:
                query = spec.query
                query_key = normalize_governance_query_key(brand, query)
                if query_key in control_query_keys:
                    continue
                products = list(
                    self.operational.search_products(brand, query, page_size=limit)
                    or []
                )[:limit]
                for product in products:
                    product_id = _product_id(product)
                    if not product_id or (query_key, product_id) in processed:
                        continue
                    if not query_demotion_source_bucket_is_eligible(_bucket(product)):
                        protected_bucket_skip_count += 1
                        processed.add((query_key, product_id))
                        continue
                    rate = self.operational.get_product_broken_rate(brand, product)
                    if rate is None:
                        continue
                    rate_percent = int(round(float(rate) * 100))
                    if float(rate) >= threshold:
                        query_stats[query]["broken"] += 1
                    else:
                        query_stats[query]["normal"] += 1
                    task = active_by_key.get((query_key, product_id))
                    if float(rate) >= threshold and task is None:
                        additions[query].append(
                            {
                                "query": query,
                                "product_id": product_id,
                                "product_name": product.get("productName")
                                or product.get("name")
                                or "",
                                "broken_rate_percent": rate_percent,
                            }
                        )
                    elif task is not None and float(rate) >= threshold:
                        self.task_stock_updater(task.id, rate_percent)
                    elif task is not None and float(rate) < threshold:
                        removals[query].append((task, rate_percent))
                    processed.add((query_key, product_id))

            changed = False
            for query, candidates in additions.items():
                try:
                    result = self.governance.execute_inventory_monitor_demotion(
                        brand,
                        query,
                        candidates,
                        recovery_candidates=[],
                    )
                except Exception as exc:
                    # 单 Query 动作失败不能中断整个巡检周期；记录失败后继续处理其余 Query。
                    logging.exception(
                        "断码降桶动作执行异常 brand=%s query=%s", brand, query
                    )
                    action_failures.append(
                        {
                            "query": query,
                            "product_count": len(candidates),
                            "error_code": "INVENTORY_ACTION_FAILED",
                            "error_message": f"库存降桶动作执行异常: {exc}",
                        }
                    )
                    continue
                if isinstance(result, Mapping) and result.get("status") == "verifying":
                    for candidate in candidates:
                        self.action_logger(
                            {
                                "brand": brand,
                                "query": query,
                                "spu": candidate["product_id"],
                                "name": candidate["product_name"],
                                "type": 2,
                                "reason": f"断码率自动降桶: {candidate['broken_rate_percent']}%",
                            }
                        )
                        self.task_saver(
                            {
                                "brand": brand,
                                "spu": candidate["product_id"],
                                "name": candidate["product_name"],
                                "query": query,
                                "stock": candidate["broken_rate_percent"],
                            }
                        )
                    added += len(candidates)
                    changed = True
                else:
                    action_failures.append(
                        {
                            "query": query,
                            "product_count": len(candidates),
                            "error_code": str(result.get("error_code") or "INVENTORY_ACTION_FAILED") if isinstance(result, Mapping) else "INVENTORY_ACTION_FAILED",
                            "error_message": str(result.get("error_message") or "库存降桶动作未完成") if isinstance(result, Mapping) else "库存降桶动作未完成",
                        }
                    )
            for query, candidates in list(removals.items()):
                recovery_candidates = [
                    {
                        "product_id": task.product_id,
                        "product_name": task.product_name,
                        "broken_rate_percent": rate,
                    }
                    for task, rate in candidates
                ]
                try:
                    result = self.governance.execute_inventory_monitor_demotion(
                        brand, query, [], recovery_candidates=recovery_candidates,
                    )
                except Exception as exc:
                    logging.exception(
                        "断码恢复动作执行异常 brand=%s query=%s", brand, query
                    )
                    action_failures.append(
                        {
                            "query": query,
                            "product_count": len(candidates),
                            "error_code": "INVENTORY_ACTION_FAILED",
                            "error_message": f"库存恢复动作执行异常: {exc}",
                        }
                    )
                    continue
                if not isinstance(result, Mapping) or result.get("status") != "verifying":
                    action_failures.append(
                        {
                            "query": query,
                            "product_count": len(candidates),
                            "error_code": str(result.get("error_code") or "INVENTORY_ACTION_FAILED") if isinstance(result, Mapping) else "INVENTORY_ACTION_FAILED",
                            "error_message": str(result.get("error_message") or "库存恢复动作未完成") if isinstance(result, Mapping) else "库存恢复动作未完成",
                        }
                    )
                    continue
                for task, rate in candidates:
                    self.action_logger(
                        {
                            "brand": brand,
                            "query": task.original_query,
                            "spu": task.product_id,
                            "name": task.product_name,
                            "type": 3,
                            "reason": f"断码率自动恢复: {rate}%",
                        }
                    )
                    self.task_recoverer(task.id, rate)
                    recovered += 1
                    changed = True
                    query_stats[str(task.original_query or "")]["recovered"] += 1
                removals.pop(query, None)
            if not changed:
                break
        if batch is not None and getattr(batch, "batch_id", None):
            self._record_inventory_scan_events(batch.batch_id, brand, query_stats, dt.datetime.now())
        return {
            "status": "error" if action_failures else "success",
            "batch_id": batch.batch_id if batch is not None else None,
            "added_count": added,
            "recovered_count": recovered,
            "auto_stopped_count": auto_stopped,
            "protected_bucket_skip_count": protected_bucket_skip_count,
            "warnings": warnings,
            "action_failures": action_failures,
        }

    def _record_inventory_scan_events(
        self,
        batch_id: str,
        brand: str,
        query_stats: Mapping[str, Mapping[str, int]],
        scan_time: dt.datetime,
    ) -> None:
        if not self.session_factory or not batch_id or not query_stats:
            return
        try:
            from sqlalchemy import select
            from src.query_governance.models import QueryGovernanceAgentEvent, QueryGovernanceTask

            with self.session_factory() as session:
                tasks = session.execute(
                    select(QueryGovernanceTask.id, QueryGovernanceTask.query_text).where(
                        QueryGovernanceTask.batch_id == batch_id,
                        QueryGovernanceTask.brand == brand,
                    )
                ).all()
                task_by_query = {task_query: task_id for task_id, task_query in tasks}
                for query, stats in query_stats.items():
                    task_id = task_by_query.get(query)
                    if task_id is None:
                        continue
                    session.add(
                        QueryGovernanceAgentEvent(
                            task_id=task_id,
                            brand=brand,
                            query_text=query,
                            agent_name="inventory_monitor",
                            node_key="inventory",
                            event_type="inventory_scan",
                            output_json=json.dumps(
                                {
                                    "checkedAt": scan_time.strftime("%Y-%m-%d %H:%M"),
                                    "brokenProductCount": int(stats.get("broken", 0)),
                                    "normalProductCount": int(stats.get("normal", 0)),
                                    "recoveredProductCount": int(stats.get("recovered", 0)),
                                },
                                ensure_ascii=False,
                            ),
                            created_at=scan_time,
                        )
                    )
                session.commit()
        except Exception as exc:
            logging.warning("写入断码巡检事件失败 batch_id=%s brand=%s: %s", batch_id, brand, exc)


def run_inventory_monitor_cycle(brand_filter: str | None = None) -> dict[str, Any]:
    """Run one cycle; scheduler owns reload and fencing around this call."""

    try:
        return InventoryMonitorService().run(brand_filter)
    except Exception as exc:  # Keep one brand failure visible without killing scheduler.
        logging.exception("inventory monitor cycle failed: %s", exc)
        return {
            "status": "error",
            "error_code": "INVENTORY_MONITOR_FAILED",
            "error": str(exc),
        }


__all__ = [
    "InventoryMonitorService",
    "SqlAlchemyLatestQueryBatchReader",
    "resolve_latest_persisted_queries",
    "run_inventory_monitor_cycle",
]
