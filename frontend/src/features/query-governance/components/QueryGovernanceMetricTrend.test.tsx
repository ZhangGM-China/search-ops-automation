import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import QueryGovernanceMetricTrend from './QueryGovernanceMetricTrend';

vi.mock('@ant-design/charts', () => ({
  DualAxes: (props: { data?: unknown; children?: Array<{ slider?: unknown }> }) => (
    <div
      data-testid="ant-design-dual-axes-chart"
      data-chart-data={JSON.stringify(props.data)}
      data-slider={JSON.stringify(props.children?.find((child) => child.slider)?.slider)}
      role="img"
      aria-label="Ant Design 双轴趋势图"
    />
  ),
}));

describe('QueryGovernanceMetricTrend', () => {
  it('renders empty state when there is no trend data', () => {
    render(<QueryGovernanceMetricTrend data={[]} query="连衣裙" />);

    expect(screen.getByText('暂无趋势数据')).toBeInTheDocument();
  });

  it('renders the Ant Design dual-axes chart with dual axes and tooltip configuration', () => {
    render(
      <QueryGovernanceMetricTrend
        query="连衣裙"
        metrics={['pv', 'ctr', 'cvr']}
        data={[
          { date: '2026-07-01', pv: 300, ctr: 0.1, cvr: 0.02 },
          { date: '2026-07-02', pv: 320, ctr: 0.12, cvr: 0.025 },
          { date: '2026-07-03', pv: 280, ctr: 0.09, cvr: 0.018 },
        ]}
      />,
    );

    expect(screen.getByRole('region', { name: '连衣裙 3项指标数据变化' })).toBeInTheDocument();
    const chart = screen.getByTestId('ant-design-dual-axes-chart');
    expect(chart).toBeInTheDocument();
    expect(chart).toHaveAttribute('aria-label', 'Ant Design 双轴趋势图');
    expect(chart).not.toHaveAttribute('data-slider');
    expect(JSON.parse(chart.getAttribute('data-chart-data') || '[]')).toHaveLength(2);
  });

  it('lets users toggle a metric from the legend and trims dense date labels', () => {
    const onToggleMetric = vi.fn();
    const data = Array.from({ length: 30 }, (_, index) => ({
      date: `2026-08-${String(index + 1).padStart(2, '0')}`,
      pv: 300 + index,
      ctr: 0.1 + index / 1000,
      cvr: 0.01,
    }));
    render(<QueryGovernanceMetricTrend query="连衣裙" metrics={['pv', 'ctr']} data={data} onToggleMetric={onToggleMetric} />);

    const pvLegend = screen.getByRole('button', { name: '切换 PV' });
    expect(pvLegend).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(pvLegend);
    expect(onToggleMetric).toHaveBeenCalledWith('pv');
    expect(JSON.parse(screen.getByTestId('ant-design-dual-axes-chart').getAttribute('data-chart-data') || '[]')).toHaveLength(2);
  });

  it('uses the chart library slider for dense trend data instead of horizontal navigation controls', () => {
    const data = Array.from({ length: 24 }, (_, index) => ({
      date: `2026-08-${String(index + 1).padStart(2, '0')}`,
      pv: index + 1,
      ctr: 0.1,
      cvr: 0.01,
    }));
    render(<QueryGovernanceMetricTrend query="连衣裙" metrics={['pv']} data={data} />);
    const chart = screen.getByTestId('ant-design-dual-axes-chart');
    expect(chart).toBeInTheDocument();
    expect(chart).toHaveAttribute('data-slider');
    expect(screen.queryByRole('button', { name: '向左查看趋势' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '向右查看趋势' })).not.toBeInTheDocument();
  });

  it('turns a hidden legend swatch gray and keeps the toggle interactive', () => {
    render(<QueryGovernanceMetricTrend query="连衣裙" metrics={['pv']} data={[{ date: '2026-08-01', pv: 1, ctr: 0.1, cvr: 0.01 }]} onToggleMetric={vi.fn()} />);
    const ctrLegend = screen.getByRole('button', { name: '切换 CTR' });
    expect(ctrLegend).toHaveClass('ops-trend-chart__legend-item--hidden');
    expect(within(ctrLegend).getByTestId('trend-legend-swatch-ctr')).toHaveClass('ops-trend-chart__legend-swatch--hidden');
  });
});
