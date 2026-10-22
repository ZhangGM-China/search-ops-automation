"""Production composition root for the deterministic Query workflow.

The workflow modules are intentionally small and dependency-free.  This
module is the only place where the scheduler-facing production adapters are
assembled: operational search, persistence, model runtimes, external facts,
behavior evidence, and governed actions.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from src.config import get_brand_business_config
from src.governance.action_gateway import ActionGateway
from src.governance.query_policy import assert_query_governance_allowed
from src.integrations.operational_service import OperationalService
from src.persistence.mysql_timeouts import mysql_timeout_connect_args
from src.persistence import SessionLocal
from src.llm.config_store import capture_runtime_snapshot
from src.llm.node_config import (
    QUERY_GOVERNANCE_NODE_KEYS,
    NodeConfigSnapshot,
)

from .action_execution import RoutingActionGateway
from .async_tasks import (
    QueryGovernanceAsyncTaskRepository,
    QueryGovernanceAsyncTaskService,
)
from .behavior_evidence import (
    BehaviorEvidenceConfig,
    BehaviorEvidenceError,
    BehaviorEvidenceService,
    BehaviorEvidenceSourceSpec,
    OperationalProductDetailGateway,
    SqlBehaviorEvidenceSource,
)
from .checkpoint import SqlAlchemyCheckpointStore
from .image_bridge import BrowserBridgeImageFetcher
from .context import (
    ConfigBrandContextSource,
    SharedContextService,
    SqlAlchemySharedContextStore,
    SqlXhsBrandContextSource,
)
from .facts import WorldFactsService
from .kg_agent import (
    KGAgent,
    KnowledgeGraphReadTools,
    QueryGovernanceActionPlanRepository,
    QueryGovernanceKGWorker,
)
from .kg_execution import QueryGovernanceKGActionExecutor
from .langfuse import QueryGovernanceObservability
from .manual_relevance import (
    ManualRelevanceCheckRepository,
    ManualRelevanceCheckService,
    ProductDetailBatchGateway,
)
from .model_runtime import QueryGovernanceModelRuntime
from .models import NodeResult, NodeStatus, QuerySpec
from .preference_generation import (
    PreferenceGenerationConfig,
    PreferenceGenerationInput,
    PreferenceGenerationRunner,
)
from .recall import RecallCheckRunner, SqlAlchemyRecallHistoryGateway
from .relevance import (
    ImageCacheService,
    PillowImageTransformer,
    RelevanceCallGate,
    RelevanceCheckRunner,
    RemoteImageFetcher,
    SqlAlchemyImageAssetStore,
    SqlAlchemyRelevanceStore,
    TencentCosObjectStorage,
    UnavailableObjectStorage,
)
from .relevance_batch import RelevanceBatchConfig, RelevanceBatchRunner
from .relevance_actions import RelevanceActionPlanRepository, RelevanceBlacklistActionGateway
from .relevance_remediation import RelevanceGovernanceAgent
from .pi_governance import PiGovernanceClient
from .relevance_config import RelevanceConcurrencyController, get_relevance_runtime_config
from .relevance_guidance import RelevanceGuidanceService
from .retag_verification import RelevanceRetagVerificationService
from .reorder_actions import (
    QueryReorderActionGateway,
    QueryReorderActionExecutor,
    QueryReorderActionPlanRepository,
    QueryReorderActionVerifier,
)
from .vector_reorder import (
    VectorReorderClient,
    VectorReorderConfig,
    VectorReorderNodeRunner,
)
from .workflow import QueryGovernanceWorkflow
from .visibility import effective_check_modes, strictest_execution_mode


def build_behavior_evidence_engine() -> Any:
    """Build the shared external read-only engine for behavior evidence.

    Query behavior evidence is stored in the shared analytical database, not
    in the application's local SQLite persistence database.  The existing
    ``RD_RIVIERA`` connection is the configured read-only connection for that
    shared source, including the four supported brands.
    """

    from sqlalchemy import create_engine

    values = {
        name: os.getenv(f"RD_RIVIERA_{name.upper()}")
        for name in ("user", "pass", "host", "port", "db")
    }
    missing = [f"RD_RIVIERA_{name.upper()}" for name, value in values.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(
            "行为证据外部只读数据源未配置：" + ", ".join(missing)
        )
    user = urllib.parse.quote_plus(str(values["user"]))
    password = urllib.parse.quote_plus(str(values["pass"]))
    url = (
        f"mysql+pymysql://{user}:{password}@{values['host']}:{values['port']}"
        f"/{values['db']}?charset=utf8mb4"
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args=mysql_timeout_connect_args("RD_DB"),
    )


def _query_concurrency_for_brand(brand: str, configured: int) -> int:
    """Bound concurrent governance searches for brands with shared limiters."""
    if brand == "summit":
        return min(configured, 2)
    return configured


class OperationalSearchGateway:
    """Expose the operational Mixed endpoint through the new search port."""

    def __init__(
        self,
        operational_service: Any,
        *,
        page_size: int = 16,
        page_num: int = 1,
    ) -> None:
        if isinstance(page_size, bool) or page_size <= 0:
            raise ValueError("page_size must be positive")
        if isinstance(page_num, bool) or page_num <= 0:
            raise ValueError("page_num must be positive")
        self.operational_service = operational_service
        self.page_size = int(page_size)
        self.page_num = int(page_num)

    def mixed_with_total(self, brand: str, query: str, *, page_size: int | None = None, page_num: int | None = None) -> Mapping[str, Any]:
        search_kwargs = {
            "page_size": self.page_size if page_size is None else page_size,
            "page_num": self.page_num if page_num is None else page_num,
        }
        if brand == "summit":
            search_kwargs["enrich_details"] = False
        response = self.operational_service.search_products_with_total(
            brand, query, **search_kwargs
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("Mixed 返回结果无效")
        products = response.get("products")
        if response.get("success") is not True or not isinstance(products, Sequence) or isinstance(products, (str, bytes)):
            raise RuntimeError("Mixed 商品结果无效")
        return {**dict(response), "products": [dict(product) for product in products if isinstance(product, Mapping)]}

    def mixed(self, brand: str, query: str) -> Sequence[Mapping[str, Any]]:
        return tuple(self.mixed_with_total(brand, query).get("products") or [])


class QueryGovernancePreferenceProvider:
    """Collect behavior evidence and generate preferences before reorder."""

    def __init__(
        self,
        *,
        evidence: BehaviorEvidenceService,
        runner: PreferenceGenerationRunner,
        config_provider: Callable[[str], Mapping[str, Any]] = get_brand_business_config,
    ) -> None:
        self.evidence = evidence
        self.runner = runner
        self.config_provider = config_provider

    def run(
        self,
        *,
        specs: Sequence[QuerySpec],
        shared_contexts: Mapping[str, Any],
        data_date: date | None,
        batch_id: str | None,
        trace_batch: Any | None = None,
    ) -> Mapping[str, NodeResult]:
        del batch_id
        if data_date is None:
            raise ValueError("data_date is required for preference generation")

        inputs: list[PreferenceGenerationInput] = []
        results: dict[str, NodeResult] = {}
        pending_specs = []
        for spec in specs:
            if spec.reorder_eligible is False:
                results[spec.query_id] = NodeResult.skipped(
                    spec.reorder_skip_reason or "CTR/CVR 正常"
                )
                continue
            pending_specs.append(spec)

        def collect_input(spec: QuerySpec):
            context = shared_contexts.get(spec.brand)
            if not isinstance(context, Mapping):
                return spec, None, NodeResult.error(
                    "SHARED_CONTEXT_UNAVAILABLE", "共享上下文未生成"
                )
            try:
                evidence_result = self.evidence.collect(
                    brand=spec.brand,
                    query=spec.query,
                    data_date=data_date,
                    config=BehaviorEvidenceConfig.from_mapping(
                        self.config_provider(spec.brand) or {}
                    ),
                )
            except BehaviorEvidenceError as exc:
                return spec, None, NodeResult.error(exc.error_code, exc.message)
            except Exception as exc:
                return spec, None, NodeResult.error(
                    "PREFERENCE_EVIDENCE_FAILED", self._error_text(exc)
                )
            return spec, PreferenceGenerationInput(
                spec=spec,
                shared_context=dict(context.get("global_facts") or {}),
                brand_context=dict(context.get("brand_context") or {}),
                products=evidence_result.products,
                requested_data_date=(getattr(evidence_result, "requested_data_date", None) or data_date).isoformat(),
                evidence_data_date=getattr(evidence_result, "data_date", data_date).isoformat(),
                evidence_date_fallback_reason=getattr(evidence_result, "date_fallback_reason", None),
            ), None

        if pending_specs:
            concurrency = max(1, int(getattr(getattr(self.runner, "config", None), "concurrency", 1)))
            with ThreadPoolExecutor(max_workers=min(concurrency, len(pending_specs))) as pool:
                collected = list(pool.map(collect_input, pending_specs))
            for spec, input_item, error in collected:
                if error is not None:
                    results[spec.query_id] = error
                elif input_item is not None:
                    inputs.append(input_item)

        if not inputs:
            return results

        callbacks = None
        metadata: dict[str, Any] = {}
        batch_config: Mapping[str, Any] | None = None
        if trace_batch is not None:
            try:
                callbacks = trace_batch.callbacks("preference_generation")
            except Exception:
                callbacks = None
            try:
                batch_config = trace_batch.batch_config(
                    "preference_generation",
                    [item.spec.query_id for item in inputs],
                )
            except Exception:
                batch_config = None
            metadata["node"] = "query_governance.preference_generation"
        try:
            generated = self.runner.run(
                inputs,
                callbacks=callbacks,
                metadata=metadata or None,
                batch_config=batch_config,
            )
        except Exception as exc:
            generated = {
                item.spec.query_id: NodeResult.error(
                    "PREFERENCE_GENERATION_FAILED", self._error_text(exc)
                )
                for item in inputs
            }
        results.update(generated)
        return results

    @staticmethod
    def _error_text(exc: Exception) -> str:
        return str(exc).strip() or exc.__class__.__name__


def _runtime_factory(
    node_key: str,
    snapshot: NodeConfigSnapshot,
    *,
    model_runtime_factory: Callable[..., Any] | None,
    timeout: float,
    max_completion_tokens: int | None,
    extra_body: Mapping[str, Any] | None = None,
) -> Any:
    if model_runtime_factory is not None:
        return model_runtime_factory(
            node_key,
            snapshot,
            timeout=timeout,
            temperature=0.1,
            max_completion_tokens=max_completion_tokens,
            extra_body=extra_body,
        )
    return QueryGovernanceModelRuntime.from_snapshot(
        node_key,
        snapshot,
        timeout=timeout,
        temperature=0.1,
        max_completion_tokens=max_completion_tokens,
        extra_body=extra_body,
    )


def _cos_key_prefix() -> str:
    configured = str(os.getenv("QUERY_GOVERNANCE_COS_KEY_PREFIX") or "").strip().strip("/")
    if configured:
        return configured
    environment = str(os.getenv("ENV") or "test").strip().lower() or "test"
    return f"query-governance/images/{environment}/v1"


def _image_proxy_url() -> str | None:
    configured = str(os.getenv("QUERY_GOVERNANCE_IMAGE_PROXY_URL") or "").strip()
    return configured or None


def _image_fetcher(timeout: float) -> RemoteImageFetcher:
    bridge_url = str(os.getenv("QG_BROWSER_BRIDGE_URL") or "").strip()
    browser = None
    if bridge_url:
        token = str(os.getenv("QG_BROWSER_BRIDGE_TOKEN") or "").strip()
        if not token:
            raise ValueError("图片桥接服务配置缺少 Token")
        browser = BrowserBridgeImageFetcher(bridge_url, token=token, timeout=max(90.0, timeout))
    return RemoteImageFetcher(timeout=timeout, proxy_url=_image_proxy_url(), browser_fetcher=browser)


def _image_storage() -> Any:
    required = {
        "QUERY_GOVERNANCE_COS_REGION": os.getenv("QUERY_GOVERNANCE_COS_REGION"),
        "QUERY_GOVERNANCE_COS_BUCKET": os.getenv("QUERY_GOVERNANCE_COS_BUCKET"),
        "TENCENT_COS_SECRET_ID": os.getenv("TENCENT_COS_SECRET_ID"),
        "TENCENT_COS_SECRET_KEY": os.getenv("TENCENT_COS_SECRET_KEY"),
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        return UnavailableObjectStorage("图片对象存储未配置，缺少必要运行时配置")
    try:
        return TencentCosObjectStorage()
    except (ValueError, RuntimeError) as exc:
        return UnavailableObjectStorage(f"图片对象存储不可用：{str(exc) or exc.__class__.__name__}")


def build_query_governance_workflow(
    brand: str,
    *,
    session_factory: Callable[[], Any] = SessionLocal,
    operational_service: Any | None = None,
    model_config_snapshot: NodeConfigSnapshot | None = None,
    model_runtime_factory: Callable[..., Any] | None = None,
    behavior_engine: Any | None = None,
    observability: QueryGovernanceObservability | None = None,
) -> QueryGovernanceWorkflow:
    """Build one fully wired deterministic workflow for a brand.

    The model snapshot is captured once per factory call.  Scheduler retries or
    later batches therefore cannot observe a partially published model bundle.
    """

    normalized_brand = str(brand or "").strip()
    if not normalized_brand:
        raise ValueError("brand is required")
    operational = operational_service or OperationalService()
    snapshot = model_config_snapshot or capture_runtime_snapshot(
        node_keys=(*QUERY_GOVERNANCE_NODE_KEYS, "query_governance.relevance_governance"),
        session_factory=session_factory,
    )
    business_config = get_brand_business_config(normalized_brand) or {}
    tool_modes = business_config.get("top_query_tool_modes")
    query_reorder_mode = tool_modes.get("query_reorder") if isinstance(tool_modes, Mapping) else None
    product_blacklist_mode = tool_modes.get("product_blacklist", "auto") if isinstance(tool_modes, Mapping) else "auto"
    check_modes = effective_check_modes(business_config)
    agent_modes = business_config.get("top_query_check_modes")
    agent_modes = dict(agent_modes) if isinstance(agent_modes, Mapping) else {}
    product_blacklist_mode = strictest_execution_mode(
        agent_modes.get("relevance", "auto"), product_blacklist_mode,
    )
    search = OperationalSearchGateway(
        operational,
        page_size=int(business_config.get("top_query_check_product_limit", 16)),
    )
    checkpoint = SqlAlchemyCheckpointStore(session_factory)

    def runtime(
        node_key: str,
        timeout: float,
        max_tokens: int | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> Any:
        effective_node_key = node_key
        if (
            node_key == "query_governance.relevance_governance"
            and node_key not in snapshot.nodes
        ):
            effective_node_key = "query_governance.kg_agent"
        return _runtime_factory(
            effective_node_key,
            snapshot,
            model_runtime_factory=model_runtime_factory,
            timeout=timeout,
            max_completion_tokens=max_tokens,
            extra_body=extra_body,
        )

    async_service = QueryGovernanceAsyncTaskService(
        QueryGovernanceAsyncTaskRepository(session_factory)
    )
    from .recall_drop import RecallDropAssessor
    recall = RecallCheckRunner(drop_assessor=RecallDropAssessor(
        operational, OperationalProductDetailGateway(operational, include_tag_sources=True)))

    facts = WorldFactsService()
    external_behavior_engine = behavior_engine if behavior_engine is not None else build_behavior_evidence_engine()
    shared_context = SharedContextService(
        facts=facts,
        brands=SqlXhsBrandContextSource(
            external_behavior_engine,
            base_source=ConfigBrandContextSource(),
        ),
        store=SqlAlchemySharedContextStore(session_factory),
    )

    preference_runner = PreferenceGenerationRunner(
        runtime=runtime(
            "query_governance.preference_generation",
            timeout=int(
                business_config.get(
                    "query_governance_preference_generation_timeout_seconds",
                    1200,
                )
            ),
            # Output limits follow the selected model's native defaults; this
            # is intentionally not brand-configurable.
            max_tokens=None,
            extra_body={"enable_thinking": False},
        ),
        config=PreferenceGenerationConfig.from_mapping(business_config),
    )
    behavior = BehaviorEvidenceService(
        source=SqlBehaviorEvidenceSource(
            external_behavior_engine,
            specs={
                supported_brand: BehaviorEvidenceSourceSpec()
                for supported_brand in ("atlas", "harbor", "summit", "riviera")
            },
        ),
        product_details=OperationalProductDetailGateway(operational),
    )
    preference_provider = QueryGovernancePreferenceProvider(
        evidence=behavior,
        runner=preference_runner,
    )

    vector_config = VectorReorderConfig.from_mapping(business_config)
    reorder = VectorReorderNodeRunner(
        client=VectorReorderClient(
            endpoint_url=vector_config.endpoint_url,
            timeout_seconds=vector_config.timeout_seconds,
        ),
        config=vector_config,
        config_provider=lambda current_brand: get_brand_business_config(
            current_brand
        ),
    )

    relevance_config = get_relevance_runtime_config(normalized_brand)
    relevance_store = SqlAlchemyRelevanceStore(session_factory)
    image_cache = ImageCacheService(
        fetcher=_image_fetcher(relevance_config.image_download_timeout_seconds),
        transformer=PillowImageTransformer(),
        storage=_image_storage(),
        key_prefix=_cos_key_prefix(),
        asset_store=SqlAlchemyImageAssetStore(session_factory),
    )
    relevance_runtime = runtime(
        "query_governance.relevance_multimodal",
        timeout=relevance_config.model_timeout_seconds,
        max_tokens=relevance_config.max_completion_tokens,
        extra_body={"enable_thinking": False},
    )
    relevance = RelevanceCheckRunner(
        runtime=relevance_runtime,
        record_store=relevance_store,
        image_cache=image_cache,
        concurrency_controller=RelevanceConcurrencyController(
            relevance_config.multimodal_concurrency
        ),
    )
    relevance_batch = RelevanceBatchRunner(
        runtime=relevance_runtime,
        record_store=relevance_store,
        image_cache=image_cache,
        config=RelevanceBatchConfig.from_runtime_config(relevance_config),
        concurrency_controller=RelevanceConcurrencyController(
            relevance_config.multimodal_concurrency
        ),
    )
    # 商品目录与取词共用同一只读数据源（ety_standard_product 所在的
    # search-ops-admin 库）：PRODUCT_DB_* 显式配置优先，未配置时回退
    # RD_RDS_*，避免各环境为同一实例重复下发两份凭据。
    product_db_host = str(os.getenv("PRODUCT_DB_HOST") or os.getenv("RD_RDS_HOST") or "").strip()
    product_db_user = str(os.getenv("PRODUCT_DB_USER") or os.getenv("RD_RDS_USER") or "").strip()
    product_db_password = str(os.getenv("PRODUCT_DB_PASSWORD") or os.getenv("RD_RDS_PASS") or "")

    def authorize(intent: Any) -> bool:
        assert_query_governance_allowed(
            str(getattr(intent, "brand", "") or ""),
            str(getattr(intent, "query_text", "") or ""),
        )
        return True

    recall_rate_evaluator = None
    if product_db_host and product_db_user and product_db_password:
        from .recall_rate import (
            JevTagSelector,
            MySQLCatalogProductSource,
            RecallRateEvaluator,
            RuntimeLLMRecallGateway,
        )
        from .recall_rate_actions import (
            RecallRateActionGateway,
            RecallRateActionPlanRepository,
        )

        brand_tenant = (
            (OperationalService.FIXED_BRAND_CONFIGS.get(normalized_brand) or {}).get("tenantId")
        )
        default_catalog_db = (
            "search-ops-admin"
            if str(os.getenv("ENV") or "test").strip().lower() == "prod"
            else "search-ops-admin-stg"
        )
        product_db_name = str(
            os.getenv("PRODUCT_DB_NAME") or os.getenv("RD_RDS_DB") or default_catalog_db
        ).strip()
        product_source = MySQLCatalogProductSource(
            host=product_db_host,
            database=product_db_name,
            user=product_db_user,
            password=product_db_password,
            port=int(os.getenv("PRODUCT_DB_PORT") or os.getenv("RD_RDS_PORT") or "3306"),
            tenant_id=int(brand_tenant) if brand_tenant is not None else None,
        )
        recall_rate_runtime_config = business_config or {}
        try:
            recall_rate_batch_size = int(
                recall_rate_runtime_config.get("query_governance_recall_rate_product_batch_size") or 200
            )
        except (TypeError, ValueError):
            recall_rate_batch_size = 200
        try:
            recall_rate_score_threshold = float(
                recall_rate_runtime_config.get("query_governance_recall_rate_score_threshold") or 0.0
            )
        except (TypeError, ValueError):
            recall_rate_score_threshold = 0.0
        try:
            recall_rate_jev_threshold = float(
                recall_rate_runtime_config.get("query_governance_recall_rate_jev_threshold") or 0.7
            )
        except (TypeError, ValueError):
            recall_rate_jev_threshold = 0.7
        llm_recall_gateway = RuntimeLLMRecallGateway(
            product_source=product_source,
            runtime=relevance_runtime,
            image_cache=image_cache,
            product_batch_size=recall_rate_batch_size,
            score_threshold=recall_rate_score_threshold,
        )
        recall_rate_action_gateway = RecallRateActionGateway(
            operational_service=operational,
            fact_gateway=ActionGateway(session_factory=session_factory, authorizer=authorize),
            action_plan_repository=RecallRateActionPlanRepository(session_factory),
        )
        recall_rate_evaluator = RecallRateEvaluator(
            search=search,
            llm_recall=llm_recall_gateway,
            relevance_runner=relevance_batch,
            relevance_store=relevance_store,
            operational_service=operational,
            jev_selector=JevTagSelector(threshold=recall_rate_jev_threshold),
            action_gateway=recall_rate_action_gateway,
        )
    recall.recall_rate_evaluator = recall_rate_evaluator

    reorder_fact_gateway = __import__(
        "src.governance.action_gateway", fromlist=["ActionGateway"]
    ).ActionGateway(session_factory=session_factory, authorizer=authorize)
    reorder_actions = QueryReorderActionGateway(
        search=search,
        executor=QueryReorderActionExecutor(operational),
        verifier=QueryReorderActionVerifier(),
        fact_gateway=reorder_fact_gateway,
        action_plan_repository=QueryReorderActionPlanRepository(session_factory),
    )
    blacklist_fact_gateway = __import__(
        "src.governance.action_gateway", fromlist=["ActionGateway"]
    ).ActionGateway(session_factory=session_factory, authorizer=authorize)
    blacklist_actions = RelevanceBlacklistActionGateway(
        operational_service=operational,
        fact_gateway=blacklist_fact_gateway,
        action_plan_repository=RelevanceActionPlanRepository(session_factory),
    )
    action_gateway = RoutingActionGateway(
        {"query_reorder": reorder_actions, "product_blacklist": blacklist_actions}
    )

    configured_query_concurrency = int(
        business_config.get("query_governance_query_concurrency", 10)
    )
    # Summit requests share a cross-process file-backed request governor
    # with the API and scheduler Pods.  Ten concurrent S0 calls contend on
    # that file lock and turn a sub-second search into repeated 15s waits.
    # Keep the brand-specific concurrency bounded while preserving the
    # configured value for every other brand.
    query_concurrency = _query_concurrency_for_brand(
        normalized_brand, configured_query_concurrency
    )

    workflow = QueryGovernanceWorkflow(
        search=search,
        store=checkpoint,
        checkpoint=checkpoint,
        runners={"recall": recall, "reorder": reorder, "relevance": relevance},
        action_gateway=action_gateway,
        facts=facts,
        model_config_snapshot=snapshot,
        async_tasks=async_service,
        recall_history=SqlAlchemyRecallHistoryGateway(session_factory),
        shared_context=shared_context,
        preference_provider=preference_provider,
        observability=observability,
        reorder_enabled=(
            check_modes["open_factor"]
            and query_reorder_mode != "disabled"
        ),
        relevance_enabled=check_modes["relevance"],
        recall_enabled=check_modes["recall"],
        relevance_blacklist_execution_mode=product_blacklist_mode,
        reorder_batch_runner=reorder,
        relevance_batch_runner=relevance_batch,
        relevance_independent_actions=True,
        query_concurrency=query_concurrency,
        relevance_query_batch_size=int(
            business_config.get("query_governance_relevance_query_batch_size", 5)
        ),
    )

    # The KG worker is a sidecar to the mainline.  Keep it attached to the
    # composition for the scheduler/worker entrypoint without making it part
    # of QueryGovernanceWorkflow.run_batch.
    kg_client = getattr(operational, "kg_client", None)
    if kg_client is not None:
        kg_tools = KnowledgeGraphReadTools(
            kg_client,
            search=operational,
            observability=workflow.observability,
        )
        kg_agent = KGAgent(
            runtime=runtime(
                "query_governance.kg_agent",
                timeout=int(
                    business_config.get(
                        "query_governance_kg_agent_timeout_seconds",
                        1200,
                    )
                ),
                # Output limits follow the selected model's native defaults;
                # this is intentionally not brand-configurable.
                max_tokens=None,
            ),
            tools=kg_tools,
        )
        workflow.kg_worker = QueryGovernanceKGWorker(
            async_service=async_service,
            agent=kg_agent,
            relevance_root_cause_agent=RelevanceGovernanceAgent(
                runtime=runtime(
                    "query_governance.relevance_governance",
                    timeout=int(
                        business_config.get(
                            "query_governance_kg_agent_timeout_seconds",
                            1200,
                        )
                    ),
                    max_tokens=None,
                ),
                kg_tools=kg_tools,
                product_details=OperationalProductDetailGateway(operational, include_tag_sources=True),
                pi_client=PiGovernanceClient(
                    timeout_seconds=int(
                        business_config.get(
                            "query_governance_kg_agent_timeout_seconds",
                            1200,
                        )
                    )
                ),
                reference_provider=lambda current_brand: str(
                    get_brand_business_config(current_brand).get(
                        "query_governance_kg_reference", ""
                    )
                    or ""
                ),
            ),
            agent_execution_modes=agent_modes,
            kg_action_executor=QueryGovernanceKGActionExecutor(
                session_factory=session_factory,
                kg_client=kg_client,
                authorizer=authorize,
                plan_guard=__import__("src.query_governance.kg_agent", fromlist=["KGPlanConflictChecker"]).KGPlanConflictChecker(kg_tools),
            ),
            kg_execution_modes={
                key: value
                for key, value in (
                    tool_modes.items() if isinstance(tool_modes, Mapping) else ()
                )
                if key in {"kg_tag", "kg_relation"}
            },
            action_plan_repository=QueryGovernanceActionPlanRepository(session_factory),
            observability=workflow.observability,
        )
        workflow.kg_worker.relevance_root_cause_agent.observability = workflow.observability
    else:
        workflow.kg_worker = None
    retag_fact_gateway = __import__(
        "src.governance.action_gateway", fromlist=["ActionGateway"]
    ).ActionGateway(session_factory=session_factory, authorizer=authorize)
    workflow.retag_verification = RelevanceRetagVerificationService(
        session_factory=session_factory,
        operational_service=operational,
        fact_gateway=retag_fact_gateway,
    )
    workflow.brand = normalized_brand
    return workflow


def build_manual_relevance_check_service(
    *,
    session_factory: Callable[[], Any] = SessionLocal,
    operational_service: Any | None = None,
    model_runtime_factory: Callable[..., Any] | None = None,
) -> ManualRelevanceCheckService:
    """Build the caller-driven relevance boundary without governance actions."""

    operational = operational_service or OperationalService()
    guidance_service = RelevanceGuidanceService(session_factory)

    def runtime_factory(current_brand: str) -> Any:
        config = get_relevance_runtime_config(current_brand)
        snapshot = capture_runtime_snapshot(
            node_keys=("query_governance.relevance_multimodal",),
            session_factory=session_factory,
        )
        return _runtime_factory(
            "query_governance.relevance_multimodal",
            snapshot,
            model_runtime_factory=model_runtime_factory,
            timeout=config.model_timeout_seconds,
            max_completion_tokens=config.max_completion_tokens,
            extra_body={"enable_thinking": False},
        )

    def image_cache_factory(current_brand: str) -> ImageCacheService:
        config = get_relevance_runtime_config(current_brand)
        return ImageCacheService(
            fetcher=_image_fetcher(config.image_download_timeout_seconds),
            transformer=PillowImageTransformer(),
            storage=_image_storage(),
            key_prefix=_cos_key_prefix(),
            asset_store=SqlAlchemyImageAssetStore(session_factory),
        )

    def guidance_reader(current_brand: str, query: str) -> str:
        payload = guidance_service.get(current_brand, query)
        if payload.get("status") != "active":
            return ""
        return str(payload.get("rules") or "")

    return ManualRelevanceCheckService(
        repository=ManualRelevanceCheckRepository(session_factory),
        gateway=ProductDetailBatchGateway(operational, search_client=operational),
        runtime_factory=runtime_factory,
        image_cache=image_cache_factory,
        batch_size=lambda current_brand: get_relevance_runtime_config(
            current_brand
        ).batch_size,
        guidance_reader=guidance_reader,
        observability=QueryGovernanceObservability.from_environment(),
    )


__all__ = [
    "OperationalSearchGateway",
    "QueryGovernancePreferenceProvider",
    "build_manual_relevance_check_service",
    "build_query_governance_workflow",
]
