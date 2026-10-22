import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import { BrowserRouter, MemoryRouter } from 'react-router-dom';
import Login from './index';
import { authApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

const mobileState = vi.hoisted(() => ({ enabled: false }));

// Mock the API
vi.mock('@/shared/api', () => ({
  authApi: {
    login: vi.fn(),
  },
}));

vi.mock('@/shared/hooks/useMobile', () => ({
  default: () => mobileState.enabled,
}));

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

describe('Login Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mobileState.enabled = false;
    localStorage.clear();
    sessionStorage.clear();
  });

  it('renders login form', () => {
    render(<BrowserRouter><Login /></BrowserRouter>);
    expect(screen.getByAltText('拾心AI logo')).toHaveAttribute(
      'src',
      '/brand-logo.png',
    );
    expect(screen.getByText('AI OPS')).toBeInTheDocument();
    expect(screen.getByText('拾心 · 智能运营平台')).toBeInTheDocument();
    expect(screen.getByText('智能发现、智能诊断、智能治理')).toBeInTheDocument();
    expect(screen.queryByText('当前页面仅用于账号认证，不展示任何业务数据。登录后将按权限进入对应工作台。')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('用户名')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('密码')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '登 录' })).toBeInTheDocument();
  });

  it('does not show the previous session expiration alert', () => {
    sessionStorage.setItem('search_ops_auth_expired', '1');

    render(<BrowserRouter><Login /></BrowserRouter>);

    expect(screen.queryByText('登录状态已失效，请重新登录')).not.toBeInTheDocument();
  });

  it('submits form and navigates on success', async () => {
    const mockResponse = axiosResponse({
      access_token: 'test-token',
      user: { username: 'admin', isAdmin: true, pagePermissions: ['/'], brandPermissions: ['atlas'] },
    });
    vi.mocked(authApi.login).mockResolvedValue(mockResponse);

    render(<BrowserRouter><Login /></BrowserRouter>);

    fireEvent.change(screen.getByPlaceholderText('用户名'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByPlaceholderText('密码'), { target: { value: 'test-admin-password' } });
    fireEvent.click(screen.getByRole('button', { name: '登 录' }));

    await waitFor(() => {
      expect(authApi.login).toHaveBeenCalledWith({ username: 'admin', password: 'test-admin-password' });
      expect(localStorage.getItem('token')).toBe('test-token');
      expect(mockNavigate).toHaveBeenCalledWith('/ops-dashboard', { replace: true });
    });
  });

  it('returns to the protected workspace that triggered login', async () => {
    vi.mocked(authApi.login).mockResolvedValue(axiosResponse({
      access_token: 'test-token',
      user: { username: 'admin', isAdmin: true, pagePermissions: ['/ops-dashboard'], brandPermissions: ['atlas'] },
    }));

    render(
      <MemoryRouter initialEntries={[{
        pathname: '/login',
        state: { from: '/ops-dashboard/atlas?date=2026-07-13&querySource=hot_query' },
      }]}
      >
        <Login />
      </MemoryRouter>
    );

    fireEvent.change(screen.getByPlaceholderText('用户名'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByPlaceholderText('密码'), { target: { value: 'test-admin-password' } });
    fireEvent.click(screen.getByRole('button', { name: '登 录' }));

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith(
        '/ops-dashboard/atlas?date=2026-07-13&querySource=hot_query',
        { replace: true }
      );
    });
  });

  it('shows error message on failure', async () => {
    vi.mocked(authApi.login).mockRejectedValue({
      response: { data: { detail: 'Incorrect username or password' } },
    });

    render(<BrowserRouter><Login /></BrowserRouter>);

    fireEvent.change(screen.getByPlaceholderText('用户名'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByPlaceholderText('密码'), { target: { value: 'wrong' } });
    fireEvent.click(screen.getByRole('button', { name: '登 录' }));

    await waitFor(() => {
      expect(screen.getByText('Incorrect username or password')).toBeInTheDocument();
    });
  });

  it('shows the mobile login form and handles mobile failures', async () => {
    mobileState.enabled = true;
    vi.mocked(authApi.login).mockRejectedValue({
      response: { data: { detail: 'Mobile login failed' } },
    });

    render(<BrowserRouter><Login /></BrowserRouter>);

    expect(screen.getByText('AI OPS')).toBeInTheDocument();
    expect(screen.getByText('拾心 · 智能运营平台')).toBeInTheDocument();
    expect(screen.getByText('智能发现、智能诊断、智能治理')).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('请输入用户名'), { target: { value: 'mobile' } });
    fireEvent.change(screen.getByPlaceholderText('请输入密码'), { target: { value: 'bad' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));

    await waitFor(() => {
      expect(authApi.login).toHaveBeenCalledWith({ username: 'mobile', password: 'bad' });
      expect(screen.getByText('Mobile login failed')).toBeInTheDocument();
    });
  });
});
