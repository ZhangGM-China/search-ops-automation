import { describe, expect, it } from 'vitest';
import type { TopQueryGovernancePlan } from '@/shared/types';
import { groupProducts, relevanceStrategyPlans } from './relevanceStrategyGroups';

const plan = (id: string, groups: unknown[], products: unknown[] = [], extra = {}) => ({ id, type: 'kg', actionType: 'relevance_plan', title: '相关性治理 Agent 调整方案', causeGroups: groups, products, ...extra }) as TopQueryGovernancePlan;
const assignments = (plans: TopQueryGovernancePlan[]) => plans.flatMap(p => (p.causeGroups || []).flatMap(g => (g.products || []).map(product => [g.groupId, product.productId || product.product_id])));

describe('relevance strategy product membership', () => {
  it('retains snake-case members even when camel-case arrays are empty', () => {
    const groups = [{ groupId: 'kg', rootCause: 'kg_issue', productIds: [], product_ids: ['P1'], products: [], affected_products: [{ product_id: 'P2' }] }];
    const plans = relevanceStrategyPlans([plan('relevance', groups)]);
    expect(assignments(plans)).toEqual([['kg', 'P2'], ['kg', 'P1']]);
    expect(groupProducts(groups[0]).map(p => p.productId || p.product_id)).toEqual(['P2', 'P1']);
  });

  it('keeps recall KG products outside the relevance strategy partition', () => {
    const recall = plan('recall', [{ groupId: 'recall-kg', rootCause: 'kg_issue', productIds: ['P1'] }], [{ productId: 'P1' }], { actionType: 'kg_plan', title: '召回数量治理策略' });
    const plans = relevanceStrategyPlans([recall, plan('relevance', [{ groupId: 'relevance-kg', rootCause: 'kg_issue', productIds: ['P1'] }])]);
    expect(plans[0]).toBe(recall);
    expect(assignments(plans.slice(1))).toEqual([['relevance-kg', 'P1']]);
  });

  it('assigns later explicit strategies before filling any unclassified candidates', () => {
    const plans = relevanceStrategyPlans([
      plan('one', [{ groupId: 'kg', rootCause: 'kg_issue', productIds: ['P1'] }], [{ productId: 'P1' }, { productId: 'P2' }, { productId: 'P3' }]),
      plan('two', [{ groupId: 'individual', rootCause: 'individual', products: [{ productId: 'P2' }] }]),
    ]);
    expect(assignments(plans)).toEqual([['kg', 'P1'], ['one:unclassified', 'P3'], ['individual', 'P2']]);
  });

  it('keeps group-only candidate products not covered by the final analysis', () => {
    const plans = relevanceStrategyPlans([
      plan('final', [{ groupId: 'kg', rootCause: 'kg_issue', productIds: ['P1'] }]),
      plan('candidate', [{ groupId: 'old', rootCause: 'uncertain', product_ids: ['P1', 'P2'], affected_products: [{ product_id: 'P2', productName: '剩余商品' }] }], [], { type: 'blacklist', actionType: 'product_blacklist' }),
    ]);
    expect(assignments(plans)).toEqual([['kg', 'P1'], ['final:unclassified', 'P2']]);
  });

  it('keeps standalone blacklist facts as an individual strategy without hiding themselves', () => {
    const plans = relevanceStrategyPlans([plan('executed', [], [{ productId: 'P1' }, { productId: 'P1' }], { type: 'blacklist', actionType: 'product_blacklist', actionIds: [90] })]);
    expect(assignments(plans)).toEqual([['executed:unclassified', 'P1']]);
    expect(plans[0].causeGroups?.[0].rootCause).toBe('individual');
  });
});
