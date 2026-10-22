import type { NoResultGovernanceItem } from '@/shared/types';

export const executionButtonText = (status?: string) => {
  if (status === 'running') return '暂停任务';
  if (status === 'paused') return '继续任务';
  if (status === 'completed') return '已完成';
  return '一键执行';
};

export const hasExecutableActions = (record: NoResultGovernanceItem) => record.actions.some((action) => action.status === 'pending_execute');
