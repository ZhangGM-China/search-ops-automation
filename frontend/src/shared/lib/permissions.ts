import type { UserInfo } from '@/shared/types';

/** Only the canonical admin account is the super administrator.
 * Test and legacy payloads without a username retain role-based compatibility.
 */
export const isPlatformAdmin = (user: Pick<UserInfo, 'role'> & { username?: string; isAdmin?: boolean }) => (
  user.username ? user.username === 'admin' : user.role === 'platform_admin' || Boolean(user.isAdmin)
);
