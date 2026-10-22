import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import AccountManagement from './index';
import { adminApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

// Mock the API
vi.mock('@/shared/api', () => ({
  adminApi: {
    getUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    deleteUser: vi.fn(),
    resetPassword: vi.fn(),
  },
}));

const mockUsers = [
  {
    id: 1,
    username: 'admin',
    isAdmin: true,
    role: 'platform_admin',
    actionPermissions: ['manage_platform_users', 'manage_platform_config'],
    pagePermissions: ['/', '/config'],
    brandPermissions: ['atlas'],
  },
  {
    id: 2,
    username: 'operator',
    isAdmin: false,
    role: 'brand_member',
    actionPermissions: [],
    pagePermissions: ['/'],
    brandPermissions: ['atlas'],
  },
  {
    id: 3,
    username: 'brand-admin',
    isAdmin: false,
    role: 'brand_admin',
    actionPermissions: ['manage_brand_users', 'manage_brand_config'],
    pagePermissions: ['/account-management', '/config', '/smart-assistant'],
    brandPermissions: ['atlas'],
  },
];

describe('AccountManagement Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(adminApi.getUsers).mockResolvedValue(axiosResponse(mockUsers));
  });

  afterEach(() => {
    cleanup();
    // Reset window width
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1024 });
  });

  it('removes the standalone assistant access section and does not load its configuration', async () => {
    render(<AccountManagement />);

    expect(await screen.findByText('admin')).toBeInTheDocument();
    expect(screen.getAllByText('operator').length).toBeGreaterThan(0);
    expect(screen.getByText('超级管理员')).toBeInTheDocument();
    expect(screen.getAllByText('子账号').length).toBeGreaterThan(0);
    expect(screen.getAllByText('品牌管理员').length).toBeGreaterThan(0);
    expect(screen.queryByText('智能助理访问权限')).not.toBeInTheDocument();
    expect(screen.queryByText('保存访问权限')).not.toBeInTheDocument();
  });

  it('opens create user modal when clicking "新建账号"', async () => {
    render(<AccountManagement />);
    
    const createBtn = screen.getByText('新建账号');
    fireEvent.click(createBtn);
    
    expect(screen.getByText('账号名')).toBeInTheDocument();
    expect(screen.getByText('页面权限')).toBeInTheDocument();
    expect(screen.queryByLabelText('异常 Query 日志')).not.toBeInTheDocument();
  });

  it('creates a user and refreshes the account list', async () => {
    vi.mocked(adminApi.createUser).mockResolvedValue(
      axiosResponse({
        id: 3,
        username: 'new-user',
        isAdmin: false,
        pagePermissions: ['/'],
        brandPermissions: ['atlas'],
        temporaryPassword: 'tmp-pass',
      })
    );

    render(<AccountManagement />);

    fireEvent.click(screen.getByText('新建账号'));
    fireEvent.change(screen.getByLabelText('账号名'), { target: { value: 'new-user' } });
    fireEvent.click(screen.getByLabelText('品牌总览'));
    fireEvent.click(screen.getByLabelText('ATLAS'));
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => {
      expect(adminApi.createUser).toHaveBeenCalledWith(expect.objectContaining({ username: 'new-user' }));
      expect(adminApi.getUsers).toHaveBeenCalledTimes(2);
    });
  });

  it('does not submit platform-level actions for brand member accounts', async () => {
    vi.mocked(adminApi.createUser).mockResolvedValue(
      axiosResponse({
        id: 4,
        username: 'brand-member',
        isAdmin: false,
        role: 'brand_member',
        pagePermissions: ['/config'],
        brandPermissions: ['atlas'],
        actionPermissions: [],
        temporaryPassword: 'tmp-pass',
      })
    );

    render(<AccountManagement />);

    fireEvent.click(screen.getByText('新建账号'));
    fireEvent.change(screen.getByLabelText('账号名'), { target: { value: 'brand-member' } });
    expect(screen.queryByLabelText('管理平台配置')).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('系统配置'));
    fireEvent.click(screen.getByLabelText('ATLAS'));
    fireEvent.click(screen.getByLabelText('管理品牌配置'));
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => {
      expect(adminApi.createUser).toHaveBeenCalledWith(expect.objectContaining({
        username: 'brand-member',
        role: 'brand_member',
        actionPermissions: ['manage_brand_config'],
      }));
    });
  });

  it('updates an existing user and resets a password', async () => {
    vi.mocked(adminApi.updateUser).mockResolvedValue(axiosResponse({ success: true }));
    vi.mocked(adminApi.resetPassword).mockResolvedValue(axiosResponse({ temporaryPassword: 'reset-pass' }));

    render(<AccountManagement />);

    const editButtons = await screen.findAllByText('修改权限');
    fireEvent.click(editButtons[1]);
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => {
      expect(adminApi.updateUser).toHaveBeenCalledWith(2, expect.any(Object));
    });

    fireEvent.click(screen.getAllByText('重置密码')[0]);
    const resetConfirmButtons = screen.getAllByText('OK');
    fireEvent.click(resetConfirmButtons[resetConfirmButtons.length - 1]);

    await waitFor(() => {
      expect(adminApi.resetPassword).toHaveBeenCalledWith(2);
    });
  });

  it('calls deleteUser when confirming deletion', async () => {
    vi.mocked(adminApi.deleteUser).mockResolvedValue(axiosResponse({ msg: 'deleted' }));
    render(<AccountManagement />);
    
    const deleteBtn = await screen.findAllByText('删除');
    fireEvent.click(deleteBtn[0]); // For operator
    
    const confirmBtn = screen.getByText('OK');
    fireEvent.click(confirmBtn);
    
    await waitFor(() => {
      expect(adminApi.deleteUser).toHaveBeenCalledWith(2);
    });
  });

  it('renders correctly in mobile view', async () => {
    // Mock mobile width
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 375 });
    window.dispatchEvent(new Event('resize'));

    render(<AccountManagement />);
    
    // Wait for data to load and render
    expect(await screen.findByText('admin')).toBeInTheDocument();
    expect(screen.getAllByText('operator').length).toBeGreaterThan(0);
    
    // Verify permission summary is rendered
    expect(screen.getAllByText(/权限范围: 全部页面 \/ 全部品牌（含新增）/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/权限范围: 1 页面 \/ 1 品牌/).length).toBeGreaterThan(0);
  });

});
