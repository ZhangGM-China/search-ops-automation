import React from 'react';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import AccountManagement from '@/features/account/pages/AccountManagement';
import BlacklistLogs from '@/features/operations/pages/BlacklistLogs';
import ConfigPage from '@/features/config/pages/Config';
import DemotedLogs from '@/features/operations/pages/DemotedLogs';
import StockTasks from '@/features/operations/pages/StockTasks';
import { adminApi, dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

vi.mock('@/shared/hooks/useMobile', () => ({
  default: () => true,
}));

vi.mock('antd-mobile-icons', () => ({
  FilterOutline: () => <span>filter</span>,
}));

vi.mock('@ant-design/charts', () => ({
  Line: () => <div data-testid="mock-line-chart" />,
}));

vi.mock('antd-mobile', () => {
  let submitCurrentForm: (() => void) | undefined;
  const mobileFormValues = {
    username: 'mobile-user',
    pagePermissions: ['/'],
    brandPermissions: ['atlas'],
    brand: 'atlas',
    status: 'monitoring',
  };
  const Button = ({ children, onClick, type }: { children: React.ReactNode; onClick?: () => void; type?: string }) => (
    <button type={type === 'submit' ? 'submit' : 'button'} onClick={onClick}>{children}</button>
  );
  const Collapse = Object.assign(
    ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    { Panel: ({ title, children }: { title: string; children: React.ReactNode }) => <section><h4>{title}</h4>{children}</section> }
  );
  const Form = ({ children, footer, onFinish }: { children: React.ReactNode; footer?: React.ReactNode; onFinish?: (values: Record<string, unknown>) => void }) => {
    submitCurrentForm = () => onFinish?.(mobileFormValues);
    return (
      <form onSubmit={(event) => {
        event.preventDefault();
        onFinish?.(mobileFormValues);
      }}>
        {children}
        {footer}
      </form>
    );
  };
  Form.useForm = () => [{
    setFieldsValue: vi.fn(),
    resetFields: vi.fn(),
    getFieldsValue: vi.fn(() => mobileFormValues),
    submit: () => submitCurrentForm?.(),
  }];
  Form.Item = ({ children, label, onClick }: { children: React.ReactNode; label?: string; onClick?: () => void }) => (
    <label onClick={onClick}>{label}{children}</label>
  );
  const Radio = ({ children }: { children: React.ReactNode }) => <label>{children}</label>;
  Radio.Group = ({ children }: { children: React.ReactNode }) => <div>{children}</div>;

  return {
    Button,
    Form,
    Card: ({ title, extra, children, onClick }: { title: string; extra?: string; children: React.ReactNode; onClick?: () => void }) => (
      <button type="button" onClick={onClick}>
        <span>{title}</span>
        {extra && <span>{extra}</span>}
        {children}
      </button>
    ),
    SearchBar: ({ placeholder, onSearch, onClear }: { placeholder?: string; onSearch?: (value: string) => void; onClear?: () => void }) => (
      <input
        placeholder={placeholder}
        onChange={(event) => onSearch?.(event.target.value)}
        onDoubleClick={() => onClear?.()}
      />
    ),
    Popup: ({ visible, children }: { visible: boolean; children: React.ReactNode }) => visible ? <div>{children}</div> : null,
    DatePicker: ({ children, onConfirm, onClose }: { children?: (value?: Date) => React.ReactNode; onConfirm?: (value: Date) => void; onClose?: () => void }) => (
      <button type="button" onClick={() => { onConfirm?.(new Date('2026-06-16')); onClose?.(); }}>
        {children ? children(new Date('2026-06-16')) : '日期'}
      </button>
    ),
    Input: ({ placeholder, disabled }: { placeholder?: string; disabled?: boolean }) => <input placeholder={placeholder} disabled={disabled} />,
    Selector: () => <div>selector</div>,
    Stepper: ({ disabled }: { disabled?: boolean }) => <input aria-label="stepper" disabled={disabled} />,
    Switch: ({ disabled }: { disabled?: boolean }) => <input aria-label="switch" type="checkbox" disabled={disabled} />,
    Radio,
    Space: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    Tag: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
    DotLoading: () => <span>loading</span>,
    Tabs: Object.assign(
      ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
      { Tab: ({ title, children }: { title: string; children: React.ReactNode }) => <section><h4>{title}</h4>{children}</section> }
    ),
    Divider: () => <hr />,
    Collapse,
    InfiniteScroll: ({ loadMore, hasMore }: { loadMore: () => void; hasMore: boolean }) => (
      hasMore ? <button type="button" onClick={loadMore}>load more</button> : null
    ),
    ActionSheet: {
      show: vi.fn(({ actions, onAction }) => {
        actions.forEach((action: { key: string }) => onAction(action));
      }),
    },
    Dialog: {
      confirm: vi.fn(({ onConfirm }) => onConfirm?.()),
    },
  };
});

vi.mock('@/shared/api', () => ({
  adminApi: {
    getUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    deleteUser: vi.fn(),
    resetPassword: vi.fn(),
    getConfig: vi.fn(),
    updateConfig: vi.fn(),
  },
  dashboardApi: {
    getBlacklistLogs: vi.fn(),
    getDemotedLogs: vi.fn(),
    getStockTasks: vi.fn(),
    getTopQueryExperimentBrandStatus: vi.fn(),
    getTopQueryExperimentSamplePool: vi.fn(),
    cancelBlacklist: vi.fn(),
    blacklistProduct: vi.fn(),
    cancelDemotion: vi.fn(),
    toggleStockTaskStatus: vi.fn(),
  },
}));

const actionLog = {
  id: 1,
  productName: 'Mobile Action Product',
  sku: 'MOB1',
  brand: 'atlas',
  query: 'mobile query',
  reason: 'mobile reason',
  createdAt: '2026-06-16 10:00:00',
  isCanceled: false,
};

const canceledActionLog = {
  ...actionLog,
  id: 10,
  productName: 'Canceled Mobile Product',
  sku: 'MOB-CANCELED',
  isCanceled: true,
};

const stockTask = {
  id: 2,
  productName: 'Mobile Stock Action',
  sku: 'STOCK1',
  brand: 'atlas',
  query: 'stock query',
  lastStock: 20,
  lastBrokenRate: 20,
  status: 'monitoring',
  createdAt: '2026-06-16 09:00:00',
  updatedAt: '2026-06-16 10:00:00',
};

const mockConfig = {
  safety_config: {
    circuit_breaker_threshold: 10,
    max_daily_adjustments: 100,
    max_retry_count: 3,
    api_delay_threshold: 5,
  },
  brand_configs: {
    atlas: {
      enabled: true,
      top_query_governance_enabled: true,
      top_query_check_product_limit: 16,
      top_query_inventory_broken_rate_threshold: 50,
      top_query_inventory_check_interval_minutes: 60,
    },
  },
};

describe('mobile callback coverage', () => {
  afterEach(() => {
    cleanup();
  });
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem('user', JSON.stringify({ brandPermissions: ['atlas'] }));
    vi.mocked(adminApi.getUsers).mockResolvedValue(
      axiosResponse([
        { id: 1, username: 'admin', isAdmin: true, pagePermissions: ['/'], brandPermissions: ['atlas'] },
        { id: 2, username: 'operator', isAdmin: false, pagePermissions: ['/'], brandPermissions: ['atlas'] },
      ])
    );
    vi.mocked(adminApi.createUser).mockResolvedValue(
      axiosResponse({
        id: 3,
        username: 'mobile-user',
        isAdmin: false,
        pagePermissions: ['/'],
        brandPermissions: ['atlas'],
        temporaryPassword: 'tmp-pass',
      })
    );
    vi.mocked(adminApi.updateUser).mockResolvedValue(axiosResponse({ success: true }));
    vi.mocked(adminApi.deleteUser).mockResolvedValue(axiosResponse({ success: true }));
    vi.mocked(adminApi.resetPassword).mockResolvedValue(axiosResponse({ temporaryPassword: 'reset-pass' }));
    vi.mocked(adminApi.getConfig).mockResolvedValue(axiosResponse(mockConfig));
    vi.mocked(adminApi.updateConfig).mockResolvedValue(axiosResponse(mockConfig));
    vi.mocked(dashboardApi.getBlacklistLogs).mockResolvedValue(axiosResponse({ total: 3, items: [actionLog, canceledActionLog] }));
    vi.mocked(dashboardApi.getDemotedLogs).mockResolvedValue(axiosResponse({ total: 3, items: [actionLog, canceledActionLog] }));
    vi.mocked(dashboardApi.getStockTasks).mockResolvedValue(axiosResponse({ total: 2, items: [stockTask] }));
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({
      brand: 'atlas',
      current: null,
      items: [],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockResolvedValue(axiosResponse({
      brand: 'atlas',
      sampleDataDate: '2026-07-07',
      items: [],
    }));
    vi.mocked(dashboardApi.cancelBlacklist).mockResolvedValue(axiosResponse({ success: true }));
    vi.mocked(dashboardApi.blacklistProduct).mockResolvedValue(axiosResponse({ success: true }));
    vi.mocked(dashboardApi.cancelDemotion).mockResolvedValue(axiosResponse({ success: true }));
    vi.mocked(dashboardApi.toggleStockTaskStatus).mockResolvedValue(axiosResponse({ success: true }));
  });

  afterEach(() => {
    cleanup();
  });

  it('triggers mobile account actions and submit callbacks', async () => {
    render(<AccountManagement />);

    const operatorLabels = await screen.findAllByText('operator');
    expect(operatorLabels.length).toBeGreaterThan(0);
    fireEvent.click(operatorLabels[0]);

    await waitFor(() => {
      expect(adminApi.resetPassword).toHaveBeenCalledWith(2);
      expect(adminApi.deleteUser).toHaveBeenCalledWith(2);
    });

    fireEvent.click(screen.getByText('提交'));

    await waitFor(() => {
      expect(adminApi.updateUser).toHaveBeenCalledWith(2, expect.objectContaining({ username: 'mobile-user' }));
    });

    fireEvent.click(screen.getByText('新建账号'));
    fireEvent.click(screen.getByText('取消'));
  });

  it('renders and saves the mobile config form', async () => {
    localStorage.setItem('user', JSON.stringify({
      role: 'brand_member',
      pagePermissions: ['/config'],
      brandPermissions: ['atlas'],
      actionPermissions: ['manage_brand_config'],
    }));
    render(<ConfigPage />);

    expect(await screen.findByText('品牌配置')).toBeInTheDocument();
    expect(screen.queryByText('安全配置')).not.toBeInTheDocument();
    expect(screen.queryByText('全局默认配置')).not.toBeInTheDocument();

    const actions = document.getElementById('config-context-actions');
    await waitFor(() => expect(actions?.querySelector('button')).toHaveTextContent('修改配置'));
    fireEvent.click(actions!.querySelector('button')!);
    fireEvent.click(await screen.findByRole('button', { name: '保存' }));

    await waitFor(() => {
      expect(adminApi.updateConfig).toHaveBeenCalledWith(
        expect.not.objectContaining({ global_config: expect.anything() })
      );
    });
  });

  it('triggers mobile blacklist search, filter, load more, and action callbacks', async () => {
    render(<BlacklistLogs />);

    expect(await screen.findByText('Mobile Action Product')).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('SKU/名称/Query'), { target: { value: 'sku' } });
    fireEvent.doubleClick(screen.getByPlaceholderText('SKU/名称/Query'));
    fireEvent.click(screen.getByText(/筛选/));
    fireEvent.click(screen.getByText('重置'));
    fireEvent.click(screen.getByText('确认'));
    fireEvent.click(screen.getByText('load more'));
    fireEvent.click(screen.getByText('Canceled Mobile Product'));
    fireEvent.click(screen.getByText('Mobile Action Product'));

    await waitFor(() => {
      expect(dashboardApi.cancelBlacklist).toHaveBeenCalledWith({ brand: 'atlas', query: 'mobile query', sku: 'MOB1' });
      expect(dashboardApi.blacklistProduct).toHaveBeenCalledWith({
        brand: 'atlas',
        query: 'mobile query',
        sku: 'MOB-CANCELED',
        productName: 'Canceled Mobile Product',
      });
    });
  });

  it('triggers mobile demotion callbacks', async () => {
    render(<DemotedLogs />);

    expect(await screen.findByText('Mobile Action Product')).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('SKU/名称/Query'), { target: { value: 'query' } });
    fireEvent.click(screen.getByText(/筛选/));
    fireEvent.click(screen.getByText('确认'));
    fireEvent.click(screen.getByText('load more'));
    fireEvent.click(screen.getByText('Canceled Mobile Product'));
    fireEvent.click(screen.getByText('Mobile Action Product'));

    await waitFor(() => {
      expect(dashboardApi.cancelDemotion).toHaveBeenCalledWith({ brand: 'atlas', query: 'mobile query', sku: 'MOB1' });
    });
  });

  it('triggers mobile stock actions for monitoring and stopped tasks', async () => {
    const { unmount } = render(<StockTasks />);

    expect(await screen.findByText('Mobile Stock Action')).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('SKU/名称/Query'), { target: { value: 'stock' } });
    fireEvent.click(screen.getByText(/筛选/));
    fireEvent.click(screen.getByText('重置'));
    fireEvent.click(screen.getByText('确认'));
    fireEvent.click(screen.getByText('load more'));
    fireEvent.click(screen.getByText('Mobile Stock Action'));

    await waitFor(() => {
      expect(dashboardApi.cancelDemotion).toHaveBeenCalledWith({ brand: 'atlas', query: 'stock query', sku: 'STOCK1', taskId: 2 });
      expect(dashboardApi.toggleStockTaskStatus).toHaveBeenCalledWith(2, 'manually_stopped');
    });

    unmount();
    vi.mocked(dashboardApi.getStockTasks).mockResolvedValue(
      axiosResponse({ total: 1, items: [{ ...stockTask, id: 4, status: 'auto_stopped' }] })
    );
    render(<StockTasks />);

    expect(await screen.findByText('Mobile Stock Action')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Mobile Stock Action'));

    await waitFor(() => {
      expect(dashboardApi.toggleStockTaskStatus).toHaveBeenCalledWith(4, 'monitoring');
    });
  });
});
