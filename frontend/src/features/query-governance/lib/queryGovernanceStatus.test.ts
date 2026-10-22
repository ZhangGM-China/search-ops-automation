import { describe, expect, it } from 'vitest';
import {
  checkStateLabel,
  checkStateTone,
  getTopQueryStatusLabel,
  hasStepIssueEvidence,
  resolveCheckExecutionState,
  resolveCheckState,
  type CheckState,
} from './queryGovernanceStatus';

const states: CheckState[] = ['failed', 'pending', 'not_run', 'attention', 'normal'];

describe('check state model', () => {
  it('maps each state to exactly one label and one color', () => {
    expect(states.map(checkStateLabel)).toEqual(['异常', '待处理', '未执行', '需关注', '正常']);
    expect(states.map(checkStateTone)).toEqual(['red', 'yellow', 'default', 'blue', 'green']);
  });

  it('resolves the check execution state from the check result only', () => {
    expect(resolveCheckExecutionState()).toBe('not_run');
    expect(resolveCheckExecutionState({})).toBe('not_run');
    expect(resolveCheckExecutionState({ status: 'executed', displayStatus: 'executed' })).toBe('normal');
    expect(resolveCheckExecutionState({ displayStatus: '已执行' })).toBe('normal');
    expect(resolveCheckExecutionState({ status: 'pass' })).toBe('normal');
    expect(resolveCheckExecutionState({ status: 'running' })).toBe('pending');
    expect(resolveCheckExecutionState({ status: 'executing' })).toBe('pending');
    expect(resolveCheckExecutionState({ status: 'failed' })).toBe('failed');
    expect(resolveCheckExecutionState({ status: 'error' })).toBe('failed');
    expect(resolveCheckExecutionState({ status: 'still_problematic' })).toBe('failed');
    expect(resolveCheckExecutionState({ status: 'skipped' })).toBe('not_run');
    expect(resolveCheckExecutionState({ status: 'cancelled' })).toBe('not_run');
    expect(resolveCheckExecutionState({ displayStatus: '未执行' })).toBe('not_run');
    expect(resolveCheckExecutionState({ displayStatus: '已跳过' })).toBe('not_run');
    expect(resolveCheckExecutionState({ displayStatus: '异常' })).toBe('failed');
  });

  it('detects issue evidence from the check payload', () => {
    expect(hasStepIssueEvidence(undefined)).toBe(false);
    expect(hasStepIssueEvidence({ issueTypes: [] })).toBe(false);
    expect(hasStepIssueEvidence({ issueTypes: ['inventory'] })).toBe(true);
    expect(hasStepIssueEvidence({ issue_types: ['low_result'] })).toBe(true);
    expect(hasStepIssueEvidence({ type: 'relevance' })).toBe(true);
    expect(hasStepIssueEvidence({ should_reorder: true })).toBe(true);
    expect(hasStepIssueEvidence({ mismatch_count: 3 })).toBe(true);
    expect(hasStepIssueEvidence({ mismatch_count: 0 })).toBe(false);
    expect(hasStepIssueEvidence({ affectedProductCount: '2' })).toBe(true);
  });

  it.each([
    ['a check without any record', {}, false, 'not_run'],
    ['a successful check', { status: 'executed' }, false, 'normal'],
    ['a successful check with a detected issue', { status: 'executed' }, true, 'attention'],
    ['a fixed check', { status: 'fixed' }, false, 'attention'],
    ['a running check', { status: 'running' }, false, 'pending'],
    ['a failed check', { status: 'error' }, true, 'failed'],
    ['a skipped check that already detected an issue', { status: 'skipped' }, true, 'not_run'],
    ['a check waiting for its governance plan', { status: 'executed', governanceStatus: { status: 'pending' } }, true, 'pending'],
    ['a check whose governance plan runs', { status: 'executed', governanceStatus: { status: 'executing' } }, false, 'pending'],
    ['a check whose governance failed', { status: 'executed', governanceStatus: { status: 'failed' } }, false, 'failed'],
    ['a check whose governance was rolled back', { status: 'executed', governanceStatus: { status: 'rolled_back' } }, true, 'attention'],
    ['a check whose governance was rejected without an issue', { status: 'executed', governanceStatus: { status: 'rejected' } }, false, 'normal'],
  ])('resolves %s', (_name, step, hasDetectedIssue, expected) => {
    expect(resolveCheckState(step as never, hasDetectedIssue)).toBe(expected);
  });
});

describe('top query status labels', () => {
  it('maps empty, known and unknown task statuses', () => {
    expect(getTopQueryStatusLabel()).toBe('正常');
    expect(getTopQueryStatusLabel(null)).toBe('正常');
    ['normal', 'pass', 'completed', 'fixed', 'waiting_human_approval', 'manual_required', 'warning', 'still_problematic', 'failed', 'check_failed', 'cancelled', 'superseded'].forEach((status) => expect(getTopQueryStatusLabel(status as never)).toBeTruthy());
    expect(getTopQueryStatusLabel('unknown' as never)).toBe('处理后仍有问题');
  });
  it.each([
    [undefined, '正常'], ['normal', '正常'], ['completed', '已处理'], ['fixed', '已改善'],
    ['waiting_human_approval', '需处理待确认'], ['still_problematic', '处理后仍有问题'], ['cancelled', '已失效'], ['unknown', '处理后仍有问题'],
  ])('maps %s', (status, label) => expect(getTopQueryStatusLabel(status as never)).toBe(label));
});