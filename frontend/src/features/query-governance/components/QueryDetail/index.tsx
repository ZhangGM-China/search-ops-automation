import React from 'react';
import { Button, Empty, Flex, Tabs, Tag, message } from 'antd';
import type { TabsProps } from 'antd';
import { dashboardApi } from '@/shared/api';
import type { OpsTaskProductApiDetail, TopQueryAction, TopQueryDetailData, TopQueryEvent, TopQueryGovernancePlan } from '@/shared/types';
import { getApiErrorMessage } from '@/shared/lib/error';
import { usePageConfigBoolean, usePageConfigMetrics } from '@/shared/pageConfig/context';
import { QueryDetailOverlays } from './QueryDetailOverlays';
import { SnapshotComparison } from './ProductComparison';
import { GovernancePlans } from './GovernancePlans';
import { getProductPrimaryId } from './productPresentation';
import RelevanceGuidancePanel from './RelevanceGuidancePanel';
import ExecutionOverview from './ExecutionOverview';
import { buildExecutionOverview, type ExecutionOverviewNode } from './executionOverviewModel';

const percent = (value?: number | null) => `${(((value ?? 0) <= 1 ? (value ?? 0) * 100 : (value ?? 0))).toFixed(2)}%`;
const numberText = (value?: number | null) => new Intl.NumberFormat('zh-CN').format(value ?? 0);
const relativeChange = (value?: number | null, baseline?: number | null) => {
  if (value == null || baseline == null) return null;
  if (baseline === 0) return value === 0 ? 0 : null;
  return ((value - baseline) / Math.abs(baseline)) * 100;
};
const relativeChangeText = (value?: number | null, baseline?: number | null) => {
  const change = relativeChange(value, baseline);
  if (change == null) return null;
  return `较近期 ${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
};
const fallbackPlanStatusLabel = (action: TopQueryAction): TopQueryGovernancePlan['statusLabel'] | null => {
  if (action.status === 'pending') return '待执行';
  if (action.status === 'executing') return '执行中';
  if (action.status === 'execution_unknown') return '结果待确认';
  if (action.status === 'rollback_pending') return '回滚中';
  if (action.status === 'rolled_back') return '已回滚';
  if (action.status === 'rollback_failed') return '回滚失败';
  if (action.status === 'rejected') return '已取消';
  if (action.status === 'verified') return action.executorType === 'human' ? '手动已执行' : action.executorType === 'ai_auto' ? 'AI 已执行' : null;
  if (action.status === 'failed') return action.executorType === 'human' ? '手动执行失败' : action.executorType === 'ai_auto' ? 'AI 执行失败' : null;
  return null;
};
const fallbackPlanFromAction = (action: TopQueryAction): TopQueryGovernancePlan | null => {
  const type = action.actionType === 'query_reorder' ? 'reorder'
    : ['product_blacklist', 'blacklist'].includes(action.actionType) ? 'blacklist'
      : action.actionType === 'query_demote' ? 'demotion' : 'kg';
  const titles = { reorder: 'AI 重排序策略', blacklist: '错误商品', demotion: '降桶商品', kg: '召回数量治理策略' } as const;
  const executable = action.status === 'pending';
  const statusLabel = fallbackPlanStatusLabel(action);
  if (!statusLabel) return null;
  return {
    id: `action-${action.id}`,
    taskId: action.taskId,
    actionId: action.id,
    actionIds: [action.id],
    actionType: action.actionType,
    type,
    title: titles[type],
    objective: '',
    summary: action.reason || action.outcome?.summary || null,
    reason: action.reason,
    steps: [],
    evidence: [],
    executionMode: action.executionMode,
    executorType: action.executorType,
    executorUserId: action.executorUserId,
    executedAt: action.executedAt,
    failureStage: action.failureStage,
    executable,
    cancelable: ['blacklist', 'demotion'].includes(type)
      && action.executionMode === 'auto'
      && action.status === 'verified',
    status: action.status as TopQueryGovernancePlan['status'],
    statusLabel: (action.statusLabel as TopQueryGovernancePlan['statusLabel']) || statusLabel,
    products: [],
  };
};
const QueryDetail: React.FC<{
  detail?: TopQueryDetailData | null;
  events?: TopQueryEvent[];
  onRollbackAction?: (action: TopQueryAction) => void;
  rollingBackActionId?: number | null;
  onExecuteTaskActions?: (taskId: number, actionIds?: number[], causeGroupId?: string) => void;
  onExecuteKGPlan?: (plan: TopQueryGovernancePlan) => void;
  onCancelPlan?: (plan: TopQueryGovernancePlan) => void;
  onBlacklistProduct?: (
    plan: TopQueryGovernancePlan,
    product: Record<string, unknown>,
    action: 'blacklist' | 'cancel',
    reason?: string,
  ) => boolean | Promise<boolean>;
  onBatchBlacklistProducts?: (
    plan: TopQueryGovernancePlan,
    products: Array<Record<string, unknown>>,
    action: 'blacklist' | 'cancel',
    reason?: string,
  ) => boolean | Promise<boolean>;
  onCorrectProduct?: (plan: TopQueryGovernancePlan, product: Record<string, unknown>, rule: string, mode: 'only' | 'cancel_blacklist') => boolean | Promise<boolean>;
  taskActionPreviewLoading?: boolean;
  taskActionExecuting?: boolean;
  hideTitleBar?: boolean;
}> = ({
  detail,
  events = [],
  onRollbackAction,
  onExecuteTaskActions,
  onExecuteKGPlan,
  onCancelPlan,
  onBlacklistProduct,
  onBatchBlacklistProducts,
  onCorrectProduct,
  taskActionPreviewLoading = false,
  taskActionExecuting = false,
  hideTitleBar = false,
}) => {
  const showQueryMetrics = usePageConfigBoolean('brand_detail.yesterday.show');
  const showExecutionOverview = usePageConfigBoolean('brand_detail.show_execution_overview');
  const showGovernancePlans = usePageConfigBoolean('brand_detail.tabs.show_governance_plans');
  const showResultComparison = usePageConfigBoolean('brand_detail.tabs.show_result_comparison');
  const showRelevanceRules = usePageConfigBoolean('brand_detail.tabs.show_relevance_rules');
  const yesterdayMetrics = usePageConfigMetrics('brand_detail.yesterday.metrics');
  const [trendDrawerOpen, setTrendDrawerOpen] = React.useState(false);
  const [executionOverviewModal, setExecutionOverviewModal] = React.useState<ExecutionOverviewNode | null>(null);
  const [productModalOpen, setProductModalOpen] = React.useState(false);
  const [selectedProduct, setSelectedProduct] = React.useState<Record<string, unknown> | null>(null);
  const [selectedProductDetail, setSelectedProductDetail] = React.useState<OpsTaskProductApiDetail | null>(null);
  const [productDetailLoading, setProductDetailLoading] = React.useState(false);
  const [relevanceGuidanceRefreshToken, setRelevanceGuidanceRefreshToken] = React.useState(0);
  const productRequestSequenceRef = React.useRef(0);
  if (!detail) {
    return (
      <div className="ops-query-detail-section ops-query-detail-section--empty">
        <Empty description="请选择 Query" />
      </div>
    );
  }

  const { task, actions, trend = [] } = detail;
  const invalidated = Boolean(
    task.invalidated
    || ['cancelled', 'superseded'].includes(String(task.status || '').toLowerCase())
    || String(task.displayStatus || '').toLowerCase() === 'superseded'
  );
  const afterSnapshot = detail.liveRecall ? {
    after: {
      result_count: detail.liveRecall.resultCount,
      products: detail.liveRecall.products,
    },
  } : undefined;
  const beforeSnapshot = detail.s0 ? { before: detail.s0 } : undefined;
  const productDisplayLimit = detail.productDisplayLimit ?? 16;
  const inventoryBrokenRateThreshold = detail.inventoryBrokenRateThreshold ?? 0.25;
  const verificationProducts = detail.liveRecall?.products?.length ? detail.liveRecall.products : detail.s0?.products ?? [];

  const latestTrend = [...trend].reverse().find((point) => point.date === task.dataDate) ?? trend.at(-1);
  const historyDays = Math.max(1, detail.historyDays ?? 7);
  const previousTrend = trend
    .filter((point) => point !== latestTrend && (!task.dataDate || point.date < task.dataDate))
    .slice(-historyDays);
  const weightedRate = (numerator: Array<number | undefined>, denominator: Array<number | undefined>) => {
    const totalNumerator = numerator.reduce<number>((sum, value) => sum + (value ?? 0), 0);
    const totalDenominator = denominator.reduce<number>((sum, value) => sum + (value ?? 0), 0);
    return totalDenominator > 0 ? totalNumerator / totalDenominator : null;
  };
  const recentPv = previousTrend.length ? previousTrend.reduce((sum, point) => sum + (point.pv ?? 0), 0) / previousTrend.length : null;
  const recentUv = previousTrend.length ? previousTrend.reduce((sum, point) => sum + (point.uv ?? 0), 0) / previousTrend.length : null;
  const recentCtr = weightedRate(previousTrend.map((point) => point.clickUv), previousTrend.map((point) => point.uv));
  const recentCvr = weightedRate(previousTrend.map((point) => point.purchaseUv), previousTrend.map((point) => point.uv));
  const recentAtc = weightedRate(previousTrend.map((point) => point.addToCartUv), previousTrend.map((point) => point.uv));
  const yesterdayMetricValues: Record<string, { label: string; value: string; delta?: string | null }> = {
    pv: { label: 'PV', value: numberText(latestTrend?.pv ?? task.searchPv), delta: relativeChangeText(latestTrend?.pv ?? task.searchPv, recentPv) },
    uv: { label: 'UV', value: numberText(latestTrend?.uv ?? task.searchUv), delta: relativeChangeText(latestTrend?.uv ?? task.searchUv, recentUv) },
    ctr: { label: 'CTR', value: percent(latestTrend?.ctr ?? task.ctr), delta: relativeChangeText(latestTrend?.ctr ?? task.ctr, recentCtr ?? task.avgCtr) },
    cvr: { label: 'CVR', value: percent(latestTrend?.cvr ?? task.cvr), delta: relativeChangeText(latestTrend?.cvr ?? task.cvr, recentCvr ?? task.avgCvr) },
    atc: { label: 'ATC', value: latestTrend?.atc == null ? '—' : percent(latestTrend.atc), delta: relativeChangeText(latestTrend?.atc, recentAtc) },
    clickUv: { label: '点击 UV', value: latestTrend?.clickUv == null ? '—' : numberText(latestTrend.clickUv) },
    addToCartUv: { label: '加购 UV', value: latestTrend?.addToCartUv == null ? '—' : numberText(latestTrend.addToCartUv) },
    purchaseUv: { label: '购买 UV', value: latestTrend?.purchaseUv == null ? '—' : numberText(latestTrend.purchaseUv) },
  };
  const metricCards = yesterdayMetrics.flatMap((key) => yesterdayMetricValues[key] ? [yesterdayMetricValues[key]] : []);
  const executionOverview = buildExecutionOverview({ steps: detail.stepStatuses, nodes: detail.nodes, s0: detail.s0, events, actions, inventory: detail.inventory, inventoryExecution: detail.inventoryExecution, inventoryCheckIntervalMinutes: detail.inventoryCheckIntervalMinutes, recoveredTodayCount: detail.recoveredTodayCount, inventoryScans: detail.inventoryScans, recall: detail.recall, relevance: detail.relevance, reorder: detail.reorder ?? undefined, visibleCheckKeys: detail.visibleCheckKeys });

  const handleOpenProduct = async (product: Record<string, unknown>) => {
    const productId = getProductPrimaryId(product);
    const requestSequence = productRequestSequenceRef.current + 1;
    productRequestSequenceRef.current = requestSequence;
    setSelectedProduct(product);
    setSelectedProductDetail(null);
    setProductModalOpen(true);
    if (!productId) return;
    setProductDetailLoading(true);
    try {
      const response = await dashboardApi.getTopQueryProductDetail(task.brand, productId, task.id);
      if (productRequestSequenceRef.current !== requestSequence) return;
      setSelectedProductDetail(response.data.product);
    } catch (error: unknown) {
      if (productRequestSequenceRef.current !== requestSequence) return;
      message.error(getApiErrorMessage(error, '获取商品详情失败'));
    } finally {
      if (productRequestSequenceRef.current === requestSequence) {
        setProductDetailLoading(false);
      }
    }
  };

  const providedPlans = (detail.governancePlans ?? []).filter((plan) => Boolean(plan.type && plan.title));
  const governancePlans = providedPlans.length
    ? providedPlans
    : actions.map(fallbackPlanFromAction).filter((plan): plan is TopQueryGovernancePlan => plan !== null);
  const executePlan = (plan: TopQueryGovernancePlan) => {
    // AI 召回率补召回动作与 KG 方案共用 /api/query-governance/actions/{id}/execute 受控执行端点。
    if ((plan.type === 'kg' || plan.type === 'whitelist_tag' || plan.type === 'query_whitelist') && onExecuteKGPlan) {
      onExecuteKGPlan(plan);
      return;
    }
    if (!onExecuteTaskActions) return;
    onExecuteTaskActions(
      task.id,
      plan.actionIds?.length ? plan.actionIds : plan.actionId ? [plan.actionId] : undefined,
      plan.causeGroupId,
    );
  };
  const cancelPlan = (plan: TopQueryGovernancePlan) => {
    const action = actions.find((candidate) => plan.actionIds?.includes(candidate.id) || candidate.id === plan.actionId);
    if (action) onRollbackAction?.(action);
  };

  const allItems: TabsProps['items'] = [
    {
      key: 'plans',
      label: '治理方案',
      children: <GovernancePlans plans={governancePlans} displayLimit={productDisplayLimit} inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={handleOpenProduct} onExecutePlan={onExecuteTaskActions || onExecuteKGPlan ? executePlan : undefined} onCancelPlan={onCancelPlan ?? (onRollbackAction ? cancelPlan : undefined)} onBlacklistProduct={onBlacklistProduct} onBatchBlacklistProducts={onBatchBlacklistProducts} onCorrectProduct={onCorrectProduct} query={task.queryText} brand={task.brand} verificationProducts={verificationProducts} executing={taskActionExecuting} />,
    },
    {
      key: 'result',
      label: '效果对比',
      children: (
        <SnapshotComparison
          afterSnapshot={afterSnapshot}
          beforeSnapshot={beforeSnapshot}
          fallbackCount={task.resultCount}
          displayLimit={productDisplayLimit}
          inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
          onOpenProduct={handleOpenProduct}
        />
      ),
    },
    {
      key: 'relevance-guidance',
      label: '相关性规则',
      children: <RelevanceGuidancePanel brand={task.brand} query={task.queryText} refreshToken={relevanceGuidanceRefreshToken} />,
    },
  ];
  const items = allItems.filter((item) => (
    (item?.key === 'plans' && showGovernancePlans)
    || (item?.key === 'result' && showResultComparison)
    || (item?.key === 'relevance-guidance' && showRelevanceRules)
  ));

  return (
    <Flex vertical gap={0} className="ops-query-detail-drawer">
      {!hideTitleBar ? (
        <div className="ops-query-detail-title-bar">
          <div className="ops-query-detail-title-bar__name">{task.queryText}</div>
        </div>
      ) : null}
      <div className="ops-query-detail-body">
      {invalidated ? (
        <div className="ops-query-detail-section">
          <Tag>已失效</Tag>
          <div className="ops-query-detail-note" style={{ marginTop: 6 }}>
            该历史任务已失效，仅展示当前实时召回，不恢复原治理任务。
          </div>
        </div>
      ) : null}
      {showQueryMetrics ? <section className="ops-query-detail-section ops-query-detail-section--metrics">
        <Flex justify="space-between" align="center" gap={12} className="ops-query-detail-section__title-row">
          <div className="ops-query-detail-section__title">昨日数据</div>
          <Button type="text" size="small" className="ops-query-detail-inline-action" onClick={() => setTrendDrawerOpen(true)}>
            数据明细
          </Button>
        </Flex>
        <div className="ops-query-metric-row">
          {metricCards.map(({ label, value, delta }) => (
            <span className="ops-query-metric-row__item" key={label}>
              <small>{label}</small>
              <strong>{value}</strong>
              {delta ? <small className={`ops-query-metric-row__delta${delta.includes('+') ? ' ops-query-metric-row__delta--positive' : delta.includes('-') ? ' ops-query-metric-row__delta--negative' : ''}`}>{delta}</small> : null}
            </span>
          ))}
        </div>
      </section> : null}

      {showExecutionOverview ? <ExecutionOverview nodes={executionOverview} onOpen={setExecutionOverviewModal} /> : null}

      {items.length ? <div className="ops-surface ops-query-detail-tabs ops-query-detail-tabs--sticky">
        <Tabs className="ops-query-record-tabs" defaultActiveKey={items[0]?.key} size="small" items={items} onChange={(key) => {
          if (key === 'relevance-guidance') setRelevanceGuidanceRefreshToken((current) => current + 1);
        }} tabBarExtraContent={showGovernancePlans && (onExecuteTaskActions || onExecuteKGPlan) && governancePlans.some((plan) => plan.executable) ? <Button type="primary" size="small" loading={taskActionPreviewLoading || taskActionExecuting} disabled={taskActionPreviewLoading || taskActionExecuting} onClick={() => {
          const executableKG = governancePlans.find((plan) => plan.type === 'kg' && plan.executable);
          if (executableKG && onExecuteKGPlan) onExecuteKGPlan(executableKG);
          else onExecuteTaskActions?.(task.id);
        }}>一键执行</Button> : null} />
      </div> : null}
      </div>
      <QueryDetailOverlays
        executionOverviewModal={executionOverviewModal}
        onCloseExecutionOverview={() => setExecutionOverviewModal(null)}
        onCloseProduct={() => {
          productRequestSequenceRef.current += 1;
          setProductModalOpen(false);
          setSelectedProduct(null);
          setSelectedProductDetail(null);
        }}
        onOpenProduct={handleOpenProduct}
        onCloseTrendDrawer={() => setTrendDrawerOpen(false)}
        productDetailLoading={productDetailLoading}
        productModalOpen={productModalOpen}
        selectedProduct={selectedProduct}
        selectedProductDetail={selectedProductDetail}
        task={task}
        trend={trend}
        inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
        trendDrawerOpen={showQueryMetrics && trendDrawerOpen}
      />
    </Flex>
  );
};

export default QueryDetail;
