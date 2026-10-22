import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import BlacklistLogs from '@/features/operations/pages/BlacklistLogs';
import DemotedLogs from '@/features/operations/pages/DemotedLogs';
import StockTasks from '@/features/operations/pages/StockTasks';
import { dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

vi.mock('@/shared/hooks/useMobile', () => ({
  default: () => true,
}));

vi.mock('antd-mobile', async () => {
  const actual = await vi.importActual<typeof import('antd-mobile')>('antd-mobile');
  return {
    ...actual,
    DatePicker: () => <div data-testid="mock-mobile-date-picker" />,
  };
});

vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getBlacklistLogs: vi.fn(),
    getDemotedLogs: vi.fn(),
    getStockTasks: vi.fn(),
    cancelBlacklist: vi.fn(),
    blacklistProduct: vi.fn(),
    cancelDemotion: vi.fn(),
    toggleStockTaskStatus: vi.fn(),
  },
}));

vi.mock('@ant-design/charts', () => ({
  Line: () => <div data-testid="mock-line-chart" />,
}));

describe('mobile dashboard views', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem('user', JSON.stringify({ brandPermissions: ['atlas', 'harbor'] }));
    vi.mocked(dashboardApi.getBlacklistLogs).mockResolvedValue(
      axiosResponse({
        total: 1,
        items: [
          {
            id: 1,
            productName: 'Mobile Blacklist',
            sku: 'MB1',
            brand: 'atlas',
            query: 'bad query',
            reason: 'description mismatch',
            createdAt: '2026-06-16 10:00:00',
            isCanceled: true,
          },
        ],
      })
    );
    vi.mocked(dashboardApi.getDemotedLogs).mockResolvedValue(
      axiosResponse({
        total: 1,
        items: [
          {
            id: 2,
            productName: 'Mobile Demoted',
            sku: 'MD1',
            brand: 'atlas',
            query: 'low coverage',
            reason: 'coverage low',
            createdAt: '2026-06-16 10:00:00',
            isCanceled: true,
          },
        ],
      })
    );
    vi.mocked(dashboardApi.getStockTasks).mockResolvedValue(
      axiosResponse({
        total: 1,
        items: [
          {
            id: 4,
            productName: 'Mobile Stock',
            sku: 'MS1',
            brand: 'atlas',
            query: 'stock query',
            lastStock: 45,
            lastBrokenRate: 45,
            status: 'auto_stopped',
            createdAt: '2026-06-16 09:00:00',
            updatedAt: '2026-06-16 10:00:00',
          },
        ],
      })
    );
  });

  it('renders mobile blacklist and demotion lists with canceled state', async () => {
    const { unmount } = render(<BlacklistLogs />);
    expect(await screen.findByText('Mobile Blacklist')).toBeInTheDocument();
    expect(screen.getByText('已取消')).toBeInTheDocument();

    unmount();
    render(<DemotedLogs />);
    expect(await screen.findByText('Mobile Demoted')).toBeInTheDocument();
    expect(screen.getByText('MD1')).toBeInTheDocument();
    expect(screen.getByText('low coverage')).toBeInTheDocument();
  });

  it('renders stock tracking mobile cards', async () => {
    render(<StockTasks />);
    expect(await screen.findByText('Mobile Stock')).toBeInTheDocument();
    expect(screen.getByText('断码: 45%')).toBeInTheDocument();
    expect(screen.getByText('自动停止')).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('SKU/名称/Query'), { target: { value: 'stock' } });
    await waitFor(() => {
      expect(dashboardApi.getStockTasks).toHaveBeenCalled();
    });
  });
});
