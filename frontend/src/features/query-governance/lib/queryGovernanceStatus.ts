import type { TopQueryDisplayStatus, TopQueryStepStatus } from '@/shared/types';

/**
 * 检查项状态模型：一种状态对应一个标签和一种颜色，状态可以有多条触发条件。
 * 判定按 failed > pending > not_run > attention > normal 的优先级取第一条命中。
 */
export type CheckState = 'failed' | 'pending' | 'not_run' | 'attention' | 'normal';
export type CheckStateLabel = '异常' | '待处理' | '未执行' | '需关注' | '正常';
export type CheckStateTone = 'red' | 'yellow' | 'default' | 'blue' | 'green';

export const checkStateMeta: Record<CheckState, { label: CheckStateLabel; tone: CheckStateTone }> = {
  failed: { label: '异常', tone: 'red' },
  pending: { label: '待处理', tone: 'yellow' },
  not_run: { label: '未执行', tone: 'default' },
  attention: { label: '需关注', tone: 'blue' },
  normal: { label: '正常', tone: 'green' },
};

export const checkStateLabel = (state: CheckState) => checkStateMeta[state].label;
export const checkStateTone = (state: CheckState) => checkStateMeta[state].tone;

const localizedStatusAliases: Record<string, string> = {
  已执行: 'executed', 已完成: 'completed', 已改善: 'fixed', 正常: 'normal',
  执行中: 'running', 待执行: 'pending', 未执行: 'skipped', 已跳过: 'skipped',
  执行失败: 'error', 异常: 'error', 处理后仍有问题: 'still_problematic',
};

const checkFailureStatuses = new Set(['failed', 'check_failed', 'error', 'still_problematic']);
const checkPendingStatuses = new Set([
  'pending', 'running', 'in_progress', 'collecting', 'planning', 'executing',
  'waiting_human_approval', 'manual_required', 'warning',
]);
const checkNotRunStatuses = new Set(['skipped', 'skip', 'disabled', 'not_run', 'cancelled', 'superseded']);
const checkAttentionStatuses = new Set(['fixed', 'completed', 'improved', 'verified']);

const governanceFailureStatuses = new Set(['failed', 'rollback_failed']);
const governancePendingStatuses = new Set(['pending', 'executing', 'manual_review', 'execution_unknown', 'rollback_pending']);

const checkStatusOf = (step: Partial<Pick<TopQueryStepStatus, 'status' | 'displayStatus'>>) => {
  const value = String(step.displayStatus || step.status || '').trim().toLowerCase();
  return localizedStatusAliases[value] ?? value;
};

/** 检查项自身的执行状态，只看检查结果，不看下游治理和问题证据。 */
export const resolveCheckExecutionState = (
  step: Partial<Pick<TopQueryStepStatus, 'status' | 'displayStatus'>> = {},
): CheckState => {
  const status = checkStatusOf(step);
  if (checkFailureStatuses.has(status)) return 'failed';
  if (checkPendingStatuses.has(status)) return 'pending';
  if (checkNotRunStatuses.has(status) || status === '') return 'not_run';
  return 'normal';
};

/** 检查证据里是否记录了需要关注的问题（相关性不符、断码、需要重排序等）。 */
export const hasStepIssueEvidence = (evidence: unknown): boolean => {
  const record = evidence && typeof evidence === 'object' && !Array.isArray(evidence)
    ? evidence as Record<string, unknown>
    : {};
  const rawIssueTypes = record.issueTypes ?? record.issue_types ?? record.type;
  if (Array.isArray(rawIssueTypes) ? rawIssueTypes.length > 0 : Boolean(rawIssueTypes)) return true;
  if (record.should_reorder === true || record.shouldReorder === true) return true;
  return ['mismatch_count', 'mismatchCount', 'irrelevant_count', 'irrelevantCount', 'affectedProductCount']
    .some((key) => {
      const value = record[key];
      if (value == null || value === '') return false;
      const parsed = Number(value);
      return Number.isFinite(parsed) && parsed > 0;
    });
};

/**
 * 检查项最终状态：合并检查执行状态、下游治理状态与问题证据。
 * 治理失败/待处理优先于检查结果，因为此时问题还没收敛。
 */
export const resolveCheckState = (
  step: Partial<Pick<TopQueryStepStatus, 'status' | 'displayStatus' | 'governanceStatus'>> = {},
  hasDetectedIssue = false,
): CheckState => {
  const governanceStatus = String(step.governanceStatus?.status || '').trim().toLowerCase();
  if (governanceFailureStatuses.has(governanceStatus)) return 'failed';
  const executionState = resolveCheckExecutionState(step);
  if (executionState === 'failed') return 'failed';
  if (governancePendingStatuses.has(governanceStatus) || executionState === 'pending') return 'pending';
  if (executionState === 'not_run') return 'not_run';
  if (checkAttentionStatuses.has(checkStatusOf(step)) || hasDetectedIssue) return 'attention';
  return 'normal';
};

/** 任务列表筛选用的标签，只描述治理任务本身，不参与检查状态配色。 */
const taskStatusLabelMap: Record<string, string> = {
  normal: '正常',
  pass: '正常',
  completed: '已处理',
  fixed: '已改善',
  waiting_human_approval: '需处理待确认',
  manual_required: '需处理待确认',
  warning: '需处理待确认',
  still_problematic: '处理后仍有问题',
  failed: '处理后仍有问题',
  check_failed: '处理后仍有问题',
  pending: '待执行',
  running: '执行中',
  executed: '已执行',
  skipped: '未执行',
  error: '执行失败',
  cancelled: '已失效',
  superseded: '已失效',
};

export const getTopQueryStatusLabel = (status?: TopQueryDisplayStatus | null) => {
  if (!status) return taskStatusLabelMap.normal;
  return taskStatusLabelMap[String(status)] ?? taskStatusLabelMap.still_problematic;
};