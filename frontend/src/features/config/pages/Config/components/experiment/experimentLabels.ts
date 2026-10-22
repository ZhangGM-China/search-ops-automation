export type CalculationVersionKind = 'v2' | 'unsupported';

export const calculationVersionKind = (value?: string | null): CalculationVersionKind => {
  if (value === 'top-query-ab-test-v2') return 'v2';
  return 'unsupported';
};

export const calculationVersionLabel = (value?: string | null) => {
  const kind = calculationVersionKind(value);
  if (kind === 'v2') return '新版 7 日加权口径';
  return '不支持的计算版本';
};

const experimentStatuses: Record<string, string> = {
  draft: '草稿',
  scheduled: '已排期',
  running: '运行中',
  ended: '已结束',
  cancelled: '已取消',
};

const observationStatuses: Record<string, string> = {
  observed: '已观测',
  data_quality_failed: '数据异常',
  not_synced: '未同步',
  not_observed: '未观测',
  unobserved: '未观测',
  missing: '未观测',
};

const contaminationStatuses: Record<string, string> = {
  clean: '无污染',
  contaminated: '已污染',
};

const dataQualityStatuses: Record<string, string> = {
  complete: '完整',
  partial: '部分可用',
  failed: '异常',
  missing: '缺失',
};

const numeratorSources: Record<string, string> = {
  raw: '真实分子',
  derived: '推导分子',
  partial: '部分分子',
  invalid: '分子无效',
  mixed: '混合来源',
  missing: '分子缺失',
};

const unknownLabel = (prefix: string, value?: string | null) => value ? `${prefix}（${value}）` : '未知';

export const experimentStatusLabel = (value?: string | null) => experimentStatuses[value ?? ''] ?? unknownLabel('未知状态', value);
export const observationStatusLabel = (value?: string | null) => observationStatuses[value ?? ''] ?? unknownLabel('未知状态', value);
export const contaminationStatusLabel = (value?: string | null) => contaminationStatuses[value ?? ''] ?? unknownLabel('未知状态', value);
export const dataQualityStatusLabel = (value?: string | null) => dataQualityStatuses[value ?? ''] ?? unknownLabel('未知状态', value);
export const numeratorSourceLabel = (value?: string | null) => numeratorSources[value ?? ''] ?? unknownLabel('未知来源', value);
export const experimentGroupLabel = (value?: string | null) => {
  if (value === 'treatment') return '实验组';
  if (value === 'control') return '对照组';
  return unknownLabel('未知组别', value);
};

export const numeratorEvidenceLabel = (click?: string | null, purchase?: string | null) => (
  `点击${dataQualityStatusLabel(click)} / 购买${dataQualityStatusLabel(purchase)}`
);
