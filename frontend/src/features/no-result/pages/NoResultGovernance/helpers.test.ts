import { describe, expect, it } from 'vitest';
import { executionButtonText, hasExecutableActions } from './columns';
import { formatJson, getMobileStatusColor } from './constants';

describe('NoResultGovernance helpers', () => {
  it('maps execution states to operator actions', () => {
    expect(executionButtonText('running')).toBe('暂停任务');
    expect(executionButtonText('paused')).toBe('继续任务');
    expect(executionButtonText('completed')).toBe('已完成');
    expect(executionButtonText(undefined)).toBe('一键执行');
  });

  it('detects pending executable actions', () => {
    expect(hasExecutableActions({ actions: [{ status: 'pending_execute' }] } as never)).toBe(true);
    expect(hasExecutableActions({ actions: [{ status: 'completed' }] } as never)).toBe(false);
  });

  it('formats JSON and resolves mobile status colors', () => {
    expect(formatJson({ foo: 'bar' })).toContain('"foo": "bar"');
    expect(formatJson({})).toBe('{}');
    expect(getMobileStatusColor('completed')).toBe('success');
    expect(getMobileStatusColor('failed')).toBe('danger');
    expect(getMobileStatusColor('unknown')).toBe('primary');
  });
});
