import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { TopQueryExperimentReportData } from '@/shared/types';
import ExperimentReport from './ExperimentReport';

vi.mock('@ant-design/charts', () => ({
  DualAxes: () => <div data-testid="mock-dual-axes-chart" />,
  Line: () => <div data-testid="mock-line-chart" />,
}));

const completeDayQuality = {
  expectedSamples: 40,
  observedSamples: 40,
  treatmentSamples: 20,
  controlSamples: 20,
  treatmentTaskCount: 20,
  controlTaskCount: 0,
  controlActionCount: 0,
  contaminationCount: 0,
  failedTaskCount: 0,
  unresolvedTaskCount: 0,
  failedActionCount: 0,
  unresolvedActionCount: 0,
};

const group = {
  sampleCount: 20,
  searchUv: 300,
  searchPv: 900,
  clickUv: 8,
  purchaseUv: 2,
  baselineSearchUv: 280,
  baselineSearchPv: 840,
  baselineClickUv: 20,
  baselinePurchaseUv: 5,
  baselineCtr: 0.0714,
  baselineCvr: 0.0179,
  currentCtr: 0.08,
  currentCvr: 0.02,
  ctrChange: 0.0086,
  cvrChange: 0.0021,
};

const report: TopQueryExperimentReportData = {
  reportSnapshotToken: 'snapshot-9-v1',
  experiment: {
    experimentId: 9,
    brand: 'atlas',
    experimentName: 'ATLAS 七日实验',
    status: 'running',
    plannedStartDate: '2026-07-08',
    plannedEndDate: '2026-07-14',
    baselineStartDate: '2026-07-01',
    baselineEndDate: '2026-07-07',
    baselineLockedAt: '2026-07-08T00:10:00+08:00',
    calculationVersion: 'top-query-ab-test-v2',
  },
  calculationVersion: 'top-query-ab-test-v2',
  generatedAt: '2026-07-12T08:00:00+08:00',
  timezone: 'Asia/Shanghai',
  baselineQuality: {
    status: 'complete',
    calculationVersion: 'top-query-ab-test-v2',
    sampleCount: 40,
    treatmentSamples: 20,
    controlSamples: 20,
    expectedDays: 7,
    baselineStartDate: '2026-07-01',
    baselineEndDate: '2026-07-07',
    baselineLockedAt: '2026-07-08T00:10:00+08:00',
    windowValid: true,
    locked: true,
  },
  quality: { status: 'complete', completeDays: 1, failedDays: 0, latestDayQuality: completeDayQuality },
  groups: { treatment: group, control: { ...group, currentCtr: 0.062, ctrChange: -0.0094 } },
  effect: {
    ctrNetChange: 0.0267,
    cvrNetChange: 0.0042,
    ctrPositiveDays: 1,
    cvrPositiveDays: 1,
    estimatedIncrementalClickUv: 8,
    estimatedIncrementalPurchaseUv: 2,
  },
  operations: {
    governanceCoverage: 1,
    actionSuccessRate: 0.5,
    plannedActions: 2,
    executedActions: 1,
    failedActions: 1,
    inconclusiveActions: 0,
    unresolvedActions: 0,
    rejectedActions: 0,
    cancelledActions: 0,
    tokenUsage: 321,
    durationSeconds: 12.5,
  },
  trend: [{
    dataDate: '2026-07-08',
    quality: { status: 'complete', latestDayQuality: completeDayQuality },
    groups: { treatment: group, control: { ...group, currentCtr: 0.062, ctrChange: -0.0094 } },
    effect: { ctrNetChange: 0.0267, cvrNetChange: 0.0042 },
    operations: {
      governanceCoverage: 1, actionSuccessRate: 0.5, plannedActions: 2, executedActions: 1,
      failedActions: 1, tokenUsage: 321, durationSeconds: 12.5,
    },
  }],
  queries: [{
    queryText: '连衣裙 女士', groupType: 'treatment', sampleRank: 1,
    baselineSearchUv: 100, baselineSearchPv: 300, baselineClickUv: 7, baselinePurchaseUv: 2,
    baselineObservedDays: 7, baselineCtr: 0.07, baselineCvr: 0.02,
    latestDataDate: '2026-07-08', observedStatus: 'observed', contaminationStatus: 'clean',
    currentSearchUv: 110, currentSearchPv: 330, currentClickUv: 10, currentPurchaseUv: 3,
    currentCtr: 0.0909, currentCvr: 0.0273, ctrChange: 0.0209, cvrChange: 0.0073,
    observedDays: 1, unobservedDays: 0, contaminatedDays: 0,
    numeratorSource: 'raw', dataQualityStatus: 'complete',
    groupContribution: { ctr: 0.01, cvr: 0.003 }, netCtrContribution: 0.008, netCvrContribution: 0.002,
    effectDirection: 'positive',
    governanceTaskId: 101, governanceStatus: 'completed', actionCount: 2, tokenUsage: 321, durationSeconds: 12.5,
  }],
  queryDailyMetrics: [{
    dataDate: '2026-07-08', queryText: '连衣裙 女士', groupType: 'treatment',
    observedStatus: 'observed', contaminationStatus: 'clean', searchUv: 110, searchPv: 330,
    clickUv: 10, purchaseUv: 3, ctr: 0.0909, cvr: 0.0273, numeratorSource: 'raw',
    clickNumeratorSource: 'raw', purchaseNumeratorSource: 'raw',
    clickNumeratorStatus: 'complete', purchaseNumeratorStatus: 'complete',
    dataQualityStatus: 'complete', includedInEffect: true, governanceTaskId: 101,
    governanceStatus: 'completed', actionCount: 2, autoActionCount: 1, manualActionCount: 1,
    tokenUsage: 321, durationSeconds: 12.5,
  }],
  baselineEvidence: [],
  actions: [{
    recordType: 'action', detailAvailable: true, dataDate: '2026-07-08', groupType: 'treatment',
    id: 1, taskId: 101, brand: 'atlas', queryText: '连衣裙 女士', checkKey: 'inventory',
    actionType: 'query_demote', executionMode: 'auto', status: 'failed', reason: '断码率过高',
    errorMessage: 'timeout', createdAt: '2026-07-08T10:00:00+08:00',
  }],
  dataStartDate: '2026-07-08', dataEndDate: '2026-07-08', dataDays: 1,
  overview: {
    experimentId: 9, brand: 'atlas', experimentName: 'ATLAS 七日实验', status: 'running',
    quality: { status: 'complete', completeDays: 1, failedDays: 0, latestDayQuality: completeDayQuality },
    groups: { treatment: group, control: { ...group, currentCtr: 0.062, ctrChange: -0.0094 } },
    effect: { ctrNetChange: 0.0267, cvrNetChange: 0.0042, ctrPositiveDays: 1, cvrPositiveDays: 1, estimatedIncrementalClickUv: 8, estimatedIncrementalPurchaseUv: 2 },
    operations: {
      governanceCoverage: 1, actionSuccessRate: 0.5, plannedActions: 2, executedActions: 1,
      failedActions: 1, tokenUsage: 321, durationSeconds: 12.5,
    },
  },
};

afterEach(cleanup);

describe('ExperimentReport', () => {
  it('renders the server-provided seven-day baseline and effect without recomputing', () => {
    const reportWithEffectEvidence = {
      ...report,
      overview: {
        ...report.overview,
        effect: {
          ...report.overview.effect,
          ctrPositiveDays: 4,
          cvrPositiveDays: 3,
          estimatedIncrementalClickUv: 12.5,
          estimatedIncrementalPurchaseUv: -2,
        },
      },
    } as TopQueryExperimentReportData;
    render(<ExperimentReport report={reportWithEffectEvidence} isMobile={false} />);
    expect(screen.getByText('2026-07-01 至 2026-07-07')).toBeVisible();
    expect(screen.getByText('+2.67%')).toBeVisible();
    expect(screen.getByText('效果结果已生成')).toBeVisible();
    expect(screen.getByText('请结合 CTR 与 CVR 查看')).toBeVisible();
    expect(screen.getByText('CTR 正向效果天数')).toBeVisible();
    expect(screen.getByText('4 天')).toBeVisible();
    expect(screen.getByText('CVR 正向效果天数')).toBeVisible();
    expect(screen.getByText('3 天')).toBeVisible();
    expect(screen.getByText('预估 Click UV 增量')).toBeVisible();
    expect(screen.getByText('+12.50')).toBeVisible();
    expect(screen.getByText('预估 Purchase UV 增量')).toBeVisible();
    expect(screen.getByText('预估 Purchase UV 增量').closest('.ant-statistic')).toHaveTextContent('-2.00');
    expect(screen.getByText('估算，不等同因果归因')).toBeVisible();
  });

  it('does not infer an overall positive or negative conclusion when CTR and CVR disagree', () => {
    const mixedReport: TopQueryExperimentReportData = {
      ...report,
      effect: { ...report.effect!, ctrNetChange: 0.0267, cvrNetChange: -0.0042 },
      overview: { ...report.overview, effect: { ...report.overview.effect!, ctrNetChange: 0.0267, cvrNetChange: -0.0042 } },
    };
    render(<ExperimentReport report={mixedReport} isMobile={false} />);
    expect(screen.getByText('+2.67%')).toBeVisible();
    expect(screen.getByText('-0.42%')).toBeVisible();
    expect(screen.queryByText('形成正向效果')).not.toBeInTheDocument();
    expect(screen.queryByText('当前未形成正向效果')).not.toBeInTheDocument();
    expect(screen.getByText('CTR 净效果')).toBeVisible();
    expect(screen.getByText('CVR 净效果')).toBeVisible();
    expect(screen.queryByText('CTR 净提升')).not.toBeInTheDocument();
    expect(screen.queryByText('CVR 净提升')).not.toBeInTheDocument();
  });

  it('renders all report sections in workbook order', () => {
    render(<ExperimentReport report={report} isMobile={false} />);
    expect(screen.getAllByRole('heading').map((node) => node.textContent)).toEqual([
      '1. 效果结论', '2. 每日组效果', '3. Query 综合效果', '4. Query 每日变化',
      '5. 治理动作与质量审计', '6. 方法说明',
    ]);
    expect(screen.queryByRole('heading', { name: '6. 口径说明' })).not.toBeInTheDocument();
  });

  it('shows the latest available data date for a running experiment', () => {
    const view = render(<ExperimentReport report={report} isMobile={false} />);
    expect(screen.getByText('截至数据日')).toBeVisible();
    expect(screen.getByText('截至数据日').closest('.ant-descriptions-item')).toHaveTextContent('2026-07-08');
    expect(screen.queryByText('观测窗口')).not.toBeInTheDocument();

    view.rerender(<ExperimentReport report={{ ...report, dataEndDate: null }} isMobile={false} />);
    expect(screen.getByText('截至数据日').closest('.ant-descriptions-item')).toHaveTextContent('-');
  });

  it('keeps the observation window for an ended experiment', () => {
    render(<ExperimentReport report={{ ...report, experiment: { ...report.experiment, status: 'ended' } }} isMobile={false} />);
    expect(screen.getByText('观测窗口')).toBeVisible();
    expect(screen.getByText('2026-07-08 至 2026-07-08')).toBeVisible();
    expect(screen.queryByText('截至数据日')).not.toBeInTheDocument();
  });

  it('hides the formal conclusion when baseline quality fails but keeps audit evidence', () => {
    const failedReport: TopQueryExperimentReportData = {
      ...report,
      baselineQuality: { ...report.baselineQuality, status: 'failed', error: 'baseline_window_incomplete', windowValid: false },
    };
    render(<ExperimentReport report={failedReport} isMobile={false} />);
    expect(screen.getByText('7日基线数据不完整')).toBeVisible();
    expect(screen.queryByText('效果结果已生成')).not.toBeInTheDocument();
    expect(screen.queryByText('CTR 正向效果天数')).not.toBeInTheDocument();
    expect(screen.queryByText('预估 Click UV 增量')).not.toBeInTheDocument();
    expect(screen.queryByRole('img', { name: '实验效果趋势图' })).not.toBeInTheDocument();
    expect(screen.queryByText('CTR +2.67% / CVR +0.42%')).not.toBeInTheDocument();
    expect(screen.getAllByText('质量未通过，不输出净效果').length).toBeGreaterThan(0);
    expect(screen.getAllByText('8.00%').length).toBeGreaterThan(0);
    expect(screen.getAllByText('连衣裙 女士').length).toBeGreaterThan(0);
  });

  it('hides the formal conclusion when report quality fails but keeps audit evidence', () => {
    const failedReport: TopQueryExperimentReportData = {
      ...report,
      quality: { ...report.quality, status: 'failed', error: 'experiment_quality_gate_failed' },
      trend: [
        report.trend[0],
        { ...report.trend[0], dataDate: '2026-07-09', quality: { status: 'failed', error: 'experiment_quality_gate_failed' }, effect: null },
      ],
    };
    render(<ExperimentReport report={failedReport} isMobile={false} />);
    expect(screen.getAllByText('样本、任务或动作数据未通过完整性门禁').length).toBeGreaterThan(0);
    expect(screen.queryByText('效果结果已生成')).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: '实验效果趋势图' })).toBeVisible();
    expect(screen.getAllByText('CTR +2.67% / CVR +0.42%')).toHaveLength(2);
    expect(screen.getAllByText('质量未通过，不输出净效果').length).toBeGreaterThan(0);
    expect(screen.getAllByText('8.00%').length).toBeGreaterThan(0);
    expect(screen.getAllByText('连衣裙 女士').length).toBeGreaterThan(0);
  });

  it('distinguishes v2 from unsupported calculation versions', () => {
    const v2View = render(<ExperimentReport report={report} isMobile={false} />);
    expect(screen.queryByText('旧版计算口径')).not.toBeInTheDocument();
    expect(screen.queryByText('不支持的计算版本')).not.toBeInTheDocument();

    v2View.rerender(<ExperimentReport report={{
      ...report,
      calculationVersion: 'top-query-ab-test-v99',
      experiment: { ...report.experiment, calculationVersion: 'top-query-ab-test-v99' },
      baselineQuality: { ...report.baselineQuality, calculationVersion: 'top-query-ab-test-v99' },
    }} isMobile={false} />);
    expect(screen.getAllByText('不支持的计算版本').length).toBeGreaterThan(0);
    expect(screen.queryByText('旧版计算口径')).not.toBeInTheDocument();
    expect(screen.queryByText('效果结果已生成')).not.toBeInTheDocument();
  });

  it('renders experiment and audit statuses in Chinese', () => {
    render(<ExperimentReport report={report} isMobile={false} />);
    expect(screen.getByText('运行中')).toBeVisible();
    expect(screen.getAllByText('已观测').length).toBeGreaterThan(0);
    expect(screen.getAllByText('无污染').length).toBeGreaterThan(0);
    expect(screen.getByText('真实分子')).toBeVisible();
    expect(screen.queryByText('running')).not.toBeInTheDocument();
    expect(screen.queryByText('observed')).not.toBeInTheDocument();
    expect(screen.queryByText('clean')).not.toBeInTheDocument();
    expect(screen.queryByText('raw')).not.toBeInTheDocument();
  });

  it('uses tables on desktop and cards on mobile', () => {
    const view = render(<ExperimentReport report={report} isMobile={false} />);
    expect(document.querySelectorAll('.experiment-report .ant-table').length).toBeGreaterThanOrEqual(3);
    expect(document.querySelector('.experiment-report .experiment-mobile-query-card')).not.toBeInTheDocument();

    view.rerender(<ExperimentReport report={report} isMobile />);
    expect(document.querySelectorAll('.experiment-report .experiment-mobile-query-card').length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText('执行超时')).toBeVisible();
  });

  it('keeps action summaries when row-level details are unavailable', () => {
    render(<ExperimentReport report={{
      ...report,
      actions: [{
        recordType: 'summary', detailAvailable: false, missingDetailReason: 'metric_without_action_details',
        dataDate: '2026-07-08', queryText: '连衣裙 女士', groupType: 'treatment',
        planned: 2, executed: 1, failed: 1, inconclusive: 0, unresolved: 0, rejected: 0, cancelled: 0,
      }],
    }} isMobile={false} />);
    expect(screen.getByText('历史动作明细不可用')).toBeVisible();
    expect(screen.getByText('计划 2 / 成功 1 / 失败 1 / 未决 0')).toBeVisible();
  });

  it('shows an explicit empty action state', () => {
    render(<ExperimentReport report={{ ...report, actions: [] }} isMobile={false} />);
    expect(screen.getByText('本实验暂无治理动作，保留质量审计结果')).toBeVisible();
  });
});
