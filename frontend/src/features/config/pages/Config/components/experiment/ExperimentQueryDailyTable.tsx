import React from 'react';
import { Card, Empty, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TopQueryExperimentDailyMetric } from '@/shared/types';
import { formatPercent } from './experimentUtils';
import {
  contaminationStatusLabel,
  dataQualityStatusLabel,
  experimentGroupLabel,
  numeratorEvidenceLabel,
  numeratorSourceLabel,
  observationStatusLabel,
} from './experimentLabels';

interface Props { items: TopQueryExperimentDailyMetric[]; isMobile: boolean }

const ExperimentQueryDailyTable: React.FC<Props> = ({ items, isMobile }) => {
  const columns: ColumnsType<TopQueryExperimentDailyMetric> = [
    { title: '日期', dataIndex: 'dataDate', key: 'date', fixed: 'left' },
    { title: 'Query', dataIndex: 'queryText', key: 'query', fixed: 'left' },
    { title: '分组', dataIndex: 'groupType', key: 'group', render: (value) => experimentGroupLabel(value) },
    { title: 'Search UV', dataIndex: 'searchUv', key: 'searchUv', render: (value) => value ?? '-' },
    { title: 'Click UV', dataIndex: 'clickUv', key: 'clickUv', render: (value) => value ?? '-' },
    { title: 'Purchase UV', dataIndex: 'purchaseUv', key: 'purchaseUv', render: (value) => value ?? '-' },
    { title: 'CTR', dataIndex: 'ctr', key: 'ctr', render: (value) => formatPercent(value) },
    { title: 'CVR', dataIndex: 'cvr', key: 'cvr', render: (value) => formatPercent(value) },
    { title: '观测', dataIndex: 'observedStatus', key: 'observed', render: (value) => <Tag>{observationStatusLabel(value)}</Tag> },
    { title: '污染', dataIndex: 'contaminationStatus', key: 'contamination', render: (value) => <Tag color={value === 'clean' ? 'green' : 'red'}>{contaminationStatusLabel(value)}</Tag> },
    { title: '分子来源', dataIndex: 'numeratorSource', key: 'numerator', render: (value) => numeratorSourceLabel(value) },
    { title: '分子证据', key: 'numeratorEvidence', render: (_, item) => numeratorEvidenceLabel(item.clickNumeratorStatus, item.purchaseNumeratorStatus) },
    { title: '数据质量', dataIndex: 'dataQualityStatus', key: 'quality', render: (value) => <Tag color={value === 'complete' ? 'green' : 'red'}>{dataQualityStatusLabel(value)}</Tag> },
    { title: '计入效果', dataIndex: 'includedInEffect', key: 'included', render: (value) => value ? '是' : '否' },
  ];
  if (!items.length) return <Card size="small"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Query 每日数据" /></Card>;
  if (isMobile) return <div className="experiment-mobile-list">{items.map((item, index) => (
    <article className="experiment-mobile-query-card" key={`${item.dataDate}-${item.groupType}-${item.queryText}-${index}`}>
      <div><strong>{item.queryText}</strong><Tag>{experimentGroupLabel(item.groupType)}</Tag></div>
      <small>{item.dataDate ?? '-'}</small>
      <dl>
        <div><dt>Search / Click / Purchase UV</dt><dd>{item.searchUv ?? '-'} / {item.clickUv ?? '-'} / {item.purchaseUv ?? '-'}</dd></div>
        <div><dt>CTR / CVR</dt><dd>{formatPercent(item.ctr)} / {formatPercent(item.cvr)}</dd></div>
        <div><dt>观测 / 污染</dt><dd><span>{observationStatusLabel(item.observedStatus)}</span> / <span>{contaminationStatusLabel(item.contaminationStatus)}</span></dd></div>
        <div><dt>分子来源</dt><dd>{numeratorSourceLabel(item.numeratorSource)}</dd></div>
        <div><dt>分子证据</dt><dd>{numeratorEvidenceLabel(item.clickNumeratorStatus, item.purchaseNumeratorStatus)}</dd></div>
        <div><dt>数据质量</dt><dd>{dataQualityStatusLabel(item.dataQualityStatus)}</dd></div>
        <div><dt>计入效果</dt><dd>{item.includedInEffect ? '是' : '否'}</dd></div>
      </dl>
    </article>
  ))}</div>;
  return <Table rowKey={(item) => `${item.dataDate}-${item.groupType}-${item.canonicalQueryKey ?? item.queryText}`} columns={columns} dataSource={items} size="small" pagination={{ pageSize: 20 }} scroll={{ x: 1400 }} />;
};

export default ExperimentQueryDailyTable;
