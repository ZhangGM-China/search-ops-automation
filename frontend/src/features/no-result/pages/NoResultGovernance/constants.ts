export const brandOptions = [
  { label: 'Atlas', value: 'atlas' },
  { label: 'HARBOR', value: 'harbor' },
  { label: 'Summit', value: 'summit' },
  { label: 'Meadow', value: 'meadow' },
  { label: 'Riviera', value: 'riviera' },
  { label: 'Lumen', value: 'lumen' },
];

export const statusLabelMap: Record<string, string> = {
  pending: '待处理',
  rolling_back: '回滚中',
  pending_execute: '待执行',
  awaiting_retag: '待重打标签',
  retagged_pending_review: '已重打标签待复查',
  completed: '已完成',
  rolled_back: '已回滚',
  manual_required: '待人工调整',
  failed: '治理失败',
  partial_failed: '部分失败',
  conflict_manual_review: '回滚冲突待人工',
  ignored: '不处理',
};

export const statusColorMap: Record<string, string> = {
  pending: 'gold',
  rolling_back: 'processing',
  pending_execute: 'gold',
  awaiting_retag: 'processing',
  retagged_pending_review: 'processing',
  completed: 'success',
  rolled_back: 'default',
  manual_required: 'warning',
  failed: 'error',
  partial_failed: 'error',
  conflict_manual_review: 'warning',
  ignored: 'default',
};

export const actionStatusLabelMap: Record<string, string> = {
  success: '已执行',
  pending_execute: '待执行',
  failed: '执行失败',
  manual_review: '待人工复核',
  skipped: '已跳过',
};

export const actionStatusColorMap: Record<string, string> = {
  success: 'success',
  pending_execute: 'gold',
  failed: 'error',
  manual_review: 'warning',
  skipped: 'default',
};

export const rollbackStatusLabelMap: Record<string, string> = {
  pending: '待回滚',
  rolling_back: '回滚中',
  success: '已回滚',
  failed: '回滚失败',
  partial_failed: '部分失败',
  conflict: '冲突待人工',
  conflict_manual_review: '冲突待人工',
  not_required: '无需回滚',
};

export const rollbackStatusColorMap: Record<string, string> = {
  pending: 'gold',
  rolling_back: 'processing',
  success: 'success',
  failed: 'error',
  partial_failed: 'error',
  conflict: 'warning',
  conflict_manual_review: 'warning',
  not_required: 'default',
};

export const riskColorMap: Record<string, string> = {
  low: 'success',
  medium: 'warning',
  high: 'error',
};

export const formatJson = (value: Record<string, unknown>) => {
  if (!value || Object.keys(value).length === 0) return '{}';
  return JSON.stringify(value, null, 2);
};

export const getMobileStatusColor = (status: string) => {
  if (['completed', 'rolled_back'].includes(status)) return 'success';
  if (['failed', 'partial_failed'].includes(status)) return 'danger';
  if (['manual_required', 'conflict_manual_review', 'pending', 'pending_execute'].includes(status)) return 'warning';
  return 'primary';
};

export type NoResultGovernanceFilterValues = {
  date?: string | Date | number | object | null;
  brand?: string | string[] | null;
  status?: string | string[] | null;
  query_text?: string | null;
  page?: number;
  limit?: number;
  [key: string]: string | string[] | number | boolean | Date | object | null | undefined;
};
