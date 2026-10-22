import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import dayjs from 'dayjs';
import { Form } from 'antd';
import { Form as MobForm } from 'antd-mobile';
import {
  NoResultActionDetail,
  NoResultAgentTimeline,
  NoResultGovernanceDesktopView,
  NoResultGovernanceMobileView,
} from './components';
import type { NoResultGovernanceFilterValues } from './constants';
import type { NoResultGovernanceAction, NoResultGovernanceItem, QueryFilters } from '@/shared/types';


const sampleAction: NoResultGovernanceAction = {
  id: 12,
  actionType: 'delete_relationship',
  targetInterface: 'tagRelationship/deleteRelationship',
  status: 'manual_review',
  rollbackStatus: 'pending',
  riskLevel: 'high',
  agentReason: '需要人工复核',
  requestPayload: { riskExplanation: '高风险', rollbackSuggestion: '回滚' },
  responsePayload: { code: 0 },
  beforeSnapshot: { id: 'rel-1' },
  rollbackPayload: { id: 'rollback-1' },
  errorMessage: '等待审批',
  createdAt: '2026-06-22 09:00:00',
};

const sampleRecord: NoResultGovernanceItem = {
  id: 1,
  batchId: 7,
  dataDate: '2026-06-22',
  brand: 'atlas',
  query: '露营天幕',
  status: 'retagged_pending_review',
  aiDecision: 'process',
  aiReason: '购物意图明确',
  finalStrategy: '新增标签',
  beforeResultCount: 0,
  afterResultCount: 8,
  searchPv: 42,
  searchUv: 21,
  executionStatus: 'idle',
  actions: [sampleAction],
  agentTimeline: [],
  createdAt: '2026-06-22 09:00:00',
  processedAt: '2026-06-22 09:10:00',
};

function MobileHarness(props: {
  data?: NoResultGovernanceItem[];
  hasMore?: boolean;
  filterVisible?: boolean;
  pickerVisible?: boolean;
  onFetchData?: (values: QueryFilters, append?: boolean) => Promise<void>;
  onLoadMore?: () => Promise<void>;
}) {
  const [mobForm] = MobForm.useForm<NoResultGovernanceFilterValues>();
  const [mobileFilters, setMobileFilters] = React.useState<NoResultGovernanceFilterValues>({ date: dayjs('2026-06-22').toDate() });
  const [page, setPage] = React.useState(1);
  const [filterVisible, setFilterVisible] = React.useState(Boolean(props.filterVisible));
  const [pickerVisible, setPickerVisible] = React.useState(Boolean(props.pickerVisible));
  return (
    <NoResultGovernanceMobileView
      data={props.data ?? [sampleRecord]}
      loading={false}
      page={page}
      pageSize={10}
      hasMore={props.hasMore ?? true}
      filterVisible={filterVisible}
      pickerVisible={pickerVisible}
      mobileFilters={mobileFilters}
      mobForm={mobForm}
      onOpenDetail={vi.fn()}
      onExecuteTask={vi.fn()}
      onFetchData={props.onFetchData ?? vi.fn().mockResolvedValue(undefined)}
      onLoadMore={props.onLoadMore ?? vi.fn().mockResolvedValue(undefined)}
      onFilterVisibleChange={setFilterVisible}
      onPickerVisibleChange={setPickerVisible}
      normalizeMobileValues={(values) => values}
      setMobileFilters={setMobileFilters}
      setPage={setPage}
    />
  );
}

function DesktopHarness(props: { onSearch?: (values: NoResultGovernanceFilterValues) => void; onRollbackBrandDate?: () => void }) {
  const [form] = Form.useForm<NoResultGovernanceFilterValues>();
  return (
    <NoResultGovernanceDesktopView
      form={form}
      data={[sampleRecord]}
      total={1}
      loading={false}
      page={1}
      pageSize={10}
      columns={[{ title: 'Query', dataIndex: 'query', key: 'query' }]}
      onSearch={props.onSearch ?? vi.fn()}
      onFetchPage={vi.fn()}
      onRollbackBrandDate={props.onRollbackBrandDate ?? vi.fn()}
    />
  );
}

describe('NoResultGovernance components', () => {
  it('renders empty and populated agent timelines', () => {
    const { rerender } = render(<NoResultAgentTimeline events={[]} />);
    expect(screen.getByText('暂无 Agent 留痕')).toBeInTheDocument();

    rerender(
      <NoResultAgentTimeline
        events={[
          {
            id: 1,
            stage: 'diagnosis',
            eventType: 'tool_result',
            toolName: 'kg_lookup',
            summary: '命中图谱关系',
            payload: { relation: 'subordinate' },
            createdAt: '2026-06-22 09:03:00',
          },
        ]}
      />
    );
    expect(screen.getByText('kg_lookup')).toBeInTheDocument();
    expect(screen.getByText('命中图谱关系')).toBeInTheDocument();
    expect(screen.getByText(/"relation": "subordinate"/)).toBeInTheDocument();
  });

  it('renders action detail and handles manual review callbacks', () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(<NoResultActionDetail action={sampleAction} onApprove={onApprove} onReject={onReject} />);

    expect(screen.getByText('高风险')).toBeInTheDocument();
    expect(screen.getByText('高风险')).toBeInTheDocument();
    expect(screen.getByText(/"id": "rollback-1"/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '批准执行' }));
    fireEvent.click(screen.getByRole('button', { name: '拒绝动作' }));

    expect(onApprove).toHaveBeenCalledWith(12);
    expect(onReject).toHaveBeenCalledWith(12);
  });

  it('renders mobile view empty state and interactive list actions', async () => {
    const onFetchData = vi.fn().mockResolvedValue(undefined);
    const onLoadMore = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<MobileHarness data={[]} hasMore={false} onFetchData={onFetchData} onLoadMore={onLoadMore} />);
    expect(screen.getByText('暂无无结果词记录')).toBeInTheDocument();

    rerender(<MobileHarness onFetchData={onFetchData} onLoadMore={onLoadMore} />);
    fireEvent.click(screen.getByRole('button', { name: /筛选/i }));
    expect(screen.getByText('筛选条件')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重置' }));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));

    fireEvent.click(screen.getByRole('button', { name: '详情' }));
    fireEvent.click(screen.getByRole('button', { name: '一键执行' }));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '天幕' } });
    fireEvent.keyDown(screen.getByRole('searchbox'), { key: 'Enter', code: 'Enter' });

    await waitFor(() => {
      expect(onFetchData).toHaveBeenCalled();
    });
  });

  it('renders desktop view and submits filters with rollback action', async () => {
    const onSearch = vi.fn();
    const onRollbackBrandDate = vi.fn();
    render(<DesktopHarness onSearch={onSearch} onRollbackBrandDate={onRollbackBrandDate} />);

    expect(screen.getByText('无结果词列表')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /查\s*询/ }));
    await waitFor(() => {
      expect(onSearch).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole('button', { name: '回滚品牌日期' }));
    fireEvent.click(screen.getByText(/确\s*定/));
    expect(onRollbackBrandDate).toHaveBeenCalled();
  });
});
