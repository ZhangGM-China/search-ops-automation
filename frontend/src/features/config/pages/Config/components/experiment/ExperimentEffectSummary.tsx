import React from 'react';
import { Alert, Card, Statistic, Typography } from 'antd';
import type { TopQueryExperimentOverviewData } from '@/shared/types';
import { formatPercent, formatSignedPercent } from './experimentUtils';

interface Props { overview: TopQueryExperimentOverviewData; showConclusion?: boolean }

const formatDayCount = (value: number | null | undefined) => value == null ? '-' : `${value} 天`;
const formatSignedNumber = (value: number | null | undefined) => (
  value == null ? '-' : `${value > 0 ? '+' : ''}${value.toFixed(2)}`
);

const ExperimentEffectSummary: React.FC<Props> = ({ overview, showConclusion = true }) => {
  if (!overview.effect) {
    return <Card size="small"><Typography.Text type="secondary">质量门禁未通过，不生成效果结论。</Typography.Text></Card>;
  }
    return (
    <>
    {showConclusion ? <Alert type="info" showIcon title="效果结果已生成" description={<><span>请结合 CTR 与 CVR 查看</span><br /><span>结果仅适用于当前固定样本和观测窗口，不代表统计显著或长期因果效果。</span><br /><span>估算，不等同因果归因</span></>} /> : null}
    <div className="experiment-stat-grid">
      <Card size="small"><Statistic title="CTR 净效果" value={formatSignedPercent(overview.effect.ctrNetChange)} valueRender={(node) => <span className="experiment-stat-value">{node}</span>} /><small>实验组变化 - 对照组变化</small></Card>
      <Card size="small"><Statistic title="CVR 净效果" value={formatSignedPercent(overview.effect.cvrNetChange)} valueRender={(node) => <span className="experiment-stat-value">{node}</span>} /><small>Difference-in-differences</small></Card>
      <Card size="small"><Statistic title="CTR 正向效果天数" value={formatDayCount(overview.effect.ctrPositiveDays)} valueRender={(node) => <span>{node}</span>} /><small>观测窗口内净效果为正</small></Card>
      <Card size="small"><Statistic title="CVR 正向效果天数" value={formatDayCount(overview.effect.cvrPositiveDays)} valueRender={(node) => <span>{node}</span>} /><small>观测窗口内净效果为正</small></Card>
      <Card size="small"><Statistic title="预估 Click UV 增量" value={formatSignedNumber(overview.effect.estimatedIncrementalClickUv)} valueRender={(node) => <span>{node}</span>} /><small>按当前实验组 Search UV 估算</small></Card>
      <Card size="small"><Statistic title="预估 Purchase UV 增量" value={formatSignedNumber(overview.effect.estimatedIncrementalPurchaseUv)} valueRender={(node) => <span>{node}</span>} /><small>按当前实验组 Search UV 估算</small></Card>
      <Card size="small"><Statistic title="治理覆盖率" value={formatPercent(overview.operations.governanceCoverage)} valueRender={(node) => <span>{node}</span>} /><small>实验组 20 个 Query</small></Card>
      <Card size="small"><Statistic title="动作成功率" value={formatPercent(overview.operations.actionSuccessRate)} valueRender={(node) => <span>{node}</span>} /><small>{overview.operations.executedActions}/{overview.operations.plannedActions} 已执行</small></Card>
    </div>
    <div className="experiment-group-grid">
      {(['treatment', 'control'] as const).map((key) => {
        const group = overview.groups[key];
        if (!group) return null;
        return <Card size="small" key={key} title={key === 'treatment' ? '实验组指标' : '对照组指标'}>
          <div className="experiment-group-metrics"><span>CTR 基线 {formatPercent(group.baselineCtr)}</span><span>当前 {formatPercent(group.currentCtr)}</span><strong>变化 {formatPercent(group.ctrChange)}</strong></div>
          <div className="experiment-group-metrics"><span>CVR 基线 {formatPercent(group.baselineCvr)}</span><span>当前 {formatPercent(group.currentCvr)}</span><strong>变化 {formatPercent(group.cvrChange)}</strong></div>
        </Card>;
      })}
    </div>
    </>
  );
};

export default ExperimentEffectSummary;
