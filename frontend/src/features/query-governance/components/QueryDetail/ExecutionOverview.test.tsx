import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ExecutionOverview from './ExecutionOverview';

describe('ExecutionOverview', () => {
  it('renders four nodes inside one divided overview panel and opens the selected node', () => {
    const onOpen = vi.fn();
    const nodes = [
      { key: 'recall', label: '召回数量', state: 'normal', summary: '检查完成', details: [] },
      { key: 'open_factor', label: '重排序', state: 'not_run', summary: '未触发重排序', details: [] },
      { key: 'relevance', label: '相关性', state: 'normal', summary: '3 件不相关', details: [] },
      { key: 'inventory', label: '断码', state: 'pending', summary: '巡检 2026-09-08 03:04', details: [] },
    ] as never;

    render(<ExecutionOverview nodes={nodes} onOpen={onOpen} />);

    expect(screen.getByRole('region', { name: '执行概览' })).toBeInTheDocument();
    expect(screen.getByTestId('execution-overview-panel')).toBeInTheDocument();
    expect(screen.getAllByText('查看详情')).toHaveLength(4);
    fireEvent.click(screen.getByRole('button', { name: '查看相关性详情' }));
    expect(onOpen).toHaveBeenCalledWith(nodes[2]);
  });

  it('marks the overview title as a sticky section heading', () => {
    render(<ExecutionOverview nodes={[]} onOpen={vi.fn()} />);

    expect(screen.getByText('执行概览')).toHaveClass('ops-query-detail-sticky-heading');
  });

  it('keeps summary copy in the normal color even when a node is abnormal', () => {
    const nodes = [
      { key: 'open_factor', label: '重排序', state: 'failed', summary: '倾向轻薄透气', details: [] },
    ] as never;

    render(<ExecutionOverview nodes={nodes} onOpen={vi.fn()} />);

    expect(screen.getByText('倾向轻薄透气')).not.toHaveClass('ops-execution-overview-card__summary--error');
    expect(screen.getByText('异常')).toHaveAttribute('class', expect.stringContaining('ant-tag'));
  });

  it('uses the same green/blue execution semantics as the compact status strip', () => {
    const nodes = [
      { key: 'recall', label: '召回数量', state: 'normal', summary: '数量充足', details: [] },
      { key: 'inventory', label: '断码', state: 'attention', summary: '检测到断码商品', details: [] },
    ] as never;

    render(<ExecutionOverview nodes={nodes} onOpen={vi.fn()} />);

    expect(screen.getByText('正常')).toHaveClass('ant-tag-green');
    expect(screen.getByText('需关注')).toHaveClass('ant-tag-blue');
  });

  it('keeps the original recall summary sentence in the execution overview', () => {
    const summary = '查询到 24 件商品；结论：本次召回 24 件，数量充足，无需调整。';
    render(<ExecutionOverview
      nodes={[{
        key: 'recall',
        label: '召回数量',
        state: 'normal',
        summary,
        cardSummary: {
          primary: '24 件',
          secondary: '暂无上次数据',
          conclusion: '本次召回 24 件，数量充足，无需调整。',
        },
        details: [],
      }] as never}
      onOpen={vi.fn()}
    />);

    expect(screen.getByText(summary)).toBeInTheDocument();
    expect(screen.queryByText('暂无上次数据')).not.toBeInTheDocument();
    expect(document.querySelector('.ops-execution-overview-card__metric-summary')).not.toBeInTheDocument();
  });
});
