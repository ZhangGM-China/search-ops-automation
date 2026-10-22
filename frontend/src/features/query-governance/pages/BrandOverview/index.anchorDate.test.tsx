import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { dashboardApi } from '@/shared/api';
import BrandOverview from './index';

vi.mock('@/shared/api', () => ({
  dashboardApi: { getTopQueryOverview: vi.fn(), getTopQueryBrandSummary: vi.fn(), getTopQueryBrandTrend: vi.fn() },
}));

vi.mock('../../components/BrandMetricDetailDrawer', () => ({
  default: (props: { open: boolean; anchorDate: string }) => props.open
    ? <div data-testid="brand-metric-drawer-anchor">{props.anchorDate}</div>
    : null,
}));

describe('BrandOverview monitoring-date anchor', () => {
  beforeEach(() => vi.clearAllMocks());

  it('anchors the operations drawer to the selected monitoring date', async () => {
    vi.mocked(dashboardApi.getTopQueryOverview).mockResolvedValue({ data: { brands: [{
      brand: 'atlas', batchId: 'batch-1', dataDate: '2026-09-08', status: 'executed', queryCount: 1,
      nodeStatusCounts: { pending: 0, running: 0, executed: 1, skipped: 0, error: 0 },
      durationSeconds: 1, startedAt: null, completedAt: null,
    }] } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandSummary).mockResolvedValue({ data: {
      brand: 'atlas', dataDate: '2026-09-08', stats: null,
    } } as never);
    vi.mocked(dashboardApi.getTopQueryBrandTrend).mockResolvedValue({ data: { trend: [] } } as never);

    render(<MemoryRouter initialEntries={['/ops-dashboard?date=2026-09-09']}><BrandOverview /></MemoryRouter>);
    await waitFor(() => expect(screen.getByRole('button', { name: '运营数据' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '运营数据' }));

    await waitFor(() => expect(dashboardApi.getTopQueryBrandSummary).toHaveBeenCalledWith('atlas', { date: '2026-09-09' }, { signal: expect.any(AbortSignal) }));
    await waitFor(() => expect(screen.getByTestId('brand-metric-drawer-anchor')).toHaveTextContent('2026-09-09'));
  });
});
