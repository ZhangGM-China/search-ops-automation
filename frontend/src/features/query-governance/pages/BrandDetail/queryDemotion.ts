import type { UserInfo } from '@/shared/types';
import { isPlatformAdmin } from '@/shared/lib/permissions';

export const isQueryDemotionInteractionCurrent = (
  interactionRouteKey: string | null,
  currentRouteKey: string,
) => Boolean(interactionRouteKey && interactionRouteKey === currentRouteKey);

export const releaseMatchingActionId = (
  currentActionId: number | null,
  completedActionId: number,
) => currentActionId === completedActionId ? null : currentActionId;

export const releaseMatchingPreviewRequest = (
  currentRequestId: number | null,
  completedRequestId: number,
) => currentRequestId === completedRequestId ? null : currentRequestId;

export const getQueryDemotionRollbackFeedback = (status: string) => {
  if (status === 'rollback_failed') {
    return { type: 'error' as const, message: '回滚失败，需立即人工处理' };
  }
  if (status === 'rollback_pending') {
    return { type: 'warning' as const, message: '回滚已提交，等待权威配置核查' };
  }
  if (status === 'rolled_back') {
    return { type: 'success' as const, message: '已回滚并确认配置恢复' };
  }
  return {
    type: 'warning' as const,
    message: `回滚请求已处理，当前状态：${status || '待核查'}`,
  };
};

export const canExecuteQueryGovernanceActions = (user: UserInfo | null) => Boolean(
  user
  && (
    isPlatformAdmin(user)
    || user.actionPermissions?.includes('execute_governance_actions')
  )
);
