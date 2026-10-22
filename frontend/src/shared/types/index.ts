export interface PaginatedResponse<T> {
  total: number;
  items: T[];
}

export type QueryFilters = Record<string, unknown>;

export interface ActionLog {
  id: number;
  productName: string;
  imageUrl?: string | null;
  sku: string;
  brand: string;
  query: string;
  reason: string;
  createdAt: string;
  isCanceled?: boolean;
}

export interface RelevanceGuidance {
  id: number | null;
  brand: string;
  query: string;
  rules: string;
  status: 'active' | 'inactive' | 'preview' | string;
  updatedBy?: string | null;
  updatedAt?: string | null;
  reasonHistory?: Array<{
    id: number;
    productId: string;
    reason: string;
    operator: string;
    createdAt?: string | null;
  }>;
}

export interface ManualRelevanceProductResult {
  sku: string;
  status: string;
  isRelevant: boolean | null;
  reason?: string | null;
}

export interface ManualRelevanceQueryResult {
  query: string;
  status: string;
  products: ManualRelevanceProductResult[];
}

export interface ManualRelevanceCheckResponse {
  checkId: string;
  brand: string;
  status: string;
  createdAt?: string | null;
  updatedAt?: string | null;
  queries: ManualRelevanceQueryResult[];
}

export interface InventoryTask {
  id: number;
  productName: string;
  imageUrl?: string | null;
  sku: string;
  brand: string;
  query: string;
  lastStock: number;
  lastBrokenRate?: number;
  status: string;
  createdAt: string;
  updatedAt: string;
}

export interface UserInfo {
  id?: number;
  username: string;
  isAdmin: boolean;
  role?: 'platform_admin' | 'brand_admin' | 'brand_member' | string;
  pagePermissions: string[];
  brandPermissions: string[];
  actionPermissions?: string[];
  isActive?: boolean;
}

export interface SafetyConfig {
  circuit_breaker_threshold: number;
  max_retry_count: number;
  api_delay_threshold: number;
  health_check_stale_minutes?: number;
  health_check_error_rate_threshold_percent?: number;
  health_check_api_p95_threshold_seconds?: number;
}

export type PageConfigKey =
  | 'brand_overview.show_global_metrics'
  | 'brand_overview.global_metrics'
  | 'brand_overview.card.primary_metric'
  | 'brand_overview.card.metrics'
  | 'brand_overview.card.show_runtime'
  | 'brand_overview.card.show_operational_data'
  | 'brand_overview.drawer.summary_metrics'
  | 'brand_overview.drawer.show_chart'
  | 'brand_overview.drawer.show_table'
  | 'brand_overview.drawer.trend_metrics'
  | 'brand_detail.query_card.show_metrics'
  | 'brand_detail.query_card.metrics'
  | 'brand_detail.show_execution_overview'
  | 'brand_detail.yesterday.show'
  | 'brand_detail.yesterday.metrics'
  | 'brand_detail.tabs.show_governance_plans'
  | 'brand_detail.tabs.show_result_comparison'
  | 'brand_detail.tabs.show_relevance_rules';

export type PageConfigValue = boolean | string[];
export type PageConfigLegacyKey =
  | 'brand_overview.show_operational_data'
  | 'brand_detail.show_query_metrics'
  | 'brand_overview.primary_metric'
  | 'brand_overview.action_metric_1'
  | 'brand_overview.action_metric_2'
  | 'brand_overview.action_metric_3';

export interface PageConfigDefinition {
  key: PageConfigKey;
  groupKey: string;
  groupLabel: string;
  label: string;
  description: string;
  type: 'boolean' | 'metric_list';
  scope: 'global';
  defaultValue: PageConfigValue;
  order: number;
  options?: Array<{ value: string; label: string; tip?: string }>;
  minItems?: number;
  maxItems?: number;
}

export interface PageConfigPayload {
  schemaVersion: number;
  version: number;
  values: Partial<Record<PageConfigKey, PageConfigValue>> & Partial<Record<PageConfigLegacyKey, boolean | string>>;
  definitions: PageConfigDefinition[];
}

export interface PageConfigUpdatePayload {
  expectedVersion: number;
  values: Partial<Record<PageConfigKey, PageConfigValue>>;
}

export interface BrandConfig {
  enabled: boolean;
  no_result_governance_enabled?: boolean;
  no_result_extract_limit?: number;
  no_result_search_pv_threshold?: number;
  no_result_search_uv_threshold?: number;
  no_result_excluded_keywords?: string[];
  no_result_execution_mode?: string;
  no_result_auto_max_risk?: string;
  no_result_retag_cooldown_hours?: number;
  no_result_review_wait_minutes?: number;
  no_result_top_n?: number;
  no_result_agent_max_rounds?: number;
  no_result_ignore_ttl_days?: number;
  top_query_governance_enabled?: boolean;
  query_governance_extract_limit?: number;
  query_governance_query_concurrency?: number;
  query_governance_query_min_pv?: number;
  query_governance_query_min_metric_type?: 'uv' | 'pv' | string;
  query_governance_query_min_metric_value?: number;
  query_governance_excluded_keywords?: string[];
  query_governance_candidate_high_pv_limit?: number;
  query_governance_candidate_alert_ctr_gap_pp?: number;
  query_governance_candidate_alert_cvr_gap_pp?: number;
  query_governance_candidate_history_days?: number;
  query_governance_candidate_new_query_pv_threshold?: number;
  query_governance_candidate_pv_growth_ratio_floor?: number;
  query_governance_candidate_pv_growth_sqrt_multiplier?: number;
  query_governance_candidate_pv_growth_abs_floor?: number;
  query_governance_preference_top_purchase_product_limit?: number;
  query_governance_preference_top_click_product_limit?: number;
  query_governance_preference_generation_batch_query_limit?: number;
  query_governance_preference_generation_concurrency?: number;
  query_governance_preference_generation_timeout_seconds?: number;
  query_governance_kg_agent_timeout_seconds?: number;
  query_governance_kg_reference?: string;
  query_governance_kg_reference_updated_by?: string;
  query_governance_kg_reference_updated_at?: string;
  query_governance_reorder_enabled?: boolean;
  query_governance_vector_reorder_concurrency?: number;
  query_governance_vector_reorder_retry_attempts?: number;
  query_governance_vector_reorder_retry_backoff_seconds?: number;
  query_governance_relevance_enabled?: boolean;
  query_governance_relevance_multimodal_concurrency?: number;
  query_governance_relevance_batch_size?: number;
  query_governance_relevance_model_timeout_seconds?: number;
  query_governance_relevance_image_download_timeout_seconds?: number;
  query_governance_relevance_buckets?: string[];
  query_governance_recall_rate_enabled?: boolean;
  query_governance_recall_rate_product_batch_size?: number;
  query_governance_recall_rate_score_threshold?: number;
  query_governance_recall_rate_jev_threshold?: number;
  top_query_check_product_limit?: number;
  top_query_low_result_threshold?: number;
  top_query_result_drop_threshold?: number;
  top_query_inventory_broken_rate_threshold?: number;
  top_query_inventory_check_interval_minutes?: number;
  top_query_inventory_max_cycles?: number;
  top_query_world_knowledge_config?: {
    weather_cities?: Array<string | { city: string; city_code: number | string }>;
    weather_mcp?: { command?: string; args?: string[]; timeout?: number; tool_name?: string; env?: Record<string, string> };
    festival_mcp?: { command?: string; args?: string[]; timeout?: number; tool_name?: string; env?: Record<string, string> };
    social_trends?: Array<Record<string, unknown>>;
    tag_rules?: Array<Record<string, unknown>>;
    default_filters?: Array<Record<string, unknown>>;
    default_after_bucket?: string;
  };
  top_query_check_modes?: Record<string, TopQueryExecutionMode>;
  top_query_tool_modes?: Record<string, TopQueryExecutionMode>;
}

export interface AppConfig {
  safety_config?: SafetyConfig;
  brand_configs: Record<string, BrandConfig>;
}

export interface NodeModelConfigNode {
  nodeKey: string;
  channel: string;
  primaryModel: string;
  fallbackModel: string | null;
  primarySource?: string;
  fallbackSource?: string;
  requiredCapabilities?: string[];
}

export interface NodeModelConfigResponse {
  activeVersion: number | null;
  nodes: NodeModelConfigNode[];
}

export interface NodeModelConfigDraftPayload {
  entries: Record<string, {
    channel: string;
    primaryModel: string;
    fallbackModel: string | null;
  }>;
  changeNote?: string;
  requestId?: string;
}

export interface NodeModelConfigDraftResponse {
  status: string;
  version: number;
  state: string;
  nodes: NodeModelConfigNode[];
}

export interface NodeModelConfigPublishResponse {
  status: string;
  activeVersion: number;
  nodes: NodeModelConfigNode[];
}

export interface NodeModelConfigRollbackPayload {
  targetVersion: number;
  expectedVersion: number | null;
  requestId?: string;
}

export interface NodeModelConfigRollbackResponse {
  status: string;
  activeVersion: number;
  nodes: NodeModelConfigNode[];
  rolledBackFromVersion?: number;
  rolledBackToVersion?: number;
}

export type TopQueryExperimentStatus = 'draft' | 'scheduled' | 'running' | 'ended' | 'cancelled' | string;
export type TopQueryExperimentGroupType = 'treatment' | 'control' | string;

export interface TopQueryExperimentSamplePoolItem {
  queryText: string;
  governanceKey: string;
  queryRank: number;
  searchUv: number;
  searchPv: number;
  ctr: number;
  cvr: number;
  avgCtr: number;
  avgCvr: number;
  queryPvShare: number;
}

export interface TopQueryExperimentSamplePoolData {
  brand: string;
  sampleDataDate: string;
  items: TopQueryExperimentSamplePoolItem[];
}

export interface TopQueryExperimentSampleItem {
  queryText: string;
  groupType: TopQueryExperimentGroupType;
  sampleRank: number;
  sampleSearchUv: number;
  sampleSearchPv: number;
  sampleCtr: number;
  sampleCvr: number;
  sampleAvgCtr?: number | null;
  sampleAvgCvr?: number | null;
  sampleQueryPvShare?: number | null;
  baselineObservedDays?: number | null;
  baselineSearchUv?: number | null;
  baselineSearchPv?: number | null;
  baselineClickUv?: number | null;
  baselinePurchaseUv?: number | null;
  baselineCtr?: number | null;
  baselineCvr?: number | null;
}

export interface TopQueryExperimentDetailItem {
  queryText: string;
  groupType: TopQueryExperimentGroupType;
  sampleRank: number;
  sampleSearchPv: number;
  sampleSearchUv: number;
  sampleCtr: number;
  sampleCvr: number;
  observedStatus?: string;
  contaminationStatus?: string;
  contaminationReason?: string | null;
  latestSearchPv?: number | null;
  latestSearchUv?: number | null;
  latestCtr?: number | null;
  latestCvr?: number | null;
  governanceTaskId?: number | null;
  governanceStatus?: string | null;
}

export interface TopQueryExperimentGroupSummary {
  observedCount: number;
  cleanCount: number;
  contaminatedCount: number;
  avgCtr: number;
  avgCvr: number;
  totalSearchPv: number;
  totalSearchUv: number;
}

export interface TopQueryExperimentDetailData {
  id: number;
  brand: string;
  experimentName?: string;
  status: TopQueryExperimentStatus;
  sampleDataDate?: string | null;
  plannedStartDate?: string | null;
  plannedEndDate?: string | null;
  baselineStartDate?: string | null;
  baselineEndDate?: string | null;
  baselineLockedAt?: string | null;
  calculationVersion?: string;
  startedAt?: string | null;
  endedAt?: string | null;
  createdBy?: string | null;
  samples: TopQueryExperimentSampleItem[];
  items?: TopQueryExperimentDetailItem[];
  groupSummary?: Partial<Record<'treatment' | 'control', TopQueryExperimentGroupSummary>>;
}

export interface TopQueryExperimentHistorySummary {
  id: number;
  brand: string;
  experimentName?: string | null;
  status: TopQueryExperimentStatus;
  sampleDataDate?: string | null;
  plannedStartDate?: string | null;
  plannedEndDate?: string | null;
  startedAt?: string | null;
  endedAt?: string | null;
  createdBy?: string | null;
  treatmentCount: number;
  controlCount: number;
  quality?: Record<string, unknown> | null;
}

export interface TopQueryBrandExperimentListData {
  brand: string;
  current?: TopQueryExperimentHistorySummary | null;
  items: TopQueryExperimentHistorySummary[];
  total?: number;
  page?: number;
  pageSize?: number;
}

export interface TopQueryExperimentDayQuality {
  [key: string]: unknown;
  status?: 'complete' | 'failed' | string;
  error?: string | null;
  expectedSamples?: number;
  observedSamples?: number;
  cleanObservedSamples?: number;
  treatmentSamples?: number;
  controlSamples?: number;
  treatmentTaskCount?: number;
  controlTaskCount?: number;
  controlActionCount?: number;
  contaminationCount?: number;
  failedTaskCount?: number;
  unresolvedTaskCount?: number;
  failedActionCount?: number;
  inconclusiveActionCount?: number;
  unresolvedActionCount?: number;
  invalidNumeratorCount?: number;
  baselineMissingKeys?: string[];
  zeroBaselineGroups?: string[];
}

export interface TopQueryExperimentQuality {
  status: 'complete' | 'failed' | string;
  error?: string | null;
  completeDays?: number;
  failedDays?: number;
  latestDataDate?: string;
  latestDay?: TopQueryExperimentDayQuality;
  failedDates?: string[];
  latestDayQuality?: TopQueryExperimentDayQuality | null;
  expectedSamples?: number;
  observedSamples?: number;
  cleanObservedSamples?: number;
  treatmentSamples?: number;
  controlSamples?: number;
  treatmentTaskCount?: number;
  controlTaskCount?: number;
  controlActionCount?: number;
  contaminationCount?: number;
  failedTaskCount?: number;
  unresolvedTaskCount?: number;
  failedActionCount?: number;
  inconclusiveActionCount?: number;
  unresolvedActionCount?: number;
  baselineMissingKeys?: string[];
  zeroBaselineGroups?: string[];
  invalidNumeratorCount?: number;
}

export interface TopQueryExperimentGroupAnalytics {
  sampleCount: number;
  observedRows?: number;
  searchUv: number;
  searchPv: number;
  clickUv?: number;
  purchaseUv?: number;
  currentSearchUv?: number;
  currentSearchPv?: number;
  currentClickUv?: number;
  currentPurchaseUv?: number;
  baselineSearchUv?: number;
  baselineSearchPv?: number;
  baselineClickUv?: number;
  baselinePurchaseUv?: number;
  baselineValidSampleCount?: number;
  baselineCtr: number | null;
  baselineCvr: number | null;
  currentCtr: number | null;
  currentCvr: number | null;
  ctrChange: number | null;
  cvrChange: number | null;
}

export interface TopQueryExperimentOperations {
  governanceCoverage: number;
  actionSuccessRate: number | null;
  plannedActions: number;
  executedActions: number;
  failedActions: number;
  inconclusiveActions?: number;
  unresolvedActions?: number;
  rejectedActions?: number;
  cancelledActions?: number;
  tokenUsage: number;
  durationSeconds: number;
}

export interface TopQueryExperimentOverviewData {
  experimentId: number;
  brand: string;
  experimentName?: string | null;
  status: TopQueryExperimentStatus;
  quality: TopQueryExperimentQuality;
  groups: Partial<Record<'treatment' | 'control', TopQueryExperimentGroupAnalytics>>;
  effect: {
    ctrNetChange: number | null;
    cvrNetChange: number | null;
    ctrPositiveDays: number | null;
    cvrPositiveDays: number | null;
    estimatedIncrementalClickUv: number | null;
    estimatedIncrementalPurchaseUv: number | null;
  } | null;
  operations: TopQueryExperimentOperations;
}

export interface TopQueryExperimentTrendItem {
  dataDate: string;
  quality: TopQueryExperimentQuality;
  groups: Partial<Record<'treatment' | 'control', TopQueryExperimentGroupAnalytics>>;
  effect: { ctrNetChange: number | null; cvrNetChange: number | null } | null;
  operations: TopQueryExperimentOperations;
}

export interface TopQueryExperimentTrendData {
  experimentId: number;
  brand: string;
  items: TopQueryExperimentTrendItem[];
}

export interface TopQueryExperimentQueryAnalytics {
  queryText: string;
  groupType: TopQueryExperimentGroupType;
  sampleRank: number;
  baselineSearchUv: number;
  baselineSearchPv?: number | null;
  baselineClickUv?: number | null;
  baselinePurchaseUv?: number | null;
  baselineObservedDays?: number | null;
  baselineCtr: number | null;
  baselineCvr: number | null;
  latestDataDate: string | null;
  observedStatus: string;
  contaminationStatus: string;
  currentSearchUv: number | null;
  currentSearchPv: number | null;
  currentClickUv?: number | null;
  currentPurchaseUv?: number | null;
  currentCtr: number | null;
  currentCvr: number | null;
  ctrChange: number | null;
  cvrChange: number | null;
  observedDays?: number;
  unobservedDays?: number;
  contaminatedDays?: number;
  contaminationReason?: string | null;
  numeratorSource?: 'raw' | 'mixed' | string | null;
  dataQualityStatus?: string;
  groupContribution: { ctr: number | null; cvr: number | null };
  netCtrContribution: number | null;
  netCvrContribution: number | null;
  effectDirection: 'positive' | 'negative' | 'mixed' | 'neutral' | 'unavailable' | 'undetermined' | (string & {});
  governanceTaskId: number | null;
  governanceStatus: string | null;
  actionCount: number;
  tokenUsage: number;
  durationSeconds: number;
}

export interface TopQueryExperimentQueriesData {
  experimentId: number;
  brand: string;
  items: TopQueryExperimentQueryAnalytics[];
  total: number;
  page: number;
  pageSize: number;
}

export interface TopQueryExperimentMetadata {
  experimentId: number;
  brand: string;
  experimentName?: string | null;
  status: TopQueryExperimentStatus;
  sampleDataDate?: string | null;
  plannedStartDate?: string | null;
  plannedEndDate?: string | null;
  baselineStartDate?: string | null;
  baselineEndDate?: string | null;
  baselineLockedAt?: string | null;
  calculationVersion: string;
  startedAt?: string | null;
  endedAt?: string | null;
}

export interface TopQueryExperimentBaselineQuality extends TopQueryExperimentQuality {
  calculationVersion: string;
  sampleCount: number;
  treatmentSamples: number;
  controlSamples: number;
  expectedDays: number;
  baselineStartDate?: string | null;
  baselineEndDate?: string | null;
  baselineLockedAt?: string | null;
  windowValid: boolean;
  locked: boolean;
}

export interface TopQueryExperimentBaselineEvidence {
  queryText: string;
  groupType: TopQueryExperimentGroupType;
  sampleRank: number;
  sampleSearchUv: number;
  sampleSearchPv: number;
  sampleCtr: number;
  sampleCvr: number;
  sampleAvgCtr?: number | null;
  sampleAvgCvr?: number | null;
  sampleQueryPvShare?: number | null;
  baselineWindowType: 'seven_day_weighted' | string;
  baselineStartDate?: string | null;
  baselineEndDate?: string | null;
  baselineObservedDays?: number | null;
  baselineSearchUv?: number | null;
  baselineSearchPv?: number | null;
  baselineClickUv?: number | null;
  baselinePurchaseUv?: number | null;
  baselineCtr?: number | null;
  baselineCvr?: number | null;
  qualityStatus: string;
  qualityError?: string | null;
}

export interface TopQueryExperimentDailyMetric {
  dataDate: string | null;
  queryText: string;
  groupType: TopQueryExperimentGroupType;
  canonicalQueryKey?: string | null;
  lockedQueryText?: string | null;
  lockedGroupType?: TopQueryExperimentGroupType | null;
  identityStatus?: 'complete' | 'failed' | string;
  identityError?: string | null;
  observedStatus: string;
  contaminationStatus: string;
  contaminationReason?: string | null;
  searchUv?: number | null;
  searchPv?: number | null;
  clickUv?: number | null;
  purchaseUv?: number | null;
  ctr?: number | null;
  cvr?: number | null;
  numeratorSource: 'raw' | 'mixed' | 'partial' | 'missing' | 'invalid' | string;
  clickNumeratorSource: 'raw' | 'missing' | 'invalid' | string;
  purchaseNumeratorSource: 'raw' | 'missing' | 'invalid' | string;
  clickNumeratorStatus: 'complete' | 'failed' | string;
  purchaseNumeratorStatus: 'complete' | 'failed' | string;
  clickNumeratorError?: string | null;
  purchaseNumeratorError?: string | null;
  dataQualityStatus: string;
  dataQualityError?: string | null;
  includedInEffect: boolean;
  governanceTaskId?: number | null;
  governanceStatus?: string | null;
  actionCount: number;
  autoActionCount: number;
  manualActionCount: number;
  tokenUsage: number;
  durationSeconds: number;
}

export interface TopQueryExperimentActionAudit {
  recordType: 'action' | 'summary' | string;
  detailAvailable: boolean;
  missingDetailReason?: string | null;
  dataDate?: string | null;
  groupType?: TopQueryExperimentGroupType | null;
  governanceStatus?: string | null;
  id?: number;
  taskId?: number | null;
  brand?: string | null;
  queryText?: string | null;
  checkKey?: string | null;
  actionType?: string | null;
  executionMode?: string | null;
  status?: string | null;
  reason?: string | null;
  errorMessage?: string | null;
  createdAt?: string | null;
  planned?: number;
  executed?: number;
  failed?: number;
  inconclusive?: number;
  unresolved?: number;
  rejected?: number;
  cancelled?: number;
}

export interface TopQueryExperimentReportData {
  reportSnapshotToken: string;
  experiment: TopQueryExperimentMetadata;
  calculationVersion: string;
  generatedAt: string;
  timezone: 'Asia/Shanghai' | string;
  reportDate?: string;
  baselineQuality: TopQueryExperimentBaselineQuality;
  quality: TopQueryExperimentQuality;
  groups: Partial<Record<'treatment' | 'control', TopQueryExperimentGroupAnalytics>>;
  effect: TopQueryExperimentOverviewData['effect'];
  operations: TopQueryExperimentOperations;
  trend: TopQueryExperimentTrendItem[];
  queries: TopQueryExperimentQueryAnalytics[];
  queryDailyMetrics: TopQueryExperimentDailyMetric[];
  baselineEvidence: TopQueryExperimentBaselineEvidence[];
  actions: TopQueryExperimentActionAudit[];
  dataStartDate?: string | null;
  dataEndDate?: string | null;
  dataDays: number;
  overview: TopQueryExperimentOverviewData;
}

export type QueryGovernanceStatus = 'pending' | 'running' | 'executed' | 'skipped' | 'error';
export type QueryGovernanceNodeKey = 's0' | 'recall' | 'reorder' | 'relevance';

export type QueryGovernanceStatusCounts = Record<QueryGovernanceStatus, number>;

export interface QueryGovernanceDurationStats {
  averageSeconds: number | null;
  p50Seconds: number | null;
  p95Seconds: number | null;
}

export interface QueryGovernanceQueryTelemetry {
  endToEndSeconds: number | null;
  nodeWallClockSeconds: number | null;
  independentProcessingSeconds: number | null;
  stageDurations: Record<string, number>;
  roundDurations: number[];
  tokenUsage?: number | null;
  tokenUsageMissing?: boolean;
  relevanceTokenUsage?: number | null;
  relevanceTokenUsageMissing?: boolean;
}

export interface QueryGovernanceBatchTelemetry {
  wallClockSeconds: number | null;
  queryCount: number;
  productCount: number | null;
  batchCount: number | null;
  roundCount: number | null;
  queryEndToEnd: QueryGovernanceDurationStats;
  independentProcessing: QueryGovernanceDurationStats;
  modelSeconds: number | null;
  actionSeconds: number | null;
  recheckSeconds: number | null;
  maxConcurrency: number | null;
  modelCallCount: number | null;
  retryCount: number | null;
  truncationCount: number | null;
  imageSuccessCount: number | null;
  imageFailureCount: number | null;
  tokenUsage: number | null;
  detectedProductCount: number;
  reusedProductCount?: number;
  inspectedProductCount?: number;
  irrelevantProductCount: number;
  irrelevantProductPairCount?: number;
  blacklistedProductCount?: number | null;
  relevanceDetectionAccuracy?: number | null;
  avgSearchRecallRate?: number | null;
  recallRateQueryCount?: number | null;
  searchRecallRateSum?: number | null;
  irrelevantQueryCount: number;
  noRecallCount?: number;
  noRecallRate?: number | null;
  passCount?: number;
  passRate?: number | null;
  aiProcessedCount?: number;
  pendingCount?: number;
  relevanceAccuracy: number | null;
  irrelevantRate?: number | null;
  preferenceAdjustedQueryCount: number;
  highPvQueryCount: number;
  topQueryLimit: number | null;
  rapidPvGrowthQueryCount: number;
  newQueryCount: number;
  ctrDeclineQueryCount: number;
  cvrDeclineQueryCount: number;
  ctrDeclineThresholdPp?: number | null;
  cvrDeclineThresholdPp?: number | null;
  stockBucketReductionProductCount: number;
  relevanceAverageSeconds: number | null;
  preferenceAverageSeconds: number | null;
  relevanceTokenUsage?: number | null;
  preferenceTokenUsage?: number | null;
}

export interface QueryGovernanceNode {
  nodeKey: QueryGovernanceNodeKey;
  status: QueryGovernanceStatus;
  result: Record<string, unknown> | null;
  errorCode: string | null;
  errorMessage: string | null;
  durationSeconds: number | null;
  tokenUsage?: number | null;
  tokenUsageMissing?: boolean;
  startedAt?: string | null;
  completedAt?: string | null;
  telemetry?: Record<string, unknown>;
}

export interface QueryGovernanceQuery {
  id: number;
  batchId: string;
  brand: string;
  dataDate: string;
  queryText: string;
  queryRank: number | null;
  status: QueryGovernanceStatus;
  errorCode: string | null;
  errorMessage: string | null;
  durationSeconds: number;
  telemetry?: QueryGovernanceQueryTelemetry;
  nodes: QueryGovernanceNode[];
  experimentId?: number | null;
  groupType?: string | null;
  /** Optional legacy display metrics. The deterministic API may omit them. */
  searchPv?: number | null;
  ctr?: number | null;
  cvr?: number | null;
  resultCount?: number | null;
}

export interface QueryGovernanceBrandMetrics {
  searchUv?: number | null;
  clickUv?: number | null;
  addToCartUv?: number | null;
  purchaseUv?: number | null;
  ctr?: number | null;
  atc?: number | null;
  cvr?: number | null;
}

export interface QueryGovernanceBatch {
  reason?: string | null;
  batchId: string;
  brand: string;
  dataDate: string | null;
  status: QueryGovernanceStatus;
  currentNode: string | null;
  modelConfigVersion: number | null;
  startedAt: string | null;
  completedAt: string | null;
  telemetry?: QueryGovernanceBatchTelemetry;
}

export interface QueryGovernanceOverviewBrand {
  reason?: string | null;
  brand: string;
  batchId: string;
  dataDate: string | null;
  status: QueryGovernanceStatus;
  queryCount: number;
  nodeStatusCounts: QueryGovernanceStatusCounts;
  durationSeconds: number;
  startedAt: string | null;
  completedAt: string | null;
  telemetry?: QueryGovernanceBatchTelemetry;
}

export interface QueryGovernanceOverviewData {
  brands: QueryGovernanceOverviewBrand[];
}

export interface QueryGovernanceBrandSummary {
  queryCount: number;
  queryStatusCounts: QueryGovernanceStatusCounts;
  nodeStatusCounts: QueryGovernanceStatusCounts;
}

export interface QueryGovernanceModuleSlot {
  available: boolean;
  source?: string;
  status?: string;
}

export interface QueryGovernanceBrandSlots {
  kgAgent: QueryGovernanceModuleSlot;
  sharedFacts: QueryGovernanceModuleSlot;
  preference: QueryGovernanceModuleSlot;
  relevance: QueryGovernanceModuleSlot;
  inventory: QueryGovernanceModuleSlot;
  modelConfig: QueryGovernanceModuleSlot;
  abTest: QueryGovernanceModuleSlot;
}

export interface QueryGovernanceBrandDetailData {
  brand: string;
  dataDate?: string | null;
  batch: QueryGovernanceBatch | null;
  summary: QueryGovernanceBrandSummary;
  queries: QueryGovernanceQuery[];
  slots: QueryGovernanceBrandSlots;
  /** Optional legacy funnel metrics; absent values must render as unavailable. */
  metrics?: QueryGovernanceBrandMetrics | null;
  /** Persisted business metrics used by the brand overview data drawer. */
  stats?: TopQueryBrandStats | null;
  trend?: TopQueryTrendPoint[];
}

export type TopQueryDisplayStatus =
  | 'normal'
  | 'fixed'
  | 'waiting_human_approval'
  | 'still_problematic'
  | 'pass'
  | 'failed'
  | 'warning'
  | 'check_failed'
  | string;

export type TopQueryExecutionMode = 'disabled' | 'human_approval' | 'auto' | string;

export interface TopQueryStepStatus {
  checkKey: 'recall' | 'relevance' | 'open_factor' | 'inventory' | string;
  status: 'pass' | 'fixed' | 'failed' | 'warning' | 'check_failed' | 'waiting_human_approval' | string;
  displayStatus: TopQueryDisplayStatus;
  summary?: string | null;
  evidence?: unknown;
  governanceStatus?: {
    status?: string | null;
    label?: string | null;
    errorCode?: string | null;
    errorMessage?: string | null;
  } | null;
}

export interface TopQueryExecutionNode {
  nodeKey: string;
  status?: string;
  result?: unknown;
  errorCode?: string | null;
  errorMessage?: string | null;
  durationSeconds?: number | null;
  tokenUsage?: number | null;
  tokenUsageMissing?: boolean;
}

export interface TopQueryOverviewBrand {
  brand: string;
  dataDate?: string | null;
  executionDate?: string | null;
  brandUv: number;
  brandPv: number;
  clickUv?: number;
  addToCartUv?: number;
  purchaseUv?: number;
  purchaseAmount?: number;
  ctr: number;
  atc?: number;
  cvr: number;
  avgBrandUv7d?: number | null;
  avgClickUv7d?: number | null;
  avgAddToCartUv7d?: number | null;
  avgPurchaseUv7d?: number | null;
  avgCtr7d?: number | null;
  avgAtc7d?: number | null;
  avgCvr7d?: number | null;
  brandUvDelta7d?: number | null;
  clickUvDelta7d?: number | null;
  addToCartUvDelta7d?: number | null;
  purchaseUvDelta7d?: number | null;
  ctrDelta7d?: number | null;
  atcDelta7d?: number | null;
  cvrDelta7d?: number | null;
  metricSource?: string;
  topQueryCount: number;
  topQueryPvShare?: number | null;
  needsGovernanceCount?: number;
  autoCompletedCount?: number;
  manualRequiredCount: number;
  durationSeconds?: number;
  tokenUsage?: number;
  experiment?: TopQueryExperimentSummary | null;
}

export interface TopQueryOverviewData {
  brands: TopQueryOverviewBrand[];
}

export interface TopQueryTask {
  telemetry?: QueryGovernanceQueryTelemetry;
  id: number;
  brand: string;
  dataDate?: string | null;
  queryText: string;
  queryRank: number;
  searchUv: number;
  searchPv: number;
  ctr: number;
  cvr: number;
  avgCtr?: number | null;
  avgCvr?: number | null;
  queryPvShare?: number | null;
  status: string;
  displayStatus: TopQueryDisplayStatus;
  resultCount?: number | null;
  stepStatuses?: TopQueryStepStatus[];
  querySource?: 'top_query' | 'hot_query' | string;
  hotWordType?: string | null;
  hotStatus?: string | null;
  invalidated?: boolean;
  visibleCheckKeys?: string[];
}

export interface TopQueryBrandStats {
  topQueryCount: number;
  topQueryPvShare?: number | null;
  noResultOrLowResultCount?: number;
  resultDropCount?: number;
  relevanceIssueCount?: number;
  openFactorIssueCount?: number;
  inventoryProductIssueCount?: number;
  manualRequiredCount: number;
  completedCount: number;
  stepIssueCounts?: Record<string, number>;
  brandUv: number;
  brandPv: number;
  clickUv?: number;
  addToCartUv?: number;
  purchaseUv?: number;
  purchaseAmount?: number;
  ctr: number;
  atc?: number;
  cvr: number;
  avgBrandUv7d?: number | null;
  avgClickUv7d?: number | null;
  avgAddToCartUv7d?: number | null;
  avgPurchaseUv7d?: number | null;
  avgCtr7d?: number | null;
  avgAtc7d?: number | null;
  avgCvr7d?: number | null;
  brandUvDelta7d?: number | null;
  clickUvDelta7d?: number | null;
  addToCartUvDelta7d?: number | null;
  purchaseUvDelta7d?: number | null;
  ctrDelta7d?: number | null;
  atcDelta7d?: number | null;
  cvrDelta7d?: number | null;
  metricSource?: string;
  durationSeconds?: number;
  tokenUsage?: number;
  experimentMode?: boolean;
}

export interface TopQueryExperimentSummary {
  id: number;
  experimentName?: string | null;
  status: string;
  sampleDataDate?: string | null;
  plannedStartDate?: string | null;
  plannedEndDate?: string | null;
  treatmentQueryCount: number;
  controlQueryCount: number;
}

export interface TopQueryBrandDetailData {
  brand: string;
  dataDate?: string | null;
  executionDate?: string | null;
  stats: TopQueryBrandStats;
  trend?: TopQueryTrendPoint[];
  queries: TopQueryTask[];
  experiment?: TopQueryExperimentSummary | null;
}

export interface TopQueryBrandSummaryData {
  brand: string;
  dataDate?: string | null;
  executionDate?: string | null;
  batch?: QueryGovernanceBatch | null;
  summary?: QueryGovernanceBrandSummary;
  stats: TopQueryBrandStats;
  experiment?: TopQueryExperimentSummary | null;
}

export interface TopQueryBrandQueriesData {
  brand: string;
  reason?: string | null;
  dataDate?: string | null;
  executionDate?: string | null;
  queries: TopQueryTask[];
  total: number;
  page?: number;
  pageSize?: number;
  hasNext?: boolean;
  filter?: string;
  stats?: TopQueryBrandStats;
  experiment?: TopQueryExperimentSummary | null;
  visibleCheckKeys?: string[];
}

export interface TopQueryBrandTrendData {
  brand: string;
  dataDate?: string | null;
  trend: TopQueryTrendPoint[];
}

export interface TopQueryBrandMetricTrendDay {
  date: string;
  batchId?: string | null;
  metrics?: Record<string, number | null> | null;
}

export interface TopQueryBrandMetricTrendData {
  brand: string;
  startDate?: string | null;
  endDate?: string | null;
  days: TopQueryBrandMetricTrendDay[];
}

export interface TopQueryQueryTrendData {
  taskId: number;
  brand: string;
  queryText: string;
  trend: TopQueryTrendPoint[];
}

export interface TopQueryCurrentResultCountData {
  taskId: number;
  resultCount: number | null;
  products?: Record<string, unknown>[];
  productDisplayLimit?: number;
}

export type TopQueryActionVerificationStatus = 'verified' | 'verification_failed' | 'verification_inconclusive';

export type TopQueryActionStatus =
  | 'waiting_human_approval'
  | 'approved'
  | 'dry_run_executed'
  | 'executed'
  | 'verifying'
  | 'execution_unknown'
  | TopQueryActionVerificationStatus
  | 'rollback_pending'
  | 'rolled_back'
  | 'rollback_failed'
  | 'completed'
  | 'failed'
  | 'rejected'
  | (string & {});

export interface TopQueryActionVerificationSnapshot {
  targets?: string[];
  observedRemainingIds?: string[];
  remainingIds?: string[];
  unverifiedIds?: string[];
  result?: TopQueryActionVerificationStatus;
  verifiedAt?: string | null;
  reason?: string | null;
  resultCount?: number | null;
  checkedProductCount?: number | null;
  [key: string]: unknown;
}

export type QueryDemotionEvidenceStatus = 'pending' | 'passed' | 'failed' | 'inconclusive' | (string & {});

export interface QueryDemotionConfigVerification {
  status: QueryDemotionEvidenceStatus;
  expectedIds?: string[];
  actualIds?: string[];
  missingIds?: string[];
  unexpectedIds?: string[];
  version?: string | null;
  expectedVersion?: string | null;
  actualVersion?: string | null;
  reason?: string | null;
  errorReason?: string | null;
  verifiedAt?: string | null;
  checkedAt?: string | null;
  [key: string]: unknown;
}

export interface QueryDemotionSearchEffect {
  status: QueryDemotionEvidenceStatus;
  observedRemainingIds?: string[];
  unverifiedIds?: string[];
  resultCount?: number | null;
  resultComplete?: boolean;
  reason?: string | null;
  errorReason?: string | null;
  verifiedAt?: string | null;
  checkedAt?: string | null;
  [key: string]: unknown;
}

export interface QueryDemotionRollbackVerification {
  status: QueryDemotionEvidenceStatus;
  expectedIds?: string[];
  actualIds?: string[];
  missingIds?: string[];
  unexpectedIds?: string[];
  version?: string | null;
  expectedVersion?: string | null;
  actualVersion?: string | null;
  reason?: string | null;
  errorReason?: string | null;
  verifiedAt?: string | null;
  checkedAt?: string | null;
  [key: string]: unknown;
}

export interface TopQueryAction {
  id: number;
  taskId: number;
  checkKey: string;
  toolKey: string;
  actionType: string;
  executionMode: TopQueryExecutionMode;
  executorType?: 'ai_auto' | 'human' | null;
  executorUserId?: string | null;
  failureStage?: 'generation' | 'execution' | 'verification' | 'rollback' | null;
  status: TopQueryActionStatus;
  reason?: string | null;
  requestPayload?: unknown;
  responsePayload?: unknown;
  productIds?: string[];
  request?: unknown;
  response?: unknown;
  before?: unknown;
  after?: TopQueryActionVerificationSnapshot | null;
  configDiff?: unknown;
  kgPlan?: {
    actionType?: string | null;
    params?: Record<string, unknown>;
    riskLevel?: string | null;
    evidence?: unknown;
    expectedImpact?: string | null;
    negativeImpact?: string | null;
    rollbackSuggestion?: string | null;
  } | null;
  outcome?: {
    summary?: string;
    detailLines?: string[];
    tone?: 'success' | 'warning' | 'danger' | 'neutral' | string;
  } | null;
  statusLabel?: string | null;
  statusTone?: 'success' | 'warning' | 'danger' | 'neutral' | string | null;
  configVerification?: QueryDemotionConfigVerification | null;
  searchEffect?: QueryDemotionSearchEffect | null;
  rollbackVerification?: QueryDemotionRollbackVerification | null;
  rollbackAvailable?: boolean;
  rollbackStatus?: string | null;
  rollbackActionId?: number | null;
  proposedAt?: string | null;
  approvedAt?: string | null;
  executedAt?: string | null;
  verifiedAt?: string | null;
  lifecycle?: {
    proposedAt?: string | null;
    approvedAt?: string | null;
    executedAt?: string | null;
    verifiedAt?: string | null;
    executionStartedAt?: string | null;
    executionFinishedAt?: string | null;
    durationSeconds?: number | null;
    [key: string]: unknown;
  } | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  duration?: number | null;
  tokenUsage?: number | null;
  riskLevel?: 'low' | 'medium' | 'high' | string;
  idempotencyKey?: string | null;
  supersededByActionId?: number | null;
  proposalKind?: 'primary' | 'alternative' | string | null;
  alternativeOfActionId?: number | null;
}

export interface QueryDemotionTargetProduct {
  productId: string;
  productName: string | null;
  currentPrice: number | string | null;
  referencePrice: number | string | null;
  rank: number | string | null;
  checkKey: string[];
  actionIds: number[];
}

export interface QueryDemotionExecutionPreview {
  taskId: number;
  brand: string;
  query: string;
  actionIds: number[];
  targetIds: string[];
  beforeIds: string[];
  desiredIds: string[];
  addedIds: string[];
  retainedIds: string[];
  removedIds: string[];
  targetProducts: QueryDemotionTargetProduct[];
  configVersion: string | null;
  rollbackAvailable: boolean;
  executable: boolean;
  blockedReason: string | null;
  requiresRebuild?: boolean;
}

export interface QueryDemotionRollbackResponse {
  rollbackActionId: number;
  status: string;
}

export interface TopQueryPlannerPlan {
  governanceGoal?: string | null;
  optimizationHypothesis?: string | null;
  executionStrategy?: string | null;
  tasks?: Array<{ executor: string; goal: string; successCriteria?: string | null }>;
  successCriteria?: string[] | string | null;
  replanTriggers?: string[] | string | null;
  reviewReason?: string | null;
}

export type TopQueryGovernancePlanStatus =
  | 'pending'
  | 'executing'
  | 'verified'
  | 'failed'
  | 'execution_unknown'
  | 'rollback_pending'
  | 'rolled_back'
  | 'rollback_failed'
  | 'rejected';

export type TopQueryGovernancePlanStatusLabel =
  | '待执行'
  | '执行中'
  | 'AI 已执行'
  | '手动已执行'
  | 'AI 执行失败'
  | '手动执行失败'
  | '结果待确认'
  | '回滚中'
  | '已回滚'
  | '回滚失败'
  | '已取消'
  | '生成中'
  | '生成失败'
  | '无需调整'
  | '需人工复核';

export interface TopQueryGovernancePlan {
  id: string;
  taskId: number;
  actionId: number | null;
  type?: 'kg' | 'reorder' | 'blacklist' | 'demotion' | 'demotion_restore' | string;
  actionType: string;
  title: string;
  objective: string;
  summary?: string | null;
  steps: Array<TopQueryGovernanceActionStep & {
    index: number;
    title: string;
    description: string;
  }>;
  evidence: Array<{
    label: string;
    value: string;
    tone?: 'default' | 'warning' | 'danger' | string;
  }>;
  impact?: string[];
  riskNotes?: string[];
  executionMode: TopQueryExecutionMode;
  executorType?: 'ai_auto' | 'human' | null;
  executorUserId?: string | null;
  executedAt?: string | null;
  failureStage?: 'generation' | 'execution' | 'verification' | 'rollback' | null;
  executable: boolean;
  status: TopQueryGovernancePlanStatus;
  riskLevel?: 'low' | 'medium' | 'high' | string;
  products?: Array<Record<string, unknown>>;
  productNames?: string[];
  statusLabel?: TopQueryGovernancePlanStatusLabel;
  reason?: string | null;
  decision?: 'adjust' | 'no_change' | 'manual_review' | string | null;
  actionIds?: number[];
  causeGroupId?: string;
  cancelable?: boolean;
  metrics?: {
    fromRate?: number | string | null;
    toRate?: number | string | null;
    [key: string]: unknown;
  };
  causeGroups?: TopQueryGovernanceCauseGroup[];
  cause_groups?: TopQueryGovernanceCauseGroup[];
  errorProductIds?: string[];
  error_product_ids?: string[];
}

export interface TopQueryGovernanceActionStep {
  step?: number;
  index?: number;
  actionType?: string;
  action_type?: string;
  actionId?: number | null;
  action_id?: number | null;
  title?: string;
  description?: string;
  params?: Record<string, unknown>;
  dependsOn?: number[];
  depends_on?: number[];
}

export interface TopQueryGovernanceEvidenceItem {
  label: string;
  value: unknown;
  tone?: 'default' | 'warning' | 'danger' | string;
}

export type TopQueryGovernanceEvidence = TopQueryGovernanceEvidenceItem | string | number | boolean | Record<string, unknown>;

export interface TopQueryGovernanceCauseGroup {
  validationErrors?: string[];
  groupId?: string;
  group_id?: string;
  title?: string | null;
  rootCause?: 'individual' | 'kg_issue' | 'uncertain' | string;
  root_cause?: 'individual' | 'kg_issue' | 'uncertain' | string;
  conclusion?: string | null;
  needKgAdjustment?: boolean;
  need_kg_adjustment?: boolean;
  products?: Array<Record<string, unknown>>;
  productIds?: string[];
  product_ids?: string[];
  affectedProducts?: Array<Record<string, unknown>>;
  affected_products?: Array<Record<string, unknown>>;
  actions?: TopQueryGovernanceActionStep[];
  steps?: TopQueryGovernanceActionStep[];
  evidence?: TopQueryGovernanceEvidence[] | Record<string, unknown>;
  analysisEvidence?: TopQueryGovernanceEvidence[] | Record<string, unknown>;
  analysis_evidence?: TopQueryGovernanceEvidence[] | Record<string, unknown>;
  reasons?: string[] | string;
  confidence?: number | string | null;
  actionId?: number | null;
  action_id?: number | null;
  actionIds?: number[];
  action_ids?: number[];
  causeGroupId?: string;
  executable?: boolean;
  status?: TopQueryGovernancePlanStatus;
  statusLabel?: TopQueryGovernancePlanStatusLabel;
  status_label?: TopQueryGovernancePlanStatusLabel;
}

export interface TopQueryTrendPoint {
  date: string;
  uv?: number;
  pv: number;
  clickUv?: number;
  addToCartUv?: number;
  purchaseUv?: number;
  ctr: number;
  atc?: number;
  cvr: number;
}

export interface TopQueryDetailData {
  task: TopQueryTask;
  actions: TopQueryAction[];
  s0?: {
    resultCount: number;
    products: Record<string, unknown>[];
  } | null;
  plannerPlan?: TopQueryPlannerPlan | null;
  governancePlans?: TopQueryGovernancePlan[];
  stepStatuses?: TopQueryStepStatus[];
  nodes?: TopQueryExecutionNode[];
  productDisplayLimit?: number;
  inventoryBrokenRateThreshold?: number;
  inventory?: TopQueryInventoryRecord[];
  inventoryExecution?: {
    available: boolean;
    source?: string;
    status?: 'success' | 'failed' | 'inconclusive' | string;
    errorCode?: string | null;
    errorMessage?: string | null;
  } | null;
  inventoryCheckIntervalMinutes?: number | null;
  recoveredTodayCount?: number | null;
  inventoryScans?: InventoryScanRecord[];
  historyDays?: number;
  trend?: TopQueryTrendPoint[];
  visibleCheckKeys?: string[];
  events?: TopQueryEvent[];
  liveRecall?: {
    resultCount: number;
    products: Record<string, unknown>[];
  } | null;
  recall?: {
    currentResultCount: number | null;
    previousResultCount: number | null;
    comparisonStatus: 'normal' | 'drop' | 'not_comparable' | string;
    conclusion: string;
    kgStrategy?: {
      items: string[];
      tags: Array<{ name: string; status: 'exists' | 'pending_create' | string; resultCount: number | null }>;
    } | null;
    recallRate?: {
      initialSearchRecallRate?: number | null;
      postSearchRecallRate?: number | null;
      llmRecallRate?: number | null;
      isFullRecall?: boolean;
      unrecalledProductIds?: string[];
      unrecalledProducts?: Array<{
        product_id: string;
        product_name?: string;
        image_url?: string;
        applied_whitelist_tags?: string[];
        applied_query_whitelist?: boolean;
      }>;
      operatorAlert?: string | null;
      taggingActions?: Array<{
        action_type?: string;
        tag?: string;
        product_ids?: string[];
        execution_mode?: string;
        status?: string;
        plan_id?: number;
        error_message?: string | null;
      }>;
      pendingActionCount?: number | null;
    } | null;
    recallRateStatus?: string | null;
  } | null;
  relevance?: TopQueryRelevanceSummary | null;
  reorder?: {
    currentCtr?: number | null;
    recentCtr?: number | null;
    ctrChange?: number | null;
    ctrAlertThreshold?: number | null;
    currentCvr?: number | null;
    recentCvr?: number | null;
    cvrChange?: number | null;
    cvrAlertThreshold?: number | null;
    recallCount?: number | null;
    lowRecallThreshold?: number | null;
    productsLimit?: number | null;
    conclusion?: string | null;
    requiresReorder?: boolean;
    strategyStatus?: 'success' | 'failed' | 'pending' | 'not_run' | 'not_applicable' | null;
    executionStatus?: 'success' | 'failed' | 'pending' | 'not_run' | 'not_applicable' | null;
  } | null;
}

export interface InventoryScanRecord {
  checkedAt: string;
  brokenProductCount: number;
  normalProductCount: number;
  recoveredProductCount: number;
}

export interface TopQueryInventoryRecord {
  id: number;
  productId: string;
  productName?: string | null;
  query: string;
  lastBrokenRate?: number | null;
  status: string;
  imageUrl?: string | null;
  checkedAt?: string | null;
  recoveredAt?: string | null;
  brokenRateBefore?: number | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface TopQueryRelevanceCandidate {
  productId?: string | null;
  product_id?: string | null;
  reason?: string | null;
  detectionTypes?: string[];
  detection_types?: string[];
}

export interface TopQueryRelevanceSummary {
  query: string;
  productCount: number;
  requestedProductIds: string[];
  reusedProductIds: string[];
  detectedProductIds: string[];
  relevantProductIds: string[];
  blacklistCandidates: TopQueryRelevanceCandidate[];
  warnings: Array<Record<string, unknown>>;
  detectedProductCount: number;
  remediation?: TopQueryRelevanceRemediationSummary | null;
  governance?: TopQueryRelevanceRemediationSummary | null;
  [key: string]: unknown;
}

export interface TopQueryRelevanceRemediationSummary {
  blacklistStatus?: string | null;
  rootCauseGroupCount?: number | null;
  kgStrategyStatus?: string | null;
  retagStatus?: string | null;
  verifiedAt?: string | null;
  incorrectProductsDisappeared?: boolean | string | null;
  correctProductsAffected?: boolean | string | null;
  beforeRecallCount?: number | null;
  afterRecallCount?: number | null;
  finalConclusion?: string | null;
  [key: string]: unknown;
}

export interface TopQueryProductDetailData {
  product: OpsTaskProductApiDetail;
  detailMeta?: {
    detailSource: 'live' | 'live_with_snapshot' | 'after_search_snapshot' | 'before_search_snapshot' | 'snapshot' | string;
    capturedAt?: string | null;
    isSnapshot: boolean;
    taskId?: number | null;
  };
}

export interface TopQueryModelMetrics {
  modelName: string;
  modelRole: 'primary' | 'fallback' | string;
  fallbackUsed: boolean;
  tokenUsage: number;
  durationSeconds: number;
  errorCode?: string | null;
}

export interface TopQueryEvent {
  id: number;
  agentName: string;
  displayName?: string | null;
  stage: string;
  eventType: string;
  summary: string;
  payload?: unknown;
  duration?: number | null;
  modelMetrics?: TopQueryModelMetrics | null;
  createdAt?: string | null;
}

export interface TopQueryEventsData {
  items: TopQueryEvent[];
}

export interface OpsTaskProductApiDetail {
  brand?: string;
  brandDisplay?: string | null;
  skuId?: string;
  spuId?: string;
  spu?: string;
  productId?: string;
  productName?: string;
  mainImage?: string;
  imageUrls?: string[] | null;
  imageUrl?: string;
  description?: string;
  gender?: string;
  category?: string;
  multiCategory?: string | null;
  color?: string;
  size?: string;
  material?: string;
  price?: number;
  discountPrice?: number;
  stock?: number;
  inventory?: number;
  sales?: number;
  salesVolume?: number;
  status?: number;
  isNew?: number | boolean;
  isDiscount?: number | boolean | null;
  isClearance?: number | boolean | null;
  releaseDate?: string;
  shelveStartTime?: string | null;
  shelveEndTime?: string | null;
  listTime?: string | null;
  createTime?: string | null;
  updateTime?: string | null;
  modifyTime?: string | null;
  freshnessDate?: string | null;
  mainCategory?: string;
  categories?: string[];
  tags?: string[];
  tagName?: string | null;
  whiteList?: string | null;
  whiteListAi?: string | null;
  blackList?: string | null;
  blackListAi?: string | null;
  processFlag?: string | null;
  attr1?: string | null;
  attr2?: string | null;
  attr3?: string | null;
  attr4?: string | null;
  attr5?: string | null;
  attr6?: string | null;
  attr7?: string | null;
  attr8?: string | null;
  attr9?: string | null;
  attr10?: string | null;
  dimensions?: Record<string, string | null> | null;
  score?: number;
  ext?: {
    bucket?: string;
    stock?: number;
    scoreDetail?: string;
    isTop?: boolean;
    topIndex?: number | null;
  };
}

export interface AssistantContextSummary {
  taskCount: number;
  pendingCount: number;
  actionCount: number;
  monitoringCount: number;
}

export interface AssistantMessageAttachment {
  id: string;
  type: 'image' | 'file';
  name: string;
  size: number;
  mimeType?: string;
  url?: string;
}

export interface AssistantMessageVersion {
  id: string;
  title: string;
  content: string;
  createdAt: string;
  toolCalls?: AssistantToolCall[];
  toolResults?: AssistantToolResult[];
}

export type AssistantExecutionState =
  | 'idle'
  | 'thinking'
  | 'tool_running'
  | 'waiting'
  | 'done'
  | 'failed'
  | 'cancelled';

export interface AssistantContextMessage {
  id: string;
  role: 'assistant' | 'operator';
  title: string;
  content: string;
  createdAt: string;
  attachments?: AssistantMessageAttachment[];
  toolCalls?: AssistantToolCall[];
  toolResults?: AssistantToolResult[];
  versions?: AssistantMessageVersion[];
  activeVersionIndex?: number;
  executionState?: AssistantExecutionState;
}

export interface AssistantConversationItem {
  id: string;
  title: string;
  preview: string;
  updatedAt: string;
  status?: 'active' | 'empty';
  source?: 'current_context' | 'suggested_prompt';
}

export interface AssistantCapability {
  key: string;
  title: string;
  description: string;
  status: 'ready' | 'planned';
}

export interface AssistantAvailableBinding {
  agentBindingId: number;
  brand?: string | null;
  displayName: string;
  modelDisplayName: string;
  modelName?: string;
  availableModels?: string[];
  supportsModelSwitch?: boolean;
  bindingType: 'agent' | 'team' | string;
}

export interface AssistantToolDefinition {
  key: string;
  title: string;
  mode: 'read' | 'confirm' | string;
}

export interface AssistantContextData {
  summary: AssistantContextSummary;
  suggestedPrompts: string[];
  messages: AssistantContextMessage[];
  capabilities: AssistantCapability[];
  runtime?: AssistantRuntimeInfo;
  availableBindings?: AssistantAvailableBinding[];
  availableBrands?: string[];
  availableTools?: AssistantToolDefinition[];
}

export interface AssistantWorkspaceEntry {
  name: string;
  path: string;
  kind: 'file' | 'directory' | string;
  size?: number | null;
  modifiedAt?: number | null;
}

export interface AssistantWorkspaceData {
  path: string;
  relativePath: string;
  entries: AssistantWorkspaceEntry[];
}

export interface AssistantWorkspaceFilePreview {
  name: string;
  path: string;
  size: number;
  truncated: boolean;
  isBinary: boolean;
  content: string;
}

export interface AssistantPageContext {
  pageKey?: string;
  brand?: string;
  selectedQuery?: string;
  selectedTaskId?: number;
  selectedDate?: string;
  displayStatus?: string;
  resultCount?: number | null;
  ctr?: number | null;
  cvr?: number | null;
  searchPv?: number | null;
  querySource?: string;
  stepStatuses?: Array<{
    checkKey: string;
    displayStatus: string;
    summary?: string | null;
  }>;
}

export interface AssistantRuntimeInfo {
  provider: string;
  modelLabel: string;
  modelName?: string;
  selectedModelName?: string;
  availableModels?: string[];
  supportsModelSwitch?: boolean;
  agentId?: string;
  teamId?: string | null;
  channel?: string;
}

export interface AssistantRuntimeInspector {
  runtime: AssistantRuntimeInfo;
  permissions: { accountId: number; brands: string[]; isAdmin: boolean; workspaceScoped: boolean };
  workspace: { roots: string[] };
  tools: Array<{ name: string; description?: string }>;
  events: Array<{ type: string; status?: string; message?: string; createdAt?: string }>;
  context: { sessionId?: string | null; summary?: Record<string, unknown> | null };
}

export interface AssistantToolCall {
  id?: string;
  name: string;
  arguments?: string;
  status?: AssistantExecutionState;
  output?: string;
  error?: string;
  exitCode?: number | null;
  startedAt?: string;
  completedAt?: string;
}

export interface AssistantToolResult {
  toolCallId?: string;
  toolName: string;
  summary: string;
  status?: 'completed' | 'failed' | 'cancelled';
  output?: string;
  error?: string;
  exitCode?: number | null;
  durationMs?: number | null;
}

export interface AssistantProposedAction {
  id?: number;
  actionType: string;
  brand?: string;
  query?: string;
  target?: string;
  reason?: string;
  riskLevel?: string;
  status?: string;
  payload?: Record<string, unknown>;
  resultSummary?: string;
}

export interface AssistantChatResponse {
  sessionId: string;
  message: string;
  toolCalls?: AssistantToolCall[];
  toolResults?: AssistantToolResult[];
  proposedActions: AssistantProposedAction[];
  contextSummary: Partial<AssistantContextSummary>;
  status: 'ok' | 'unconfigured' | 'failed' | string;
  runtime?: AssistantRuntimeInfo;
}

export interface AssistantSessionRecord {
  id: number;
  sessionId: string;
  brand?: string | null;
  agentBindingId?: number | null;
  title: string;
  manualTitle?: string | null;
  generatedTitle?: string | null;
  preview: string;
  pinned: boolean;
  updatedAt?: string | null;
  lastOpenedAt?: string | null;
  messages: AssistantContextMessage[];
  proposedActions: AssistantProposedAction[];
  runtime?: AssistantRuntimeInfo;
  parentSessionId?: string | null;
}

export interface AssistantTerminalSession {
  terminalSessionId: string;
  cwd: string;
  status: 'idle' | 'running' | 'completed' | 'failed' | 'cancelled' | string;
  output: string;
  exitCode?: number | null;
}

export interface AssistantSessionListResponse {
  items: AssistantSessionRecord[];
}

export interface NoResultGovernanceAction {
  id: number;
  actionType: string;
  targetInterface: string;
  requestPayload: Record<string, unknown>;
  responsePayload: Record<string, unknown>;
  beforeSnapshot: Record<string, unknown>;
  rollbackPayload: Record<string, unknown>;
  status: string;
  rollbackStatus: string;
  riskLevel: 'low' | 'medium' | 'high' | string;
  agentReason: string;
  errorMessage?: string | null;
  createdAt: string;
}

export interface NoResultAgentTimelineEvent {
  id: number;
  stage: string;
  eventType: string;
  toolName?: string | null;
  summary: string;
  payload: Record<string, unknown>;
  createdAt: string;
}

export interface NoResultGovernanceItem {
  id: number;
  batchId: number;
  dataDate: string;
  brand: string;
  query: string;
  status: string;
  aiDecision: string;
  aiReason: string;
  finalStrategy: string;
  beforeResultCount: number;
  afterResultCount: number;
  searchPv: number;
  searchUv?: number | null;
  executionStatus?: 'idle' | 'running' | 'paused' | 'completed' | 'blocked' | string;
  actions: NoResultGovernanceAction[];
  agentTimeline?: NoResultAgentTimelineEvent[];
  createdAt: string;
  processedAt: string;
}
