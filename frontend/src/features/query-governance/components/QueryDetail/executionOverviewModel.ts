import type { InventoryScanRecord, TopQueryAction, TopQueryDetailData, TopQueryEvent, TopQueryExecutionNode, TopQueryInventoryRecord, TopQueryRelevanceSummary, TopQueryStepStatus } from '@/shared/types';
import { formatBackendDateTime } from '@/shared/lib/backendTime';
import {
  hasStepIssueEvidence,
  resolveCheckExecutionState,
  resolveCheckState,
  type CheckState,
} from '../../lib/queryGovernanceStatus';

export type EventResultEntry = { label: string; value: string; rawValue?: unknown };

export interface ExecutionOverviewNode {
  key: 'recall' | 'open_factor' | 'relevance' | 'inventory';
  label: string;
  state: CheckState;
  summary: string;
  details: EventResultEntry[];
  productsLimit?: number | null;
  kgStrategy?: NonNullable<NonNullable<TopQueryDetailData['recall']>['kgStrategy']>;
  governanceStatus?: TopQueryStepStatus['governanceStatus'];
  inventoryScans?: InventoryScanRecord[];
}

export interface ReorderEvidence {
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
  executionStatus?: 'success' | 'failed' | 'pending' | 'inconclusive' | 'not_run' | 'not_applicable' | null;
}

type RecordValue = Record<string, unknown>;
type Input = {
  visibleCheckKeys?: string[];
  steps?: Array<Partial<TopQueryStepStatus>>;
  nodes?: TopQueryExecutionNode[];
  s0?: { resultCount?: number | null; products?: RecordValue[] } | null;
  recall?: TopQueryDetailData['recall'];
  events?: TopQueryEvent[];
  actions?: TopQueryAction[];
  inventory?: TopQueryInventoryRecord[];
  inventoryExecution?: TopQueryDetailData['inventoryExecution'];
  inventoryCheckIntervalMinutes?: number | null;
  recoveredTodayCount?: number | null;
  inventoryScans?: InventoryScanRecord[];
  relevance?: TopQueryRelevanceSummary | null;
  reorder?: ReorderEvidence;
};

const order = ['recall', 'open_factor', 'relevance', 'inventory'] as const;
const labels: Record<(typeof order)[number], string> = {
  recall: '召回数量',
  open_factor: '重排序',
  relevance: '相关性',
  inventory: '断码',
};

const asRecord = (value: unknown): RecordValue => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
);

const first = (...values: unknown[]) => values.find((value) => value != null && String(value).trim() !== '');
const text = (value: unknown) => value == null ? '' : String(value).trim();
const number = (...values: unknown[]) => {
  const value = first(...values);
  if (value == null || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

/** 合成节点状态：断码动作失败 / 缺执行事实 / 检查与治理状态。 */
const stateOfNode = (
  key: (typeof order)[number],
  step: Partial<TopQueryStepStatus> | undefined,
  hasInventoryExecutionFact: boolean,
  inventoryExecutionFailed: boolean,
  hasDetectedIssue: boolean,
): CheckState => {
  if (inventoryExecutionFailed) return 'failed';
  if (key === 'inventory' && !hasInventoryExecutionFact) return 'not_run';
  return resolveCheckState(step, hasDetectedIssue);
};

const payloadOf = (value: unknown) => asRecord(value);
const withRawValue = (entry: EventResultEntry, rawValue: unknown): EventResultEntry => {
  Object.defineProperty(entry, 'rawValue', { value: rawValue, enumerable: false, configurable: true });
  return entry;
};
const eventFor = (key: string, events: TopQueryEvent[]) => events
  .filter((event) => {
    const payload = payloadOf(event.payload);
    const eventKey = text(payload.checkKey || payload.check_key || payload.nodeKey || payload.node_key);
    const stage = text(event.stage).toLowerCase();
    return eventKey === key || (key === 'open_factor' && (stage === 'reorder' || stage === 'open_factor')) || stage === key;
  })
  .sort((left, right) => String(right.createdAt || '').localeCompare(String(left.createdAt || '')))[0];

const findDeep = (root: unknown, keys: string[]): unknown => {
  const record = asRecord(root);
  for (const key of keys) {
    if (record[key] != null && text(record[key])) return record[key];
  }
  for (const value of Object.values(record)) {
    if (value && typeof value === 'object') {
      const found = findDeep(value, keys);
      if (found != null && text(found)) return found;
    }
  }
  return null;
};

const productsFrom = (value: unknown): RecordValue[] => {
  const record = asRecord(value);
  const list = [record.products, record.affectedProducts, record.relatedProducts, record.items, record.results]
    .find((item) => Array.isArray(item));
  return Array.isArray(list) ? list.filter((item) => item && typeof item === 'object').map(asRecord) : [];
};

const productIdOf = (product: RecordValue) => text(first(
  product.productId,
  product.product_id,
  product.spuId,
  product.spu,
  product.skuId,
  product.sku_id,
));

const productForId = (id: unknown, productsById: Map<string, RecordValue>) => {
  const normalizedId = text(id);
  return productsById.get(normalizedId) ?? { productId: normalizedId, productName: normalizedId || '未知商品' };
};

const productText = (product: RecordValue) => {
  const name = text(first(product.productName, product.product_name, product.name, product.title, product.productId, product.product_id));
  const oldRank = number(product.oldRank, product.old_rank, product.beforeRank, product.before_rank);
  const newRank = number(product.newRank, product.new_rank, product.afterRank, product.after_rank, product.rank);
  if (oldRank != null && newRank != null) return `${name}（${oldRank}→${newRank}）`;
  return name;
};

const formatTime = (value: unknown) => {
  const raw = text(value);
  if (!raw) return '';
  return formatBackendDateTime(raw, 'YYYY-MM-DD HH:mm');
};

// Inventory records persist local (Beijing) timestamps; normalize their
// shape only, without any timezone shift.
const formatLocalTime = (value: unknown) => {
  const raw = text(value);
  if (!raw) return '';
  const normalized = raw.replace('T', ' ').replace(/\.\d+(?=Z|$)/, '').replace(/Z$/, '');
  return normalized.length >= 16 ? normalized.slice(0, 16) : normalized;
};

const yesNoText = (value: unknown) => {
  if (value === true) return '是';
  if (value === false) return '否';
  const normalized = text(value).toLowerCase();
  if (['true', 'yes', 'passed', 'disappeared', 'affected'].includes(normalized)) return '是';
  if (['false', 'no', 'not_affected', 'none'].includes(normalized)) return '否';
  return text(value);
};

const relevanceRemediationDetails = (relevance: TopQueryRelevanceSummary): EventResultEntry[] => {
  const root = asRecord(relevance);
  const nested = asRecord(first(root.remediation, root.governance, root.remediationSummary, root.remediation_summary));
  const source = { ...root, ...nested };
  const blacklistStatus = first(source.blacklistStatus, source.blacklist_status);
  const rootCauseGroupCount = number(source.rootCauseGroupCount, source.root_cause_group_count, source.causeGroupCount, source.cause_group_count);
  const kgStrategyStatus = first(source.kgStrategyStatus, source.kg_strategy_status);
  const retagStatus = first(source.retagStatus, source.retag_status);
  const verifiedAt = first(source.verifiedAt, source.verified_at, source.verificationTime, source.verification_time);
  const incorrectProductsDisappeared = first(source.incorrectProductsDisappeared, source.incorrect_products_disappeared);
  const correctProductsAffected = first(source.correctProductsAffected, source.correct_products_affected);
  const beforeRecallCount = number(source.beforeRecallCount, source.before_recall_count);
  const afterRecallCount = number(source.afterRecallCount, source.after_recall_count);
  const finalConclusion = first(source.finalConclusion, source.final_conclusion, source.verificationConclusion, source.verification_conclusion);
  const entries: EventResultEntry[] = [];
  if (blacklistStatus != null) entries.push({ label: '拉黑状态', value: text(blacklistStatus) });
  if (rootCauseGroupCount != null) entries.push({ label: '根因分组', value: `${rootCauseGroupCount} 组` });
  if (kgStrategyStatus != null) entries.push({ label: 'KG 策略', value: text(kgStrategyStatus) });
  if (retagStatus != null) entries.push({ label: '重新打标签', value: text(retagStatus) });
  if (verifiedAt != null) entries.push({ label: '验证时间', value: formatTime(verifiedAt) });
  if (incorrectProductsDisappeared != null) entries.push({ label: '错误商品是否消失', value: yesNoText(incorrectProductsDisappeared) });
  if (correctProductsAffected != null) entries.push({ label: '正确商品是否误伤', value: yesNoText(correctProductsAffected) });
  if (beforeRecallCount != null || afterRecallCount != null) entries.push({
    label: '验证前后召回数量',
    value: `${beforeRecallCount == null ? '未记录' : `${beforeRecallCount} 件`} → ${afterRecallCount == null ? '未记录' : `${afterRecallCount} 件`}`,
  });
  if (finalConclusion != null) entries.push({ label: '最终结论', value: text(finalConclusion) });
  return entries;
};

const formatRate = (value: unknown) => {
  const parsed = number(value);
  return parsed == null ? null : `${(parsed <= 1 ? parsed * 100 : parsed).toFixed(2)}%`;
};

const inventoryStatusLabels: Record<string, string> = {
  monitoring: '监测中',
  recovered: '已恢复',
  auto_stopped: '已停止',
  manually_stopped: '手动停止',
};

const inventoryStatusLabel = (value: unknown) => {
  const status = text(value).toLowerCase();
  return inventoryStatusLabels[status] || text(value) || '未知';
};

const inventoryRateText = (value: unknown) => {
  const parsed = number(value);
  if (parsed == null) return '';
  const percent = parsed <= 1 ? parsed * 100 : parsed;
  return `${Number.isInteger(percent) ? percent.toFixed(0) : percent.toFixed(2)}%`;
};

const formatChange = (value: unknown, currentValue: unknown, recentValue: unknown) => {
  const explicit = number(value);
  const current = number(currentValue);
  const recent = number(recentValue);
  const delta = explicit ?? (current == null || recent == null ? null : (current - recent) * 100);
  if (delta == null) return '暂无数据';
  if (delta === 0) return '无变化';
  return `${delta < 0 ? '下降 ' : '上升 '}${Math.abs(delta).toFixed(2)}%`;
};

const formatAlertThreshold = (value: unknown) => {
  const parsed = number(value);
  return parsed == null ? '暂无数据' : `下降 ≥ ${parsed} %`;
};

const actionFailureText = (action: TopQueryAction | undefined) => {
  if (!action) return '';
  const payload = payloadOf(action.responsePayload ?? action.response);
  return text(first(
    findDeep(payload, ['errorMessage', 'error_message', 'error', 'reason', 'message']),
    action.reason,
  ));
};

const reorderFailureReasonMap: Record<string, string> = {
  action_execution_failed: '排序接口执行失败，排序未完成。',
  action_gateway_failed: '排序服务调用异常，排序未完成。',
  action_readback_failed: '排序已提交，但系统无法从搜索结果确认是否生效。',
  action_readback_inconclusive: '排序结果暂时无法确认，请稍后检查搜索结果。',
  action_verification_failed: '排序结果与预期不一致，系统已撤销本次排序。',
  action_result_unknown: '排序结果未知，需要人工检查搜索结果。',
  action_recovery_readback_failed: '中断后的排序结果无法确认，请人工检查搜索结果。',
  action_idempotency_conflict: '检测到重复的排序请求，本次未重复执行。',
  action_in_progress: '已有相同排序任务正在执行，请稍后查看结果。',
  action_authorization_failed: '当前账号无权执行排序。',
  action_result_invalid: '排序服务返回了无法识别的结果。',
  action_rollback_failed: '排序未确认生效，且自动撤销失败，需要人工处理。',
  verification_failed: '排序结果与预期不一致，系统已撤销本次排序。',
  verification_inconclusive: '排序结果暂时无法确认，请稍后检查搜索结果。',
  readback_failed: '排序已提交，但系统无法从搜索结果确认是否生效。',
  execution_unknown: '排序结果暂时无法确认，请稍后检查搜索结果。',
};

const reorderFailureReason = (
  action: TopQueryAction | undefined,
  step: Partial<TopQueryStepStatus>,
  persistedNode: TopQueryExecutionNode | undefined,
  evidence: RecordValue,
  rawSummary: string,
) => {
  const actionPayload = payloadOf(action?.responsePayload ?? action?.response);
  const gateway = payloadOf(actionPayload.gateway);
  const code = text(first(
    persistedNode?.errorCode,
    evidence.errorCode,
    evidence.error_code,
    gateway.errorCode,
    gateway.error_code,
    action?.status,
    gateway.reason,
    actionPayload.reason,
  )).toLowerCase();
  const mapped = reorderFailureReasonMap[code];
  if (mapped) return mapped;

  const message = text(first(
    persistedNode?.errorMessage,
    actionFailureText(action),
    step.summary,
    rawSummary,
  ));
  if (message && !/[a-z][a-z0-9_:-]{2,}/i.test(message)) return message;
  return '排序执行异常，请检查排序接口返回或稍后重试。';
};

const reorderConclusionWithStatuses = (
  conclusion: string,
  reorder: ReorderEvidence,
  strategyAvailable: boolean,
  actionStatus: string,
) => {
  if (reorder.requiresReorder !== true) return conclusion;
  if (conclusion.includes('AI 重排序策略')) return conclusion;
  const strategyStatus = reorder.strategyStatus ?? (strategyAvailable ? 'success' : 'failed');
  const executionStatus = reorder.executionStatus ?? (
    ['executed', 'verified', 'completed', 'success'].includes(actionStatus)
      ? 'success'
      : ['execution_unknown', 'action_readback_inconclusive', 'verification_inconclusive'].includes(actionStatus)
        ? 'inconclusive'
      : actionStatus
        ? 'failed'
        : 'not_run'
  );
  const strategyText = strategyStatus === 'success'
    ? 'AI 重排序策略已生成'
    : strategyStatus === 'pending'
      ? 'AI 重排序策略生成中'
      : 'AI 重排序策略生成失败';
  const executionText = executionStatus === 'success'
    ? '重排序执行成功'
    : executionStatus === 'pending'
      ? '重排序执行中'
      : executionStatus === 'not_run'
        ? '重排序未执行'
        : executionStatus === 'inconclusive'
          ? '重排序结果待确认'
        : '重排序执行失败';
  return `${conclusion}${strategyStatus === 'success' && executionStatus === 'failed' ? `${strategyText}，但${executionText}。` : `${strategyText}，${executionText}。`}`;
};

const errorDetails = (step: Partial<TopQueryStepStatus>, summary: string, errorCode?: string | null): EventResultEntry[] => {
  const evidence = payloadOf(step.evidence);
  const code = text(first(errorCode, evidence.errorCode, evidence.error_code, evidence.code));
  if (resolveCheckExecutionState(step) !== 'failed') return [];
  return [{ label: '错误原因', value: [code, summary].filter(Boolean).join('：') || '节点执行失败' }];
};

const stageNode = (nodes: TopQueryExecutionNode[], key: string) => nodes.find((node) => (
  node.nodeKey === key || (key === 'open_factor' && node.nodeKey === 'reorder')
));

const stageMetricEntries = (nodes: TopQueryExecutionNode[], events: TopQueryEvent[], key: string): EventResultEntry[] => {
  const node = stageNode(nodes, key);
  const event = eventFor(key, events);
  const eventMetrics = event?.modelMetrics;
  const duration = node?.durationSeconds ?? event?.duration ?? eventMetrics?.durationSeconds;
  const token = node?.tokenUsage ?? eventMetrics?.tokenUsage;
  const tokenMissing = node?.tokenUsageMissing ?? false;
  return [
    { label: '耗时', value: duration == null ? '未记录' : `${duration.toFixed(2)}s` },
    { label: 'Token', value: tokenMissing || token == null ? '未记录' : String(token) },
  ];
};

const nodeResult = (node?: TopQueryExecutionNode) => {
  let value: unknown = node?.result;
  for (let depth = 0; depth < 2; depth += 1) {
    const record = asRecord(value);
    if (!record.result || typeof record.result !== 'object') return record;
    value = record.result;
  }
  return asRecord(value);
};

const buildNode = (
  key: (typeof order)[number],
  step: Partial<TopQueryStepStatus> | undefined,
  nodes: TopQueryExecutionNode[],
  s0: Input['s0'],
  recall: Input['recall'],
  events: TopQueryEvent[],
  actions: TopQueryAction[],
  inventory: TopQueryInventoryRecord[],
  relevance: Input['relevance'],
  reorder: Input['reorder'],
  inventoryCheckIntervalMinutes?: number | null,
  recoveredTodayCount?: number | null,
  inventoryExecution?: TopQueryDetailData['inventoryExecution'],
  inventoryScans?: InventoryScanRecord[],
): ExecutionOverviewNode => {
  const event = eventFor(key, events);
  const eventPayload = payloadOf(event?.payload);
  const persistedNode = nodes.find((node) => node.nodeKey === key || (key === 'open_factor' && node.nodeKey === 'reorder'));
  const persistedResult = nodeResult(persistedNode);
  const persistedEvidence = payloadOf(persistedResult.evidence);
  const evidence = { ...persistedEvidence, ...payloadOf(step?.evidence) };
  const hasInventoryExecutionFact = Boolean(
    step
    || persistedNode
    || event
    || actions.some((action) => action.checkKey === key)
    || inventory.length,
  );
  const inventoryFailed = key === 'inventory' && inventoryExecution?.status === 'failed';
  const governanceFailed = ['failed', 'rollback_failed'].includes(String(step?.governanceStatus?.status || '').toLowerCase());
  const hasDetectedIssue = hasStepIssueEvidence(evidence);
  const state = stateOfNode(key, step, hasInventoryExecutionFact, inventoryFailed, hasDetectedIssue);
  const rawSummary = text(first(
    step?.summary,
    event?.summary,
    persistedResult.summary,
    persistedResult.reason,
    persistedNode?.errorMessage,
  ));
  const details: EventResultEntry[] = [];
  let summary = rawSummary;
  let productsDisplayLimit: number | null = null;

  if (key === 'recall') {
    const current = number(
      recall?.currentResultCount,
      evidence.result_count,
      evidence.resultCount,
      persistedResult.result_count,
      persistedResult.resultCount,
      s0?.resultCount,
    );
    const previous = number(recall?.previousResultCount, evidence.previous_result_count, evidence.previousResultCount, persistedResult.previous_result_count, persistedResult.previousResultCount);
    const conclusion = text(first(recall?.conclusion, evidence.conclusion, persistedResult.conclusion)) || '正常';
    if (current != null || previous != null) {
      summary = `查询到 ${current != null ? `${current} 件商品` : '未知'}${previous != null ? `，较上一次 ${previous} 件` : ''}；结论：${conclusion}`;
      details.push({ label: '当前召回数量', value: current != null ? `${current} 件` : '未获取' });
      details.push({ label: '上一次召回数量', value: previous != null ? `${previous} 件` : '暂无' });
      details.push(...stageMetricEntries(nodes, events, key));
      details.push({ label: '结论', value: conclusion });
    } else {
      summary = rawSummary || '暂无召回数量摘要';
      details.push(...stageMetricEntries(nodes, events, key));
    }
    const recallRate = recall?.recallRate || payloadOf(evidence.recall_rate ?? persistedResult.recall_rate);
    const postRecallRate = number(recallRate?.postSearchRecallRate, (recallRate as Record<string, unknown>)?.post_search_recall_rate);
    const initialRecallRate = number(recallRate?.initialSearchRecallRate, (recallRate as Record<string, unknown>)?.initial_search_recall_rate);
    const llmRecallRate = number(recallRate?.llmRecallRate, (recallRate as Record<string, unknown>)?.llm_recall_rate);
    const operatorAlert = text(first(recallRate?.operatorAlert, (recallRate as Record<string, unknown>)?.operator_alert));
    const pendingActionCount = number(recallRate?.pendingActionCount, (recallRate as Record<string, unknown>)?.pending_action_count);
    const unrecalledIds = Array.isArray(recallRate?.unrecalledProductIds)
      ? recallRate.unrecalledProductIds
      : Array.isArray((recallRate as Record<string, unknown>)?.unrecalled_product_ids)
        ? ((recallRate as Record<string, unknown>).unrecalled_product_ids as string[])
        : [];
    if (initialRecallRate != null) {
      details.push({ label: '治理前搜索召回率', value: formatRate(initialRecallRate) || `${(initialRecallRate * 100).toFixed(1)}%` });
    }
    if (postRecallRate != null) {
      details.push({ label: '当前搜索引擎召回率', value: formatRate(postRecallRate) || `${(postRecallRate * 100).toFixed(1)}%` });
    }
    if (llmRecallRate != null) {
      details.push({ label: '大模型召回率', value: formatRate(llmRecallRate) || `${(llmRecallRate * 100).toFixed(1)}%` });
    }
    if (pendingActionCount != null && pendingActionCount > 0) {
      details.push({ label: '待审批补召回动作', value: `${pendingActionCount} 项（见治理方案区）` });
    }
    if (unrecalledIds.length > 0) {
      details.push({ label: '仍未召回商品', value: unrecalledIds.join('、') });
    }
    if (operatorAlert) {
      details.push({ label: '召回率复检提示', value: operatorAlert });
      if (!summary.includes(operatorAlert)) {
        summary = `${summary} ${operatorAlert}`.trim();
      }
    }
    if (governanceFailed) {
      summary = `${summary} 治理方案${step?.governanceStatus?.label || '执行失败'}${step?.governanceStatus?.errorMessage ? `：${step.governanceStatus.errorMessage}` : ''}。`;
    }
  } else if (key === 'open_factor') {
    const reorderActions = actions.filter((action) => action.checkKey === key || action.actionType === 'query_reorder');
    const strategy = text(first(
      findDeep(eventPayload, ['preference_description', 'preferenceDescription', 'strategy', 'preference']),
      findDeep(persistedResult, ['preference_description', 'preferenceDescription', 'strategy', 'preference']),
      ...reorderActions.map((action) => findDeep(action.requestPayload ?? action.request, ['preference_description', 'preferenceDescription', 'strategy', 'preference'])),
    ));
    const products = [
      ...productsFrom(eventPayload),
      ...productsFrom(persistedResult),
      ...reorderActions.flatMap((action) => [
      ...productsFrom(action.requestPayload ?? action.request),
      ...productsFrom(action.responsePayload ?? action.response),
      ]),
    ]
      .filter((product, index, list) => productText(product) && list.findIndex((item) => productText(item) === productText(product)) === index);
    const productsLimit = number(reorder?.productsLimit, persistedResult.requested_page_size, persistedResult.requestedPageSize);
    productsDisplayLimit = productsLimit;
    const baseConclusion = text(first(reorder?.conclusion));
    const action = reorderActions[0];
    const actionStatus = text(action?.status).toLowerCase();
    const actionPayload = payloadOf(action?.responsePayload ?? action?.response);
    const matchedCount = number(findDeep(actionPayload, ['matched_count', 'matchedCount']));
    const expectedCount = number(findDeep(actionPayload, ['expected_count', 'expectedCount']));
    const partialMatch = matchedCount != null && expectedCount != null && matchedCount > 0 && matchedCount < expectedCount;
    const conclusion = reorder
      ? reorderConclusionWithStatuses(baseConclusion, reorder, Boolean(strategy), actionStatus)
      : baseConclusion;
    summary = conclusion || strategy || summary || (state === 'not_run' ? '未触发重排序' : '暂无策略摘要');
    if (reorder) {
      details.push({ label: '当前 CTR', value: formatRate(reorder.currentCtr) || '暂无数据' });
      details.push({ label: '近期 CTR', value: formatRate(reorder.recentCtr) || '暂无数据' });
      details.push({ label: 'CTR 变化', value: formatChange(reorder.ctrChange, reorder.currentCtr, reorder.recentCtr) });
      details.push({ label: 'CTR 告警阈值', value: formatAlertThreshold(reorder.ctrAlertThreshold) });
      details.push({ label: '当前 CVR', value: formatRate(reorder.currentCvr) || '暂无数据' });
      details.push({ label: '近期 CVR', value: formatRate(reorder.recentCvr) || '暂无数据' });
      details.push({ label: 'CVR 变化', value: formatChange(reorder.cvrChange, reorder.currentCvr, reorder.recentCvr) });
      details.push({ label: 'CVR 告警阈值', value: formatAlertThreshold(reorder.cvrAlertThreshold) });
      details.push({ label: '当前召回商品数', value: reorder.recallCount == null ? '暂无数据' : `${reorder.recallCount} 件` });
      details.push({ label: '低召回豁免阈值', value: reorder.lowRecallThreshold == null ? '暂无数据' : `≤ ${reorder.lowRecallThreshold} 件` });
      details.push(...stageMetricEntries(nodes, events, key));
      details.push({ label: '结论', value: conclusion || '暂无结论' });
    } else {
      details.push(...stageMetricEntries(nodes, events, key));
    }
    if (reorder && state !== 'not_run') {
      details.push({ label: 'AI 重排序策略', value: strategy || '暂无生成策略' });
    }
    if (products.length) {
      const displayProducts = productsLimit == null ? products : products.slice(0, productsLimit);
      details.push(withRawValue({
        label: reorder ? `向量重排序商品（前 ${productsLimit ?? products.length}）` : '调整商品',
        value: reorder ? `共 ${displayProducts.length} 件商品` : products.map(productText).join('、'),
      }, displayProducts));
    } else if (reorder && state !== 'not_run') {
      details.push({ label: `向量重排序商品（前 ${productsLimit ?? 'N'}）`, value: '暂无返回商品' });
    }
    if (reorder && state !== 'not_run') {
      if (matchedCount != null && expectedCount != null) {
        details.push({
          label: '排序回验命中',
          value: `${matchedCount}/${expectedCount} 个目标坑位${partialMatch ? '（部分生效）' : matchedCount === 0 ? '（全部未命中）' : '（全部命中）'}`,
        });
      }
      const executionValue = ['executed', 'verified', 'completed'].includes(actionStatus)
        ? '成功'
        : ['execution_unknown', 'action_readback_inconclusive', 'verification_inconclusive'].includes(actionStatus)
          ? '结果待确认'
          : '失败';
      details.push({ label: '重排序执行结果', value: executionValue });
      if (executionValue !== '成功') {
        details.push({
          label: '错误原因',
          value: reorderFailureReason(action, step || {}, persistedNode, evidence, rawSummary),
        });
      }
    } else if (state === 'failed') {
      details.push({
        label: '错误原因',
        value: reorderFailureReason(undefined, step || {}, persistedNode, evidence, rawSummary),
      });
    }
  } else if (key === 'relevance') {
    if (relevance) {
      const reusedIds = relevance.reusedProductIds ?? [];
      const detectedIds = relevance.detectedProductIds ?? [];
      const relevantIds = relevance.relevantProductIds ?? [];
      const blacklistCandidates = relevance.blacklistCandidates ?? [];
      const warnings = relevance.warnings ?? [];
      const detectedSet = new Set(detectedIds.map(String));
      const relevantNewCount = relevantIds.filter((id) => detectedSet.has(String(id))).length;
      const checkedCount = detectedIds.length;
      const accuracy = detectedIds.length > 0 ? (relevantNewCount / detectedIds.length) * 100 : null;
      const accuracyText = accuracy == null ? '暂无本轮准确率' : `本轮准确率 ${accuracy.toFixed(2)}%`;
      summary = `检测 ${checkedCount} 件：相关 ${relevantNewCount} 件，${accuracyText}`;
      const productEntries: Array<[string, RecordValue]> = (s0?.products ?? [])
        .map((product): [string, RecordValue] => [productIdOf(product), product])
        .filter(([id]) => Boolean(id));
      const productsById = new Map<string, RecordValue>(productEntries);
      const correctProducts = relevantIds
        .filter((id) => detectedSet.has(String(id)))
        .map((id) => productForId(id, productsById));
      const incorrectProducts = blacklistCandidates.map((candidate) => {
        const id = first(candidate.productId, candidate.product_id);
        const reason = text(candidate.reason);
        return { ...productForId(id, productsById), ...(reason ? { failureReason: reason } : {}) };
      });
      details.push({ label: '准确率', value: accuracy == null ? '暂无准确率' : `${accuracy.toFixed(2)}%` });
      details.push({ label: 'AI 检测', value: `${checkedCount} 件` });
      details.push({ label: '历史复用', value: `${reusedIds.length} 件` });
      details.push({ label: '正确商品', value: `${correctProducts.length} 件` });
      details.push({ label: '错误商品', value: `${incorrectProducts.length} 件` });
      details.push(...relevanceRemediationDetails(relevance));
      details.push(...stageMetricEntries(nodes, events, key));
      details.push(withRawValue({ label: '错误商品', value: incorrectProducts.length ? `共 ${incorrectProducts.length} 件商品` : '暂无错误商品' }, incorrectProducts));
      details.push(withRawValue({ label: '正确商品', value: correctProducts.length ? `共 ${correctProducts.length} 件商品` : '暂无正确商品' }, correctProducts));
      details.push({ label: '结论', value: summary });
      if (warnings.length) details.push({ label: '检测警告', value: warnings.map((warning) => {
        const item = asRecord(warning);
        return [text(first(item.code, item.warningCode, item.warning_code)), text(first(item.message, item.reason))].filter(Boolean).join('：') || JSON.stringify(warning);
      }).join('、') });
    } else {
      const irrelevant = number(evidence.mismatch_count, evidence.mismatchCount, evidence.irrelevant_count, evidence.irrelevantCount, eventPayload.mismatch_count, eventPayload.mismatchCount);
      const checked = number(evidence.checked_count, evidence.checkedCount, eventPayload.checked_count, eventPayload.checkedCount);
      if (irrelevant != null) {
        summary = `${irrelevant} 件不相关`;
        details.push({ label: '不相关商品', value: `${irrelevant} 件` });
        if (checked != null) details.push({ label: '检测商品', value: `${checked} 件` });
      } else summary = summary || '暂无相关性结果';
    }
  } else if (key === 'inventory') {
    if (inventoryFailed) {
      const errorCode = text(inventoryExecution?.errorCode);
      const errorMessage = text(inventoryExecution?.errorMessage) || '断码动作执行失败';
      summary = `执行失败：${errorMessage}`;
      details.push({ label: '错误原因', value: [errorCode, errorMessage].filter(Boolean).join('：') });
    }
    const latestInventory = [...inventory]
      .sort((left, right) => String(right.checkedAt || right.updatedAt || right.createdAt || '').localeCompare(String(left.checkedAt || left.updatedAt || left.createdAt || '')))[0];
    // Query-governance timestamps are naive UTC; inventory records are local
    // Beijing time. Normalize both before comparing for the latest check.
    const governanceTimes = [
      evidence.checked_at, evidence.checkedAt, evidence.last_checked_at, evidence.lastCheckedAt,
      eventPayload.checked_at, eventPayload.checkedAt,
      ...actions.filter((action) => action.checkKey === key).flatMap((action) => [
        action.verifiedAt, action.executedAt, action.finishedAt, action.startedAt,
      ]),
      event?.createdAt,
    ]
      .map((value) => formatTime(value))
      .filter(Boolean);
    const inventoryTimes = [
      ...inventory.map((record) => record.checkedAt),
      latestInventory?.updatedAt,
      latestInventory?.createdAt,
    ]
      .map((value) => formatLocalTime(value))
      .filter(Boolean);
    const time = [...governanceTimes, ...inventoryTimes]
      .sort((left, right) => String(right).localeCompare(String(left)))[0] || '';
    const latestScanTime = time || formatLocalTime(inventoryScans && inventoryScans[0]?.checkedAt);
    const activeInventory = inventory.filter((record) => ['monitoring', 'auto_stopped', 'manually_stopped'].includes(text(record.status).toLowerCase()));
    if (inventoryFailed) {
      // Keep error summary
    } else if (activeInventory.length > 0) {
      summary = latestScanTime ? `巡检 ${latestScanTime}；检测到断码商品` : '检测到断码商品';
    } else {
      summary = latestScanTime
        ? `最近一次巡检时间：${latestScanTime} ；未发现断码商品`
        : (rawSummary && rawSummary !== '已巡检，未发现断码商品' ? rawSummary : '暂无巡检记录');
    }
    if (latestScanTime) details.push({ label: '最近巡检', value: latestScanTime });
    const recoveredCount = recoveredTodayCount ?? number(evidence.recovered_count, evidence.recoveredCount) ?? inventory.filter((record) => text(record.status).toLowerCase() === 'recovered').length;
    details.push({ label: '断码商品数量', value: `${activeInventory.length} 件` });
    details.push({ label: '今日恢复商品', value: `${recoveredCount} 件` });
    if (inventoryCheckIntervalMinutes != null) details.push({ label: '监测间隔', value: `每 ${inventoryCheckIntervalMinutes} 分钟` });
    if (latestInventory) {
      details.push(withRawValue({ label: '断码商品明细', value: `共 ${inventory.length} 件商品` }, inventory.map((record) => ({
        productId: record.productId,
        productName: record.productName,
        ...(record.imageUrl ? { imageUrl: record.imageUrl } : {}),
        ...(number(record.lastBrokenRate) == null ? {} : {
          broken_size_rate: Number(record.lastBrokenRate) <= 1 ? Number(record.lastBrokenRate) : Number(record.lastBrokenRate) / 100,
        }),
        inventoryStatus: inventoryStatusLabel(record.status),
        inventoryCheckedAt: record.checkedAt || record.updatedAt || record.createdAt,
        ...(record.brokenRateBefore != null && record.lastBrokenRate != null ? {
          brokenRateTransition: `${inventoryRateText(record.brokenRateBefore)} → ${inventoryRateText(record.lastBrokenRate)}`,
        } : {}),
      }))));
    }
  }

  if (
    rawSummary
    && key !== 'relevance'
    && key !== 'inventory'
    && key !== 'open_factor'
    && !details.some((entry) => entry.value === rawSummary)
  ) {
    details.push({ label: '执行结果', value: rawSummary });
  }
  if (key !== 'open_factor') details.push(...errorDetails(step || {}, rawSummary, persistedNode?.errorCode));
  if (step?.governanceStatus?.label) {
    details.push({ label: '治理方案状态', value: step.governanceStatus.label });
    if (step.governanceStatus.errorMessage) details.push({
      label: '治理方案错误原因',
      value: [step.governanceStatus.errorCode, step.governanceStatus.errorMessage].filter(Boolean).join('：'),
    });
  }
  return {
    key,
    label: labels[key],
    state,
    summary,
    details,
    governanceStatus: step?.governanceStatus,
    ...(key === 'recall' && recall?.kgStrategy ? { kgStrategy: recall.kgStrategy } : {}),
    ...(key === 'open_factor' ? { productsLimit: productsDisplayLimit } : {}),
    ...(key === 'inventory' ? { inventoryScans } : {}),
  };
};

export const buildExecutionOverview = ({ steps = [], nodes = [], s0, recall, relevance, events = [], actions = [], inventory = [], inventoryCheckIntervalMinutes, recoveredTodayCount, inventoryExecution, inventoryScans, reorder, visibleCheckKeys }: Input): ExecutionOverviewNode[] => {
  const byKey = new Map(steps.map((step) => [String(step.checkKey || ''), step]));
  return order.filter((key) => !visibleCheckKeys || visibleCheckKeys.includes(key)).map((key) => buildNode(key, byKey.get(key), nodes, s0, recall, events, actions, inventory, relevance, reorder, inventoryCheckIntervalMinutes, recoveredTodayCount, inventoryExecution, inventoryScans));
};
