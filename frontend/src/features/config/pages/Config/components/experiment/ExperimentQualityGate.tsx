import React from 'react';
import { Alert, Card, Progress, Tag, Typography } from 'antd';
import type { TopQueryExperimentQuality } from '@/shared/types';
import { formatQualityError, hasCompleteQualityEvidence } from './experimentQuality';

interface Props { quality: TopQueryExperimentQuality }

const ExperimentQualityGate: React.FC<Props> = ({ quality }) => {
  const evidenceComplete = hasCompleteQualityEvidence(quality);
  const passed = quality.status === 'complete' && evidenceComplete;
  const unknown = !evidenceComplete;
  const latest = quality.latestDayQuality ?? quality.latestDay ?? {};
  const numberValue = (key: string) => typeof latest[key] === 'number' && Number.isFinite(latest[key]) ? latest[key] as number : null;
  const display = (key: string) => numberValue(key) ?? '未知';
  const expected = numberValue('expectedSamples');
  const observed = numberValue('observedSamples');
  const treatmentTasks = numberValue('treatmentTaskCount');
  const contamination = numberValue('contaminationCount');
  const statusLabel = unknown ? '质量证据不完整' : passed ? '实验质量正常' : '实验质量异常';
  return (
    <Card size="small" title="数据质量门禁" extra={<Tag color={unknown ? 'gold' : passed ? 'green' : 'red'}>{statusLabel}</Tag>}>
      <div className="experiment-quality-grid">
        <div><Typography.Text type="secondary">观测样本</Typography.Text><strong>{display('observedSamples')}/{display('expectedSamples')}</strong><small><span>实验组 {display('treatmentSamples')}/20</span><span>对照组 {display('controlSamples')}/20</span></small><Progress percent={expected && observed !== null ? Math.min(100, observed / expected * 100) : 0} showInfo={false} status={passed ? 'success' : 'exception'} /></div>
        <div><Typography.Text type="secondary">实验组任务</Typography.Text><strong>{treatmentTasks ?? '未知'}/20</strong><small>必须全部收敛</small></div>
        <div><Typography.Text type="secondary">对照组隔离</Typography.Text><strong>对照任务 {display('controlTaskCount')}</strong><small>对照动作 {display('controlActionCount')}</small></div>
        <div><Typography.Text type="secondary">治理失败</Typography.Text><strong>失败/未收敛任务 {display('failedTaskCount')}/{display('unresolvedTaskCount')}</strong><small>失败/未收敛动作 {display('failedActionCount')}/{display('unresolvedActionCount')}</small></div>
        <div><Typography.Text type="secondary">对照污染</Typography.Text><strong>污染 {contamination ?? '未知'}</strong><small>必须为 0</small></div>
        <div><Typography.Text type="secondary">完整日</Typography.Text><strong>{quality.completeDays ?? 0}</strong><small>异常 {quality.failedDays ?? 0} 天</small></div>
      </div>
      {unknown ? <Alert type="warning" showIcon title="质量证据不完整，暂不输出效果结论" /> : !passed ? <Alert type="error" showIcon title={formatQualityError(quality.error)} /> : null}
    </Card>
  );
};

export default ExperimentQualityGate;
