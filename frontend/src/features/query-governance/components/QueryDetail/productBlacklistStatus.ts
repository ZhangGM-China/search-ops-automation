export type ProductBlacklistStatus = 'blacklisted' | 'not_blacklisted';

export const getProductBlacklistStatus = (
  product: Record<string, unknown>,
  fallback: ProductBlacklistStatus = 'not_blacklisted',
): ProductBlacklistStatus => {
  const value = String(
    product.blacklistStatus
      ?? product.blacklist_status
      ?? product.status
      ?? product.blackList
      ?? product.blackListAi
      ?? '',
  ).trim().toLowerCase();
  if (['not_blacklisted', 'cancelled', 'canceled', 'failed', 'failure', 'pending', 'pending_create', '待拉黑', '未拉黑', '拉黑失败'].includes(value) || value.includes('未拉黑') || value.includes('拉黑失败')) return 'not_blacklisted';
  if (['blacklisted', 'success', 'executed', '已拉黑'].includes(value) || (value.includes('blacklisted') && !value.includes('not_blacklisted')) || value.includes('已拉黑')) return 'blacklisted';
  return fallback;
};
