import dayjs from 'dayjs';
import type { TopQueryAction, TopQueryDetailData, TopQueryStepStatus, TopQueryTask } from '@/shared/types';
import { getTopQueryStatusLabel } from '../../lib/queryGovernanceStatus';

export const brandNames: Record<string, string> = {
  atlas: 'Atlas',
  harbor: 'Harbor',
  meadow: 'Meadow',
  summit: 'Summit',
  riviera: 'Riviera',
  lumen: 'Lumen',
};

const DESKTOP_LIST_WIDTH = 392;
const numberText = (value?: number | null) => new Intl.NumberFormat('zh-CN').format(value ?? 0);
const percent = (value?: number | null) => `${(((value ?? 0) <= 1 ? (value ?? 0) * 100 : (value ?? 0))).toFixed(2)}%`;
const isDateText = (value: string | null) => Boolean(value && /^\d{4}-\d{2}-\d{2}$/.test(value) && dayjs(value).isValid());
const httpStatus = (error: unknown) => {
  if (!error || typeof error !== 'object') return null;
  const response = (error as { response?: { status?: unknown } }).response;
  return typeof response?.status === 'number' ? response.status : null;
};

const isInvalidatedTask = (task?: TopQueryTask | null) => Boolean(
  task?.invalidated
  || ['cancelled', 'superseded'].includes(String(task?.status || '').toLowerCase())
  || String(task?.displayStatus || '').toLowerCase() === 'superseded'
);

const withLiveRecallSnapshot = (
  detail: TopQueryDetailData,
  liveResult: { resultCount: number | null; products?: Record<string, unknown>[]; productDisplayLimit?: number } | null
): TopQueryDetailData => {
  if (!liveResult) return detail;
  const nextDetail = {
    ...detail,
    ...(typeof liveResult.productDisplayLimit === 'number' && liveResult.productDisplayLimit > 0
      ? { productDisplayLimit: liveResult.productDisplayLimit }
      : {}),
  };
  if (liveResult.resultCount == null) return nextDetail;
  return {
    ...nextDetail,
    liveRecall: {
      resultCount: liveResult.resultCount,
      products: liveResult.products ?? [],
    },
    task: {
      ...detail.task,
      resultCount: liveResult.resultCount,
    },
  };
};

type UnknownRecord = Record<string, unknown>;

const asRecord = (value: unknown): UnknownRecord => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : {}
);

const normalizeStepStatuses = (rawValue: unknown): TopQueryStepStatus[] => {
  const raw = asRecord(rawValue);
  const source = Array.isArray(raw?.stepStatuses)
    ? raw.stepStatuses
    : Array.isArray(raw?.nodes) ? raw.nodes : [];
  const mapped: TopQueryStepStatus[] = source
    .map(asRecord)
    .filter((step) => ['recall', 'reorder', 'open_factor', 'relevance', 'inventory'].includes(String(step.checkKey || step.nodeKey)))
    .map((step): TopQueryStepStatus => {
      const governanceStatus = asRecord(step.governanceStatus);
      return ({
        checkKey: String(step.checkKey || (step.nodeKey === 'reorder' ? 'open_factor' : step.nodeKey)),
        status: String(step.status || 'normal'),
        displayStatus: String(step.displayStatus || step.status || 'normal'),
        summary: step.summary ? String(step.summary) : step.errorMessage ? String(step.errorMessage) : null,
        evidence: step.evidence,
        governanceStatus: step.governanceStatus && typeof step.governanceStatus === 'object'
          ? {
            status: governanceStatus.status ? String(governanceStatus.status) : null,
            label: governanceStatus.label ? String(governanceStatus.label) : null,
            errorCode: governanceStatus.errorCode ? String(governanceStatus.errorCode) : null,
            errorMessage: governanceStatus.errorMessage ? String(governanceStatus.errorMessage) : null,
          }
          : null,
      });
    });
  const byKey = new Map(mapped.map((step) => [step.checkKey, step]));
  return ['recall', 'open_factor', 'relevance', 'inventory'].map((checkKey) => (
    byKey.get(checkKey) || { checkKey, status: 'normal', displayStatus: 'normal' }
  ));
};

const normalizeTopQueryTask = (rawValue: unknown): TopQueryTask => {
  const raw = asRecord(rawValue);
  const source = asRecord(raw.task || raw.query || raw);
  const status = String(source.status || 'pending');
  const displayStatus = source.displayStatus
    || (status === 'error' ? 'still_problematic' : status === 'pending' ? 'waiting_human_approval' : 'normal');
  return {
    ...source,
    id: Number(source.id),
    brand: String(source.brand || ''),
    dataDate: source.dataDate ?? null,
    queryText: String(source.queryText || ''),
    queryRank: source.queryRank ?? null,
    searchUv: Number(source.searchUv ?? 0),
    searchPv: Number(source.searchPv ?? 0),
    ctr: Number(source.ctr ?? 0),
    cvr: Number(source.cvr ?? 0),
    avgCtr: source.avgCtr ?? 0,
    avgCvr: source.avgCvr ?? 0,
    status,
    displayStatus,
    resultCount: source.resultCount ?? null,
    stepStatuses: normalizeStepStatuses(source),
    visibleCheckKeys: Array.isArray(source.visibleCheckKeys) ? source.visibleCheckKeys.map(String) : undefined,
  } as TopQueryTask;
};

const normalizeTopQueryDetail = (rawValue: unknown): TopQueryDetailData => {
  const raw = asRecord(rawValue);
  const task = normalizeTopQueryTask(raw);
  const actions: TopQueryAction[] = (Array.isArray(raw.actions) ? raw.actions : []).map((item) => {
    const action = asRecord(item);
    return {
      ...action,
      id: Number(action.id),
      taskId: Number(action.taskId ?? task.id),
      checkKey: String(action.checkKey || action.nodeKey || ''),
      toolKey: String(action.toolKey || action.actionType || ''),
      actionType: String(action.actionType || ''),
      executionMode: String(action.executionMode || 'auto'),
      status: String(action.status || 'pending'),
    } as TopQueryAction;
  });
  return {
    ...raw,
    task,
    actions,
    stepStatuses: normalizeStepStatuses(Array.isArray(asRecord(raw.query).stepStatuses) ? raw.query : raw),
    visibleCheckKeys: Array.isArray(raw.visibleCheckKeys) ? raw.visibleCheckKeys.map(String) : task.visibleCheckKeys,
    trend: raw?.trend || [],
  } as TopQueryDetailData;
};

type FilterKey =
  | 'all'
  | 'waiting_human_approval'
  | 'fixed'
  | 'normal'
  | 'still_problematic'
  | 'recall'
  | 'result_drop'
  | 'relevance'
  | 'open_factor'
  | 'inventory';

const toFilterKey = (task: TopQueryTask): FilterKey => {
  const label = getTopQueryStatusLabel(task.displayStatus);
  if (label === '正常') return 'normal';
  if (label === '已处理' || label === '已改善') return 'fixed';
  if (label === '需处理待确认') return 'waiting_human_approval';
  return 'still_problematic';
};

const stepHasIssue = (step: TopQueryStepStatus) => getTopQueryStatusLabel(step.displayStatus || step.status) !== '正常';
const stepMatchesFilter = (step: TopQueryStepStatus) => {
  const status = String(step.status || step.displayStatus || '').toLowerCase();
  if (step.checkKey === 'open_factor' || step.checkKey === 'reorder') {
    const evidence = step.evidence && typeof step.evidence === 'object' && !Array.isArray(step.evidence)
      ? step.evidence as Record<string, unknown>
      : null;
    const rawIssueTypes = evidence?.issueTypes ?? evidence?.issue_types ?? evidence?.type;
    if (Array.isArray(rawIssueTypes) ? rawIssueTypes.length > 0 : Boolean(rawIssueTypes)) return true;
    return ['failed', 'error', 'still_problematic', 'check_failed', '执行失败'].includes(status);
  }
  const evidence = step.evidence && typeof step.evidence === 'object' && !Array.isArray(step.evidence)
    ? step.evidence as Record<string, unknown>
    : null;
  const rawIssueTypes = evidence?.issueTypes ?? evidence?.issue_types ?? evidence?.type;
  if (Array.isArray(rawIssueTypes) ? rawIssueTypes.length > 0 : Boolean(rawIssueTypes)) return true;
  const candidates = evidence?.blacklistCandidates ?? evidence?.blacklist_candidates;
  if (Array.isArray(candidates) && candidates.length > 0) return true;
  const irrelevantCount = evidence?.irrelevantProductCount ?? evidence?.irrelevant_product_count;
  if (typeof irrelevantCount === 'number' && irrelevantCount > 0) return true;
  return !['', 'normal', 'pass', 'executed', 'skipped', 'unknown', 'missing', 'pending', 'running', '已执行', '未执行', '正常'].includes(status);
};
const stepIssueTypes = (step: TopQueryStepStatus) => {
  const evidence = step.evidence;
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return [];
  const record = evidence as Record<string, unknown>;
  const raw = record.issueTypes || record.issue_types || record.type || record.reason;
  if (Array.isArray(raw)) return raw.map(String);
  return raw ? [String(raw)] : [];
};

export {
  DESKTOP_LIST_WIDTH,
  httpStatus,
  isDateText,
  isInvalidatedTask,
  numberText,
  percent,
  stepHasIssue,
  stepMatchesFilter,
  stepIssueTypes,
  toFilterKey,
  withLiveRecallSnapshot,
  normalizeTopQueryDetail,
  normalizeTopQueryTask,
};
export type { FilterKey };
