import React from 'react';
import { Card } from 'antd';
import ExperimentBalanceSummary from './ExperimentBalanceSummary';

interface Props {
  treatmentCount: number;
  controlCount: number;
  sampleDataDate?: string;
  children: React.ReactNode;
}

const ExperimentDraftBuilder: React.FC<Props> = ({ treatmentCount, controlCount, sampleDataDate, children }) => (
  <Card
    size="small"
    title="建立固定样本"
    extra={<ExperimentBalanceSummary treatmentCount={treatmentCount} controlCount={controlCount} sampleDataDate={sampleDataDate} />}
  >
    {children}
  </Card>
);

export default ExperimentDraftBuilder;
