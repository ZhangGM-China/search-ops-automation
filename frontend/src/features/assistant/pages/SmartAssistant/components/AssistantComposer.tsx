import React from 'react';
import { Button, Dropdown, Flex, Input, Space, Tag, Tooltip, Typography } from 'antd';
import type { MenuProps } from 'antd';
import {
  ArrowUpOutlined,
  DeleteOutlined,
  FileOutlined,
  PaperClipOutlined,
  StopOutlined,
} from '@ant-design/icons';
import type { AssistantAvailableBinding, AssistantProposedAction } from '@/shared/types';
import {
  type ConversationState,
  type DraftAttachment,
  getActionStatusText,
} from '../conversationModel';

const { Paragraph, Text } = Typography;

type AssistantComposerProps = {
  activeConversation: ConversationState | null;
  attachments: DraftAttachment[];
  availableBindings: AssistantAvailableBinding[];
  draft: string;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  handleAttachFiles: (files: FileList | null) => Promise<void>;
  handleCancelAction: (action: AssistantProposedAction) => Promise<void>;
  handleConfirmAction: (action: AssistantProposedAction) => Promise<void>;
  handleSendMessage: () => Promise<void>;
  runtimeDisplayName: string;
  selectedBindingId: number | null;
  sending: boolean;
  setAttachments: React.Dispatch<React.SetStateAction<DraftAttachment[]>>;
  setDraft: React.Dispatch<React.SetStateAction<string>>;
  setSelectedBindingId: React.Dispatch<React.SetStateAction<number | null>>;
  stopGeneration: () => void;
};

export const AssistantComposer: React.FC<AssistantComposerProps> = ({
  activeConversation,
  attachments,
  availableBindings,
  draft,
  fileInputRef,
  handleAttachFiles,
  handleCancelAction,
  handleConfirmAction,
  handleSendMessage,
  runtimeDisplayName,
  selectedBindingId,
  sending,
  setAttachments,
  setDraft,
  setSelectedBindingId,
  stopGeneration,
}) => {
  const activeBinding = availableBindings.find((item) => (
    item.agentBindingId === (selectedBindingId ?? activeConversation?.agentBindingId)
  )) || null;
  const modelBindings = React.useMemo(() => {
    const uniqueBindings = new Map<string, AssistantAvailableBinding>();
    availableBindings.forEach((item) => {
      const modelName = String(item.modelName || runtimeDisplayName).trim();
      if (modelName && !uniqueBindings.has(modelName)) uniqueBindings.set(modelName, item);
    });
    return [...uniqueBindings.entries()].map(([modelName, binding]) => ({ modelName, binding }));
  }, [availableBindings, runtimeDisplayName]);
  const activeModelName = activeBinding?.modelName || runtimeDisplayName;
  const modelMenuItems: MenuProps['items'] = modelBindings.map(({ modelName, binding }) => ({
    key: String(binding.agentBindingId),
    label: modelName,
  }));

  return (
          <div className="ops-smart-assistant-composer">
            <div className="ops-smart-assistant-composer-shell">
              <Input.TextArea
                aria-label="输入智能运营助理消息"
                className="ops-smart-assistant-composer-input"
                placeholder={`给 ${activeModelName} 发消息`}
                value={draft}
                autoSize={{ minRows: 3, maxRows: 8 }}
                variant="borderless"
                onChange={(event) => setDraft(event.target.value)}
                onPressEnter={(event) => {
                  if (!event.shiftKey) {
                    event.preventDefault();
                    void handleSendMessage();
                  }
                }}
              />
              {attachments.length ? (
                <div className="ops-smart-assistant-attachment-list">
                  {attachments.map((item) => (
                    <div key={item.id} className="ops-smart-assistant-attachment-chip">
                      {item.type === 'image' && item.url ? (
                        <img src={item.url} alt={item.name} className="ops-smart-assistant-attachment-chip__preview" />
                      ) : (
                        <span className="ops-smart-assistant-attachment-chip__icon" aria-hidden="true">
                          <FileOutlined />
                        </span>
                      )}
                      <div className="ops-smart-assistant-attachment-chip__meta">
                        <span>{item.name}</span>
                        <small>{Math.max(1, Math.round(item.size / 1024))} KB</small>
                      </div>
                      <Button
                        type="text"
                        size="small"
                        aria-label={`移除附件 ${item.name}`}
                        icon={<DeleteOutlined />}
                        onClick={() => setAttachments((current) => current.filter((file) => file.id !== item.id))}
                      />
                    </div>
                  ))}
                </div>
              ) : null}
              <div className="ops-smart-assistant-composer-bar">
                <Space size={8}>
                  {modelBindings.length > 1 ? (
                    <Dropdown
                      menu={{
                        items: modelMenuItems,
                        onClick: ({ key }) => {
                          const nextId = Number(key);
                          if (Number.isFinite(nextId)) setSelectedBindingId(nextId);
                        },
                      }}
                      trigger={['click']}
                      placement="topLeft"
                    >
                      <button type="button" className="ops-clickable ops-smart-assistant-model-button" aria-label="切换模型">
                        {activeModelName}
                      </button>
                    </Dropdown>
                  ) : (
                    <span className="ops-smart-assistant-model-button ops-smart-assistant-model-button--static" aria-label="当前模型">
                      {activeModelName}
                    </span>
                  )}
                  <input
                    ref={fileInputRef}
                    aria-label="选择附件"
                    className="ops-smart-assistant-file-input"
                    type="file"
                    multiple
                    onChange={(event) => handleAttachFiles(event.target.files)}
                  />
                  <Tooltip title="添加附件">
                    <Button
                      type="text"
                      shape="circle"
                      aria-label="添加附件"
                      icon={<PaperClipOutlined />}
                      onClick={() => fileInputRef.current?.click()}
                    />
                  </Tooltip>
                </Space>
                {sending ? (
                  <Button
                    shape="circle"
                    aria-label="停止生成"
                    icon={<StopOutlined />}
                    onClick={stopGeneration}
                  />
                ) : (
                  <Button
                    type="primary"
                    shape="circle"
                    aria-label="发送消息"
                    disabled={!draft.trim() && !attachments.length}
                    icon={<ArrowUpOutlined />}
                    onClick={() => { void handleSendMessage(); }}
                  />
                )}
              </div>
            </div>

            {activeConversation?.proposedActions.length ? (
              <div className="ops-smart-assistant-plan-panel" aria-label="待确认治理计划">
                <Flex justify="space-between" align="center" gap={12} wrap>
                  <Text strong>待确认治理计划</Text>
                  <Text type="secondary">未执行</Text>
                </Flex>
                <div className="ops-smart-assistant-plan-list">
                  {activeConversation.proposedActions.map((action, index) => (
                    <div key={`${action.actionType}-${action.target || index}`} className="ops-smart-assistant-plan-item">
                      <Space size={8} wrap>
                        <Tag color={action.status === 'executed' ? 'green' : action.status === 'cancelled' ? 'default' : action.riskLevel === 'high' ? 'red' : action.riskLevel === 'medium' ? 'orange' : 'blue'}>
                          {getActionStatusText(action)}
                        </Tag>
                        <Text strong>{action.actionType}</Text>
                        {action.query ? <Text>{action.query}</Text> : null}
                        {action.target ? <Text code>{action.target}</Text> : null}
                      </Space>
                      {action.reason ? <Paragraph className="ops-smart-assistant-plan-item__reason">{action.reason}</Paragraph> : null}
                      {action.resultSummary ? <Paragraph className="ops-smart-assistant-plan-item__reason">{action.resultSummary}</Paragraph> : null}
                      {action.status === 'executed' || action.status === 'cancelled' ? null : (
                        <Space size={8}>
                          <Button size="small" type="primary" aria-label={`确认执行 ${action.actionType}`} onClick={() => { void handleConfirmAction(action); }}>
                            确认执行
                          </Button>
                          {action.id ? (
                            <Button size="small" aria-label={`取消动作 ${action.actionType}`} onClick={() => { void handleCancelAction(action); }}>
                              取消
                            </Button>
                          ) : null}
                        </Space>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
  );
};
