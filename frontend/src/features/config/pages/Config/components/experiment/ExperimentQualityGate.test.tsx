import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import ExperimentQualityGate from './ExperimentQualityGate';

describe('ExperimentQualityGate', () => {
  it('treats a complete status without the full latest-day evidence as unknown', () => {
    render(<ExperimentQualityGate quality={{ status: 'complete', latestDayQuality: { expectedSamples: 40, observedSamples: 40, treatmentSamples: 20, controlSamples: 20 } }} />);
    expect(screen.getByText('质量证据不完整')).toBeInTheDocument();
    expect(screen.queryByText('实验质量正常')).not.toBeInTheDocument();
    expect(screen.getByText('数据质量门禁').closest('.ant-card')).toHaveTextContent('实验组任务未知/20');
  });

  it('shows the complete 40/40 and governance quality contract', () => {
    render(<ExperimentQualityGate quality={{ status: 'failed', latestDayQuality: { expectedSamples: 40, observedSamples: 39, treatmentSamples: 20, controlSamples: 19, treatmentTaskCount: 19, controlTaskCount: 0, controlActionCount: 0, contaminationCount: 1, failedTaskCount: 1, unresolvedTaskCount: 2, failedActionCount: 1, unresolvedActionCount: 3 } }} />);
    expect(screen.getByText('39/40')).toBeInTheDocument();
    expect(screen.getByText('实验组 20/20')).toBeInTheDocument();
    expect(screen.getByText('对照组 19/20')).toBeInTheDocument();
    expect(screen.getByText('19/20')).toBeInTheDocument();
    expect(screen.getByText('对照任务 0')).toBeInTheDocument();
    expect(screen.getByText('对照动作 0')).toBeInTheDocument();
    expect(screen.getByText('失败/未收敛任务 1/2')).toBeInTheDocument();
    expect(screen.getByText('失败/未收敛动作 1/3')).toBeInTheDocument();
    expect(screen.getByText('污染 1')).toBeInTheDocument();
  });

  it('translates quality error codes for operators', () => {
    render(<ExperimentQualityGate quality={{ status: 'failed', error: 'governance_not_resolved', latestDayQuality: { expectedSamples: 40, observedSamples: 40, treatmentSamples: 20, controlSamples: 20, treatmentTaskCount: 20, controlTaskCount: 0, controlActionCount: 0, contaminationCount: 0, failedTaskCount: 0, unresolvedTaskCount: 1, failedActionCount: 0, unresolvedActionCount: 1 } }} />);
    expect(screen.getByText('实验组治理任务或动作尚未全部收敛')).toBeInTheDocument();
  });
});
