import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { TopQueryExperimentActionAudit, TopQueryExperimentOperations } from '@/shared/types';
import ExperimentActionAudit from './ExperimentActionAudit';

const operations: TopQueryExperimentOperations = {
  governanceCoverage: 1,
  actionSuccessRate: 0.5,
  plannedActions: 8,
  executedActions: 2,
  failedActions: 1,
  inconclusiveActions: 2,
  unresolvedActions: 1,
  rejectedActions: 1,
  cancelledActions: 1,
  tokenUsage: 100,
  durationSeconds: 20,
};

const actions: TopQueryExperimentActionAudit[] = [
  { recordType: 'action', detailAvailable: true, id: 1, queryText: '验证失败 Query', status: 'verification_failed' },
  { recordType: 'action', detailAvailable: true, id: 2, queryText: '验证未确定 Query', status: 'verification_inconclusive' },
  { recordType: 'action', detailAvailable: true, id: 3, queryText: '执行未知 Query', status: 'execution_unknown' },
  { recordType: 'action', detailAvailable: true, id: 4, queryText: '演练 Query', status: 'dry_run_executed' },
];

describe('ExperimentActionAudit', () => {
  it('maps real action statuses to Chinese semantic states without exposing raw English', () => {
    render(<ExperimentActionAudit actions={actions} operations={operations} isMobile={false} />);

    expect(screen.getByText('验证失败').closest('.ant-tag')).toHaveClass('ant-tag-error');
    expect(screen.getByText('验证未完成/未确定').closest('.ant-tag')).toHaveClass('ant-tag-warning');
    expect(screen.getByText('执行结果未知').closest('.ant-tag')).toHaveClass('ant-tag-warning');
    expect(screen.getByText('演练已执行（非真实生效）').closest('.ant-tag')).toHaveClass('ant-tag-warning');
    for (const rawStatus of ['verification_failed', 'verification_inconclusive', 'execution_unknown', 'dry_run_executed']) {
      expect(screen.queryByText(rawStatus)).not.toBeInTheDocument();
    }
  });

  it('shows failed, inconclusive and unresolved action counts explicitly', () => {
    render(<ExperimentActionAudit actions={[]} operations={operations} isMobile={false} />);
    expect(screen.getByText('已执行 2 / 失败 1 / 未确定 2 / 未收口 1')).toBeVisible();
    expect(screen.getByText('已拒绝 1 / 已取消 1')).toBeVisible();
  });
});
