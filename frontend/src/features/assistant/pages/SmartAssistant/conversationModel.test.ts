import { describe, expect, it, vi } from 'vitest';
import { brandLabel, buildAttachmentSummary, buildConversationFromSession, buildConversationTitle, createDraftConversation, getActionStatusText, getConversationStatusText, getMessageVersions, pickPreferredBinding, sortConversations, toConversationPreview, toMessageAttachments } from './conversationModel';

describe('conversation model helpers', () => {
  it('builds titles, previews, attachments and brand labels', () => {
    expect(brandLabel('atlas')).toBe('atlas'); expect(brandLabel('')).toBe('未选择品牌');
    expect(buildConversationTitle('  这是标题  ', [])).toBe('这是标题');
    expect(buildConversationTitle('', [{ id: '1', name: '图片.png', size: 1, type: 'image' }])).toBe('图片.png');
    expect(toConversationPreview('  hi  ')).toBe('hi'); expect(buildAttachmentSummary([{ id: '1', name: 'a', size: 1, type: 'file' }, { id: '2', name: 'b', size: 1, type: 'file' }])).toBe('a、b');
    expect(toMessageAttachments([{ id: '1', name: 'a', size: 1, type: 'file' }])).toHaveLength(1);
  });
  it('creates and restores conversations with versions', () => {
    vi.spyOn(Date, 'now').mockReturnValue(1);
    expect(createDraftConversation()).toEqual(expect.objectContaining({ id: 'draft-1', syncStatus: 'draft' })); vi.restoreAllMocks();
    const record = { id: 2, title: 't', preview: 'p', updatedAt: '2026-01-01T00:00:00Z', messages: [{ id: 'm', title: 'M', content: 'C', createdAt: 'x', toolCalls: [], toolResults: [] }], proposedActions: [], sessionId: 's', pinned: true, brand: 'atlas' } as never;
    const restored = buildConversationFromSession(record); expect(restored.syncStatus).toBe('synced'); expect(getMessageVersions(restored.messages[0])).toHaveLength(1);
  });
  it('sorts and labels conversations/actions', () => {
    const a = { id: 'a', pinned: false, updatedAt: '2026-01-01', proposedActions: [], activityState: 'active' } as never;
    const b = { id: 'b', pinned: true, updatedAt: '2025-01-01', proposedActions: [], activityState: 'empty' } as never;
    expect(sortConversations([a, b])[0]).toBe(b); expect(getConversationStatusText(b)).toBe('草稿');
    expect(getActionStatusText({ status: 'executed' } as never)).toBe('已执行'); expect(getActionStatusText({ status: 'pending', riskLevel: 'high' } as never)).toBe('高风险');
    expect(pickPreferredBinding([{ agentBindingId: 2 }, { agentBindingId: 1 }] as never, 1)?.agentBindingId).toBe(1);
    expect(pickPreferredBinding([{ agentBindingId: 2 }, { agentBindingId: 1 }] as never, null)).toBeNull();
    expect(pickPreferredBinding([{ agentBindingId: 2 }, { agentBindingId: 1 }] as never, 99)).toBeNull();
  });
  it('covers empty and fallback conversation branches', () => {
    expect(brandLabel('unknown')).toBe('unknown');
    expect(buildConversationTitle('   ', [])).toBe('新对话');
    expect(toConversationPreview('   ')).toBe('暂无对话内容');
    expect(pickPreferredBinding([], null)).toBeNull();
    const pending = { id: 'p', pinned: false, updatedAt: '', proposedActions: [{ status: 'pending' }], activityState: 'active' } as never;
    expect(getConversationStatusText(pending)).toBe('待确认');
    expect(getActionStatusText({ status: 'cancelled' } as never)).toBe('已取消');
    expect(getActionStatusText({ status: 'pending', riskLevel: 'medium' } as never)).toBe('中风险');
    expect(getActionStatusText({ status: 'pending', riskLevel: 'low' } as never)).toBe('低风险');
    expect(getActionStatusText({ status: 'pending' } as never)).toBe('待确认');
    expect(getMessageVersions({ id: 'm', title: '', content: '', createdAt: '', versions: [{ id: '', title: '', content: '', createdAt: '' }] } as never)[0].id).toBe('m-version-1');
    expect(buildConversationTitle('这是一个非常非常长的对话标题用于覆盖截断逻辑并且超过二十二个字符长度', [])).toMatch(/\.\.\.$/);
    expect(pickPreferredBinding([{ agentBindingId: 2 }] as never, 9)?.agentBindingId).toBe(2);
    const noDate = { id: 'a', pinned: false, updatedAt: '', proposedActions: [], activityState: 'active' } as never;
    const dated = { id: 'b', pinned: false, updatedAt: '2026-01-01', proposedActions: [], activityState: 'active' } as never;
    expect(sortConversations([noDate, dated])[0]).toBe(noDate);
    expect(sortConversations([dated, noDate])[0]).toBe(noDate);
  });
  it('restores an empty session and orders equal-priority conversations without mutating input', () => {
    const empty = buildConversationFromSession({ id: 8, title: '空会话', preview: '', updatedAt: '', messages: [], proposedActions: [], sessionId: 's8', pinned: false, brand: '', agentBindingId: null } as never);
    expect(empty).toMatchObject({ activityState: 'empty', updatedAt: '', brand: undefined, agentBindingId: undefined, toolCalls: {}, toolResults: {} });
    const older = { ...empty, id: 'old', updatedAt: '2026-01-01' };
    const newer = { ...empty, id: 'new', updatedAt: '2026-02-01' };
    const input = [older, newer];
    expect(sortConversations(input).map(item => item.id)).toEqual(['new', 'old']);
    expect(input).toEqual([older, newer]);
    expect(sortConversations([{ ...empty, id: 'a' }, { ...empty, id: 'b' }]).map(item => item.id)).toEqual(['b', 'a']);
    expect(getConversationStatusText({ ...empty, activityState: 'active' })).toBe('已同步');
    for (const brand of ['meadow', 'harbor', 'summit', 'riviera', 'lumen']) expect(brandLabel(brand)).toBe(brand);
  });
});
