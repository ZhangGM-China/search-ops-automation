import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import BrandMetricDetailDrawer from './BrandMetricDetailDrawer';
import { drawerMetricOptions } from '@/shared/pageConfig/definitions';

vi.mock('@ant-design/charts', () => ({
  DualAxes: () => <div data-testid="mock-dual-axes-chart" />,
  Line: () => <div data-testid="mock-line-chart" />,
}));

describe('BrandMetricDetailDrawer', () => {
  it('renders fallback trend and closes', () => {
    const onClose = vi.fn();
    render(<BrandMetricDetailDrawer brandName="Atlas" open onClose={onClose} trend={[]} stats={{ brandUv: 10, brandPv: 20, clickUv: 3, addToCartUv: 2, purchaseUv: 1, ctr: 0.3, atc: 0.2, cvr: 0.1 } as never} anchorDate="2026-01-01" />);
    expect(screen.getByText('数据明细')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalled();
  });
  it('shows skeleton while loading', () => {
    render(<BrandMetricDetailDrawer brandName="Atlas" open onClose={vi.fn()} trend={[]} loading />);
    expect(document.querySelector('.ant-skeleton')).toBeInTheDocument();
  });

  it('supports metric filtering, locked metrics and invalid trend rows', () => {
    render(<BrandMetricDetailDrawer brandName="Atlas" open onClose={vi.fn()} anchorDate="2026-01-15" availableMetrics={['ctr', 'cvr']} lockMetrics trend={[{ date: 'bad', ctr: null, cvr: 0.2 }, { date: '2026-01-10', ctr: 0.1, cvr: 0.2 }] as never} />);
    fireEvent.click(screen.getByRole('radio', { name: '按周' }));
    expect(screen.getByRole('cell', { name: '2026-01-04 周' })).toBeInTheDocument();
    expect(screen.queryByRole('cell', { name: 'bad' })).not.toBeInTheDocument();
    expect(screen.queryByText('维度筛选')).not.toBeInTheDocument();
  });

  it('renders empty and month-granularity trends without stats', () => {
    render(<BrandMetricDetailDrawer brandName="Harbor" open onClose={vi.fn()} anchorDate="invalid" trend={[]} stats={null} />);
    fireEvent.click(screen.getByRole('radio', { name: '按月' }));
    expect(screen.queryByRole('row', { name: /2026/ })).not.toBeInTheDocument();
  });

  it.each([['按周', '2026-01-04 周'], ['按月', '2026-01'], ['按年', '2026']])('aggregates same-period rows when selecting %s and ignores out-of-range data', (label, bucket) => {
    render(<BrandMetricDetailDrawer brandName="Atlas" open onClose={vi.fn()} anchorDate="2026-01-15" trend={[
      { date: '01-05', uv: 10, clickUv: 2, addToCartUv: 1, purchaseUv: 1, ctr: 0.2, atc: 0.1, cvr: 0.1 },
      { date: '2026-01-06', uv: 20, clickUv: 6, addToCartUv: 3, purchaseUv: 1, ctr: 0.4, atc: 0.3, cvr: 0.1 },
      { date: '1990-01-01', uv: 999 },
    ] as never} availableMetrics={['uv', 'ctr']} lockMetrics />);
    fireEvent.click(screen.getByRole('radio', { name: label }));
    const row = screen.getByRole('cell', { name: bucket }).closest('tr')!;
    expect(within(row).getByRole('cell', { name: '30' })).toBeInTheDocument();
    expect(within(row).getByRole('cell', { name: '30.00%' })).toBeInTheDocument();
    expect(screen.queryByRole('cell', { name: '999' })).not.toBeInTheDocument();
  });

  it('retains explicit zero metrics when a bucket contains absent measurements', () => {
    render(<BrandMetricDetailDrawer brandName="Atlas" open onClose={vi.fn()} anchorDate="2026-01-15" trend={[{ date: '2026-01-05' }, { date: '2026-01-06' }] as never} availableMetrics={['uv', 'ctr']} lockMetrics />);
    fireEvent.click(screen.getByRole('radio', { name: '按月' }));
    const row = screen.getByRole('cell', { name: '2026-01' }).closest('tr')!;
    expect(within(row).getByRole('cell', { name: '0' })).toBeInTheDocument();
    expect(within(row).getByRole('cell', { name: '0.00%' })).toBeInTheDocument();
  });

  it('shows every persisted daily snapshot by default, including dates after the task anchor', () => {
    render(<BrandMetricDetailDrawer
      brandName="Atlas"
      open
      onClose={vi.fn()}
      anchorDate="2026-09-07"
      trend={[
        { date: '2026-09-05', uv: 79, pv: 188 },
        { date: '2026-09-06', uv: 113, pv: 286 },
        { date: '2026-09-07', uv: 101, pv: 294 },
        { date: '2026-09-08', uv: 34, pv: 84 },
      ] as never}
      availableMetrics={['uv', 'pv']}
      lockMetrics
    />);

    expect(screen.getByRole('cell', { name: '2026-09-05' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '2026-09-08' })).toBeInTheDocument();
  });


  it('removes the redundant dimension filter and exposes the drawer metric tips', () => {
    render(<BrandMetricDetailDrawer
      brandName="Atlas"
      open
      onClose={vi.fn()}
      trend={[]}
      stats={{} as never}
      summaryMetrics={[
        { key: 'token_usage', label: 'Token 消耗', value: '139.0K', tip: '本批次模型调用消耗的 Token 总量。' },
        { key: 'duration_seconds', label: '耗时', value: '7m 30s', tip: '本批次从开始到完成的总耗时。' },
        { key: 'started_at', label: '开始时间', value: '06:25:17', tip: '本批次任务开始执行的时间。' },
        { key: 'completed_at', label: '完成时间', value: '06:32:47', tip: '本批次任务完成或终止的时间。' },
      ]}
    />);
    expect(screen.queryByText('维度筛选')).not.toBeInTheDocument();
    expect(screen.getByLabelText('耗时说明')).toBeInTheDocument();
    expect(screen.getByLabelText('Token 消耗说明')).toBeInTheDocument();
    expect(screen.getByText('139.0K')).toBeInTheDocument();
    expect(screen.getByText('7m 30s')).toBeInTheDocument();
    const metrics = Array.from(document.querySelectorAll('.ops-diagnosis-metric > span')).map((node) => node.textContent?.trim());
    expect(metrics.slice(0, 3)).toEqual(['Token 消耗', '耗时', '开始时间']);
    expect(screen.queryByText('召回商品数')).not.toBeInTheDocument();
  });

  it('renders every configured drawer metric with a tip from the metric dictionary', () => {
    const metrics = drawerMetricOptions.map((option) => ({ key: option.value, label: option.label, value: option.value, tip: option.tip }));
    render(<BrandMetricDetailDrawer
      brandName="Atlas"
      open
      onClose={vi.fn()}
      trend={[]}
      stats={{} as never}
      summaryMetrics={metrics}
    />);
    expect(drawerMetricOptions.every((option) => Boolean(option.tip?.trim()))).toBe(true);
    metrics.forEach((metric) => {
      expect(screen.getByLabelText(`${metric.label}说明`)).toBeInTheDocument();
    });
  });

  it('renders the renamed execution metrics and fixed table structure', () => {
    render(<BrandMetricDetailDrawer
      brandName="Atlas"
      open
      onClose={vi.fn()}
      trend={[{ date: '2026-08-10', uv: 10, pv: 20 }] as never}
      summaryMetrics={[
        { key: 'error_products', label: '错误商品', value: '53/147', tip: '数字格式为“不相关商品数/应检商品总数”（含送检商品与历史复用商品）。' },
        { key: 'top_query_limit', label: 'Top Query', value: 500, tip: '系统配置的每日最多治理 Query 数，是 Top Query 取词上限。' },
        { key: 'rapid_pv_growth_query_count', label: '快速增长', value: 12, tip: '命中快速增长规则并被选中进入本批次治理的 Query 数量。' },
        { key: 'relevance_accuracy', label: '商品准确率', value: '63.9%', tip: '(应检商品数 - 不相关商品数) ÷ 应检商品数（含送检商品与历史复用商品）。' },
        { key: 'preference_adjusted_query_count', label: '重排序 Query 数', value: 43, tip: '产生重排序策略调整建议的 Query 数量。' },
      ]}
    />);
    expect(screen.getByText('53/147')).toBeInTheDocument();
    expect(screen.getByText('错误商品')).toBeInTheDocument();
    expect(screen.getByText('Top Query')).toBeInTheDocument();
    expect(screen.getByText('快速增长')).toBeInTheDocument();
    expect(screen.queryByText('不相关商品数')).not.toBeInTheDocument();
    expect(screen.queryByText('高 PV Query')).not.toBeInTheDocument();
    const metricLabels = Array.from(document.querySelectorAll('.ops-diagnosis-metric__label > span:first-child')).map((node) => node.textContent?.trim());
    expect(metricLabels.slice(0, 4)).toEqual(['错误商品', 'Top Query', '快速增长', '商品准确率']);
    expect(screen.getByText('重排序 Query 数')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '日期' })).toHaveClass('ant-table-cell-fix-start');
    expect(document.querySelector('.ops-trend-table table')).toHaveStyle({ width: 'max-content' });
    expect(document.querySelector('.ops-trend-table')).toBeInTheDocument();
  });
});
