import React from 'react';
import { Card, Empty, Select, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TopQueryExperimentQueryAnalytics } from '@/shared/types';
import useMobile from '@/shared/hooks/useMobile';
import { formatPercent, formatSignedPercent } from './experimentUtils';

interface Props { items: TopQueryExperimentQueryAnalytics[]; qualityPassed?: boolean; isMobile?: boolean; title?: string | false }

type GroupFilter = 'all' | 'treatment' | 'control';
type EffectDirectionFilter = 'all' | 'positive' | 'negative' | 'mixed' | 'neutral' | 'undetermined';
type QualityFilter = 'all' | 'complete' | 'failed';

const observedStatus = (value: string) => {
  if (value === 'observed') return { label: '已观测', color: 'green' };
  if (value === 'not_synced') return { label: '未同步', color: 'default' };
  if (value === 'data_quality_failed') return { label: '数据异常', color: 'red' };
  if (['missing', 'unobserved', 'not_observed'].includes(value)) return { label: '未观测', color: 'red' };
  return { label: value || '未知', color: 'default' };
};

const contaminationStatus = (value: string) => {
  if (value === 'clean') return { label: '无污染', color: 'green' };
  if (value === 'contaminated') return { label: '已污染', color: 'red' };
  return { label: value || '未知', color: 'default' };
};

const effectDirectionStatus = (value: string) => {
  if (value === 'positive') return { label: '正向', color: 'green' };
  if (value === 'negative') return { label: '负向', color: 'red' };
  if (value === 'mixed') return { label: '效果分化', color: 'gold' };
  if (value === 'neutral') return { label: '基本持平', color: 'default' };
  if (['undetermined', 'unavailable'].includes(value)) return { label: '不可判定', color: 'default' };
  return { label: value || '不可判定', color: 'default' };
};

const ExperimentQueryTable: React.FC<Props> = ({ items, qualityPassed = true, isMobile: forcedMobile, title = 'Query 贡献' }) => {
  const detectedMobile = useMobile();
  const isMobile = forcedMobile ?? detectedMobile;
  const [groupFilter, setGroupFilter] = React.useState<GroupFilter>('all');
  const [effectDirectionFilter, setEffectDirectionFilter] = React.useState<EffectDirectionFilter>('all');
  const [qualityFilter, setQualityFilter] = React.useState<QualityFilter>('all');
  const filteredItems = React.useMemo(() => items.filter((item) => {
    if (groupFilter !== 'all' && item.groupType !== groupFilter) return false;
    if (effectDirectionFilter === 'undetermined') {
      if (!['undetermined', 'unavailable'].includes(item.effectDirection)) return false;
    } else if (effectDirectionFilter !== 'all' && item.effectDirection !== effectDirectionFilter) {
      return false;
    }
    if (qualityFilter === 'complete' && item.dataQualityStatus !== 'complete') return false;
    if (qualityFilter === 'failed' && item.dataQualityStatus === 'complete') return false;
    return true;
  }), [effectDirectionFilter, groupFilter, items, qualityFilter]);
  const columns: ColumnsType<TopQueryExperimentQueryAnalytics> = React.useMemo(() => [
    { title: 'Query', dataIndex: 'queryText', key: 'queryText', render: (value, record) => <div><strong>{value}</strong><div><Tag color={record.groupType === 'treatment' ? 'blue' : 'default'}>{record.groupType === 'treatment' ? '实验组' : '对照组'}</Tag></div></div> },
    { title: '周期 UV', dataIndex: 'currentSearchUv', key: 'uv', render: (value) => value ?? '-' },
    { title: '基线 CTR', dataIndex: 'baselineCtr', key: 'baselineCtr', render: (value) => formatPercent(value) },
    { title: '当前 CTR', dataIndex: 'currentCtr', key: 'ctr', render: (value) => formatPercent(value) },
    { title: 'CTR 变化', dataIndex: 'ctrChange', key: 'ctrChange', render: (value) => formatSignedPercent(value) },
    { title: '基线 CVR', dataIndex: 'baselineCvr', key: 'baselineCvr', render: (value) => formatPercent(value) },
    { title: '当前 CVR', dataIndex: 'currentCvr', key: 'cvr', render: (value) => formatPercent(value) },
    { title: 'CVR 变化', dataIndex: 'cvrChange', key: 'cvrChange', render: (value) => formatSignedPercent(value) },
    { title: '观测', dataIndex: 'observedStatus', key: 'observed', render: (value) => { const status = observedStatus(value); return <Tag color={status.color}>{status.label}</Tag>; } },
    { title: '污染', dataIndex: 'contaminationStatus', key: 'contamination', render: (value) => { const status = contaminationStatus(value); return <Tag color={status.color}>{status.label}</Tag>; } },
    { title: '效果方向', dataIndex: 'effectDirection', key: 'effectDirection', render: (value) => { const status = effectDirectionStatus(value); return <Tag color={status.color}>{status.label}</Tag>; } },
    { title: 'CTR 贡献', dataIndex: 'netCtrContribution', key: 'contribution', render: (value) => qualityPassed ? formatPercent(value) : '质量未通过' },
    { title: 'CVR 贡献', dataIndex: 'netCvrContribution', key: 'cvrContribution', render: (value) => qualityPassed ? formatPercent(value) : '质量未通过' },
    { title: '治理', dataIndex: 'governanceStatus', key: 'governance', render: (value, record) => record.groupType === 'control' ? '只观测' : value === 'completed' ? '已完成' : (value ?? '-') },
    { title: '动作 / Token / 耗时', key: 'cost', render: (_, record) => `${record.actionCount} / ${record.tokenUsage} / ${record.durationSeconds}秒` },
  ], [qualityPassed]);
  const cardTitle = title === false ? undefined : title;
  const filters = <div className="experiment-query-filters">
    <Select aria-label="分组筛选" value={groupFilter} onChange={setGroupFilter} options={[{ value: 'all', label: '全部分组' }, { value: 'treatment', label: '实验组' }, { value: 'control', label: '对照组' }]} />
    <Select aria-label="效果方向筛选" value={effectDirectionFilter} onChange={setEffectDirectionFilter} options={[{ value: 'all', label: '全部效果方向' }, { value: 'positive', label: '正向' }, { value: 'negative', label: '负向' }, { value: 'mixed', label: '效果分化' }, { value: 'neutral', label: '基本持平' }, { value: 'undetermined', label: '不可判定' }]} />
    <Select aria-label="质量状态筛选" value={qualityFilter} onChange={setQualityFilter} options={[{ value: 'all', label: '全部质量状态' }, { value: 'complete', label: '质量完整' }, { value: 'failed', label: '质量异常' }]} />
  </div>;
  if (!items.length) return <Card size="small" title={cardTitle}>{filters}<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Query 效果数据" /></Card>;
  const content = !filteredItems.length
    ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选无 Query 效果数据" />
    : isMobile
      ? <div className="experiment-mobile-list">{filteredItems.map((item) => { const observed = observedStatus(item.observedStatus); const contamination = contaminationStatus(item.contaminationStatus); const direction = effectDirectionStatus(item.effectDirection); const governance = item.groupType === 'control' ? '只观测' : item.governanceStatus === 'completed' ? '已完成' : (item.governanceStatus ?? '-'); return <article key={`${item.groupType}-${item.queryText}`} className="experiment-mobile-query-card"><div><strong>{item.queryText}</strong><Tag>{item.groupType === 'treatment' ? '实验组' : '对照组'}</Tag></div><dl><div><dt>周期 UV</dt><dd>{item.currentSearchUv ?? '-'}</dd></div><div><dt>CTR 基线 / 当前 / 变化</dt><dd>{formatPercent(item.baselineCtr)} / {formatPercent(item.currentCtr)} / {formatSignedPercent(item.ctrChange)}</dd></div><div><dt>CVR 基线 / 当前 / 变化</dt><dd>{formatPercent(item.baselineCvr)} / {formatPercent(item.currentCvr)} / {formatSignedPercent(item.cvrChange)}</dd></div><div><dt>观测状态</dt><dd><Tag color={observed.color}>{observed.label}</Tag></dd></div><div><dt>污染状态</dt><dd><Tag color={contamination.color}>{contamination.label}</Tag></dd></div><div><dt>效果方向</dt><dd><Tag color={direction.color}>{direction.label}</Tag></dd></div><div><dt>CTR 贡献</dt><dd>{qualityPassed ? formatPercent(item.netCtrContribution) : '质量未通过'}</dd></div><div><dt>CVR 贡献</dt><dd>{qualityPassed ? formatPercent(item.netCvrContribution) : '质量未通过'}</dd></div><div><dt>治理 / 成本</dt><dd><span>{governance}</span> / {item.actionCount} 动作 / {item.tokenUsage} Token / {item.durationSeconds}秒</dd></div></dl></article>; })}</div>
      : <Table rowKey={(item) => `${item.groupType}-${item.queryText}`} columns={columns} dataSource={filteredItems} size="small" pagination={false} scroll={title === false ? { x: 1580 } : undefined} className="experiment-query-table" />;
  return <Card size="small" title={cardTitle}>{filters}{content}</Card>;
};

export default React.memo(ExperimentQueryTable);
