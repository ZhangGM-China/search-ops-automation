import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { message } from 'antd';
import BlacklistLogs from './index';
import { dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

// Mock the API
vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getBlacklistLogs: vi.fn(),
    cancelBlacklist: vi.fn(),
    cancelBlacklistBatch: vi.fn(),
    blacklistProduct: vi.fn(),
  },
}));

describe('BlacklistLogs Page', () => {
  const mockLogs = [
    {
      id: 1,
      productName: 'Test Product',
      sku: 'SKU123',
      brand: 'atlas',
      query: 'test query',
      reason: 'low ctr',
      createdAt: '2024-01-01 10:00:00',
      isCanceled: false,
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(dashboardApi.getBlacklistLogs).mockResolvedValue(
      axiosResponse({ total: 1, items: mockLogs })
    );
  });

  it('should fetch and display logs on mount', async () => {
    render(<BlacklistLogs />);
    
    await waitFor(() => {
      expect(dashboardApi.getBlacklistLogs).toHaveBeenCalled();
    });
    
    expect(screen.getByText('Test Product')).toBeInTheDocument();
    expect(screen.getByText('SKU123')).toBeInTheDocument();
  });

  it('places bulk action in the filter row and total in the table pagination', async () => {
    render(<BlacklistLogs />);

    await screen.findByText('Test Product');

    const bulkButton = screen.getByRole('button', { name: '批量取消拉黑' });
    expect(bulkButton.closest('.operations-log-filter-actions')).toBeInTheDocument();
    expect(screen.getByText('共 1 条').closest('.ant-pagination')).toBeInTheDocument();
    expect(screen.queryByText('共 1 条')?.closest('.operations-bulk-toolbar')).toBeNull();
  });

  it('explains that bulk cancel needs a selection instead of silently doing nothing', async () => {
    const warning = vi.spyOn(message, 'warning').mockImplementation(() => ({}) as never);

    render(<BlacklistLogs />);

    await screen.findByText('Test Product');

    const bulkButton = screen.getByRole('button', { name: '批量取消拉黑' });
    expect(bulkButton).toBeEnabled();
    fireEvent.click(bulkButton);

    expect(warning).toHaveBeenCalledWith('请先选择要取消拉黑的记录');
    expect(dashboardApi.cancelBlacklistBatch).not.toHaveBeenCalled();
    warning.mockRestore();
  });

  it('cancels all selected active rows through the batch API', async () => {
    vi.mocked(dashboardApi.cancelBlacklistBatch).mockResolvedValue(
      axiosResponse({
        code: 0,
        msg: 'ok',
        succeeded: 1,
        failed: 0,
        items: [{ sku: 'SKU123', status: 'success' }],
      })
    );

    render(<BlacklistLogs />);

    await screen.findByText('Test Product');
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[checkboxes.length - 1]);
    fireEvent.click(screen.getByRole('button', { name: '批量取消拉黑' }));

    await waitFor(() => {
      expect(dashboardApi.cancelBlacklistBatch).toHaveBeenCalledWith([
        { brand: 'atlas', query: 'test query', sku: 'SKU123' },
      ]);
    });
    expect(await screen.findByRole('button', { name: '拉黑' })).toBeEnabled();
  });

  it('should handle cancel blacklist action', async () => {
    vi.mocked(dashboardApi.cancelBlacklist).mockResolvedValue(axiosResponse({ success: true }));
    
    render(<BlacklistLogs />);
    
    await waitFor(() => {
      expect(screen.getByText('取消拉黑')).toBeInTheDocument();
    });
    
    const cancelButton = screen.getByText('取消拉黑');
    fireEvent.click(cancelButton);
    
    // Ant Design Popconfirm shows "确定" button
    const confirmButton = await screen.findByText('确 定');
    fireEvent.click(confirmButton);
    
    await waitFor(() => {
      expect(dashboardApi.cancelBlacklist).toHaveBeenCalledWith({
        brand: 'atlas',
        query: 'test query',
        sku: 'SKU123'
      });
    });
    
    // The row can be restored after cancellation.
    await waitFor(() => {
      const restoreButton = screen.getByText('拉黑');
      expect(restoreButton).toBeInTheDocument();
      expect(restoreButton).toBeEnabled();
    });

  });

  it('should reset page to 1 when searching', async () => {
    render(<BlacklistLogs />);
    
    await waitFor(() => {
      expect(dashboardApi.getBlacklistLogs).toHaveBeenCalled();
    });

    const searchInput = screen.getByPlaceholderText('SKU/商品名称/Query');
    fireEvent.change(searchInput, { target: { value: 'search term' } });
    fireEvent.keyDown(searchInput, { key: 'Enter', code: 'Enter', charCode: 13 });
    
    await waitFor(() => {
      expect(dashboardApi.getBlacklistLogs).toHaveBeenLastCalledWith(
        expect.objectContaining({ search: 'search term', page: 1 })
      );
    });
  });

  it('should expose canceled rows and recover from API failures without crashing', async () => {
    vi.mocked(dashboardApi.getBlacklistLogs).mockResolvedValueOnce(
      axiosResponse({ total: 1, items: [{ ...mockLogs[0], isCanceled: true }] })
    );

    const { unmount } = render(<BlacklistLogs />);

    expect(await screen.findByRole('button', { name: '拉黑' })).toBeEnabled();
    unmount();

    vi.mocked(dashboardApi.getBlacklistLogs).mockRejectedValueOnce(new Error('load failed'));
    render(<BlacklistLogs />);

    await waitFor(() => {
      expect(dashboardApi.getBlacklistLogs).toHaveBeenCalled();
    });
    expect(screen.getByRole('tab', { name: '相关性检测' })).toBeInTheDocument();
  });

  it('should leave the row active when cancel blacklist fails', async () => {
    vi.mocked(dashboardApi.cancelBlacklist).mockRejectedValueOnce(new Error('cancel failed'));

    render(<BlacklistLogs />);

    await screen.findByText('取消拉黑');
    fireEvent.click(screen.getByText('取消拉黑'));
    fireEvent.click(screen.getByText('确 定'));

    await waitFor(() => {
      expect(dashboardApi.cancelBlacklist).toHaveBeenCalled();
    });
    expect(screen.getByText('取消拉黑')).toBeInTheDocument();
  });
});
