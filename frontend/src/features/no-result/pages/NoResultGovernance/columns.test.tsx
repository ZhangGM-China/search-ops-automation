import { describe, expect, it } from 'vitest';
import {
  executionButtonText,
  hasExecutableActions,
} from './columns';
import type { NoResultGovernanceItem } from '@/shared/types';

const baseRecord: NoResultGovernanceItem = {
  id: 1,
  batchId: 9,
  dataDate: '2026-07-09',
  brand: 'atlas',
  query: '露营天幕',
  status: 'pending',
  aiDecision: 'process',
  aiReason: '购物意图明确',
  finalStrategy: '',
  beforeResultCount: 0,
  afterResultCount: 8,
  searchPv: 42,
  searchUv: 21,
  executionStatus: 'idle',
  actions: [
    {
      id: 1001,
      actionType: 'create_relationship',
      targetInterface: 'tagRelationship/createRelationship',
      requestPayload: { relationType: 'subordinate' },
      responsePayload: { code: 0 },
      beforeSnapshot: { existed: false },
      rollbackPayload: { relationId: 'rel-1001' },
      status: 'pending_execute',
      rollbackStatus: 'pending',
      riskLevel: 'medium',
      agentReason: '图谱关系缺失，需要补建',
      createdAt: '2026-07-09 10:00:00',
    },
  ],
  agentTimeline: [],
  createdAt: '2026-07-09 10:00:00',
  processedAt: '2026-07-09 10:05:00',
};

describe('NoResultGovernance columns', () => {
  it('maps execution status to button text and detects executable actions', () => {
    expect(executionButtonText('running')).toBe('暂停任务');
    expect(executionButtonText('paused')).toBe('继续任务');
    expect(executionButtonText('completed')).toBe('已完成');
    expect(executionButtonText(undefined)).toBe('一键执行');
    expect(hasExecutableActions(baseRecord)).toBe(true);
    expect(hasExecutableActions({ ...baseRecord, actions: [] })).toBe(false);
  });
});
