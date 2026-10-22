import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MessageStream } from './MessageStream';
import type React from 'react';

const props = (activeConversation: unknown = null) => ({ activeConversation, availableBindings: [], availableBrands: [], currentConversationBrand: null, detailScrollRef: { current: null }, handleCopyMessage: vi.fn(async () => {}), loading: false, messageVersionIndexMap: {}, scrollToBottom: vi.fn(), sendMessage: vi.fn(async () => {}), setMessageVersionIndexMap: vi.fn(), setSelectedBindingId: vi.fn(), setSelectedBrand: vi.fn(), showScrollButton: true, updateScrollState: vi.fn() });
describe('MessageStream', () => {
  it('renders empty state and scroll control', () => { const p = props(); render(<MessageStream {...(p as unknown as React.ComponentProps<typeof MessageStream>)} />); expect(screen.getByLabelText('空对话状态')).toBeInTheDocument(); fireEvent.click(screen.getByText('回到底部')); expect(p.scrollToBottom).toHaveBeenCalled(); });
  it('renders messages and copy action', () => { const p = props({ messages: [{ id: 'm', role: 'operator', title: '用户', content: '你好', createdAt: '' }], toolCalls: {}, toolResults: {} }); render(<MessageStream {...(p as unknown as React.ComponentProps<typeof MessageStream>)} />); expect(screen.getByText('你好')).toBeInTheDocument(); fireEvent.click(screen.getByRole('button', { name: '复制消息' })); expect(p.handleCopyMessage).toHaveBeenCalledWith('你好'); });
  it('renders assistant versions, attachments and retry', () => { const p = props({ messages: [{ id: 'm1', role: 'operator', title: '用户', content: '问', createdAt: '' }, { id: 'm2', role: 'assistant', title: '助手', content: '答', createdAt: '', versions: [{ id: 'v1', title: '助手', content: '旧答', createdAt: '' }, { id: 'v2', title: '助手', content: '新答', createdAt: '' }], attachments: [{ id: 'f', type: 'file', name: 'a.txt', size: 1024 }] }], toolCalls: {}, toolResults: {} }); render(<MessageStream {...(p as unknown as React.ComponentProps<typeof MessageStream>)} />); fireEvent.click(screen.getByRole('button', { name: '重试消息' })); expect(p.sendMessage).toHaveBeenCalledWith('问', { retryOfMessageId: 'm2' }); fireEvent.click(screen.getByRole('button', { name: '查看上一版回复' })); expect(p.setMessageVersionIndexMap).toHaveBeenCalled(); expect(screen.getByText('a.txt')).toBeInTheDocument(); });

  it('renders the agent phase and expandable tool execution details', () => {
    const p = props({
      executionState: 'tool_running',
      messages: [{ id: 'm', role: 'assistant', title: 'PI Agent', content: '', createdAt: '' }],
      toolCalls: {
        m: [{ id: 'call-1', name: 'bash', arguments: '{"command":"pwd"}', status: 'failed' }],
      },
      toolResults: {
        m: [{ toolCallId: 'call-1', toolName: 'bash', summary: 'command failed', status: 'failed', output: 'permission denied', exitCode: 1 }],
      },
    });

    render(<MessageStream {...(p as unknown as React.ComponentProps<typeof MessageStream>)} />);

    expect(screen.getByRole('status')).toHaveTextContent('正在执行工具');
    expect(screen.getByText('执行失败')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看工具 bash 详情' }));
    expect(screen.getByText('{"command":"pwd"}')).toBeInTheDocument();
    expect(screen.getByText('permission denied')).toBeInTheDocument();
    expect(screen.getByText('退出码 1')).toBeInTheDocument();
  });
});
