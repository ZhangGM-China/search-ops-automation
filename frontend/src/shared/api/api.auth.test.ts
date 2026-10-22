import { beforeEach, describe, expect, it, vi } from 'vitest';

const axiosState = vi.hoisted(() => {
  const state: {
    rejected?: (error: unknown) => Promise<never>;
    api: Record<string, unknown>;
  } = {
    api: {},
  };
  state.api = {
    interceptors: {
      request: { use: vi.fn() },
      response: {
        use: vi.fn((_fulfilled, rejected) => {
          state.rejected = rejected;
        }),
      },
    },
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  };
  return state;
});

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => axiosState.api),
  },
}));

await import('./index');

describe('API authentication expiry', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    window.history.pushState({}, '', '/login');
  });

  it('clears the stale session and notifies the React shell on 401', async () => {
    localStorage.setItem('token', 'expired-token');
    localStorage.setItem('user', JSON.stringify({ username: 'operator' }));
    window.history.pushState({}, '', '/ops-dashboard/atlas?date=2026-07-19');
    const listener = vi.fn();
    window.addEventListener('search-ops:auth-expired', listener);
    const error = {
      response: { status: 401 },
      config: { url: '/dashboard', headers: { Authorization: 'Bearer expired-token' } },
    };

    await expect(axiosState.rejected?.(error)).rejects.toBe(error);

    expect(localStorage.getItem('token')).toBeNull();
    expect(localStorage.getItem('user')).toBeNull();
    expect(sessionStorage.getItem('search_ops_auth_return_to')).toBe('/ops-dashboard/atlas?date=2026-07-19');
    expect(sessionStorage.getItem('search_ops_auth_expired')).toBeNull();
    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener('search-ops:auth-expired', listener);
  });

  it('does not treat invalid login credentials as an expired authenticated session', async () => {
    localStorage.setItem('token', 'stale-token');
    localStorage.setItem('user', JSON.stringify({ username: 'operator' }));
    const listener = vi.fn();
    window.addEventListener('search-ops:auth-expired', listener);
    const error = {
      response: { status: 401 },
      config: { url: '/auth/login', headers: { Authorization: 'Bearer stale-token' } },
    };

    await expect(axiosState.rejected?.(error)).rejects.toBe(error);

    expect(localStorage.getItem('token')).toBe('stale-token');
    expect(localStorage.getItem('user')).not.toBeNull();
    expect(sessionStorage.getItem('search_ops_auth_expired')).toBeNull();
    expect(listener).not.toHaveBeenCalled();
    window.removeEventListener('search-ops:auth-expired', listener);
  });

  it('ignores duplicate and late 401 responses from an older token', async () => {
    localStorage.setItem('token', 'expired-token');
    localStorage.setItem('user', JSON.stringify({ username: 'operator' }));
    const listener = vi.fn();
    window.addEventListener('search-ops:auth-expired', listener);
    const error = {
      response: { status: 401 },
      config: { url: '/dashboard', headers: { Authorization: 'Bearer expired-token' } },
    };

    await expect(axiosState.rejected?.(error)).rejects.toBe(error);
    await expect(axiosState.rejected?.(error)).rejects.toBe(error);

    localStorage.setItem('token', 'new-token');
    localStorage.setItem('user', JSON.stringify({ username: 're-authenticated' }));
    await expect(axiosState.rejected?.(error)).rejects.toBe(error);

    expect(localStorage.getItem('token')).toBe('new-token');
    expect(localStorage.getItem('user')).toContain('re-authenticated');
    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener('search-ops:auth-expired', listener);
  });

  it('still handles expiry when a stale legacy dedupe flag remains in sessionStorage', async () => {
    // 旧版本会把 search_ops_auth_expired 置为 '1' 且仅在 Login 挂载时清除；
    // 残留标记不得再静默跳过会话失效处理。
    sessionStorage.setItem('search_ops_auth_expired', '1');
    localStorage.setItem('token', 'expired-token');
    localStorage.setItem('user', JSON.stringify({ username: 'operator' }));
    window.history.pushState({}, '', '/ops-dashboard');
    const listener = vi.fn();
    window.addEventListener('search-ops:auth-expired', listener);
    const error = {
      response: { status: 401 },
      config: { url: '/dashboard', headers: { Authorization: 'Bearer expired-token' } },
    };

    await expect(axiosState.rejected?.(error)).rejects.toBe(error);

    expect(localStorage.getItem('token')).toBeNull();
    expect(localStorage.getItem('user')).toBeNull();
    expect(sessionStorage.getItem('search_ops_auth_return_to')).toBe('/ops-dashboard');
    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener('search-ops:auth-expired', listener);
  });

  it('handles a second expiry after the user re-authenticates', async () => {
    localStorage.setItem('token', 'token-1');
    localStorage.setItem('user', JSON.stringify({ username: 'operator' }));
    const listener = vi.fn();
    window.addEventListener('search-ops:auth-expired', listener);

    await expect(axiosState.rejected?.({
      response: { status: 401 },
      config: { url: '/dashboard', headers: { Authorization: 'Bearer token-1' } },
    })).rejects.toBeDefined();
    expect(listener).toHaveBeenCalledTimes(1);

    localStorage.setItem('token', 'token-2');
    localStorage.setItem('user', JSON.stringify({ username: 'operator' }));
    await expect(axiosState.rejected?.({
      response: { status: 401 },
      config: { url: '/dashboard', headers: { Authorization: 'Bearer token-2' } },
    })).rejects.toBeDefined();

    expect(localStorage.getItem('token')).toBeNull();
    expect(listener).toHaveBeenCalledTimes(2);
    window.removeEventListener('search-ops:auth-expired', listener);
  });
});
