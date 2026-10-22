import { describe, expect, it } from 'vitest';
import {
  canExecuteQueryGovernanceActions, getQueryDemotionRollbackFeedback,
  isQueryDemotionInteractionCurrent, releaseMatchingActionId,
  releaseMatchingPreviewRequest,
} from './queryDemotion';

describe('query demotion state helpers', () => {
  it('releases only matching pending ids', () => {
    expect(releaseMatchingActionId(1, 1)).toBeNull();
    expect(releaseMatchingActionId(1, 2)).toBe(1);
    expect(releaseMatchingPreviewRequest(2, 2)).toBeNull();
    expect(releaseMatchingPreviewRequest(null, 2)).toBeNull();
  });
  it('matches interaction routes and authorization', () => {
    expect(isQueryDemotionInteractionCurrent('a', 'a')).toBe(true);
    expect(isQueryDemotionInteractionCurrent(null, 'a')).toBe(false);
    expect(canExecuteQueryGovernanceActions(null)).toBe(false);
    expect(canExecuteQueryGovernanceActions({ isAdmin: true } as never)).toBe(true);
    expect(canExecuteQueryGovernanceActions({ role: 'platform_admin' } as never)).toBe(true);
    expect(canExecuteQueryGovernanceActions({ actionPermissions: ['execute_governance_actions'] } as never)).toBe(true);
    expect(canExecuteQueryGovernanceActions({ role: 'viewer' } as never)).toBe(false);
  });
  it('maps every rollback state to user feedback', () => {
    expect(getQueryDemotionRollbackFeedback('rollback_failed').type).toBe('error');
    expect(getQueryDemotionRollbackFeedback('rollback_pending').type).toBe('warning');
    expect(getQueryDemotionRollbackFeedback('rolled_back').type).toBe('success');
    expect(getQueryDemotionRollbackFeedback('')).toEqual({ type: 'warning', message: '回滚请求已处理，当前状态：待核查' });
  });
});
