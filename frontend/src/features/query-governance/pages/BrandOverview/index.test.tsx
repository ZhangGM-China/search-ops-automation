import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { dashboardApi } from '@/shared/api';
import BrandOverview from './index';
import { PageConfigProvider } from '@/shared/pageConfig';

vi.mock('@ant-design/charts', () => ({
  DualAxes: () => <div data-testid="mock-dual-axes-chart" />,
  Line: () => <div data-testid="mock-line-chart" />,
}));

vi.mock('@/shared/api', () => ({
  dashboardApi: { getTopQueryOverview: vi.fn(), getTopQueryBrandDetail: vi.fn(), getTopQueryBrandSummary: vi.fn(), getTopQueryBrandTrend: vi.fn(), getTopQueryBrandMetricTrend: vi.fn() },
}));

describe('Query governance brand overview', () => {
  beforeEach(() => vi.clearAllMocks());

  it('loads the lightweight summary instead of the full brand detail for operations data', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed',
      queryCount: 1, nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
      durationSeconds: 1, startedAt: null, completedAt: null,
    }] } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-07', batch: { telemetry: { tokenUsage: 10 } },
      summary: { queryCount: 1, queryStatusCounts: {}, nodeStatusCounts: {} },
      stats: { brandUv: 10, brandPv: 20, ctr: 0.1, cvr: 0.02 },
    } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandTrend).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-07', trend: [],
    } } as never);

    render(<MemoryRouter><BrandOverview /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));

    await waitFor(() => expect(dashboardApi.getTopQueryBrandSummary).toHaveBeenCalledTimes(1));
    expect(dashboardApi.getTopQueryBrandSummary).toHaveBeenCalledWith(
      'atlas',
      { date: expect.any(String) },
      { signal: expect.any(AbortSignal) },
    );
    expect(dashboardApi.getTopQueryBrandDetail).not.toHaveBeenCalled();
  });

  it('hides operations data and does not request drawer data when disabled', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed', queryCount: 1,
      nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
      durationSeconds: 1, startedAt: null, completedAt: null,
    }] } } as never);

    render(
      <PageConfigProvider loader={async () => ({
        schemaVersion: 1,
        version: 1,
        values: { 'brand_overview.card.show_operational_data': false },
        definitions: [],
      })}>
        <MemoryRouter><BrandOverview /></MemoryRouter>
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: '运营数据' })).not.toBeInTheDocument();
    expect(dashboardApi.getTopQueryBrandSummary).not.toHaveBeenCalled();
    expect(dashboardApi.getTopQueryBrandTrend).not.toHaveBeenCalled();
  });

  it('hides the global dashboard when its page configuration switch is disabled', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed', queryCount: 1,
      nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
      durationSeconds: 1, startedAt: null, completedAt: null,
    }] } } as never);

    render(
      <PageConfigProvider loader={async () => ({
        schemaVersion: 2,
        version: 1,
        values: { 'brand_overview.show_global_metrics': false },
        definitions: [],
      })}>
        <MemoryRouter><BrandOverview /></MemoryRouter>
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    expect(screen.queryByLabelText('全局数据')).not.toBeInTheDocument();
    expect(screen.getByText('Query')).toBeInTheDocument();
  });

  it('renders the configured primary metric and fills one row with configured metrics', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed', queryCount: 10,
      nodeStatusCounts: { pending: 0, running: 0, executed: 10, skipped: 0, error: 0 },
      durationSeconds: 1, startedAt: null, completedAt: null,
      telemetry: { passRate: 0.8, noRecallRate: 0.1, pendingCount: 2, relevanceAccuracy: 0.9 },
    }] } } as never);

    render(
      <PageConfigProvider loader={async () => ({
        schemaVersion: 2,
        version: 1,
        values: {
          'brand_overview.card.primary_metric': ['pass_rate'],
          'brand_overview.card.metrics': ['pass_rate', 'no_recall_rate', 'pending_count', 'relevance_accuracy'],
          'brand_overview.global_metrics': ['query_count', 'pass_rate', 'pending_count', 'relevance_accuracy'],
        },
        definitions: [],
      })}>
        <MemoryRouter><BrandOverview /></MemoryRouter>
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    expect(document.querySelector('.ops-diagnosis-score')).toHaveTextContent('80.0%通过率');
    expect(document.querySelector('.ops-diagnosis-action-grid')).toHaveStyle({ gridTemplateColumns: 'repeat(4, minmax(0, 1fr))' });
    expect(screen.getByLabelText('全局数据')).toHaveStyle({ gridTemplateColumns: 'repeat(4, minmax(0, 1fr))' });
  });

  it('aggregates global metrics from brands with data when another brand has no data', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [
      {
        brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed', queryCount: 10,
        nodeStatusCounts: { pending: 0, running: 0, executed: 10, skipped: 0, error: 0 },
        durationSeconds: 1, startedAt: null, completedAt: null,
        telemetry: { passCount: 8, passRate: 0.8, aiProcessedCount: 7, pendingCount: 2 },
      },
      {
        brand: 'meadow', batchId: '', dataDate: null, status: 'skipped', reason: 'no_data', queryCount: 0,
        nodeStatusCounts: { pending: 0, running: 0, executed: 0, skipped: 0, error: 0 },
        durationSeconds: null, startedAt: null, completedAt: null, telemetry: {},
      },
    ] } } as never);

    render(
      <PageConfigProvider loader={async () => ({
        schemaVersion: 2,
        version: 1,
        values: {
          'brand_overview.global_metrics': ['query_count', 'pass_rate', 'ai_processed_count', 'pending_count'],
        },
        definitions: [],
      })}>
        <MemoryRouter><BrandOverview /></MemoryRouter>
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    const globalMetrics = screen.getByLabelText('全局数据');
    expect(globalMetrics).toHaveTextContent('10');
    expect(globalMetrics).toHaveTextContent('80.0%');
    expect(globalMetrics).toHaveTextContent('7');
    expect(globalMetrics).toHaveTextContent('2');
    expect(globalMetrics).not.toHaveTextContent('—');
  });

  it('shows the explicit no-candidates outcome', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'summit', batchId: 'empty', dataDate: '2026-09-07', status: 'skipped',
      reason: 'no_candidates', queryCount: 0,
      nodeStatusCounts: { pending: 0, running: 0, executed: 0, skipped: 0, error: 0 },
      durationSeconds: null, startedAt: null, completedAt: null,
    }] } } as never);
    render(<MemoryRouter><BrandOverview /></MemoryRouter>);
    expect(await screen.findByText('无符合条件的 Query')).toBeInTheDocument();
    expect(screen.queryByText('正常跳过')).not.toBeInTheDocument();
    expect(screen.queryByText('已跳过')).not.toBeInTheDocument();
    expect(document.querySelector('.ant-tag')).not.toBeInTheDocument();
  });

  it('shows every brand as no data for the selected date without displaying a historical date', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: '', dataDate: null, status: 'skipped', reason: 'no_data', queryCount: 0,
      nodeStatusCounts: { pending: 0, running: 0, executed: 0, skipped: 0, error: 0 },
      durationSeconds: null, startedAt: null, completedAt: null, telemetry: {},
    }] } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-09']}><BrandOverview /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    expect(dashboardApi.getTopQueryOverview).toHaveBeenCalledWith(
      { date: '2026-09-09' },
      { signal: expect.any(AbortSignal) },
    );
    expect(screen.getByText('暂无治理记录')).toBeInTheDocument();
    expect(screen.queryByText(/本日|当日未执行|今日未执行/)).not.toBeInTheDocument();
    expect(screen.getByText('Query')).toBeInTheDocument();
    expect(screen.getAllByText('0').length).toBeGreaterThan(0);
    expect(screen.queryByText('2026-09-08')).not.toBeInTheDocument();
  });

  it('allows an empty drawer summary in the no-data state', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: '', dataDate: null, status: 'skipped', reason: 'no_data', queryCount: 0,
      nodeStatusCounts: { pending: 0, running: 0, executed: 0, skipped: 0, error: 0 },
      durationSeconds: null, startedAt: null, completedAt: null, telemetry: {},
    }] } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: null, batch: null,
      summary: { queryCount: 0, queryStatusCounts: { pending: 0, running: 0, executed: 0, skipped: 0, error: 0 }, nodeStatusCounts: { pending: 0, running: 0, executed: 0, skipped: 0, error: 0 } },
      stats: null, trend: [], queries: [],
    } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-09']}><BrandOverview /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole('button', { name: '运营数据' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));
    await waitFor(() => expect(screen.getByText('数据明细')).toBeInTheDocument());
    expect(screen.queryByText('开始时间')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('暂无趋势数据')).toBeInTheDocument());
  });

  it('opens the metric trend modal for clickable drawer metrics only', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed',
      queryCount: 2, nodeStatusCounts: { pending: 0, running: 0, executed: 2, skipped: 0, error: 0 },
      durationSeconds: 1, startedAt: '2026-09-07T01:00:00', completedAt: '2026-09-07T02:00:00',
      telemetry: { passCount: 1, passRate: 0.5 },
    }] } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-07',
      batch: { telemetry: { passCount: 1, passRate: 0.5, detectedProductCount: 10, irrelevantProductCount: 2 } },
      summary: { queryCount: 2, queryStatusCounts: {}, nodeStatusCounts: {} },
      stats: null, trend: [], queries: [],
    } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandMetricTrend).mockResolvedValue({ data: {
      brand: 'atlas', startDate: '2026-09-01', endDate: '2026-09-07',
      days: [{ date: '2026-09-07', batchId: 'batch-1', metrics: { query_count: 2, pass_count: 1 } }],
    } } as never);

    render(
      <PageConfigProvider loader={async () => ({
        schemaVersion: 2,
        version: 1,
        values: { 'brand_overview.drawer.summary_metrics': ['pass_rate', 'started_at'] },
        definitions: [],
      })}>
        <MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-07']}><BrandOverview /></MemoryRouter>
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '查看通过率趋势' })).toBeInTheDocument());

    expect(screen.getByText('开始时间')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '查看开始时间趋势' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '查看通过率趋势' }));

    await waitFor(() => expect(dashboardApi.getTopQueryBrandMetricTrend).toHaveBeenCalledWith(
      'atlas',
      { startDate: expect.any(String), endDate: '2026-09-07' },
      { signal: expect.any(AbortSignal) },
    ));
    expect(await screen.findByText('Atlas · 通过率趋势')).toBeInTheDocument();
  });

  it('renders the relevance detection accuracy drawer metric from batch telemetry', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed',
      queryCount: 2, nodeStatusCounts: { pending: 0, running: 0, executed: 2, skipped: 0, error: 0 },
      durationSeconds: 1, startedAt: '2026-09-07T01:00:00', completedAt: '2026-09-07T02:00:00',
      telemetry: { passCount: 1, passRate: 0.5 },
    }] } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-07',
      batch: { telemetry: {
        passCount: 1, passRate: 0.5, detectedProductCount: 10, irrelevantProductCount: 2,
        irrelevantProductPairCount: 6, blacklistedProductCount: 5, relevanceDetectionAccuracy: 0.8333,
      } },
      summary: { queryCount: 2, queryStatusCounts: {}, nodeStatusCounts: {} },
      stats: null, trend: [], queries: [],
    } } as never);

    render(
      <PageConfigProvider loader={async () => ({
        schemaVersion: 2,
        version: 1,
        values: { 'brand_overview.drawer.summary_metrics': ['relevance_accuracy', 'relevance_detection_accuracy'] },
        definitions: [],
      })}>
        <MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-07']}><BrandOverview /></MemoryRouter>
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));

    expect(await screen.findByText('相关性检测准确率')).toBeInTheDocument();
    expect(screen.getByText('83.3%')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看相关性检测准确率趋势' })).toBeInTheDocument();
  });

  it('restores the legacy overview card structure with current batch fields', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({
      data: {
        brands: [{
          brand: 'atlas',
          batchId: 'batch-1',
          dataDate: '2026-08-28',
          status: 'error',
          queryCount: 1,
          nodeStatusCounts: { pending: 0, running: 0, executed: 2, skipped: 1, error: 1 },
          durationSeconds: 33.6,
          startedAt: '2026-08-29T00:30:00',
          completedAt: '2026-08-29T00:42:00',
          telemetry: {
            tokenUsage: 201541,
            irrelevantQueryCount: 1,
            irrelevantProductCount: 570,
            preferenceAdjustedQueryCount: 99,
            wallClockSeconds: 720,
            productCount: 24,
            batchCount: 2,
            roundCount: 3,
            queryEndToEnd: { averageSeconds: 12, p50Seconds: 10, p95Seconds: 20 },
            independentProcessing: { averageSeconds: 5, p50Seconds: 4, p95Seconds: 8 },
            modelSeconds: 60,
            actionSeconds: 2,
            recheckSeconds: 1,
            maxConcurrency: 5,
            modelCallCount: 3,
            retryCount: 1,
            truncationCount: 1,
            imageSuccessCount: 20,
            imageFailureCount: 4,
          },
        }],
      },
    } as never);

    render(<MemoryRouter><BrandOverview /></MemoryRouter>);

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    expect(screen.getByText('品牌总览')).toBeInTheDocument();
    expect(screen.queryByText('2026-08-28')).not.toBeInTheDocument();
    expect(screen.queryByText(/监测日期与品牌数据日期分开显示/)).not.toBeInTheDocument();
    expect(screen.getByText('治理异常')).toBeInTheDocument();
    expect(screen.queryByText('异常')).not.toBeInTheDocument();
    expect(document.querySelector('.ant-tag')).not.toBeInTheDocument();
    expect(screen.getByText('Query')).toBeInTheDocument();
    expect(screen.getAllByText('1').length).toBeGreaterThan(0);
    const irrelevantMetric = screen.getByText('不相关').closest('.ops-diagnosis-action');
    expect(irrelevantMetric).toHaveTextContent('1不相关');
    expect(irrelevantMetric).not.toHaveTextContent('570');
    expect(screen.getByText('重排序')).toBeInTheDocument();
    expect(screen.queryByText('不相关商品')).not.toBeInTheDocument();
    expect(screen.queryByText('排序策略')).not.toBeInTheDocument();
    expect(screen.getByText('断码')).toBeInTheDocument();
    expect(screen.queryByText('节点执行状态')).not.toBeInTheDocument();
    expect(screen.getByText('耗时 34s / Token 201.5K')).toBeInTheDocument();
    expect(screen.queryByText(/总耗时/)).not.toBeInTheDocument();
    expect(screen.queryByText(/总 Token 数 201,541/)).not.toBeInTheDocument();
    expect(screen.queryByText(/总 Token 数/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '查看数据详情' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('选择监测日期')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '进入 Atlas 详情' })).toBeInTheDocument();
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-08-28',
      stats: { topQueryCount: 1, manualRequiredCount: 0, completedCount: 1, brandUv: 1200, brandPv: 3400, ctr: 0.12, cvr: 0.03 },
      trend: [{ date: '2026-08-28', uv: 1200, pv: 3400, ctr: 0.12, cvr: 0.03 }],
    } } as never);
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));
    await waitFor(() => expect(screen.getByText('数据明细')).toBeInTheDocument());
    expect(screen.getAllByText('搜索UV').length).toBeGreaterThan(0);
    expect(screen.getAllByText('PV').length).toBeGreaterThan(0);
    expect(screen.getAllByText('CTR').length).toBeGreaterThan(0);
    expect(screen.queryByText('执行汇总')).not.toBeInTheDocument();
    expect(screen.queryByText('Query 明细')).not.toBeInTheDocument();
  });

  it('formats long total duration with readable hour/minute/second units', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({
      data: {
        brands: [{
          brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-03', status: 'executed',
          queryCount: 1, nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
          durationSeconds: 16956.1, startedAt: null, completedAt: null,
        }],
      },
    } as never);

    render(<MemoryRouter><BrandOverview /></MemoryRouter>);

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    expect(screen.getByText('耗时 4h 42m 36s / Token -')).toBeInTheDocument();
  });

  it('keeps long durations in total hours for stable card width', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({
      data: {
        brands: [{
          brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-03', status: 'executed',
          queryCount: 1, nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
          durationSeconds: 359999, startedAt: null, completedAt: null,
        }],
      },
    } as never);

    render(<MemoryRouter><BrandOverview /></MemoryRouter>);

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: '运营数据' }).parentElement).toHaveTextContent('耗时 99h 59m 59s / Token -');
  });

  it('formats token usage with one decimal and larger units', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({
      data: {
        brands: [{
          brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-03', status: 'executed',
          queryCount: 1, nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
          durationSeconds: 1, startedAt: null, completedAt: null,
          telemetry: { tokenUsage: 1_234_567 },
        }],
      },
    } as never);

    render(<MemoryRouter><BrandOverview /></MemoryRouter>);

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: '运营数据' }).parentElement).toHaveTextContent('耗时 1s / Token 1.2M');
  });


  it('queries the selected monitoring date when moving from yesterday to today', async () => {
    vi.setSystemTime(new Date('2026-09-07T08:00:00+08:00'));
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{ brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed', queryCount: 1, nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 }, durationSeconds: 1, startedAt: null, completedAt: null }] } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-06']}><BrandOverview /></MemoryRouter>);
    await waitFor(() => expect(dashboardApi.getTopQueryOverview).toHaveBeenCalledWith(
      { date: '2026-09-06' },
      { signal: expect.any(AbortSignal) },
    ));
    const next = await screen.findByRole('button', { name: '后一天' });
    fireEvent.click(next);
    await waitFor(() => expect(dashboardApi.getTopQueryOverview).toHaveBeenLastCalledWith(
      { date: '2026-09-07' },
      { signal: expect.any(AbortSignal) },
    ));
    vi.useRealTimers();
  });

  it('aborts the stale overview request when the monitoring date changes', async () => {
    const pending: Array<{
      signal: AbortSignal;
      resolve: (value: unknown) => void;
    }> = [];
    vi.mocked(dashboardApi.getTopQueryOverview).mockImplementation((_params, options) => (
      new Promise((resolve) => {
        pending.push({ signal: options?.signal as AbortSignal, resolve });
      }) as never
    ));

    render(<MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-09']}><BrandOverview /></MemoryRouter>);
    await waitFor(() => expect(pending).toHaveLength(1));
    pending[0].resolve({ data: { brands: [{
      brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-09', status: 'executed', queryCount: 0,
      nodeStatusCounts: { pending: 0, running: 0, executed: 0, skipped: 0, error: 0 },
      durationSeconds: null, startedAt: null, completedAt: null,
    }] } });
    await waitFor(() => expect(screen.getByRole('button', { name: '前一天' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '前一天' }));
    await waitFor(() => expect(pending).toHaveLength(2));

    expect(pending[0].signal.aborted).toBe(true);
    expect(pending[1].signal.aborted).toBe(false);
    pending[1].resolve({ data: { brands: [] } });
    await waitFor(() => expect(screen.getByText('暂无 Query 治理品牌数据')).toBeInTheDocument());
  });


  it('opens the legacy business metrics drawer with PV, UV and CTR instead of execution details', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({
      data: {
        brands: [{
          brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed',
          queryCount: 1, nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
          durationSeconds: 1, startedAt: null, completedAt: null,
        }],
      },
    } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-07',
      stats: { topQueryCount: 1, manualRequiredCount: 0, completedCount: 1, brandUv: 1200, brandPv: 3400, ctr: 0.12, cvr: 0.03 },
      trend: [{ date: '2026-09-07', uv: 1200, pv: 3400, ctr: 0.12, cvr: 0.03 }],
      queries: [],
    } } as never);

    render(<MemoryRouter><BrandOverview /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));

    await waitFor(() => expect(screen.getByText('数据明细')).toBeInTheDocument());
    expect(screen.getAllByText('搜索UV').length).toBeGreaterThan(0);
    expect(screen.getAllByText('PV').length).toBeGreaterThan(0);
    expect(screen.getAllByText('CTR').length).toBeGreaterThan(0);
    expect(screen.queryByText('执行汇总')).not.toBeInTheDocument();
    expect(screen.queryByText('Query 明细')).not.toBeInTheDocument();
  });

  it('shows configured Top Query limit before rapid-growth Query count', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({
      data: {
        brands: [{
          brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed',
          queryCount: 1, nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
          durationSeconds: 1, startedAt: null, completedAt: null,
        }],
      },
    } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-07',
      batch: { telemetry: { topQueryLimit: 500, rapidPvGrowthQueryCount: 12, highPvQueryCount: 99 } },
      summary: { queryCount: 1, queryStatusCounts: {}, nodeStatusCounts: {} },
      stats: null, trend: [], queries: [],
    } } as never);

    render(<PageConfigProvider loader={async () => ({
      schemaVersion: 2, version: 1,
      values: { 'brand_overview.drawer.summary_metrics': ['top_query_limit', 'rapid_pv_growth_query_count'] },
      definitions: [],
    })}><MemoryRouter><BrandOverview /></MemoryRouter></PageConfigProvider>);
    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));

    await waitFor(() => expect(screen.getByText('Top Query')).toBeInTheDocument());
    expect(screen.getByText('500')).toBeInTheDocument();
    expect(screen.getByText('快速增长')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.queryByText('高 PV Query')).not.toBeInTheDocument();
  });

  it('loads the persisted brand trend once instead of duplicating complete trend windows', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({
      data: {
        brands: [{
          brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-08', status: 'executed',
          queryCount: 1, nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
          durationSeconds: 1, startedAt: null, completedAt: null,
        }],
      },
    } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-08',
      stats: { topQueryCount: 1, manualRequiredCount: 0, completedCount: 1, brandUv: 9429, brandPv: 26381, ctr: 0.756, cvr: 0.0068 },
      trend: [], queries: [],
    } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandTrend).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-08', trend: [
        { date: '2026-08-10', uv: 11659, pv: 44827, clickUv: 7436, addToCartUv: 2644, purchaseUv: 182, ctr: 0.6378, atc: 0.2268, cvr: 0.0156 },
        { date: '2026-09-08', uv: 9429, pv: 26381, clickUv: 7128, addToCartUv: 2509, purchaseUv: 64, ctr: 0.756, atc: 0.2661, cvr: 0.0068 },
      ],
    } } as never);

    render(<MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-08']}><BrandOverview /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));

    await waitFor(() => expect(dashboardApi.getTopQueryBrandTrend).toHaveBeenCalledTimes(1));
    expect(dashboardApi.getTopQueryBrandTrend).toHaveBeenCalledWith('atlas', { date: '2026-09-08' }, { signal: expect.any(AbortSignal) });
    await waitFor(() => expect(screen.getByText('11,659')).toBeInTheDocument());
    expect(screen.getAllByText('11,659')).toHaveLength(1);
  });

  it('keeps the operations drawer anchored to the selected monitoring date', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({
      data: {
        brands: [{
          brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-08', status: 'executed',
          queryCount: 1, nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
          durationSeconds: 1, startedAt: null, completedAt: null,
        }],
      },
    } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-08',
      stats: { topQueryCount: 1, manualRequiredCount: 0, completedCount: 1, brandUv: 9429, brandPv: 26381, ctr: 0.756, cvr: 0.0068 },
      trend: [{ date: '2026-09-08', uv: 9429, pv: 26381, ctr: 0.756, cvr: 0.0068 }], queries: [],
    } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandTrend).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-08',
      trend: [{ date: '2026-09-08', uv: 9429, pv: 26381, ctr: 0.756, cvr: 0.0068 }],
    } } as never);

    render(<MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-09']}><BrandOverview /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));

    await waitFor(() => expect(screen.getByText('数据明细')).toBeInTheDocument());
    const rangeInputs = Array.from(document.querySelectorAll<HTMLInputElement>('.ant-picker-range input'));
    expect(rangeInputs.at(-1)?.value).toBe('2026-09-09');
  });

  it('renders error_products using inspectedProductCount or detected plus reused', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed',
      queryCount: 300, nodeStatusCounts: { pending: 0, running: 0, executed: 300, skipped: 0, error: 0 },
      durationSeconds: 1, startedAt: '2026-09-07T01:00:00', completedAt: '2026-09-07T02:00:00',
      telemetry: {
        passCount: 270,
        passRate: 0.9,
        inspectedProductCount: 662,
        detectedProductCount: 200,
        reusedProductCount: 462,
        irrelevantProductCount: 131,
      },
    }] } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-07',
      batch: {
        telemetry: {
          inspectedProductCount: 662,
          detectedProductCount: 200,
          reusedProductCount: 462,
          irrelevantProductCount: 131,
        },
      },
      summary: { queryCount: 300, queryStatusCounts: {}, nodeStatusCounts: {} },
      stats: null, trend: [], queries: [],
    } } as never);

    render(
      <PageConfigProvider loader={async () => ({
        schemaVersion: 2,
        version: 1,
        values: { 'brand_overview.drawer.summary_metrics': ['error_products'] },
        definitions: [],
      })}>
        <MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-07']}><BrandOverview /></MemoryRouter>
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));

    expect(await screen.findByText('错误商品')).toBeInTheDocument();
    expect(screen.getByText('131/662')).toBeInTheDocument();
  });

  it('calculates global relevance_accuracy across brands using inspectedProductCount', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [
      {
        brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-07', status: 'executed',
        queryCount: 100, nodeStatusCounts: { pending: 0, running: 0, executed: 100, skipped: 0, error: 0 },
        durationSeconds: 1, startedAt: null, completedAt: null,
        telemetry: {
          inspectedProductCount: 400,
          detectedProductCount: 100,
          reusedProductCount: 300,
          irrelevantProductCount: 40,
        },
      },
      {
        brand: 'harbor', batchId: 'batch-2', dataDate: '2026-09-07', status: 'executed',
        queryCount: 100, nodeStatusCounts: { pending: 0, running: 0, executed: 100, skipped: 0, error: 0 },
        durationSeconds: 1, startedAt: null, completedAt: null,
        telemetry: {
          inspectedProductCount: 600,
          detectedProductCount: 200,
          reusedProductCount: 400,
          irrelevantProductCount: 60,
        },
      },
    ] } } as never);

    render(
      <PageConfigProvider loader={async () => ({
        schemaVersion: 2,
        version: 1,
        values: {
          'brand_overview.show_global_metrics': true,
          'brand_overview.global_metrics': ['relevance_accuracy'],
        },
        definitions: [],
      })}>
        <MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-07']}><BrandOverview /></MemoryRouter>
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getByText('Atlas')).toBeInTheDocument());
    // Total inspected = 400 + 600 = 1000. Total irrelevant = 40 + 60 = 100. Accuracy = 90.0%
    expect(await screen.findByText('90.0%')).toBeInTheDocument();
  });

  it('shows empty state when overview has no brands', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [] } } as never);
    render(<MemoryRouter><BrandOverview /></MemoryRouter>);
    expect(await screen.findByText('暂无 Query 治理品牌数据')).toBeInTheDocument();
  });

  it('shows load failure without rendering brand cards', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockRejectedValueOnce(new Error('overview failed')).mockResolvedValueOnce({ data: { brands: [] } } as never);
    render(<MemoryRouter><BrandOverview /></MemoryRouter>);
    expect(await screen.findByText(/overview failed/i)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '进入 Atlas 详情' })).not.toBeInTheDocument();
  });

});
