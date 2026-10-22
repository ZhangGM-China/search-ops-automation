import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { dashboardApi } from '@/shared/api';
import { useConversationController } from './useConversationController';

vi.mock('@/shared/api', () => ({ dashboardApi: { getAssistantContext: vi.fn(), getAssistantSessions: vi.fn(), updateAssistantSession: vi.fn(), deleteAssistantSession: vi.fn(), prepareAssistantAction: vi.fn(), confirmAssistantAction: vi.fn(), cancelAssistantAction: vi.fn(), streamAssistantMessage: vi.fn(), sendAssistantMessage: vi.fn() } }));

describe('useConversationController initialization', () => {
  beforeEach(() => vi.clearAllMocks());
  it('hydrates runtime and account-scoped bindings without exposing brand state', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: { runtime: { provider: 'P', modelName: 'M' }, availableBindings: [{ brand: 'atlas', agentBindingId: 1, modelName: 'M', displayName: '助手' }], availableBrands: ['atlas'] } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [] } } as never);
    const { result } = renderHook(() => useConversationController());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.runtimeDisplayName).toBe('M');
    expect(result.current).not.toHaveProperty('availableBrands');
  });

  it('does not require client-side brand selection when multiple brands are available', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: {
      runtime: null,
      availableBindings: [
        { agentBindingId: 1, brand: 'atlas', modelName: 'deepseek-v4.1-flash' },
        { agentBindingId: 2, brand: 'harbor', modelName: 'deepseek-v4.1-flash' },
      ],
      availableBrands: [' atlas ', 'harbor', ''],
    } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [] } } as never);
    const { result } = renderHook(() => useConversationController());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).not.toHaveProperty('availableBrands');
    expect(result.current.runtimeDisplayName).toBe('PI Agent');
    expect(result.current.selectedBindingId).toBeNull();
  });

  it('restores the first session brand and filters conversations by title', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: { runtime: { provider: 'P' }, availableBindings: [], availableBrands: ['atlas', 'harbor'] } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [
      { id: 1, title: 'Atlas 对话', preview: '连衣裙', updatedAt: '', messages: [], proposedActions: [], sessionId: 's', brand: 'atlas', pinned: false },
      { id: 2, title: 'HARBOR 对话', preview: '衬衫', updatedAt: '', messages: [], proposedActions: [], sessionId: 's2', brand: 'harbor', pinned: false },
    ] } } as never);
    const { result } = renderHook(() => useConversationController());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.activeConversation?.brand).toBe('atlas');
    expect(result.current).not.toHaveProperty('currentConversationBrand');
    result.current.setSearchKeyword('harbor');
    await waitFor(() => expect(result.current.visibleConversations).toHaveLength(1));
    expect(result.current.visibleConversations[0].title).toBe('HARBOR 对话');
  });

  it('finishes loading when context or session bootstrap fails', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockRejectedValueOnce(new Error('上下文加载失败'));
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValueOnce({ data: { items: [] } } as never);
    const first = renderHook(() => useConversationController());
    await waitFor(() => expect(first.result.current.loading).toBe(false));
    first.unmount();
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValueOnce({ data: { availableBindings: [], availableBrands: [] } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockRejectedValueOnce(new Error('会话加载失败'));
    const second = renderHook(() => useConversationController());
    await waitFor(() => expect(second.result.current.loading).toBe(false));
  });

  it('updates, deletes and cancels actions while preserving local state on API failures', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: { availableBindings: [], availableBrands: [] } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [{ id: 1, title: '原标题', preview: 'p', updatedAt: '', messages: [], proposedActions: [{ id: 5, target: 'x', actionType: 'blacklist', status: 'pending' }], sessionId: 's', pinned: false }] } } as never);
    vi.mocked(dashboardApi.updateAssistantSession).mockRejectedValue(new Error('更新失败'));
    vi.mocked(dashboardApi.deleteAssistantSession).mockRejectedValue(new Error('删除失败'));
    vi.mocked(dashboardApi.cancelAssistantAction).mockRejectedValue(new Error('取消失败'));
    const { result } = renderHook(() => useConversationController());
    await waitFor(() => expect(result.current.loading).toBe(false));
    const conversation = result.current.activeConversation!;
    result.current.handleRenameStart(conversation); result.current.setRenameDraft('新标题');
    await result.current.handleRenameCommit();
    result.current.handleTogglePinned(conversation.id);
    await result.current.handleDeleteConversation(conversation.id);
    await result.current.handleCancelAction(conversation.proposedActions[0]);
    await waitFor(() => expect(result.current.visibleConversations).toHaveLength(0));
  });

  it('marks the conversation failed without falling back to the non-stream endpoint', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: { availableBindings: [], availableBrands: ['atlas'] } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [] } } as never);
    vi.mocked(dashboardApi.streamAssistantMessage).mockRejectedValue(new Error('stream unavailable'));
    const { result } = renderHook(() => useConversationController()); await waitFor(() => expect(result.current.loading).toBe(false));
    await result.current.sendMessage('你好');
    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(dashboardApi.sendAssistantMessage).not.toHaveBeenCalled();
    await waitFor(() => expect(result.current.activeConversation?.executionState).toBe('failed'));
  });

  it('sends without a brand and handles action confirmation and cancellation', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: { availableBindings: [], availableBrands: ['atlas', 'harbor'] } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [{ id: 1, title: 't', preview: 'p', updatedAt: '', messages: [], proposedActions: [{ id: 7, target: 'p', actionType: 'blacklist', status: 'pending' }], sessionId: 's', pinned: false }] } } as never);
    const { result } = renderHook(() => useConversationController()); await waitFor(() => expect(result.current.loading).toBe(false));
    await result.current.sendMessage('需要品牌');
    expect(dashboardApi.streamAssistantMessage).toHaveBeenCalledTimes(1);
    vi.mocked(dashboardApi.prepareAssistantAction).mockResolvedValue({ data: { id: 8 } } as never);
    vi.mocked(dashboardApi.confirmAssistantAction).mockResolvedValue({ data: { status: 'executed' } } as never);
    await result.current.handleConfirmAction(result.current.activeConversation!.proposedActions[0]);
    vi.mocked(dashboardApi.cancelAssistantAction).mockResolvedValue({ data: { status: 'cancelled' } } as never);
    await result.current.handleCancelAction({ id: 7, target: 'p', actionType: 'blacklist' } as never);
    expect(dashboardApi.confirmAssistantAction).toHaveBeenCalledWith(8);
  });

  it('allows sending without a client-side brand selection', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: {
      runtime: { provider: 'PI Agent', modelName: 'M' },
      availableBindings: [
        { agentBindingId: 1, brand: 'atlas', modelName: 'M' },
        { agentBindingId: 2, brand: 'harbor', modelName: 'M' },
      ],
      availableBrands: ['atlas', 'harbor'],
    } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [] } } as never);
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementation(async (_payload, handlers) => {
      handlers.onDelta('按账号权限汇总');
      handlers.onDone({ sessionId: 'account-scope-session', runtime: { provider: 'PI Agent', modelLabel: 'PI Agent' } });
    });
    const { result } = renderHook(() => useConversationController());
    await waitFor(() => expect(result.current.loading).toBe(false));

    await result.current.sendMessage('汇总昨天运营情况');

    expect(dashboardApi.streamAssistantMessage).toHaveBeenCalledWith(
      expect.objectContaining({ message: '汇总昨天运营情况', agentBindingId: undefined }),
      expect.anything(),
      expect.anything(),
    );
  });

  it('handles attachment selection and scroll state without a mounted container', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: { availableBindings: [], availableBrands: [] } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [] } } as never);
    const { result } = renderHook(() => useConversationController()); await waitFor(() => expect(result.current.loading).toBe(false));
    await result.current.handleAttachFiles(null);
    result.current.scrollToBottom('auto'); result.current.updateScrollState(); result.current.stopGeneration();
    expect(result.current.attachments).toEqual([]);
    result.current.handleCreateConversation();
    await waitFor(() => expect(result.current.activeConversationId).toMatch(/^draft-/));
  });

  it('resets a historical brand binding when a new account-scoped conversation is created', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: {
      runtime: { provider: 'PI Agent', modelName: 'M' },
      availableBindings: [
        { agentBindingId: 1, brand: 'atlas', modelName: 'M' },
        { agentBindingId: 2, brand: 'harbor', modelName: 'M' },
      ],
      availableBrands: ['atlas', 'harbor'],
    } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [{
      id: 1,
      title: '历史会话',
      preview: 'p',
      updatedAt: '',
      messages: [{ id: 'm', role: 'assistant', title: '助手', content: '历史', createdAt: '' }],
      proposedActions: [],
      sessionId: 's',
      pinned: false,
      brand: 'atlas',
      agentBindingId: 1,
    }] } } as never);
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementation(async (_payload, handlers) => {
      handlers.onDone({ sessionId: 'account-session' });
    });
    const { result } = renderHook(() => useConversationController());
    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() => {
      result.current.setSelectedBindingId(1);
      result.current.handleCreateConversation();
    });
    await waitFor(() => {
      expect(result.current.selectedBindingId).toBeNull();
      expect(result.current.activeConversationId).toMatch(/^draft-/);
    });
    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(dashboardApi.getAssistantSessions).toHaveBeenCalledTimes(1);
    await act(async () => result.current.sendMessage('账号范围查询'));

    expect(dashboardApi.streamAssistantMessage).toHaveBeenCalledWith(
      expect.objectContaining({ message: '账号范围查询', agentBindingId: undefined }),
      expect.anything(),
      expect.anything(),
    );
  });

  it('resets the binding when the reusable empty draft is selected as a new conversation', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue({ data: {
      runtime: { provider: 'PI Agent', modelName: 'M' },
      availableBindings: [
        { agentBindingId: 1, brand: 'atlas', modelName: 'M' },
        { agentBindingId: 2, brand: 'harbor', modelName: 'M' },
      ],
      availableBrands: ['atlas', 'harbor'],
    } } as never);
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue({ data: { items: [] } } as never);
    const { result } = renderHook(() => useConversationController());
    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() => result.current.handleCreateConversation());
    await waitFor(() => expect(result.current.activeConversationId).toMatch(/^draft-/));
    act(() => result.current.setSelectedBindingId(1));
    await waitFor(() => expect(result.current.selectedBindingId).toBe(1));
    act(() => result.current.handleCreateConversation());

    expect(result.current.selectedBindingId).toBeNull();
  });
});
