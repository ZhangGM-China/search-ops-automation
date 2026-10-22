import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { message } from 'antd';
import DemotedLogs from './index';
import { dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getDemotedLogs: vi.fn(),
    cancelDemotion: vi.fn(),
  },
}));

vi.mock('@/shared/hooks/useMobile', () => ({
  default: () => false,
}));

describe('DemotedLogs Page', () => {
  const mockLogs = [
    {
      id: 1,
      productName: 'Demoted Product',
      sku: 'SKU-D1',
      brand: 'atlas',
      query: 'dress',
      reason: 'size coverage low',
      createdAt: '2026-06-16 10:00:00',
      isCanceled: false,
    },
    {
      id: 2,
      productName: 'Recovered Product',
      sku: 'SKU-D2',
      brand: 'atlas',
      query: 'shirt',
      reason: 'already canceled',
      createdAt: '2026-06-16 11:00:00',
      isCanceled: true,
    },
  ];

  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(message, 'success').mockImplementation(() => undefined as never);
    vi.spyOn(message, 'error').mockImplementation(() => undefined as never);
    localStorage.setItem('user', JSON.stringify({ brandPermissions: ['atlas'] }));
    vi.mocked(dashboardApi.getDemotedLogs).mockResolvedValue(
      axiosResponse({ total: mockLogs.length, items: mockLogs })
    );
    vi.mocked(dashboardApi.cancelDemotion).mockResolvedValue(axiosResponse({ success: true }));
  });

  it('loads demotion logs and searches on Enter from page one', async () => {
    render(<DemotedLogs />);

    expect(await screen.findByText('Demoted Product')).toBeInTheDocument();
    expect(screen.getByText('Recovered Product')).toBeInTheDocument();

    const searchInput = screen.getByPlaceholderText('SKU/商品名称/Query');
    fireEvent.change(searchInput, { target: { value: 'dress' } });
    fireEvent.keyDown(searchInput, { key: 'Enter', code: 'Enter', charCode: 13 });

    await waitFor(() => {
      expect(dashboardApi.getDemotedLogs).toHaveBeenLastCalledWith(
        expect.objectContaining({ search: 'dress', page: 1 })
      );
    });
  });

  it('cancels a demotion and marks the row as canceled', async () => {
    render(<DemotedLogs />);

    await screen.findByText('Demoted Product');
    fireEvent.click(screen.getByText('取消分桶调整'));
    fireEvent.click(screen.getByText('确 定'));

    await waitFor(() => {
      expect(dashboardApi.cancelDemotion).toHaveBeenCalledWith({
        brand: 'atlas',
        query: 'dress',
        sku: 'SKU-D1',
      });
    });

    await waitFor(() => {
      expect(screen.getAllByText('已取消').length).toBeGreaterThan(0);
    });
    expect(message.success).toHaveBeenCalledWith('已取消分桶调整并同步至系统');
  });

  it('handles canceled rows and API failures', async () => {
    vi.mocked(dashboardApi.getDemotedLogs).mockResolvedValueOnce(
      axiosResponse({ total: 1, items: [{ ...mockLogs[1] }] })
    );

    const { unmount } = render(<DemotedLogs />);

    expect(await screen.findByText('Recovered Product')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /已取消/i })).toBeDisabled();
    unmount();

    vi.mocked(dashboardApi.getDemotedLogs).mockRejectedValueOnce(new Error('load failed'));
    render(<DemotedLogs />);

    await waitFor(() => {
      expect(dashboardApi.getDemotedLogs).toHaveBeenCalled();
    });
    expect(screen.getByRole('tab', { name: '断码检测' })).toBeInTheDocument();
  });

  it('keeps an active row when cancel demotion fails', async () => {
    vi.mocked(dashboardApi.cancelDemotion).mockRejectedValueOnce(new Error('cancel failed'));

    render(<DemotedLogs />);

    await screen.findByText('Demoted Product');
    fireEvent.click(screen.getByText('取消分桶调整'));
    fireEvent.click(screen.getByText('确 定'));

    await waitFor(() => {
      expect(dashboardApi.cancelDemotion).toHaveBeenCalled();
    });
    expect(screen.getByText('取消分桶调整')).toBeInTheDocument();
    expect(message.error).toHaveBeenCalledWith('取消分桶调整失败');
  });
});
