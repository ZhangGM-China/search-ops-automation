import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import QueryDemotionExecutionPanel, { QueryDemotionVerificationPanel } from './QueryDemotionExecutionPanel';

describe('QueryDemotionVerificationPanel', () => {
  it('renders evidence rows and rollback action', () => {
    const rollback = vi.fn();
    render(<QueryDemotionVerificationPanel action={{ id: 1, rollbackAvailable: true, configVerification: { status: 'passed', version: 'v1', missingIds: [], unexpectedIds: [] }, searchEffect: { status: 'inconclusive', resultCount: 2, observedRemainingIds: ['a'], unverifiedIds: ['b'] }, rollbackVerification: { status: 'failed', reason: 'bad' } } as never} onRollback={rollback} rollbackLoading={false} />);
    expect(screen.getByText('已通过')).toBeInTheDocument();
    expect(screen.getByText('无结论')).toBeInTheDocument();
    expect(screen.getByText('回滚失败')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '回滚降桶' }));
    expect(rollback).toHaveBeenCalled();
  });
  it('renders no rows when evidence is absent', () => {
    const { container } = render(<QueryDemotionVerificationPanel action={{ id: 2 } as never} />);
    expect(container.querySelector('.ops-demotion-verification__rows')).toBeNull();
  });
  it('renders loading and blocked preview states', () => {
    const p = { open: true, preview: null, loading: true, executing: false, error: null, conflictMessage: null, onConfirm: vi.fn(), onClose: vi.fn() };
    const { rerender } = render(<QueryDemotionExecutionPanel {...p} />);
    expect(screen.getByText(/正在回读权威/)).toBeInTheDocument();
    rerender(<QueryDemotionExecutionPanel {...p} loading={false} error="失败" conflictMessage="冲突" preview={{ brand: 'atlas', query: '鞋', configVersion: 'v1', executable: false, blockedReason: 'no_query_demote_actions', targetIds: [], beforeIds: [], addedIds: [], retainedIds: [], removedIds: [], desiredIds: [], targetProducts: [], rollbackAvailable: false, actionIds: [] } as never} />);
    expect(screen.getByText(/没有可执行/)).toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
    expect(screen.getByText('冲突')).toBeInTheDocument();
  });
  it('confirms executable preview and closes modal', () => {
    const onConfirm = vi.fn(); const onClose = vi.fn();
    render(<QueryDemotionExecutionPanel open preview={{ brand: 'atlas', query: '鞋', configVersion: 'v2', executable: true, blockedReason: null, targetIds: ['p1'], beforeIds: [], addedIds: ['p1'], retainedIds: [], removedIds: [], desiredIds: ['p1'], targetProducts: [], rollbackAvailable: true, actionIds: [3] } as never} loading={false} executing={false} error={null} conflictMessage={null} onConfirm={onConfirm} onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: /确认执行降桶/ }));
    expect(onConfirm).toHaveBeenCalledWith('v2', [3]);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
  });
});
