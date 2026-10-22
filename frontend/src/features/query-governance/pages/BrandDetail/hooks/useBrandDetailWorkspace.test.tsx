import React from 'react';
import { renderHook, waitFor, act } from '@testing-library/react';
import dayjs from 'dayjs';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { dashboardApi } from '@/shared/api';
import useMobile from '@/shared/hooks/useMobile';
import { useBrandDetailWorkspace } from './useBrandDetailWorkspace';

const messageApi = { success: vi.fn(), warning: vi.fn(), error: vi.fn() };
vi.mock('antd', () => ({ message: { useMessage: () => [messageApi, null] } }));
vi.mock('@/shared/hooks/useMobile', () => ({ default: vi.fn(() => false) }));
vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getTopQueryBrandSummary: vi.fn(),
    getTopQueryBrandQueries: vi.fn(),
    getTopQueryDetail: vi.fn(),
    getTopQueryQueryTrend: vi.fn(),
    getTopQueryCurrentResultCount: vi.fn(),
    getTopQueryEvents: vi.fn(),
    getTopQueryWorkspace: vi.fn(),
    previewTopQueryTaskActions: vi.fn(),
    executeTopQueryTaskActions: vi.fn(),
    rollbackTopQueryAction: vi.fn(),
    blacklistProductBatch: vi.fn(),
    cancelBlacklist: vi.fn(),
    cancelBlacklistBatch: vi.fn(),
  },
}));

const summary = {
  brand: 'atlas', dataDate: '2026-09-07', executionDate: '2026-09-08',
  stats: { topQueryCount: 5, manualRequiredCount: 2, completedCount: 2, brandUv: 100, brandPv: 1200, ctr: 0.23, cvr: 0.04 },
};
const passSteps = [
  { checkKey: 'recall', status: 'pass', displayStatus: 'pass' },
  { checkKey: 'open_factor', status: 'pass', displayStatus: 'pass' },
  { checkKey: 'relevance', status: 'pass', displayStatus: 'pass' },
  { checkKey: 'inventory', status: 'pass', displayStatus: 'pass' },
];
const task = (id = 1, queryText = '连衣裙', extra: Record<string, unknown> = {}) => ({
  id, taskId: id, brand: 'atlas', dataDate: '2026-09-07', queryText, queryRank: id,
  searchUv: 100, searchPv: 1200, ctr: 0.23, cvr: 0.04, status: 'completed', displayStatus: 'normal', resultCount: 18,
  stepStatuses: passSteps, ...extra,
});
const detailFor = (t = task(), actions: unknown[] = []) => ({
  task: t, actions, governancePlans: [], stepStatuses: t.stepStatuses, trend: [{ date: '2026-09-07', value: 2 }],
  liveRecall: { resultCount: 18, products: [] }, productDisplayLimit: 16, inventoryBrokenRateThreshold: 0.25,
});
const demotionAction = {
  id: 3, taskId: 1, actionType: 'query_demote', checkKey: 'inventory', executionMode: 'human_approval',
  status: 'waiting_human_approval', productIds: ['p1'],
};
const preview = {
  taskId: 1, brand: 'atlas', query: '连衣裙', configVersion: 'v1', executable: true, blockedReason: null,
  targetIds: ['p1'], beforeIds: [], addedIds: ['p1'], retainedIds: [], removedIds: [], desiredIds: ['p1'], targetProducts: [], rollbackAvailable: true, actionIds: [3],
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

const wrapperFor = (initialEntry = '/ops-dashboard/atlas?date=2026-09-07') => ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter initialEntries={[initialEntry]}>
    <Routes><Route path="/ops-dashboard/:brand" element={children} /></Routes>
  </MemoryRouter>
);

describe('useBrandDetailWorkspace integration contract', () => {
  afterEach(() => vi.useRealTimers());
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useMobile).mockReturnValue(false);
    window.localStorage.clear();
    window.localStorage.setItem('user', JSON.stringify({ isAdmin: true }));
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: summary } as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries: [task()], total: 1 } } as never);
    vi.mocked(dashboardApi.getTopQueryQueryTrend).mockResolvedValue({ data: { trend: [{ date: '2026-09-07', value: 9 }] } } as never);
    vi.mocked(dashboardApi.getTopQueryCurrentResultCount).mockResolvedValue({ data: { taskId: 1, resultCount: 21, products: [{ id: 'p1' }] } } as never);
    vi.mocked(dashboardApi.getTopQueryEvents).mockResolvedValue({ data: { items: [{ id: 1 }] } } as never);
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: { ...detailFor(), trend: [{ date: '2026-09-07', value: 9 }], events: [{ id: 1 }] } } as never);
    vi.mocked(dashboardApi.previewTopQueryTaskActions).mockResolvedValue({ data: preview } as never);
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockResolvedValue({ data: { executedActions: [3], skippedActions: [] } } as never);
    vi.mocked(dashboardApi.rollbackTopQueryAction).mockResolvedValue({ data: { status: 'rolled_back' } } as never);
  });

  it('loads the selected Query through one workspace request before live recall', async () => {
    const live = deferred<{ data: { taskId: number; resultCount: number; products: unknown[] } }>();
    vi.mocked(dashboardApi.getTopQueryCurrentResultCount).mockReturnValue(live.promise as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail?.task.id).toBe(1));
    expect(dashboardApi.getTopQueryWorkspace).toHaveBeenCalledWith(1, expect.anything());
    expect(dashboardApi.getTopQueryDetail).not.toHaveBeenCalled();
    expect(dashboardApi.getTopQueryQueryTrend).not.toHaveBeenCalled();
    expect(dashboardApi.getTopQueryEvents).not.toHaveBeenCalled();
    expect(dashboardApi.getTopQueryCurrentResultCount).toHaveBeenCalledWith(1, expect.anything());
    live.resolve({ data: { taskId: 1, resultCount: 21, products: [] } });
    await waitFor(() => expect(result.current.detail?.liveRecall?.resultCount).toBe(21));
  });

  it('reloads the detail when a filter keeps the same first Query selected', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({
      data: { ...summary, queries: [task()], total: 1 },
    } as never);

    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail?.task.id).toBe(1));
    expect(dashboardApi.getTopQueryWorkspace).toHaveBeenCalledTimes(1);

    act(() => result.current.handleFilterChange('relevance'));

    await waitFor(() => expect(dashboardApi.getTopQueryWorkspace).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.detailLoading).toBe(false));
  });

  it('loads detail with trend/live snapshot and computes issue and filter counts', async () => {
    const problematic = task(2, '外套', { status: 'failed', displayStatus: '需处理', stepStatuses: [
      { checkKey: 'recall', status: 'failed', displayStatus: 'failed', evidence: { issueTypes: ['result_count_drop'] } },
      { checkKey: 'relevance', status: 'failed', displayStatus: 'failed' },
      { checkKey: 'open_factor', status: 'failed', displayStatus: 'failed' },
      { checkKey: 'inventory', status: 'failed', displayStatus: 'failed' },
    ] });
    const fixed = task(3, '鞋', { displayStatus: 'completed' });
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries: [task(), problematic, fixed], total: 3 } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail?.task.resultCount).toBe(21));
    expect(result.current.detail?.trend).toEqual([{ date: '2026-09-07', value: 9 }]);
    expect(result.current.events).toHaveLength(1);
    expect(result.current.workflowSteps.map((s) => s.value)).toEqual([5, 1, 1, 1, 1]);
    await act(async () => result.current.setFilter('result_drop'));
    expect(result.current.queries.map((q) => q.id)).toEqual([2]);
    await act(async () => result.current.setFilter('fixed'));
    expect(result.current.queries.map((q) => q.id)).toEqual([3]);
    await act(async () => result.current.setFilter('normal'));
    expect(result.current.queries.map((q) => q.id)).toEqual([1]);
    await act(async () => result.current.setFilter('recall'));
    expect(result.current.queries.map((q) => q.id)).toEqual([2]);
    await act(async () => result.current.setFilter('relevance'));
    expect(result.current.queries.map((q) => q.id)).toEqual([2]);
    await act(async () => result.current.setFilter('open_factor'));
    expect(result.current.queries.map((q) => q.id)).toEqual([2]);
    await act(async () => result.current.setFilter('inventory'));
    expect(result.current.queries.map((q) => q.id)).toEqual([2]);
  });

  it('hides disabled filter options and returns to all when the active check becomes hidden', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: {
      ...summary, queries: [task()], total: 1,
      visibleCheckKeys: ['recall', 'open_factor', 'relevance', 'inventory'],
    } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.queryData?.queries).toHaveLength(1));
    act(() => result.current.setFilter('inventory'));
    await waitFor(() => expect(result.current.filter).toBe('inventory'));

    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: {
      ...summary, queries: [task()], total: 1,
      visibleCheckKeys: ['recall', 'open_factor', 'relevance'],
    } } as never);
    act(() => result.current.retryQueryList());

    await waitFor(() => expect(result.current.filter).toBe('all'));
    await waitFor(() => expect(result.current.workflowSteps.map((step) => step.key)).toEqual([
      'all', 'recall', 'open_factor', 'relevance',
    ]));
  });

  it('selects the first query using the same PV ordering as the visible list', async () => {
    const lowTrafficFirst = task(10, '接口第一条', { searchPv: 9, queryRank: 222 });
    const highTrafficSecond = task(11, 'PV第一条', { searchPv: 675, queryRank: 1 });
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({
      data: { ...summary, queries: [lowTrafficFirst, highTrafficSecond], total: 2 },
    } as never);
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockImplementation(async (taskId) => ({
      data: detailFor(taskId === 11 ? highTrafficSecond : lowTrafficFirst),
    } as never));

    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });

    await waitFor(() => expect(result.current.selectedTaskId).toBe(11));
    expect(result.current.queries[0].id).toBe(11);
    expect(result.current.detail?.task.id).toBe(11);
  });

  it('uses issue Query counts for workflow badges instead of node reach counts', async () => {
    const issueStats = {
      ...summary.stats,
      topQueryCount: 5,
      noResultOrLowResultCount: 2,
      resultDropCount: 1,
      relevanceIssueCount: 3,
      openFactorIssueCount: 1,
      inventoryProductIssueCount: 4,
      stepIssueCounts: { recall: 3, relevance: 3, open_factor: 1, inventory: 4 },
    };
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: { ...summary, stats: issueStats } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, stats: issueStats, queries: [task()], total: 1 } } as never);

    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.workflowSteps.map((step) => step.value)).toEqual([5, 3, 1, 3, 4]);
  });

  it('counts governance filters from issue evidence rather than executed node status', async () => {
    const issueSteps = [
      { checkKey: 'recall', status: 'executed', displayStatus: 'executed', evidence: { issueTypes: ['low_result'] } },
      { checkKey: 'open_factor', status: 'error', displayStatus: 'error', evidence: { issueTypes: ['should_reorder'] } },
      { checkKey: 'relevance', status: 'executed', displayStatus: 'executed', evidence: { issueTypes: ['blacklist_candidates'] } },
      { checkKey: 'inventory', status: 'executed', displayStatus: 'executed', evidence: { issueTypes: ['inventory'] } },
    ];
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: { ...summary, stats: {} } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries: [task(4, '有治理问题', { stepStatuses: issueSteps })], total: 1 } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.workflowSteps.map((step) => step.value)).toEqual([1, 1, 1, 1, 1]);
    expect(result.current.workflowSteps[1].label).toBe('召回数量');
    await act(async () => result.current.setFilter('recall'));
    expect(result.current.queries.map((query) => query.id)).toEqual([4]);
  });

  it('does not filter a healthy step when display text is localized as executed', async () => {
    const healthyWithLocalizedDisplay = task(4, '正常词', { stepStatuses: [
      { checkKey: 'recall', status: 'pass', displayStatus: '已执行' },
      { checkKey: 'relevance', status: 'pass', displayStatus: '已执行' },
      { checkKey: 'open_factor', status: 'pass', displayStatus: '已执行' },
      { checkKey: 'inventory', status: 'pass', displayStatus: '已执行' },
    ] });
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries: [healthyWithLocalizedDisplay], total: 1 } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => result.current.setFilter('relevance'));
    expect(result.current.queries).toHaveLength(0);
  });

  it('counts and filters relevance issues by Query when blacklist candidates exist', async () => {
    const relevanceIssue = task(5, '含不相关商品', { stepStatuses: [
      ...passSteps.filter((step) => step.checkKey !== 'relevance'),
      {
        checkKey: 'relevance',
        status: 'executed',
        displayStatus: 'executed',
        evidence: {
          irrelevantProductCount: 2,
          blacklistCandidates: [{ productId: 'p1' }, { productId: 'p2' }],
        },
      },
    ] });
    const healthy = task(6, '全部相关', { stepStatuses: [
      ...passSteps.filter((step) => step.checkKey !== 'relevance'),
      {
        checkKey: 'relevance',
        status: 'executed',
        displayStatus: 'executed',
        evidence: { irrelevantProductCount: 0, blacklistCandidates: [] },
      },
    ] });
    const issueStats = {
      ...summary.stats,
      relevanceIssueCount: 1,
      stepIssueCounts: { relevance: 1 },
    };
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: { ...summary, stats: issueStats } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({
      data: { ...summary, queries: [relevanceIssue, healthy], total: 2 },
    } as never);

    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.workflowSteps.find((step) => step.key === 'relevance')?.value).toBe(1);
    await act(async () => result.current.setFilter('relevance'));
    expect(result.current.queries.map((query) => query.id)).toEqual([5]);
  });

  it('does not count a successful reorder execution without issue evidence', async () => {
    const healthyExecuted = task(5, '重排正常', { stepStatuses: [
      { checkKey: 'recall', status: 'executed', displayStatus: '已执行' },
      { checkKey: 'open_factor', status: 'executed', displayStatus: '已执行' },
      { checkKey: 'relevance', status: 'executed', displayStatus: '已执行' },
      { checkKey: 'inventory', status: 'executed', displayStatus: '已执行' },
    ] });
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, stats: {}, queries: [healthyExecuted], total: 1 } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.workflowSteps[2].value).toBe(0);
  });

  it('keeps reorder errors visible when authoritative stats only count should-reorder evidence', async () => {
    const reorderError = task(7, '重排异常', { stepStatuses: [
      { checkKey: 'recall', status: 'executed', displayStatus: 'executed' },
      {
        checkKey: 'open_factor',
        status: 'error',
        displayStatus: 'error',
        summary: '共享上下文采集或持久化失败',
        evidence: { issueTypes: [] },
      },
      { checkKey: 'relevance', status: 'executed', displayStatus: 'executed' },
      { checkKey: 'inventory', status: 'executed', displayStatus: 'executed' },
    ] });
    const staleStats = {
      ...summary.stats,
      stepIssueCounts: { open_factor: 0 },
      openFactorIssueCount: 0,
    };
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: { ...summary, stats: staleStats } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({
      data: { ...summary, stats: staleStats, queries: [reorderError], total: 1 },
    } as never);

    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.queryData?.queries[0]?.id).toBe(7));
    expect(result.current.queryData?.queries[0]?.stepStatuses?.find((step) => step.checkKey === 'open_factor')?.status).toBe('error');

    expect(result.current.workflowSteps.find((step) => step.key === 'open_factor')?.value).toBe(1);
    await act(async () => result.current.setFilter('open_factor'));
    expect(result.current.queries.map((query) => query.id)).toEqual([7]);
  });

  it('keeps an implicit today route on today when today has no data', async () => {
    const today = dayjs().format('YYYY-MM-DD');
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: { ...summary, dataDate: null, executionDate: null } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, dataDate: null, executionDate: null, queries: [], total: 0 } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor('/ops-dashboard/atlas') });
    await waitFor(() => expect(result.current.currentDate).toBe(today));
    await waitFor(() => expect(result.current.queryData?.queries).toEqual([]));
    expect(result.current.queryData?.queries).toEqual([]);
    expect(dashboardApi.getTopQueryBrandSummary).toHaveBeenCalledWith('atlas', { date: today }, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(dashboardApi.getTopQueryBrandSummary).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it('queries today explicitly for an implicit today route', async () => {
    const today = dayjs().format('YYYY-MM-DD');
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor('/ops-dashboard/atlas') });

    await waitFor(() => expect(result.current.queryData?.queries).toHaveLength(1));

    expect(dashboardApi.getTopQueryBrandSummary).toHaveBeenCalledWith('atlas', { date: today }, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(dashboardApi.getTopQueryBrandQueries).toHaveBeenCalledWith('atlas', expect.objectContaining({ date: today, page: 1, pageSize: 30, filter: 'all' }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it('handles 401 and non-401 route errors without conflating notifications', async () => {
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockRejectedValue({ response: { status: 401 } });
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockRejectedValue({ response: { status: 401 } });
    const first = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(first.result.current.activeLoadError).toBe('登录状态已失效，请重新登录'));
    expect(messageApi.error).not.toHaveBeenCalled();
    first.unmount();
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockRejectedValue(new Error('服务不可用'));
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockRejectedValue(new Error('服务不可用'));
    const second = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(second.result.current.activeLoadError).toBe('服务不可用'));
    expect(messageApi.error).toHaveBeenCalledWith('服务不可用');
  });

  it('ignores stale route responses and resets state on date switch', async () => {
    const firstSummary = deferred<{ data: typeof summary }>();
    const firstQueries = deferred<{ data: { queries: typeof task[] } }>();
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockReturnValueOnce(firstSummary.promise as never).mockResolvedValue({ data: summary } as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockReturnValueOnce(firstQueries.promise as never).mockResolvedValue({ data: { ...summary, queries: [task(2, '新日期')] } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await act(async () => result.current.switchDate('2026-09-06'));
    firstSummary.resolve({ data: summary });
    firstQueries.resolve({ data: { queries: [task(1)] } } as never);
    await act(async () => Promise.resolve());
    expect(result.current.currentDate).toBe('2026-09-06');
    await waitFor(() => expect(result.current.queryData?.queries[0].queryText).toBe('新日期'));
    await act(async () => result.current.switchDate('2026-09-06'));
    expect(result.current.currentDate).toBe('2026-09-06');
  });

  it('does not restore historical queries after switching back to a date with no data', async () => {
    const today = dayjs().format('YYYY-MM-DD');
    const historicalDate = '2026-09-08';
    const historicalTask = task(9, '历史词', { dataDate: historicalDate });
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockImplementation(async (_brand, params = {}) => ({
      data: params.date === today ? { ...summary, dataDate: null } : { ...summary, dataDate: historicalDate },
    }) as never);
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockImplementation(async (_brand, params = {}) => ({
      data: params.date === today
        ? { ...summary, dataDate: null, queries: [], total: 0 }
        : { ...summary, dataDate: historicalDate, queries: [historicalTask], total: 1 },
    }) as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor(`/ops-dashboard/atlas?date=${today}`) });
    await waitFor(() => expect(result.current.queryData?.queries).toHaveLength(0));
    act(() => result.current.switchDate(historicalDate));
    await waitFor(() => expect(result.current.queryData?.queries[0].queryText).toBe('历史词'));
    act(() => result.current.switchDate(today));
    await waitFor(() => expect(result.current.queryData?.queries).toEqual([]));
    expect(dashboardApi.getTopQueryBrandQueries).toHaveBeenCalledWith('atlas', expect.objectContaining({ date: today, page: 1, pageSize: 30, filter: 'all' }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it('supports desktop rail collapse, peek lock/unlock and date boundaries', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-09-08T12:00:00+08:00'));
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor('/ops-dashboard/atlas?date=2026-09-07') });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.isDesktop).toBe(true);
    expect(result.current.listVisible).toBe(true);
    act(() => result.current.toggleListCollapsed());
    expect(result.current.listCollapsed).toBe(true);
    expect(result.current.listVisible).toBe(false);
    act(() => result.current.openPeekList());
    expect(result.current.listPeekOpen).toBe(false); // rail is locked until pointer exits
    act(() => result.current.unlockPeekOnRailExit());
    act(() => result.current.openPeekList());
    expect(result.current.listPeekOpen).toBe(true);
    act(() => result.current.closePeekList());
    expect(result.current.listPeekOpen).toBe(false);
    act(() => result.current.toggleListCollapsed());
    expect(result.current.listPinnedOpen).toBe(true);
    expect(result.current.detailOffset).toBe(392);
    act(() => result.current.shiftDate(1));
    await waitFor(() => expect(result.current.currentDate).toBe('2026-09-08'));
    const calls = vi.mocked(dashboardApi.getTopQueryBrandSummary).mock.calls.length;
    act(() => result.current.shiftDate(1));
    expect(result.current.currentDate).toBe('2026-09-08');
    expect(dashboardApi.getTopQueryBrandSummary).toHaveBeenCalledTimes(calls);
    act(() => result.current.shiftDate(-1));
    await waitFor(() => expect(result.current.currentDate).toBe('2026-09-07'));
  });

  it('collapses the list on mobile selection and restores it on date switch', async () => {
    vi.mocked(useMobile).mockReturnValue(true);
    const second = task(2, '外套');
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries: [task(), second] } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.queryData?.queries).toHaveLength(2));
    act(() => result.current.handleSelectTask(second as never));
    expect(result.current.selectedTaskId).toBe(2);
    expect(result.current.listCollapsed).toBe(true);
    act(() => result.current.handleSelectTask(second as never));
    expect(result.current.listCollapsed).toBe(true);
    act(() => result.current.switchDate('2026-09-06'));
    expect(result.current.listCollapsed).toBe(false);
    expect(result.current.detailOffset).toBe(0);
  });

  it.each([
    { executedActions: [3], skippedActions: [4], text: '已提交 1 个动作，1 个动作未执行' },
    { executedActions: [3], skippedActions: [], text: '已提交，正在验证' },
    { executedActions: [], skippedActions: [3], text: '没有动作成功提交，请查看未执行原因' },
  ])('submits non-demotion governance actions and reports $text', async (response) => {
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: detailFor(task(), [{ ...demotionAction, actionType: 'blacklist' }]) } as never);
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockResolvedValue({ data: response } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail).not.toBeNull());
    act(() => result.current.handleExecuteTaskActions(1));
    await waitFor(() => expect(messageApi.warning.mock.calls.flat().concat(messageApi.success.mock.calls.flat(), messageApi.error.mock.calls.flat())).toContain(response.text));
    expect(dashboardApi.executeTopQueryTaskActions).toHaveBeenCalledWith(1, { dryRun: false });
    await waitFor(() => expect(dashboardApi.getTopQueryWorkspace).toHaveBeenCalledTimes(2));
  });

  it('guards duplicate execution, handles execution failure and refresh failure', async () => {
    const execution = deferred<{ data: { executedActions: number[]; skippedActions: number[] } }>();
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: detailFor(task(), [{ ...demotionAction, actionType: 'blacklist' }]) } as never);
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockReturnValue(execution.promise as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail).not.toBeNull());
    act(() => { result.current.handleExecuteTaskActions(1); result.current.handleExecuteTaskActions(1); });
    expect(dashboardApi.executeTopQueryTaskActions).toHaveBeenCalledTimes(1);
    execution.resolve({ data: { executedActions: [3], skippedActions: [] } });
    await waitFor(() => expect(result.current.executingTaskIds.size).toBe(0));
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockRejectedValueOnce(new Error('执行网络错误'));
    act(() => result.current.handleExecuteTaskActions(1));
    await waitFor(() => expect(messageApi.error).toHaveBeenCalledWith('执行网络错误'));
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockResolvedValueOnce({ data: { executedActions: [3], skippedActions: [] } } as never);
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockRejectedValueOnce(new Error('刷新失败'));
    act(() => result.current.handleExecuteTaskActions(1));
    await waitFor(() => expect(messageApi.warning).toHaveBeenCalledWith('动作已提交，但页面刷新失败'));
  });

  it('loads and closes demotion preview, rejects invalid confirmations and handles preview errors', async () => {
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: detailFor(task(), [demotionAction]) } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail).not.toBeNull());
    act(() => result.current.handleConfirmDemotion('bad', [99]));
    expect(dashboardApi.executeTopQueryTaskActions).not.toHaveBeenCalled();
    act(() => result.current.handleExecuteTaskActions(1));
    await waitFor(() => expect(result.current.demotionPreview).toEqual(preview));
    expect(result.current.demotionPreviewLoading).toBe(false);
    act(() => result.current.handleCloseDemotionPreview());
    expect(result.current.demotionPreviewOpen).toBe(false);
    vi.mocked(dashboardApi.previewTopQueryTaskActions).mockRejectedValueOnce(new Error('预览不可用'));
    act(() => result.current.handleExecuteTaskActions(1));
    await waitFor(() => expect(result.current.demotionPreviewError).toBe('预览不可用'));
    expect(result.current.demotionPreviewOpen).toBe(true);
  });

  it('rejects stale preview responses after task changes and handles config conflicts', async () => {
    const pending = deferred<{ data: typeof preview }>();
    const second = task(2, '外套');
    vi.mocked(dashboardApi.getTopQueryBrandQueries).mockResolvedValue({ data: { ...summary, queries: [task(), second] } } as never);
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: detailFor(task(), [demotionAction]) } as never);
    vi.mocked(dashboardApi.previewTopQueryTaskActions).mockReturnValue(pending.promise as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail).not.toBeNull());
    act(() => result.current.handleExecuteTaskActions(1));
    act(() => result.current.handleSelectTask(second as never));
    pending.resolve({ data: preview });
    await act(async () => Promise.resolve());
    expect(result.current.demotionPreview).toBeNull();
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: detailFor(task(), [demotionAction]) } as never);
    vi.mocked(dashboardApi.previewTopQueryTaskActions).mockResolvedValueOnce({ data: preview } as never).mockResolvedValueOnce({ data: { ...preview, configVersion: 'v2' } } as never);
    vi.mocked(dashboardApi.executeTopQueryTaskActions).mockRejectedValueOnce({ response: { status: 409 } });
    act(() => result.current.handleSelectTask(task() as never));
    await waitFor(() => expect(result.current.selectedTaskId).toBe(1));
    act(() => result.current.handleExecuteTaskActions(1));
    await waitFor(() => expect(result.current.demotionPreview?.configVersion).toBe('v1'));
    act(() => result.current.handleConfirmDemotion('v1', [3]));
    await waitFor(() => expect(result.current.demotionConflictMessage).toBe('配置已变化，请核对新影响范围后重新确认'));
    expect(dashboardApi.previewTopQueryTaskActions).toHaveBeenCalledTimes(3);
  });

  it.each(['rolled_back', 'rollback_pending', 'rollback_failed', 'unknown'])('rolls back action with %s feedback and reconciles detail', async (status) => {
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockResolvedValue({ data: detailFor(task(), [{ ...demotionAction, status: 'approved' }]) } as never);
    vi.mocked(dashboardApi.rollbackTopQueryAction).mockResolvedValue({ data: { status } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail).not.toBeNull());
    act(() => result.current.openRollbackCandidate({ ...demotionAction, id: 9 } as never));
    expect(result.current.rollbackCandidate?.id).toBe(9);
    await act(async () => result.current.handleRollbackAction({ ...demotionAction, id: 9 } as never));
    expect(dashboardApi.rollbackTopQueryAction).toHaveBeenCalledWith(9);
    const expected = status === 'rolled_back' ? '已回滚并确认配置恢复' : status === 'rollback_pending' ? '回滚已提交，等待权威配置核查' : status === 'rollback_failed' ? '回滚失败，需立即人工处理' : '回滚请求已处理，当前状态：unknown';
    expect(messageApi.success.mock.calls.flat().concat(messageApi.warning.mock.calls.flat(), messageApi.error.mock.calls.flat())).toContain(expected);
  });

  it('handles rollback guards, failed rollback refresh and invalid local storage', async () => {
    window.localStorage.setItem('user', '{invalid');
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.canExecuteGovernanceActions).toBe(false);
    await act(async () => result.current.handleRollbackAction({ ...demotionAction, id: 7 } as never));
    expect(dashboardApi.rollbackTopQueryAction).not.toHaveBeenCalled();
    act(() => result.current.openRollbackCandidate({ ...demotionAction, id: 7, taskId: 0 } as never));
    await act(async () => result.current.handleRollbackAction({ ...demotionAction, id: 7, taskId: 0 } as never));
    expect(dashboardApi.rollbackTopQueryAction).toHaveBeenCalledWith(7);
    vi.mocked(dashboardApi.rollbackTopQueryAction).mockRejectedValueOnce(new Error('回滚网络错误'));
    vi.mocked(dashboardApi.getTopQueryWorkspace).mockRejectedValueOnce(new Error('回滚刷新失败'));
    act(() => result.current.openRollbackCandidate({ ...demotionAction, id: 8 } as never));
    await act(async () => result.current.handleRollbackAction({ ...demotionAction, id: 8 } as never));
    expect(messageApi.error).toHaveBeenCalledWith('回滚网络错误');
  });

  it('reports partial batch blacklist results as a warning instead of success', async () => {
    vi.mocked(dashboardApi.cancelBlacklistBatch).mockResolvedValue({
      data: { code: 0, msg: 'ok', succeeded: 1, failed: 1, items: [{ sku: 'p1', status: 'success' }] },
    } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail).not.toBeNull());

    let success = true;
    await act(async () => { success = await result.current.handleBatchBlacklistProducts(
      { id: 'plan-1', taskId: 1, type: 'blacklist' } as never,
      [{ productId: 'p1', productName: '商品 1' }, { productId: 'p2', productName: '商品 2' }],
      'cancel',
    ); });

    expect(success).toBe(false);
    expect(dashboardApi.cancelBlacklistBatch).toHaveBeenCalledWith([
      { brand: 'atlas', query: '连衣裙', sku: 'p1' },
      { brand: 'atlas', query: '连衣裙', sku: 'p2' },
    ]);
    expect(messageApi.warning).toHaveBeenCalledWith('批量取消拉黑部分成功：1 个成功，1 个失败');
    expect(messageApi.success).not.toHaveBeenCalledWith('批量取消拉黑已完成');
    expect(dashboardApi.getTopQueryWorkspace).toHaveBeenCalledTimes(2);
  });

  it('sends the operator-written correction rule as correctionRule when cancelling a blacklist', async () => {
    vi.mocked(dashboardApi.cancelBlacklist).mockResolvedValue({ data: { code: 0 } } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail).not.toBeNull());

    let success = false;
    await act(async () => { success = await result.current.handleCorrectProduct(
      { id: 'plan-1', taskId: 1, type: 'blacklist' } as never,
      { productId: 'p1', productName: '商品 1' },
      '泡泡袖等同于灯笼袖',
      'cancel_blacklist',
    ); });

    expect(success).toBe(true);
    expect(dashboardApi.cancelBlacklist).toHaveBeenCalledWith({
      brand: 'atlas', query: '连衣裙', sku: 'p1', correctionRule: '泡泡袖等同于灯笼袖',
    });
  });

  it('reports a fully successful batch blacklist result after detail readback', async () => {
    vi.mocked(dashboardApi.blacklistProductBatch).mockResolvedValue({
      data: { code: 0, msg: 'ok', succeeded: 2, failed: 0, items: [
        { sku: 'p1', status: 'success' }, { sku: 'p2', status: 'success' },
      ] },
    } as never);
    const { result } = renderHook(() => useBrandDetailWorkspace(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.detail).not.toBeNull());

    let success = false;
    await act(async () => { success = await result.current.handleBatchBlacklistProducts(
      { id: 'plan-1', taskId: 1, type: 'blacklist' } as never,
      [{ productId: 'p1', productName: '商品 1' }, { productId: 'p2', productName: '商品 2' }],
      'blacklist',
    ); });

    expect(success).toBe(true);
    expect(messageApi.success).toHaveBeenCalledWith('批量拉黑已完成');
    expect(dashboardApi.getTopQueryWorkspace).toHaveBeenCalledTimes(2);
  });
});
