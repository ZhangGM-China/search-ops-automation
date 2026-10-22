import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { dashboardApi } from '@/shared/api';
import { KGTagDetailModal } from './KGTagDetailModal';

vi.mock('@/shared/api', () => ({ dashboardApi: { getKnowledgeGraphTagDetails: vi.fn() } }));

beforeEach(() => vi.resetAllMocks());

it('keeps loaded tag details across equivalent parent renders and reloads after reopening', async () => {
  vi.mocked(dashboardApi.getKnowledgeGraphTagDetails).mockResolvedValue({ data: { name: '羊毛', viewTag: '羊毛面料' } } as never);
  const onClose = vi.fn();
  const { rerender } = render(<KGTagDetailModal brand="atlas" tags={[{ name: '羊毛' }]} open onClose={onClose} />);
  await screen.findByText('羊毛面料');
  rerender(<KGTagDetailModal brand="atlas" tags={[{ name: '羊毛' }]} open onClose={onClose} />);
  await waitFor(() => expect(dashboardApi.getKnowledgeGraphTagDetails).toHaveBeenCalledTimes(1));
  rerender(<KGTagDetailModal brand="atlas" tags={[{ name: '羊毛' }]} open={false} onClose={onClose} />);
  rerender(<KGTagDetailModal brand="atlas" tags={[{ name: '羊毛' }]} open onClose={onClose} />);
  await waitFor(() => expect(dashboardApi.getKnowledgeGraphTagDetails).toHaveBeenCalledTimes(2));
});
