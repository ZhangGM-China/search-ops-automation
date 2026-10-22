import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { dashboardApi } from '@/shared/api';
import BrandDetail from './index';
import { PageConfigProvider } from '@/shared/pageConfig';

vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getTopQueryBrandSummary: vi.fn(),
    getTopQueryBrandQueries: vi.fn(),
    getTopQueryDetail: vi.fn(),
    getTopQueryWorkspace: vi.fn(),
    getTopQueryQueryTrend: vi.fn(),
    getTopQueryCurrentResultCount: vi.fn(),
    getTopQueryEvents: vi.fn(),
    previewTopQueryTaskActions: vi.fn(),
    executeTopQueryTaskActions: vi.fn(),
    rollbackTopQueryAction: vi.fn(),
    cancelBlacklist: vi.fn(),
    blacklistProduct: vi.fn(),
    getLatestManualRelevanceCheck: vi.fn().mockResolvedValue({ data: null }),
  },
}));

const summary = {
  brand: 'atlas', dataDate: '2026-09-06', executionDate: '2026-09-07',
  stats: { topQueryCount: 1, manualRequiredCount: 0, completedCount: 1, brandUv: 100, brandPv: 1200, ctr: 0.23, cvr: 0.04 },
};
const task = {
  id: 1, brand: 'atlas', dataDate: '2026-09-06', queryText: '连衣裙', queryRank: 1,
  searchUv: 100, searchPv: 1200, ctr: 0.23, cvr: 0.04, status: 'completed', displayStatus: '正常', resultCount: 18,
  stepStatuses: [
    { checkKey: 'recall', status: 'pass', displayStatus: '正常', summary: '召回数量正常' },
    { checkKey: 'open_factor', status: 'pass', displayStatus: '正常', summary: '重排序正常' },
    { checkKey: 'relevance', status: 'pass', displayStatus: '正常', summary: '相关性正常' },
    { checkKey: 'inventory', status: 'pass', displayStatus: '正常', summary: '断码正常' },
  ],
};
const detail = { task, actions: [], governancePlans: [], stepStatuses: task.stepStatuses, trend: [], liveRecall: { resultCount: 18, products: [] }, productDisplayLimit: 16, inventoryBrokenRateThreshold: 0.25 };
const demotionAction = { id: 3, taskId: 1, actionType: 'query_demote', checkKey: 'inventory', executionMode: 'human_approval', status: 'waiting_human_approval', productIds: ['p1'] };
const executableDemotionPlan = {
  id: 'demotion-plan-1', taskId: 1, type: 'demotion', title: '降桶商品', status: 'pending',
  statusLabel: '待执行', executable: true, actionIds: [3], actionType: 'query_demote',
  executionMode: 'human_approval', steps: [], evidence: [], products: [],
};
const executableBlacklistPlan = {
  id: 'blacklist-plan-1', taskId: 1, type: 'blacklist', title: '拉黑商品', status: 'pending',
  statusLabel: '待执行', executable: true, actionIds: [3], actionType: 'product_blacklist',
  executionMode: 'auto', steps: [], evidence: [], products: [],
};
const preview = { taskId: 1, brand: 'atlas', query: '连衣裙', configVersion: 'v1', executable: true, blockedReason: null, targetIds: ['p1'], beforeIds: [], addedIds: ['p1'], retainedIds: [], removedIds: [], desiredIds: ['p1'], targetProducts: [], rollbackAvailable: true, actionIds: [3] };

describe('Brand detail production workspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: summary } as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries: [task], total: 1 } } as never);
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: { ...detail, events: [] } } as never);
    vi.mocked(dashboardApi.getTopQueryQueryTrend).mockResolvedValue({ data: { trend: [] } } as never);
    vi.mocked(dashboardApi.getTopQueryCurrentResultCount).mockResolvedValue({ data: { taskId: 1, resultCount: 18, products: [] } } as never);
    vi.mocked(dashboardApi.getTopQueryEvents).mockResolvedValue({ data: { items: [] } } as never);
    vi.mocked(dashboardApi.previewTopQueryTaskActions).mockResolvedValue({ data: preview } as never);
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockResolvedValue({ data: { executedActions: [3], skippedActions: [] } } as never);
    vi.mocked(dashboardApi.rollbackTopQueryAction).mockResolvedValue({ data: { status: 'rolled_back' } } as never);
    vi.mocked(dashboardApi.cancelBlacklist).mockResolvedValue({ data: { code: 0 } } as never);
    vi.mocked(dashboardApi.blacklistProduct).mockResolvedValue({ data: { code: 0 } } as never);
    window.localStorage.setItem('user', JSON.stringify({ isAdmin: true }));
  });

  it('supports desktop rail collapse and return navigation', async () => {
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /><Route path="/ops-dashboard" element={<div>overview</div>} /></Routes></MemoryRouter>);
    await screen.findAllByText('连衣裙');
    fireEvent.click(screen.getByRole('button', { name: /收起 Query 列表/i }));
    expect(screen.getByRole('button', { name: /展开 Query 列表/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回品牌总览' }));
    expect(await screen.findByText('overview')).toBeInTheDocument();
  });

  it('renders an empty state when the brand has no Query data', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries: [], total: 0 } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    expect(await screen.findByText(/当前日期暂无 Query 治理数据/i)).toBeInTheDocument();
  });

  it('requires preview confirmation and submits the current config version', async () => {
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: { ...detail, actions: [demotionAction], governancePlans: [executableDemotionPlan] } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole('button', { name: '一键执行' })).toBeInTheDocument());
    await import('@testing-library/react').then(({ fireEvent }) => fireEvent.click(screen.getByRole('button', { name: '一键执行' })));
    expect(await screen.findByText('确认执行 Query 降桶')).toBeInTheDocument();
    expect(dashboardApi.executeTopQueryTaskActions).not.toHaveBeenCalled();
    const confirm = screen.getByRole('button', { name: /确认执行降桶/ });
    await import('@testing-library/react').then(({ fireEvent }) => fireEvent.click(confirm));
    await waitFor(() => expect(dashboardApi.executeTopQueryTaskActions).toHaveBeenCalledWith(1, expect.objectContaining({ expectedConfigVersion: 'v1', actionIds: [3] })));
  });

  it('restores the two-pane production layout without retired score or price surfaces', async () => {
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText('连衣裙').length).toBeGreaterThan(0));
    expect(screen.getByRole('button', { name: '返回品牌总览' })).toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: 'Query 列表' })).toBeInTheDocument();
    expect(screen.getAllByText('召回数量').length).toBeGreaterThan(0);
    expect(screen.queryByText('结果数量')).not.toBeInTheDocument();
    expect(screen.getAllByText('重排序').length).toBeGreaterThan(0);
    expect(screen.getAllByText('相关性').length).toBeGreaterThan(0);
    expect(screen.getAllByText('断码').length).toBeGreaterThan(0);
    expect(await screen.findByText('治理方案')).toBeInTheDocument();
    expect(screen.queryByText('诊断建议')).not.toBeInTheDocument();
    expect(screen.getByText('昨日数据')).toBeInTheDocument();
    expect(screen.getByText('效果对比')).toBeInTheDocument();
    expect(screen.queryByText('Top词治理方案')).not.toBeInTheDocument();
    expect(screen.queryByText('治理记录')).not.toBeInTheDocument();
    expect(screen.queryByText('健康分')).not.toBeInTheDocument();
    expect(screen.queryByText('价格高')).not.toBeInTheDocument();
  });

  it('hides query metrics and asks the workspace endpoint to omit trend data', async () => {
    render(
      <PageConfigProvider loader={async () => ({
        schemaVersion: 1,
        version: 1,
        values: { 'brand_detail.yesterday.show': false },
        definitions: [],
      })}>
        <MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}>
          <Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes>
        </MemoryRouter>
      </PageConfigProvider>,
    );

    await waitFor(() => expect(screen.getAllByText('连衣裙').length).toBeGreaterThan(0));
    expect(screen.queryByText('昨日数据')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '数据明细' })).not.toBeInTheDocument();
    expect(dashboardApi.getTopQueryWorkspace).toHaveBeenCalledWith(1, expect.objectContaining({ includeTrend: false }));
  });

  it('shows the PV order position as a hash-prefixed number', async () => {
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await screen.findAllByText('连衣裙');
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.queryByText('排名 #1')).not.toBeInTheDocument();
  });

  it('does not expose the source query rank when the list is ordered by PV', async () => {
    const highPv = { ...task, id: 2, queryText: 'PV第一词', queryRank: 223, searchPv: 797 };
    const lowerPv = { ...task, id: 3, queryText: 'PV第二词', queryRank: 50, searchPv: 643 };
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({
      data: { ...summary, queries: [lowerPv, highPv], total: 2 },
    } as never);
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockImplementation(async (taskId) => ({
      data: { ...detail, task: taskId === 2 ? highPv : lowerPv, stepStatuses: (taskId === 2 ? highPv : lowerPv).stepStatuses, events: [] },
    } as never));
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await screen.findAllByText('PV第一词');
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('#2')).toBeInTheDocument();
    expect(screen.queryByText('#223')).not.toBeInTheDocument();
    expect(screen.queryByText('#50')).not.toBeInTheDocument();
  });

  it('appends the next server batch when the Query rail scrolls near the bottom', async () => {
    const secondTask = { ...task, id: 31, queryText: '第二页 Query', queryRank: 31 };
    vi.mocked(dashboardApi.getTopQueryBrandQueries)
      .mockResolvedValueOnce({ data: { ...summary, stats: { ...summary.stats, topQueryCount: 61 }, queries: [task], total: 61, page: 1, pageSize: 30, hasNext: true, filter: 'all' } } as never)
      .mockResolvedValueOnce({ data: { ...summary, stats: { ...summary.stats, topQueryCount: 61 }, queries: [secondTask], total: 61, page: 2, pageSize: 30, hasNext: true, filter: 'all' } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await screen.findAllByText('连衣裙');
    const list = document.querySelector('.ops-brand-detail-list-scroll');
    expect(list).not.toBeNull();
    Object.defineProperties(list as Element, {
      scrollHeight: { configurable: true, value: 1200 },
      clientHeight: { configurable: true, value: 600 },
      scrollTop: { configurable: true, value: 500 },
    });
    fireEvent.scroll(list as Element);
    await waitFor(() => expect(dashboardApi.getTopQueryBrandQueries).toHaveBeenLastCalledWith(
      'atlas',
      expect.objectContaining({ date: '2026-09-06', page: 2, pageSize: 30, filter: 'all' }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    expect(await screen.findByText('第二页 Query')).toBeInTheDocument();
    expect(screen.getAllByText('连衣裙').length).toBeGreaterThan(0);
    expect(screen.queryByRole('navigation', { name: 'Query 列表分页' })).not.toBeInTheDocument();
    expect(screen.queryByText('继续下滑加载更多')).not.toBeInTheDocument();
    expect(screen.queryByText('已加载全部 Query')).not.toBeInTheDocument();
  });

  it('reloads the first server page when a rail filter changes', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandQueries)
      .mockResolvedValueOnce({ data: { ...summary, queries: [task], total: 10, page: 1, pageSize: 30, hasNext: false, filter: 'all' } } as never)
      .mockResolvedValueOnce({ data: { ...summary, queries: [task], total: 1, page: 1, pageSize: 30, hasNext: false, filter: 'recall' } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await screen.findAllByText('连衣裙');
    fireEvent.click(screen.getByRole('button', { name: '筛选召回数量 Query' }));
    await waitFor(() => expect(dashboardApi.getTopQueryBrandQueries).toHaveBeenLastCalledWith(
      'atlas',
      expect.objectContaining({ date: '2026-09-06', page: 1, pageSize: 30, filter: 'recall' }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
  });

  it('keeps the unfiltered Query total when switching between governance filters', async () => {
    const filterTotals = { recall: 1, open_factor: 16, relevance: 6, inventory: 2 };
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({
      data: { ...summary, stats: { ...summary.stats, topQueryCount: 0 } },
    } as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockImplementation(async (_brand, params = {}) => {
      const filter = params.filter || 'all';
      const total = filter === 'all' ? 30 : filterTotals[filter as keyof typeof filterTotals];
      return { data: {
        ...summary,
        queries: [{ ...task, queryText: `${filter} Query` }],
        total,
        page: 1,
        pageSize: 30,
        hasNext: false,
        filter,
      } } as never;
    });

    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await screen.findByText('all Query');
    const allButton = screen.getByRole('button', { name: '筛选全部 Query' });
    expect(allButton).toHaveTextContent('全部30');

    for (const [label, filter] of [
      ['召回数量', 'recall'], ['重排序', 'open_factor'], ['相关性', 'relevance'], ['断码', 'inventory'],
    ]) {
      fireEvent.click(screen.getByRole('button', { name: `筛选${label} Query` }));
      await screen.findByText(`${filter} Query`);
      expect(allButton).toHaveTextContent('全部30');
    }
  });

  it('keeps loaded Queries visible when the next batch fails and supports retry', async () => {
    const secondTask = { ...task, id: 31, queryText: '重试后的 Query', queryRank: 31 };
    vi.mocked(dashboardApi.getTopQueryBrandQueries)
      .mockResolvedValueOnce({ data: { ...summary, queries: [task], total: 61, page: 1, pageSize: 30, hasNext: true, filter: 'all' } } as never)
      .mockRejectedValueOnce(new Error('下一批 Query 加载失败'))
      .mockResolvedValueOnce({ data: { ...summary, queries: [secondTask], total: 61, page: 2, pageSize: 30, hasNext: false, filter: 'all' } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await screen.findAllByText('连衣裙');
    const list = document.querySelector('.ops-brand-detail-list-scroll');
    expect(list).not.toBeNull();
    Object.defineProperties(list as Element, {
      scrollHeight: { configurable: true, value: 1200 },
      clientHeight: { configurable: true, value: 600 },
      scrollTop: { configurable: true, value: 500 },
    });
    fireEvent.scroll(list as Element);
    expect(await screen.findByText('下一批 Query 加载失败')).toBeInTheDocument();
    expect(screen.getAllByText('连衣裙').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByText('重试后的 Query')).toBeInTheDocument();
    expect(screen.getAllByText('连衣裙').length).toBeGreaterThan(0);
  });

  it('shows a filter empty state without treating the brand date as empty', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandQueries)
      .mockResolvedValueOnce({ data: { ...summary, queries: [task], total: 10, page: 1, pageSize: 30, hasNext: false, filter: 'all' } } as never)
      .mockResolvedValueOnce({ data: { ...summary, queries: [], total: 0, page: 1, pageSize: 30, hasNext: false, filter: 'inventory' } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await screen.findAllByText('连衣裙');
    fireEvent.click(screen.getByRole('button', { name: '筛选断码 Query' }));
    expect(await screen.findAllByText('当前筛选下暂无 Query')).not.toHaveLength(0);
    expect(screen.queryByText('当前日期暂无 Query 治理数据')).not.toBeInTheDocument();
  });

  it('keeps the list shell visible when the selected query detail fails', async () => {
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockRejectedValue(new Error('Request failed with status code 500'));
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText('连衣裙').length).toBeGreaterThan(0));
    expect(screen.getByRole('complementary', { name: 'Query 列表' })).toBeInTheDocument();
  });

  it('renders an actionable empty state when the brand query load fails', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockRejectedValueOnce(new Error('品牌数据不可用'));
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockRejectedValueOnce(new Error('品牌数据不可用'));
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    expect(await screen.findByText('品牌数据不可用')).toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: 'Query 列表' })).not.toBeInTheDocument();
  });

  it('requires fresh confirmation after a configuration conflict', async () => {
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: { ...detail, actions: [demotionAction], governancePlans: [executableDemotionPlan] } } as never);
    vi.mocked(dashboardApi.previewTopQueryTaskActions)
      .mockResolvedValueOnce({ data: preview } as never)
      .mockResolvedValueOnce({ data: { ...preview, configVersion: 'v2' } } as never);
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockRejectedValueOnce({ response: { status: 409 } });
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '一键执行' }));
    const confirm = await screen.findByRole('button', { name: /确认执行降桶/ });
    await waitFor(() => expect(confirm).not.toBeDisabled());
    fireEvent.click(confirm);
    expect(await screen.findByText('配置已变化，请核对新影响范围后重新确认')).toBeInTheDocument();
    expect(dashboardApi.executeTopQueryTaskActions).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(confirm).not.toBeDisabled());
    fireEvent.click(confirm);
    await waitFor(() => expect(dashboardApi.executeTopQueryTaskActions).toHaveBeenLastCalledWith(1, { dryRun: false, expectedConfigVersion: 'v2', actionIds: [3] }));
  });

  it('blocks submission when the authoritative preview cannot be loaded', async () => {
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: { ...detail, actions: [demotionAction], governancePlans: [executableDemotionPlan] } } as never);
    vi.mocked(dashboardApi.previewTopQueryTaskActions).mockRejectedValueOnce(new Error('预览服务暂不可用'));
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '一键执行' }));
    expect(await screen.findByText('预览服务暂不可用')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /确认执行降桶/ })).toBeDisabled();
    expect(dashboardApi.executeTopQueryTaskActions).not.toHaveBeenCalled();
    expect(screen.getByRole('complementary', { name: 'Query 列表' })).toBeInTheDocument();
  });

  it.each([
    { executedActions: [3], skippedActions: [4], feedback: '已提交 1 个动作，1 个动作未执行' },
    { executedActions: [], skippedActions: [3], feedback: '没有动作成功提交，请查看未执行原因' },
  ])('reports partial or rejected execution accurately: $feedback', async ({ feedback, ...result }) => {
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: { ...detail, actions: [{ ...demotionAction, actionType: 'blacklist' }], governancePlans: [executableBlacklistPlan] } } as never);
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockResolvedValueOnce({ data: result } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '一键执行' }));
    expect(await screen.findByText(feedback)).toBeInTheDocument();
    expect(dashboardApi.executeTopQueryTaskActions).toHaveBeenCalledWith(1, { dryRun: false });
    expect(dashboardApi.previewTopQueryTaskActions).not.toHaveBeenCalled();
    await waitFor(() => expect(dashboardApi.getTopQueryWorkspace).toHaveBeenCalledTimes(2));
  });

  it('reports execution network failure without claiming success', async () => {
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: { ...detail, actions: [{ ...demotionAction, actionType: 'blacklist' }], governancePlans: [executableBlacklistPlan] } } as never);
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockRejectedValueOnce(new Error('执行网络错误'));
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: '一键执行' }));
    expect(await screen.findByText('执行网络错误')).toBeInTheDocument();
    expect(screen.queryByText('已提交，正在验证')).not.toBeInTheDocument();
  });

  it('hides governance execution controls when the user lacks permission', async () => {
    window.localStorage.setItem('user', JSON.stringify({ role: 'viewer' }));
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({
      data: { ...detail, actions: [{ ...demotionAction, actionType: 'blacklist' }] },
    } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getAllByText('连衣裙').length).toBeGreaterThan(0));
    expect(screen.queryByRole('button', { name: '一键执行' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '一键执行' })).not.toBeInTheDocument();
    expect(dashboardApi.executeTopQueryTaskActions).not.toHaveBeenCalled();
  });

  it('ignores a late preview response after switching to another Query', async () => {
    const firstPreview = new Promise<never>(() => undefined);
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: { ...detail, actions: [demotionAction], governancePlans: [executableDemotionPlan] } } as never);
    vi.mocked(dashboardApi.previewTopQueryTaskActions).mockReturnValue(firstPreview as never);
    const queries = [{ ...task, id: 1 }, { ...task, id: 2, queryText: '外套' }];
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries, total: 2 } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    const query = (await screen.findAllByText('连衣裙'))[0];
    fireEvent.click(await screen.findByRole('button', { name: '一键执行' }));
    expect(await screen.findByText('确认执行 Query 降桶')).toBeInTheDocument();
    fireEvent.click(screen.getByText('外套'));
    expect(query).toBeInTheDocument();
    expect(dashboardApi.executeTopQueryTaskActions).not.toHaveBeenCalled();
  });

  it('keeps implementation telemetry out of the Query card', async () => {
    const queries = [
      { ...task, telemetry: { tokenUsage: 125.5, relevanceTokenUsage: 100.5, relevanceTokenUsageMissing: true, tokenUsageMissing: true } },
      { ...task, id: 2, queryText: '缓存词', telemetry: { tokenUsage: 0, relevanceTokenUsage: 0, relevanceTokenUsageMissing: false, tokenUsageMissing: false } },
    ];
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries, total: 2 } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    await screen.findAllByText('连衣裙');
    expect(screen.queryByText(/Token/)).not.toBeInTheDocument();
    expect(screen.queryByText('不完整')).not.toBeInTheDocument();
  });

  it('distinguishes an empty selection from missing daily data', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: {
      ...summary, queries: [], total: 0, reason: 'no_candidates',
    } } as never);
    render(<MemoryRouter initialEntries={['/ops-dashboard/atlas?date=2026-09-06']}><Routes><Route path="/ops-dashboard/:brand" element={<BrandDetail />} /></Routes></MemoryRouter>);
    expect(await screen.findByText('当日无符合条件的 Query，已正常跳过治理')).toBeInTheDocument();
    expect(dashboardApi.getTopQueryWorkspace).not.toHaveBeenCalled();
  });
});
