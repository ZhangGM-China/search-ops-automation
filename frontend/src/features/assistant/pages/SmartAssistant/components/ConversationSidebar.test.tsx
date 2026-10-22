import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ConversationSidebar } from './ConversationSidebar';

const base = () => ({ activeConversationId: null, exportConversation: vi.fn(), handleCreateConversation: vi.fn(), handleDeleteConversation: vi.fn(async () => {}), handleRenameCommit: vi.fn(async () => {}), handleRenameStart: vi.fn(), handleTogglePinned: vi.fn(async () => {}), renameDraft: '', renamingId: null, searchKeyword: '', setActiveConversationId: vi.fn(), setRenameDraft: vi.fn(), setSearchKeyword: vi.fn(), setSelectedBindingId: vi.fn(), visibleConversations: [] });
describe('ConversationSidebar', () => {
  it('renders empty state and opens conversations', () => {
    const props = base(); const setActive = vi.fn();
    render(<ConversationSidebar {...props} setActiveConversationId={setActive} visibleConversations={[{ id: 'c1', title: '对话一', preview: '预览', pinned: true, brand: 'atlas', agentBindingId: null, proposedActions: [], activityState: 'active', updatedAt: '今天' } as never]} />);
    expect(screen.getByText('对话一')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /打开历史对话/ }));
    expect(setActive).toHaveBeenCalledWith('c1');
    expect(screen.queryByText('今天')).not.toBeInTheDocument();
    expect(screen.queryByText('预览')).not.toBeInTheDocument();
    expect(screen.queryByText('atlas')).not.toBeInTheDocument();
  });
  it('creates and searches conversations', () => {
    const props = base(); render(<ConversationSidebar {...props} />);
    expect(screen.getByText('暂无对话')).toBeInTheDocument(); fireEvent.click(screen.getByText('新对话')); expect(props.handleCreateConversation).toHaveBeenCalled();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索对话' }), { target: { value: 'x' } }); expect(props.setSearchKeyword).toHaveBeenCalledWith('x');
  });
});
