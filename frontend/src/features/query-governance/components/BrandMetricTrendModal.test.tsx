import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps } from 'react';
import dayjs from 'dayjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { dashboardApi } from '@/shared/api';
import BrandMetricTrendModal from './BrandMetricTrendModal';

vi.mock('@ant-design/charts', () => ({
  Line: ({ data }: { data: Array<{ period: string; value: number | null }> }) => (
    <div data-testid="mock-line-chart">{JSON.stringify(data)}</div>
  ),
}));

vi.mock('@/shared/api', () => ({
  dashboardApi: { getTopQueryBrandMetricTrend: vi.fn() },
}));

const anchor = dayjs('2026-09-28');

const trendDay = (date: string, metrics: Record<string, number | null> | null) => ({
  date,
  batchId: metrics ? `batch-${date}` : null,
  metrics,
});

const chartPoints = () => JSON.parse(screen.getByTestId('mock-line-chart').textContent ?? '[]') as Array<{ period: string; value: number | null }>;

const renderModal = (metricKey: string, overrides: Partial<ComponentProps<typeof BrandMetricTrendModal>> = {}) => render(
  <BrandMetricTrendModal
    open
    brand="atlas"
    brandName="Atlas"
    metricKey={metricKey}
    metricLabel="通过率"
    anchorDate="2026-09-28"
    onClose={() => {}}
    {...overrides}
  />,
);

describe('Brand metric trend modal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('requests the 180-day snapshot window and defaults to the last 7 days by day', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandMetricTrend).mockResolvedValue({ data: { brand: 'atlas', days: [] } } as never);

    renderModal('pass_rate');

    await waitFor(() => expect(dashboardApi.getTopQueryBrandMetricTrend).toHaveBeenCalledTimes(1));
    expect(dashboardApi.getTopQueryBrandMetricTrend).toHaveBeenCalledWith(
      'atlas',
      { startDate: anchor.subtract(179, 'day').format('YYYY-MM-DD'), endDate: '2026-09-28' },
      { signal: expect.any(AbortSignal) },
    );
    expect(screen.getByRole('radio', { name: '按天' })).toBeChecked();
    expect(screen.getByText('Atlas · 通过率趋势')).toBeInTheDocument();
  });

  it('renders the last 7 daily buckets and re-buckets the same payload by week', async () => {
    const days = Array.from({ length: 10 }, (_, index) => trendDay(
      anchor.subtract(9 - index, 'day').format('YYYY-MM-DD'),
      { query_count: 2 },
    ));
    vi.mocked(dashboardApi.getTopQueryBrandMetricTrend).mockResolvedValue({ data: { brand: 'atlas', days } } as never);

    renderModal('query_count');

    await waitFor(() => expect(screen.getByTestId('mock-line-chart')).toBeInTheDocument());
    expect(chartPoints().map((point) => point.period)).toHaveLength(7);
    expect(chartPoints()[0].period).toBe(anchor.subtract(6, 'day').format('YYYY-MM-DD'));
    expect(chartPoints()[0].value).toBe(2);

    fireEvent.click(screen.getByRole('radio', { name: '按周' }));

    await waitFor(() => expect(chartPoints().every((point) => point.period.endsWith(' 周'))).toBe(true));
    expect(chartPoints().reduce((total, point) => total + (point.value ?? 0), 0)).toBe(20);
  });

  it('rebuilds ratio buckets from numerator and denominator sums instead of averaging daily rates', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandMetricTrend).mockResolvedValue({ data: { brand: 'atlas', days: [
      trendDay('2026-09-27', { query_count: 2, pass_count: 1 }),
      trendDay('2026-09-28', { query_count: 4, pass_count: 3 }),
    ] } } as never);

    renderModal('pass_rate');

    await waitFor(() => expect(screen.getByTestId('mock-line-chart')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('radio', { name: '按周' }));

    await waitFor(() => expect(chartPoints()).toHaveLength(1));
    expect(chartPoints()[0].value).toBeCloseTo(66.6667, 3);
    expect(chartPoints()[0].value).not.toBeCloseTo(62.5, 3);
  });

  it('re-aggregates relevance detection accuracy from blacklisted and pair sums', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandMetricTrend).mockResolvedValue({ data: { brand: 'atlas', days: [
      trendDay('2026-09-27', { irrelevant_product_pair_count: 4, blacklisted_product_count: 4 }),
      trendDay('2026-09-28', { irrelevant_product_pair_count: 6, blacklisted_product_count: 3 }),
    ] } } as never);

    renderModal('relevance_detection_accuracy', { metricLabel: '相关性检测准确率' });

    await waitFor(() => expect(screen.getByTestId('mock-line-chart')).toBeInTheDocument());
    expect(chartPoints().map((point) => point.value)).toEqual([100, 50]);

    fireEvent.click(screen.getByRole('radio', { name: '按周' }));

    await waitFor(() => expect(chartPoints()).toHaveLength(1));
    // (4+3)/(4+6) = 70%，不是日均 (100%+50%)/2 = 75%。
    expect(chartPoints()[0].value).toBeCloseTo(70, 3);
    expect(chartPoints()[0].value).not.toBeCloseTo(75, 3);
  });

  it('keeps days without a summary as gaps instead of fake zeroes', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandMetricTrend).mockResolvedValue({ data: { brand: 'atlas', days: [
      trendDay('2026-09-27', { query_count: 5 }),
      trendDay('2026-09-28', null),
    ] } } as never);

    renderModal('query_count');

    await waitFor(() => expect(screen.getByTestId('mock-line-chart')).toBeInTheDocument());
    expect(chartPoints()).toEqual([
      { period: '2026-09-27', value: 5 },
      { period: '2026-09-28', value: null },
    ]);
  });

  it('shows the empty trend state when every returned day is a gap', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandMetricTrend).mockResolvedValue({ data: { brand: 'atlas', days: [
      trendDay('2026-09-27', null),
      trendDay('2026-09-28', null),
    ] } } as never);

    renderModal('query_count');

    expect(await screen.findByText('暂无趋势数据')).toBeInTheDocument();
    expect(screen.queryByTestId('mock-line-chart')).not.toBeInTheDocument();
  });

  it('surfaces the request failure instead of an empty chart', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandMetricTrend).mockRejectedValue({ response: { data: { detail: '指标趋势快照读取失败' } } });

    renderModal('query_count');

    expect(await screen.findByText('指标趋势快照读取失败')).toBeInTheDocument();
    expect(screen.queryByTestId('mock-line-chart')).not.toBeInTheDocument();
  });
});