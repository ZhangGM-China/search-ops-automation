import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { TopQueryExperimentDailyMetric } from '@/shared/types';
import ExperimentQueryDailyTable from './ExperimentQueryDailyTable';

const item: TopQueryExperimentDailyMetric = {
  dataDate: '2026-07-08', queryText: '连衣裙', groupType: 'treatment',
  observedStatus: 'observed', contaminationStatus: 'clean', searchUv: 100, searchPv: 200,
  clickUv: 8, purchaseUv: 2, ctr: 0.08, cvr: 0.02, numeratorSource: 'raw',
  clickNumeratorSource: 'raw', purchaseNumeratorSource: 'missing',
  clickNumeratorStatus: 'complete', purchaseNumeratorStatus: 'failed',
  dataQualityStatus: 'partial', includedInEffect: false, actionCount: 0,
  autoActionCount: 0, manualActionCount: 0, tokenUsage: 0, durationSeconds: 0,
};

describe('ExperimentQueryDailyTable', () => {
  it('uses centralized Chinese labels for observation, contamination and numerator evidence', () => {
    render(<ExperimentQueryDailyTable items={[item]} isMobile={false} />);
    expect(screen.getByText('已观测')).toBeVisible();
    expect(screen.getByText('无污染')).toBeVisible();
    expect(screen.getByText('真实分子')).toBeVisible();
    expect(screen.getByText('点击完整 / 购买异常')).toBeVisible();
    expect(screen.getByText('部分可用')).toBeVisible();
    for (const raw of ['observed', 'clean', 'raw', 'partial']) expect(screen.queryByText(raw)).not.toBeInTheDocument();
  });

  it('keeps unknown status auditable without presenting raw English alone', () => {
    render(<ExperimentQueryDailyTable items={[{
      ...item,
      groupType: 'future_group' as never,
      observedStatus: 'future_observed_state',
      contaminationStatus: 'future_contamination_state',
      numeratorSource: 'future_numerator_source',
      clickNumeratorStatus: 'future_click_status',
      purchaseNumeratorStatus: 'future_purchase_status',
    }]} isMobile />);
    expect(screen.getByText('未知组别（future_group）')).toBeVisible();
    expect(screen.getByText('未知状态（future_observed_state）')).toBeVisible();
    expect(screen.getByText('未知状态（future_contamination_state）')).toBeVisible();
    expect(screen.getByText('未知来源（future_numerator_source）')).toBeVisible();
    expect(screen.getByText('点击未知状态（future_click_status） / 购买未知状态（future_purchase_status）')).toBeVisible();
    expect(screen.queryByText('对照组')).not.toBeInTheDocument();
    expect(screen.queryByText('future_observed_state')).not.toBeInTheDocument();
  });

  it('maps all legal observation and numerator DTO values without exposing raw codes', () => {
    const values = [
      ['data_quality_failed', 'partial'],
      ['not_synced', 'invalid'],
      ['not_observed', 'mixed'],
      ['unobserved', 'partial'],
      ['missing', 'missing'],
    ] as const;
    render(<ExperimentQueryDailyTable items={values.map(([observedStatus, numeratorSource], index) => ({
      ...item,
      dataDate: `2026-07-${String(index + 8).padStart(2, '0')}`,
      queryText: `query-${index}`,
      observedStatus,
      numeratorSource,
    }))} isMobile={false} />);
    expect(screen.getByText('数据异常')).toBeVisible();
    expect(screen.getByText('未同步')).toBeVisible();
    expect(screen.getAllByText('未观测')).toHaveLength(3);
    expect(screen.getAllByText('部分分子')).toHaveLength(2);
    expect(screen.getByText('分子无效')).toBeVisible();
    expect(screen.getByText('混合来源')).toBeVisible();
    expect(screen.getByText('分子缺失')).toBeVisible();
    for (const raw of values.flat()) expect(screen.queryByText(raw)).not.toBeInTheDocument();
  });

  it('does not mislabel a corrupted group as control on desktop or mobile', () => {
    const corrupted = { ...item, groupType: 'broken_group' as never };
    const view = render(<ExperimentQueryDailyTable items={[corrupted]} isMobile={false} />);
    expect(screen.getByText('未知组别（broken_group）')).toBeVisible();
    expect(screen.queryByText('对照组')).not.toBeInTheDocument();

    view.rerender(<ExperimentQueryDailyTable items={[corrupted]} isMobile />);
    expect(screen.getByText('未知组别（broken_group）')).toBeVisible();
    expect(screen.queryByText('对照组')).not.toBeInTheDocument();
  });
});
