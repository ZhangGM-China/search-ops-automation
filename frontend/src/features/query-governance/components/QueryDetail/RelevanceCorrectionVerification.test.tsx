import React from 'react';
import { fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { dashboardApi } from '@/shared/api';
import type { ManualRelevanceCheckResponse } from '@/shared/types';
import RelevanceCorrectionVerification, { RelevanceCorrectionVerification as NamedVerification } from './RelevanceCorrectionVerification';
import { useLatestManualRelevanceCheck } from './useLatestManualRelevanceCheck';

vi.mock('@/shared/api', async () => {
  const actual = await vi.importActual<typeof import('@/shared/api')>('@/shared/api');
  return { ...actual, dashboardApi: { ...actual.dashboardApi } };
});

const products = [
  { productId: 'p1', productName: '正确商品', imageUrl: 'https://img/p1.jpg' },
  { productId: 'p2', productName: '错误商品', imageUrl: 'https://img/p2.jpg' },
];

const completedCheck = {
  checkId: 'relchk-2', brand: 'atlas', status: 'completed', updatedAt: '2026-09-30T08:10:00Z',
  queries: [{ query: '连衣裙', status: 'completed', products: [
    { sku: 'p1', status: 'completed', isRelevant: true, reason: null },
    { sku: 'p2', status: 'completed', isRelevant: false, reason: '不符合 Query' },
  ] }],
} as unknown as ManualRelevanceCheckResponse;

const Harness: React.FC<{ initial: ManualRelevanceCheckResponse | null }> = ({ initial }) => {
  const [check, setCheck] = React.useState<ManualRelevanceCheckResponse | null>(initial);
  return (
    <NamedVerification
      brand="atlas"
      query="连衣裙"
      products={products}
      displayLimit={16}
      onOpenProduct={vi.fn()}
      check={check}
      onCheckChange={setCheck}
    />
  );
};

describe('RelevanceCorrectionVerification', () => {
  it('opens the modal and starts a single-query verification', async () => {
    vi.spyOn(dashboardApi, 'submitManualRelevanceCheck').mockResolvedValue({ data: { checkId: 'relchk-1', status: 'queued' } } as never);

    render(<Harness initial={null} />);

    fireEvent.click(screen.getByRole('button', { name: '纠正验证' }));
    expect(screen.getByRole('dialog', { name: '纠正验证' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '开始验证' }));

    await waitFor(() => expect(dashboardApi.submitManualRelevanceCheck).toHaveBeenCalledWith(
      'atlas',
      { query: '连衣裙', products: ['p1', 'p2'] },
      expect.any(String),
    ));
    await waitFor(() => expect(screen.getByRole('button', { name: /执行中/ })).toBeInTheDocument());
  });

  it('orders incorrect products before correct products and shows the latest time below the button', () => {
    render(<Harness initial={completedCheck} />);
    fireEvent.click(screen.getByRole('button', { name: '纠正验证' }));

    expect(screen.getByText('最近验证：2026-09-30 16:10:00')).toBeInTheDocument();
    const cards = screen.getAllByRole('button', { name: /^商品 / });
    expect(cards[0]).toHaveTextContent('错误商品');
    expect(cards[1]).toHaveTextContent('正确商品');
  });
});

describe('useLatestManualRelevanceCheck', () => {
  it('reads the latest check once per query and treats an empty result as no check', async () => {
    const latest = vi.spyOn(dashboardApi, 'getLatestManualRelevanceCheck').mockResolvedValue({ data: null } as never);

    const { result } = renderHook(() => useLatestManualRelevanceCheck('atlas', '连衣裙'));

    await waitFor(() => expect(latest).toHaveBeenCalledTimes(1));
    expect(result.current.check).toBeNull();
  });

  it('exposes the stored check for the display points', async () => {
    vi.spyOn(dashboardApi, 'getLatestManualRelevanceCheck').mockResolvedValue({ data: completedCheck } as never);

    const { result } = renderHook(() => useLatestManualRelevanceCheck('atlas', '连衣裙'));

    await waitFor(() => expect(result.current.check?.checkId).toBe('relchk-2'));
  });
});

describe('module export', () => {
  it('keeps the default export aligned with the named component', () => {
    expect(RelevanceCorrectionVerification).toBe(NamedVerification);
  });
});
