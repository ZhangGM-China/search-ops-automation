import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import SmartAssistant from './index';
import { dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

vi.mock('@/shared/api', () => ({
  dashboardApi: {
    getAssistantContext: vi.fn(),
    getAssistantSessions: vi.fn(),
    getAssistantRuntimeInspector: vi.fn(),
    updateAssistantSession: vi.fn(),
    deleteAssistantSession: vi.fn(),
    sendAssistantMessage: vi.fn(),
    streamAssistantMessage: vi.fn(),
    prepareAssistantAction: vi.fn(),
    confirmAssistantAction: vi.fn(),
    cancelAssistantAction: vi.fn(),
  },
}));

describe('SmartAssistant Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(dashboardApi.getAssistantRuntimeInspector).mockResolvedValue(axiosResponse({ runtime: { provider: 'PI Agent', modelLabel: 'PI Agent' }, permissions: { accountId: 1, brands: ['atlas'], isAdmin: false, workspaceScoped: true }, workspace: { roots: ['/tmp'] }, tools: [{ name: 'read_today_context', description: 'read' }], events: [], context: { sessionId: null, summary: null } }));
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue(axiosResponse({
      summary: {
        taskCount: 7,
        pendingCount: 3,
        actionCount: 11,
        monitoringCount: 4,
      },
      suggestedPrompts: [
        '分析 Atlas 今日高风险 Query 的共同原因',
        '检查衬衫 女士的最近 AI 建议',
      ],
      messages: [
        {
          id: 'analysis-1',
          role: 'assistant',
          title: '衬衫 女士',
          content: '自动治理后仍有不相关商品，建议补充拉黑并观察。',
          createdAt: '2026-06-22 09:00',
        },
      ],
      capabilities: [
        {
          key: 'read_task_context',
          title: '读取真实任务上下文',
          description: '已接入本地治理库和 AI 分析结果。',
          status: 'ready',
        },
      ],
      runtime: {
        provider: 'PI Agent',
        modelLabel: 'ATLAS Agent',
        modelName: 'gpt-4.1',
        agentId: 'agt_test',
        availableModels: ['gpt-4.1', 'gpt-4o'],
        supportsModelSwitch: true,
      },
      availableBindings: [
        {
          agentBindingId: 11,
          brand: 'atlas',
          displayName: 'ATLAS Agent',
          modelDisplayName: 'ATLAS Agent',
          modelName: 'gpt-4.1',
          availableModels: ['gpt-4.1', 'gpt-4o'],
          supportsModelSwitch: true,
          bindingType: 'agent',
        },
      ],
      availableBrands: ['atlas'],
    }));
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue(axiosResponse({
      items: [
        {
          id: 1,
          sessionId: 'assistant-session',
          brand: 'atlas',
          agentBindingId: 11,
          title: '衬衫 女士',
          manualTitle: null,
          generatedTitle: '衬衫 女士',
          preview: '自动治理后仍有不相关商品，建议补充拉黑并观察。',
          pinned: false,
          updatedAt: '2026-06-22T09:00:00',
          lastOpenedAt: '2026-06-22T09:00:00',
          messages: [
            {
              id: 'analysis-1',
              role: 'assistant',
              title: '衬衫 女士',
              content: '自动治理后仍有不相关商品，建议补充拉黑并观察。',
              createdAt: '2026-06-22 09:00',
            },
          ],
          proposedActions: [],
          runtime: {
            provider: 'PI Agent',
            modelLabel: 'PI Agent',
            agentId: 'agt_test',
          },
        },
      ],
    }));
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementation(async (_payload, handlers) => {
      handlers.onDelta('建议');
      handlers.onDelta('先复核');
      handlers.onDone({
        sessionId: 'assistant-session',
        proposedActions: [],
        runtime: {
          provider: 'PI Agent',
          modelLabel: 'ATLAS Agent',
          modelName: 'gpt-4.1',
        },
      });
    });
    vi.mocked(dashboardApi.prepareAssistantAction).mockResolvedValue(axiosResponse({
      id: 301,
      status: 'prepared',
      actionType: 'demote',
      brand: 'atlas',
      query: '衬衫 女士',
      target: 'SKU-1',
      reason: '价格明显高于同 Query 均值',
      riskLevel: 'medium',
    }));
    vi.mocked(dashboardApi.confirmAssistantAction).mockResolvedValue(axiosResponse({
      id: 301,
      status: 'executed',
      actionType: 'demote',
      brand: 'atlas',
      query: '衬衫 女士',
      target: 'SKU-1',
      reason: '价格明显高于同 Query 均值',
      riskLevel: 'medium',
    }));
    vi.mocked(dashboardApi.cancelAssistantAction).mockResolvedValue(axiosResponse({
      id: 301,
      status: 'cancelled',
      actionType: 'demote',
      brand: 'atlas',
      query: '衬衫 女士',
      target: 'SKU-1',
      reason: '价格明显高于同 Query 均值',
      riskLevel: 'medium',
    }));
  });

  it('renders real assistant context from dashboard api', async () => {
    render(<SmartAssistant />);

    expect(screen.queryByTestId('pi-agent-workspace')).not.toBeInTheDocument();
    expect(screen.queryByText('PI Agent 工作台')).not.toBeInTheDocument();
    expect(screen.queryByText('原生工具驱动的运营诊断与执行')).not.toBeInTheDocument();
    const currentConversation = await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' });
    expect(currentConversation).toBeInTheDocument();
    expect(currentConversation).toHaveAttribute('aria-current', 'page');
    expect((await screen.findAllByText('衬衫 女士')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('自动治理后仍有不相关商品，建议补充拉黑并观察。').length).toBeGreaterThan(0);
    expect(screen.getByLabelText('当前模型')).toHaveTextContent('gpt-4.1');
    expect(screen.queryByText('GPT-4o')).not.toBeInTheDocument();
    expect(screen.queryByText('Claude 3.7')).not.toBeInTheDocument();
    expect(screen.queryByText('读取真实任务上下文')).not.toBeInTheDocument();
    expect(screen.queryByText('今日任务')).not.toBeInTheDocument();
    expect(screen.queryByText('对话窗口')).not.toBeInTheDocument();
    expect(screen.queryByText('建议发起')).not.toBeInTheDocument();
    expect(screen.queryByText('最近上下文')).not.toBeInTheDocument();
    expect(screen.queryByText('试样模式')).not.toBeInTheDocument();
    expect(screen.queryByText('前端试样')).not.toBeInTheDocument();
  });

  it('exports the active conversation as a readable markdown file', async () => {
    const createObjectURL = vi.fn(() => 'blob:assistant-export');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    render(<SmartAssistant />);

    await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' });
    fireEvent.click(screen.getByLabelText('打开对话操作 衬衫 女士'));
    fireEvent.click(await screen.findByText('导出'));

    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:assistant-export');
    anchorClick.mockRestore();
  });

  it('keeps empty state unselected until the first message starts a conversation', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue(axiosResponse({
      summary: {
        taskCount: 0,
        pendingCount: 0,
        actionCount: 0,
        monitoringCount: 0,
      },
      suggestedPrompts: [],
      messages: [],
      capabilities: [],
      runtime: {
        provider: 'PI Agent',
        modelLabel: 'PI Agent',
      },
      availableBindings: [],
      availableBrands: ['atlas'],
    }));
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue(axiosResponse({ items: [] }));

    render(<SmartAssistant />);

    expect(await screen.findByText('暂无对话')).toBeInTheDocument();
    expect(screen.queryByRole('button', { current: 'page' })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '先分析今天的治理异常' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => {
      expect(dashboardApi.streamAssistantMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          message: '先分析今天的治理异常',
          sessionId: expect.any(String),
        }),
        expect.objectContaining({
          onDelta: expect.any(Function),
          onDone: expect.any(Function),
        }),
        { signal: expect.any(AbortSignal) },
      );
    });

    expect(await screen.findByRole('button', { name: '打开历史对话 先分析今天的治理异常' })).toHaveAttribute('aria-current', 'page');
  });

  it('refreshes assistant bindings before send when the current page holds stale multi-brand context', async () => {
    vi.mocked(dashboardApi.getAssistantContext)
      .mockResolvedValueOnce(axiosResponse({
        summary: {
          taskCount: 0,
          pendingCount: 0,
          actionCount: 0,
          monitoringCount: 0,
        },
        suggestedPrompts: [],
        messages: [],
        capabilities: [],
        runtime: {
          provider: 'PI Agent',
          modelLabel: 'PI Agent',
        },
        availableBindings: [],
        availableBrands: ['atlas', 'meadow'],
      }))
      .mockResolvedValueOnce(axiosResponse({
        summary: {
          taskCount: 0,
          pendingCount: 0,
          actionCount: 0,
          monitoringCount: 0,
        },
        suggestedPrompts: [],
        messages: [],
        capabilities: [],
        runtime: {
          provider: 'PI Agent',
          modelLabel: 'ATLAS Agent',
          modelName: 'qwen3.7-max',
        },
        availableBindings: [
          {
            agentBindingId: 21,
            brand: 'atlas',
            displayName: 'ATLAS 助理',
            modelDisplayName: 'Qwen 3.7 Max',
            modelName: 'qwen3.7-max',
            availableModels: ['qwen3.6-plus', 'qwen3.7-max', 'glm-5.2'],
            supportsModelSwitch: false,
            bindingType: 'agent',
          },
        ],
        availableBrands: ['atlas', 'meadow'],
      }));
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValueOnce(axiosResponse({
      items: [
        {
          id: 9,
          sessionId: 'stale-atlas-session',
          brand: 'atlas',
          agentBindingId: null,
          title: '旧会话',
          manualTitle: null,
          generatedTitle: '旧会话',
          preview: '暂无对话内容',
          pinned: false,
          updatedAt: '2026-06-22T09:00:00',
          lastOpenedAt: '2026-06-22T09:00:00',
          messages: [],
          proposedActions: [],
          runtime: {
            provider: 'PI Agent',
            modelLabel: 'PI Agent',
            agentId: 'agt_test',
          },
        },
      ],
    }));

    render(<SmartAssistant />);

    expect(await screen.findByText('旧会话')).toBeInTheDocument();
    expect(screen.queryByText('账号权限工作区')).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '你好' },
    });
    expect(screen.getByRole('button', { name: '发送消息' })).not.toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => {
      expect(dashboardApi.streamAssistantMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          message: '你好',
          agentBindingId: 21,
        }),
        expect.objectContaining({
          onDelta: expect.any(Function),
          onDone: expect.any(Function),
        }),
        { signal: expect.any(AbortSignal) },
      );
    });
  });

  it('does not expose implementation-specific permission workspace copy', async () => {
    render(<SmartAssistant />);

    await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' });
    expect(screen.queryByText('账号权限工作区')).not.toBeInTheDocument();
    expect(screen.queryByText('按账号权限')).not.toBeInTheDocument();
  });

  it('uses the currently selected binding for composer copy and send payload even when the active conversation has older binding metadata', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue(axiosResponse({
      summary: {
        taskCount: 7,
        pendingCount: 3,
        actionCount: 11,
        monitoringCount: 4,
      },
      suggestedPrompts: [],
      messages: [],
      capabilities: [],
      runtime: {
        provider: 'PI Agent',
        modelLabel: 'ATLAS Agent',
        modelName: 'qwen3.7-max',
      },
      availableBindings: [
        {
          agentBindingId: 11,
          brand: 'atlas',
          displayName: 'ATLAS Agent',
          modelDisplayName: 'Qwen 3.7 Max',
          modelName: 'qwen3.7-max',
          availableModels: ['qwen3.6-plus', 'qwen3.7-max'],
          supportsModelSwitch: false,
          bindingType: 'agent',
        },
        {
          agentBindingId: 12,
          brand: 'atlas',
          displayName: 'ATLAS Agent',
          modelDisplayName: 'Qwen 3.6 Plus',
          modelName: 'qwen3.6-plus',
          availableModels: ['qwen3.6-plus', 'qwen3.7-max'],
          supportsModelSwitch: false,
          bindingType: 'agent',
        },
      ],
      availableBrands: ['atlas'],
    }));
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValue(axiosResponse({
      items: [
        {
          id: 1,
          sessionId: 'assistant-session',
          brand: 'atlas',
          agentBindingId: 11,
          title: '衬衫 女士',
          manualTitle: null,
          generatedTitle: '衬衫 女士',
          preview: '自动治理后仍有不相关商品，建议补充拉黑并观察。',
          pinned: false,
          updatedAt: '2026-06-22T09:00:00',
          lastOpenedAt: '2026-06-22T09:00:00',
          messages: [
            {
              id: 'analysis-1',
              role: 'assistant',
              title: '衬衫 女士',
              content: '自动治理后仍有不相关商品，建议补充拉黑并观察。',
              createdAt: '2026-06-22 09:00',
            },
          ],
          proposedActions: [],
          runtime: {
            provider: 'PI Agent',
            modelLabel: 'ATLAS Agent',
            modelName: 'qwen3.7-max',
          },
        },
      ],
    }));
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementation(async (_payload, handlers) => {
      handlers.onDelta('你好');
      handlers.onDone({
        sessionId: 'assistant-session',
        proposedActions: [],
        runtime: {
          provider: 'PI Agent',
          modelLabel: 'ATLAS Agent',
          modelName: 'qwen3.6-plus',
        },
      });
    });

    render(<SmartAssistant />);

    expect(await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '切换模型' }));
    fireEvent.click(await screen.findByText('qwen3.6-plus'));

    expect(screen.getByRole('button', { name: '切换模型' })).toHaveTextContent('qwen3.6-plus');
    expect(screen.getByPlaceholderText('给 qwen3.6-plus 发消息')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '你好，切模型验证' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => {
      expect(dashboardApi.streamAssistantMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          message: '你好，切模型验证',
          agentBindingId: 12,
        }),
        expect.objectContaining({
          onDelta: expect.any(Function),
          onDone: expect.any(Function),
        }),
        { signal: expect.any(AbortSignal) },
      );
    });
    expect(screen.getByRole('button', { name: '切换模型' })).toHaveTextContent('qwen3.6-plus');
  });

  it('sends an assistant message and renders proposed actions as pending plans', async () => {
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementationOnce(async (_payload, handlers) => {
      handlers.onDelta('建议先复核第 3 位高价商品，不直接执行分桶。');
      handlers.onDone({
        sessionId: 'assistant-session',
        proposedActions: [
          {
            actionType: 'demote',
            brand: 'atlas',
            query: '衬衫 女士',
            target: 'SKU-1',
            reason: '价格明显高于同 Query 均值',
            riskLevel: 'medium',
          },
        ],
        runtime: {
          provider: 'PI Agent',
          modelLabel: 'ATLAS Agent',
          modelName: 'gpt-4.1',
        },
      });
    });
    render(<SmartAssistant />);

    expect(await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '分析衬衫 女士' },
    });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    expect((await screen.findAllByText('建议先复核第 3 位高价商品，不直接执行分桶。')).length).toBeGreaterThan(0);
    expect(screen.getByText('待确认治理计划')).toBeInTheDocument();
    expect(screen.getByText('demote')).toBeInTheDocument();
    expect(screen.getByText('SKU-1')).toBeInTheDocument();
    expect(screen.queryByText('已执行')).not.toBeInTheDocument();
    expect(dashboardApi.sendAssistantMessage).not.toHaveBeenCalled();
  });

  it('restores the composer when the streaming assistant request fails', async () => {
    vi.mocked(dashboardApi.streamAssistantMessage).mockRejectedValueOnce(new Error('stream unavailable'));

    render(<SmartAssistant />);

    expect(await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '失败态验证' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '发送消息' })).toBeInTheDocument();
    });
    expect(dashboardApi.sendAssistantMessage).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '停止生成' })).not.toBeInTheDocument();
    expect(screen.queryByText('失败态验证')).not.toBeInTheDocument();
  });

  it('renders real model metadata and confirms a prepared assistant action', async () => {
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementationOnce(async (_payload, handlers) => {
      handlers.onToolResult?.({
        toolName: 'read_today_context',
        summary: '已读取今日 7 条任务与 11 条动作。',
        status: 'completed',
      });
      handlers.onDone({
        sessionId: 'assistant-session',
        proposedActions: [
          {
            actionType: 'demote',
            brand: 'atlas',
            query: '衬衫 女士',
            target: 'SKU-1',
            reason: '价格明显高于同 Query 均值',
            riskLevel: 'medium',
          },
        ],
        runtime: {
          provider: 'PI Agent',
          modelLabel: 'ATLAS Agent',
          modelName: 'gpt-4.1',
        },
      });
    });
    render(<SmartAssistant />);

    expect(await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' })).toBeInTheDocument();
    expect(screen.getByLabelText('当前模型')).toHaveTextContent('gpt-4.1');
    expect(screen.getByPlaceholderText('给 gpt-4.1 发消息')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '分析衬衫 女士' },
    });
    fireEvent.click(screen.getByRole('button', { name: /发\s*送/ }));

    expect(await screen.findByText('已读取今日 7 条任务与 11 条动作。')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '确认执行 demote' }));

    await waitFor(() => {
      expect(dashboardApi.prepareAssistantAction).toHaveBeenCalledWith({
        sessionId: expect.any(String),
        action: expect.objectContaining({
          actionType: 'demote',
          target: 'SKU-1',
        }),
      });
    });
    await waitFor(() => {
      expect(dashboardApi.confirmAssistantAction).toHaveBeenCalledWith(301);
    });
    expect(await screen.findByText('已执行')).toBeInTheDocument();
  });

  it('streams assistant response chunks into the active conversation', async () => {
    render(<SmartAssistant />);

    expect(await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '流式分析衬衫 女士' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => {
      expect(dashboardApi.streamAssistantMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          message: '流式分析衬衫 女士',
          sessionId: expect.any(String),
        }),
        expect.objectContaining({
          onDelta: expect.any(Function),
          onDone: expect.any(Function),
        }),
        { signal: expect.any(AbortSignal) },
      );
    });
    expect((await screen.findAllByText('建议先复核')).length).toBeGreaterThan(0);
    expect(dashboardApi.sendAssistantMessage).not.toHaveBeenCalled();
  });

  it('supports attachment shell and allows sending with attachments only', async () => {
    render(<SmartAssistant />);

    expect(await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' })).toBeInTheDocument();
    const fileInput = screen.getByLabelText('选择附件') as HTMLInputElement;
    const attachment = new File(['hello'], 'ops-note.txt', { type: 'text/plain' });
    fireEvent.change(fileInput, { target: { files: [attachment] } });

    expect(await screen.findByText('ops-note.txt')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

    await waitFor(() => {
      expect(dashboardApi.streamAssistantMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          message: '\n\n附件：ops-note.txt',
          sessionId: expect.any(String),
        }),
        expect.objectContaining({
          onDelta: expect.any(Function),
          onDone: expect.any(Function),
        }),
        { signal: expect.any(AbortSignal) },
      );
    });
    expect(await screen.findByLabelText('消息附件')).toBeInTheDocument();
  });

  it('allows stopping an in-flight streaming response and restores the send composer', async () => {
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementationOnce(async (_payload, _handlers, options) => (
      new Promise<void>((_resolve, reject) => {
        if (!options?.signal) {
          reject(new Error('missing abort signal'));
          return;
        }
        options.signal.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'));
        });
      })
    ));

    render(<SmartAssistant />);

    expect(await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '请持续生成，稍后停止' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

    expect(await screen.findByRole('button', { name: '停止生成' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '停止生成' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '发送消息' })).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: '停止生成' })).not.toBeInTheDocument();
    expect(dashboardApi.sendAssistantMessage).not.toHaveBeenCalled();
  });

  it('renders streamed tool calls as compact tool blocks', async () => {
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementationOnce(async (_payload, handlers) => {
      handlers.onToolCall?.({
        id: 'call-1',
        name: 'search_ops_context',
        arguments: '{"brand":"atlas"}',
      });
      handlers.onDelta('工具上下文已读取');
      handlers.onDone({
        sessionId: 'assistant-session',
        proposedActions: [],
      });
    });
    render(<SmartAssistant />);

    expect(await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '检查工具调用' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

    expect(await screen.findByText('工具调用')).toBeInTheDocument();
    expect(screen.getByText('search_ops_context')).toBeInTheDocument();
    expect(screen.getByText('{"brand":"atlas"}')).toBeInTheDocument();
  });

  it('retries an assistant reply in place and keeps version history on the same message node', async () => {
    let attempt = 0;
    vi.mocked(dashboardApi.streamAssistantMessage).mockImplementation(async (_payload, handlers) => {
      attempt += 1;
      handlers.onDelta(attempt === 1 ? '首次回答' : '重试回答');
      handlers.onDone({
        sessionId: 'assistant-session',
        proposedActions: [],
        runtime: {
          provider: 'PI Agent',
          modelLabel: 'ATLAS Agent',
          modelName: 'gpt-4.1',
        },
      });
    });
    render(<SmartAssistant />);

    expect(await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('输入智能运营助理消息'), {
      target: { value: '帮我再看一次' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

    expect(await screen.findAllByLabelText('重试消息')).toHaveLength(1);
    fireEvent.click(screen.getByLabelText('重试消息'));

    await waitFor(() => {
      expect(dashboardApi.streamAssistantMessage).toHaveBeenLastCalledWith(
        expect.objectContaining({
          message: '帮我再看一次',
          sessionId: expect.any(String),
          retryOfMessageId: expect.any(String),
        }),
        expect.objectContaining({
          onDelta: expect.any(Function),
          onDone: expect.any(Function),
        }),
        { signal: expect.any(AbortSignal) },
      );
    });
    expect(screen.getAllByText('帮我再看一次')).toHaveLength(1);
    expect((await screen.findAllByText('重试回答')).length).toBeGreaterThan(0);
    expect(screen.getByText('2/2')).toBeInTheDocument();
  });

  it('renders markdown code blocks and bullet lists with semantic elements', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValueOnce(axiosResponse({
      summary: {
        taskCount: 1,
        pendingCount: 0,
        actionCount: 0,
        monitoringCount: 0,
      },
      suggestedPrompts: [],
      messages: [],
      capabilities: [],
      runtime: {
        provider: 'PI Agent',
        modelLabel: 'PI Agent',
      },
      availableBindings: [],
      availableBrands: ['atlas'],
    }));
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValueOnce(axiosResponse({
      items: [
        {
          id: 7,
          sessionId: 'markdown-session',
          title: '治理摘要',
          preview: '建议动作',
          pinned: false,
          updatedAt: '2026-07-03T18:00:00',
          lastOpenedAt: '2026-07-03T18:00:00',
          messages: [
            {
              id: 'markdown-1',
              role: 'assistant',
              title: '治理摘要',
              content: '建议动作：\n- 复核高价商品\n- 观察 CTR\n\n```json\n{"brand":"atlas"}\n```',
              createdAt: '2026-07-03 18:00',
            },
          ],
          proposedActions: [],
          runtime: {
            provider: 'PI Agent',
            modelLabel: 'PI Agent',
          },
        },
      ],
    }));

    render(<SmartAssistant />);

    expect(await screen.findByText('建议动作：')).toBeInTheDocument();
    expect(screen.getByRole('list')).toBeInTheDocument();
    expect(screen.getByText('复核高价商品').closest('li')).not.toBeNull();
    expect(screen.getByText('观察 CTR').closest('li')).not.toBeNull();
    expect(screen.getByText('{"brand":"atlas"}').tagName.toLowerCase()).toBe('code');
  });

  it('renders markdown tables with semantic table elements', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValueOnce(axiosResponse({
      summary: {
        taskCount: 1,
        pendingCount: 0,
        actionCount: 0,
        monitoringCount: 0,
      },
      suggestedPrompts: [],
      messages: [],
      capabilities: [],
      runtime: {
        provider: 'PI Agent',
        modelLabel: 'PI Agent',
      },
      availableBindings: [],
      availableBrands: ['atlas'],
    }));
    vi.mocked(dashboardApi.getAssistantSessions).mockResolvedValueOnce(axiosResponse({
      items: [
        {
          id: 8,
          sessionId: 'markdown-table-session',
          title: '断码表格',
          preview: '尺码覆盖',
          pinned: false,
          updatedAt: '2026-07-03T18:00:00',
          lastOpenedAt: '2026-07-03T18:00:00',
          messages: [
            {
              id: 'markdown-table-1',
              role: 'assistant',
              title: '断码表格',
              content: '| Query | 覆盖率 |\n| --- | --- |\n| 四折精选 女士 | 40% |\n| 连衣裙 女士 | 62% |',
              createdAt: '2026-07-03 18:00',
            },
          ],
          proposedActions: [],
          runtime: {
            provider: 'PI Agent',
            modelLabel: 'PI Agent',
          },
        },
      ],
    }));

    render(<SmartAssistant />);

    expect(await screen.findByRole('table')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Query' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '四折精选 女士' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: '62%' })).toBeInTheDocument();
  });

  it('supports pinning and manually renaming a conversation title', async () => {
    render(<SmartAssistant />);

    const currentConversation = await screen.findByRole('button', { name: '打开历史对话 衬衫 女士' });
    fireEvent.click(screen.getByLabelText('打开对话操作 衬衫 女士'));
    fireEvent.click(await screen.findByText('置顶'));
    await waitFor(() => {
      expect(dashboardApi.updateAssistantSession).toHaveBeenCalledWith(1, { pinned: true });
    });
    expect(currentConversation).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('打开对话操作 衬衫 女士'));
    fireEvent.click(await screen.findByText('重命名'));
    const renameInput = screen.getByLabelText('编辑对话标题');
    fireEvent.change(renameInput, { target: { value: 'Atlas 衬衫治理复盘' } });
    fireEvent.keyDown(renameInput, { key: 'Enter' });

    expect(await screen.findByRole('button', { name: '打开历史对话 Atlas 衬衫治理复盘' })).toBeInTheDocument();
    expect(currentConversation).toBeInTheDocument();
  });

  it('shows an explicit unconfigured state instead of a transport placeholder', async () => {
    vi.mocked(dashboardApi.getAssistantContext).mockResolvedValue(axiosResponse({
      summary: { taskCount: 0, pendingCount: 0, actionCount: 0, monitoringCount: 0 },
      suggestedPrompts: [],
      messages: [],
      capabilities: [],
      runtime: {
        provider: 'PI Agent',
        modelLabel: 'PI Agent',
      },
      availableBindings: [],
      availableBrands: ['atlas'],
    }));

    render(<SmartAssistant />);

    expect(await screen.findByLabelText('当前模型')).toHaveTextContent('PI Agent');
    expect(screen.queryByText('pi-agent')).not.toBeInTheDocument();
  });

});
