"""Machine-readable contract for the externally supported read-only API."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _OpenAPIObject(BaseModel):
    model_config = ConfigDict(extra="allow")


class HealthResponse(_OpenAPIObject):
    releaseSha: str | None = None
    schemaRelease: str | None = None
    artifactManifestSha256: str | None = None
    healthy: bool | None = None
    releaseReadiness: dict[str, Any] | None = None
    env: str | None = None
    database: dict[str, Any] | None = None
    tasks: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None


class ManualRelevanceProduct(BaseModel):
    sku: str
    status: str
    isRelevant: bool | None = None
    reason: str | None = None


class ManualRelevanceQueryResult(BaseModel):
    query: str
    status: str
    products: list[ManualRelevanceProduct] = Field(default_factory=list)


class ManualRelevanceCheckResponse(BaseModel):
    checkId: str
    brand: str
    status: str
    createdAt: str | None = None
    updatedAt: str | None = None
    queries: list[ManualRelevanceQueryResult] = Field(default_factory=list)


class ManualRelevanceQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: NonEmptyString
    products: list[NonEmptyString] = Field(min_length=1)


class ManualRelevanceCheckSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: NonEmptyString
    queries: list[ManualRelevanceQueryInput] = Field(min_length=1)


class ManualRelevanceCheckSubmitResponse(BaseModel):
    checkId: str
    status: str


QueryGovernanceStatus = Literal["pending", "running", "executed", "skipped", "error"]


class QueryGovernanceStatusCounts(BaseModel):
    pending: int = 0
    running: int = 0
    executed: int = 0
    skipped: int = 0
    error: int = 0


class QueryGovernanceDurationStats(BaseModel):
    averageSeconds: float | None = None
    p50Seconds: float | None = None
    p95Seconds: float | None = None


class QueryGovernanceQueryTelemetry(BaseModel):
    endToEndSeconds: float | None = None
    nodeWallClockSeconds: float | None = None
    independentProcessingSeconds: float | None = None
    stageDurations: dict[str, float] = Field(default_factory=dict)
    roundDurations: list[float] = Field(default_factory=list)
    tokenUsage: float | None = None
    tokenUsageMissing: bool = False
    relevanceTokenUsage: float | None = None
    relevanceTokenUsageMissing: bool = False


class QueryGovernanceBatchTelemetry(BaseModel):
    wallClockSeconds: float | None = None
    queryCount: int = 0
    productCount: int | None = None
    batchCount: int | None = None
    roundCount: int | None = None
    queryEndToEnd: QueryGovernanceDurationStats = Field(default_factory=QueryGovernanceDurationStats)
    independentProcessing: QueryGovernanceDurationStats = Field(default_factory=QueryGovernanceDurationStats)
    modelSeconds: float | None = None
    actionSeconds: float | None = None
    recheckSeconds: float | None = None
    maxConcurrency: int | None = None
    modelCallCount: int | None = None
    retryCount: int | None = None
    truncationCount: int | None = None
    imageSuccessCount: int | None = None
    imageFailureCount: int | None = None
    tokenUsage: int | None = None
    detectedProductCount: int = 0
    reusedProductCount: int = 0
    inspectedProductCount: int = 0
    irrelevantProductCount: int = 0
    irrelevantProductPairCount: int = 0
    blacklistedProductCount: int | None = None
    relevanceDetectionAccuracy: float | None = None
    irrelevantQueryCount: int = 0
    noRecallCount: int = 0
    noRecallRate: float | None = None
    passCount: int = 0
    passRate: float | None = None
    aiProcessedCount: int = 0
    pendingCount: int = 0
    relevanceAccuracy: float | None = None
    irrelevantRate: float | None = None
    preferenceAdjustedQueryCount: int = 0
    highPvQueryCount: int = 0
    topQueryLimit: int | None = None
    rapidPvGrowthQueryCount: int = 0
    newQueryCount: int = 0
    ctrDeclineQueryCount: int = 0
    cvrDeclineQueryCount: int = 0
    ctrDeclineThresholdPp: float | None = None
    cvrDeclineThresholdPp: float | None = None
    stockBucketReductionProductCount: int = 0
    relevanceAverageSeconds: float | None = None
    preferenceAverageSeconds: float | None = None
    relevanceTokenUsage: int | None = None
    preferenceTokenUsage: int | None = None


class QueryGovernanceNode(BaseModel):
    nodeKey: Literal["s0", "recall", "reorder", "relevance"]
    status: QueryGovernanceStatus
    result: dict[str, Any] | None = None
    errorCode: str | None = None
    errorMessage: str | None = None
    durationSeconds: float | None = None
    tokenUsage: float | None = None
    tokenUsageMissing: bool = False
    startedAt: str | None = None
    completedAt: str | None = None
    telemetry: dict[str, Any] = Field(default_factory=dict)


class QueryGovernanceQuery(BaseModel):
    id: int
    batchId: str
    brand: str
    dataDate: str
    queryText: str
    queryRank: int | None = None
    status: QueryGovernanceStatus
    errorCode: str | None = None
    errorMessage: str | None = None
    durationSeconds: float = 0
    telemetry: QueryGovernanceQueryTelemetry = Field(default_factory=QueryGovernanceQueryTelemetry)
    nodes: list[QueryGovernanceNode] = Field(default_factory=list)
    experimentId: int | None = None
    groupType: str | None = None


class QueryGovernanceQueryListItem(_OpenAPIObject):
    id: int
    batchId: str
    brand: str
    dataDate: str
    queryText: str
    queryRank: int | None = None
    searchPv: int = 0
    searchUv: int = 0
    ctr: float = 0
    cvr: float = 0
    avgCtr: float = 0
    avgCvr: float = 0
    queryPvShare: float = 0
    resultCount: int | None = None
    status: QueryGovernanceStatus
    errorCode: str | None = None
    errorMessage: str | None = None
    durationSeconds: float = 0
    stepStatuses: list[dict[str, Any]] = Field(default_factory=list)
    experimentId: int | None = None
    groupType: str | None = None
    querySource: str | None = None
    hotWordType: str | None = None
    hotStatus: str | None = None
    invalidated: bool = False


class QueryGovernanceBatch(BaseModel):
    batchId: str
    brand: str
    dataDate: str | None = None
    status: QueryGovernanceStatus
    currentNode: str | None = None
    modelConfigVersion: int | None = None
    startedAt: str | None = None
    completedAt: str | None = None
    telemetry: QueryGovernanceBatchTelemetry = Field(default_factory=QueryGovernanceBatchTelemetry)


class QueryGovernanceBrandOverview(BaseModel):
    brand: str
    batchId: str
    dataDate: str | None = None
    status: QueryGovernanceStatus
    queryCount: int
    nodeStatusCounts: QueryGovernanceStatusCounts
    durationSeconds: float = 0
    startedAt: str | None = None
    completedAt: str | None = None
    telemetry: QueryGovernanceBatchTelemetry = Field(default_factory=QueryGovernanceBatchTelemetry)


class QueryGovernanceBrandSummary(BaseModel):
    queryCount: int
    queryStatusCounts: QueryGovernanceStatusCounts
    nodeStatusCounts: QueryGovernanceStatusCounts


class QueryGovernanceS0Product(BaseModel):
    productId: str | None = None
    rank: int | None = None
    productName: str | None = None
    imageUrl: str | None = None
    originalPrice: float | int | None = None
    discountPrice: float | int | None = None


class QueryGovernanceS0(BaseModel):
    resultCount: int
    products: list[QueryGovernanceS0Product] = Field(default_factory=list)


class QueryGovernanceAction(BaseModel):
    id: int
    nodeKey: str
    actionType: str
    executionMode: str
    executorType: Literal["ai_auto", "human"] | None = None
    executorUserId: str | None = None
    executedAt: str | None = None
    failureStage: Literal["generation", "execution", "verification", "rollback"] | None = None
    status: str
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    errorCode: str | None = None
    errorMessage: str | None = None
    createdAt: str | None = None
    updatedAt: str | None = None


QueryGovernancePlanStatus = Literal[
    "pending",
    "executing",
    "verified",
    "failed",
    "execution_unknown",
    "rollback_pending",
    "rolled_back",
    "rollback_failed",
    "rejected",
]

QueryGovernancePlanStatusLabel = Literal[
    "待执行",
    "执行中",
    "AI 已执行",
    "手动已执行",
    "AI 执行失败",
    "手动执行失败",
    "结果待确认",
    "回滚中",
    "已回滚",
    "回滚失败",
    "已取消",
]


class QueryGovernancePlan(BaseModel):
    id: str
    taskId: int
    actionId: int | None = None
    type: str
    title: str
    actionType: str = ""
    objective: str = ""
    summary: str | None = None
    reason: str | None = None
    status: QueryGovernancePlanStatus
    statusLabel: QueryGovernancePlanStatusLabel
    executionMode: str
    executorType: Literal["ai_auto", "human"] | None = None
    executorUserId: str | None = None
    executedAt: str | None = None
    failureStage: Literal["generation", "execution", "verification", "rollback"] | None = None
    executable: bool = False
    cancelable: bool = False
    actionIds: list[int] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    products: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    impact: list[str] = Field(default_factory=list)
    riskNotes: list[str] = Field(default_factory=list)


class QueryGovernanceRelevanceCandidate(_OpenAPIObject):
    product_id: str | None = None
    productId: str | None = None
    reason: str | None = None
    detection_types: list[str] = Field(default_factory=list)
    detectionTypes: list[str] = Field(default_factory=list)


class QueryGovernanceRelevanceSummary(BaseModel):
    query: str
    productCount: int = 0
    requestedProductIds: list[str] = Field(default_factory=list)
    reusedProductIds: list[str] = Field(default_factory=list)
    detectedProductIds: list[str] = Field(default_factory=list)
    relevantProductIds: list[str] = Field(default_factory=list)
    blacklistCandidates: list[QueryGovernanceRelevanceCandidate] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    detectedProductCount: int = 0


class QueryGovernanceInventoryRecord(BaseModel):
    id: int
    productId: str
    productName: str | None = None
    query: str
    lastBrokenRate: float | int | None = None
    status: str
    createdAt: str | None = None
    updatedAt: str | None = None


class QueryGovernanceAgentEvent(_OpenAPIObject):
    id: int
    agentName: str
    nodeKey: str
    eventType: str
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    errorCode: str | None = None
    durationSeconds: float | None = None
    modelName: str | None = None
    modelRole: str | None = None
    fallbackUsed: bool | None = None
    tokenUsage: int | None = None
    createdAt: str | None = None


class QueryGovernanceExperimentContext(BaseModel):
    id: int
    name: str
    status: str
    groupType: str | None = None
    sampleRank: int | None = None
    sampleSearchPv: int | None = None
    sampleCtr: float | None = None
    sampleCvr: float | None = None


class QueryGovernanceModelConfigNode(BaseModel):
    nodeKey: str
    channel: str
    primaryModel: str
    fallbackModel: str | None = None


class QueryGovernanceModelConfig(BaseModel):
    version: int
    status: str
    changeNote: str | None = None
    nodes: list[QueryGovernanceModelConfigNode] = Field(default_factory=list)


class QueryGovernanceModuleSlot(BaseModel):
    available: bool
    source: str | None = None
    version: int | None = None


class QueryGovernanceDetailSlots(BaseModel):
    kgTasks: list[dict[str, Any]] = Field(default_factory=list)
    sharedFacts: list[dict[str, Any]] = Field(default_factory=list)
    preference: QueryGovernanceModuleSlot
    relevance: QueryGovernanceModuleSlot
    inventory: QueryGovernanceModuleSlot
    modelConfig: QueryGovernanceModuleSlot
    abTest: QueryGovernanceModuleSlot


class BrandOverviewResponse(BaseModel):
    brands: list[QueryGovernanceBrandOverview] = Field(default_factory=list)


class BrandQueryListResponse(BaseModel):
    brand: str
    dataDate: str | None = None
    reason: str | None = None
    queries: list[QueryGovernanceQueryListItem] = Field(default_factory=list)
    total: int = 0
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=30, ge=1, le=100)
    hasNext: bool = False
    filter: str = "all"
    stats: dict[str, Any] | None = None


class BrandSummaryResponse(BaseModel):
    brand: str
    batch: QueryGovernanceBatch | None = None
    summary: QueryGovernanceBrandSummary


class BrandTrendResponse(_OpenAPIObject):
    brand: str | None = None
    dataDate: str | None = None
    trend: list[dict[str, Any]] = Field(default_factory=list)


class BrandMetricTrendDay(BaseModel):
    date: str
    batchId: str | None = None
    metrics: dict[str, Any] | None = None


class BrandMetricTrendResponse(_OpenAPIObject):
    brand: str | None = None
    startDate: str | None = None
    endDate: str | None = None
    days: list[BrandMetricTrendDay] = Field(default_factory=list)


class TaskListResponse(BaseModel):
    items: list[QueryGovernanceQuery] = Field(default_factory=list)
    total: int = 0


class TaskDetailResponse(BaseModel):
    query: QueryGovernanceQuery
    nodes: list[QueryGovernanceNode] = Field(default_factory=list)
    s0: QueryGovernanceS0 | None = None
    actions: list[QueryGovernanceAction] = Field(default_factory=list)
    governancePlans: list[QueryGovernancePlan] = Field(default_factory=list)
    relevance: QueryGovernanceRelevanceSummary
    inventory: list[QueryGovernanceInventoryRecord] = Field(default_factory=list)
    events: list[QueryGovernanceAgentEvent] = Field(default_factory=list)
    experiment: QueryGovernanceExperimentContext | None = None
    modelConfig: QueryGovernanceModelConfig | None = None
    productDisplayLimit: int = Field(default=16, ge=1)
    slots: QueryGovernanceDetailSlots


class QueryTrendResponse(_OpenAPIObject):
    taskId: int | None = None
    brand: str | None = None
    queryText: str | None = None
    trend: list[dict[str, Any]] = Field(default_factory=list)


class ResultCountResponse(_OpenAPIObject):
    taskId: int | None = None
    status: QueryGovernanceStatus | None = None
    resultCount: int | None = None
    resultCountKnown: bool | None = None
    products: list[dict[str, Any]] = Field(default_factory=list)
    productDisplayLimit: int = Field(default=16, ge=1)
    errorCode: str | None = None
    errorMessage: str | None = None


class ProductDetailResponse(_OpenAPIObject):
    product: dict[str, Any] | None = None
    detailMeta: dict[str, Any] | None = None


class SnapshotValidationResponse(_OpenAPIObject):
    brand: str | None = None
    monitoringDate: str | None = None
    dataDate: str | None = None
    source: dict[str, Any] | None = None
    local: dict[str, Any] | None = None
    queryDiffs: list[dict[str, Any]] = Field(default_factory=list)


class ExperimentListResponse(_OpenAPIObject):
    brand: str | None = None
    current: dict[str, Any] | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    total: int | None = None
    page: int | None = None
    pageSize: int | None = None


class ExperimentSamplePoolResponse(_OpenAPIObject):
    brand: str | None = None
    sampleDataDate: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)


class ExperimentResponse(_OpenAPIObject):
    id: int | None = None
    brand: str | None = None
    experimentName: str | None = None
    status: str | None = None
    sampleDataDate: str | None = None
    plannedStartDate: str | None = None
    plannedEndDate: str | None = None
    baselineStartDate: str | None = None
    baselineEndDate: str | None = None
    baselineLockedAt: str | None = None
    calculationVersion: str | None = None
    startedAt: str | None = None
    endedAt: str | None = None
    createdBy: str | None = None
    samples: list[dict[str, Any]] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    dataQuality: dict[str, Any] | None = None
    groupSummary: dict[str, Any] | None = None


class ExperimentReportResponse(_OpenAPIObject):
    experiment: dict[str, Any] | None = None
    calculationVersion: str | None = None
    generatedAt: str | None = None
    timezone: str | None = None
    reportDate: str | None = None
    baselineQuality: dict[str, Any] | None = None
    quality: dict[str, Any] | None = None
    groups: dict[str, Any] | None = None
    effect: dict[str, Any] | None = None
    operations: dict[str, Any] | None = None
    trend: list[dict[str, Any]] = Field(default_factory=list)
    queries: list[dict[str, Any]] = Field(default_factory=list)
    queryDailyMetrics: list[dict[str, Any]] = Field(default_factory=list)
    baselineEvidence: Any = None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    dataStartDate: str | None = None
    dataEndDate: str | None = None
    dataDays: int | None = None
    overview: dict[str, Any] | None = None
    reportSnapshotToken: str | None = None


class NoResultListResponse(_OpenAPIObject):
    items: list[dict[str, Any]] = Field(default_factory=list)
    total: int | None = None


class NoResultExecutionResponse(_OpenAPIObject):
    queryId: int | None = None
    status: str | None = None
    executedActions: int | None = None
    pendingActions: int | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str | list[dict[str, Any]] | None = None


ResponseModel = type[BaseModel]


@dataclass(frozen=True)
class ExternalReadOperation:
    path: str
    operation_id: str
    summary: str
    response_model: ResponseModel | None
    brand_boundary: str
    response_media_types: tuple[str, ...] = ("application/json",)
    auth_required: bool = True
    error_status_codes: tuple[int, ...] = (400, 401, 403, 404, 409, 422, 500)
    http_method: str = "get"
    exposure: str = "external-read"
    side_effects: str = "read-only"
    success_status_code: int = 200
    response_nullable: bool = False


EXTERNAL_READ_OPERATIONS: tuple[ExternalReadOperation, ...] = (
    ExternalReadOperation(
        "/api/health",
        "health_read",
        "Read service health",
        HealthResponse,
        "none",
        auth_required=False,
        error_status_codes=(500,),
    ),
    ExternalReadOperation("/api/dashboard/top-query/overview", "top_query_overview_read", "Read authorized brand overview", BrandOverviewResponse, "authorized brands"),
    ExternalReadOperation("/api/dashboard/top-query/brands/{brand}/summary", "top_query_brand_summary_read", "Read brand summary", BrandSummaryResponse, "path brand"),
    ExternalReadOperation("/api/dashboard/top-query/brands/{brand}/queries", "top_query_brand_queries_read", "Read brand query list", BrandQueryListResponse, "path brand"),
    ExternalReadOperation("/api/dashboard/top-query/brands/{brand}/trend", "top_query_brand_trend_read", "Read brand trend", BrandTrendResponse, "path brand"),
    ExternalReadOperation("/api/dashboard/top-query/brands/{brand}/metric-trend", "top_query_brand_metric_trend_read", "Read brand operations metric trend", BrandMetricTrendResponse, "path brand"),
    ExternalReadOperation("/api/dashboard/top-query/tasks", "top_query_tasks_read", "Read top query task list", TaskListResponse, "query brand or authorized brands"),
    ExternalReadOperation("/api/dashboard/top-query/queries/{task_id}", "top_query_detail_read", "Read top query detail", TaskDetailResponse, "task brand"),
    ExternalReadOperation("/api/dashboard/top-query/queries/{task_id}/workspace", "top_query_workspace_read", "Read top query detail workspace", TaskDetailResponse, "task brand"),
    ExternalReadOperation("/api/dashboard/top-query/queries/{task_id}/trend", "top_query_query_trend_read", "Read query trend", QueryTrendResponse, "task brand"),
    ExternalReadOperation("/api/dashboard/top-query/queries/{task_id}/current-result-count", "top_query_current_result_count_read", "Read current result count", ResultCountResponse, "task brand"),
    ExternalReadOperation("/api/dashboard/top-query/brands/{brand}/products/{product_id}", "top_query_product_detail_read", "Read product detail", ProductDetailResponse, "path brand and optional task brand"),
    ExternalReadOperation("/api/dashboard/top-query/brands/{brand}/snapshot/validate", "top_query_snapshot_validate_read", "Validate query snapshot", SnapshotValidationResponse, "path brand"),
    ExternalReadOperation("/api/dashboard/top-query/experiments/brands/{brand}", "top_query_experiments_read", "Read brand experiments", ExperimentListResponse, "path brand"),
    ExternalReadOperation("/api/dashboard/top-query/experiments/brands/{brand}/sample-pool", "top_query_experiment_sample_pool_read", "Read experiment sample pool", ExperimentSamplePoolResponse, "path brand"),
    ExternalReadOperation("/api/dashboard/top-query/experiments/{experiment_id}", "top_query_experiment_read", "Read experiment", ExperimentResponse, "experiment brand"),
    ExternalReadOperation("/api/dashboard/top-query/experiments/{experiment_id}/report", "top_query_experiment_report_read", "Read experiment report", ExperimentReportResponse, "experiment brand"),
    ExternalReadOperation(
        "/api/dashboard/top-query/experiments/{experiment_id}/export",
        "top_query_experiment_export_read",
        "Export experiment report",
        None,
        "experiment brand",
        response_media_types=("text/csv", "application/zip", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ),
    ExternalReadOperation("/api/dashboard/no-result-governance", "no_result_governance_read", "Read no-result governance list", NoResultListResponse, "query brand or authorized brands"),
    ExternalReadOperation("/api/dashboard/no-result-governance/queries/{query_id}/execution", "no_result_execution_read", "Read no-result execution state", NoResultExecutionResponse, "query brand"),
)


EXTERNAL_COMPUTE_OPERATIONS: tuple[ExternalReadOperation, ...] = (
    ExternalReadOperation(
        "/api/query-governance/relevance-checks",
        "manual_relevance_check_submit",
        "Submit a manual product relevance check",
        ManualRelevanceCheckSubmitResponse,
        "request brand",
        http_method="post",
        exposure="external-compute",
        side_effects="internal-job-state-only",
        success_status_code=202,
    ),
    ExternalReadOperation(
        "/api/query-governance/relevance-checks/latest",
        "manual_relevance_check_latest_read",
        "Read the latest manual product relevance check for a Query",
        ManualRelevanceCheckResponse,
        "query brand",
        exposure="external-compute",
        side_effects="read-only",
        response_nullable=True,
    ),
    ExternalReadOperation(
        "/api/query-governance/relevance-checks/{check_id}",
        "manual_relevance_check_read",
        "Read a manual product relevance check",
        ManualRelevanceCheckResponse,
        "stored job brand",
        exposure="external-compute",
        side_effects="read-only",
    ),
)


EXTERNAL_OPERATIONS = EXTERNAL_READ_OPERATIONS + EXTERNAL_COMPUTE_OPERATIONS


_RESPONSE_MODELS: tuple[ResponseModel, ...] = tuple(
    dict.fromkeys(
        operation.response_model
        for operation in EXTERNAL_OPERATIONS
        if operation.response_model is not None
    )
)


def _model_schema(model: ResponseModel) -> dict[str, Any]:
    return model.model_json_schema(ref_template="#/components/schemas/{model}")


def _error_response() -> dict[str, Any]:
    return {
        "description": "Request failed",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"}
            }
        },
    }


def _response_contract(operation: ExternalReadOperation) -> dict[str, Any]:
    if operation.response_model is None:
        content = {
            media_type: {"schema": {"type": "string", "format": "binary"}}
            for media_type in operation.response_media_types
        }
    else:
        model_ref = {"$ref": f"#/components/schemas/{operation.response_model.__name__}"}
        schema = (
            {"anyOf": [model_ref, {"type": "null"}]}
            if operation.response_nullable
            else model_ref
        )
        content = {
            media_type: {"schema": schema}
            for media_type in operation.response_media_types
        }
    responses: dict[str, Any] = {
        str(operation.success_status_code): {
            "description": "Successful response",
            "content": content,
        },
    }
    responses.update(
        {str(status_code): _error_response() for status_code in operation.error_status_codes}
    )
    return responses


def _apply_operation_contract(spec: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(spec)
    result.setdefault("components", {})
    result["components"].setdefault("schemas", {})
    result["components"]["schemas"]["ErrorResponse"] = ErrorResponse.model_json_schema()
    result["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Issued JWT. External consumers receive tokens through a controlled account flow.",
        }
    }

    for model in _RESPONSE_MODELS:
        result["components"]["schemas"][model.__name__] = _model_schema(model)

    operation_by_path = {
        (operation.path, operation.http_method): operation
        for operation in EXTERNAL_OPERATIONS
    }
    external_paths: dict[str, Any] = {}
    for (path, method), operation in operation_by_path.items():
        source_path = result.get("paths", {}).get(path)
        if not source_path or method not in source_path:
            raise RuntimeError(
                f"external contract route missing from FastAPI app: {method.upper()} {path}"
            )
        source_operation = source_path[method]
        contract_operation = deepcopy(source_operation)
        contract_operation.update(
            {
                "operationId": operation.operation_id,
                "summary": operation.summary,
                "tags": [
                    "External Read"
                    if operation.exposure == "external-read"
                    else "External Compute"
                ],
                "x-exposure": operation.exposure,
                "x-brand-boundary": operation.brand_boundary,
                "x-side-effects": operation.side_effects,
                "security": [] if not operation.auth_required else [{"BearerAuth": []}],
                "responses": _response_contract(operation),
            }
        )
        external_paths.setdefault(path, {})[method] = contract_operation

    result["paths"] = external_paths
    # Multipart request schemas belong to internal browser/runtime routes and
    # must not leak into the externally supported read-only contract.
    for schema_name in list(result["components"]["schemas"]):
        if schema_name.startswith("Body_upload_assistant_workspace_file_"):
            result["components"]["schemas"].pop(schema_name, None)
    result["info"] = {
        "title": "Search Ops Automation External API",
        "version": "2026-09-17",
        "description": "Versioned machine contract for externally supported read and compute operations.",
    }
    result["x-contract-version"] = "2026-09-17"
    result["x-auth-boundary"] = "Bearer JWT; all non-health operations enforce server-side brand permissions"
    return result


def install_openapi_contract(app: Any) -> None:
    """Attach stable metadata to the app OpenAPI without changing runtime routes."""
    if getattr(app, "_external_openapi_contract_installed", False):
        return
    original_openapi = app.openapi

    def contract_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = _apply_operation_contract(original_openapi())
        return app.openapi_schema

    app.openapi = contract_openapi
    app._external_openapi_contract_installed = True


def build_external_openapi(app: Any) -> dict[str, Any]:
    """Return the filtered external contract used by CI and release artifacts."""
    return _apply_operation_contract(app.openapi())


def external_operation_paths() -> tuple[str, ...]:
    return tuple(operation.path for operation in EXTERNAL_OPERATIONS)
