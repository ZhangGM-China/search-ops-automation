import { describe, expect, it } from 'vitest';

import { defaultRelevanceBuckets, relevanceBucketOptions } from './brandConfigOptions';

describe('relevance bucket options', () => {
  it('offers Dynamic as an opt-in bucket without changing the global defaults', () => {
    expect(relevanceBucketOptions).toContainEqual({ value: 'Dynamic', label: '动态召回' });
    expect(defaultRelevanceBuckets).not.toContain('Dynamic');
  });

  it('shows the business hierarchy in stable Mixed bucket order', () => {
    expect(relevanceBucketOptions).toEqual([
      { value: 'top', label: 'L0-1 置顶桶' },
      { value: 'productIdsByAgent', label: 'L0-2 Agent 选品' },
      { value: 'productIdsByCode', label: 'L1-1 编码完全召回' },
      { value: 'productIdsByProductNameTag', label: 'L1-2 商品名召回' },
      { value: 'productIdsByCodeSpellCorrections', label: 'L2-1 编码纠错召回' },
      { value: 'productIdsByTagsAll', label: 'L2-2 商品标签召回' },
      { value: 'productIdsByWhiteList', label: 'L2-3 白名单/主分类机器召回' },
      { value: 'productIdsBySubTag', label: 'L2-4 二次分词召回' },
      { value: 'productIdsByHighLowGroup', label: 'L3-1 高低组召回' },
      { value: 'productIdsByMultiCategory', label: 'L3-2 多分类召回' },
      { value: 'productIdsByTagNameDowngrade', label: 'L3-3 标签名降权召回' },
      { value: 'productIdsByOpsLowWeight', label: 'L3-4 运营低权重桶' },
      { value: 'productIdsByLowWeight', label: 'L3-5 低权重桶' },
      { value: 'productIdsByFuzzy', label: 'L3-6 模糊召回' },
      { value: 'productIdsByProductNameFuzzy', label: 'L3-7 商品名模糊召回' },
      { value: 'productIdsByTagFuzzy', label: 'L3-8 标签模糊召回' },
      { value: 'productIdsByFuzzySpellCorrections', label: 'L3-9 模糊纠错召回' },
      { value: 'Dynamic', label: '动态召回' },
      { value: 'productIdsByNoStock', label: 'L4 无库存桶' },
    ]);
  });
});
