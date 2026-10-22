import React from 'react'
import { Button, Empty, Flex, Input, Space, Spin, Tag, Typography } from 'antd'
import { SendOutlined } from '@ant-design/icons'

import MarkdownContent from '@/shared/ui/MarkdownContent'
import type { AssistantProposedAction, TopQueryTask } from '@/shared/types'

import type { BrandAssistantSessionModel } from './hooks/useBrandAssistantSession'

const { Paragraph, Text } = Typography

type BrandAssistantDrawerContentProps = {
  brandLabel: string
  onClose: () => void
  session: BrandAssistantSessionModel
  task?: TopQueryTask | null
}

const actionRiskColor = (action: AssistantProposedAction) => (
  action.riskLevel === 'high' ? 'red' : action.riskLevel === 'medium' ? 'orange' : 'blue'
)

export const BrandAssistantDrawerContent: React.FC<BrandAssistantDrawerContentProps> = ({
  brandLabel,
  onClose,
  session,
  task,
}) => {
  const {
    contextMeta,
    displayedRecentMessages,
    draft,
    handleCancelAction,
    handleConfirmAction,
    handleSend,
    loading,
    messages,
    promptLeadText,
    proposedActions,
    sending,
    setDraft,
    suggestedPrompts,
  } = session

  return (
    <div className="ops-brand-assistant-shell">
      <div className="ops-brand-assistant-shell__scroll">
        <div className="ops-brand-assistant-context-card">
          <Flex justify="space-between" align="flex-start" gap={12}>
            <div>
              <div className="ops-brand-assistant-context-card__title">{brandLabel}</div>
              <div className="ops-brand-assistant-context-card__query">{task?.queryText || '当前品牌总览'}</div>
            </div>
          </Flex>
          <div className="ops-brand-assistant-context-card__meta">
            {contextMeta.map((item) => <span key={item.label}>{item.label} {item.value}</span>)}
          </div>
        </div>

        <div className="ops-brand-assistant-prompt-group">
          <div className="ops-brand-assistant-prompt-intro">
            <div className="ops-brand-assistant-prompt-intro__lead">{promptLeadText}</div>
            <div className="ops-brand-assistant-prompt-intro__helper">你可以尝试这样问我：</div>
          </div>
          <div className="ops-brand-assistant-prompt-list">
            {suggestedPrompts.length ? suggestedPrompts.map((prompt) => (
              <button
                key={prompt}
                type="button"
                className="ops-clickable ops-brand-assistant-prompt-chip"
                aria-label={prompt}
                onClick={() => { void handleSend(prompt) }}
              >
                {prompt}
              </button>
            )) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无快捷提问" />
            )}
          </div>
        </div>

        {displayedRecentMessages.length ? (
          <div className="ops-brand-assistant-recent-group">
            <div className="ops-brand-assistant-section-title">专家团队动态</div>
            <div className="ops-brand-assistant-recent-list">
              {displayedRecentMessages.slice(0, 3).map((item) => (
                <div key={item.id} className="ops-brand-assistant-recent-card">
                  <Text strong>{item.title}</Text>
                  <Paragraph ellipsis={{ rows: 2, tooltip: item.content }} style={{ marginBottom: 0 }}>
                    {item.content}
                  </Paragraph>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        <div className="ops-brand-assistant-thread">
          <div className="ops-brand-assistant-section-title">会话</div>
          <div className="ops-brand-assistant-message-list">
            {loading ? (
              <div className="ops-brand-assistant-loading">
                <Spin size="small" />
              </div>
            ) : messages.length ? messages.map((item) => {
              const isOperator = item.role === 'operator'
              return (
                <div key={item.id} className={`ops-brand-assistant-message${isOperator ? ' ops-brand-assistant-message--operator' : ''}`}>
                  <div className="ops-brand-assistant-message__head">
                    <Text strong>{isOperator ? '运营人员' : 'AI运营领队'}</Text>
                    <Text type="secondary">{item.createdAt}</Text>
                  </div>
                  <MarkdownContent content={item.content} className="ops-brand-assistant-message__content" />
                </div>
              )
            }) : (
              <div className="ops-brand-assistant-empty">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="从快捷提问开始，或直接输入你的问题" />
              </div>
            )}
          </div>
        </div>

        {proposedActions.length ? (
          <div className="ops-brand-assistant-action-group">
            <div className="ops-brand-assistant-section-title">待确认动作</div>
            <div className="ops-brand-assistant-action-list">
              {proposedActions.map((action, index) => (
                <div key={`${action.actionType}-${action.target || index}`} className="ops-brand-assistant-action-card">
                  <Space size={6} wrap>
                    <Tag color={actionRiskColor(action)}>{action.riskLevel || '待确认'}</Tag>
                    <Text strong>{action.actionType}</Text>
                    {action.target ? <Text code>{action.target}</Text> : null}
                  </Space>
                  {action.reason ? <div className="ops-brand-assistant-action-card__reason">{action.reason}</div> : null}
                  <div className="ops-brand-assistant-action-card__actions">
                    <Button size="small" type="primary" onClick={() => { void handleConfirmAction(action) }}>确认执行</Button>
                    <Button size="small" onClick={() => { void handleCancelAction(action) }}>取消</Button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </div>

      <div className="ops-brand-assistant-composer">
        <Input.TextArea
          aria-label="输入智能助理消息"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={task?.queryText ? `继续追问「${task.queryText}」的治理情况` : '请输入你的问题'}
          autoSize={{ minRows: 2, maxRows: 6 }}
          onPressEnter={(event) => {
            if (!event.shiftKey) {
              event.preventDefault()
              void handleSend()
            }
          }}
        />
        <div className="ops-brand-assistant-composer__bar">
          <Text type="secondary">AI运营领队</Text>
          <div className="ops-brand-assistant-composer__actions">
            <Button onClick={onClose}>收起</Button>
            <Button type="primary" icon={<SendOutlined />} loading={sending} onClick={() => { void handleSend() }}>
              发送
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
