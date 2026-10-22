import dayjs from 'dayjs';
import type { AssistantStreamToolCall } from '@/shared/api';
import type {
  AssistantAvailableBinding,
  AssistantContextMessage,
  AssistantExecutionState,
  AssistantMessageAttachment,
  AssistantMessageVersion,
  AssistantProposedAction,
  AssistantRuntimeInfo,
  AssistantSessionRecord,
  AssistantToolResult,
} from '@/shared/types';

type ConversationStatus = 'draft' | 'synced';

export type DraftAttachment = {
  id: string;
  name: string;
  size: number;
  type: 'image' | 'file';
  mimeType?: string;
  url?: string;
};

export type SendMessageOptions = {
  retryOfMessageId?: string;
};

export type ConversationState = {
  id: string;
  sessionMetaId?: number;
  title: string;
  preview: string;
  updatedAt: string;
  source: 'current_context';
  activityState: 'active' | 'empty';
  messages: AssistantContextMessage[];
  proposedActions: AssistantProposedAction[];
  sessionId: string;
  brand?: string;
  agentBindingId?: number | null;
  syncStatus: ConversationStatus;
  pinned: boolean;
  toolCalls: Record<string, AssistantStreamToolCall[]>;
  toolResults: Record<string, AssistantToolResult[]>;
  executionState?: AssistantExecutionState;
  executionLabel?: string;
  parentSessionId?: string | null;
};

export const DEFAULT_RUNTIME: AssistantRuntimeInfo = {
  provider: 'PI Agent',
  modelLabel: 'PI Agent',
};

const brandLabel = (brand: string) => {
  const normalized = String(brand || '').trim();
  if (!normalized) return '未选择品牌';
  if (normalized === 'atlas') return 'atlas';
  if (normalized === 'meadow') return 'meadow';
  if (normalized === 'harbor') return 'harbor';
  if (normalized === 'summit') return 'summit';
  if (normalized === 'riviera') return 'riviera';
  if (normalized === 'lumen') return 'lumen';
  return normalized;
};

const generateConversationTitle = (content: string) => {
  const normalized = content.replace(/\s+/g, ' ').replace(/[。！？!?]+$/g, '').trim();
  if (!normalized) return '新对话';
  return normalized.length > 22 ? `${normalized.slice(0, 22)}...` : normalized;
};

const toConversationPreview = (content: string) => {
  const normalized = content.replace(/\s+/g, ' ').trim();
  return normalized || '暂无对话内容';
};

const createDraftConversation = (): ConversationState => ({
  id: `draft-${Date.now()}`,
  title: '新对话',
  preview: '暂无对话内容',
  updatedAt: '',
  source: 'current_context',
  activityState: 'empty',
  messages: [],
  proposedActions: [],
  sessionId: `assistant-session-${Date.now()}`,
  brand: undefined,
  agentBindingId: undefined,
  syncStatus: 'draft',
  pinned: false,
  toolCalls: {},
  toolResults: {},
  executionState: 'idle',
});

const buildAttachmentSummary = (attachments: DraftAttachment[]) => attachments.map((item) => item.name).join('、');

const buildConversationTitle = (content: string, attachments: DraftAttachment[]) => {
  const normalized = content.trim();
  if (normalized) return generateConversationTitle(normalized);
  const attachmentSummary = buildAttachmentSummary(attachments);
  return attachmentSummary ? generateConversationTitle(attachmentSummary) : '新对话';
};

const toMessageAttachments = (attachments: DraftAttachment[]): AssistantMessageAttachment[] => attachments.map((item) => ({
  id: item.id,
  type: item.type,
  name: item.name,
  size: item.size,
  mimeType: item.mimeType,
  url: item.url,
}));

const pickPreferredBinding = (
  bindings: AssistantAvailableBinding[],
  currentBindingId: number | null,
) => {
  if (!bindings.length) return null;
  const currentBinding = currentBindingId === null
    ? null
    : bindings.find((item) => item.agentBindingId === currentBindingId) || null;
  if (currentBinding) return currentBinding;
  return bindings.length === 1 ? bindings[0] : null;
};

const buildConversationFromSession = (record: AssistantSessionRecord): ConversationState => ({
  id: `session-${record.id}`,
  sessionMetaId: record.id,
  title: record.title,
  preview: record.preview,
  updatedAt: record.updatedAt ? dayjs(record.updatedAt).format('YYYY-MM-DD HH:mm') : '',
  source: 'current_context',
  activityState: record.messages.length ? 'active' : 'empty',
  messages: record.messages,
  proposedActions: record.proposedActions,
  sessionId: record.sessionId,
  brand: record.brand || undefined,
  agentBindingId: record.agentBindingId ?? undefined,
  syncStatus: 'synced',
  pinned: record.pinned,
  toolCalls: Object.fromEntries(record.messages.filter((item) => item.toolCalls?.length).map((item) => [item.id, item.toolCalls || []])),
  toolResults: Object.fromEntries(record.messages.filter((item) => item.toolResults?.length).map((item) => [item.id, item.toolResults || []])),
  executionState: 'done',
  parentSessionId: record.parentSessionId,
});

const sortConversations = (items: ConversationState[]) => [...items].sort((left, right) => {
  if (left.pinned !== right.pinned) return left.pinned ? -1 : 1;
  if (!left.updatedAt && !right.updatedAt) return right.id.localeCompare(left.id);
  if (!left.updatedAt) return -1;
  if (!right.updatedAt) return 1;
  return dayjs(right.updatedAt).valueOf() - dayjs(left.updatedAt).valueOf();
});

const getConversationStatusText = (conversation: ConversationState) => {
  if (conversation.proposedActions.some((item) => item.status !== 'executed' && item.status !== 'cancelled')) {
    return '待确认';
  }
  if (conversation.activityState === 'empty') {
    return '草稿';
  }
  return '已同步';
};

const getActionStatusText = (action: AssistantProposedAction) => {
  if (action.status === 'executed') return '已执行';
  if (action.status === 'cancelled') return '已取消';
  if (action.riskLevel === 'high') return '高风险';
  if (action.riskLevel === 'medium') return '中风险';
  if (action.riskLevel === 'low') return '低风险';
  return '待确认';
};


const buildMessagePrimaryVersion = (
  message: AssistantContextMessage,
  fallbackToolCalls: AssistantStreamToolCall[] = [],
  fallbackToolResults: AssistantToolResult[] = [],
): AssistantMessageVersion => ({
  id: `${message.id}-current`,
  title: message.title,
  content: message.content,
  createdAt: message.createdAt,
  toolCalls: message.toolCalls || fallbackToolCalls,
  toolResults: message.toolResults || fallbackToolResults,
});

const getMessageVersions = (
  message: AssistantContextMessage,
  fallbackToolCalls: AssistantStreamToolCall[] = [],
  fallbackToolResults: AssistantToolResult[] = [],
) => {
  const versions = message.versions?.length
    ? message.versions
    : [buildMessagePrimaryVersion(message, fallbackToolCalls, fallbackToolResults)];
  return versions.map((item, index) => ({
    ...item,
    id: item.id || `${message.id}-version-${index + 1}`,
    toolCalls: item.toolCalls || [],
    toolResults: item.toolResults || [],
  }));
};
export {
  brandLabel,
  buildAttachmentSummary,
  buildConversationFromSession,
  buildConversationTitle,
  createDraftConversation,
  getActionStatusText,
  getConversationStatusText,
  getMessageVersions,
  pickPreferredBinding,
  sortConversations,
  toConversationPreview,
  toMessageAttachments,
};
