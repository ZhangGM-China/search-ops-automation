import type { UserInfo } from '@/shared/types';
import { isPlatformAdmin } from '@/shared/lib/permissions';

const routeActionPermissionMap: Record<string, string[]> = {};

export const hasPagePermission = (user: UserInfo, path: string) => isPlatformAdmin(user) || user.pagePermissions.includes(path);

export const hasRouteActionPermission = (user: UserInfo, path: string) => {
  if (isPlatformAdmin(user)) return true;
  const requiredActions = routeActionPermissionMap[path];
  if (!requiredActions || requiredActions.length === 0) return true;
  const actionPermissions = user.actionPermissions || [];
  return requiredActions.some((permission) => actionPermissions.includes(permission));
};

export const roleDisplayLabel = (user: UserInfo) => {
  if (isPlatformAdmin(user)) return '超级管理员';
  if (user.role === 'brand_admin') return '品牌管理员';
  if (user.role === 'brand_member') return '品牌成员';
  return user.isAdmin || user.role === 'platform_admin' ? '管理员账号' : '运营账号';
};
