import { isPlatformAdmin } from '@/shared/lib/permissions'
import type { UserInfo } from '@/shared/types'

export type AccountOption = {
  label: string
  value: string
}

const platformActionPermissionValues = new Set(['manage_platform_users', 'manage_platform_config'])

export const accountPages: AccountOption[] = [
  { label: '品牌总览', value: '/ops-dashboard' },
  { label: '相关性检测', value: '/blacklist' },
  { label: '断码检测', value: '/demotion' },
  { label: '断码追踪', value: '/stock-tracking' },
  { label: '无结果词', value: '/no-result-governance' },
  { label: '智能运营助理', value: '/smart-assistant' },
  { label: '账号管理', value: '/account-management' },
  { label: '系统配置', value: '/config' },
]

export const accountBrands: AccountOption[] = [
  { label: 'ATLAS', value: 'atlas' },
  { label: 'Meadow', value: 'meadow' },
  { label: 'Harbor', value: 'harbor' },
  { label: 'Summit', value: 'summit' },
  { label: 'Riviera', value: 'riviera' },
  { label: 'Lumen', value: 'lumen' },
]

const actionPermissionOptions: AccountOption[] = [
  { label: '管理品牌账号', value: 'manage_brand_users' },
  { label: '管理品牌配置', value: 'manage_brand_config' },
  { label: '执行治理动作', value: 'execute_governance_actions' },
  { label: '管理无结果执行', value: 'manage_no_result_execution' },
  { label: '撤销治理动作', value: 'cancel_governance_actions' },
  { label: '管理平台账号', value: 'manage_platform_users' },
  { label: '管理平台配置', value: 'manage_platform_config' },
]

export const roleLabel = (user: Pick<UserInfo, 'username' | 'isAdmin' | 'role'>) => {
  if (isPlatformAdmin(user)) return '超级管理员'
  if (user.role === 'brand_admin') return '品牌管理员'
  if (user.isAdmin || user.role === 'platform_admin') return '管理员'
  return '子账号'
}

export const getRoleOptions = (canAssignPrivilegedRoles: boolean): AccountOption[] => (
  canAssignPrivilegedRoles
    ? [
        { label: '超级管理员', value: 'platform_admin' },
        { label: '品牌管理员', value: 'brand_admin' },
        { label: '品牌成员', value: 'brand_member' },
      ]
    : [{ label: '品牌成员', value: 'brand_member' }]
)

export const getVisibleActionPermissionOptions = (role?: string): AccountOption[] => (
  role === 'platform_admin'
    ? actionPermissionOptions
    : actionPermissionOptions.filter((item) => !platformActionPermissionValues.has(item.value))
)

export const normalizeActionPermissionsForRole = (actionPermissions: unknown, role?: string): string[] => {
  const values = Array.isArray(actionPermissions) ? actionPermissions.map((item) => String(item)) : []
  return role === 'platform_admin'
    ? values
    : values.filter((item) => !platformActionPermissionValues.has(item))
}

export const createDefaultAccountValues = (): Partial<UserInfo> => ({
  role: 'brand_member',
  actionPermissions: [],
  pagePermissions: ['/ops-dashboard', '/blacklist', '/demotion', '/stock-tracking', '/no-result-governance', '/smart-assistant'],
  brandPermissions: accountBrands.map((brand) => brand.value),
})
