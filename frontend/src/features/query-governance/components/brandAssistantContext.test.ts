import { describe, expect, it } from 'vitest';
import { buildAssistantSessionId, buildContextMeta, buildPageContext, buildPromptLeadText, buildQueryPrompts, dedupePrompts, pickPreferredBinding } from './brandAssistantContext';

describe('brand assistant context', () => {
  it('deduplicates prompts and chooses the strongest scoped model', () => {
    expect(dedupePrompts([' a ', 'a', '', 'b'])).toEqual([' a ', 'b']);
    expect(pickPreferredBinding([{ brand: 'atlas', modelName: 'glm-5.2' }, { brand: 'atlas', modelName: 'qwen3.7-max' }] as never, 'atlas')?.modelName).toBe('qwen3.7-max');
    expect(pickPreferredBinding([], 'atlas')).toBeNull();
  });
  it('builds prompts, session ids and page context from a task', () => {
    const task = { id: 3, queryText: '裙子', displayStatus: 'normal', resultCount: 5, ctr: 0.2, cvr: 0.1, searchPv: 9, stepStatuses: [{ checkKey: 'relevance', displayStatus: 'normal', summary: 'ok' }] } as never;
    expect(buildQueryPrompts(task)).toHaveLength(3);
    expect(buildQueryPrompts()).toHaveLength(2);
    expect(buildAssistantSessionId('brand_detail', 'atlas', task, '2026-01-01')).toContain('atlas-3');
    expect(buildPromptLeadText(task)).toContain('裙子');
    expect(buildPageContext('brand_detail', 'atlas', '2026-01-01', task)).toEqual(expect.objectContaining({ selectedQuery: '裙子', resultCount: 5, querySource: 'top_query' }));
  });
  it('builds compact summary metadata', () => {
    expect(buildContextMeta({ taskCount: 1, pendingCount: 2, actionCount: 3, monitoringCount: 4 })).toEqual([{ label: '待处理', value: 2 }, { label: '治理动作', value: 3 }, { label: '断码监测', value: 4 }]);
    expect(buildPromptLeadText()).toContain('品牌总览');
  });
});
