import React from 'react';
import dayjs from 'dayjs';
import { message } from 'antd';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { dashboardApi } from '@/shared/api';
import type {
  QueryDemotionExecutionPreview,
  TopQueryAction,
  TopQueryBrandQueriesData,
  TopQueryBrandSummaryData,
  TopQueryDetailData,
  TopQueryEvent,
  TopQueryGovernancePlan,
  TopQueryTask,
  UserInfo,
} from '@/shared/types';
import { getApiErrorMessage } from '@/shared/lib/error';
import { notifyRelevanceGuidanceUpdated } from '../../../components/QueryDetail/relevanceGuidanceEvents';
import useMobile from '@/shared/hooks/useMobile';
import { usePageConfig } from '@/shared/pageConfig/context';
import {
  DESKTOP_LIST_WIDTH,
  type FilterKey,
  brandNames,
  httpStatus,
  isDateText,
  normalizeTopQueryDetail,
  normalizeTopQueryTask,
  stepMatchesFilter,
  stepIssueTypes,
  toFilterKey,
  withLiveRecallSnapshot,
} from '../brandDetailUtils';
import {
  canExecuteQueryGovernanceActions,
  getQueryDemotionRollbackFeedback,
  isQueryDemotionInteractionCurrent,
  releaseMatchingActionId,
  releaseMatchingPreviewRequest,
} from '../queryDemotion';

export const useBrandDetailWorkspace = () => {
  const { brand = 'atlas' } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [messageApi, messageContextHolder] = message.useMessage();
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<{ routeKey: string; message: string } | null>(null);
  const [summary, setSummary] = React.useState<TopQueryBrandSummaryData | null>(null);
  const summaryRef = React.useRef<TopQueryBrandSummaryData | null>(null);
  const summaryRouteKeyRef = React.useRef<string | null>(null);
  const [queryData, setQueryData] = React.useState<TopQueryBrandQueriesData | null>(null);
  const queryDataRef = React.useRef<TopQueryBrandQueriesData | null>(null);
  const [allQueryTotal, setAllQueryTotal] = React.useState<{ routeKey: string; total: number } | null>(null);
  const [filter, setFilter] = React.useState<FilterKey>('all');
  const [queryPage, setQueryPage] = React.useState(1);
  const queryPageSize = 30;
  const [queryListLoading, setQueryListLoading] = React.useState(false);
  const [queryListError, setQueryListError] = React.useState<string | null>(null);
  const [queryRetryToken, setQueryRetryToken] = React.useState(0);
  const [selectedTaskId, setSelectedTaskId] = React.useState<number | null>(null);
  const selectedTaskIdRef = React.useRef<number | null>(null);
  const [detail, setDetail] = React.useState<TopQueryDetailData | null>(null);
  const [events, setEvents] = React.useState<TopQueryEvent[]>([]);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [detailReloadToken, setDetailReloadToken] = React.useState(0);
  const [demotionPreviewOpen, setDemotionPreviewOpen] = React.useState(false);
  const [demotionPreview, setDemotionPreview] = React.useState<QueryDemotionExecutionPreview | null>(null);
  const [demotionPreviewLoading, setDemotionPreviewLoading] = React.useState(false);
  const [executingTaskIds, setExecutingTaskIds] = React.useState<Set<number>>(() => new Set());
  const [demotionPreviewError, setDemotionPreviewError] = React.useState<string | null>(null);
  const [demotionConflictMessage, setDemotionConflictMessage] = React.useState<string | null>(null);
  const [demotionPreviewRouteKey, setDemotionPreviewRouteKey] = React.useState<string | null>(null);
  const [rollingBackActionId, setRollingBackActionId] = React.useState<number | null>(null);
  const [rollbackCandidate, setRollbackCandidate] = React.useState<TopQueryAction | null>(null);
  const [rollbackCandidateRouteKey, setRollbackCandidateRouteKey] = React.useState<string | null>(null);
  const demotionPreviewSequenceRef = React.useRef(0);
  const demotionPreviewInFlightRef = React.useRef<number | null>(null);
  const executingTaskIdsRef = React.useRef(new Set<number>());
  const rollbackInFlightActionIdsRef = React.useRef(new Set<number>());
  const [listCollapsed, setListCollapsed] = React.useState(false);
  const [listPeekOpen, setListPeekOpen] = React.useState(false);
  const [peekBlockedUntilRailExit, setPeekBlockedUntilRailExit] = React.useState(false);
  const [assistantDrawerOpen, setAssistantDrawerOpen] = React.useState(false);
  const [selectedDate, setSelectedDate] = React.useState(() => {
    const dateParam = searchParams.get('date');
    return isDateText(dateParam) ? dateParam as string : dayjs().format('YYYY-MM-DD');
  });
  const activeRouteKeyRef = React.useRef(`${brand}:${selectedDate}`);
  const detailRefreshSequenceRef = React.useRef(0);
  const [loadedRouteKey, setLoadedRouteKey] = React.useState(`${brand}:${selectedDate}`);
  const loadedRouteKeyRef = React.useRef(`${brand}:${selectedDate}`);
  const isMobile = useMobile();
  const showQueryMetrics = usePageConfig('brand_detail.yesterday.show') === true;
  const isDesktop = !isMobile;
  const canExecuteGovernanceActions = React.useMemo(() => {
    try {
      const user = window.localStorage.getItem('user');
      return canExecuteQueryGovernanceActions(user ? JSON.parse(user) as UserInfo : null);
    } catch {
      return false;
    }
  }, []);
  const listPaneRef = React.useRef<HTMLElement | null>(null);
  const routeBrandName = brandNames[brand] ?? brand;
  const currentRouteKey = `${brand}:${selectedDate}`;
  const resetDemotionInteraction = React.useCallback(() => {
    demotionPreviewSequenceRef.current += 1;
    demotionPreviewInFlightRef.current = null;
    setDemotionPreviewRouteKey(null);
    setDemotionPreviewOpen(false);
    setDemotionPreview(null);
    setDemotionPreviewLoading(false);
    setDemotionPreviewError(null);
    setDemotionConflictMessage(null);
    setRollbackCandidate(null);
    setRollbackCandidateRouteKey(null);
  }, []);
  const loadQueryWorkspace = React.useCallback(async (taskId: number, signal: AbortSignal): Promise<TopQueryDetailData> => {
    const response = await dashboardApi.getTopQueryWorkspace(taskId, { signal, includeTrend: showQueryMetrics });
    return normalizeTopQueryDetail(response.data);
  }, [showQueryMetrics]);

  const loadQueryDetailWithTrend = React.useCallback(async (taskId: number): Promise<TopQueryDetailData> => {
    const response = await dashboardApi.getTopQueryWorkspace(taskId, { includeTrend: showQueryMetrics });
    return normalizeTopQueryDetail(response.data);
  }, [showQueryMetrics]);

  const isRequestCancelled = React.useCallback((error: unknown) => (
    Boolean(error && typeof error === 'object' && (
      (error as { code?: string }).code === 'ERR_CANCELED'
      || (error as { name?: string }).name === 'CanceledError'
      || (error as { name?: string }).name === 'AbortError'
    ))
  ), []);

  const orderQueries = React.useCallback((items: TopQueryTask[]) => [...items].sort((a, b) => {
    const pvDelta = Number(b.searchPv ?? 0) - Number(a.searchPv ?? 0);
    if (pvDelta !== 0) return pvDelta;
    const queryA = String(a.queryText ?? '');
    const queryB = String(b.queryText ?? '');
    const queryDelta = queryA < queryB ? -1 : queryA > queryB ? 1 : 0;
    if (queryDelta !== 0) return queryDelta;
    return Number(a.id) - Number(b.id);
  }), []);

  React.useEffect(() => {
    activeRouteKeyRef.current = currentRouteKey;
    selectedTaskIdRef.current = null;
    detailRefreshSequenceRef.current += 1;
    demotionPreviewSequenceRef.current += 1;
    demotionPreviewInFlightRef.current = null;
  }, [currentRouteKey]);

  React.useEffect(() => {
    let mounted = true;
    const controller = new AbortController();
    const routeKey = `${brand}:${selectedDate}`;
    const previousQueryData = queryDataRef.current;
    const isAppending = queryPage > 1
      && previousQueryData?.filter === filter
      && loadedRouteKeyRef.current === routeKey;
    const cachedSummary = summaryRouteKeyRef.current === routeKey ? summaryRef.current : null;
    const dateParams = { date: selectedDate };
    const queryParams = { date: selectedDate, page: queryPage, pageSize: queryPageSize, filter };
    Promise.all([
      cachedSummary
        ? Promise.resolve({ data: cachedSummary })
        : dashboardApi.getTopQueryBrandSummary(brand, dateParams, { signal: controller.signal }),
      dashboardApi.getTopQueryBrandQueries(brand, queryParams, { signal: controller.signal }),
    ])
      .then(async ([summaryResponse, queriesResponse]) => {
        if (!mounted || activeRouteKeyRef.current !== routeKey) return;
        const nextSummary = summaryResponse.data;
        const nextQueryData = {
          ...queriesResponse.data,
          queries: orderQueries(queriesResponse.data.queries.map(normalizeTopQueryTask)),
        };
        const mergedQueryData = isAppending && previousQueryData
          ? {
            ...nextQueryData,
            queries: orderQueries([
              ...previousQueryData.queries,
              ...nextQueryData.queries.filter((task) => !previousQueryData.queries.some((loadedTask) => loadedTask.id === task.id)),
            ]),
          }
          : nextQueryData;
        setSummary(nextSummary);
        summaryRef.current = nextSummary;
        summaryRouteKeyRef.current = routeKey;
        queryDataRef.current = mergedQueryData;
        setQueryData(mergedQueryData);
        if (filter === 'all') {
          setAllQueryTotal({ routeKey, total: nextQueryData.total });
        }
        loadedRouteKeyRef.current = routeKey;
        setLoadedRouteKey(routeKey);
        setQueryListError(null);
        if (!isAppending) {
          const firstTask = nextQueryData.queries[0];
          const shouldReloadSameTask = firstTask?.id != null
            && selectedTaskIdRef.current === firstTask.id;
          resetDemotionInteraction();
          selectedTaskIdRef.current = firstTask?.id ?? null;
          setSelectedTaskId(firstTask?.id ?? null);
          if (!firstTask) {
            setDetailLoading(false);
            setDetail(null);
            setEvents([]);
          } else {
            setDetailLoading(true);
            if (shouldReloadSameTask) setDetailReloadToken((token) => token + 1);
          }
        }
      })
      .catch((error: unknown) => {
        if (!mounted || activeRouteKeyRef.current !== routeKey || isRequestCancelled(error)) return;
        const errorMessage = httpStatus(error) === 401
          ? '登录状态已失效，请重新登录'
          : getApiErrorMessage(error, '获取品牌 Query 治理详情失败');
        if (isAppending) {
          setQueryListError(errorMessage);
        } else {
          setLoadError({ routeKey, message: errorMessage });
        }
        if (httpStatus(error) !== 401 && !isAppending) {
          messageApi.error(errorMessage);
        }
      })
      .finally(() => {
        if (mounted) {
          setLoading(false);
          setQueryListLoading(false);
        }
      });
    return () => {
      mounted = false;
      controller.abort();
    };
  }, [brand, filter, isRequestCancelled, messageApi, orderQueries, queryPage, queryPageSize, queryRetryToken, resetDemotionInteraction, selectedDate]);

  React.useEffect(() => {
    if (!selectedTaskId) return;
    let mounted = true;
    const workspaceController = new AbortController();
    const liveController = new AbortController();
    const routeKey = `${brand}:${selectedDate}`;
    loadQueryWorkspace(selectedTaskId, workspaceController.signal)
      .then((nextDetail) => {
        if (!mounted || activeRouteKeyRef.current !== routeKey) return;
        setDetail(nextDetail);
        setEvents(nextDetail.events ?? []);
        setDetailLoading(false);
        void dashboardApi.getTopQueryCurrentResultCount(selectedTaskId, { signal: liveController.signal })
          .then((liveResponse) => {
            if (!mounted || activeRouteKeyRef.current !== routeKey || selectedTaskIdRef.current !== selectedTaskId) return;
            setDetail((current) => current ? withLiveRecallSnapshot(current, liveResponse.data) : current);
          })
          .catch((error: unknown) => {
            if (!isRequestCancelled(error)) return;
          });
      })
      .catch((error: unknown) => {
        if (isRequestCancelled(error)) return;
        messageApi.error(getApiErrorMessage(error, '获取 Query 详情失败'));
      })
      .finally(() => {
        if (mounted && !workspaceController.signal.aborted) setDetailLoading(false);
      });
    return () => {
      mounted = false;
      workspaceController.abort();
      liveController.abort();
    };
  }, [brand, detailReloadToken, isRequestCancelled, loadQueryWorkspace, messageApi, selectedDate, selectedTaskId]);

  const queries = React.useMemo(() => {
    const items = queryData?.queries ?? [];
    if (queryData?.filter === filter) return items;
    if (filter === 'all') return items;
    if (['recall', 'relevance', 'open_factor', 'inventory'].includes(filter)) {
      return items.filter((task) => task.stepStatuses?.some((step) => step.checkKey === filter && stepMatchesFilter(step)));
    }
    if (filter === 'result_drop') {
      return items.filter((task) => task.stepStatuses?.some((step) => (
        step.checkKey === 'recall' && stepIssueTypes(step).includes('result_count_drop')
      )));
    }
    return items.filter((task) => toFilterKey(task) === filter);
  }, [queryData?.filter, queryData?.queries, filter]);

  React.useEffect(() => {
    if (filter !== 'all' && queryData?.visibleCheckKeys && !queryData.visibleCheckKeys.includes(filter)) {
      const timer = window.setTimeout(() => {
        setFilter('all');
        setQueryPage(1);
      }, 0);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [filter, queryData?.visibleCheckKeys]);
  const handleFilterChange = React.useCallback((nextFilter: FilterKey) => {
    setFilter(nextFilter);
    setQueryPage(1);
    setQueryListLoading(true);
    setQueryListError(null);
  }, []);

  const queryHasNext = Boolean(queryData?.hasNext ?? (
    queryData && queryData.total > queryData.queries.length
  ));
  const loadMoreQueries = React.useCallback(() => {
    if (queryListLoading || !queryHasNext) return;
    setQueryListLoading(true);
    setQueryPage((currentPage) => currentPage + 1);
  }, [queryHasNext, queryListLoading]);
  const retryQueryList = React.useCallback(() => {
    setQueryListLoading(true);
    setQueryListError(null);
    setQueryRetryToken((token) => token + 1);
  }, []);

  const statusCounts = React.useMemo(() => {
    const counts: Record<FilterKey, number> = {
      all: queryData?.queries.length ?? 0,
      normal: 0,
      fixed: 0,
      waiting_human_approval: 0,
      still_problematic: 0,
      recall: 0,
      result_drop: 0,
      relevance: 0,
      open_factor: 0,
      inventory: 0,
    };
    const stats = summary?.stats || queryData?.stats;
    const issueCount = (key: 'recall' | 'relevance' | 'open_factor' | 'inventory') => {
      const explicit = stats?.stepIssueCounts?.[key];
      if (key === 'recall') {
        const derived = stats?.noResultOrLowResultCount !== undefined || stats?.resultDropCount !== undefined
          ? (stats?.noResultOrLowResultCount ?? 0) + (stats?.resultDropCount ?? 0)
          : undefined;
        if (typeof explicit === 'number' && typeof derived === 'number') return Math.max(explicit, derived);
        return typeof explicit === 'number' ? explicit : derived;
      }
      if (typeof explicit === 'number') return explicit;
      if (key === 'relevance') return stats?.relevanceIssueCount;
      if (key === 'open_factor') return stats?.openFactorIssueCount;
      return stats?.inventoryProductIssueCount;
    };
    (queryData?.queries ?? []).forEach((task) => {
      counts[toFilterKey(task)] += 1;
      task.stepStatuses?.forEach((step) => {
        const isRerankAttempt = step.checkKey === 'open_factor' && stepMatchesFilter(step);
        const isIssue = step.checkKey !== 'open_factor' && stepMatchesFilter(step);
        if (isRerankAttempt || isIssue) {
          const key = step.checkKey as keyof typeof counts;
          if (key in counts) counts[key] += 1;
        }
      });
      if (task.stepStatuses?.some((step) => (
        step.checkKey === 'recall' && stepIssueTypes(step).includes('result_count_drop')
      ))) {
        counts.result_drop += 1;
      }
    });
    if (stats && (stats.stepIssueCounts || stats.noResultOrLowResultCount !== undefined || stats.resultDropCount !== undefined || stats.relevanceIssueCount !== undefined || stats.openFactorIssueCount !== undefined || stats.inventoryProductIssueCount !== undefined)) {
      (['recall', 'relevance', 'open_factor', 'inventory'] as const).forEach((key) => {
        const authoritative = issueCount(key);
        if (typeof authoritative !== 'number') return;
        counts[key] = key === 'open_factor'
          ? Math.max(counts[key], authoritative)
          : authoritative;
      });
    }
    return counts;
  }, [queryData?.queries, queryData?.stats, summary?.stats]);

  const stats = summary?.stats;
  const summaryQueryTotal = stats?.topQueryCount || summary?.summary?.queryCount || null;
  const queryTotal = summaryQueryTotal
    ?? (allQueryTotal?.routeKey === currentRouteKey ? allQueryTotal.total : 0);
  const allWorkflowSteps: Array<{ label: string; value: number; key: FilterKey }> = [
    { label: '全部', value: queryTotal, key: 'all' },
    { label: '召回数量', value: statusCounts.recall, key: 'recall' },
    { label: '重排序', value: statusCounts.open_factor, key: 'open_factor' },
    { label: '相关性', value: statusCounts.relevance, key: 'relevance' },
    { label: '断码', value: statusCounts.inventory, key: 'inventory' },
  ];
  const visibleCheckKeys = queryData?.visibleCheckKeys;
  const workflowSteps = allWorkflowSteps.filter((step) => (
    step.key === 'all' || !visibleCheckKeys || visibleCheckKeys.includes(step.key)
  ));

  const createDetailRefreshContext = (taskId: number) => ({
    sequence: ++detailRefreshSequenceRef.current,
    routeKey: `${brand}:${selectedDate}`,
    taskId,
  });
  const createDetailMutationContext = (taskId: number) => ({
    routeKey: `${brand}:${selectedDate}`,
    taskId,
  });
  const isDetailMutationCurrent = (context: { routeKey: string; taskId: number }) => (
    activeRouteKeyRef.current === context.routeKey
    && selectedTaskIdRef.current === context.taskId
  );
  const isDetailRefreshCurrent = (context: { sequence: number; routeKey: string; taskId: number }) => (
    detailRefreshSequenceRef.current === context.sequence
    && isDetailMutationCurrent(context)
  );
  const reconcileDetailAfterMutation = async (
    taskId: number,
    refreshFailureMessage: string,
    onApplied: () => void,
  ) => {
    const refreshContext = createDetailRefreshContext(taskId);
    try {
      const nextDetail = await loadQueryDetailWithTrend(taskId);
      if (!isDetailRefreshCurrent(refreshContext)) return;
      setDetail(nextDetail);
      setEvents(nextDetail.events ?? []);
      onApplied();
    } catch {
      if (!isDetailRefreshCurrent(refreshContext)) return;
      messageApi.warning(refreshFailureMessage);
    }
  };

  const submitTaskActions = async (
    taskId: number,
    payload: { dryRun: boolean; expectedConfigVersion?: string; actionIds?: number[]; causeGroupId?: string },
  ) => {
    if (executingTaskIdsRef.current.has(taskId)) return;
    executingTaskIdsRef.current.add(taskId);
    setExecutingTaskIds((current) => new Set(current).add(taskId));
    const mutationContext = createDetailMutationContext(taskId);
    try {
      const kgActionId = payload.actionIds?.length === 1 ? payload.actionIds[0] : undefined;
      const response = payload.causeGroupId && kgActionId != null
        ? await dashboardApi.executeQueryGovernanceKGAction(kgActionId, { groupId: payload.causeGroupId })
        : await dashboardApi.executeTopQueryTaskActions(taskId, payload);
      if (!isDetailMutationCurrent(mutationContext)) return;
      const executedCount = 'executedActions' in response.data
        ? response.data.executedActions?.length ?? 0
        : response.data.status === 'verified' ? 1 : 0;
      const skippedCount = 'skippedActions' in response.data
        ? response.data.skippedActions?.length ?? 0
        : response.data.status === 'skipped' ? 1 : 0;
      if (executedCount && skippedCount) {
        messageApi.warning(`已提交 ${executedCount} 个动作，${skippedCount} 个动作未执行`);
      } else if (executedCount) {
        messageApi.success('已提交，正在验证');
      } else {
        messageApi.error('没有动作成功提交，请查看未执行原因');
      }
      setDemotionPreviewOpen(false);
      await reconcileDetailAfterMutation(taskId, '动作已提交，但页面刷新失败', () => undefined);
    } catch (error: unknown) {
      if (!isDetailMutationCurrent(mutationContext)) return;
      if (httpStatus(error) === 409 && payload.expectedConfigVersion) {
        await loadDemotionPreview(taskId, '配置已变化，请核对新影响范围后重新确认');
        return;
      }
      messageApi.error(getApiErrorMessage(error, '执行失败'));
    } finally {
      executingTaskIdsRef.current.delete(taskId);
      setExecutingTaskIds((current) => {
        const next = new Set(current);
        next.delete(taskId);
        return next;
      });
    }
  };

  const loadDemotionPreview = async (taskId: number, conflictMessage: string | null = null) => {
    if (demotionPreviewInFlightRef.current !== null) return;
    const requestSequence = ++demotionPreviewSequenceRef.current;
    demotionPreviewInFlightRef.current = requestSequence;
    const mutationContext = createDetailMutationContext(taskId);
    setDemotionPreviewRouteKey(mutationContext.routeKey);
    setDemotionPreviewOpen(true);
    setDemotionPreview(null);
    setDemotionPreviewLoading(true);
    setDemotionPreviewError(null);
    setDemotionConflictMessage(conflictMessage);
    try {
      const response = await dashboardApi.previewTopQueryTaskActions(taskId);
      if (
        requestSequence !== demotionPreviewSequenceRef.current
        || !isDetailMutationCurrent(mutationContext)
      ) return;
      setDemotionPreview(response.data);
    } catch (error: unknown) {
      if (
        requestSequence !== demotionPreviewSequenceRef.current
        || !isDetailMutationCurrent(mutationContext)
      ) return;
      setDemotionPreviewError(getApiErrorMessage(error, '获取 Query 降桶预览失败'));
    } finally {
      demotionPreviewInFlightRef.current = releaseMatchingPreviewRequest(
        demotionPreviewInFlightRef.current,
        requestSequence,
      );
      if (
        requestSequence === demotionPreviewSequenceRef.current
        && isDetailMutationCurrent(mutationContext)
      ) setDemotionPreviewLoading(false);
    }
  };

  const handleExecuteTaskActions = (taskId: number, actionIds?: number[], causeGroupId?: string) => {
    if (causeGroupId && actionIds?.length === 1) {
      void submitTaskActions(taskId, { dryRun: false, actionIds, causeGroupId });
      return;
    }
    const hasQueryDemotion = detail?.task.id === taskId && detail.actions.some((action) => (
      action.actionType === 'query_demote'
      && action.checkKey === 'inventory'
      && action.executionMode === 'human_approval'
      && ['waiting_human_approval', 'pending_confirmation', 'approved'].includes(action.status)
    ));
    if (hasQueryDemotion) {
      void loadDemotionPreview(taskId);
      return;
    }
    void submitTaskActions(taskId, { dryRun: false, ...(actionIds?.length ? { actionIds } : {}) });
  };

  const handleExecuteKGPlan = async (plan: TopQueryGovernancePlan) => {
    const planId = Number(plan.actionId || 0);
    const taskId = Number(plan.taskId || selectedTaskIdRef.current || 0);
    if (!planId || !taskId || executingTaskIdsRef.current.has(taskId)) return;
    const isRecallRateAction = plan.type === 'whitelist_tag' || plan.type === 'query_whitelist';
    executingTaskIdsRef.current.add(taskId);
    setExecutingTaskIds((current) => new Set(current).add(taskId));
    const mutationContext = createDetailMutationContext(taskId);
    try {
      const response = await dashboardApi.executeQueryGovernanceKGPlan(planId);
      if (!isDetailMutationCurrent(mutationContext)) return;
      const actionLabel = isRecallRateAction ? '补召回动作' : 'KG 方案';
      messageApi.success(response.data.status === 'verified' ? `${actionLabel}已执行并完成验证` : `${actionLabel}执行状态已更新`);
      await reconcileDetailAfterMutation(taskId, `${actionLabel}已执行，但页面刷新失败`, () => undefined);
    } catch (error: unknown) {
      const actionLabel = isRecallRateAction ? '补召回动作' : 'KG 方案';
      if (isDetailMutationCurrent(mutationContext)) messageApi.error(getApiErrorMessage(error, `${actionLabel}执行失败`));
    } finally {
      executingTaskIdsRef.current.delete(taskId);
      setExecutingTaskIds((current) => {
        const next = new Set(current);
        next.delete(taskId);
        return next;
      });
    }
  };

  const handleConfirmDemotion = (expectedConfigVersion: string, actionIds: number[]) => {
    const taskId = demotionPreview?.taskId ?? selectedTaskIdRef.current;
    if (
      !taskId
      || selectedTaskIdRef.current !== taskId
      || !isQueryDemotionInteractionCurrent(demotionPreviewRouteKey, currentRouteKey)
    ) return;
    void submitTaskActions(taskId, {
      dryRun: false,
      expectedConfigVersion,
      actionIds,
    });
  };

  const handleRollbackAction = async (action: TopQueryAction) => {
    if (!isQueryDemotionInteractionCurrent(rollbackCandidateRouteKey, currentRouteKey)) return;
    if (rollbackInFlightActionIdsRef.current.has(action.id)) return;
    const taskId = Number(action.taskId || selectedTaskIdRef.current || 0);
    if (!taskId) return;
    rollbackInFlightActionIdsRef.current.add(action.id);
    setRollingBackActionId(action.id);
    const mutationContext = createDetailMutationContext(taskId);
    try {
      const response = await dashboardApi.rollbackTopQueryAction(action.id);
      if (!isDetailMutationCurrent(mutationContext)) return;
      const feedback = getQueryDemotionRollbackFeedback(response.data.status);
      messageApi[feedback.type](feedback.message);
      setRollbackCandidate(null);
      setRollbackCandidateRouteKey(null);
      await reconcileDetailAfterMutation(taskId, '回滚已提交，但页面刷新失败', () => undefined);
    } catch (error: unknown) {
      if (!isDetailMutationCurrent(mutationContext)) return;
      messageApi.error(getApiErrorMessage(error, '回滚失败'));
      setRollbackCandidate(null);
      setRollbackCandidateRouteKey(null);
      await reconcileDetailAfterMutation(taskId, '回滚状态刷新失败，请稍后重新打开详情', () => undefined);
    } finally {
      rollbackInFlightActionIdsRef.current.delete(action.id);
      setRollingBackActionId((current) => releaseMatchingActionId(current, action.id));
    }
  };

  const handleCancelGovernancePlan = async (plan: TopQueryGovernancePlan) => {
    const firstProduct = plan.products?.[0] as Record<string, unknown> | undefined;
    const sku = String(firstProduct?.productId || firstProduct?.product_id || firstProduct?.skuId || firstProduct?.sku_id || '').trim();
    if (!sku) {
      messageApi.error('治理方案缺少商品标识，无法取消');
      return;
    }
    const taskId = Number(plan.taskId || selectedTaskIdRef.current || 0);
    if (!taskId) return;
    try {
      if (plan.type === 'blacklist') {
        await dashboardApi.cancelBlacklist({ brand, query: detail?.task.queryText || '', sku });
      } else {
        await dashboardApi.cancelDemotion({ brand, query: detail?.task.queryText || '', sku, taskId });
      }
      messageApi.success('已提交取消治理方案');
      await reconcileDetailAfterMutation(taskId, '取消已提交，但页面刷新失败', () => undefined);
    } catch (error: unknown) {
      messageApi.error(getApiErrorMessage(error, '取消治理方案失败'));
    }
  };

  const handleBlacklistProduct = async (
    plan: TopQueryGovernancePlan,
    product: Record<string, unknown>,
    action: 'blacklist' | 'cancel',
    reason?: string,
  ): Promise<boolean> => {
    const sku = String(product.productId || product.product_id || product.skuId || product.sku_id || '').trim();
    const productName = String(product.productName || product.product_name || product.name || sku).trim();
    const taskId = Number(plan.taskId || selectedTaskIdRef.current || 0);
    const query = detail?.task.queryText || '';
    if (!sku || !taskId || !query) {
      messageApi.error('商品或 Query 信息不完整，无法执行拉黑操作');
      return false;
    }
    try {
      if (action === 'cancel') {
        await dashboardApi.cancelBlacklist({ brand, query, sku, reason: String(reason || '').trim() || undefined });
        messageApi.success('已取消拉黑');
      } else {
        await dashboardApi.blacklistProduct({
          brand,
          query,
          sku,
          productName,
          reason: String(product.reason || product.failureReason || '相关性检测判定商品不相关'),
        });
        messageApi.success('商品已拉黑');
      }
      await reconcileDetailAfterMutation(taskId, '操作已完成，但页面状态刷新失败', () => undefined);
      return true;
    } catch (error: unknown) {
      messageApi.error(getApiErrorMessage(error, action === 'cancel' ? '取消拉黑失败' : '拉黑失败'));
      return false;
    }
  };

  const handleBatchBlacklistProducts = async (
    plan: TopQueryGovernancePlan,
    products: Array<Record<string, unknown>>,
    action: 'blacklist' | 'cancel',
    reason?: string,
  ): Promise<boolean> => {
    const query = detail?.task.queryText || '';
    const taskId = Number(plan.taskId || selectedTaskIdRef.current || 0);
    const items = products.map((product) => ({
      brand,
      query,
      sku: String(product.productId || product.product_id || product.skuId || product.sku_id || '').trim(),
      productName: String(product.productName || product.product_name || product.name || '').trim(),
      reason: String(reason || product.reason || product.failureReason || '相关性检测判定商品不相关'),
    })).filter((item) => item.sku);
    if (!query || !taskId || !items.length) {
      messageApi.error('商品或 Query 信息不完整，无法执行批量操作');
      return false;
    }
    try {
      // 取消拉黑不上送理由：相关性规则只由用户手动填写
      const response = action === 'cancel'
        ? await dashboardApi.cancelBlacklistBatch(items.map((item) => ({ brand: item.brand, query: item.query, sku: item.sku })))
        : await dashboardApi.blacklistProductBatch(items);
      const result = response.data;
      const succeeded = Number(result.succeeded || 0);
      const failed = Number(result.failed || 0);
      const allSucceeded = result.code === 0 && succeeded === items.length && failed === 0;

      await reconcileDetailAfterMutation(taskId, '批量操作已完成，但页面状态刷新失败', () => undefined);
      if (allSucceeded) {
        messageApi.success(action === 'cancel' ? '批量取消拉黑已完成' : '批量拉黑已完成');
        return true;
      }
      if (succeeded > 0 && failed > 0) {
        messageApi.warning(`${action === 'cancel' ? '批量取消拉黑' : '批量拉黑'}部分成功：${succeeded} 个成功，${failed} 个失败`);
      } else {
        messageApi.error(action === 'cancel' ? '批量取消拉黑失败' : '批量拉黑失败');
      }
      return false;
    } catch (error: unknown) {
      messageApi.error(getApiErrorMessage(error, action === 'cancel' ? '批量取消拉黑失败' : '批量拉黑失败'));
      return false;
    }
  };

  const handleCorrectProduct = async (
    plan: TopQueryGovernancePlan,
    product: Record<string, unknown>,
    rule: string,
    mode: 'only' | 'cancel_blacklist',
  ): Promise<boolean> => {
    const sku = String(product.productId || product.product_id || product.skuId || product.sku_id || '').trim();
    const query = detail?.task.queryText || '';
    const taskId = Number(plan.taskId || selectedTaskIdRef.current || 0);
    if (!sku || !query || !taskId || !rule.trim()) {
      messageApi.error('商品、Query 或纠正规则不完整');
      return false;
    }
    try {
      if (mode === 'cancel_blacklist') {
        await dashboardApi.cancelBlacklist({ brand, query, sku, correctionRule: rule.trim() });
        try {
          const updated = await dashboardApi.getRelevanceGuidance(brand, query);
          notifyRelevanceGuidanceUpdated(updated.data);
        } catch {
          // The cancellation and rule append have already passed authoritative readback.
        }
        messageApi.success('已取消拉黑，纠正规则已追加');
      } else {
        const saved = await dashboardApi.appendRelevanceGuidanceRule(brand, { query, rule: rule.trim() });
        notifyRelevanceGuidanceUpdated(saved.data);
        messageApi.success('纠正规则已追加');
      }
      await reconcileDetailAfterMutation(taskId, '操作已完成，但页面状态刷新失败', () => undefined);
      return true;
    } catch (error: unknown) {
      messageApi.error(getApiErrorMessage(error, mode === 'cancel_blacklist' ? '取消拉黑并纠正失败' : '保存纠正规则失败'));
      return false;
    }
  };

  const openRollbackCandidate = (action: TopQueryAction) => {
    setRollbackCandidateRouteKey(currentRouteKey);
    setRollbackCandidate(action);
  };

  const handleCloseDemotionPreview = () => {
    if (demotionPreview?.taskId && executingTaskIdsRef.current.has(demotionPreview.taskId)) return;
    resetDemotionInteraction();
  };

  const toggleListCollapsed = () => {
    setListCollapsed((value) => {
      const nextCollapsed = !value;
      setListPeekOpen(false);
      setPeekBlockedUntilRailExit(nextCollapsed);
      return nextCollapsed;
    });
  };
  const openPeekList = () => {
    if (isDesktop && listCollapsed && !peekBlockedUntilRailExit) {
      setListPeekOpen(true);
    }
  };
  const closePeekList = () => {
    if (isDesktop && listCollapsed) {
      setListPeekOpen(false);
    }
  };
  const unlockPeekOnRailExit = () => {
    if (isDesktop && listCollapsed) {
      setPeekBlockedUntilRailExit(false);
    }
  };

  const brandName = summary ? (brandNames[summary.brand] ?? summary.brand) : routeBrandName;
  const routeContentLoading = loadedRouteKey !== `${brand}:${selectedDate}`;
  const activeLoadError = loadError?.routeKey === currentRouteKey ? loadError.message : null;
  const hasAnyQuery = (queryData?.queries?.length ?? 0) > 0;
  const currentDate = selectedDate || summary?.dataDate;
  const todayDate = dayjs().format('YYYY-MM-DD');
  const effectiveDate = currentDate || todayDate;
  const canGoNext = Boolean(currentDate && currentDate < todayDate);
  const assistantTask = detail?.task ?? queryData?.queries.find((item) => item.id === selectedTaskId) ?? null;
  const selectedTask = detail?.task ?? queryData?.queries.find((item) => item.id === selectedTaskId) ?? null;
  const detailTitle = selectedTask?.queryText ?? '';
  const listPinnedOpen = !listCollapsed;
  const listVisible = isMobile ? !listCollapsed : !listCollapsed || listPeekOpen;
  const detailOffset = isMobile ? 0 : (listPinnedOpen ? DESKTOP_LIST_WIDTH : 0);
  const workspaceStyle = {
    ['--ops-brand-detail-list-width' as const]: `${DESKTOP_LIST_WIDTH}px`,
    ['--ops-brand-detail-detail-offset' as const]: `${detailOffset}px`,
  } as React.CSSProperties;
  const switchDate = (nextDate: string) => {
    if (nextDate === selectedDate) return;
    resetDemotionInteraction();
    if (isMobile) setListCollapsed(false);
    setLoading(true);
    setQueryListLoading(true);
    setQueryListError(null);
    setLoadError(null);
    setDetailLoading(true);
    setQueryPage(1);
    setFilter('all');
    setSummary(null);
    queryDataRef.current = null;
    loadedRouteKeyRef.current = `${brand}:${nextDate}`;
    setQueryData(null);
    setDetail(null);
    setEvents([]);
    selectedTaskIdRef.current = null;
    setSelectedTaskId(null);
    setSelectedDate(nextDate);
    setSearchParams({ date: nextDate }, { replace: true });
  };
  const shiftDate = (days: number) => {
    const base = currentDate ? dayjs(currentDate) : dayjs();
    const nextDate = base.add(days, 'day').format('YYYY-MM-DD');
    if (nextDate > todayDate) return;
    switchDate(nextDate);
  };

  const handleSelectTask = React.useCallback((task: TopQueryTask) => {
    if (task.id !== selectedTaskId) {
      resetDemotionInteraction();
      detailRefreshSequenceRef.current += 1;
      setDetailLoading(true);
      selectedTaskIdRef.current = task.id;
      setSelectedTaskId(task.id);
    }
    if (isMobile) setListCollapsed(true);
  }, [isMobile, resetDemotionInteraction, selectedTaskId]);

  return {
    activeLoadError,
    assistantDrawerOpen,
    assistantTask,
    brand,
    brandName,
    canExecuteGovernanceActions,
    canGoNext,
    closePeekList,
    currentDate,
    currentRouteKey,
    demotionConflictMessage,
    demotionPreview,
    demotionPreviewError,
    demotionPreviewLoading,
    demotionPreviewOpen,
    demotionPreviewRouteKey,
    detail,
    detailLoading,
    detailOffset,
    detailTitle,
    effectiveDate,
    events,
    executingTaskIds,
    filter,
    handleFilterChange,
    handleCloseDemotionPreview,
    handleConfirmDemotion,
    handleExecuteTaskActions,
    handleExecuteKGPlan,
    handleCancelGovernancePlan,
    handleBlacklistProduct,
    handleBatchBlacklistProducts,
    handleCorrectProduct,
    handleRollbackAction,
    handleSelectTask,
    hasAnyQuery,
    isDesktop,
    isMobile,
    listCollapsed,
    listPaneRef,
    loadMoreQueries,
    listPeekOpen,
    listPinnedOpen,
    listVisible,
    loading,
    messageContextHolder,
    navigate,
    openRollbackCandidate,
    openPeekList,
    queries,
    queryData,
    queryListError,
    queryListLoading,
    rollbackCandidate,
    rollbackCandidateRouteKey,
    rollingBackActionId,
    routeContentLoading,
    selectedTask,
    selectedTaskId,
    setAssistantDrawerOpen,
    setFilter,
    retryQueryList,
    setListCollapsed,
    setRollbackCandidate,
    setRollbackCandidateRouteKey,
    shiftDate,
    summary,
    switchDate,
    todayDate,
    toggleListCollapsed,
    unlockPeekOnRailExit,
    workspaceStyle,
    workflowSteps,
    visibleCheckKeys,
  };
};
