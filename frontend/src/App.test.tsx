import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import App from './App';
import { authApi } from '@/shared/api';

const feedback = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual<typeof import('antd')>('antd');
  return {
    ...actual,
    message: {
      ...actual.message,
      useMessage: () => [feedback, null],
    },
  };
});

vi.mock('@/shared/hooks/useMobile', () => ({
  default: () => false,
}));

vi.mock('./app/layouts/MobileLayout', () => ({
  default: () => <div>mobile-shell</div>,
}));

vi.mock('./features/operations/pages/BlacklistLogs', () => ({ default: () => <div>blacklist-page</div> }));
vi.mock('./features/operations/pages/DemotedLogs', () => ({ default: () => <div>demotion-page</div> }));
vi.mock('./features/operations/pages/StockTasks', () => ({ default: () => <div>stock-page</div> }));
vi.mock('./features/no-result/pages/NoResultGovernance', () => ({ default: () => <div>no-result-governance-page</div> }));
vi.mock('./features/query-governance/pages/BrandOverview', () => ({ default: () => <div>brand-overview-page</div> }));
vi.mock('./features/query-governance/pages/BrandDetail', () => ({ default: () => <div>brand-detail-page</div> }));
vi.mock('./features/assistant/pages/SmartAssistant', () => ({ default: () => <div>smart-assistant-page</div> }));
vi.mock('./features/account/pages/AccountManagement', () => ({ default: () => <div>account-page</div> }));
vi.mock('./features/config/pages/Config', () => ({ default: () => <div>config-page</div> }));
vi.mock('./features/auth/pages/Login', () => ({ default: () => <div>login-page</div> }));

vi.mock('@/shared/api', () => ({
  AUTH_EXPIRED_EVENT: 'search-ops:auth-expired',
  authApi: {
    changePassword: vi.fn(),
    logout: vi.fn(),
  },
}));

describe('App routing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    window.history.pushState({}, '', '/');
  });

  it('revokes the server session before clearing the desktop session', async () => {
    localStorage.setItem('token', 'desktop-token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'operator',
        isAdmin: false,
        pagePermissions: ['/ops-dashboard'],
        brandPermissions: ['atlas'],
      })
    );
    vi.mocked(authApi.logout).mockImplementation(async () => {
      expect(localStorage.getItem('token')).toBe('desktop-token');
      return { data: { msg: 'All sessions revoked' } } as never;
    });

    render(<App />);
    fireEvent.click(screen.getByLabelText('打开账号菜单'));
    fireEvent.click(await screen.findByText('退出登录'));

    await waitFor(() => expect(authApi.logout).toHaveBeenCalledTimes(1));
    expect(localStorage.getItem('token')).toBeNull();
    expect(localStorage.getItem('user')).toBeNull();
    expect(feedback.success).toHaveBeenCalledWith('已退出登录');
    expect(await screen.findByText('login-page')).toBeInTheDocument();
  });

  it('shows a desktop error but still clears the local session when logout fails', async () => {
    localStorage.setItem('token', 'desktop-token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'operator',
        isAdmin: false,
        pagePermissions: ['/ops-dashboard'],
        brandPermissions: ['atlas'],
      })
    );
    vi.mocked(authApi.logout).mockRejectedValue(new Error('network unavailable'));

    render(<App />);
    fireEvent.click(screen.getByLabelText('打开账号菜单'));
    fireEvent.click(await screen.findByText('退出登录'));

    await waitFor(() => expect(authApi.logout).toHaveBeenCalledTimes(1));
    expect(localStorage.getItem('token')).toBeNull();
    expect(localStorage.getItem('user')).toBeNull();
    expect(feedback.error).toHaveBeenCalledWith('退出登录失败，已清理本地登录状态');
    expect(feedback.success).not.toHaveBeenCalled();
    expect(await screen.findByText('login-page')).toBeInTheDocument();
  });

  it('shows login page when no session exists', async () => {
    render(<App />);
    expect(await screen.findByText('login-page')).toBeInTheDocument();
  });

  it('uses brand overview as the authenticated home page', async () => {
    localStorage.setItem('token', 'token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'operator',
        isAdmin: false,
        pagePermissions: ['/ops-dashboard'],
        brandPermissions: ['atlas'],
      })
    );

    render(<App />);

    expect(await screen.findByText('brand-overview-page')).toBeInTheDocument();
  });

  it('removes the authenticated shell immediately when the session expires', async () => {
    localStorage.setItem('token', 'token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'operator',
        isAdmin: false,
        pagePermissions: ['/ops-dashboard'],
        brandPermissions: ['atlas'],
      })
    );
    window.history.pushState({}, '', '/ops-dashboard');

    render(<App />);
    expect(await screen.findByText('brand-overview-page')).toBeInTheDocument();

    localStorage.removeItem('token');
    localStorage.removeItem('user');
    window.dispatchEvent(new Event('search-ops:auth-expired'));

    expect(await screen.findByText('login-page')).toBeInTheDocument();
    expect(screen.queryByText('brand-overview-page')).not.toBeInTheDocument();
  });

  it('removes the authenticated shell when another tab clears the session', async () => {
    localStorage.setItem('token', 'token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'operator',
        isAdmin: false,
        pagePermissions: ['/ops-dashboard'],
        brandPermissions: ['atlas'],
      })
    );
    window.history.pushState({}, '', '/ops-dashboard');

    render(<App />);
    expect(await screen.findByText('brand-overview-page')).toBeInTheDocument();

    localStorage.removeItem('token');
    localStorage.removeItem('user');
    window.dispatchEvent(new StorageEvent('storage', {
      key: 'token',
      oldValue: 'token',
      newValue: null,
    }));

    expect(await screen.findByText('login-page')).toBeInTheDocument();
    expect(screen.queryByText('brand-overview-page')).not.toBeInTheDocument();
  });

  it('renders protected content and hides unauthorized routes', async () => {
    localStorage.setItem('token', 'token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'admin',
        isAdmin: true,
        pagePermissions: ['/', '/config'],
        brandPermissions: ['atlas'],
      })
    );
    window.history.pushState({}, '', '/config');

    render(<App />);

    expect(await screen.findByText('config-page')).toBeInTheDocument();
  });

  it('renders the access denied state for missing permissions', () => {
    localStorage.setItem('token', 'token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'viewer',
        isAdmin: false,
        pagePermissions: ['/'],
        brandPermissions: ['atlas'],
      })
    );
    window.history.pushState({}, '', '/config');

    render(<App />);

    expect(screen.getByText('当前账号无权限访问')).toBeInTheDocument();
  });

  it('allows read-only users with /config page permission to open A/B Test', async () => {
    localStorage.setItem('token', 'token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'config-viewer',
        isAdmin: false,
        role: 'brand_member',
        pagePermissions: ['/config'],
        brandPermissions: ['atlas'],
        actionPermissions: [],
      })
    );
    window.history.pushState({}, '', '/config');

    render(<App />);

    expect(await screen.findByText('config-page')).toBeInTheDocument();
    expect(screen.queryByText('当前账号无权限访问')).not.toBeInTheDocument();
    expect(screen.getByText('系统配置')).toBeInTheDocument();
  });

  it('allows brand config managers to access config route', async () => {
    localStorage.setItem('token', 'token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'brand-config-manager',
        isAdmin: false,
        role: 'brand_admin',
        pagePermissions: ['/config'],
        brandPermissions: ['atlas'],
        actionPermissions: ['manage_brand_config'],
      })
    );
    window.history.pushState({}, '', '/config');

    render(<App />);

    expect(await screen.findByText('config-page')).toBeInTheDocument();
    expect(screen.getByText('系统配置')).toBeInTheDocument();
  });

  it('renders the reordered desktop menu groups and smart assistant route', async () => {
    localStorage.setItem('token', 'token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'operator',
        isAdmin: false,
        pagePermissions: ['/ops-dashboard', '/blacklist', '/no-result-governance', '/smart-assistant', '/account-management', '/config'],
        brandPermissions: ['atlas'],
        actionPermissions: ['manage_brand_config'],
      })
    );
    window.history.pushState({}, '', '/smart-assistant');

    render(<App />);

    expect(await screen.findByText('smart-assistant-page')).toBeInTheDocument();
    expect(screen.getAllByText('拾心 · 智能运营').length).toBeGreaterThan(0);
    expect(screen.getAllByText('智能发现 · 智能诊断 · 智能治理').length).toBeGreaterThan(0);
    expect(screen.getAllByText('品牌总览').length).toBeGreaterThan(0);
    expect(document.querySelector('.app-sider')).not.toBeInTheDocument();
    expect(document.querySelector('.app-top-nav')).toBeInTheDocument();
    expect(document.querySelector('.app-top-nav__menu')).toBeInTheDocument();
    expect(document.querySelector('.app-breadcrumb-row')).not.toBeInTheDocument();
    const menuLabels = screen.getAllByRole('menuitem').map((item) => item.textContent || '');
    expect(menuLabels[0]).toContain('品牌总览');
    expect(menuLabels.some((label) => label.includes('每日任务'))).toBe(false);
    const operationsLogMenu = screen.getByRole('menuitem', { name: /运营日志/ });
    expect(operationsLogMenu).not.toHaveAttribute('aria-haspopup');
    expect(screen.queryByRole('menuitem', { name: /相关性/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: /分桶调整/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: /断码追踪/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: /无结果词/ })).not.toBeInTheDocument();
    fireEvent.click(operationsLogMenu);
    expect(await screen.findByText('blacklist-page')).toBeInTheDocument();
    expect(screen.getAllByText('智能运营助理').length).toBeGreaterThan(0);
    expect(screen.getByText('账号管理')).toBeInTheDocument();
    expect(screen.getByText('系统配置')).toBeInTheDocument();
    expect(screen.getByLabelText('打开账号菜单')).toHaveTextContent('operator');
  });

  it('renders no-result governance route under operations logs', async () => {
    localStorage.setItem('token', 'token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'operator',
        isAdmin: false,
        pagePermissions: ['/no-result-governance'],
        brandPermissions: ['summit'],
      })
    );
    window.history.pushState({}, '', '/no-result-governance');

    render(<App />);

    expect(await screen.findByText('no-result-governance-page')).toBeInTheDocument();
  });
});
