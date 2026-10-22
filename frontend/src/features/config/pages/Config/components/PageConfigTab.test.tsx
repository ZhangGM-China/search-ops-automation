import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PageConfigProvider } from '@/shared/pageConfig';
import { adminApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';
import PageConfigTab from './PageConfigTab';

vi.mock('@/shared/api', () => ({
  adminApi: {
    getPageConfig: vi.fn(),
    updatePageConfig: vi.fn(),
  },
}));

describe('PageConfigTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('groups related controls into compact panels instead of one card per setting', async () => {
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2,
      version: 0,
      values: {},
      definitions: [],
    }));
    const onDirtyChange = vi.fn();

    render(
      <PageConfigProvider>
        <PageConfigTab onDirtyChange={onDirtyChange} />
      </PageConfigProvider>,
    );

    await waitFor(() => expect(adminApi.getPageConfig).toHaveBeenCalledTimes(1));
    expect(screen.getAllByRole('region')).toHaveLength(5);
    expect(document.querySelectorAll('.page-config-tab__item')).toHaveLength(0);
    expect(document.querySelectorAll('.page-config-tab__metric-summary').length).toBeGreaterThan(0);
    expect(document.querySelectorAll('.page-config-tab__toggle').length).toBeGreaterThan(0);
    expect(document.querySelectorAll('.page-config-tab__toggle-grid')).toHaveLength(0);
  });

  it('lets an administrator choose and save the brand card primary metric', async () => {
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2, version: 0, values: {}, definitions: [],
    }));
    vi.mocked(adminApi.updatePageConfig).mockImplementation(async (update) => axiosResponse({
      schemaVersion: 2, version: 1, values: update.values, definitions: [],
    }));

    render(<PageConfigProvider><PageConfigTab onDirtyChange={vi.fn()} /></PageConfigProvider>);
    await waitFor(() => expect(adminApi.getPageConfig).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    const primaryMetric = screen.getByRole('combobox', { name: '主指标' });
    fireEvent.mouseDown(primaryMetric.parentElement!);
    fireEvent.click(await screen.findByTitle('通过率'));
    fireEvent.click(screen.getByRole('button', { name: /保\s*存/ }));

    await waitFor(() => expect(adminApi.updatePageConfig).toHaveBeenCalledWith(expect.objectContaining({
      values: expect.objectContaining({ 'brand_overview.card.primary_metric': ['pass_rate'] }),
    })));
  });

  it('keeps field guidance in title tooltips and lays fields out as a three-column grid', async () => {
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2,
      version: 0,
      values: {},
      definitions: [],
    }));

    render(
      <PageConfigProvider>
        <PageConfigTab onDirtyChange={vi.fn()} />
      </PageConfigProvider>,
    );

    await waitFor(() => expect(adminApi.getPageConfig).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText('看板指标提示')).toHaveClass('ops-tooltip-icon');
    expect(screen.queryByText('所选日期全部品牌合计数据；比率按合计分子与分母计算。')).not.toBeInTheDocument();
    fireEvent.mouseEnter(screen.getByLabelText('看板指标提示'));
    expect(await screen.findByText('所选日期全部品牌合计数据；比率按合计分子与分母计算。')).toBeInTheDocument();
    expect(document.querySelector('.page-config-tab__group-body')).toHaveClass('page-config-tab__group-body');
  });

  it('uses compact copy, text-free switches, and definition order for mixed controls', async () => {
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2,
      version: 0,
      values: {},
      definitions: [],
    }));

    render(
      <PageConfigProvider>
        <PageConfigTab onDirtyChange={vi.fn()} />
      </PageConfigProvider>,
    );

    await waitFor(() => expect(adminApi.getPageConfig).toHaveBeenCalledTimes(1));
    expect(screen.getAllByText('数据看板')).toHaveLength(2);
    expect(screen.getByText('看板指标')).toBeInTheDocument();
    expect(screen.getByText('耗时/Token')).toBeInTheDocument();
    expect(screen.getAllByText('运营数据')).toHaveLength(2);
    expect(screen.getByText('基础数据')).toBeInTheDocument();
    expect(screen.getByText('转化指标')).toBeInTheDocument();
    expect(screen.getByText('转化率')).toBeInTheDocument();
    expect(screen.queryByText('转化率图表')).not.toBeInTheDocument();
    expect(screen.queryByText('转化率表单')).not.toBeInTheDocument();
    expect(screen.getByText('品牌详情')).toBeInTheDocument();
    expect(screen.getByText('Query 列表')).toBeInTheDocument();
    expect(screen.getByText('Query 详情')).toBeInTheDocument();
    expect(screen.queryByText('右侧详情')).not.toBeInTheDocument();
    expect(screen.getByText('治理方案')).toBeInTheDocument();
    expect(screen.getByText('效果对比')).toBeInTheDocument();
    expect(screen.getByText('相关性规则')).toBeInTheDocument();
    expect(screen.getAllByRole('switch').every((element) => element.textContent === '')).toBe(true);
    expect(screen.queryByText('调整指标')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    expect(screen.getAllByRole('button', { name: /：调整$/ }).length).toBeGreaterThan(0);
    const overviewBody = screen.getByRole('region', { name: '数据看板' }).querySelector('.page-config-tab__group-body');
    expect(overviewBody?.textContent?.indexOf('数据看板')).toBeLessThan(overviewBody?.textContent?.indexOf('看板指标') ?? -1);
  });

  it('combines chart and table visibility with the conversion metric editor', async () => {
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2,
      version: 0,
      values: {},
      definitions: [],
    }));

    render(
      <PageConfigProvider>
        <PageConfigTab onDirtyChange={vi.fn()} />
      </PageConfigProvider>,
    );

    await waitFor(() => expect(adminApi.getPageConfig).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));

    expect(screen.getByRole('checkbox', { name: '图表' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: '表单' })).toBeChecked();
    expect(screen.queryByRole('switch', { name: '转化率图表' })).not.toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: '转化率表单' })).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: '运营数据' })).toHaveTextContent('转化率');
  });

  it('combines the Query list visibility switch with its metric editor', async () => {
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2,
      version: 0,
      values: {},
      definitions: [],
    }));

    render(
      <PageConfigProvider>
        <PageConfigTab onDirtyChange={vi.fn()} />
      </PageConfigProvider>,
    );

    await waitFor(() => expect(adminApi.getPageConfig).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    const queryList = screen.getByRole('region', { name: 'Query 列表' });
    expect(queryList.querySelector('.page-config-tab__group-body')?.children).toHaveLength(1);
    expect(queryList.querySelector('button[aria-label="卡片指标：调整"]')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('switch', { name: '转化指标' }));
    expect(queryList.querySelector('button[aria-label="卡片指标：调整"]')).not.toBeInTheDocument();
  });

  it('hides the operations configuration panel when its entry switch is disabled', async () => {
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2,
      version: 0,
      values: {},
      definitions: [],
    }));

    render(
      <PageConfigProvider>
        <PageConfigTab onDirtyChange={vi.fn()} />
      </PageConfigProvider>,
    );

    await waitFor(() => expect(adminApi.getPageConfig).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('region', { name: '运营数据' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.click(screen.getByRole('switch', { name: '运营数据' }));
    expect(screen.queryByRole('region', { name: '运营数据' })).not.toBeInTheDocument();
  });

  it('opens every metric in an ordered list and applies switch changes from the modal', async () => {
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2,
      version: 0,
      values: {},
      definitions: [],
    }));

    render(
      <PageConfigProvider>
        <PageConfigTab onDirtyChange={vi.fn()} />
      </PageConfigProvider>,
    );

    await waitFor(() => expect(adminApi.getPageConfig).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.click(screen.getByRole('button', { name: '看板指标：调整' }));

    expect(screen.getByRole('dialog', { name: '看板指标' })).toBeInTheDocument();
    expect(screen.getByText('无召回率')).toBeInTheDocument();
    expect(screen.getByText('AI 处理')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: '看板指标：无召回率' })).not.toBeChecked();

    const rows = document.querySelectorAll('.page-config-tab__metric-modal-row');
    fireEvent.dragStart(rows[2]!);
    fireEvent.drop(rows[0]!);
    fireEvent.click(screen.getByRole('switch', { name: '看板指标：无召回率' }));
    fireEvent.click(screen.getByRole('button', { name: /应\s*用/ }));

    expect(screen.getByText('待处理；Query 数量；通过率；无召回率')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '看板指标第1项上移' })).not.toBeInTheDocument();
  });

  it('edits and saves the extensible page visibility settings', async () => {
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2,
      version: 0,
      values: {
        'brand_overview.card.show_operational_data': true,
        'brand_detail.yesterday.show': true,
      },
      definitions: [],
    }));
    vi.mocked(adminApi.updatePageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 2,
      version: 2,
      values: {
        'brand_overview.card.show_operational_data': false,
        'brand_detail.yesterday.show': true,
      },
      definitions: [],
    }));
    const onDirtyChange = vi.fn();

    render(
      <PageConfigProvider>
        <PageConfigTab onDirtyChange={onDirtyChange} />
      </PageConfigProvider>,
    );

    await waitFor(() => expect(adminApi.getPageConfig).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.click(screen.getByRole('switch', { name: '运营数据' }));
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByRole('button', { name: /保\s*存/ }));

    await waitFor(() => expect(adminApi.updatePageConfig).toHaveBeenCalledWith({
      expectedVersion: 0,
      values: expect.objectContaining({
        'brand_overview.card.show_operational_data': false,
        'brand_detail.yesterday.show': true,
      }),
    }));
    expect(await screen.findByText('页面配置已保存')).toBeInTheDocument();
  });
});
