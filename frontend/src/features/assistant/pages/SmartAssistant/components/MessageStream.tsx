import React from 'react';
import { Button, Space, Spin, Tooltip } from 'antd';
import {
  ArrowUpOutlined,
  CopyOutlined,
  FileOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import {
  type ConversationState,
  type SendMessageOptions,
  getMessageVersions,
} from '../conversationModel';
import { renderMarkdownLite } from './AssistantMarkdown';
import { ToolExecutionPanel } from './ToolExecutionPanel';

type MessageStreamProps = {
  activeConversation: ConversationState | null;
  detailScrollRef: React.RefObject<HTMLDivElement | null>;
  handleCopyMessage: (content: string) => Promise<void>;
  loading: boolean;
  messageVersionIndexMap: Record<string, number>;
  scrollToBottom: (behavior?: ScrollBehavior) => void;
  sendMessage: (messageText: string, options?: SendMessageOptions) => Promise<void>;
  setMessageVersionIndexMap: React.Dispatch<React.SetStateAction<Record<string, number>>>;
  showScrollButton: boolean;
  updateScrollState: () => void;
  handleBranchMessage?: (messageId: string) => Promise<void>;
};

export const MessageStream: React.FC<MessageStreamProps> = ({
  activeConversation,
  detailScrollRef,
  handleCopyMessage,
  loading,
  messageVersionIndexMap,
  scrollToBottom,
  sendMessage,
  setMessageVersionIndexMap,
  showScrollButton,
  updateScrollState,
  handleBranchMessage,
}) => {
  return (
        <div className="ops-smart-assistant-conversation">
          <div ref={detailScrollRef} className="ops-smart-assistant-detail-scroll" onScroll={updateScrollState}>
            <div className="ops-smart-assistant-chat-body" aria-label="对话消息流">
              {activeConversation?.executionState && activeConversation.executionState !== 'idle' ? (
                <div className="ops-smart-assistant-chat-status">
                  <span className={`ops-smart-assistant-execution-state ops-smart-assistant-execution-state--${activeConversation.executionState}`} role="status">
                    {activeConversation.executionLabel || ({ thinking: '正在思考', tool_running: '正在执行工具', waiting: '等待 Agent 响应', done: '执行完成', failed: '执行失败', cancelled: '已取消' } as Record<string, string>)[activeConversation.executionState] || activeConversation.executionState}
                  </span>
                </div>
              ) : null}
              <Spin spinning={loading}>
                {activeConversation?.messages.length ? (
                  <div className="ops-smart-assistant-message-list">
                    {activeConversation.messages.map((item) => {
                      const isOperator = item.role === 'operator';
                      const messageToolCalls = activeConversation.toolCalls[item.id] || [];
                      const messageToolResults = activeConversation.toolResults[item.id] || [];
                      const messageVersions = getMessageVersions(item, messageToolCalls, messageToolResults);
                      const defaultVersionIndex = item.activeVersionIndex ?? (messageVersions.length - 1);
                      const selectedVersionIndex = Math.min(
                        Math.max(messageVersionIndexMap[item.id] ?? defaultVersionIndex, 0),
                        messageVersions.length - 1,
                      );
                      const currentVersion = messageVersions[selectedVersionIndex];
                      const messageIndex = activeConversation.messages.findIndex((message) => message.id === item.id);
                      const retrySourceMessage = isOperator
                        ? null
                        : [...activeConversation.messages]
                          .slice(0, messageIndex)
                          .reverse()
                          .find((message) => message.role === 'operator') || null;
                      return (
                        <div key={item.id} className={`ops-smart-assistant-message-row${isOperator ? ' ops-smart-assistant-message-row--operator' : ''}`}>
                          <article className="ops-smart-assistant-message-card" aria-label={isOperator ? '你的消息' : '助理消息'}>
                            <div className="ops-smart-assistant-message-card__actions">
                              <Space size={4}>
                                <Tooltip title="复制">
                                  <Button type="text" size="small" aria-label="复制消息" icon={<CopyOutlined />} onClick={() => { void handleCopyMessage(currentVersion.content); }} />
                                </Tooltip>
                                {!isOperator && retrySourceMessage ? (
                                  <Tooltip title="重试">
                                    <Button
                                      type="text"
                                      size="small"
                                      aria-label="重试消息"
                                      icon={<ReloadOutlined />}
                                      onClick={() => { void sendMessage(retrySourceMessage.content, { retryOfMessageId: item.id }); }}
                                    />
                                  </Tooltip>
                                ) : null}
                                {activeConversation.sessionMetaId && handleBranchMessage ? (
                                  <Tooltip title="从此消息创建分支">
                                    <Button type="text" size="small" aria-label="从此消息创建分支" icon={<FileOutlined />} onClick={() => { void handleBranchMessage(item.id); }} />
                                  </Tooltip>
                                ) : null}
                              </Space>
                            </div>
                            {(currentVersion.toolCalls?.length || currentVersion.toolResults?.length) ? (
                              <ToolExecutionPanel calls={currentVersion.toolCalls || []} results={currentVersion.toolResults || []} />
                            ) : null}
                            <div className="ops-smart-assistant-message-card__body">
                              {renderMarkdownLite(currentVersion.content)}
                            </div>
                            {!isOperator && messageVersions.length > 1 ? (
                              <div className="ops-smart-assistant-message-version-nav">
                                <button
                                  type="button"
                                  className="ops-clickable ops-smart-assistant-message-version-nav__button"
                                  aria-label="查看上一版回复"
                                  disabled={selectedVersionIndex <= 0}
                                  onClick={() => {
                                    setMessageVersionIndexMap((current) => ({
                                      ...current,
                                      [item.id]: Math.max(selectedVersionIndex - 1, 0),
                                    }));
                                  }}
                                >
                                  {'<'}
                                </button>
                                <span className="ops-smart-assistant-message-version-nav__text">
                                  {selectedVersionIndex + 1}/{messageVersions.length}
                                </span>
                                <button
                                  type="button"
                                  className="ops-clickable ops-smart-assistant-message-version-nav__button"
                                  aria-label="查看下一版回复"
                                  disabled={selectedVersionIndex >= messageVersions.length - 1}
                                  onClick={() => {
                                    setMessageVersionIndexMap((current) => ({
                                      ...current,
                                      [item.id]: Math.min(selectedVersionIndex + 1, messageVersions.length - 1),
                                    }));
                                  }}
                                >
                                  {'>'}
                                </button>
                              </div>
                            ) : null}
                            {item.attachments?.length ? (
                              <div className="ops-smart-assistant-message-attachments" aria-label="消息附件">
                                {item.attachments.map((attachment) => (
                                  attachment.type === 'image' && attachment.url ? (
                                    <img
                                      key={attachment.id}
                                      src={attachment.url}
                                      alt={attachment.name}
                                      className="ops-smart-assistant-message-attachments__image"
                                    />
                                  ) : (
                                    <div key={attachment.id} className="ops-smart-assistant-message-attachments__file">
                                      <FileOutlined />
                                      <div className="ops-smart-assistant-message-attachments__file-meta">
                                        <span>{attachment.name}</span>
                                        <small>{Math.max(1, Math.round(attachment.size / 1024))} KB</small>
                                      </div>
                                    </div>
                                  )
                                ))}
                              </div>
                            ) : null}
                          </article>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="ops-smart-assistant-empty">
                    <div className="ops-smart-assistant-empty-card" aria-label="空对话状态">
                      <div className="ops-smart-assistant-empty-card__title">{activeConversation ? '开始这个对话' : '还没有历史对话'}</div>
                      <div className="ops-smart-assistant-empty-card__text">
                        {activeConversation ? '直接在下方输入问题，当前会话会自动开始记录。' : '点击左上角新对话，或者直接在下方输入问题。'}
                      </div>
                    </div>
                  </div>
                )}
              </Spin>
            </div>
            {showScrollButton ? (
              <button type="button" className="ops-clickable ops-smart-assistant-scroll-button" onClick={() => scrollToBottom()}>
                <ArrowUpOutlined rotate={180} />
                <span>回到底部</span>
              </button>
            ) : null}
          </div>
        </div>
  );
};
