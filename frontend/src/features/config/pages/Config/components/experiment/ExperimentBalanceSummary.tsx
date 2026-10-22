import React from 'react';
import { Progress, Typography } from 'antd';

interface Props { treatmentCount: number; controlCount: number; sampleDataDate?: string }

const ExperimentBalanceSummary: React.FC<Props> = ({ treatmentCount, controlCount, sampleDataDate }) => (
  <div className="experiment-balance-summary">
    <Typography.Text type="secondary">样本基准日 {sampleDataDate || '-'}</Typography.Text>
    <span>实验组 {treatmentCount}/20</span>
    <Progress percent={Math.round((treatmentCount + controlCount) / 40 * 100)} showInfo={false} status={treatmentCount === 20 && controlCount === 20 ? 'success' : 'normal'} />
    <span>对照组 {controlCount}/20</span>
  </div>
);

export default ExperimentBalanceSummary;
