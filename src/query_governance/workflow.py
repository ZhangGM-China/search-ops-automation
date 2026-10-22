"""Fixed-order, batch-oriented Query governance mainline."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date
import logging
import time
from typing import Any
from uuid import uuid4

from .collection import S0Collector, S0CollectionError
from .langfuse import QueryGovernanceObservability
from .models import (
    ActionRecoveryResult,
    ActionRecoveryStatus,
    NodeResult,
    NodeStatus,
    QueryRunResult,
    QuerySpec,
)
from .ports import (
    ActionGateway,
    CheckpointStore,
    NodeRunner,
    SearchGateway,
    StateStore,
    WorldFactsGateway,
)
from .context import SharedContextService
from .recall import RecallHistoryGateway
from .relevance_config import RelevanceRuntimeConfig, get_relevance_runtime_config
from .relevance_buckets import filter_relevance_products
from .relevance_batch import RelevanceBatchInput
from .relevance_remediation import build_relevance_remediation_payload, decide_blacklist_handling


logger = logging.getLogger(__name__)


def _workflow_product_id(product: Mapping[str, Any]) -> str:
    for key in ("product_id", "productId", "skuId", "spuId", "taggingUid", "id"):
        value = product.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


_TELEMETRY_SUM_KEYS = frozenset(
    {
        "executionSeconds",
        "independentExecutionSeconds",
        "imagePreparationSeconds",
        "modelSeconds",
        "batchModelSeconds",
        "sharedBatchSeconds",
        "roundSeconds",
        "retrySeconds",
        "retryModelSeconds",
        "actionSeconds",
        "recheckSeconds",
        "imageSuccessCount",
        "imageFailureCount",
        "modelCallCount",
        "retryCount",
        "actionCount",
    }
)


def _accumulate_telemetry(total: dict[str, Any], sample: Mapping[str, Any]) -> None:
    for key, value in sample.items():
        if key in _TELEMETRY_SUM_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = round(float(total.get(key) or 0) + float(value), 6)
            if key.endswith("Count") or key in {"modelCallCount", "retryCount", "actionCount"}:
                total[key] = int(total[key])
        elif key == "maxConcurrency" and isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = max(int(total.get(key) or 0), int(value))
        elif key not in total or value is not None:
            total[key] = value
from src.llm.config_store import capture_runtime_snapshot
from src.llm.node_config import (
    QUERY_GOVERNANCE_NODE_KEYS,
    NodeConfigSnapshot,
)


def _default_model_config_snapshot() -> NodeConfigSnapshot:
    return capture_runtime_snapshot(node_keys=QUERY_GOVERNANCE_NODE_KEYS)


class _WorkflowYield(BaseException):
    """End an invocation at a node boundary without failing the batch."""


class QueryGovernanceWorkflow:
    """Run S0, recall, reorder, and relevance in fixed stage batches.

    The workflow owns ordering, stage barriers, error isolation, and immediate
    persistence. Business rules and external integrations stay behind ports.
    """

    _RUNNER_KEYS = ("recall", "reorder", "relevance")

    def __init__(
        self,
        *,
        search: SearchGateway,
        store: StateStore,
        runners: Mapping[str, NodeRunner],
        action_gateway: ActionGateway,
        facts: WorldFactsGateway | None = None,
        model_config_snapshot: NodeConfigSnapshot | None = None,
        model_config_provider: Callable[[], NodeConfigSnapshot] = _default_model_config_snapshot,
        checkpoint: CheckpointStore | None = None,
        async_tasks: object | None = None,
        recall_history: RecallHistoryGateway | None = None,
        shared_context: SharedContextService | None = None,
        preference_provider: object | None = None,
        observability: QueryGovernanceObservability | None = None,
        experiment_observer: object | None = None,
        relevance_blacklist_ratio_threshold: float | None = None,
        relevance_blacklist_execution_mode: str = "auto",
        relevance_max_rounds: int | None = None,
        reorder_enabled: bool = True,
        relevance_enabled: bool = True,
        recall_enabled: bool = True,
        relevance_config_provider: Callable[[str], RelevanceRuntimeConfig] = get_relevance_runtime_config,
        reorder_batch_runner: object | None = None,
        relevance_batch_runner: object | None = None,
        relevance_independent_actions: bool = False,
        query_concurrency: int = 1,
        relevance_query_batch_size: int = 5,
        max_node_starts: int | None = None,
        max_execution_seconds: float | None = None,
    ) -> None:
        missing = [key for key in self._RUNNER_KEYS if key not in runners]
        if missing:
            raise ValueError(f"missing node runners: {', '.join(missing)}")
        self.search = search
        self.store = store
        self.runners = dict(runners)
        self.action_gateway = action_gateway
        self.facts = facts
        self._fixed_model_config_snapshot = model_config_snapshot
        self.model_config_provider = model_config_provider
        self.checkpoint = checkpoint
        self.async_tasks = async_tasks
        self.recall_history = recall_history
        self.shared_context = shared_context
        # The provider owns behavior-evidence collection and batched preference
        # generation.  Keeping it behind one optional boundary lets tests and
        # non-scheduled callers continue to supply precomputed results.
        self.preference_provider = preference_provider
        self.observability = observability or QueryGovernanceObservability.from_environment()
        self.experiment_observer = experiment_observer
        self._relevance_blacklist_ratio_threshold = relevance_blacklist_ratio_threshold
        self._relevance_blacklist_execution_mode = str(relevance_blacklist_execution_mode or "auto").strip().lower()
        decide_blacklist_handling([], execution_mode=self._relevance_blacklist_execution_mode)
        self._relevance_max_rounds = relevance_max_rounds
        self._relevance_independent_actions = bool(relevance_independent_actions)
        self.reorder_enabled = bool(reorder_enabled)
        self.relevance_enabled = bool(relevance_enabled)
        self.recall_enabled = bool(recall_enabled)
        self.relevance_config_provider = relevance_config_provider
        self.reorder_batch_runner = reorder_batch_runner
        self.relevance_batch_runner = relevance_batch_runner
        if isinstance(query_concurrency, bool) or not isinstance(query_concurrency, int) or query_concurrency <= 0:
            raise ValueError("query_concurrency must be a positive integer")
        if (
            isinstance(relevance_query_batch_size, bool)
            or not isinstance(relevance_query_batch_size, int)
            or relevance_query_batch_size <= 0
        ):
            raise ValueError("relevance_query_batch_size must be a positive integer")
        self.query_concurrency = query_concurrency
        self.relevance_query_batch_size = relevance_query_batch_size
        if max_node_starts is not None and (isinstance(max_node_starts, bool)
                or not isinstance(max_node_starts, int) or max_node_starts < 1):
            raise ValueError("max_node_starts must be a positive integer")
        if max_execution_seconds is not None and (isinstance(max_execution_seconds, bool)
                or not isinstance(max_execution_seconds, (int, float)) or max_execution_seconds <= 0):
            raise ValueError("max_execution_seconds must be positive")
        if (max_node_starts is not None or max_execution_seconds is not None) and checkpoint is None:
            raise ValueError("bounded workflow requires a checkpoint")
        self.max_node_starts = max_node_starts
        self.max_execution_seconds = max_execution_seconds
        self._node_starts = 0
        self._invocation_started = 0.0
        self._resumed_after_yield = False
        self._invocation_yielded = False
        self._last_specs: tuple[QuerySpec, ...] = ()
        self._last_batch_id: str | None = None
        self._last_data_date: date | None = None
        self._last_preference_results: Mapping[str, Any] | None = None
        self._persisted_cache: dict[str, dict[tuple[str, str], NodeResult]] = {}
        self._observed_control_results: dict[str, QueryRunResult] = {}
        self._batch_results: dict[str, QueryRunResult] = {}
        self.assert_scheduler_fence: Callable[[], None] | None = None
        self.should_continue: Callable[[], bool] | None = None
        self._trace_batch = None
        self._active_batch_id: str | None = None
        self._checkpoint_version: int | None = None
        self._checkpoint_s0_store: StateStore | None = None
        self._active_data_date: date | None = None
        self.s0_collector = S0Collector(search=search, store=store)

    def collect_facts(
        self, value: date, weather_cities: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        """Expose the fact-service boundary used by the reorder stage."""
        if self.facts is None:
            raise RuntimeError("world facts service is not configured")
        return self.facts.collect(value, weather_cities)

    def run_batch(
        self, specs: Iterable[QuerySpec], *, batch_id: str | None = None,
        data_date: date | None = None, preference_results: Mapping[str, Any] | None = None,
    ) -> dict[str, QueryRunResult]:
        self._node_starts = 0
        self._invocation_started = time.monotonic()
        self._batch_results = {}
        self._persisted_cache = {}
        if batch_id != self._last_batch_id or not self._invocation_yielded:
            self._observed_control_results = {}
        self._last_specs = tuple(specs)
        self._last_batch_id = batch_id
        self._last_data_date = data_date
        self._last_preference_results = preference_results
        self._invocation_yielded = False
        try:
            return self._run_batch(self._last_specs, batch_id=batch_id, data_date=data_date,
                                   preference_results=preference_results)
        except _WorkflowYield:
            if self.should_continue is not None and not self.should_continue():
                # Shutdown/fence loss stops the current owner from opening a
                # fresh slice.  The durable checkpoint remains running for the
                # normal stale-worker recovery path.
                return self._batch_results
            self._assert_scheduler_fence()
            checkpoint = self.checkpoint.yield_batch(
                self._require_batch_id(), self._require_checkpoint_version())
            self._checkpoint_version = checkpoint.version
            for run in self._batch_results.values():
                if not run.spec.is_control and run.final_status != "error":
                    run.final_status = "running"
            self._invocation_yielded = True
            return self._batch_results

    @property
    def invocation_yielded(self) -> bool:
        return self._invocation_yielded

    def resume_yielded_batch(self) -> dict[str, QueryRunResult]:
        """Continue the frozen batch immediately after an intentional yield."""
        if not self._invocation_yielded:
            return self._batch_results
        if self.should_continue is not None and not self.should_continue():
            return self._batch_results
        self._assert_scheduler_fence()
        return self.run_batch(
            self._last_specs,
            batch_id=self._last_batch_id,
            data_date=self._last_data_date,
            preference_results=self._last_preference_results,
        )

    def _run_batch(
        self,
        specs: Iterable[QuerySpec],
        *,
        batch_id: str | None = None,
        data_date: date | None = None,
        preference_results: Mapping[str, Any] | None = None,
    ) -> dict[str, QueryRunResult]:
        """Run one brand batch through stage barriers.

        A Query error is recorded on that Query and does not prevent another
        Query from reaching later stages. The returned mapping is keyed by
        the durable ``query_id`` carried by each ``QuerySpec``.
        """

        all_items = [
            (
                replace(
                    spec,
                    reorder_eligible=False,
                    reorder_skip_reason="品牌未启用重排序",
                )
                if not self.reorder_enabled and not spec.is_control
                else spec
            )
            for spec in specs
        ]
        if not all_items:
            raise ValueError("at least one Query is required")
        self._run_batch_id = batch_id
        self._active_data_date = data_date
        control_items = [spec for spec in all_items if spec.is_control]
        items = [spec for spec in all_items if not spec.is_control]
        snapshot = self._fixed_model_config_snapshot or self.model_config_provider()
        if not isinstance(snapshot, NodeConfigSnapshot):
            raise TypeError("model config provider returned an invalid snapshot")
        trace_batch_id = batch_id or next(
            (spec.batch_id for spec in all_items if spec.batch_id),
            f"query-governance-adhoc-{uuid4()}",
        )
        try:
            self._trace_batch = self.observability.start_batch(
                trace_batch_id,
                all_items,
                metadata={"model_config_version": snapshot.version},
            )
        except Exception as exc:
            fallback_observability = QueryGovernanceObservability.disabled(
                f"workflow_trace_failed:{exc.__class__.__name__}"
            )
            self._trace_batch = fallback_observability.start_batch(
                trace_batch_id,
                all_items,
                metadata={"model_config_version": snapshot.version},
            )
        if self.checkpoint is not None and items:
            if not batch_id:
                raise ValueError("batch_id is required for checkpointed workflow")
            checkpoint = self.checkpoint.start_batch(
                batch_id, items, data_date=data_date
            )
            self._resumed_after_yield = checkpoint.resumed_after_yield
            self._active_batch_id = batch_id
            self._checkpoint_version = checkpoint.version
            self._checkpoint_s0_store = self.checkpoint.s0_store(batch_id)
            collector = S0Collector(
                search=self.search, store=self._checkpoint_s0_store
            )
        else:
            self._active_batch_id = None
            self._checkpoint_version = None
            self._checkpoint_s0_store = None
            collector = self.s0_collector
        # Pin the model bundle only after a checkpointed batch has created its
        # durable workflow row.  This keeps the version write on the same
        # Checkpoint store used by the production composition and avoids
        # creating an orphan workflow row before Query tasks exist.
        self._persist_model_config_version(items, snapshot, batch_id=batch_id)
        results = {
            spec.query_id: QueryRunResult(spec=spec) for spec in all_items
        }
        self._batch_results = results
        self._observe_control_items(
            control_items, results, data_date=data_date, batch_id=batch_id
        )
        if not items:
            return results
        batch_query_ids = tuple(results)

        shared_contexts: dict[str, Mapping[str, Any]] = {}
        shared_context_error = False
        if self.shared_context is not None:
            if not batch_id:
                raise ValueError("batch_id is required when shared context is configured")
            if data_date is None:
                raise ValueError("data_date is required when shared context is configured")

        # Stage 1: collect every S0 before any governance node starts.
        # Mixed is an independent network request per Query.  Collect the
        # immutable S0 snapshots concurrently, then persist terminal node
        # results in input order so checkpoint semantics remain deterministic.
        pending_s0: list[tuple[QuerySpec, QueryRunResult]] = []
        effective_items = list(items)
        for index, spec in enumerate(effective_items):
            run = results[spec.query_id]
            persisted = self._persisted(spec, "s0")
            if persisted is not None and persisted.terminal and not self._should_retry_s0_skip(spec, persisted):
                has_retryable_error = (
                    persisted.status is NodeStatus.ERROR and not self._resumed_after_yield
                    and (
                        self._checkpoint_s0_store.get_s0(
                            spec.brand, spec.query
                        )
                        if self._checkpoint_s0_store is not None
                        else self.store.get_s0(spec.brand, spec.query)
                    )
                    is None
                )
                if not has_retryable_error:
                    run.nodes["s0"] = persisted
                    run.s0 = persisted.result
                    if persisted.status is NodeStatus.ERROR:
                        run.final_status = "error"
                    continue
            pending_s0.append((spec, run))

        def collect_s0(item: tuple[QuerySpec, QueryRunResult]):
            spec, _run = item
            started = time.perf_counter()
            try:
                return collector.collect(spec), max(0.0, time.perf_counter() - started)
            except Exception as exc:
                return exc, max(0.0, time.perf_counter() - started)

        if pending_s0:
            remaining = self._remaining_node_starts()
            if remaining is not None:
                pending_s0 = pending_s0[:remaining]
            # Begin and finish bounded S0 windows together.  Starting every
            # node before collecting any Query makes later checkpoint
            # durations include the whole batch barrier instead of that
            # Query's own Mixed request.
            max_workers = min(self.query_concurrency, len(pending_s0))
            for offset in range(0, len(pending_s0), max_workers):
                window = pending_s0[offset : offset + max_workers]
                for spec, _run in window:
                    self._begin(spec, "s0")
                if max_workers == 1:
                    collected = [collect_s0(item) for item in window]
                else:
                    with ThreadPoolExecutor(max_workers=len(window)) as pool:
                        collected = list(pool.map(collect_s0, window))
                for (spec, run), value in zip(window, collected):
                    collected_value, execution_seconds = value
                    if isinstance(collected_value, S0CollectionError):
                        error = NodeResult.error(collected_value.error_code, collected_value.message)
                        error.telemetry = {"executionSeconds": round(execution_seconds, 6)}
                        run.final_status = "error"
                        run.nodes["s0"] = error
                        self._record(spec, "s0", error)
                        continue
                    if isinstance(collected_value, Exception):
                        error = NodeResult.error("S0_FAILED", self._error_text(collected_value))
                        error.telemetry = {"executionSeconds": round(execution_seconds, 6)}
                        run.final_status = "error"
                        run.nodes["s0"] = error
                        self._record(spec, "s0", error)
                        continue
                    run.s0 = collected_value
                    s0_result = NodeResult.executed(run.s0)
                    s0_result.telemetry = {"executionSeconds": round(execution_seconds, 6)}
                    run.nodes["s0"] = s0_result
                    self._record(spec, "s0", s0_result)

        self._yield_if_stage_incomplete(items, "s0")

        # Reorder uses the immutable S0 evidence collected above.  Queries
        # with four or fewer recalled products have no meaningful candidate
        # set for reordering and are skipped without another Mixed call.
        effective_items = list(items)
        for index, spec in enumerate(effective_items):
            run = results[spec.query_id]
            if run.s0 is None or spec.reorder_eligible is not True:
                continue
            result_count = run.s0.get("result_count")
            if (
                isinstance(result_count, int)
                and not isinstance(result_count, bool)
                and result_count <= 4
            ):
                spec = replace(spec, reorder_eligible=False,
                               reorder_skip_reason="S0 召回商品数≤4")
                effective_items[index] = spec
                results[spec.query_id].spec = spec
        items = effective_items

        # An S0/Mixed failure is terminal for the Query, but the remaining
        # deterministic nodes still need terminal checkpoint records so the
        # Query and batch cannot remain pending/running indefinitely.
        for spec in items:
            run = results[spec.query_id]
            if run.s0 is not None:
                continue
            s0_result = run.nodes.get("s0")
            if s0_result is None or s0_result.status is not NodeStatus.ERROR:
                continue
            for node in ("reorder", "relevance", "recall"):
                self._skip_node(spec, run, node, "s0_failed")

        reorder_items = self._bounded_specs(items, "reorder")

        if self.shared_context is not None and reorder_items:
            try:
                shared_contexts = self.shared_context.prepare(
                    batch_id=batch_id,
                    data_date=data_date,
                    brands=(
                        spec.brand
                        for spec in items
                        if results[spec.query_id].s0 is not None
                    ),
                )
            except Exception as exc:
                db_error_args = getattr(getattr(exc, "orig", None), "args", ())
                db_error_code = (
                    db_error_args[0]
                    if db_error_args and isinstance(db_error_args[0], int)
                    else None
                )
                logger.error(
                    "shared context preparation failed batch_id=%s data_date=%s "
                    "error_type=%s db_error_code=%s",
                    batch_id,
                    data_date.isoformat(),
                    type(exc).__name__,
                    db_error_code,
                )
                shared_context_error = True

        # Production scheduling does not pass precomputed preferences.  Run
        # the configured behavior-evidence/preference boundary exactly once
        # after shared context is ready and before any reorder node starts.
        # A provider failure is isolated to reorder results and must not stop
        # the subsequent relevance stage.
        if preference_results is None and self.preference_provider is not None and reorder_items:
            try:
                preference_results = self._generate_preference_results(
                    reorder_items,
                    shared_contexts=shared_contexts,
                    data_date=data_date,
                    batch_id=batch_id,
                )
            except Exception as exc:
                preference_results = {
                    spec.query_id: NodeResult.error(
                        "PREFERENCE_GENERATION_FAILED", self._error_text(exc)
                    )
                    for spec in reorder_items
                    if spec.reorder_eligible is not False
                }

        # Stage 2: every reorder node reaches a terminal state before stage 3.
        # VectorReorderBatchRunner is used when the production composition
        # exposes it; the checkpoint/action side effects remain serialized
        # below while independent vector requests run concurrently.
        if callable(getattr(self.reorder_batch_runner, "run_batch", None)):
            self._run_reorder_stage_batch(
                reorder_items,
                results,
                preference_results,
                shared_contexts,
                shared_context_error,
                snapshot,
                batch_query_ids,
            )
        else:
            for spec in reorder_items:
                run = results[spec.query_id]
                if run.s0 is None:
                    continue
                if spec.reorder_eligible is False:
                    self._record_reorder_skipped(
                        spec,
                        run,
                        spec.reorder_skip_reason or "CTR/CVR 正常",
                    )
                    continue
                preference_context = self._preference_context(spec, preference_results)
                if self.shared_context is None:
                    self._run_node(
                        spec,
                        run,
                        "reorder",
                        snapshot,
                        batch_query_ids,
                        preference_context,
                    )
                    continue
                context = shared_contexts.get(spec.brand)
                if context is None:
                    self._record_reorder_error(
                        spec,
                        run,
                        (
                            "SHARED_CONTEXT_FAILED"
                            if shared_context_error
                            else "SHARED_CONTEXT_UNAVAILABLE"
                        ),
                        (
                            "共享上下文采集或持久化失败"
                            if shared_context_error
                            else "共享上下文未生成"
                        ),
                    )
                else:
                    preference_context = {
                        **preference_context,
                        "shared_context": context,
                    }
                    self._run_node(
                        spec,
                        run,
                        "reorder",
                        snapshot,
                        batch_query_ids,
                        preference_context,
                    )

        self._yield_if_stage_incomplete(items, "reorder")
        relevance_items = self._bounded_specs(items, "relevance")

        # Stage 3: relevance starts after the reorder barrier, including
        # when one or more reorder nodes ended in error.  The batch runner is
        # optional so direct callers and legacy tests keep the single-Query
        # behavior unless the composition root wires the new boundary.
        if not self.relevance_enabled:
            for spec in relevance_items:
                self._skip_node(spec, results[spec.query_id], "relevance", "品牌未启用相关性检测")
        elif self.relevance_batch_runner is not None:
            enabled_items = [spec for spec in relevance_items if spec.relevance_enabled is not False]
            for spec in relevance_items:
                if spec.relevance_enabled is False:
                    self._skip_node(spec, results[spec.query_id], "relevance", "品牌未启用相关性检测")
            if enabled_items:
                for offset in range(0, len(enabled_items), self.relevance_query_batch_size):
                    self._run_relevance_stage_batch(
                        enabled_items[offset : offset + self.relevance_query_batch_size],
                        results,
                        snapshot,
                        batch_query_ids,
                    )
        else:
            for spec in relevance_items:
                if spec.relevance_enabled is False:
                    self._skip_node(spec, results[spec.query_id], "relevance", "品牌未启用相关性检测")
                else:
                    self._run_relevance_stage_single(spec, results[spec.query_id], snapshot, batch_query_ids)

        self._yield_if_stage_incomplete(items, "relevance")

        # Stage 4: run recall-rate detection + deterministic recall quantity check after relevance.
        for spec in self._bounded_specs(items, "recall"):
            run = results[spec.query_id]
            if run.s0 is None:
                continue
            if not self.recall_enabled or spec.recall_enabled is False:
                self._skip_node(spec, run, "recall", "品牌未启用召回数量检查")
                continue
            recall_context: dict[str, Any] = {"s0": run.s0}
            if self.recall_history is not None:
                try:
                    recall_context["history_baseline"] = self.recall_history.get_baseline(
                        spec,
                        data_date=data_date,
                        batch_id=self._active_batch_id or batch_id,
                    )
                except Exception as exc:
                    recall_context["history_error"] = self._error_text(exc)
            self._run_node(
                spec,
                run,
                "recall",
                snapshot,
                batch_query_ids,
                recall_context,
            )

        self._yield_if_stage_incomplete(items, "recall")

        if (
            self.checkpoint is not None
            and self._active_batch_id is not None
            and self.checkpoint.all_nodes_terminal(self._active_batch_id)
        ):
            checkpoint = self.checkpoint.finish_batch(
                self._active_batch_id, self._require_checkpoint_version()
            )
            self._checkpoint_version = checkpoint.version

        observer = self.experiment_observer
        if observer is not None:
            for spec in items:
                if spec.experiment_id is None or not spec.group_type:
                    continue
                try:
                    run = results[spec.query_id]
                    observer.record(
                        spec,
                        run,
                        data_date=data_date,
                        batch_id=batch_id,
                    )
                except Exception:
                    # Metrics must never alter or block governance outcomes.
                    continue

        return results

    def _run_reorder_stage_batch(
        self,
        specs: Sequence[QuerySpec],
        results: Mapping[str, QueryRunResult],
        preference_results: Mapping[str, Any] | None,
        shared_contexts: Mapping[str, Any],
        shared_context_error: bool,
        snapshot: NodeConfigSnapshot,
        batch_query_ids: Sequence[str],
    ) -> None:
        """Run vector reorder requests across Queries, then apply actions in order."""
        runner = self.reorder_batch_runner
        if runner is None:
            return
        pending: list[tuple[QuerySpec, QueryRunResult, Mapping[str, Any]]] = []
        for spec in specs:
            run = results[spec.query_id]
            if run.s0 is None:
                continue
            persisted = self._persisted(spec, "reorder")
            if persisted is not None and persisted.terminal and not self._should_retry_s0_skip(spec, persisted):
                self._set_node_result(run, "reorder", persisted)
                continue
            if persisted is not None and persisted.status is NodeStatus.RUNNING:
                # An interrupted action has Query-specific recovery state and
                # must not be mixed into a fresh cross-Query request.
                self._run_node(
                    spec,
                    run,
                    "reorder",
                    snapshot,
                    batch_query_ids,
                    self._reorder_context(spec, preference_results, shared_contexts),
                )
                continue
            if spec.reorder_eligible is False:
                self._record_reorder_skipped(
                    spec,
                    run,
                    spec.reorder_skip_reason or "CTR/CVR 正常",
                )
                continue
            preference_context = self._preference_context(spec, preference_results)
            if self.shared_context is not None:
                context = shared_contexts.get(spec.brand)
                if context is None:
                    self._record_reorder_error(
                        spec,
                        run,
                        "SHARED_CONTEXT_FAILED" if shared_context_error else "SHARED_CONTEXT_UNAVAILABLE",
                        "共享上下文采集或持久化失败" if shared_context_error else "共享上下文未生成",
                    )
                    continue
                preference_context = {**preference_context, "shared_context": context}
            self._begin(spec, "reorder")
            context = self._node_context(
                spec, "reorder", run, snapshot, batch_query_ids
            )
            context.update(preference_context)
            pending.append((spec, run, context))

        if not pending:
            return
        inputs = [(spec, context) for spec, _run, context in pending]
        try:
            batch_results = runner.run_batch(inputs)  # type: ignore[attr-defined]
            if not isinstance(batch_results, Mapping):
                raise TypeError("reorder batch runner returned an invalid mapping")
        except Exception as exc:
            batch_results = {
                spec.query_id: NodeResult.error(
                    "VECTOR_REORDER_BATCH_FAILED", self._error_text(exc)
                )
                for spec, _run, _context in pending
            }

        for spec, run, context in pending:
            outcome = batch_results.get(spec.query_id)
            if not isinstance(outcome, NodeResult):
                outcome = NodeResult.error(
                    "VECTOR_REORDER_BATCH_RESULT_MISSING", "批量重排序结果缺失"
                )
            if outcome.status is NodeStatus.ERROR or outcome.action is None:
                preference_result = self._preference_result_payload(
                    preference_results, spec.query_id
                )
                if preference_result is not None:
                    existing_result = outcome.result if isinstance(outcome.result, Mapping) else {}
                    outcome.result = {**preference_result, **dict(existing_result)}
                self._set_node_result(run, "reorder", outcome)
                self._record(spec, "reorder", outcome)
                continue
            action = outcome.action
            proposal_result = outcome.result
            vector_telemetry = dict(outcome.telemetry or {})
            try:
                if self.checkpoint is not None:
                    checkpoint = self.checkpoint.save_action(
                        self._require_batch_id(),
                        spec,
                        "reorder",
                        action,
                        self._require_checkpoint_version(),
                    )
                    self._checkpoint_version = checkpoint.version
                action_started = time.perf_counter()
                executed = self._execute_and_verify(spec, action, context)
                if not isinstance(executed, NodeResult):
                    raise TypeError("action gateway returned an invalid node result")
                executed.action = action
                executed.telemetry = {
                    **vector_telemetry,
                    **dict(executed.telemetry or {}),
                    "actionSeconds": round(
                        max(0.0, time.perf_counter() - action_started), 6
                    ),
                }
                if (
                    isinstance(action, Mapping)
                    and action.get("type") == "query_reorder"
                    and isinstance(proposal_result, Mapping)
                ):
                    action_result = executed.result
                    executed.result = {
                        **dict(proposal_result),
                        **(dict(action_result) if isinstance(action_result, Mapping) else {}),
                        "action_execution": action_result,
                    }
                outcome = executed
            except Exception as exc:
                outcome = NodeResult.error("REORDER_FAILED", self._error_text(exc))
                outcome.action = action
                outcome.result = dict(proposal_result) if isinstance(proposal_result, Mapping) else None
                outcome.telemetry = vector_telemetry
            self._set_node_result(run, "reorder", outcome)
            self._record(spec, "reorder", outcome)

    def _reorder_context(
        self,
        spec: QuerySpec,
        preference_results: Mapping[str, Any] | None,
        shared_contexts: Mapping[str, Any],
    ) -> dict[str, Any]:
        context = self._preference_context(spec, preference_results)
        if self.shared_context is not None and spec.brand in shared_contexts:
            context = {**context, "shared_context": shared_contexts[spec.brand]}
        return context

    def _run_relevance_stage_single(
        self,
        spec: QuerySpec,
        run: QueryRunResult,
        snapshot: NodeConfigSnapshot,
        batch_query_ids: Sequence[str],
    ) -> None:
        """Run the legacy per-Query relevance stage, unchanged in semantics."""
        if run.s0 is None:
            return
        persisted = self._persisted(spec, "relevance")
        if persisted is not None and persisted.terminal and not self._should_retry_s0_skip(spec, persisted):
            self._set_node_result(run, "relevance", persisted)
            return
        relevance_resume_state: Mapping[str, Any] | None = None
        relevance_pending_action: Mapping[str, Any] | None = None
        relevance_already_started = False
        if persisted is not None and persisted.status is NodeStatus.RUNNING:
            inflight = self.checkpoint.get_inflight_action(
                self._require_batch_id(), spec, "relevance"
            ) if self.checkpoint is not None else None
            checkpoint_action = (
                inflight.action
                if inflight is not None and isinstance(inflight.action, Mapping)
                else None
            )
            if isinstance(checkpoint_action, Mapping):
                candidate_state = checkpoint_action.get("_relevance_checkpoint")
                if isinstance(candidate_state, Mapping):
                    relevance_resume_state = candidate_state
            try:
                recovered = self._recover_interrupted_action(spec, run, "relevance")
            except Exception as exc:
                recovered = NodeResult.error(
                    "ACTION_RECOVERY_FAILED", self._error_text(exc)
                )
            if self._relevance_independent_actions and relevance_resume_state is not None:
                aggregate = deepcopy(relevance_resume_state.get("aggregate") or {})
                if recovered is None:
                    context = self._node_context(spec, "relevance", run, snapshot, batch_query_ids)
                    recovered = self._handle_independent_relevance_actions(spec, aggregate, context)
                else:
                    aggregate["blacklist_execution_mode"] = self._relevance_blacklist_execution_mode
                    aggregate["blacklist_action_result"] = recovered.result
                    self._enqueue_relevance_kg(spec, aggregate)
                    if recovered.status is NodeStatus.ERROR:
                        aggregate["error"] = {"code": recovered.error_code, "message": recovered.error_message}
                    recovered.result = aggregate
                self._set_node_result(run, "relevance", recovered)
                self._record(spec, "relevance", recovered)
                return
            if recovered is not None:
                recovered_payload = (
                    recovered.result
                    if recovered.status is NodeStatus.EXECUTED
                    and isinstance(recovered.result, Mapping)
                    else None
                )
                if not (
                    recovered.status is NodeStatus.EXECUTED
                    and relevance_resume_state is not None
                    and isinstance(recovered_payload, Mapping)
                    and recovered_payload.get("relevance_checkpoint") is not None
                ):
                    self._set_node_result(run, "relevance", recovered)
                    self._record(spec, "relevance", recovered)
                    return
                relevance_resume_state = dict(recovered_payload["relevance_checkpoint"])
                if isinstance(checkpoint_action, Mapping):
                    relevance_resume_state["search_effect_product_ids"] = list(
                        checkpoint_action.get("product_ids")
                        or checkpoint_action.get("product_id")
                        or []
                    )
                relevance_already_started = True
            elif checkpoint_action is not None and relevance_resume_state is not None:
                relevance_pending_action = checkpoint_action
                relevance_already_started = True
        if not relevance_already_started:
            self._begin(spec, "relevance")
            relevance_already_started = True
        try:
            current_products = list(self.search.mixed(spec.brand, spec.query))
        except Exception as exc:
            outcome = NodeResult.error(
                "RELEVANCE_SEARCH_FAILED", self._error_text(exc)
            )
            self._set_node_result(run, "relevance", outcome)
            self._record(spec, "relevance", outcome)
            return
        self._run_relevance(
            spec,
            run,
            snapshot,
            batch_query_ids,
            current_products,
            already_started=relevance_already_started,
            resume_state=relevance_resume_state,
            pending_action=relevance_pending_action,
        )

    def _run_relevance_stage_batch(
        self,
        specs: Sequence[QuerySpec],
        results: Mapping[str, QueryRunResult],
        snapshot: NodeConfigSnapshot,
        batch_query_ids: Sequence[str],
    ) -> None:
        """Run the first relevance round across complete Query groups.

        Each Query still owns its checkpoint, blacklist action and optional
        follow-up rounds.  Only the model invocation for the current Mixed
        products is packed across Queries.
        """
        runner = self.relevance_batch_runner
        if runner is None:
            return
        candidates: list[tuple[QuerySpec, QueryRunResult, list[Mapping[str, Any]]]] = []
        pending_searches: list[tuple[QuerySpec, QueryRunResult]] = []
        for spec in specs:
            run = results[spec.query_id]
            if run.s0 is None:
                continue
            persisted = self._persisted(spec, "relevance")
            if persisted is not None and persisted.terminal and not self._should_retry_s0_skip(spec, persisted):
                self._set_node_result(run, "relevance", persisted)
                continue
            # Interrupted actions contain Query-specific checkpoint state and
            # must use the recovery path before a new batch request is formed.
            if persisted is not None and persisted.status is NodeStatus.RUNNING:
                inflight = (
                    self.checkpoint.get_inflight_action(
                        self._require_batch_id(), spec, "relevance"
                    )
                    if self.checkpoint is not None
                    else None
                )
                if inflight is not None:
                    self._run_relevance_stage_single(
                        spec, run, snapshot, batch_query_ids
                    )
                    continue
                # A process can stop after begin_node (or during model
                # preparation) before an external action is checkpointed. It
                # is safe to retry that Query in the bounded cross-Query batch;
                # action-bearing interruptions must stay on the recovery path.
            self._begin(spec, "relevance")
            pending_searches.append((spec, run))

        def collect_products(item: tuple[QuerySpec, QueryRunResult]):
            spec, _run = item
            try:
                return list(self.search.mixed(spec.brand, spec.query))
            except Exception as exc:
                return exc

        if pending_searches:
            max_workers = min(self.query_concurrency, len(pending_searches))
            if max_workers == 1:
                product_results = [collect_products(item) for item in pending_searches]
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    product_results = list(pool.map(collect_products, pending_searches))
            for (spec, run), products in zip(pending_searches, product_results):
                if isinstance(products, Exception):
                    outcome = NodeResult.error(
                        "RELEVANCE_SEARCH_FAILED", self._error_text(products)
                    )
                    self._set_node_result(run, "relevance", outcome)
                    self._record(spec, "relevance", outcome)
                    continue
                try:
                    config = self.relevance_config_provider(spec.brand)
                    if isinstance(config, Mapping):
                        config = RelevanceRuntimeConfig.from_mapping(config)
                    if not isinstance(config, RelevanceRuntimeConfig):
                        raise TypeError("相关性配置提供器返回值无效")
                    products = filter_relevance_products(
                        products,
                        product_limit=config.product_limit,
                        configured_buckets=config.buckets,
                    )
                except Exception as exc:
                    outcome = NodeResult.error(
                        "RELEVANCE_CONFIG_FAILED", self._error_text(exc)
                    )
                    self._set_node_result(run, "relevance", outcome)
                    self._record(spec, "relevance", outcome)
                    continue
                candidates.append((spec, run, products))

        if not candidates:
            return
        first_spec = candidates[0][0]
        shared_context = self._node_context(
            first_spec,
            "relevance",
            candidates[0][1],
            snapshot,
            batch_query_ids,
        )
        inputs = [
            RelevanceBatchInput(
                spec=spec,
                products=products,
                context=self._node_context(
                    spec, "relevance", run, snapshot, batch_query_ids
                ),
            )
            for spec, run, products in candidates
        ]
        try:
            batch_results = runner.run(inputs, shared_context)
            if not isinstance(batch_results, Mapping):
                raise TypeError("relevance batch runner returned an invalid mapping")
        except Exception as exc:
            batch_results = {
                spec.query: NodeResult.error(
                    "RELEVANCE_BATCH_FAILED", self._error_text(exc)
                )
                for spec, _, _ in candidates
            }

        for spec, run, products in candidates:
            outcome = batch_results.get(spec.query)
            if not isinstance(outcome, NodeResult):
                outcome = NodeResult.error(
                    "RELEVANCE_BATCH_RESULT_MISSING", "批量相关性结果缺失"
                )
            if outcome.status is NodeStatus.ERROR:
                self._set_node_result(run, "relevance", outcome)
                self._record(spec, "relevance", outcome)
                continue
            payload = outcome.result if isinstance(outcome.result, Mapping) else {}
            aggregate: dict[str, Any] = {
                "query": spec.query,
                "product_count": 0,
                "reused_product_ids": [],
                "requested_product_ids": [],
                "detected_product_ids": [],
                "relevant_product_ids": [],
                "blacklist_candidates": [],
                "warnings": [],
                "rounds": [],
                "telemetry": {
                    "batch_count": 0,
                    "product_count": 0,
                    "image_download_success_count": 0,
                    "image_download_failure_count": 0,
                },
            }
            self._merge_relevance_result(aggregate, payload)
            first_round_telemetry = dict(getattr(outcome, "telemetry", {}) or {})
            first_round_telemetry.setdefault(
                "roundSeconds",
                round(
                    float(first_round_telemetry.get("imagePreparationSeconds") or 0)
                    + float(first_round_telemetry.get("batchModelSeconds") or 0),
                    6,
                ),
            )
            aggregate["rounds"].append({
                "round": 1,
                "requested_product_ids": list(payload.get("requested_product_ids") or []),
                "detected_product_ids": list(payload.get("detected_product_ids") or []),
                "blacklisted_product_ids": [
                    str(item.get("product_id"))
                    for item in payload.get("blacklist_candidates") or []
                    if isinstance(item, Mapping) and str(item.get("product_id") or "").strip()
                ],
                "telemetry": first_round_telemetry,
            })
            seen = {
                str(value).strip()
                for key in ("detected_product_ids", "reused_product_ids")
                for value in aggregate.get(key) or []
                if str(value).strip()
            }
            try:
                config = self.relevance_config_provider(spec.brand)
                if isinstance(config, Mapping):
                    config = RelevanceRuntimeConfig.from_mapping(config)
                if not isinstance(config, RelevanceRuntimeConfig):
                    raise TypeError("相关性配置提供器返回值无效")
            except Exception as exc:
                failed = NodeResult.error(
                    "RELEVANCE_CONFIG_FAILED", self._error_text(exc)
                )
                self._set_node_result(run, "relevance", failed)
                self._record(spec, "relevance", failed)
                continue
            detected = set(aggregate["detected_product_ids"])
            blacklisted = {
                str(item.get("product_id"))
                for item in aggregate["blacklist_candidates"]
                if isinstance(item, Mapping) and str(item.get("product_id") or "").strip()
            }
            aggregate["blacklist_ratio"] = round(len(blacklisted) / len(detected), 4) if detected else 0.0
            if self._relevance_independent_actions:
                context = self._node_context(spec, "relevance", run, snapshot, batch_query_ids)
                context["current_products"] = products
                result = self._handle_independent_relevance_actions(spec, aggregate, context)
                result.telemetry = {**dict(getattr(outcome, "telemetry", {})), **result.telemetry}
                self._set_node_result(run, "relevance", result)
                self._record(spec, "relevance", result)
                continue
            action = self._relevance_blacklist_action(spec, aggregate)
            if action is None:
                if blacklisted:
                    self._enqueue_relevance_kg(spec, aggregate)
                result = NodeResult.executed(aggregate)
                result.telemetry = dict(getattr(outcome, "telemetry", {}))
                self._set_node_result(run, "relevance", result)
                self._record(spec, "relevance", result)
                continue
            action_context = self._node_context(
                spec, "relevance", run, snapshot, batch_query_ids
            )
            action_context["current_products"] = products
            mode_decision = decide_blacklist_handling(
                aggregate["blacklist_candidates"],
                execution_mode=self._relevance_blacklist_execution_mode,
            )
            action["execution_mode"] = mode_decision["executionMode"]
            if not mode_decision["shouldExecute"]:
                aggregate["blacklist_execution_mode"] = mode_decision["executionMode"]
                if mode_decision["shouldPersistPending"]:
                    self._record_pending_relevance_action(spec, action, action_context)
                self._enqueue_relevance_kg(spec, aggregate)
                result = NodeResult.executed(aggregate, action=action)
                result.telemetry = dict(getattr(outcome, "telemetry", {}))
                self._set_node_result(run, "relevance", result)
                self._record(spec, "relevance", result)
                continue
            action_started = time.perf_counter()
            try:
                if self.checkpoint is not None:
                    checkpoint_action = dict(action)
                    checkpoint_action["_relevance_checkpoint"] = {
                        "aggregate": deepcopy(aggregate),
                        "seen_product_ids": sorted(seen),
                        "round_index": 1,
                    }
                    checkpoint = self.checkpoint.save_action(
                        self._require_batch_id(),
                        spec,
                        "relevance",
                        checkpoint_action,
                        self._require_checkpoint_version(),
                    )
                    self._checkpoint_version = checkpoint.version
                executed = self._execute_and_verify(spec, action, action_context)
                if not isinstance(executed, NodeResult):
                    raise TypeError("action gateway returned an invalid node result")
            except Exception as exc:
                executed = NodeResult.error("RELEVANCE_BLACKLIST_FAILED", self._error_text(exc))
            action_seconds = round(max(0.0, time.perf_counter() - action_started), 6)
            gateway_result = getattr(executed, "result", {})
            gateway_result = gateway_result if isinstance(gateway_result, Mapping) else {}
            gateway_payload = gateway_result.get("gateway")
            gateway_payload = gateway_payload if isinstance(gateway_payload, Mapping) else {}
            gateway_write_seconds = gateway_payload.get("writeSeconds")
            gateway_readback_seconds = gateway_payload.get("readbackSeconds")
            if executed.status is NodeStatus.ERROR or executed.status is not NodeStatus.EXECUTED:
                aggregate["error"] = {
                    "code": executed.error_code or "RELEVANCE_BLACKLIST_FAILED",
                    "message": executed.error_message or "商品拉黑动作未完成",
                }
                failed = NodeResult.error(
                    aggregate["error"]["code"], aggregate["error"]["message"]
                )
                failed.result = aggregate
                failed.action = action
                failed.telemetry = {
                    **dict(getattr(outcome, "telemetry", {})),
                    "actionSeconds": action_seconds,
                    "blacklistWriteSeconds": gateway_write_seconds,
                    "blacklistReadbackSeconds": gateway_readback_seconds,
                    "recheckSeconds": 0.0,
                }
                self._set_node_result(run, "relevance", failed)
                self._record(spec, "relevance", failed)
                continue
            time.sleep(5.0)
            recheck_started = time.perf_counter()
            try:
                next_products = list(self.search.mixed(spec.brand, spec.query))
            except Exception as exc:
                recheck_seconds = round(max(0.0, time.perf_counter() - recheck_started), 6)
                failed = NodeResult.error("RELEVANCE_SEARCH_FAILED", self._error_text(exc))
                failed.result = aggregate
                failed.action = action
                failed.telemetry = {
                    **dict(getattr(outcome, "telemetry", {})),
                    "actionSeconds": action_seconds,
                    "blacklistWriteSeconds": gateway_write_seconds,
                    "blacklistReadbackSeconds": gateway_readback_seconds,
                    "recheckSeconds": recheck_seconds,
                }
                self._set_node_result(run, "relevance", failed)
                self._record(spec, "relevance", failed)
                continue
            recheck_seconds = round(max(0.0, time.perf_counter() - recheck_started), 6)
            first_round_telemetry["actionSeconds"] = action_seconds
            first_round_telemetry["blacklistWriteSeconds"] = gateway_write_seconds
            first_round_telemetry["blacklistReadbackSeconds"] = gateway_readback_seconds
            first_round_telemetry["recheckSeconds"] = recheck_seconds
            first_round_telemetry["roundSeconds"] = round(
                float(first_round_telemetry.get("roundSeconds") or 0)
                + action_seconds
                + recheck_seconds,
                6,
            )
            self._run_relevance(
                spec,
                run,
                snapshot,
                batch_query_ids,
                next_products,
                already_started=True,
                resume_state={
                    "aggregate": aggregate,
                    "seen_product_ids": sorted(seen),
                    "round_index": 1,
                    "search_effect_product_ids": list(action.get("product_ids") or [])
                    if isinstance(action, Mapping)
                    else [],
                    "telemetry": first_round_telemetry,
                },
            )

    def _relevance_blacklist_action(
        self,
        spec: QuerySpec, result: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        candidates = [
            item for item in result.get("blacklist_candidates") or []
            if isinstance(item, Mapping) and str(item.get("product_id") or "").strip()
        ]
        if not candidates:
            return None
        action: dict[str, Any] = {
            "type": "product_blacklist",
            "brand": spec.brand,
            "query": spec.query,
            "product_ids": list(dict.fromkeys(str(item["product_id"]) for item in candidates)),
            "product_names": {
                str(item["product_id"]): str(item.get("product_name") or item.get("productName") or "")
                for item in candidates
            },
            "product_images": {
                str(item["product_id"]): str(item.get("image_url") or item.get("imageUrl") or "")
                for item in candidates
            },
            "reasons": {str(item["product_id"]): item.get("reason") for item in candidates},
            "execution_mode": self._relevance_blacklist_execution_mode,
        }
        product_buckets = {
            str(item["product_id"]): str(item.get("bucket") or "").strip()
            for item in candidates
            if str(item.get("bucket") or "").strip()
        }
        if product_buckets:
            action["product_buckets"] = product_buckets
        return action

    def _handle_independent_relevance_actions(
        self, spec: QuerySpec, aggregate: dict[str, Any], context: Mapping[str, Any],
    ) -> NodeResult:
        """Blacklist detection errors without waiting for root-cause analysis."""
        action = self._relevance_blacklist_action(spec, aggregate)
        if action is None:
            aggregate["blacklist_execution_mode"] = "none"
            return NodeResult.executed(aggregate)
        aggregate["blacklist_execution_mode"] = self._relevance_blacklist_execution_mode
        started = time.perf_counter()
        result = NodeResult.executed(aggregate, action=action)
        try:
            mode = self._relevance_blacklist_execution_mode
            if mode == "human_approval":
                self._record_pending_relevance_action(spec, action, context)
            elif mode == "auto":
                if self.checkpoint is not None:
                    checkpoint_action = dict(action)
                    checkpoint_action["_relevance_checkpoint"] = {
                        "aggregate": deepcopy(aggregate),
                        "seen_product_ids": list(aggregate.get("detected_product_ids") or []),
                        "round_index": 1,
                    }
                    checkpoint = self.checkpoint.save_action(
                        self._require_batch_id(), spec, "relevance", checkpoint_action,
                        self._require_checkpoint_version(),
                    )
                    self._checkpoint_version = checkpoint.version
                executed = self.action_gateway.execute_and_verify(spec, action, context)
                if not isinstance(executed, NodeResult):
                    raise TypeError("action gateway returned an invalid node result")
                aggregate["blacklist_action_result"] = executed.result
                if aggregate.get("rounds"):
                    aggregate["rounds"][-1]["action_result"] = executed.result
                if executed.status is not NodeStatus.EXECUTED:
                    result = NodeResult.error(
                        executed.error_code or "RELEVANCE_BLACKLIST_FAILED",
                        executed.error_message or "商品拉黑动作未完成",
                    )
        except Exception as exc:
            result = NodeResult.error("RELEVANCE_BLACKLIST_FAILED", self._error_text(exc))
        finally:
            # Both branches retain their own outcome even when the other fails.
            self._enqueue_relevance_kg(spec, aggregate)
        if result.status is NodeStatus.ERROR:
            aggregate["error"] = {"code": result.error_code, "message": result.error_message}
        result.result = aggregate
        result.action = action
        result.telemetry = {"actionSeconds": round(max(0.0, time.perf_counter() - started), 6)}
        return result

    @staticmethod
    def _blacklist_search_effect(
        action_or_ids: Mapping[str, Any] | Sequence[str],
        products: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        if isinstance(action_or_ids, Mapping):
            raw_ids = action_or_ids.get("product_ids") or action_or_ids.get("product_id") or []
        else:
            raw_ids = action_or_ids
        if isinstance(raw_ids, (str, bytes)):
            raw_ids = [raw_ids]
        target_ids = [str(value).strip() for value in raw_ids if str(value).strip()]
        observed = [
            product_id
            for product in products
            if (product_id := _workflow_product_id(product))
            and product_id in target_ids
        ]
        if not observed:
            return None
        return {
            "status": "failed",
            "observed_product_ids": list(dict.fromkeys(observed)),
        }

    # The original single-Query loop is now isolated in
    # ``_run_relevance_stage_single`` above; the batch path shares all later
    # round/checkpoint logic through ``_run_relevance``.
    def _legacy_stage_placeholder(self) -> None:
        return None

    def _run_node(
        self,
        spec: QuerySpec,
        run: QueryRunResult,
        node: str,
        model_config_snapshot: NodeConfigSnapshot,
        batch_query_ids: Sequence[str],
        node_context: Mapping[str, Any] | None = None,
        *,
        already_started: bool = False,
    ) -> None:
        persisted = self._persisted(spec, node)
        if persisted is not None and persisted.terminal and not self._should_retry_s0_skip(spec, persisted):
            self._set_node_result(run, node, persisted)
            return
        if persisted is not None and persisted.status is NodeStatus.RUNNING:
            try:
                recovered = self._recover_interrupted_action(spec, run, node)
            except Exception as exc:
                recovered = NodeResult.error(
                    "ACTION_RECOVERY_FAILED", self._error_text(exc)
                )
            if recovered is not None:
                self._set_node_result(run, node, recovered)
                self._record(spec, node, recovered)
                return
        if not already_started:
            self._begin(spec, node)
        context = self._node_context(
            spec, node, run, model_config_snapshot, batch_query_ids
        )
        context.update(node_context or {})
        execution_started = time.perf_counter()
        action_started: float | None = None
        action = None
        try:
            outcome = self.runners[node].run(spec, context)
            if not isinstance(outcome, NodeResult):
                raise TypeError(
                    f"{node} runner returned {type(outcome).__name__}"
                )
            if outcome.action is not None:
                action = outcome.action
                proposal_result = outcome.result
                if self.checkpoint is not None:
                    checkpoint = self.checkpoint.save_action(
                        self._require_batch_id(),
                        spec,
                        node,
                        action,
                        self._require_checkpoint_version(),
                    )
                    self._checkpoint_version = checkpoint.version
                action_started = time.perf_counter()
                outcome = self._execute_and_verify(
                    spec, action, context
                )
                if not isinstance(outcome, NodeResult):
                    raise TypeError(
                        "action gateway returned an invalid node result"
                    )
                outcome.action = action
                if (
                    isinstance(action, Mapping)
                    and action.get("type") == "query_reorder"
                    and isinstance(proposal_result, Mapping)
                ):
                    action_result = outcome.result
                    outcome.result = {
                        **dict(proposal_result),
                        **(
                            dict(action_result)
                            if isinstance(action_result, Mapping)
                            else {}
                        ),
                        "action_execution": action_result,
                    }
        except Exception as exc:  # isolate failures to the current Query/node
            outcome = NodeResult.error(
                f"{node.upper()}_FAILED", self._error_text(exc)
            )
        outcome.telemetry = {
            **getattr(outcome, "telemetry", {}),
            "executionSeconds": round(max(0.0, time.perf_counter() - execution_started), 6),
            "actionSeconds": (
                round(max(0.0, time.perf_counter() - action_started), 6)
                if action_started is not None
                else None
            ),
        }
        self._set_node_result(run, node, outcome)
        self._record(spec, node, outcome)

    def _node_context(
        self,
        spec: QuerySpec,
        node: str,
        run: QueryRunResult,
        model_config_snapshot: NodeConfigSnapshot,
        batch_query_ids: Sequence[str],
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "nodes": run.nodes,
            "model_config_snapshot": model_config_snapshot,
            "async_tasks": self.async_tasks,
            "batch_id": self._active_batch_id,
            "data_date": self._active_data_date,
        }
        if self._trace_batch is not None:
            langfuse_config = self._trace_batch.query_config(
                spec.query_id,
                node,
                model_config_version=model_config_snapshot.version,
            )
            context["langfuse_config"] = langfuse_config
            context["callbacks"] = langfuse_config["callbacks"]
            context["langfuse_trace_context"] = self._trace_batch.query_trace_context(
                spec.query_id,
                node,
                model_config_version=model_config_snapshot.version,
            )
            context["langfuse_batch_config"] = self._trace_batch.batch_config(
                node,
                batch_query_ids,
                extra_metadata={"model_config_version": model_config_snapshot.version},
            )
        return context

    def _run_relevance(
        self,
        spec: QuerySpec,
        run: QueryRunResult,
        model_config_snapshot: NodeConfigSnapshot,
        batch_query_ids: Sequence[str],
        current_products: Sequence[Mapping[str, Any]],
        *,
        already_started: bool = False,
        resume_state: Mapping[str, Any] | None = None,
        pending_action: Mapping[str, Any] | None = None,
    ) -> None:
        """Run relevance, applying blacklist actions and checking new fill-ins."""
        if not already_started:
            self._begin(spec, "relevance")
        try:
            config = self.relevance_config_provider(spec.brand)
            if isinstance(config, Mapping):
                config = RelevanceRuntimeConfig.from_mapping(config)
            if not isinstance(config, RelevanceRuntimeConfig):
                raise TypeError("相关性配置提供器返回值无效")
        except Exception as exc:
            outcome = NodeResult.error(
                "RELEVANCE_CONFIG_FAILED", self._error_text(exc)
            )
            self._set_node_result(run, "relevance", outcome)
            self._record(spec, "relevance", outcome)
            return
        max_rounds = self._relevance_max_rounds
        if max_rounds is None:
            max_rounds = config.max_rounds
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds <= 0:
            outcome = NodeResult.error(
                "RELEVANCE_CONFIG_INVALID", "相关性最大复检轮数必须为正整数"
            )
            self._set_node_result(run, "relevance", outcome)
            self._record(spec, "relevance", outcome)
            return

        seen: set[str] = {
            str(value).strip()
            for value in (resume_state.get("seen_product_ids") or [])
            if str(value).strip()
        } if isinstance(resume_state, Mapping) else set()
        initial_aggregate = (
            deepcopy(resume_state.get("aggregate"))
            if isinstance(resume_state, Mapping)
            and isinstance(resume_state.get("aggregate"), Mapping)
            else None
        )
        aggregate: dict[str, Any] = initial_aggregate or {
            "query": spec.query,
            "product_count": 0,
            "reused_product_ids": [],
            "requested_product_ids": [],
            "detected_product_ids": [],
            "relevant_product_ids": [],
            "blacklist_candidates": [],
            "warnings": [],
            "rounds": [],
        }
        last_action: Any = None
        executed_actions: list[Mapping[str, Any]] = []
        raw_products = list(current_products)
        products = filter_relevance_products(
            raw_products,
            product_limit=config.product_limit,
            configured_buckets=config.buckets,
        )
        final_error: NodeResult | None = None
        prior_action_product_ids = (
            [
                str(value).strip()
                for value in (resume_state.get("search_effect_product_ids") or [])
                if str(value).strip()
            ]
            if isinstance(resume_state, Mapping)
            else []
        )
        if prior_action_product_ids:
            search_effect = self._blacklist_search_effect(
                prior_action_product_ids,
                raw_products,
            )
            if search_effect is not None:
                if aggregate.get("rounds") and isinstance(aggregate["rounds"][-1], Mapping):
                    aggregate["rounds"][-1] = {
                        **dict(aggregate["rounds"][-1]),
                        "search_effect": search_effect,
                    }
                aggregate["search_effect"] = search_effect
                final_error = NodeResult.error(
                    "RELEVANCE_BLACKLIST_SEARCH_EFFECT_NOT_VERIFIED",
                    "拉黑商品仍存在于实时 Mixed 召回结果中",
                )
        telemetry: dict[str, Any] = {
            "actionSeconds": 0.0,
            "recheckSeconds": 0.0,
        }
        start_round = 1
        if isinstance(resume_state, Mapping):
            try:
                start_round = max(1, int(resume_state.get("round_index") or 0) + 1)
            except (TypeError, ValueError):
                start_round = 1
        if pending_action is not None:
            last_action = pending_action
            action_context = self._node_context(
                spec, "relevance", run, model_config_snapshot, batch_query_ids
            )
            action_context["current_products"] = products
            mode_decision = decide_blacklist_handling(
                [{"product_id": product_id} for product_id in pending_action.get("product_ids") or []]
                if isinstance(pending_action, Mapping)
                else [],
                execution_mode=self._relevance_blacklist_execution_mode,
            )
            if not mode_decision["shouldExecute"]:
                if mode_decision["shouldPersistPending"]:
                    self._record_pending_relevance_action(spec, pending_action, action_context)
                aggregate["blacklist_execution_mode"] = mode_decision["executionMode"]
                result = NodeResult.executed(aggregate, action=pending_action)
                self._set_node_result(run, "relevance", result)
                self._record(spec, "relevance", result)
                return
            action_started = time.perf_counter()
            try:
                executed = self._execute_and_verify(
                    spec, pending_action, action_context
                )
                if not isinstance(executed, NodeResult):
                    raise TypeError("action gateway returned an invalid node result")
            except Exception as exc:
                action_seconds = round(max(0.0, time.perf_counter() - action_started), 6)
                final_error = NodeResult.error(
                    "RELEVANCE_BLACKLIST_FAILED", self._error_text(exc)
                )
            else:
                action_seconds = round(max(0.0, time.perf_counter() - action_started), 6)
                if executed.status is NodeStatus.ERROR:
                    final_error = executed
                elif executed.status is not NodeStatus.EXECUTED:
                    final_error = NodeResult.error(
                        "RELEVANCE_BLACKLIST_FAILED", "商品拉黑动作未完成"
                    )
                else:
                    executed_actions.append(dict(pending_action))
                    time.sleep(5.0)
                    recheck_started = time.perf_counter()
                    try:
                        raw_products = list(self.search.mixed(spec.brand, spec.query))
                        products = filter_relevance_products(
                            raw_products,
                            product_limit=config.product_limit,
                            configured_buckets=config.buckets,
                        )
                    except Exception as exc:
                        recheck_seconds = round(max(0.0, time.perf_counter() - recheck_started), 6)
                        final_error = NodeResult.error(
                            "RELEVANCE_SEARCH_FAILED", self._error_text(exc)
                        )
                    else:
                        recheck_seconds = round(max(0.0, time.perf_counter() - recheck_started), 6)
                        search_effect = self._blacklist_search_effect(
                            pending_action,
                            raw_products,
                        )
                        if search_effect is not None:
                            aggregate["search_effect"] = search_effect
                            if aggregate.get("rounds") and isinstance(aggregate["rounds"][-1], Mapping):
                                aggregate["rounds"][-1] = {
                                    **dict(aggregate["rounds"][-1]),
                                    "search_effect": search_effect,
                                }
                            final_error = NodeResult.error(
                                "RELEVANCE_BLACKLIST_SEARCH_EFFECT_NOT_VERIFIED",
                                "拉黑商品仍存在于实时 Mixed 召回结果中",
                            )
                    telemetry["actionSeconds"] += action_seconds
                    telemetry["recheckSeconds"] += recheck_seconds
                    telemetry["roundSeconds"] = round(
                        float(telemetry.get("roundSeconds") or 0)
                        + action_seconds
                        + recheck_seconds,
                        6,
                    )
        for round_index in range(start_round, max_rounds + 1):
            if final_error is not None:
                break
            round_started = time.perf_counter()
            eligible = [
                item for item in products
                if _workflow_product_id(item) and _workflow_product_id(item) not in seen
            ]
            if not eligible:
                # Give the node a terminal result even when Mixed has no
                # products; concrete relevance runners use this to persist a
                # zero-count observation without invoking the model.
                if round_index == 1:
                    context = self._node_context(
                        spec, "relevance", run, model_config_snapshot, batch_query_ids
                    )
                    context["current_products"] = products
                    context["skip_product_ids"] = sorted(seen)
                    try:
                        empty_outcome = self.runners["relevance"].run(spec, context)
                        if isinstance(empty_outcome, NodeResult):
                            empty_payload = empty_outcome.result
                            if isinstance(empty_payload, Mapping):
                                self._merge_relevance_result(aggregate, empty_payload)
                            if empty_outcome.status is NodeStatus.ERROR:
                                final_error = empty_outcome
                            aggregate["rounds"].append(
                                {
                                    "round": round_index,
                                    "telemetry": {
                                        **dict(getattr(empty_outcome, "telemetry", {}) or {}),
                                        "roundSeconds": round(max(0.0, time.perf_counter() - round_started), 6),
                                    },
                                }
                            )
                    except Exception as exc:
                        final_error = NodeResult.error(
                            "RELEVANCE_FAILED", self._error_text(exc)
                        )
                break
            context = self._node_context(
                spec, "relevance", run, model_config_snapshot, batch_query_ids
            )
            context["current_products"] = eligible
            context["skip_product_ids"] = sorted(seen)
            try:
                outcome = self.runners["relevance"].run(spec, context)
                if not isinstance(outcome, NodeResult):
                    raise TypeError("relevance runner returned an invalid node result")
            except Exception as exc:
                final_error = NodeResult.error("RELEVANCE_FAILED", self._error_text(exc))
                break
            if not outcome.terminal:
                final_error = NodeResult.error(
                    "RELEVANCE_RESULT_INVALID", "相关性节点未返回终态"
                )
                break
            outcome_telemetry = dict(getattr(outcome, "telemetry", {}) or {})
            _accumulate_telemetry(telemetry, outcome_telemetry)
            round_payload = outcome.result if isinstance(outcome.result, Mapping) else {}
            self._merge_relevance_result(aggregate, round_payload)
            round_telemetry = dict(outcome_telemetry)
            batch_round = int(round_telemetry.get("batchQueryCount") or 1)
            measured_round_seconds = max(0.0, time.perf_counter() - round_started)
            if batch_round > 1:
                measured_round_seconds += float(round_telemetry.get("imagePreparationSeconds") or 0)
            round_telemetry["roundSeconds"] = round(measured_round_seconds, 6)
            round_record = {
                "round": round_index,
                "requested_product_ids": list(round_payload.get("requested_product_ids") or []),
                "detected_product_ids": list(round_payload.get("detected_product_ids") or []),
                "blacklisted_product_ids": [
                    str(item.get("product_id"))
                    for item in round_payload.get("blacklist_candidates") or []
                    if isinstance(item, Mapping) and str(item.get("product_id") or "").strip()
                ],
                "telemetry": round_telemetry,
            }
            for key in ("detected_product_ids", "reused_product_ids"):
                seen.update(
                    str(value).strip()
                    for value in round_payload.get(key) or []
                    if str(value).strip()
                )
            if outcome.status is NodeStatus.ERROR:
                final_error = outcome
                aggregate["rounds"].append(round_record)
                break
            action = outcome.action
            if self._relevance_independent_actions:
                aggregate["rounds"].append(round_record)
                result = self._handle_independent_relevance_actions(spec, aggregate, context)
                result.telemetry = {**telemetry, **result.telemetry}
                self._set_node_result(run, "relevance", result)
                self._record(spec, "relevance", result)
                return
            if action is None:
                aggregate["rounds"].append(round_record)
                break
            if isinstance(action, Mapping) and action.get("type") == "product_blacklist":
                action = dict(action)
                candidates = [
                    item for item in round_payload.get("blacklist_candidates") or []
                    if isinstance(item, Mapping) and str(item.get("product_id") or "").strip()
                ]
                action.setdefault("product_names", {
                    str(item["product_id"]): str(item.get("product_name") or item.get("productName") or "")
                    for item in candidates
                })
                action.setdefault("product_images", {
                    str(item["product_id"]): str(item.get("image_url") or item.get("imageUrl") or "")
                    for item in candidates
                })
                action.setdefault("reasons", {
                    str(item["product_id"]): item.get("reason") for item in candidates
                })
                mode_decision = decide_blacklist_handling(
                    candidates,
                    execution_mode=self._relevance_blacklist_execution_mode,
                )
                action["execution_mode"] = mode_decision["executionMode"]
                aggregate["blacklist_execution_mode"] = mode_decision["executionMode"]
                if not mode_decision["shouldExecute"]:
                    if mode_decision["shouldPersistPending"]:
                        self._record_pending_relevance_action(spec, action, context)
                    round_record["action_result"] = {
                        "status": "pending_approval"
                        if mode_decision["shouldPersistPending"]
                        else "recorded_only"
                    }
                    aggregate["rounds"].append(round_record)
                    last_action = action
                    break
            last_action = action
            action_started = time.perf_counter()
            try:
                if self.checkpoint is not None:
                    checkpoint_action = dict(action)
                    checkpoint_action["_relevance_checkpoint"] = {
                        "aggregate": deepcopy(aggregate),
                        "seen_product_ids": sorted(seen),
                        "round_index": round_index,
                    }
                    checkpoint = self.checkpoint.save_action(
                        self._require_batch_id(),
                        spec,
                        "relevance",
                        checkpoint_action,
                        self._require_checkpoint_version(),
                    )
                    self._checkpoint_version = checkpoint.version
                executed = self._execute_and_verify(spec, action, context)
                if not isinstance(executed, NodeResult):
                    raise TypeError("action gateway returned an invalid node result")
            except Exception as exc:
                action_seconds = round(max(0.0, time.perf_counter() - action_started), 6)
                telemetry["actionSeconds"] += action_seconds
                round_telemetry["actionSeconds"] = action_seconds
                telemetry["roundSeconds"] = round(
                    float(telemetry.get("roundSeconds") or 0) + action_seconds,
                    6,
                )
                final_error = NodeResult.error("RELEVANCE_BLACKLIST_FAILED", self._error_text(exc))
                aggregate["rounds"].append(round_record)
                break
            action_seconds = round(max(0.0, time.perf_counter() - action_started), 6)
            telemetry["actionSeconds"] += action_seconds
            round_telemetry["actionSeconds"] = action_seconds
            telemetry["roundSeconds"] = round(
                float(telemetry.get("roundSeconds") or 0) + action_seconds,
                6,
            )
            if executed.status is NodeStatus.ERROR:
                final_error = executed
                aggregate["rounds"].append(round_record)
                break
            if executed.status is not NodeStatus.EXECUTED:
                final_error = NodeResult.error(
                    "RELEVANCE_BLACKLIST_FAILED", "商品拉黑动作未完成"
                )
                aggregate["rounds"].append(round_record)
                break
            executed_actions.append(dict(action))
            round_record["action_result"] = executed.result
            if not isinstance(action, Mapping) or action.get("type") != "product_blacklist":
                break
            time.sleep(5.0)
            recheck_started = time.perf_counter()
            try:
                products = list(self.search.mixed(spec.brand, spec.query))
            except Exception as exc:
                recheck_seconds = round(max(0.0, time.perf_counter() - recheck_started), 6)
                telemetry["recheckSeconds"] += recheck_seconds
                round_telemetry["recheckSeconds"] = recheck_seconds
                telemetry["roundSeconds"] = round(
                    float(telemetry.get("roundSeconds") or 0) + recheck_seconds,
                    6,
                )
                aggregate["rounds"].append(round_record)
                final_error = NodeResult.error("RELEVANCE_SEARCH_FAILED", self._error_text(exc))
                break
            recheck_seconds = round(max(0.0, time.perf_counter() - recheck_started), 6)
            telemetry["recheckSeconds"] += recheck_seconds
            round_telemetry["recheckSeconds"] = recheck_seconds
            telemetry["roundSeconds"] = round(
                float(telemetry.get("roundSeconds") or 0) + recheck_seconds,
                6,
            )
            round_telemetry["roundSeconds"] = round(
                float(round_telemetry.get("roundSeconds") or 0)
                + action_seconds
                + recheck_seconds,
                6,
            )
            search_effect = self._blacklist_search_effect(action, products)
            if search_effect is not None:
                round_record["search_effect"] = search_effect
                aggregate["search_effect"] = search_effect
            aggregate["rounds"].append(round_record)
            if search_effect is not None:
                final_error = NodeResult.error(
                    "RELEVANCE_BLACKLIST_SEARCH_EFFECT_NOT_VERIFIED",
                    "拉黑商品仍存在于实时 Mixed 召回结果中",
                )
                break
            if round_index == max_rounds:
                aggregate["max_rounds_reached"] = True

        detected = list(aggregate["detected_product_ids"])
        blacklisted = [
            str(item.get("product_id"))
            for item in aggregate["blacklist_candidates"]
            if isinstance(item, Mapping) and str(item.get("product_id") or "").strip()
        ]
        aggregate["blacklist_ratio"] = round(len(set(blacklisted)) / len(set(detected)), 4) if detected else 0.0
        if blacklisted:
            self._enqueue_relevance_kg(spec, aggregate)
        if final_error is not None:
            aggregate["error"] = {
                "code": final_error.error_code,
                "message": final_error.error_message,
            }
            final_error.result = aggregate
            final_error.action = last_action
            result = final_error
        else:
            result = NodeResult.executed(
                aggregate,
                action=last_action or (executed_actions[-1] if executed_actions else None),
            )
        if isinstance(resume_state, Mapping) and isinstance(resume_state.get("telemetry"), Mapping):
            result.telemetry = {}
            _accumulate_telemetry(result.telemetry, resume_state["telemetry"])
            _accumulate_telemetry(result.telemetry, telemetry)
        elif telemetry:
            result.telemetry = dict(telemetry)
        if result.telemetry:
            result.telemetry["actionCount"] = sum(
                1
                for round_item in aggregate.get("rounds", [])
                if isinstance(round_item, Mapping) and round_item.get("action_result") is not None
            )
        self._set_node_result(run, "relevance", result)
        self._record(spec, "relevance", result)

    @staticmethod
    def _merge_relevance_result(aggregate: dict[str, Any], payload: Mapping[str, Any]) -> None:
        calls = aggregate.setdefault("modelCallAudits", [])
        incoming = payload.get("modelCallAudits")
        if not isinstance(incoming, list):
            audit = payload.get("audit")
            incoming = [audit] if isinstance(audit, Mapping) else []
        for audit in incoming:
            if not isinstance(audit, Mapping):
                continue
            request_id = audit.get("requestId")
            if request_id and any(item.get("requestId") == request_id for item in calls):
                continue
            calls.append(dict(audit))
        aggregate["product_count"] += int(payload.get("product_count") or 0)
        telemetry = payload.get("telemetry")
        if isinstance(telemetry, Mapping):
            aggregate_telemetry = aggregate.setdefault("telemetry", {})
            if isinstance(aggregate_telemetry, dict):
                aggregate_telemetry["batch_count"] = max(
                    int(aggregate_telemetry.get("batch_count") or 0),
                    int(telemetry.get("batch_count") or 0),
                )
                for key in (
                    "product_count",
                    "image_download_success_count",
                    "image_download_failure_count",
                ):
                    aggregate_telemetry[key] = int(aggregate_telemetry.get(key) or 0) + int(
                        telemetry.get(key) or 0
                    )
        for key in (
            "reused_product_ids", "requested_product_ids", "detected_product_ids",
            "relevant_product_ids", "blacklist_candidates", "warnings",
        ):
            values = payload.get(key) or []
            if not isinstance(values, list):
                continue
            if key == "blacklist_candidates":
                existing = {str(item.get("product_id")) for item in aggregate[key] if isinstance(item, Mapping)}
                aggregate[key].extend(item for item in values if isinstance(item, Mapping) and str(item.get("product_id")) not in existing)
            else:
                aggregate[key].extend(value for value in values if value not in aggregate[key])

    def _enqueue_relevance_kg(self, spec: QuerySpec, result: Mapping[str, Any]) -> None:
        service = self.async_tasks
        enqueue = getattr(service, "enqueue_for_query", None) if service is not None else None
        batch_id = self._active_batch_id or spec.batch_id or getattr(self, "_run_batch_id", None)
        if not callable(enqueue) or not batch_id:
            return
        blacklisted = [
            str(item.get("product_id"))
            for item in result.get("blacklist_candidates") or []
            if isinstance(item, Mapping) and str(item.get("product_id") or "").strip()
        ]
        remediation = build_relevance_remediation_payload(
            query=spec.query,
            candidates=[
                item
                for item in result.get("blacklist_candidates") or []
                if isinstance(item, Mapping)
            ],
            query_tags=result.get("query_tags") or result.get("queryTags") or [],
            kg_context=result.get("kg_context") or result.get("kgContext") or {},
        )
        if isinstance(result, dict):
            result["cause_groups"] = remediation["causeGroups"]
        import hashlib
        import json
        identity = json.dumps(
            {"batch_id": batch_id, "brand": spec.brand, "query": spec.query, "product_ids": sorted(set(blacklisted))},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        try:
            submission = enqueue(
                batch_id=batch_id,
                brand=spec.brand,
                query_text=spec.query,
                trigger_source="relevance_root_cause",
                evidence={
                    "error_product_ids": sorted(set(blacklisted)),
                    "detected_product_ids": list(result.get("detected_product_ids") or []),
                    "blacklist_ratio": result.get("blacklist_ratio"),
                    "blacklist_candidates": list(result.get("blacklist_candidates") or []),
                    "causeGroups": remediation["causeGroups"],
                    "queryTags": remediation["queryTags"],
                    "kgContext": remediation["kgContext"],
                },
                idempotency_key="relevance-root-cause:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                langfuse_context=(
                    self._trace_batch.query_trace_context(
                        spec.query_id,
                        "relevance_governance_agent",
                    )
                    if self._trace_batch is not None
                    else None
                ),
            )
        except Exception as exc:
            result.setdefault("warnings", []).append({
                "code": "KG_TASK_ENQUEUE_FAILED",
                "message": self._error_text(exc),
            })
            return
        task_status = getattr(submission, "status", "pending")
        task_status = getattr(task_status, "value", task_status)
        result["kg_task"] = {
            "status": str(task_status),
            "task_id": getattr(submission, "task_id", None),
            "error_code": getattr(submission, "error_code", None),
            "error_message": getattr(submission, "error_message", None),
        }
        if str(task_status) == "error":
            result.setdefault("warnings", []).append({
                "code": getattr(submission, "error_code", None) or "KG_TASK_ENQUEUE_FAILED",
                "message": getattr(submission, "error_message", None) or "KG Agent 异步任务投递失败",
            })

    def _record_pending_relevance_action(
        self,
        spec: QuerySpec,
        action: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        defer = getattr(self.action_gateway, "defer", None)
        if not callable(defer):
            return
        outcome = defer(spec, action, context)
        if isinstance(outcome, NodeResult) and outcome.status is NodeStatus.ERROR:
            raise RuntimeError(outcome.error_message or "错误商品待确认动作保存失败")


    def _record_reorder_error(
        self,
        spec: QuerySpec,
        run: QueryRunResult,
        error_code: str,
        error_message: str,
    ) -> None:
        persisted = self._persisted(spec, "reorder")
        if persisted is not None and persisted.terminal and not self._should_retry_s0_skip(spec, persisted):
            self._set_node_result(run, "reorder", persisted)
            return
        outcome = NodeResult.error(error_code, error_message)
        self._set_node_result(run, "reorder", outcome)
        self._begin(spec, "reorder")
        self._record(spec, "reorder", outcome)

    @staticmethod
    def _preference_context(
        spec: QuerySpec,
        preference_results: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(preference_results, Mapping):
            return {}
        if spec.query_id not in preference_results:
            return {}
        return {"preference_result": preference_results[spec.query_id]}

    @staticmethod
    def _preference_result_payload(
        preference_results: Mapping[str, Any] | None,
        query_id: str,
    ) -> Mapping[str, Any] | None:
        if not isinstance(preference_results, Mapping):
            return None
        value = preference_results.get(query_id)
        if isinstance(value, NodeResult):
            value = value.result
        return dict(value) if isinstance(value, Mapping) else None

    def _generate_preference_results(
        self,
        specs: Sequence[QuerySpec],
        *,
        shared_contexts: Mapping[str, Any],
        data_date: date | None,
        batch_id: str | None,
    ) -> Mapping[str, Any]:
        provider = self.preference_provider
        if provider is None:
            return {}
        method = getattr(provider, "run", None)
        if not callable(method):
            method = provider if callable(provider) else None
        if method is None:
            raise TypeError("preference provider must be callable or expose run")
        result = method(
            specs=tuple(specs),
            shared_contexts=shared_contexts,
            data_date=data_date,
            batch_id=batch_id,
            trace_batch=self._trace_batch,
        )
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            raise TypeError("preference provider must return a mapping")
        return result

    def _record_reorder_skipped(
        self,
        spec: QuerySpec,
        run: QueryRunResult,
        reason: str,
    ) -> None:
        persisted = self._persisted(spec, "reorder")
        if persisted is not None and persisted.terminal and not self._should_retry_s0_skip(spec, persisted):
            self._set_node_result(run, "reorder", persisted)
            return
        outcome = NodeResult.skipped(reason)
        self._set_node_result(run, "reorder", outcome)
        self._begin(spec, "reorder")
        self._record(spec, "reorder", outcome)

    def _observe_control_items(
        self,
        specs: Sequence[QuerySpec],
        results: dict[str, QueryRunResult],
        *,
        data_date: date | None,
        batch_id: str | None,
    ) -> None:
        """Read controls once and expose read-only terminal node states.

        Controls must be comparable with treatment samples, but they must not
        enter any governance runner or action gateway.  Their node states are
        kept in the returned result only; checkpoint and governance writes are
        intentionally bypassed.
        """
        observer = self.experiment_observer
        for spec in specs:
            cached = self._observed_control_results.get(spec.query_id)
            if cached is not None:
                # Controls are read-only evidence.  Reuse the first snapshot
                # on later slices instead of issuing Mixed or recording the
                # same observation again.
                results[spec.query_id] = deepcopy(cached)
                continue
            if self.should_continue is not None and not self.should_continue():
                raise _WorkflowYield()
            run = results[spec.query_id]
            products: list[Mapping[str, Any]] | None = None
            error: str | None = None
            try:
                products = list(self.search.mixed(spec.brand, spec.query))
                observation = {
                    "result_count": len(products),
                    "products": products,
                }
                run.observation = observation
                run.s0 = observation
                run.nodes["s0"] = NodeResult.executed(observation)
            except Exception as exc:
                error = self._error_text(exc)
                run.final_status = "error"
                run.observation_error = error
                run.nodes["s0"] = NodeResult.error(
                    "CONTROL_SEARCH_FAILED", error
                )
            for node in self._RUNNER_KEYS:
                run.nodes[node] = NodeResult.skipped(
                    "control_group_read_only"
                )
            if observer is not None:
                try:
                    observer.record_observation(
                        spec,
                        products,
                        data_date=data_date,
                        batch_id=batch_id,
                        error=error,
                    )
                except Exception:
                    pass
            self._observed_control_results[spec.query_id] = deepcopy(run)

    def _skip_node(
        self, spec: QuerySpec, run: QueryRunResult, node: str, reason: str
    ) -> None:
        persisted = self._persisted(spec, node)
        if persisted is not None and persisted.terminal:
            self._set_node_result(run, node, persisted)
            return
        outcome = NodeResult.skipped(reason)
        self._begin(spec, node)
        self._set_node_result(run, node, outcome)
        self._record(spec, node, outcome)

    @staticmethod
    def _set_node_result(
        run: QueryRunResult, node: str, result: NodeResult
    ) -> None:
        run.nodes[node] = result
        if result.status is NodeStatus.ERROR:
            run.final_status = "error"

    def _record(self, spec: QuerySpec, node: str, result: NodeResult) -> None:
        self._assert_scheduler_fence()
        if self.checkpoint is not None:
            checkpoint = self.checkpoint.record_node(
                self._require_batch_id(),
                spec,
                node,
                result,
                self._require_checkpoint_version(),
            )
            self._checkpoint_version = checkpoint.version
            # A stage cache may have been populated before this write.  Drop
            # only the changed stage so the next barrier check sees the new
            # durable terminal state without going back to per-Query reads.
            self._persisted_cache.pop(node, None)
            return
        self.store.save_step(spec.brand, spec.query, node, result)

    def _persist_model_config_version(
        self,
        specs: list[QuerySpec],
        snapshot: NodeConfigSnapshot,
        *,
        batch_id: str | None = None,
    ) -> None:
        if snapshot.version is None:
            return
        save_version = getattr(self.store, "save_model_config_version", None)
        if not callable(save_version):
            return
        batch_ids = (
            {batch_id}
            if batch_id
            else {spec.batch_id for spec in specs if spec.batch_id}
        )
        for current_batch_id in sorted(batch_ids):
            save_version(current_batch_id, snapshot.version)

    def _remaining_node_starts(self) -> int | None:
        if self.should_continue is not None and not self.should_continue():
            raise _WorkflowYield()
        if self.max_execution_seconds is not None and (
                time.monotonic() - self._invocation_started >= self.max_execution_seconds):
            raise _WorkflowYield()
        return None if self.max_node_starts is None else max(0, self.max_node_starts - self._node_starts)

    def _bounded_specs(self, specs: Sequence[QuerySpec], node: str) -> list[QuerySpec]:
        remaining = self._remaining_node_starts()
        if remaining is None:
            return list(specs)
        selected = []
        for spec in specs:
            persisted = self._persisted(spec, node)
            if persisted is not None and persisted.terminal and not self._should_retry_s0_skip(spec, persisted):
                self._set_node_result(self._batch_results[spec.query_id], node, persisted)
            elif remaining > 0:
                selected.append(spec)
                remaining -= 1
        return selected

    def _yield_if_stage_incomplete(self, specs: Sequence[QuerySpec], node: str) -> None:
        if self.max_node_starts is None and self.max_execution_seconds is None:
            return
        if any((result := self._persisted(spec, node)) is None or not result.terminal
               for spec in specs):
            raise _WorkflowYield()

    def _begin(self, spec: QuerySpec, node: str) -> None:
        self._assert_scheduler_fence()
        if self.checkpoint is None:
            return
        if self._remaining_node_starts() == 0:
            raise _WorkflowYield()
        self._node_starts += 1
        checkpoint = self.checkpoint.begin_node(
            self._require_batch_id(),
            spec,
            node,
            self._require_checkpoint_version(),
        )
        self._checkpoint_version = checkpoint.version

    def _assert_scheduler_fence(self) -> None:
        if self.assert_scheduler_fence is not None:
            self.assert_scheduler_fence()

    def _execute_and_verify(self, spec: QuerySpec, action: Any, context: Mapping[str, Any]) -> NodeResult:
        self._assert_scheduler_fence()
        return self.action_gateway.execute_and_verify(spec, action, context)

    def _persisted(self, spec: QuerySpec, node: str) -> NodeResult | None:
        if self.checkpoint is None:
            return None
        batch_id = self._require_batch_id()
        if node not in self._persisted_cache:
            get_nodes = getattr(self.checkpoint, "get_nodes", None)
            if callable(get_nodes):
                loaded = get_nodes(batch_id, self._last_specs, node)
                self._persisted_cache[node] = dict(loaded or {})
            else:
                return self.checkpoint.get_node(batch_id, spec, node)
        return self._persisted_cache[node].get((spec.brand, spec.query))

    def _should_retry_s0_skip(self, spec: QuerySpec, result: NodeResult) -> bool:
        """Allow downstream nodes skipped after S0 failure to run after S0 recovers."""
        if result.status is not NodeStatus.SKIPPED or not isinstance(result.result, Mapping):
            return False
        if result.result.get("reason") != "s0_failed":
            return False
        store = self._checkpoint_s0_store
        return store is not None and store.get_s0(spec.brand, spec.query) is not None

    def _recover_interrupted_action(
        self, spec: QuerySpec, run: QueryRunResult, node: str
    ) -> NodeResult | None:
        inflight = self.checkpoint.get_inflight_action(
            self._require_batch_id(), spec, node
        )
        if inflight is None:
            return None
        recover = getattr(self.action_gateway, "recover_interrupted", None)
        if recover is None:
            return NodeResult.error(
                "ACTION_RECOVERY_UNAVAILABLE",
                "执行中动作缺少权威回读能力，未重复执行",
            )
        context: dict[str, Any] = {"s0": run.s0, "nodes": run.nodes}
        recovery = recover(spec, node, inflight.action, context)
        if not isinstance(recovery, ActionRecoveryResult):
            return NodeResult.error(
                "ACTION_RECOVERY_INVALID",
                "执行中动作回读结果无效，未重复执行",
            )
        if recovery.status is ActionRecoveryStatus.COMPLETED:
            return recovery.result or NodeResult.error(
                "ACTION_RECOVERY_INVALID",
                "权威回读确认动作完成，但没有提供执行结果",
            )
        if recovery.status is ActionRecoveryStatus.SAFE_TO_RETRY:
            return None
        return NodeResult.error(
            recovery.error_code or "ACTION_RESULT_UNKNOWN",
            recovery.error_message or "执行中动作结果仍无法确认，未重复执行",
        )

    def _require_batch_id(self) -> str:
        if self._active_batch_id is None:
            raise RuntimeError("checkpoint batch is not active")
        return self._active_batch_id

    def _require_checkpoint_version(self) -> int:
        if self._checkpoint_version is None:
            raise RuntimeError("checkpoint version is not available")
        return self._checkpoint_version

    @staticmethod
    def _error_text(error: Exception) -> str:
        message = str(error).strip()
        return message or error.__class__.__name__
