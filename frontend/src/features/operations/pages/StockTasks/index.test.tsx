import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import StockTasks from './index';
import { dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getStockTasks: vi.fn(),
    cancelDemotion: vi.fn(),
    toggleStockTaskStatus: vi.fn(),
  },
}));

vi.mock('@/shared/hooks/useMobile', () => ({
  default: () => false,
}));

describe('StockTasks Page', () => {
  const baseTask = {
    id: 1,
    productName: '测试商品',
    sku: 'S1',
    brand: 'atlas',
    query: '连衣裙 女士',
    lastStock: -1,
    lastBrokenRate: -1,
    status: 'recovered',
    createdAt: '2026-06-15 10:00:00',
    updatedAt: '2026-06-15 11:00:00',
  };

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem('user', JSON.stringify({ brandPermissions: ['atlas'] }));
    sessionStorage.removeItem('operations-log-filters');
    vi.mocked(dashboardApi.cancelDemotion).mockResolvedValue(axiosResponse({ success: true }));
    vi.mocked(dashboardApi.toggleStockTaskStatus).mockResolvedValue(axiosResponse({ success: true }));
  });

  it('renders unknown broken rate as dash instead of negative percentage', async () => {
    vi.mocked(dashboardApi.getStockTasks).mockResolvedValue(
      axiosResponse({
        total: 1,
        items: [baseTask],
      })
    );

    render(<StockTasks />);

    await waitFor(() => {
      expect(screen.getByText('测试商品')).toBeInTheDocument();
    });

    expect(screen.queryByText('-1%')).not.toBeInTheDocument();
    expect(screen.getByText('-')).toBeInTheDocument();
  });

  it('cancels demotion and stops monitoring from the task list', async () => {
    vi.mocked(dashboardApi.getStockTasks).mockResolvedValue(
      axiosResponse({
        total: 1,
        items: [{ ...baseTask, status: 'monitoring', lastBrokenRate: 33 }],
      })
    );

    render(<StockTasks />);

    expect(await screen.findByText('测试商品')).toBeInTheDocument();
    expect(screen.getByText('33%')).toBeInTheDocument();
    expect(screen.getByText('监测中')).toBeInTheDocument();

    fireEvent.click(screen.getByText('停止追踪'));
    await waitFor(() => {
      expect(dashboardApi.toggleStockTaskStatus).toHaveBeenCalledWith(1, 'manually_stopped');
    });

    fireEvent.click(screen.getByText('取消分桶调整'));
    fireEvent.click(screen.getByText('确 定'));

    await waitFor(() => {
      expect(dashboardApi.cancelDemotion).toHaveBeenCalledWith({
        brand: 'atlas',
        query: '连衣裙 女士',
        sku: 'S1',
        taskId: 1,
      });
    });
  });

  it('restarts manually stopped tasks', async () => {
    vi.mocked(dashboardApi.getStockTasks).mockResolvedValue(
      axiosResponse({
        total: 1,
        items: [{ ...baseTask, status: 'manually_stopped', lastBrokenRate: 80 }],
      })
    );

    render(<StockTasks />);

    expect(await screen.findByText('手动停止')).toBeInTheDocument();
    fireEvent.click(screen.getByText('开启追踪'));

    await waitFor(() => {
      expect(dashboardApi.toggleStockTaskStatus).toHaveBeenCalledWith(1, 'monitoring');
    });
  });

  it('renders all operational statuses and restarts auto stopped tasks', async () => {
    vi.mocked(dashboardApi.getStockTasks).mockResolvedValue(
      axiosResponse({
        total: 2,
        items: [
          { ...baseTask, id: 2, productName: '自动停止商品', sku: 'S2', status: 'auto_stopped', lastBrokenRate: 49 },
          { ...baseTask, id: 3, productName: '未知状态商品', sku: 'S3', status: 'paused_by_rule', lastBrokenRate: undefined, lastStock: 66 },
        ],
      })
    );

    render(<StockTasks />);

    expect(await screen.findByText('自动停止')).toBeInTheDocument();
    expect(screen.getByText('paused_by_rule')).toBeInTheDocument();
    expect(screen.getByText('66%')).toBeInTheDocument();

    fireEvent.click(screen.getByText('开启追踪'));

    await waitFor(() => {
      expect(dashboardApi.toggleStockTaskStatus).toHaveBeenCalledWith(2, 'monitoring');
    });
  });

  it('keeps the page stable when loading or operations fail', async () => {
    vi.mocked(dashboardApi.getStockTasks).mockRejectedValueOnce(new Error('load failed'));

    const { unmount } = render(<StockTasks />);

    await waitFor(() => {
      expect(dashboardApi.getStockTasks).toHaveBeenCalled();
    });
    unmount();

    vi.mocked(dashboardApi.getStockTasks).mockResolvedValue(
      axiosResponse({
        total: 1,
        items: [{ ...baseTask, status: 'monitoring', lastBrokenRate: 20 }],
      })
    );
    vi.mocked(dashboardApi.cancelDemotion).mockRejectedValueOnce(new Error('cancel failed'));
    vi.mocked(dashboardApi.toggleStockTaskStatus).mockRejectedValueOnce(new Error('toggle failed'));

    render(<StockTasks />);

    expect(await screen.findByText('测试商品')).toBeInTheDocument();
    fireEvent.click(screen.getByText('停止追踪'));
    await waitFor(() => {
      expect(dashboardApi.toggleStockTaskStatus).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByText('取消分桶调整'));
    fireEvent.click(screen.getByText('确 定'));

    await waitFor(() => {
      expect(dashboardApi.cancelDemotion).toHaveBeenCalled();
    });
    expect(screen.getByText('监测中')).toBeInTheDocument();
  });

  it('renders a saved date string from the shared filter bar without crashing', async () => {
    sessionStorage.setItem('operations-log-filters', JSON.stringify({ date: '2026-06-15' }));
    vi.mocked(dashboardApi.getStockTasks).mockResolvedValue(
      axiosResponse({
        total: 1,
        items: [{ ...baseTask, status: 'monitoring', lastBrokenRate: 40 }],
      })
    );

    // 保存的日期是字符串；直接传给 DatePicker 会让整页渲染崩溃（白屏）
    render(<StockTasks />);

    expect(await screen.findByText('测试商品')).toBeInTheDocument();
    expect(screen.getByText('40%')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByDisplayValue('2026-06-15')).toBeInTheDocument();
    });
  });
});
