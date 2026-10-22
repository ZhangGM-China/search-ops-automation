import { describe, expect, it } from 'vitest';
import { brandNames, httpStatus, isDateText, isInvalidatedTask, percent, stepHasIssue, stepIssueTypes, toFilterKey, withLiveRecallSnapshot } from './brandDetailUtils';

describe('brand detail utility rules', () => {
  it('formats dates and percentages defensively', () => {
    expect(brandNames.lumen).toBe('Lumen');
    expect(isDateText('2026-01-01')).toBe(true);
    expect(isDateText('bad')).toBe(false);
    expect(percent(0.125)).toBe('12.50%');
    expect(percent(12.5)).toBe('12.50%');
    expect(isDateText('2026-2-30')).toBe(false);
    expect(percent(null)).toBe('0.00%');
  });
  it('recognizes invalid tasks and HTTP errors', () => {
    expect(isInvalidatedTask({ invalidated: true } as never)).toBe(true);
    expect(isInvalidatedTask({ status: 'superseded' } as never)).toBe(true);
    expect(isInvalidatedTask({ displayStatus: '正常' } as never)).toBe(false);
    expect(httpStatus({ response: { status: 404 } })).toBe(404);
    expect(httpStatus(null)).toBeNull();
    expect(httpStatus({ response: { status: '500' } })).toBeNull();
  });
  it('maps filters, issues and live recall snapshots', () => {
    expect(toFilterKey({ displayStatus: 'normal' } as never)).toBe('normal');
    expect(toFilterKey({ displayStatus: 'completed' } as never)).toBe('fixed');
    expect(stepHasIssue({ displayStatus: '异常' } as never)).toBe(true);
    expect(stepIssueTypes({ evidence: { issue_types: ['a', 'b'] } } as never)).toEqual(['a', 'b']);
    expect(stepIssueTypes({ evidence: { reason: 'x' } } as never)).toEqual(['x']);
    const detail = { task: { resultCount: 1 }, liveRecall: { resultCount: 1, products: [] } } as never;
    expect(withLiveRecallSnapshot(detail, { resultCount: 2, products: [{ id: 1 }] }).task.resultCount).toBe(2);
    expect(withLiveRecallSnapshot(detail, null)).toBe(detail);
    expect(stepIssueTypes({ evidence: null } as never)).toEqual([]);
    expect(stepIssueTypes({ evidence: { type: 7 } } as never)).toEqual(['7']);
    expect(withLiveRecallSnapshot(detail, { resultCount: 0, products: undefined })?.liveRecall?.products).toEqual([]);
  });

  it('uses the live result limit as the shared display limit for both comparison columns', () => {
    const detail = {
      productDisplayLimit: 24,
      task: { resultCount: 1 },
    } as never;

    expect(withLiveRecallSnapshot(detail, {
      resultCount: 16,
      products: [],
      productDisplayLimit: 16,
    }).productDisplayLimit).toBe(16);
    expect(withLiveRecallSnapshot(detail, {
      resultCount: 24,
      products: [],
      productDisplayLimit: 24,
    }).productDisplayLimit).toBe(24);
    expect(withLiveRecallSnapshot(detail, {
      resultCount: null,
      products: [],
      productDisplayLimit: 16,
    }).productDisplayLimit).toBe(16);
  });
});
