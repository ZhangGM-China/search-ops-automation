import { describe, expect, it } from 'vitest';
import {
  buildSnapshotTitle, getProductBrokenRateStatus,
  getProductPriceDisplay, getProductPrimaryId, getProductRelevance,
  getProductRelevancePresentation, getSnapshotCount, getSnapshotProducts,
} from './productPresentation';
import { getProductImageCandidates } from './productImage';

describe('QueryDetail product presentation', () => {
  it('normalizes snapshot branches and product lists', () => {
    const value = { before: { resultCount: 123, items: [{ product_id: 'a' }, null] } };
    expect(getSnapshotCount(value, 'before')).toBe(123);
    expect(getSnapshotProducts(value, 'before')).toHaveLength(1);
    expect(getSnapshotCount({ after: { total: 2 } }, 'before')).toBe(2);
    expect(getSnapshotProducts({ results: [{ id: 1 }] }, 'after')).toHaveLength(1);
    expect(buildSnapshotTitle('标题', 1234, 16)).toContain('1,234');
    expect(buildSnapshotTitle('标题')).toBe('标题');
  });

  it('handles ids, prices, discounts and broken rate thresholds', () => {
    expect(getProductPrimaryId({ sku_id: ' sku-1 ' })).toBe('sku-1');
    expect(getProductPrimaryId({})).toBe('');
    expect(getProductPriceDisplay({ originalPrice: 100, discountPrice: 80, discount: .8 })).toEqual({ originalText: '¥100', saleText: '¥80', discountText: '80%', hasDiscount: true });
    expect(getProductPriceDisplay({ price: 9.5 })).toEqual({ originalText: '¥9.50', saleText: '¥9.50', discountText: '', hasDiscount: false });
    expect(getProductBrokenRateStatus({ broken_size_rate: .3 }, .25)?.tone).toBe('danger');
    expect(getProductBrokenRateStatus({ broken_size_rate: .1 }, .25)?.text).toBe('断码率 10%');
    expect(getProductBrokenRateStatus({}, .25)).toBeNull();
  });

  it('presents relevance signals and image candidates', () => {
    const product = { relevance: { is_relevant: false, reason: 'TextReason: 不相关 | ImageReason: 符合', status: 'checked' } };
    const relevance = getProductRelevance(product);
    expect(getProductRelevancePresentation(relevance)).toEqual({ text: '相关性不符', tone: 'warning' });
    expect(getProductRelevancePresentation(getProductRelevance({ relevance_check_status: 'check_failed', is_relevant: true }))).toEqual({ text: '相关性符合', tone: 'neutral' });
    expect(getProductRelevancePresentation(null)).toBeNull();
    expect(getProductImageCandidates({ mainImage: ' a ', images: [{ url: 'b' }, { src: 'a' }], media: { images: ['c'] } })).toEqual(['a', 'b', 'c']);
  });
});
