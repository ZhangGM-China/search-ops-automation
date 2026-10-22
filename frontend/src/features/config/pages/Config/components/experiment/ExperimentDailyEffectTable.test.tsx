import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { TopQueryExperimentTrendItem } from '@/shared/types';
import ExperimentDailyEffectTable from './ExperimentDailyEffectTable';

const operations = { governanceCoverage: 1, actionSuccessRate: 1, plannedActions: 1, executedActions: 1, failedActions: 0, tokenUsage: 10, durationSeconds: 2 };
const group = { sampleCount: 20, searchUv: 100, searchPv: 200, baselineCtr: 0.1, baselineCvr: 0.02, currentCtr: 0.12, currentCvr: 0.03, ctrChange: 0.02, cvrChange: 0.01 };
const items: TopQueryExperimentTrendItem[] = [
  { dataDate: '2026-07-09', quality: { status: 'complete' }, groups: { treatment: group, control: group }, effect: { ctrNetChange: 0.01, cvrNetChange: 0.002 }, operations },
  { dataDate: '2026-07-10', quality: { status: 'failed' }, groups: { treatment: group, control: group }, effect: { ctrNetChange: 0.03, cvrNetChange: 0.004 }, operations },
];

describe('ExperimentDailyEffectTable', () => {
  it('shows complete-day effects and suppresses only failed-day effects', () => {
    render(<ExperimentDailyEffectTable items={items} isMobile={false} qualityPassed />);
    expect(screen.getAllByText('CTR +1.00% / CVR +0.20%')).toHaveLength(2);
    expect(screen.queryByText('CTR +3.00% / CVR +0.40%')).not.toBeInTheDocument();
    expect(screen.getAllByText('质量未通过，不输出净效果')).toHaveLength(2);
  });

  it('suppresses every day when the baseline or calculation version is invalid', () => {
    render(<ExperimentDailyEffectTable items={items} isMobile qualityPassed={false} />);
    expect(screen.queryByText('CTR +1.00% / CVR +0.20%')).not.toBeInTheDocument();
    expect(screen.getAllByText('质量未通过，不输出净效果')).toHaveLength(4);
  });
});
