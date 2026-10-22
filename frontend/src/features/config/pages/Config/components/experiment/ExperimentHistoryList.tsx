import React from 'react';
import dayjs from 'dayjs';
import { Button, Card, DatePicker, Empty, Pagination, Select, Space, Tag, Typography } from 'antd';
import type { TopQueryExperimentHistorySummary } from '@/shared/types';

interface Props {
  items: TopQueryExperimentHistorySummary[];
  activeExperimentId?: number;
  onOpen: (id: number) => void;
  total: number;
  page: number;
  pageSize: number;
  filters: { status?: string; startDate?: string; endDate?: string; page: number; pageSize: number };
  onChange: (filters: { status?: string; startDate?: string; endDate?: string; page: number; pageSize: number }) => void;
}

const statusLabel: Record<string, string> = { draft: '草稿', scheduled: '已排期', running: '运行中', ended: '已结束', cancelled: '已取消' };

const ExperimentHistoryList: React.FC<Props> = ({ items, activeExperimentId, onOpen, total, page, pageSize, filters, onChange }) => {
  const emit = (next: { status?: string; dateRange?: [string, string] | null; page?: number; pageSize?: number }) => {
    const nextStatus = Object.hasOwn(next, 'status') ? next.status : filters.status;
    const nextRange = Object.hasOwn(next, 'dateRange')
      ? next.dateRange
      : filters.startDate && filters.endDate ? [filters.startDate, filters.endDate] as [string, string] : null;
    onChange({ status: nextStatus, startDate: nextRange?.[0], endDate: nextRange?.[1], page: next.page ?? 1, pageSize: next.pageSize ?? pageSize });
  };
  return (
    <Card size="small" title="历史实验" className="experiment-history">
      <Space wrap className="experiment-history__filters">
        <Select aria-label="实验状态" allowClear placeholder="实验状态" value={filters.status} onChange={(value) => emit({ status: value, page: 1 })} options={Object.entries(statusLabel).map(([value, label]) => ({ value, label }))} style={{ width: 130 }} />
        <DatePicker.RangePicker value={filters.startDate && filters.endDate ? [dayjs(filters.startDate), dayjs(filters.endDate)] : null} onChange={(_, values) => { const range = values[0] && values[1] ? [values[0], values[1]] as [string, string] : null; emit({ dateRange: range, page: 1 }); }} />
      </Space>
      {items.length ? <div className="experiment-history__list">{items.map((item) => <button type="button" key={item.id} className={item.id === activeExperimentId ? 'is-active' : ''} onClick={() => onOpen(item.id)}><span><Typography.Text strong>{item.experimentName || `${item.brand.toUpperCase()} A/B Test #${item.id}`}</Typography.Text><small>{item.plannedStartDate || '-'} 至 {item.plannedEndDate || '-'}</small></span><span><Tag>{statusLabel[item.status] ?? item.status}</Tag><small>{item.treatmentCount}/{item.controlCount}</small></span></button>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配的历史实验" />}
      {total > pageSize ? <Pagination current={page} pageSize={pageSize} total={total} showSizeChanger onChange={(nextPage, nextSize) => emit({ page: nextPage, pageSize: nextSize })} /> : null}
      {activeExperimentId ? <Button type="link" onClick={() => onOpen(activeExperimentId)}>当前查看 #{activeExperimentId}</Button> : null}
    </Card>
  );
};

export default ExperimentHistoryList;
