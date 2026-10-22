import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { TopQueryExperimentQueryAnalytics } from '@/shared/types';
import ExperimentQueryTable from './ExperimentQueryTable';

const baseItem: TopQueryExperimentQueryAnalytics = {
  queryText: '基础 Query',
  groupType: 'treatment',
  sampleRank: 1,
  baselineSearchUv: 100,
  baselineCtr: 0.1,
  baselineCvr: 0.02,
  latestDataDate: '2026-07-10',
  observedStatus: 'observed',
  contaminationStatus: 'clean',
  currentSearchUv: 120,
  currentSearchPv: 300,
  currentCtr: 0.08,
  currentCvr: 0.01,
  ctrChange: -0.02,
  cvrChange: -0.01,
  dataQualityStatus: 'complete',
  groupContribution: { ctr: 0.01, cvr: 0.002 },
  netCtrContribution: 0.01,
  netCvrContribution: 0.002,
  effectDirection: 'positive',
  governanceTaskId: 1,
  governanceStatus: 'completed',
  actionCount: 1,
  tokenUsage: 10,
  durationSeconds: 2,
};

const items = [
  { ...baseItem, queryText: '服务端正向', effectDirection: 'positive' },
  { ...baseItem, queryText: '对照组负向', groupType: 'control', dataQualityStatus: 'failed', effectDirection: 'negative' },
  { ...baseItem, queryText: '效果分化', sampleRank: 2, effectDirection: 'mixed' },
] as TopQueryExperimentQueryAnalytics[];

const selectOption = (label: string, optionLabel: string) => {
  fireEvent.mouseDown(screen.getByLabelText(label));
  const option = screen.getAllByText(optionLabel).find((node) => node.closest('.ant-select-item-option'));
  fireEvent.click(option!);
};

describe('ExperimentQueryTable', () => {
  it('filters desktop rows and mobile cards by group using the same state', () => {
    const view = render(<ExperimentQueryTable items={items} isMobile={false} />);
    selectOption('分组筛选', '对照组');
    expect(screen.getByText('对照组负向')).toBeVisible();
    expect(screen.queryByText('服务端正向')).not.toBeInTheDocument();

    view.rerender(<ExperimentQueryTable items={items} isMobile />);
    expect(document.querySelectorAll('.experiment-mobile-query-card')).toHaveLength(1);
    expect(screen.getByText('对照组负向')).toBeVisible();
    expect(screen.queryByText('效果分化')).not.toBeInTheDocument();
  });

  it('filters by the server-provided effect direction without recomputing metrics', () => {
    render(<ExperimentQueryTable items={items} isMobile={false} />);
    selectOption('效果方向筛选', '正向');
    expect(screen.getByText('服务端正向')).toBeVisible();
    expect(document.querySelector('[data-row-key="control-对照组负向"]')).not.toBeInTheDocument();
    expect(document.querySelector('[data-row-key="treatment-效果分化"]')).not.toBeInTheDocument();
  });

  it('filters by query quality status', () => {
    render(<ExperimentQueryTable items={items} isMobile />);
    selectOption('质量状态筛选', '质量异常');
    expect(screen.getByText('对照组负向')).toBeVisible();
    expect(screen.queryByText('服务端正向')).not.toBeInTheDocument();
    expect(document.querySelectorAll('.experiment-mobile-query-card')).toHaveLength(1);
  });

  it('uses operator-readable labels for neutral and undetermined directions', () => {
    render(<ExperimentQueryTable items={[
      { ...baseItem, queryText: '基本持平 Query', effectDirection: 'neutral' },
      { ...baseItem, queryText: '不可判定 Query', effectDirection: 'undetermined' },
    ]} isMobile={false} />);

    expect(screen.getByText('基本持平')).toBeVisible();
    expect(screen.getByText('不可判定')).toBeVisible();
    selectOption('效果方向筛选', '基本持平');
    expect(screen.getByText('基本持平 Query')).toBeVisible();
    expect(screen.queryByText('不可判定 Query')).not.toBeInTheDocument();
  });

  it('shows an explicit empty state when combined filters have no match', () => {
    render(<ExperimentQueryTable items={items} isMobile={false} />);
    selectOption('分组筛选', '对照组');
    selectOption('效果方向筛选', '正向');
    expect(screen.getByText('当前筛选无 Query 效果数据')).toBeVisible();
  });

  it('renders empty mobile and incomplete analytics safely', () => {
    render(<ExperimentQueryTable items={[]} isMobile />);
    expect(screen.getByText('暂无 Query 效果数据')).toBeVisible();
  });
});
