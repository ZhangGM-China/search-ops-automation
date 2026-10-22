import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import QueryGovernanceStepStatusStrip from './QueryGovernanceStepStatusStrip';

describe('QueryGovernanceStepStatusStrip', () => {
  it('shows only checks allowed for this Query', () => {
    render(<QueryGovernanceStepStatusStrip steps={[
      { checkKey: 'recall', status: 'executed', displayStatus: 'executed' },
      { checkKey: 'inventory', status: 'skipped', displayStatus: 'skipped' },
    ]} visibleCheckKeys={['recall']} compact ariaLabel="checks" />);
    expect(screen.getByText('召回数量')).toBeInTheDocument();
    expect(screen.queryByText('断码')).not.toBeInTheDocument();
  });
  it('keeps a successful execution green when no issue was detected', () => {
    render(<QueryGovernanceStepStatusStrip steps={[{ checkKey: 'recall', status: 'executed', displayStatus: '已执行' }]} compact ariaLabel="checks" />);
    expect(screen.getByText('召回数量').parentElement).toHaveClass('ops-top-query-step-tag--green');
  });

  it('keeps a localized successful execution green when no issue was detected', () => {
    render(<QueryGovernanceStepStatusStrip steps={[{ checkKey: 'recall', status: 'pass', displayStatus: '已执行' }]} compact ariaLabel="checks" />);
    expect(screen.getByText('召回数量').parentElement).toHaveClass('ops-top-query-step-tag--green');
  });

  it('marks fixed recall and relevance issues blue', () => {
    render(<QueryGovernanceStepStatusStrip steps={[
      { checkKey: 'recall', status: 'fixed', displayStatus: 'fixed' },
      { checkKey: 'relevance', status: 'fixed', displayStatus: 'fixed' },
    ]} compact ariaLabel="checks" />);
    expect(screen.getByText('召回数量').parentElement).toHaveClass('ops-top-query-step-tag--blue');
    expect(screen.getByText('相关性').parentElement).toHaveClass('ops-top-query-step-tag--blue');
  });

  it('keeps normal reorder execution green and execution errors red', () => {
    render(<QueryGovernanceStepStatusStrip steps={[
      { checkKey: 'open_factor', status: 'executed', displayStatus: 'executed' },
      { checkKey: 'inventory', status: 'failed', displayStatus: 'failed' },
    ]} compact ariaLabel="checks" />);
    expect(screen.getByText('重排序').parentElement).toHaveClass('ops-top-query-step-tag--green');
    expect(screen.getByText('断码').parentElement).toHaveClass('ops-top-query-step-tag--red');
  });

  it('marks an executed reorder with issue evidence blue', () => {
    render(<QueryGovernanceStepStatusStrip steps={[
      { checkKey: 'open_factor', status: 'executed', displayStatus: 'executed', evidence: { issueTypes: ['should_reorder'] } },
    ]} compact ariaLabel="checks" />);
    expect(screen.getByText('重排序').parentElement).toHaveClass('ops-top-query-step-tag--blue');
  });

  it('does not treat skipped checks as successful improvement work', () => {
    render(<QueryGovernanceStepStatusStrip steps={[
      { checkKey: 'inventory', status: 'skipped', displayStatus: 'skipped', evidence: { issueTypes: ['inventory'] } },
    ]} compact ariaLabel="checks" />);
    expect(screen.getByText('断码').parentElement).toHaveClass('ops-top-query-step-tag--default');
  });

  it('keeps a successfully executed check blue when its evidence contains a business issue', () => {
    render(<QueryGovernanceStepStatusStrip steps={[
      { checkKey: 'inventory', status: 'executed', displayStatus: 'executed', evidence: { issueTypes: ['inventory'] } },
    ]} compact ariaLabel="checks" />);
    expect(screen.getByText('断码').parentElement).toHaveClass('ops-top-query-step-tag--blue');
  });

  it('marks a check red when its downstream governance generation fails', () => {
    render(<QueryGovernanceStepStatusStrip steps={[{
      checkKey: 'recall', status: 'executed', displayStatus: 'executed',
      evidence: { issueTypes: ['result_count_drop'] },
      governanceStatus: { status: 'failed', label: '生成失败', errorCode: 'KG_AGENT_INVALID_JSON' },
    }]} compact ariaLabel="checks" />);
    expect(screen.getByText('召回数量').parentElement).toHaveClass('ops-top-query-step-tag--red');
  });

  it('marks a check yellow while its downstream governance plan is pending', () => {
    render(<QueryGovernanceStepStatusStrip steps={[{
      checkKey: 'recall', status: 'executed', displayStatus: 'executed',
      evidence: { issueTypes: ['low_result'] },
      governanceStatus: { status: 'pending', label: '生成中' },
    }]} compact ariaLabel="checks" />);
    expect(screen.getByText('召回数量').parentElement).toHaveClass('ops-top-query-step-tag--yellow');
  });

  it('labels each tag with the resolved check state instead of the raw node status', () => {
    render(<QueryGovernanceStepStatusStrip steps={[
      { checkKey: 'recall', status: 'executed', displayStatus: '已执行' },
      { checkKey: 'relevance', status: 'executed', displayStatus: '已执行', evidence: { issueTypes: ['relevance'] } },
      { checkKey: 'open_factor', status: 'executed', displayStatus: '已执行', governanceStatus: { status: 'pending', label: '生成中' } },
      { checkKey: 'inventory', status: 'skipped', displayStatus: '已跳过' },
    ]} ariaLabel="checks" />);

    expect(screen.getByText('召回数量 · 正常')).toBeInTheDocument();
    expect(screen.getByText('相关性 · 需关注')).toBeInTheDocument();
    expect(screen.getByText('重排序 · 待处理')).toBeInTheDocument();
    expect(screen.getByText('断码 · 未执行')).toBeInTheDocument();
    expect(screen.queryByText('召回数量 · 已执行')).not.toBeInTheDocument();
  });

  it('explains the resolved check state together with the governance label in the tooltip', () => {
    render(<QueryGovernanceStepStatusStrip steps={[{
      checkKey: 'recall', status: 'executed', displayStatus: '已执行',
      governanceStatus: { status: 'failed', label: '生成失败', errorMessage: 'agent response must be JSON' },
    }]} compact ariaLabel="checks" />);

    expect(screen.getByText('召回数量').parentElement).toHaveAttribute(
      'title',
      '召回数量：检查异常；治理生成失败（agent response must be JSON）',
    );
  });
});
