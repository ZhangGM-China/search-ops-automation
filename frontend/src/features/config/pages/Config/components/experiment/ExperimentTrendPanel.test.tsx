import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ExperimentTrendPanel from './ExperimentTrendPanel';

vi.mock('@ant-design/charts', () => ({
  Line: (props: { data?: unknown; encode?: unknown; scale?: { y?: { domain?: unknown } }; annotations?: unknown; line?: { slider?: unknown } }) => (
    <div
      data-testid="ant-design-line-chart"
      data-chart-data={JSON.stringify(props.data)}
      data-encode={JSON.stringify(props.encode)}
      data-y-domain={JSON.stringify(props.scale?.y?.domain)}
      data-annotations={JSON.stringify(props.annotations)}
      data-slider={JSON.stringify(props.line?.slider)}
    />
  ),
}));

const makeItem = (dataDate: string, effect: { ctrNetChange: number | null; cvrNetChange: number | null } | null, status: 'complete' | 'failed' = 'complete') => ({
  dataDate,
  quality: { status },
  groups: {},
  effect,
  operations: { governanceCoverage: 1, actionSuccessRate: 1, plannedActions: 1, executedActions: 1, failedActions: 0, tokenUsage: 10, durationSeconds: 2 },
});

const renderTrend = (items: ReturnType<typeof makeItem>[], qualityPassed = true) => render(
  <ExperimentTrendPanel trend={{ experimentId: 1, brand: 'atlas', items }} qualityPassed={qualityPassed} />,
);

describe('ExperimentTrendPanel', () => {
  it('renders the backend net effect points with the Ant Design line chart', () => {
    renderTrend([makeItem('2026-07-09', { ctrNetChange: 0.01, cvrNetChange: 0.002 }), makeItem('2026-07-10', { ctrNetChange: 0.015, cvrNetChange: 0.003 })]);
    expect(screen.getByRole('img', { name: '实验效果趋势图' })).toBeInTheDocument();
    expect(screen.getByTestId('ant-design-line-chart')).toBeInTheDocument();
    expect(screen.getByText('CTR 净变化')).toBeInTheDocument();
    expect(screen.getByText('CVR 净变化')).toBeInTheDocument();
    expect(JSON.parse(screen.getByTestId('ant-design-line-chart').getAttribute('data-encode') || '{}')).toEqual({ x: 'date', y: 'value', color: 'metric' });
  });

  it('keeps zero inside the y-axis domain and renders a dashed baseline for positive data', () => {
    renderTrend([makeItem('2026-07-09', { ctrNetChange: 0.01, cvrNetChange: 0.002 }), makeItem('2026-07-10', { ctrNetChange: 0.015, cvrNetChange: 0.003 })]);
    const chart = screen.getByTestId('ant-design-line-chart');
    expect(JSON.parse(chart.getAttribute('data-y-domain') || '[]')[0]).toBeLessThan(0);
    expect(JSON.parse(chart.getAttribute('data-y-domain') || '[]')[1]).toBeGreaterThan(0);
    expect(JSON.parse(chart.getAttribute('data-annotations') || '[]')).toEqual([
      { type: 'lineY', encode: { y: 0 }, style: { lineDash: [6, 5], stroke: '#94a3b8' } },
    ]);
  });

  it('keeps zero inside the y-axis domain for negative data', () => {
    renderTrend([makeItem('2026-07-09', { ctrNetChange: -0.01, cvrNetChange: -0.002 }), makeItem('2026-07-10', { ctrNetChange: -0.015, cvrNetChange: -0.003 })]);
    const domain = JSON.parse(screen.getByTestId('ant-design-line-chart').getAttribute('data-y-domain') || '[]');
    expect(domain[0]).toBeLessThan(0);
    expect(domain[1]).toBeGreaterThan(0);
  });

  it('keeps a valid metric when the paired metric is missing and does not fabricate zero points', () => {
    renderTrend([makeItem('2026-07-09', { ctrNetChange: null, cvrNetChange: 0.002 }), makeItem('2026-07-10', { ctrNetChange: Number.NaN, cvrNetChange: null })]);
    const data = JSON.parse(screen.getByTestId('ant-design-line-chart').getAttribute('data-chart-data') || '[]');
    expect(data.some((point: { metric: string; value: number | null }) => point.metric === 'CVR 净变化' && point.value === 0.2)).toBe(true);
    expect(data.filter((point: { metric: string }) => point.metric === 'CTR 净变化')).toHaveLength(2);
    expect(data.every((point: { metric: string; value: number | null }) => point.metric !== 'CTR 净变化' || point.value === null)).toBe(true);
  });

  it('keeps failed report dates as null points so the chart does not connect across gaps', () => {
    renderTrend([makeItem('2026-07-09', { ctrNetChange: 0.01, cvrNetChange: 0.002 }), makeItem('2026-07-10', null, 'failed'), makeItem('2026-07-11', { ctrNetChange: 0.015, cvrNetChange: null })]);
    const data = JSON.parse(screen.getByTestId('ant-design-line-chart').getAttribute('data-chart-data') || '[]');
    expect(data.map((point: { date: string }) => point.date)).toEqual(['2026-07-09', '2026-07-09', '2026-07-10', '2026-07-10', '2026-07-11', '2026-07-11']);
    expect(data.find((point: { date: string; metric: string }) => point.date === '2026-07-10' && point.metric === 'CTR 净变化').value).toBeNull();
    expect(data.find((point: { date: string; metric: string }) => point.date === '2026-07-11' && point.metric === 'CTR 净变化').value).toBe(1.5);
  });

  it('places all-zero points on the zero baseline domain', () => {
    renderTrend([makeItem('2026-07-09', { ctrNetChange: 0, cvrNetChange: 0 })]);
    const domain = JSON.parse(screen.getByTestId('ant-design-line-chart').getAttribute('data-y-domain') || '[]');
    expect(domain[0]).toBeLessThan(0);
    expect(domain[1]).toBeGreaterThan(0);
  });

  it('uses the chart library slider for dense report dates', () => {
    renderTrend(Array.from({ length: 13 }, (_, index) => makeItem(`2026-07-${String(index + 1).padStart(2, '0')}`, { ctrNetChange: 0.01, cvrNetChange: 0.002 })));
    expect(screen.getByTestId('ant-design-line-chart')).toHaveAttribute('data-slider');
  });
});
