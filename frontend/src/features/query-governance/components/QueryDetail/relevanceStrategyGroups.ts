import type { TopQueryGovernanceCauseGroup, TopQueryGovernancePlan } from '@/shared/types';

export const productIdOf = (product: Record<string, unknown>) => String(product.productId || product.product_id || product.spu || product.spuId || '');
export const groupProducts = (group: TopQueryGovernanceCauseGroup, availableProducts: Record<string, unknown>[] = []) => {
  const explicit = [...group.products || [], ...group.affectedProducts || [], ...group.affected_products || []];
  const byId = new Map(availableProducts.map((product) => [productIdOf(product), product]));
  const products = new Map(explicit.map((product) => [productIdOf(product), { ...byId.get(productIdOf(product)), ...product }]));
  for (const id of [...group.productIds || [], ...group.product_ids || []]) {
    if (!products.has(String(id))) products.set(String(id), byId.get(String(id)) || { productId: id });
  }
  return [...products.values()];
};

export const relevanceStrategyPlans = (plans: TopQueryGovernancePlan[]) => {
  const primary = plans.find((plan) => plan.type !== 'blacklist'
    && (plan.actionType === 'relevance_plan' || plan.title.includes('相关性治理'))
    && (plan.causeGroups || plan.cause_groups || []).length);
  const blacklistPlans = plans.filter((plan) => plan.type === 'blacklist');
  const seen = new Set<string>();
  const ordered = [...plans.filter((item) => item.type !== 'blacklist'), ...blacklistPlans.filter(() => !primary)];
  const grouped = ordered.map((plan) => {
    const originalGroups = plan.causeGroups || plan.cause_groups || [];
    const isRelevance = plan.actionType === 'relevance_plan' || plan.title.includes('相关性治理');
    if (plan.type !== 'blacklist' && (!originalGroups.length || !isRelevance)) return { plan, available: [], groups: null };
    const available = [
      ...(plan.products || []),
      ...(plan.errorProductIds || plan.error_product_ids || []).map((productId) => ({ productId })),
      ...(plan === primary ? blacklistPlans.flatMap((item) => [...item.products || [], ...(item.causeGroups || item.cause_groups || []).flatMap((group) => groupProducts(group, item.products || []))]) : []),
    ];
    const groups = originalGroups.flatMap((group) => {
      const originalProducts = groupProducts(group, available);
      const products = originalProducts.filter((product) => {
        const id = productIdOf(product);
        if (id && seen.has(id)) return false;
        if (id) seen.add(id);
        return true;
      });
      if (originalProducts.length && !products.length) return [];
      return [{ ...group, products, productIds: products.map(productIdOf), product_ids: undefined }];
    });
    return { plan, available, groups };
  });
  return grouped.map(({ plan, available, groups }) => {
    if (!groups) return plan;
    const remaining = available.filter((product) => {
      const id = productIdOf(product);
      if (id && seen.has(id)) return false;
      if (id) seen.add(id);
      return true;
    });
    const standaloneBlacklist = plan.type === 'blacklist' && !(plan.causeGroups || plan.cause_groups || []).length
      && Boolean(plan.actionId || plan.actionIds?.length);
    if (remaining.length) groups.push({
      groupId: `${plan.id}:unclassified`, rootCause: standaloneBlacklist ? 'individual' : 'uncertain', products: remaining,
      productIds: remaining.map(productIdOf), product_ids: undefined,
      reasons: [standaloneBlacklist ? plan.reason || '相关性检测判定商品不相关' : '这些错误商品尚未被治理分析归入明确原因组'],
      conclusion: standaloneBlacklist ? '个例，需拉黑' : '需人工复核', executable: false,
    });
    return { ...plan, causeGroups: groups, cause_groups: undefined, products: groups.flatMap((group) => group.products) };
  });
};
