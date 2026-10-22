import React from 'react';
import { ArrowRightOutlined } from '@ant-design/icons';
import { Button, Checkbox, Empty, Flex, Input, Modal, Space, Tag, Tooltip, Typography } from 'antd';
import { dashboardApi } from '@/shared/api';
import type { TopQueryGovernanceCauseGroup, TopQueryGovernancePlan } from '@/shared/types';
import { ProductCardGrid } from './ProductComparison';
import { getProductBlacklistStatus, type ProductBlacklistStatus } from './productBlacklistStatus';
import RelevanceCorrectionVerification from './RelevanceCorrectionVerification';
import { useLatestManualRelevanceCheck } from './useLatestManualRelevanceCheck';
import { groupProducts, productIdOf, relevanceStrategyPlans } from './relevanceStrategyGroups';
import { KGActionSteps } from './KGActionSteps';

const { Text } = Typography;

const statusTone: Record<string, string | undefined> = {
  待执行: 'gold', 执行中: 'blue',
  'AI 已执行': 'green', 手动已执行: 'green',
  'AI 执行失败': 'red', 手动执行失败: 'red',
  结果待确认: 'gold', 回滚中: 'blue', 已回滚: 'default', 回滚失败: 'red', 已取消: 'default',
  生成中: 'blue', 生成失败: 'red', 无需调整: 'green', 需人工复核: 'gold',
};

const rateText = (value: unknown) => {
  if (value === null || value === undefined || value === '') return '--';
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${numeric <= 1 ? (numeric * 100).toFixed(2) : numeric.toFixed(2)}%` : String(value);
};

const causeGroupTitle = (rootCause: string) => {
  if (rootCause === 'individual') return '个例，需拉黑';
  if (rootCause === 'kg_issue') return '知识图谱调整策略';
  return '需人工复核';
};

const groupIdOf = (group: TopQueryGovernanceCauseGroup, index: number) => group.groupId || group.group_id || `cause-${index + 1}`;
const groupRootCause = (group: TopQueryGovernanceCauseGroup) => group.rootCause || group.root_cause || 'uncertain';
const groupActions = (group: TopQueryGovernanceCauseGroup) => group.actions || group.steps || [];
const groupActionIds = (group: TopQueryGovernanceCauseGroup) => {
  const listed = group.actionIds?.length ? group.actionIds : group.action_ids;
  if (listed?.length) return listed;
  const direct = group.actionId ?? group.action_id;
  if (direct != null) return [Number(direct)];
  return groupActions(group).flatMap((action) => {
    const actionId = action.actionId ?? action.action_id;
    return actionId == null ? [] : [Number(actionId)];
  });
};

const uniqueGroupProducts = (groups: TopQueryGovernanceCauseGroup[], availableProducts: Record<string, unknown>[] = []) => {
  const seen = new Set<string>();
  return groups.flatMap((group) => groupProducts(group, availableProducts)).filter((product, index) => {
    const id = String(product.productId || product.product_id || product.spu || product.spuId || `index-${index}`);
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
};

const isGeneratedReorderStrategy = (value: string | null | undefined) => {
  const strategy = value?.trim();
  return Boolean(strategy && !['AI 生成排序策略', '暂无生成策略'].includes(strategy));
};
const reorderFailureStatuses = new Set(['AI 执行失败', '手动执行失败', '回滚失败']);
const fallbackBlacklistStatus = (status: string, statusLabel: string): 'blacklisted' | 'not_blacklisted' => (
  status === 'verified' || ['AI 已执行', '手动已执行'].includes(statusLabel) ? 'blacklisted' : 'not_blacklisted'
);

export const GovernancePlans: React.FC<{
  plans: TopQueryGovernancePlan[];
  displayLimit: number;
  inventoryBrokenRateThreshold: number;
  onOpenProduct: (product: Record<string, unknown>) => void;
  onExecutePlan?: (plan: TopQueryGovernancePlan) => void;
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
  query?: string;
  brand?: string;
  verificationProducts?: Array<Record<string, unknown>>;
  executing?: boolean;
}> = ({ plans, displayLimit, inventoryBrokenRateThreshold, onOpenProduct, onExecutePlan, onCancelPlan, onBlacklistProduct, onBatchBlacklistProducts, onCorrectProduct, query, brand, verificationProducts = [], executing = false }) => {
  const [openProductPlanId, setOpenProductPlanId] = React.useState<string | null>(null);
  const [correctionTarget, setCorrectionTarget] = React.useState<{ plan: TopQueryGovernancePlan; product: Record<string, unknown> } | null>(null);
  const [correctionRule, setCorrectionRule] = React.useState('');
  const [blacklistActionLoadingProductId, setBlacklistActionLoadingProductId] = React.useState<string | null>(null);
  const [selectedProductIds, setSelectedProductIds] = React.useState<Set<string>>(() => new Set());
  const [selectedBlacklistStatus, setSelectedBlacklistStatus] = React.useState<ProductBlacklistStatus | null>(null);
  const [batchBlacklistActionLoadingPlanId, setBatchBlacklistActionLoadingPlanId] = React.useState<string | null>(null);
  const [currentRules, setCurrentRules] = React.useState('');
  const { check: verificationCheck, setCheck: setVerificationCheck } = useLatestManualRelevanceCheck(brand, query);
  React.useEffect(() => {
    if (!correctionTarget || !brand || !query) return;
    let active = true;
    void dashboardApi.getRelevanceGuidance(brand, query).then((response) => {
      if (active) setCurrentRules(response.data.rules || '');
    }).catch(() => { if (active) setCurrentRules(''); });
    return () => { active = false; };
  }, [correctionTarget, brand, query]);
  const closeCorrectionModal = () => { setCorrectionTarget(null); setCorrectionRule(''); };
  const toggleProductSelection = (product: Record<string, unknown>, selected: boolean, fallback: ProductBlacklistStatus) => {
    const productId = productIdOf(product);
    if (!productId) return;
    const status = getProductBlacklistStatus(product, fallback);
    if (selected && selectedBlacklistStatus && status !== selectedBlacklistStatus) return;
    if (!selected && selectedProductIds.size === 1 && selectedProductIds.has(productId)) setSelectedBlacklistStatus(null);
    if (selected) setSelectedBlacklistStatus(status);
    setSelectedProductIds((current) => {
      const next = new Set(current);
      if (selected) next.add(productId); else next.delete(productId);
      return next;
    });
  };
  const selectedProductsFor = (products: Record<string, unknown>[]) => products.filter((product) => selectedProductIds.has(productIdOf(product)));
  const getGroupSelectionState = (products: Record<string, unknown>[], fallback: ProductBlacklistStatus) => {
    const validProducts = products.filter((product) => Boolean(productIdOf(product)));
    const targetStatus: ProductBlacklistStatus | null = selectedBlacklistStatus
      ?? (validProducts.length ? getProductBlacklistStatus(validProducts[0], fallback) : null);
    const selectableProducts = targetStatus
      ? validProducts.filter((product) => getProductBlacklistStatus(product, fallback) === targetStatus)
      : [];
    const selectedCount = selectableProducts.filter((product) => selectedProductIds.has(productIdOf(product))).length;
    const allSelected = selectableProducts.length > 0 && selectedCount === selectableProducts.length;
    const indeterminate = selectedCount > 0 && selectedCount < selectableProducts.length;
    const disabled = Boolean(batchBlacklistActionLoadingPlanId) || selectableProducts.length === 0;
    return { validProducts, targetStatus, selectableProducts, allSelected, indeterminate, disabled };
  };
  const toggleGroupSelection = (products: Record<string, unknown>[], fallback: ProductBlacklistStatus) => {
    const { validProducts, targetStatus, selectableProducts, allSelected, disabled } = getGroupSelectionState(products, fallback);
    if (disabled) return;
    if (allSelected) {
      const groupIds = new Set(validProducts.map(productIdOf));
      const remaining = new Set([...selectedProductIds].filter((id) => !groupIds.has(id)));
      setSelectedProductIds(remaining);
      if (!remaining.size) setSelectedBlacklistStatus(null);
      return;
    }
    if (!targetStatus || !selectableProducts.length) return;
    setSelectedBlacklistStatus(targetStatus);
    setSelectedProductIds((current) => {
      const next = new Set(current);
      for (const product of selectableProducts) {
        next.add(productIdOf(product));
      }
      return next;
    });
  };
  const selectedBatchAction: 'blacklist' | 'cancel' = selectedBlacklistStatus === 'not_blacklisted' ? 'blacklist' : 'cancel';
  const selectedBatchActionLabel = selectedBatchAction === 'cancel' ? '批量取消拉黑' : '批量拉黑';
  const runBatchAction = async (plan: TopQueryGovernancePlan, products: Record<string, unknown>[], action: 'blacklist' | 'cancel') => {
    if (!onBatchBlacklistProducts || !products.length || batchBlacklistActionLoadingPlanId) return;
    setBatchBlacklistActionLoadingPlanId(plan.id);
    try {
      const succeeded = await onBatchBlacklistProducts(plan, products, action);
      if (succeeded === true) {
        const completedIds = new Set(products.map(productIdOf));
        const remaining = new Set([...selectedProductIds].filter((id) => !completedIds.has(id)));
        setSelectedProductIds(remaining);
        if (!remaining.size) setSelectedBlacklistStatus(null);
      }
    } finally {
      setBatchBlacklistActionLoadingPlanId(null);
    }
  };
  const handleBlacklistAction = async (plan: TopQueryGovernancePlan, product: Record<string, unknown>, action: 'blacklist' | 'cancel') => {
    if (!onBlacklistProduct) return;
    const productId = String(product.productId || product.product_id || product.skuId || product.sku_id || '');
    setBlacklistActionLoadingProductId(productId);
    try { await onBlacklistProduct(plan, product, action, undefined); } finally { setBlacklistActionLoadingProductId(null); }
  };
  const confirmCorrection = async (mode: 'only' | 'cancel_blacklist') => {
    if (!correctionTarget || !onCorrectProduct || !correctionRule.trim()) return;
    const productId = productIdOf(correctionTarget.product);
    setBlacklistActionLoadingProductId(productId);
    try {
      const succeeded = await onCorrectProduct(correctionTarget.plan, correctionTarget.product, correctionRule.trim(), mode);
      if (succeeded !== false) closeCorrectionModal();
    } finally { setBlacklistActionLoadingProductId(null); }
  };
  const correctionBlacklisted = String(correctionTarget?.product.blacklistStatus ?? correctionTarget?.product.status ?? (correctionTarget ? fallbackBlacklistStatus(String(correctionTarget.plan.status || ''), String(correctionTarget.plan.statusLabel || '')) : '')).toLowerCase() === 'blacklisted';
  const blacklistPlans = plans.filter((plan) => plan.type === 'blacklist');
  const productsOfPlan = (plan: TopQueryGovernancePlan) => [
    ...(plan.products || []),
    ...(plan.causeGroups || plan.cause_groups || []).flatMap((group) => groupProducts(group, plan.products || [])),
  ];
  const blacklistByProductId = new Map(
    blacklistPlans.flatMap((plan) => productsOfPlan(plan).map((product) => [productIdOf(product), plan] as const))
      .filter(([productId]) => Boolean(productId)),
  );
  const productLookup = new Map(
    [...plans.filter((plan) => plan.type !== 'blacklist'), ...blacklistPlans]
      .flatMap(productsOfPlan)
      .map((product) => [productIdOf(product), product] as const)
      .filter(([productId]) => Boolean(productId)),
  );
  const enrichProducts = (items: Record<string, unknown>[]) => items.map((item) => {
    const fallback = productLookup.get(productIdOf(item)) || {};
    const blacklistPlan = blacklistByProductId.get(productIdOf(item));
    return {
      ...fallback,
      ...item,
      imageUrl: item.imageUrl || item.image_url || fallback.imageUrl || fallback.image_url,
      blacklistStatus: blacklistPlan
        ? getProductBlacklistStatus(fallback, fallbackBlacklistStatus(String(blacklistPlan.status || ''), String(blacklistPlan.statusLabel || '')))
        : item.blacklistStatus || fallback.blacklistStatus,
    };
  });
  const visiblePlans = relevanceStrategyPlans(plans);
  let strategyIndex = 0;
  if (!visiblePlans.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无待执行治理方案" />;
  return (
    <>
    <Flex vertical gap={12} className="ops-governance-plans">
      {visiblePlans.map((plan) => {
        const products = enrichProducts((plan.products || []) as Record<string, unknown>[]);
        const metrics = plan.metrics || {};
        const causeGroups = plan.causeGroups || plan.cause_groups || [];
        const groupedIndividualProductIds = causeGroups
          .filter((group) => groupRootCause(group) === 'individual')
          .flatMap((group) => group.productIds || group.product_ids || groupProducts(group).map(productIdOf))
          .map(String)
          .filter(Boolean);
        const groupedBlacklistCompleted = causeGroups.length > 0
          && causeGroups.every((group) => groupRootCause(group) === 'individual')
          && groupedIndividualProductIds.length > 0
          && groupedIndividualProductIds.every((productId) => blacklistByProductId.get(productId)?.status === 'verified');
        const status = groupedBlacklistCompleted ? 'AI 已执行' : plan.statusLabel || plan.status || '待执行';
        const errorProductIds = plan.errorProductIds || plan.error_product_ids || [];
        const isBlacklist = plan.type === 'blacklist';
        const isKgGenerationFailure = plan.type === 'kg' && (status === '生成失败' || plan.status === 'failed');
        const hasReorderStrategy = plan.type === 'reorder' && isGeneratedReorderStrategy(plan.summary);
        const isReorderFailure = plan.type === 'reorder' && (reorderFailureStatuses.has(status) || ['failed', 'rollback_failed'].includes(plan.status));
        const isErrorProductPlan = isBlacklist || causeGroups.length > 0 || errorProductIds.length > 0;
        const allErrorProducts = products.length
          ? products
          : errorProductIds.length
            ? enrichProducts(errorProductIds.map((productId) => ({ productId })))
            : enrichProducts(uniqueGroupProducts(causeGroups, products));
        const selectedErrorProducts = selectedProductsFor(allErrorProducts);
        const displayTitle = isErrorProductPlan ? `错误商品（${allErrorProducts.length}）` : isBlacklist ? `${plan.title}（${products.length}）` : plan.title;
        const flatFallbackStatus = plan.type === 'blacklist'
          ? fallbackBlacklistStatus(String(plan.status || ''), String(plan.statusLabel || ''))
          : 'not_blacklisted';
        const flatSelectionState = isErrorProductPlan && !causeGroups.length
          ? getGroupSelectionState(allErrorProducts, flatFallbackStatus)
          : null;
        const hasReorderContent = plan.type === 'reorder' && (hasReorderStrategy || (isReorderFailure && Boolean(plan.reason)));
        const canOpenReorderProducts = hasReorderStrategy && products.length > 0;
        const openReorderProducts = () => setOpenProductPlanId(plan.id);
        const reorderContent = hasReorderContent ? <>
          {hasReorderStrategy ? <div className="ops-governance-plan__strategy">{plan.summary}</div> : null}
          {isReorderFailure && plan.reason ? <div className="ops-governance-plan__reason">错误原因：{plan.reason}</div> : null}
          {canOpenReorderProducts ? <Button type="link" size="small" className="ops-governance-plan__detail-link" aria-label="查看详情" onClick={(event) => { event.stopPropagation(); openReorderProducts(); }}>查看详情</Button> : null}
        </> : null;
        return (
          <section className="ops-governance-plan ops-governance-plan--flat" key={plan.id} data-plan-type={plan.type}>
            {!causeGroups.length ? <Flex className="ops-governance-plan__header" justify="space-between" align="flex-start" gap={12} wrap={!hasReorderContent}>
              <div>
                <Flex align="center" gap={8}>
                  {isErrorProductPlan && onBatchBlacklistProducts && flatSelectionState ? (
                    <Checkbox
                      className="ops-governance-cause-group__select-all"
                      checked={flatSelectionState.allSelected}
                      indeterminate={flatSelectionState.indeterminate}
                      disabled={flatSelectionState.disabled}
                      aria-label="全选当前错误商品"
                      onChange={() => toggleGroupSelection(allErrorProducts, flatFallbackStatus)}
                    />
                  ) : null}
                  <Text strong className="ops-governance-plan__title">{displayTitle}</Text>
                </Flex>
                {!isKgGenerationFailure && !isErrorProductPlan && plan.type !== 'reorder' && plan.summary ? <div className="ops-governance-plan__summary">{plan.summary}</div> : null}
              </div>
              <Space size={6} wrap={!hasReorderContent}>
                {!isErrorProductPlan ? <Tag color={statusTone[status]}>{status}</Tag> : null}
                {isErrorProductPlan && !causeGroups.length && onBatchBlacklistProducts ? (
                  <Button
                    type="link"
                    size="small"
                    loading={batchBlacklistActionLoadingPlanId === plan.id}
                    disabled={!selectedErrorProducts.length || Boolean(batchBlacklistActionLoadingPlanId)}
                    onClick={() => void runBatchAction(plan, selectedErrorProducts, selectedBatchAction)}
                  >
                    {selectedBatchActionLabel}
                  </Button>
                ) : null}
                {plan.type === 'kg' && (plan.actionType === 'relevance_plan' || plan.title.includes('相关性治理 Agent')) && brand && query ? (
                  <RelevanceCorrectionVerification
                    brand={brand}
                    query={query}
                    products={verificationProducts}
                    displayLimit={displayLimit}
                    inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
                    onOpenProduct={onOpenProduct}
                    check={verificationCheck}
                    onCheckChange={setVerificationCheck}
                  />
                ) : null}
                {!isKgGenerationFailure && plan.executable && !causeGroups.length && onExecutePlan ? <Button size="small" type="primary" loading={executing} onClick={() => onExecutePlan(plan)}>执行</Button> : null}
                {!isKgGenerationFailure && plan.type !== 'blacklist' && plan.cancelable && onCancelPlan ? <Button size="small" danger loading={executing} onClick={() => onCancelPlan(plan)}>取消</Button> : null}
              </Space>
            </Flex> : null}
            {canOpenReorderProducts ? <div className="ops-governance-plan__content ops-governance-plan__content--reorder" role="button" tabIndex={0} aria-label={`重排序商品卡片，共 ${products.length} 个`} onClick={openReorderProducts} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openReorderProducts(); } }}>{reorderContent}</div> : hasReorderContent ? <div className="ops-governance-plan__content">{reorderContent}</div> : null}
            {!isKgGenerationFailure && !isErrorProductPlan && plan.type !== 'reorder' && plan.reason ? <div className="ops-governance-plan__reason">原因：{plan.reason}</div> : null}
            {!isKgGenerationFailure && !isErrorProductPlan && plan.type === 'kg' && plan.steps?.length ? (
              plan.steps.some((step) => step.actionType || step.action_type) ? <KGActionSteps brand={brand} actions={plan.steps} /> : <ol className="ops-governance-plan__steps">{plan.steps.map((step) => <li key={`${step.index}-${step.title}`}><strong>{step.title}</strong><span>{step.description}</span></li>)}</ol>
            ) : null}
            {isErrorProductPlan && causeGroups.length ? (
              <div className="ops-governance-cause-groups">
                {causeGroups.map((group, index) => {
                  const groupId = groupIdOf(group, index);
                  const rootCause = groupRootCause(group);
                  const groupedProducts = enrichProducts(groupProducts(group, allErrorProducts));
                  const actions = groupActions(group).filter((action) => (action.actionType || action.action_type) !== 'product_blacklist');
                  const actionIds = groupActionIds(group);
                  const matchingBlacklistPlans = groupedProducts.map((product) => blacklistByProductId.get(productIdOf(product)));
                  const hasExplicitUnblacklistedProduct = groupedProducts.some((product: Record<string, unknown>) => {
                    const value = String(product.blacklistStatus ?? product.status ?? '').toLowerCase();
                    return ['not_blacklisted', 'pending', 'failed', 'failure'].includes(value);
                  });
                  const completedBlacklistPlan = rootCause === 'individual' && matchingBlacklistPlans.length > 0
                    && matchingBlacklistPlans.every((item) => item?.status === 'verified')
                    && !hasExplicitUnblacklistedProduct
                    ? matchingBlacklistPlans[0] : null;
                  const groupStatus = rootCause === 'kg_issue' && !actions.length ? '需人工复核' : group.statusLabel || group.status_label || group.status || status;
                  const derivedPlan: TopQueryGovernancePlan = {
                    ...plan,
                    id: `${plan.id}:${groupId}`,
                    actionId: actionIds[0] ?? null,
                    actionIds,
                    causeGroupId: groupId,
                    title: causeGroupTitle(rootCause),
                    summary: group.conclusion || null,
                    executable: Boolean(!completedBlacklistPlan && group.executable),
                    status: completedBlacklistPlan?.status || group.status || plan.status,
                    statusLabel: completedBlacklistPlan?.statusLabel || group.statusLabel || group.status_label || plan.statusLabel,
                    products: groupedProducts,
                  };
                  const groupedFallbackStatus = rootCause === 'individual' || plan.type === 'blacklist'
                    ? fallbackBlacklistStatus(String(derivedPlan.status || ''), String(derivedPlan.statusLabel || ''))
                    : 'not_blacklisted';
                  const groupSelectionState = getGroupSelectionState(groupedProducts, groupedFallbackStatus);
                  return (
                    <section className="ops-governance-cause-group" data-testid="governance-cause-group" key={groupId}>
                      <Flex justify="space-between" align="flex-start" gap={10} wrap>
                        <h3 className="ops-governance-cause-group__title">策略 {++strategyIndex}</h3>
                        <Space size={6} wrap>
                          {rootCause === 'kg_issue' ? <Tag color={statusTone[String(groupStatus)]}>{groupStatus}</Tag> : null}
                          {rootCause === 'kg_issue' && actions.length > 0 && group.executable && !isKgGenerationFailure && onExecutePlan ? <Button size="small" type="primary" loading={executing} onClick={() => onExecutePlan(derivedPlan)}>一键执行</Button> : null}
                        </Space>
                      </Flex>
                      {rootCause === 'uncertain' ? <div className="ops-governance-cause-group__conclusion">需人工复核</div> : null}
                      {rootCause === 'individual' ? <div className="ops-governance-cause-group__conclusion">个例，需拉黑</div> : null}
                      {rootCause === 'kg_issue' ? <KGActionSteps brand={brand} actions={actions} /> : null}
                      {group.validationErrors?.length ? <div role="alert" className="ops-governance-plan__empty-steps">策略生成失败：{group.validationErrors.join('；')}</div> : null}
                      {(
                        <>
                          <Flex className="ops-governance-cause-group__products-header" justify="space-between" align="center" gap={8} wrap>
                            <Flex align="center" gap={8} className="ops-governance-cause-group__products-title-row">
                              {onBatchBlacklistProducts ? (
                                <Checkbox
                                  className="ops-governance-cause-group__select-all"
                                  checked={groupSelectionState.allSelected}
                                  indeterminate={groupSelectionState.indeterminate}
                                  disabled={groupSelectionState.disabled}
                                  aria-label="全选当前错误商品"
                                  onChange={() => toggleGroupSelection(groupedProducts, groupedFallbackStatus)}
                                />
                              ) : null}
                              <h4 className="ops-governance-cause-group__products-title">错误商品（{groupedProducts.length}）</h4>
                            </Flex>
                            <Space size={4}>
                              {onBatchBlacklistProducts ? <Button type="link" size="small" loading={batchBlacklistActionLoadingPlanId === derivedPlan.id} disabled={!selectedProductsFor(groupedProducts).length || Boolean(batchBlacklistActionLoadingPlanId)} onClick={() => void runBatchAction(derivedPlan, selectedProductsFor(groupedProducts), selectedBatchAction)}>{selectedBatchActionLabel}</Button> : null}
                              {brand && query ? <RelevanceCorrectionVerification brand={brand} query={query} products={groupedProducts} displayLimit={displayLimit} inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} check={verificationCheck} onCheckChange={setVerificationCheck} /> : null}
                            </Space>
                          </Flex>
                          <ProductCardGrid products={groupedProducts} emptyDescription="暂无错误商品" displayLimit={displayLimit} variant="snapshot" inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} onCorrectProduct={onCorrectProduct ? (product) => { setCorrectionTarget({ plan: derivedPlan, product }); setCorrectionRule(''); } : undefined} onBlacklistAction={onBlacklistProduct ? (product, action) => { void handleBlacklistAction(blacklistByProductId.get(productIdOf(product)) || derivedPlan, product, action); } : undefined} selectedProductIds={selectedProductIds} onToggleProductSelection={onBatchBlacklistProducts ? (product, selected) => toggleProductSelection(product, selected, groupedFallbackStatus) : undefined} selectionStatus={selectedBlacklistStatus} selectionDisabled={Boolean(batchBlacklistActionLoadingPlanId)} defaultBlacklistStatus={groupedFallbackStatus} blacklistActionLoadingProductId={blacklistActionLoadingProductId} showReason />
                        </>
                      )}
                    </section>
                  );
                })}
              </div>
            ) : null}
            {!isKgGenerationFailure && plan.evidence?.length ? (
              <div className="ops-governance-plan__evidence">
                {plan.evidence.map((item, index) => <Tag key={`${item.label}-${index}`} color={item.tone === 'danger' ? 'red' : item.tone === 'warning' ? 'gold' : undefined}>{item.label}：{item.value}</Tag>)}
              </div>
            ) : null}
            {plan.type === 'demotion_restore' ? (
              <div className="ops-governance-plan__rate-change"><span>执行降桶时断码率 {rateText(metrics.fromRate)}</span><ArrowRightOutlined /><span>恢复时断码率 {rateText(metrics.toRate)}</span></div>
            ) : plan.type === 'demotion' ? (
              <div className="ops-governance-plan__rate-change"><span>执行降桶时断码率 {rateText(metrics.fromRate)}</span></div>
            ) : null}
            {!isKgGenerationFailure && products.length && !causeGroups.length && (plan.type !== 'reorder' || hasReorderStrategy) ? (
              plan.type === 'reorder' ? (
                <>
                  <Modal
                    title="重排序商品列表"
                    open={openProductPlanId === plan.id}
                    centered
                    footer={null}
                    onCancel={() => setOpenProductPlanId(null)}
                    width={760}
                    className="ops-fixed-detail-modal ops-reorder-products-modal"
                    destroyOnHidden
                  >
                    <div className="ops-reorder-products-modal__strategy">
                      <Text strong>AI 重排序策略</Text>
                      <div>{plan.summary}</div>
                    </div>
                    <div className="ops-reorder-products-modal__list">
                      <ProductCardGrid products={products} emptyDescription="暂无重排序商品" displayLimit={displayLimit} variant="snapshot" inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} />
                    </div>
                  </Modal>
                </>
            ) : (
                <>
                  <ProductCardGrid products={products} emptyDescription="暂无涉及商品" displayLimit={displayLimit} variant="snapshot" inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} onCorrectProduct={isErrorProductPlan && onCorrectProduct ? (product) => { setCorrectionTarget({ plan, product }); setCorrectionRule(''); } : undefined} onBlacklistAction={isErrorProductPlan && onBlacklistProduct ? (product, action) => { void handleBlacklistAction(plan, product, action); } : undefined} selectedProductIds={selectedProductIds} onToggleProductSelection={isErrorProductPlan && onBatchBlacklistProducts ? (product, selected) => toggleProductSelection(product, selected, plan.type === 'blacklist' ? fallbackBlacklistStatus(String(plan.status || ''), String(plan.statusLabel || '')) : 'not_blacklisted') : undefined} selectionStatus={selectedBlacklistStatus} selectionDisabled={Boolean(batchBlacklistActionLoadingPlanId)} defaultBlacklistStatus={isErrorProductPlan ? plan.type === 'blacklist' ? fallbackBlacklistStatus(String(plan.status || ''), String(plan.statusLabel || '')) : 'not_blacklisted' : undefined} blacklistActionLoadingProductId={blacklistActionLoadingProductId} showReason={plan.type === 'blacklist' || plan.type === 'kg'} />
                </>
              )
            ) : null}
            {!products.length && !causeGroups.length && plan.type !== 'kg' && !plan.steps?.length ? <Text type="secondary">暂无涉及商品</Text> : null}
          </section>
        );
      })}
    </Flex>
    <Modal
      title="补充Query相关性检测规则"
      open={Boolean(correctionTarget)}
      centered
      rootClassName="ops-cancel-blacklist-modal"
      footer={correctionBlacklisted ? [
        <Button key="both" autoInsertSpace={false} type="primary" onClick={() => { void confirmCorrection('cancel_blacklist'); }} loading={Boolean(blacklistActionLoadingProductId)} disabled={!correctionRule.trim()}>纠正并取消拉黑</Button>,
        <Button key="only" autoInsertSpace={false} onClick={() => { void confirmCorrection('only'); }} loading={Boolean(blacklistActionLoadingProductId)} disabled={!correctionRule.trim()}>仅纠正</Button>,
      ] : [
        <Button key="cancel" autoInsertSpace={false} onClick={closeCorrectionModal} disabled={Boolean(blacklistActionLoadingProductId)}>取消</Button>,
        <Button key="confirm" autoInsertSpace={false} type="primary" onClick={() => { void confirmCorrection('only'); }} loading={Boolean(blacklistActionLoadingProductId)} disabled={!correctionRule.trim()}>确认</Button>,
      ]}
      onCancel={closeCorrectionModal}
      styles={{ body: { paddingBottom: 32 } }}
      destroyOnHidden
    >
      <div className="ops-correction-modal__hint">新规则会追加到当前Query的相关性规则末尾。<Tooltip title={<span className="ops-current-rules-tooltip">{currentRules.trim() || '暂无相关性规则'}</span>} trigger="hover"><Button type="link" size="small" className="ops-correction-modal__rules-button">查看当前规则</Button></Tooltip></div>
      <Input.TextArea
        value={correctionRule}
        onChange={(event) => setCorrectionRule(event.target.value)}
        placeholder="请输入相关性纠正规则"
        autoSize={{ minRows: 4, maxRows: 8 }}
        maxLength={2000}
        showCount
      />
    </Modal>
    </>
  );
};
