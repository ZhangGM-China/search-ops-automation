import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { Toast } from 'antd-mobile';
import MobileLayout from './MobileLayout';
import { authApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

const navigate = vi.fn();

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

vi.mock('@/shared/api', () => ({
  authApi: {
    changePassword: vi.fn(),
    logout: vi.fn(),
  },
}));

describe('MobileLayout', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem('token', 'mobile-token');
    localStorage.setItem(
      'user',
      JSON.stringify({
        username: 'admin',
        isAdmin: true,
        pagePermissions: ['/ops-dashboard', '/smart-assistant', '/blacklist', '/config', '/account-management'],
        brandPermissions: ['atlas'],
      })
    );
  });

  it('shows an error but still clears the mobile session when logout fails', async () => {
    const toast = vi.spyOn(Toast, 'show');
    vi.mocked(authApi.logout).mockImplementation(async () => {
      expect(localStorage.getItem('token')).toBe('mobile-token');
      throw new Error('network unavailable');
    });

    render(
      <MemoryRouter initialEntries={['/ops-dashboard']}>
        <MobileLayout />
      </MemoryRouter>
    );

    fireEvent.click(screen.getByLabelText('打开账号菜单'));
    fireEvent.click(await screen.findByText('退出登录'));

    await waitFor(() => expect(authApi.logout).toHaveBeenCalledTimes(1));
    expect(localStorage.getItem('token')).toBeNull();
    expect(localStorage.getItem('user')).toBeNull();
    expect(toast).toHaveBeenCalledWith({
      icon: 'fail',
      content: '退出登录失败，已清理本地登录状态',
    });
    expect(navigate).toHaveBeenCalledWith('/login');
  });

  it('renders the mobile shell and switches tabs', () => {
    render(
      <MemoryRouter initialEntries={['/blacklist']}>
        <MobileLayout />
      </MemoryRouter>
    );

    expect(screen.queryByText('运营日志')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('助理'));
    expect(navigate).toHaveBeenCalledWith('/smart-assistant');
    fireEvent.click(screen.getByLabelText('打开账号菜单'));
    const popupItems = Array.from(document.querySelectorAll('.adm-list-item')).map((item) => item.textContent || '');
    expect(popupItems).toContain('运营日志');
    expect(popupItems).not.toContain('相关性检测');
    expect(popupItems).not.toContain('断码检测');
    expect(popupItems).not.toContain('断码追踪');
    expect(popupItems).not.toContain('无结果词');
    expect(screen.queryByText('异常 Query 日志')).not.toBeInTheDocument();
  });

  it('redirects to login when session is missing', async () => {
    localStorage.removeItem('user');
    render(
      <MemoryRouter initialEntries={['/blacklist']}>
        <Routes>
          <Route path="/login" element={<div>login-page</div>} />
          <Route path="*" element={<MobileLayout />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText('login-page')).toBeInTheDocument();
  });

  it('opens password flow validation through the public API path', async () => {
    vi.mocked(authApi.changePassword).mockResolvedValue(axiosResponse({ success: true }));

    render(
      <MemoryRouter initialEntries={['/']}>
        <MobileLayout />
      </MemoryRouter>
    );

    expect(screen.getAllByText('品牌总览').length).toBeGreaterThan(0);
    expect(screen.queryByText('每日任务')).not.toBeInTheDocument();
  });

  it('hides restricted navigation for a brand-scoped user', () => {
    localStorage.setItem('user', JSON.stringify({ username: 'brand', pagePermissions: ['/ops-dashboard'], brandPermissions: ['atlas'] }));
    render(<MemoryRouter initialEntries={['/ops-dashboard']}><MobileLayout /></MemoryRouter>);
    expect(screen.getByText('品牌总览')).toBeInTheDocument();
    expect(screen.queryByText('拉黑')).not.toBeInTheDocument();
    expect(screen.queryByText('配置')).not.toBeInTheDocument();
  });
});
