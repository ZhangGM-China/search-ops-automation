const qualityErrorLabels: Record<string, string> = {
  no_observations: '尚无可用的实验观测数据',
  source_partial: '生产数据源返回不完整',
  source_failed: '生产数据源抽取失败',
  source_data_quality_failed: '生产数据质量校验失败',
  incomplete_daily_metrics: '当日未满 40/40 观测样本',
  invalid_experiment_sample_shape: '实验组与对照组样本数不符合 20+20',
  experiment_quality_gate_failed: '样本、任务或动作数据未通过完整性门禁',
  control_group_contamination: '对照组发生了治理任务或动作污染',
  incomplete_governance: '实验组治理任务未完整收敛',
  governance_not_resolved: '实验组治理任务或动作尚未全部收敛',
  zero_search_uv_denominator: '实验组或对照组 Search UV 为 0，无法计算效果',
  invalid_baseline: '固定样本基线缺失或基线 Search UV 为 0',
  snapshot_identity_mismatch: '实验快照与当前品牌、日期或实验不匹配',
};

const requiredLatestDayEvidence = [
  'expectedSamples', 'observedSamples', 'treatmentSamples', 'controlSamples',
  'treatmentTaskCount', 'controlTaskCount', 'controlActionCount', 'contaminationCount',
  'failedTaskCount', 'unresolvedTaskCount', 'failedActionCount', 'unresolvedActionCount',
] as const;

export const hasCompleteQualityEvidence = (quality: { latestDayQuality?: Record<string, unknown> | null; latestDay?: Record<string, unknown> }) => {
  const latest = quality.latestDayQuality ?? quality.latestDay;
  return Boolean(latest && requiredLatestDayEvidence.every((key) => (
    typeof latest[key] === 'number' && Number.isFinite(latest[key])
  )));
};

export const formatQualityError = (error?: string | null) => {
  if (!error) return '样本或治理数据不完整，暂不输出效果结论';
  return qualityErrorLabels[error] ?? `数据质量异常（${error}）`;
};
