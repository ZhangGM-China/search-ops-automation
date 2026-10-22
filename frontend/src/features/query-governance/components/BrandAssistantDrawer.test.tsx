import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import BrandAssistantDrawer from './BrandAssistantDrawer';
import { dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getAssistantContext: vi.fn(),
    streamAssistantMessage: vi.fn(),
    sendAssistantMessage: vi.fn(),
    prepareAssistantAction: vi.fn(),
    confirmAssistantAction: vi.fn(),
    cancelAssistantAction: vi.fn(),
  },
}));

vi.mock('@/shared/hooks/useMobile', () => ({
  default: () => false,
}));

describe('BrandAssistantDrawer', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue(axiosResponse({
      summary: {
        taskCount: 3,
        pendingCount: 2,
        actionCount: 4,
        monitoringCount: 1,
      },
      suggestedPrompts: ['先看下这个 Query'],
      messages: [
        {
          id: 'recent-1',
          role: 'assistant',
          title: '最近上下文',
          content: '最近治理建议',
          createdAt: '2026-07-09 10:00',
        },
      ],
      capabilities: [
        {
          key: 'query_governance',
          title: 'Query 治理分析',
          description: '分析治理动作与风险',
          status: 'ready',
        },
      ],
      runtime: {
        provider: 'PI Agent',
        modelLabel: 'ATLAS Agent',
        modelName: 'qwen3.7-max',
      },
      availableBindings: [
        {
          agentBindingId: 21,
          brand: 'atlas',
          displayName: 'ATLAS Agent',
          modelDisplayName: 'Qwen 3.7 Max',
          modelName: 'qwen3.7-max',
          bindingType: 'agent',
        },
      ],
    }));
    vi.mocked(dashboardApi.sendAssistantMessage).mockResolvedValue(axiosResponse({
      sessionId: 'brand_detail-atlas-101-2026-07-09-session',
      message: '建议先确认降桶动作',
      proposedActions: [
        {
          id: 301,
          actionType: 'demote',
          target: 'SKU-1',
          reason: '断码率过高',
          riskLevel: 'medium',
        },
      ],
      contextSummary: {
        taskCount: 3,
        pendingCount: 2,
        actionCount: 5,
        monitoringCount: 1,
      },
      status: 'ok',
      runtime: {
        provider: 'PI Agent',
        modelLabel: 'ATLAS Agent',
        modelName: 'qwen3.7-max',
      },
    }));
    vi.mocked(dashboardApi.prepareAssistantAction).mockResolvedValue(axiosResponse({
      id: 301,
      actionType: 'demote',
      brand: 'atlas',
      query: '连衣裙',
      target: 'SKU-1',
      reason: '断码率过高',
      riskLevel: 'medium',
      status: 'prepared',
      payload: { bucket: 'B2' },
    }));
    vi.mocked(dashboardApi.confirmAssistantAction).mockResolvedValue(axiosResponse({
      id: 301,
      actionType: 'demote',
      brand: 'atlas',
      query: '连衣裙',
      target: 'SKU-1',
      reason: '断码率过高',
      riskLevel: 'medium',
      status: 'executed',
      payload: { bucket: 'B2' },
      resultSummary: '已执行降桶',
    }));
    vi.mocked(dashboardApi.cancelAssistantAction).mockResolvedValue(axiosResponse({
      id: 301,
      actionType: 'demote',
      brand: 'atlas',
      query: '连衣裙',
      target: 'SKU-1',
      reason: '断码率过高',
      riskLevel: 'medium',
      status: 'cancelled',
      payload: { bucket: 'B2' },
      resultSummary: '已取消动作',
    }));
  });

  it('loads context, falls back to request-response send, and manages proposed actions', async () => {
    vi.mocked(dashboardApi.streamAssistantMessage).mockRejectedValueOnce(new Error('stream unavailable'));
    const onClose = vi.fn();

    render(
      <BrandAssistantDrawer
        open
        onClose={onClose}
        brand="atlas"
        brandLabel="ATLAS"
        date="2026-07-09"
        task={{
          id: 101,
          queryText: '连衣裙',
        } as never}
      />,
    );

    expect(await screen.findByText('ATLAS')).toBeInTheDocument();
    expect(screen.getByText('连衣裙')).toBeInTheDocument();
    const suggestedPromptButton = await screen.findByRole('button', { name: '先看下这个 Query' });
    expect(suggestedPromptButton).toBeInTheDocument();

    fireEvent.click(suggestedPromptButton);
    await waitFor(() => {
      expect(dashboardApi.sendAssistantMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          message: '先看下这个 Query',
          brand: 'atlas',
          date: '2026-07-09',
          sessionId: expect.stringContaining('brand_detail-atlas-101-2026-07-09'),
        }),
        { signal: expect.any(AbortSignal) },
      );
    });

    expect(await screen.findByText('建议先确认降桶动作')).toBeInTheDocument();
    expect(screen.getByText('待确认动作')).toBeInTheDocument();
    expect(screen.getByText('SKU-1')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '确认执行' }));
    await waitFor(() => {
      expect(dashboardApi.prepareAssistantAction).toHaveBeenCalled();
      expect(dashboardApi.confirmAssistantAction).toHaveBeenCalledWith(301);
    });

    fireEvent.click(screen.getByRole('button', { name: /^取\s*消$/ }));
    await waitFor(() => {
      expect(dashboardApi.cancelAssistantAction).toHaveBeenCalledWith(301);
    });

    fireEvent.click(screen.getByRole('button', { name: /^收\s*起$/ }));
    expect(onClose).toHaveBeenCalled();
  });

  it('aborts a stale stream after the page context changes without falling back into the new session', async () => {
    let rejectStream: (reason?: unknown) => void = () => undefined;
    let streamSignal: AbortSignal | undefined;
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementation((_payload, _handlers, options) => new Promise<void>((_resolve, reject) => {
      rejectStream = reject;
      streamSignal = options?.signal;
    }));

    const view = render(
      <BrandAssistantDrawer
        open
        onClose={vi.fn()}
        brand="atlas"
        brandLabel="ATLAS"
        date="2026-07-09"
        task={{ id: 101, queryText: '连衣裙' } as never}
        pageKey="brand_detail"
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: '先看下这个 Query' }));
    await waitFor(() => expect(dashboardApi.streamAssistantMessage).toHaveBeenCalledTimes(1));

    view.rerender(
      <BrandAssistantDrawer
        open
        onClose={vi.fn()}
        brand="atlas"
        brandLabel="ATLAS"
        date="2026-07-09"
        task={{ id: 101, queryText: '连衣裙' } as never}
        pageKey="brand_overview"
      />,
    );
    await waitFor(() => expect(streamSignal?.aborted).toBe(true));
    rejectStream(new DOMException('stale stream', 'AbortError'));

    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(dashboardApi.sendAssistantMessage).not.toHaveBeenCalled();
  });
});
