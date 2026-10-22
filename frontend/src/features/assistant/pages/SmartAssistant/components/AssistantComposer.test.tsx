import { fireEvent, render, screen } from '@testing-library/react';
import type React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { AssistantComposer } from './AssistantComposer';

const makeProps = (extra: Record<string, unknown> = {}): Record<string, unknown> => ({ activeConversation: null, attachments: [], availableBindings: [], draft: 'hello', fileInputRef: { current: null }, handleAttachFiles: vi.fn(async () => {}), handleCancelAction: vi.fn(async () => {}), handleConfirmAction: vi.fn(async () => {}), handleSendMessage: vi.fn(async () => {}), requiresExplicitBrandSelection: false, runtime: { provider: 'PI Agent', modelLabel: 'PI Agent' }, runtimeDisplayName: 'PI Agent', selectedBindingId: null, sending: false, setAttachments: vi.fn(), setDraft: vi.fn(), setSelectedBindingId: vi.fn(), stopGeneration: vi.fn(), ...extra });

describe('AssistantComposer', () => {
  it('sends, edits draft and shows pending actions', () => {
    const props = makeProps({ activeConversation: { proposedActions: [{ actionType: 'blacklist', id: 1, status: 'pending', brand: 'atlas', riskLevel: 'high', reason: '原因' }] } });
    render(<AssistantComposer {...(props as unknown as React.ComponentProps<typeof AssistantComposer>)} />);
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
    expect(props.handleSendMessage).toHaveBeenCalled();
    fireEvent.change(screen.getByRole('textbox', { name: '输入智能运营助理消息' }), { target: { value: 'new' } });
    expect(props.setDraft).toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /确认执行/ })); expect(props.handleConfirmAction).toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /取消动作/ })); expect(props.handleCancelAction).toHaveBeenCalled();
    expect(screen.queryByText('atlas')).not.toBeInTheDocument();
  });
  it('keeps the composer usable while generation can be stopped', () => {
    const props = makeProps({ draft: '', sending: true });
    render(<AssistantComposer {...(props as unknown as React.ComponentProps<typeof AssistantComposer>)} />);
    fireEvent.click(screen.getByRole('button', { name: '停止生成' })); expect(props.stopGeneration).toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '发送消息' })).toBeNull();
  });
  it('does not render duplicate model choices for brand bindings using the same effective model', () => {
    const duplicateBinding = (agentBindingId: number, brand: string) => ({
      agentBindingId,
      brand,
      displayName: `${brand} 助理`,
      modelDisplayName: '旧模型名称',
      modelName: 'deepseek-v4.1-flash',
      availableModels: ['deepseek-v4.1-flash'],
      supportsModelSwitch: false,
      bindingType: 'agent',
    });
    const props = makeProps({
      availableBindings: [duplicateBinding(1, 'atlas'), duplicateBinding(2, 'harbor')],
      runtime: { provider: 'PI Agent', modelLabel: 'PI Agent', modelName: 'deepseek-v4.1-flash' },
      runtimeDisplayName: 'deepseek-v4.1-flash',
    });

    render(<AssistantComposer {...(props as unknown as React.ComponentProps<typeof AssistantComposer>)} />);

    expect(screen.queryByRole('button', { name: '切换模型' })).not.toBeInTheDocument();
    expect(screen.getAllByText('deepseek-v4.1-flash')).toHaveLength(1);
  });
});
