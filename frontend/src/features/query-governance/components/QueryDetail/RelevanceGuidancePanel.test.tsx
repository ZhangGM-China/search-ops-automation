import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import RelevanceGuidancePanel from './RelevanceGuidancePanel';
import { dashboardApi } from '@/shared/api';

vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getRelevanceGuidance: vi.fn(),
    saveRelevanceGuidance: vi.fn(),
    previewRelevanceGuidanceOrganization: vi.fn(),
    confirmRelevanceGuidanceOrganization: vi.fn(),
    deactivateRelevanceGuidance: vi.fn(),
  },
}));

const response = <T,>(data: T) => ({ data }) as never;

describe('RelevanceGuidancePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(dashboardApi.getRelevanceGuidance).mockResolvedValue(response({
      id: 1,
      brand: 'atlas',
      query: '裙子',
      rules: '排除男装',
      status: 'active',
    }));
  });

  it('loads and saves the current rule', async () => {
    vi.mocked(dashboardApi.saveRelevanceGuidance).mockResolvedValue(response({
      id: 1, brand: 'atlas', query: '裙子', rules: '排除童装', status: 'active',
    }));
    render(<RelevanceGuidancePanel brand="atlas" query="裙子" />);
    const editor = await screen.findByPlaceholderText('填写该 Query 的相关性纠正规则');
    fireEvent.change(editor, { target: { value: '排除童装' } });
    fireEvent.click(screen.getByText('保 存'));
    await waitFor(() => expect(dashboardApi.saveRelevanceGuidance).toHaveBeenCalledWith(
      'atlas', { query: '裙子', rules: '排除童装' },
    ));
    await waitFor(() => expect(screen.getByDisplayValue('排除童装')).toBeInTheDocument());
    expect(screen.getByText('填写该Query的纠正规则，纠正AI在相关性检测中产生的问题，规则会在该 Query 的下一次相关性检测中生效。')).toBeInTheDocument();
    expect(screen.queryByText('相关性纠正规则')).not.toBeInTheDocument();
    expect(screen.queryByText('未启用')).not.toBeInTheDocument();
  });

  it('requires confirmation before applying AI organization', async () => {
    vi.mocked(dashboardApi.previewRelevanceGuidanceOrganization).mockResolvedValue(response({
      id: 1, brand: 'atlas', query: '裙子', rules: '整理后的规则', status: 'preview',
    }));
    vi.mocked(dashboardApi.confirmRelevanceGuidanceOrganization).mockResolvedValue(response({
      id: 1, brand: 'atlas', query: '裙子', rules: '整理后的规则', status: 'active',
    }));
    render(<RelevanceGuidancePanel brand="atlas" query="裙子" />);
    await screen.findByDisplayValue('排除男装');
    fireEvent.click(screen.getByRole('button', { name: 'AI 整理' }));
    expect(await screen.findByText('AI 整理预览')).toBeInTheDocument();
    expect(dashboardApi.confirmRelevanceGuidanceOrganization).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '确认覆盖' }));
    await waitFor(() => expect(dashboardApi.confirmRelevanceGuidanceOrganization).toHaveBeenCalledWith(
      'atlas', { query: '裙子', rules: '整理后的规则' },
    ));
    await waitFor(() => {
      expect(screen.getByDisplayValue('整理后的规则')).toBeInTheDocument();
      expect(screen.queryByText('AI 整理预览')).not.toBeInTheDocument();
    });
  });

  it('reloads the current rule when the tab is activated again', async () => {
    const { rerender } = render(<RelevanceGuidancePanel brand="atlas" query="裙子" refreshToken={0} />);
    await waitFor(() => expect(dashboardApi.getRelevanceGuidance).toHaveBeenCalledTimes(1));

    rerender(<RelevanceGuidancePanel brand="atlas" query="裙子" refreshToken={1} />);
    await waitFor(() => expect(dashboardApi.getRelevanceGuidance).toHaveBeenCalledTimes(2));
  });

  it('keeps the change log visible after a save that omits the audit trail', async () => {
    vi.mocked(dashboardApi.getRelevanceGuidance).mockResolvedValue(response({
      id: 1,
      brand: 'atlas',
      query: '裙子',
      rules: '排除男装',
      status: 'active',
      reasonHistory: [
        { id: 7, productId: 'SKU-1', reason: '商品是休闲短裤', operator: 'joy', createdAt: '2026-10-09 11:05:14' },
      ],
    }));
    vi.mocked(dashboardApi.saveRelevanceGuidance).mockResolvedValue(response({
      id: 1, brand: 'atlas', query: '裙子', rules: '排除童装', status: 'active',
    }));
    render(<RelevanceGuidancePanel brand="atlas" query="裙子" />);

    await screen.findByText('规则变更记录');
    expect(screen.getByText('joy · 2026-10-09 11:05:14')).toBeInTheDocument();
    expect(screen.queryByText(/商品 SKU-1/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /停\s*用/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('保 存'));

    await waitFor(() => expect(screen.getByDisplayValue('排除童装')).toBeInTheDocument());
    expect(screen.getByText('规则变更记录')).toBeInTheDocument();
    expect(screen.getByText('joy · 2026-10-09 11:05:14')).toBeInTheDocument();
  });
});
