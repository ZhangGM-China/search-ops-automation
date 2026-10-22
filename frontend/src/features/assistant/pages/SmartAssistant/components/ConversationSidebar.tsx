import React from 'react';
import { Button, Dropdown, Input } from 'antd';
import type { MenuProps } from 'antd';
import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EllipsisOutlined,
  PlusOutlined,
  PushpinFilled,
  PushpinOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { type ConversationState } from '../conversationModel';

type ConversationSidebarProps = {
  activeConversationId: string | null;
  handleCreateConversation: () => void;
  handleDeleteConversation: (conversationId: string) => Promise<void>;
  handleRenameCommit: () => Promise<void>;
  handleRenameStart: (conversation: ConversationState) => void;
  handleTogglePinned: (conversationId: string) => Promise<void>;
  exportConversation: () => void;
  renameDraft: string;
  renamingId: string | null;
  searchKeyword: string;
  setActiveConversationId: React.Dispatch<React.SetStateAction<string | null>>;
  setRenameDraft: React.Dispatch<React.SetStateAction<string>>;
  setSearchKeyword: React.Dispatch<React.SetStateAction<string>>;
  setSelectedBindingId: React.Dispatch<React.SetStateAction<number | null>>;
  visibleConversations: ConversationState[];
};

export const ConversationSidebar: React.FC<ConversationSidebarProps> = ({
  activeConversationId,
  handleCreateConversation,
  handleDeleteConversation,
  handleRenameCommit,
  handleRenameStart,
  handleTogglePinned,
  exportConversation,
  renameDraft,
  renamingId,
  searchKeyword,
  setActiveConversationId,
  setRenameDraft,
  setSearchKeyword,
  setSelectedBindingId,
  visibleConversations,
}) => {
  const renderConversationRowMenu = (conversation: ConversationState): MenuProps => ({
    items: [
      {
        key: 'pin',
        label: conversation.pinned ? '取消置顶' : '置顶',
        icon: conversation.pinned ? <PushpinFilled /> : <PushpinOutlined />,
      },
      {
        key: 'rename',
        label: '重命名',
        icon: <EditOutlined />,
      },
      ...(conversation.id === activeConversationId ? [{
        key: 'export',
        label: '导出',
        icon: <DownloadOutlined />,
      }] : []),
      {
        key: 'delete',
        label: '删除',
        icon: <DeleteOutlined />,
        danger: true,
      },
    ],
    onClick: ({ key, domEvent }) => {
      domEvent.stopPropagation();
      if (key === 'pin') {
        void handleTogglePinned(conversation.id);
      } else if (key === 'rename') {
        handleRenameStart(conversation);
      } else if (key === 'export') {
        exportConversation();
      } else if (key === 'delete') {
        void handleDeleteConversation(conversation.id);
      }
    },
  });

  return (
        <aside className="ops-smart-assistant-list-pane">
          <div className="ops-smart-assistant-list-scroll">
            <div className="ops-smart-assistant-sidebar-top">
              <div className="ops-smart-assistant-toolbox" aria-label="会话工具栏">
                <button type="button" className="ops-clickable ops-smart-assistant-toolbox__action" onClick={handleCreateConversation}>
                  <PlusOutlined />
                  <span>新对话</span>
                </button>
                <label className="ops-smart-assistant-toolbox__search">
                  <SearchOutlined />
                  <input
                    aria-label="搜索对话"
                    placeholder="搜索"
                    value={searchKeyword}
                    onChange={(event) => setSearchKeyword(event.target.value)}
                  />
                </label>
              </div>
            </div>
            <div className="ops-smart-assistant-list-group">
              {visibleConversations.length ? visibleConversations.map((item) => (
                <div
                  key={item.id}
                  role="button"
                  tabIndex={0}
                  className={`ops-clickable ops-smart-assistant-list-item${item.id === activeConversationId ? ' ops-smart-assistant-list-item--active' : ''}`}
                  aria-label={`打开历史对话 ${item.title}`}
                  aria-current={item.id === activeConversationId ? 'page' : undefined}
                  onClick={() => {
                    setActiveConversationId(item.id);
                    if (item.agentBindingId) setSelectedBindingId(item.agentBindingId);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      setActiveConversationId(item.id);
                      if (item.agentBindingId) setSelectedBindingId(item.agentBindingId);
                    }
                  }}
                >
                  <div className="ops-smart-assistant-list-item__header">
                    <div className="ops-smart-assistant-list-item__main">
                      {renamingId === item.id ? (
                        <Input
                          size="small"
                          aria-label="编辑对话标题"
                          value={renameDraft}
                          autoFocus
                          onClick={(event) => event.stopPropagation()}
                          onChange={(event) => setRenameDraft(event.target.value)}
                          onBlur={handleRenameCommit}
                          onPressEnter={handleRenameCommit}
                        />
                      ) : (
                        <div className="ops-smart-assistant-list-item__title">
                          <span className="ops-smart-assistant-list-item__title-text">{item.title}</span>
                        </div>
                      )}
                    </div>
                    <Dropdown menu={renderConversationRowMenu(item)} trigger={['click']} placement="bottomRight">
                      <Button
                        type="text"
                        size="small"
                        aria-label={`打开对话操作 ${item.title}`}
                        className="ops-smart-assistant-list-item__actions"
                        icon={<EllipsisOutlined />}
                        onClick={(event) => event.stopPropagation()}
                      />
                    </Dropdown>
                  </div>
                </div>
              )) : (
                <div className="ops-smart-assistant-list-empty">暂无对话</div>
              )}
            </div>
          </div>
        </aside>
  );
};
