import { describe, expect, it } from 'vitest'

import {
  accountBrands,
  getRoleOptions,
  getVisibleActionPermissionOptions,
  normalizeActionPermissionsForRole,
} from './accountPermissions'

describe('account permissions', () => {
  it('includes Lumen in selectable brand permissions', () => {
    expect(accountBrands).toContainEqual({ label: 'Lumen', value: 'lumen' })
  })
  it('removes platform-only permissions when a user is downgraded from platform admin', () => {
    expect(normalizeActionPermissionsForRole(
      ['manage_brand_config', 'manage_platform_users', 'manage_platform_config'],
      'brand_member',
    )).toEqual(['manage_brand_config'])
  })

  it('keeps platform permissions available only to privileged role editors', () => {
    expect(getRoleOptions(false)).toEqual([{ label: '品牌成员', value: 'brand_member' }])
    expect(getVisibleActionPermissionOptions('brand_admin').map((item) => item.value)).not.toContain('manage_platform_users')
    expect(getVisibleActionPermissionOptions('platform_admin').map((item) => item.value)).toContain('manage_platform_users')
    expect(getVisibleActionPermissionOptions('platform_admin').map((item) => item.value)).not.toContain('manage_assistant_access')
  })
})
