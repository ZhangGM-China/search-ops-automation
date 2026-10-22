import { describe, expect, it } from 'vitest';
import type { UserInfo } from '@/shared/types';
import { hasPagePermission, hasRouteActionPermission, roleDisplayLabel } from './permissions';
import { roleLabel } from '@/features/account/pages/AccountManagement/accountPermissions';

const user = (username = 'admin', role?: UserInfo['role'], isAdmin = false): UserInfo => ({
  username, role, isAdmin, pagePermissions: [], brandPermissions: [], actionPermissions: [],
});

describe('super administrator permissions', () => {
  it.each([user('admin', 'platform_admin', true), user('admin', 'platform_admin'), user('admin', undefined, true)])('only the admin account inherits global permissions', (admin) => {
    expect(hasPagePermission(admin, '/future-module')).toBe(true);
    expect(roleDisplayLabel(admin)).toBe('超级管理员');
    expect(roleLabel(admin)).toBe('超级管理员');
  });

  it.each([user('hb', 'platform_admin', true), user('moli', undefined, true)])('keeps other administrator accounts out of the super administrator role', (administrator) => {
    expect(hasPagePermission(administrator, '/future-module')).toBe(false);
    expect(roleDisplayLabel(administrator)).toBe('管理员账号');
    expect(roleLabel(administrator)).toBe('管理员');
    expect(hasRouteActionPermission(administrator, '/ops-dashboard')).toBe(true);
  });

  it.each([user('operator', 'brand_member'), user('brand', 'brand_admin')])('does not promote scoped accounts', (member) => {
    expect(hasPagePermission(member, '/config')).toBe(false);
    expect(roleLabel(member)).not.toBe('超级管理员');
  });
});
