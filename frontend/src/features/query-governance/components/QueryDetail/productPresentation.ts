import { asRecord } from './productImage';

const numberText = (value?: number | null) => new Intl.NumberFormat('zh-CN').format(value ?? 0);
const wholePercent = (value?: number | null) => `${Math.round(((value ?? 0) <= 1 ? (value ?? 0) * 100 : (value ?? 0)))}%`;

const getSnapshotBranch = (value: unknown, branch: 'before' | 'after'): Record<string, unknown> => {
  const root = asRecord(value);
  const preferred = asRecord(root[branch]);
  if (Object.keys(preferred).length) return preferred;
  const fallbackBranch = branch === 'before' ? 'after' : 'before';
  const fallback = asRecord(root[fallbackBranch]);
  if (Object.keys(fallback).length) return fallback;
  return root;
};

const getSnapshotCount = (value: unknown, branch: 'before' | 'after') => {
  const body = getSnapshotBranch(value, branch);
  const count = body.result_count ?? body.resultCount ?? body.total;
  return typeof count === 'number' ? count : undefined;
};

const getSnapshotProducts = (value: unknown, branch: 'before' | 'after'): Record<string, unknown>[] => {
  const body = getSnapshotBranch(value, branch);
  const products = body.products ?? body.items ?? body.results;
  return Array.isArray(products) ? products.filter((item) => item && typeof item === 'object') as Record<string, unknown>[] : [];
};

const buildSnapshotTitle = (
  title: string,
  totalCount?: number | null,
  displayLimit = 16,
  availableProductCount?: number,
  immutableSnapshot = false,
) => {
  if (totalCount == null) return title;
  const visibleCount = availableProductCount == null
    ? displayLimit
    : Math.min(displayLimit, availableProductCount);
  if (totalCount <= visibleCount) return `${title}（总计 ${numberText(totalCount)} 个商品）`;
  if (immutableSnapshot && availableProductCount != null && availableProductCount < displayLimit) {
    return `${title}（总计 ${numberText(totalCount)} 个商品，快照仅保存前 ${numberText(availableProductCount)} 个）`;
  }
  return `${title}（总计 ${numberText(totalCount)} 个商品，仅展示前 ${numberText(visibleCount)} 个）`;
};

const getProductPriceNumber = (product: Record<string, unknown>) => {
  const price = product.price ?? product.discountPrice ?? product.salePrice;
  return typeof price === 'number' ? price : undefined;
};

const formatPriceValue = (value: unknown) => (
  typeof value === 'number' ? `¥${value.toFixed(value % 1 === 0 ? 0 : 2)}` : value ? String(value) : ''
);

const getProductPriceDisplay = (product: Record<string, unknown>) => {
  const original = typeof product.originalPrice === 'number'
    ? product.originalPrice
    : typeof product.price === 'number'
      ? product.price
      : undefined;
  const sale = typeof product.discountPrice === 'number'
    ? product.discountPrice
    : typeof product.salePrice === 'number'
      ? product.salePrice
      : undefined;
  const hasDiscount = original != null && sale != null && sale > 0 && sale < original;
  const explicitDiscount = typeof product.discount === 'number' ? product.discount : undefined;
  const discountRate = hasDiscount
    ? explicitDiscount != null && explicitDiscount > 0 && explicitDiscount <= 1
      ? Math.round(explicitDiscount * 100)
      : explicitDiscount != null && explicitDiscount > 1 && explicitDiscount <= 10
        ? Math.round(explicitDiscount * 10)
        : Math.round((sale / original) * 100)
    : undefined;
  return {
    originalText: formatPriceValue(original),
    saleText: formatPriceValue(hasDiscount ? sale : original),
    discountText: discountRate != null ? `${discountRate}%` : '',
    hasDiscount,
  };
};

const getProductPrimaryId = (product: Record<string, unknown>) => (
  [
    product.productId,
    product.product_id,
    product.spuId,
    product.spu,
    product.skuId,
    product.sku_id,
  ]
    .map((item) => String(item || '').trim())
    .find(Boolean) || ''
);

const getProductRelevance = (product: Record<string, unknown>) => {
  const explicit = asRecord(product.relevance);
  const isRelevant = explicit.isRelevant ?? explicit.is_relevant ?? product.is_relevant;
  const reason = explicit.reason ?? product.relevance_reason;
  const status = explicit.status ?? product.relevance_check_status;
  if (isRelevant == null && !reason && !status) return null;
  return {
    isRelevant: isRelevant === true,
    reason: reason ? String(reason) : '',
    status: status ? String(status) : '',
  };
};

const mismatchKeywords = /(不符|不相关|无关|失败|偏弱|异常|错误)/;
const matchKeywords = /(相关|符合|匹配|通过)/;

const getAgentSignal = (reason: string, agentLabel: 'TextReason' | 'ImageReason') => {
  const matcher = new RegExp(`${agentLabel}:\\s*([^|]+)`);
  const matched = reason.match(matcher);
  if (!matched) return null;
  const content = String(matched[1] || '').trim();
  const mismatch = mismatchKeywords.test(content);
  const match = !mismatch && matchKeywords.test(content);
  return { mismatch, match };
};

const getProductRelevancePresentation = (relevance: ReturnType<typeof getProductRelevance>) => {
  if (!relevance) return null;
  const reason = relevance.reason || '';
  const textSignal = getAgentSignal(reason, 'TextReason');
  const imageSignal = getAgentSignal(reason, 'ImageReason');
  const availableSignals = [textSignal, imageSignal].filter(Boolean) as Array<{ mismatch: boolean; match: boolean }>;
  const mismatchCount = availableSignals.filter((item) => item.mismatch).length;
  const matchCount = availableSignals.filter((item) => item.match).length;

  if (relevance.status === 'check_failed') {
    return {
      text: relevance.isRelevant ? '相关性符合' : '相关性不符',
      tone: relevance.isRelevant ? 'neutral' : 'danger',
    };
  }
  if (availableSignals.length === 1) {
    return {
      text: availableSignals[0].mismatch ? '相关性不符' : '相关性符合',
      tone: availableSignals[0].mismatch ? 'danger' : 'neutral',
    };
  }
  if (availableSignals.length >= 2) {
    if (mismatchCount >= 2) {
      return { text: '相关性不符', tone: 'danger' };
    }
    if (mismatchCount === 1 && matchCount >= 1) {
      return { text: '相关性不符', tone: 'warning' };
    }
    if (matchCount >= 2 || relevance.isRelevant) {
      return { text: '相关性符合', tone: 'neutral' };
    }
  }
  return {
    text: relevance.isRelevant ? '相关性符合' : '相关性不符',
    tone: relevance.isRelevant ? 'neutral' : 'danger',
  };
};

const getProductBrokenRateStatus = (product: Record<string, unknown>, threshold: number) => {
  const brokenRate = product.broken_size_rate;
  if (typeof brokenRate !== 'number') return null;
  return {
    text: `断码率 ${wholePercent(brokenRate)}`,
    tone: brokenRate >= threshold ? 'danger' as const : 'neutral' as const,
  };
};

export {
  getProductBrokenRateStatus,
  getProductPriceDisplay,
  getProductPriceNumber,
  getProductPrimaryId,
  getProductRelevance,
  getProductRelevancePresentation,
  getSnapshotCount,
  getSnapshotProducts,
  buildSnapshotTitle,
};
