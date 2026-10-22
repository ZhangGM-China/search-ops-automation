import React from 'react';
import dayjs from 'dayjs';
import { message as antdMessage } from 'antd';
import { dashboardApi } from '@/shared/api';
import type { AssistantStreamDonePayload, AssistantStreamPhase, AssistantStreamToolCall } from '@/shared/api';
import type {
  AssistantAvailableBinding,
  AssistantContextMessage,
  AssistantMessageVersion,
  AssistantProposedAction,
  AssistantRuntimeInfo,
  AssistantToolResult,
} from '@/shared/types';
import { getApiErrorMessage } from '@/shared/lib/error';
import {
  DEFAULT_RUNTIME,
  type ConversationState,
  type DraftAttachment,
  type SendMessageOptions,
  buildAttachmentSummary,
  buildConversationFromSession,
  buildConversationTitle,
  createDraftConversation,
  getMessageVersions,
  pickPreferredBinding,
  sortConversations,
  toConversationPreview,
  toMessageAttachments,
} from '../conversationModel';

export const useConversationController = () => {
  const [conversations, setConversations] = React.useState<ConversationState[]>([]);
  const [activeConversationId, setActiveConversationId] = React.useState<string | null>(null);
  const [draft, setDraft] = React.useState('');
  const [attachments, setAttachments] = React.useState<DraftAttachment[]>([]);
  const [sending, setSending] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [runtime, setRuntime] = React.useState<AssistantRuntimeInfo>(DEFAULT_RUNTIME);
  const [availableBindings, setAvailableBindings] = React.useState<AssistantAvailableBinding[]>([]);
  const [selectedBindingId, setSelectedBindingId] = React.useState<number | null>(null);
  const [searchKeyword, setSearchKeyword] = React.useState('');
  const [renamingId, setRenamingId] = React.useState<string | null>(null);
  const [renameDraft, setRenameDraft] = React.useState('');
  const [showScrollButton, setShowScrollButton] = React.useState(false);
  const [messageVersionIndexMap, setMessageVersionIndexMap] = React.useState<Record<string, number>>({});
  const abortRef = React.useRef<AbortController | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);
  const detailScrollRef = React.useRef<HTMLDivElement | null>(null);
  const transientIdRef = React.useRef(0);
  const isNearBottomRef = React.useRef(true);
  const userCreatedConversationRef = React.useRef(false);
  const runtimeDisplayName = runtime.modelName || runtime.modelLabel || 'PI Agent';

  const createTransientId = React.useCallback((prefix: string) => {
    transientIdRef.current += 1;
    return `${prefix}-${transientIdRef.current}`;
  }, []);

  const applyAssistantContext = React.useCallback((
    contextState: {
      runtime: AssistantRuntimeInfo;
      bindings: AssistantAvailableBinding[];
      selectedBinding: AssistantAvailableBinding | null;
    },
  ) => {
    setRuntime(contextState.runtime);
    setAvailableBindings(contextState.bindings);
    setSelectedBindingId(contextState.selectedBinding?.agentBindingId ?? null);
  }, []);

  const fetchAssistantContext = React.useCallback(async (preferredBindingId: number | null = null) => {
    const response = await dashboardApi.getAssistantContext({});
    const nextRuntime = response.data.runtime || DEFAULT_RUNTIME;
    const nextBindings = response.data.availableBindings || [];
    const nextBinding = pickPreferredBinding(nextBindings, preferredBindingId);
    return {
      runtime: nextRuntime,
      bindings: nextBindings,
      selectedBinding: nextBinding,
    };
  }, []);

  React.useEffect(() => {
    let active = true;
    Promise.all([
      fetchAssistantContext(null),
      dashboardApi.getAssistantSessions(),
    ])
      .then(([contextState, sessionResponse]) => {
        if (!active) return;
        applyAssistantContext(contextState);
        const initialConversations = sessionResponse.data.items.map(buildConversationFromSession);
        setConversations((current) => {
          const draftConversations = current.filter((item) => item.syncStatus === 'draft');
          return draftConversations.length ? [...draftConversations, ...initialConversations] : initialConversations;
        });
        if (!userCreatedConversationRef.current) setActiveConversationId(initialConversations[0]?.id || null);
        if (!userCreatedConversationRef.current && !initialConversations.length && contextState.selectedBinding?.agentBindingId) {
          setSelectedBindingId(contextState.selectedBinding.agentBindingId);
        } else if (userCreatedConversationRef.current) {
          setSelectedBindingId(null);
        }
      })
      .catch((error: unknown) => {
        void antdMessage.error(`智能运营助理上下文加载失败: ${getApiErrorMessage(error, '未知错误')}`);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      abortRef.current?.abort();
    };
  }, [applyAssistantContext, fetchAssistantContext]);

  const visibleConversations = React.useMemo(() => {
    const keyword = searchKeyword.trim().toLowerCase();
    const sorted = sortConversations(conversations);
    if (!keyword) return sorted;
    return sorted.filter((item) => {
      const haystack = `${item.title} ${item.preview}`.toLowerCase();
      return haystack.includes(keyword);
    });
  }, [conversations, searchKeyword]);

  const activeConversation = React.useMemo(
    () => conversations.find((item) => item.id === activeConversationId) || null,
    [activeConversationId, conversations],
  );

  const scrollToBottom = React.useCallback((behavior: ScrollBehavior = 'smooth') => {
    const container = detailScrollRef.current;
    if (!container) return;
    if (typeof container.scrollTo === 'function') {
      container.scrollTo({ top: container.scrollHeight, behavior });
      return;
    }
    container.scrollTop = container.scrollHeight;
  }, []);

  const updateScrollState = React.useCallback(() => {
    const container = detailScrollRef.current;
    if (!container) return;
    const hasOverflow = container.scrollHeight > container.clientHeight + 12;
    const isNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 96;
    isNearBottomRef.current = isNearBottom;
    setShowScrollButton(hasOverflow && !isNearBottom);
  }, []);

  React.useEffect(() => {
    if (!activeConversationId) return;
    isNearBottomRef.current = true;
    const frameId = requestAnimationFrame(() => {
      setShowScrollButton(false);
      scrollToBottom('auto');
    });
    return () => cancelAnimationFrame(frameId);
  }, [activeConversationId, scrollToBottom]);

  React.useEffect(() => {
    if (isNearBottomRef.current) {
      const frameId = requestAnimationFrame(() => scrollToBottom('smooth'));
      return () => cancelAnimationFrame(frameId);
    }
    updateScrollState();
  }, [activeConversation?.messages, activeConversation?.proposedActions, scrollToBottom, updateScrollState]);

  const updateConversations = React.useCallback((updater: (items: ConversationState[]) => ConversationState[]) => {
    setConversations((current) => sortConversations(updater(current)));
  }, []);

  const updateAssistantRetryVersion = React.useCallback((
    conversation: ConversationState,
    targetMessageId: string,
    nextVersion: AssistantMessageVersion,
  ): ConversationState => {
    let replaced = false;
    const nextMessages = conversation.messages.map((item) => {
      if (item.id !== targetMessageId || item.role !== 'assistant') return item;
      replaced = true;
      const previousVersions = getMessageVersions(
        item,
        conversation.toolCalls[item.id] || [],
        conversation.toolResults[item.id] || [],
      );
      const nextVersions = previousVersions.length && previousVersions[previousVersions.length - 1]?.id === nextVersion.id
        ? [...previousVersions.slice(0, -1), nextVersion]
        : [...previousVersions, nextVersion];
      return {
        ...item,
        title: nextVersion.title,
        content: nextVersion.content,
        createdAt: nextVersion.createdAt,
        toolCalls: nextVersion.toolCalls || [],
        toolResults: nextVersion.toolResults || [],
        versions: nextVersions,
        activeVersionIndex: nextVersions.length - 1,
      };
    });
    if (!replaced) return conversation;
    return {
      ...conversation,
      preview: toConversationPreview(nextVersion.content),
      updatedAt: nextVersion.createdAt,
      messages: nextMessages,
      toolCalls: {
        ...(conversation.toolCalls || {}),
        [targetMessageId]: nextVersion.toolCalls || [],
      },
      toolResults: {
        ...(conversation.toolResults || {}),
        [targetMessageId]: nextVersion.toolResults || [],
      },
    };
  }, []);

  const upsertConversation = React.useCallback((nextConversation: ConversationState) => {
    updateConversations((current) => {
      const exists = current.some((item) => item.id === nextConversation.id);
      if (!exists) return [nextConversation, ...current];
      return current.map((item) => (item.id === nextConversation.id ? nextConversation : item));
    });
  }, [updateConversations]);

  const handleCreateConversation = React.useCallback(() => {
    userCreatedConversationRef.current = true;
    if (activeConversation?.syncStatus === 'draft' && !activeConversation.messages.length && !draft.trim()) {
      setActiveConversationId(activeConversation.id);
      setSelectedBindingId(null);
      return;
    }
    const nextConversation = createDraftConversation();
    setConversations((current) => [nextConversation, ...current]);
    setActiveConversationId(nextConversation.id);
    setSelectedBindingId(null);
    setDraft('');
    setAttachments([]);
    setSearchKeyword('');
  }, [activeConversation, draft]);

  const handleBranchMessage = React.useCallback(async (messageId: string) => {
    if (!activeConversation?.sessionMetaId) return;
    try {
      const response = await dashboardApi.branchAssistantSession(activeConversation.sessionMetaId, messageId);
      const branch = buildConversationFromSession(response.data);
      upsertConversation(branch);
      setActiveConversationId(branch.id);
    } catch (error: unknown) {
      void antdMessage.error(`创建会话分支失败: ${getApiErrorMessage(error, '未知错误')}`);
    }
  }, [activeConversation, upsertConversation]);

  const handleRenameStart = (conversation: ConversationState) => {
    setRenamingId(conversation.id);
    setRenameDraft(conversation.title);
  };

  const handleRenameCommit = async () => {
    const nextTitle = renameDraft.trim();
    if (!renamingId || !nextTitle) {
      setRenamingId(null);
      return;
    }
    const target = conversations.find((item) => item.id === renamingId) || null;
    updateConversations((current) => current.map((item) => (
      item.id === renamingId ? { ...item, title: nextTitle } : item
    )));
    if (target?.sessionMetaId) {
      try {
        await dashboardApi.updateAssistantSession(target.sessionMetaId, { title: nextTitle });
      } catch (error: unknown) {
        void antdMessage.error(`重命名失败: ${getApiErrorMessage(error, '未知错误')}`);
      }
    }
    setRenamingId(null);
  };

  const handleTogglePinned = async (conversationId: string) => {
    const target = conversations.find((item) => item.id === conversationId) || null;
    if (!target) return;
    const nextPinned = !target.pinned;
    updateConversations((current) => current.map((item) => (
      item.id === conversationId ? { ...item, pinned: nextPinned } : item
    )));
    if (target.sessionMetaId) {
      try {
        await dashboardApi.updateAssistantSession(target.sessionMetaId, { pinned: nextPinned });
      } catch (error: unknown) {
        void antdMessage.error(`置顶更新失败: ${getApiErrorMessage(error, '未知错误')}`);
      }
    }
  };

  const handleDeleteConversation = async (conversationId: string) => {
    const target = conversations.find((item) => item.id === conversationId) || null;
    updateConversations((current) => current.filter((item) => item.id !== conversationId));
    if (activeConversationId === conversationId) setActiveConversationId(null);
    if (target?.sessionMetaId) {
      try {
        await dashboardApi.deleteAssistantSession(target.sessionMetaId);
      } catch (error: unknown) {
        void antdMessage.error(`删除失败: ${getApiErrorMessage(error, '未知错误')}`);
      }
    }
  };

  const handleCopyMessage = async (content: string) => {
    try {
      await navigator.clipboard.writeText(content);
      void antdMessage.success('已复制');
    } catch {
      void antdMessage.warning('当前浏览器不支持自动复制');
    }
  };

  const handleAttachFiles = async (fileList: FileList | null) => {
    const nextFiles = await Promise.all(Array.from(fileList || []).map(async (file) => {
      const nextAttachment: DraftAttachment = {
        id: `${file.name}-${file.lastModified}-${file.size}`,
        name: file.name,
        size: file.size,
        type: file.type.startsWith('image/') ? 'image' : 'file',
        mimeType: file.type,
      };
      if (!file.type.startsWith('image/')) return nextAttachment;
      const previewUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      return {
        ...nextAttachment,
        url: previewUrl,
      };
    }));
    if (nextFiles.length) setAttachments((current) => [...current, ...nextFiles]);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const sendMessage = async (messageText: string, options: SendMessageOptions = {}) => {
    if ((!messageText.trim() && !attachments.length) || sending) return;

    let selectedBinding = pickPreferredBinding(availableBindings, selectedBindingId);
    if (!selectedBinding && availableBindings.length === 0) {
      try {
        const refreshed = await fetchAssistantContext(selectedBindingId);
        applyAssistantContext(refreshed);
        selectedBinding = refreshed.selectedBinding;
      } catch (error: unknown) {
        void antdMessage.error(`智能运营助理上下文刷新失败: ${getApiErrorMessage(error, '未知错误')}`);
      }
    }
    const baseConversation = activeConversation || createDraftConversation();
    if (!activeConversation) {
      upsertConversation(baseConversation);
      setActiveConversationId(baseConversation.id);
    }
    if (options.retryOfMessageId) {
      setMessageVersionIndexMap((current) => {
        const next = { ...current };
        delete next[options.retryOfMessageId as string];
        return next;
      });
    }

    const normalizedMessage = messageText.trim();
    const attachmentSummary = buildAttachmentSummary(attachments);
    const attachmentText = attachments.length
      ? `\n\n附件：${attachmentSummary}`
      : '';
    const operatorMessage: AssistantContextMessage = {
      id: createTransientId('operator'),
      role: 'operator',
      title: '运营人员',
      content: normalizedMessage,
      createdAt: dayjs().format('YYYY-MM-DD HH:mm'),
      attachments: toMessageAttachments(attachments),
    };
    const nextMessages = options.retryOfMessageId
      ? baseConversation.messages
      : [...baseConversation.messages, operatorMessage];
    const optimisticConversation: ConversationState = {
      ...baseConversation,
      title: baseConversation.syncStatus === 'draft' ? buildConversationTitle(normalizedMessage, attachments) : baseConversation.title,
      preview: toConversationPreview(normalizedMessage || attachmentSummary),
      updatedAt: operatorMessage.createdAt,
      activityState: 'active',
      syncStatus: 'synced',
      agentBindingId: selectedBinding?.agentBindingId ?? baseConversation.agentBindingId,
      messages: nextMessages,
      toolCalls: baseConversation.toolCalls || {},
      toolResults: baseConversation.toolResults || {},
      executionState: 'thinking',
      executionLabel: '正在思考',
    };
    upsertConversation(optimisticConversation);
    if (!options.retryOfMessageId) {
      setDraft('');
      setAttachments([]);
    }
    setSending(true);
    abortRef.current = new AbortController();
    const streamController = abortRef.current;

    try {
      const streamedConversation = await streamAssistantResponse(
        optimisticConversation,
        baseConversation.sessionId,
        `${normalizedMessage}${attachmentText}`,
        streamController.signal,
        options,
      );
      upsertConversation({ ...streamedConversation, executionState: 'done', executionLabel: '执行完成' });
    } catch (error: unknown) {
      if (streamController.signal.aborted) {
        void antdMessage.info('已停止生成');
      } else {
        // 流式失败不再回退非流式接口：非流式调用会长时间独占服务端资源，
        // 失败时提示用户重试即可，避免与服务端过载互相放大。
        void antdMessage.error(`智能运营助理发送失败: ${getApiErrorMessage(error, '未知错误')}`);
        upsertConversation({ ...baseConversation, executionState: 'failed', executionLabel: '执行失败' });
      }
    } finally {
      abortRef.current = null;
      setSending(false);
    }
  };

  const streamAssistantResponse = async (
    optimisticConversation: ConversationState,
    sessionId: string,
    messageText: string,
    signal: AbortSignal,
    options: SendMessageOptions = {},
  ) => {
    const assistantMessageId = createTransientId('assistant');
    let streamedContent = '';
    let donePayload: AssistantStreamDonePayload = {};
    let toolCalls: AssistantStreamToolCall[] = [];
    let toolResults: AssistantToolResult[] = [];
    let executionState: ConversationState['executionState'] = 'thinking';
    let executionLabel = '正在思考';
    const createdAt = dayjs().format('YYYY-MM-DD HH:mm');

    const renderStreamedConversation = (content: string): ConversationState => {
      const version: AssistantMessageVersion = {
        id: `${options.retryOfMessageId || assistantMessageId}-version-stream`,
        title: donePayload.runtime?.modelName || runtimeDisplayName,
        content,
        createdAt,
        toolCalls,
        toolResults,
      };
      if (options.retryOfMessageId) {
        return {
          ...updateAssistantRetryVersion(optimisticConversation, options.retryOfMessageId, version),
          sessionId: donePayload.sessionId || optimisticConversation.sessionId,
          proposedActions: donePayload.proposedActions || [],
        };
      }
      return {
        ...optimisticConversation,
        executionState,
        executionLabel,
        sessionId: donePayload.sessionId || optimisticConversation.sessionId,
        preview: content || '正在生成...',
        updatedAt: createdAt,
        messages: [
          ...optimisticConversation.messages,
          {
            id: assistantMessageId,
            role: 'assistant',
            title: version.title,
            content,
            createdAt,
            toolCalls,
            toolResults,
          },
        ],
        proposedActions: donePayload.proposedActions || [],
        toolCalls: {
          ...(optimisticConversation.toolCalls || {}),
          [assistantMessageId]: toolCalls,
        },
        toolResults: {
          ...(optimisticConversation.toolResults || {}),
          [assistantMessageId]: toolResults,
        },
      };
    };

    await dashboardApi.streamAssistantMessage({
        message: messageText,
        sessionId,
        agentBindingId: optimisticConversation.agentBindingId ?? undefined,
        retryOfMessageId: options.retryOfMessageId,
      }, {
        onDelta: (content) => {
          executionState = 'waiting';
          executionLabel = '正在生成回复';
          streamedContent += content;
          upsertConversation(renderStreamedConversation(streamedContent));
        },
        onToolCall: (toolCall) => {
          executionState = 'tool_running';
          executionLabel = `正在执行 ${toolCall.name}`;
          toolCalls = [...toolCalls, toolCall];
          upsertConversation(renderStreamedConversation(streamedContent));
        },
        onToolResult: (toolResult) => {
          toolResults = [...toolResults, toolResult];
          executionState = toolResult.status === 'failed' ? 'failed' : 'waiting';
          executionLabel = toolResult.status === 'failed' ? '工具执行失败' : '等待 Agent 响应';
          upsertConversation(renderStreamedConversation(streamedContent));
        },
        onPhase: (phase: AssistantStreamPhase) => {
          executionState = phase.phase;
          executionLabel = phase.label || phase.phase;
          upsertConversation(renderStreamedConversation(streamedContent));
        },
        onDone: (payload) => {
          donePayload = payload;
          if (payload.runtime) setRuntime(payload.runtime);
          if (payload.toolResults) toolResults = payload.toolResults;
        },
        onError: (message) => {
          throw new Error(message);
        },
      }, { signal });

    executionState = 'done';
    executionLabel = '执行完成';
    return renderStreamedConversation(streamedContent);
  };

  const exportConversation = React.useCallback(() => {
    if (!activeConversation) return;
    const markdown = [`# ${activeConversation.title}`, '', ...activeConversation.messages.flatMap((message) => [
      `## ${message.title}`,
      message.createdAt ? `_${message.createdAt}_` : '',
      '',
      message.content,
      '',
    ])].join('\n');
    const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${activeConversation.title || 'pi-agent-session'}.md`;
    anchor.click();
    URL.revokeObjectURL(url);
  }, [activeConversation]);

  const handleSendMessage = () => sendMessage(draft);

  const handleConfirmAction = async (action: AssistantProposedAction) => {
    const activeSessionId = activeConversation?.sessionId;
    if (!activeSessionId) return;
    try {
      const prepared = await dashboardApi.prepareAssistantAction({
        sessionId: activeSessionId,
        action,
      });
      const confirmed = await dashboardApi.confirmAssistantAction(Number(prepared.data.id));
      upsertConversation({
        ...(activeConversation as ConversationState),
        proposedActions: (activeConversation?.proposedActions || []).map((item) => (
          item.target === action.target && item.actionType === action.actionType
            ? { ...item, ...confirmed.data }
            : item
        )),
      });
    } catch (error: unknown) {
      void antdMessage.error(`确认执行失败: ${getApiErrorMessage(error, '未知错误')}`);
    }
  };

  const handleCancelAction = async (action: AssistantProposedAction) => {
    if (!action.id) return;
    try {
      const cancelled = await dashboardApi.cancelAssistantAction(action.id);
      upsertConversation({
        ...(activeConversation as ConversationState),
        proposedActions: (activeConversation?.proposedActions || []).map((item) => (
          item.id === action.id ? { ...item, ...cancelled.data } : item
        )),
      });
    } catch (error: unknown) {
      void antdMessage.error(`取消动作失败: ${getApiErrorMessage(error, '未知错误')}`);
    }
  };

  const handleSelectConversation = React.useCallback((conversation: ConversationState) => {
    setActiveConversationId(conversation.id);
    if (conversation.agentBindingId) setSelectedBindingId(conversation.agentBindingId);
  }, []);

  const stopGeneration = React.useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return {
    activeConversation,
    activeConversationId,
    attachments,
    availableBindings,
    detailScrollRef,
    draft,
    fileInputRef,
    handleAttachFiles,
    handleCancelAction,
    handleBranchMessage,
    handleConfirmAction,
    handleCopyMessage,
    handleCreateConversation,
    handleDeleteConversation,
    handleRenameCommit,
    handleRenameStart,
    handleSelectConversation,
    handleSendMessage,
    handleTogglePinned,
    exportConversation,
    loading,
    messageVersionIndexMap,
    renameDraft,
    renamingId,
    runtime,
    runtimeDisplayName,
    searchKeyword,
    selectedBindingId,
    sending,
    setAttachments,
    setActiveConversationId,
    setDraft,
    setMessageVersionIndexMap,
    setRenameDraft,
    setSearchKeyword,
    setSelectedBindingId,
    showScrollButton,
    sendMessage,
    scrollToBottom,
    stopGeneration,
    updateScrollState,
    visibleConversations,
  };
};
