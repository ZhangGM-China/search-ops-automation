import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { MainLayout } from './MainLayout';
import { authApi } from '@/shared/api';

vi.mock('@/shared/api', () => ({ authApi: { logout: vi.fn(), changePassword: vi.fn() } }));

const renderPage = () => render(
  <MemoryRouter initialEntries={['/ops-dashboard']}>
    <Routes>
      <Route path="/" element={<MainLayout />}>
        <Route path="ops-dashboard" element={<div>dashboard-route</div>} />
        <Route path="blacklist" element={<div>blacklist-route</div>} />
      </Route>
    </Routes>
  </MemoryRouter>
);
describe('MainLayout access and account actions', () => {
  beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); localStorage.setItem('user', JSON.stringify({ username: 'admin', isAdmin: true, pagePermissions: ['/ops-dashboard', '/smart-assistant', '/blacklist', '/demotion', '/stock-tracking', '/no-result-governance', '/account-management', '/config'], brandPermissions: ['atlas'] })); });
  it('keeps operations logs as one top-level navigation item', async () => {
    renderPage();

    expect(screen.getByText('品牌总览')).toBeInTheDocument();
    const operationsLogMenu = screen.getByRole('menuitem', { name: /运营日志/ });
    expect(operationsLogMenu).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: '相关性检测' })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: '断码检测' })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: '断码追踪' })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: '无结果词' })).not.toBeInTheDocument();

    fireEvent.click(operationsLogMenu);
    expect(await screen.findByText('blacklist-route')).toBeInTheDocument();
  });
  it('uses the original brand logo in the top navigation', () => {
    renderPage();

    expect(screen.getByAltText('logo')).toHaveAttribute(
      'src',
      '/brand-logo.png',
    );
  });
  it('clears session after logout failure', async () => { vi.mocked(authApi.logout).mockRejectedValue(new Error('offline')); renderPage(); fireEvent.click(screen.getByLabelText('打开账号菜单')); fireEvent.click(await screen.findByText('退出登录')); await waitFor(() => expect(localStorage.getItem('user')).toBeNull()); });
});
