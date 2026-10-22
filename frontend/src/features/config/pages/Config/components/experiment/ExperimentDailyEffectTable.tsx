import React from 'react';
import { Card, Empty, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TopQueryExperimentGroupAnalytics, TopQueryExperimentTrendItem } from '@/shared/types';
import { formatPercent, formatSignedPercent } from './experimentUtils';

interface Props { items: TopQueryExperimentTrendItem[]; isMobile: boolean; qualityPassed?: boolean }

interface DailyGroupRow extends TopQueryExperimentGroupAnalytics {
  key: string;
  dataDate: string;
  groupType: 'treatment' | 'control';
  ctrNetChange: number | null;
  cvrNetChange: number | null;
  qualityStatus: string;
}

const groupLabel = (value: string) => value === 'treatment' ? '实验组' : '对照组';
const dailyEffectText = (row: DailyGroupRow, qualityPassed: boolean) => (
  qualityPassed && row.qualityStatus === 'complete'
    ? `CTR ${formatSignedPercent(row.ctrNetChange)} / CVR ${formatSignedPercent(row.cvrNetChange)}`
    : '质量未通过，不输出净效果'
);

const ExperimentDailyEffectTable: React.FC<Props> = ({ items, isMobile, qualityPassed = true }) => {
  const rows = React.useMemo(() => items.flatMap((item) => (['treatment', 'control'] as const).flatMap((groupType) => {
    const group = item.groups[groupType];
    return group ? [{
      ...group,
      key: `${item.dataDate}-${groupType}`,
      dataDate: item.dataDate,
      groupType,
      ctrNetChange: item.effect?.ctrNetChange ?? null,
      cvrNetChange: item.effect?.cvrNetChange ?? null,
      qualityStatus: item.quality.status,
    }] : [];
  })), [items]);
  const columns: ColumnsType<DailyGroupRow> = [
    { title: '日期', dataIndex: 'dataDate', key: 'date' },
    { title: '分组', dataIndex: 'groupType', key: 'group', render: (value) => <Tag color={value === 'treatment' ? 'blue' : 'default'}>{groupLabel(value)}</Tag> },
    { title: 'Search UV', dataIndex: 'currentSearchUv', key: 'uv', render: (value, row) => value ?? row.searchUv ?? '-' },
    { title: 'CTR', dataIndex: 'currentCtr', key: 'ctr', render: (value) => formatPercent(value) },
    { title: 'CTR 变化', dataIndex: 'ctrChange', key: 'ctrChange', render: (value) => formatSignedPercent(value) },
    { title: 'CVR', dataIndex: 'currentCvr', key: 'cvr', render: (value) => formatPercent(value) },
    { title: 'CVR 变化', dataIndex: 'cvrChange', key: 'cvrChange', render: (value) => formatSignedPercent(value) },
    { title: '当日净效果', key: 'effect', render: (_, row) => dailyEffectText(row, qualityPassed) },
    { title: '质量', dataIndex: 'qualityStatus', key: 'quality', render: (value) => <Tag color={value === 'complete' ? 'green' : 'red'}>{value === 'complete' ? '完整' : '异常'}</Tag> },
  ];
  if (!rows.length) return <Card size="small"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无每日组效果数据" /></Card>;
  if (isMobile) return <div className="experiment-mobile-list">{rows.map((row) => (
    <article className="experiment-mobile-query-card" key={row.key}>
      <div><strong>{row.dataDate}</strong><Tag color={row.groupType === 'treatment' ? 'blue' : 'default'}>{groupLabel(row.groupType)}</Tag></div>
      <dl>
        <div><dt>Search UV</dt><dd>{row.currentSearchUv ?? row.searchUv}</dd></div>
        <div><dt>CTR / 变化</dt><dd>{formatPercent(row.currentCtr)} / {formatSignedPercent(row.ctrChange)}</dd></div>
        <div><dt>CVR / 变化</dt><dd>{formatPercent(row.currentCvr)} / {formatSignedPercent(row.cvrChange)}</dd></div>
        <div><dt>当日净效果</dt><dd>{dailyEffectText(row, qualityPassed)}</dd></div>
        <div><dt>质量</dt><dd>{row.qualityStatus === 'complete' ? '完整' : '异常'}</dd></div>
      </dl>
    </article>
  ))}</div>;
  return <Table rowKey="key" columns={columns} dataSource={rows} size="small" pagination={false} scroll={{ x: 1080 }} />;
};

export default ExperimentDailyEffectTable;
