import React from 'react';
import useMobile from '@/shared/hooks/useMobile';
import { AssistantComposer } from './components/AssistantComposer';
import { ConversationSidebar } from './components/ConversationSidebar';
import { MessageStream } from './components/MessageStream';
import { useConversationController } from './hooks/useConversationController';

const SmartAssistant: React.FC = () => {
  const isMobile = useMobile();
  const controller = useConversationController();

  return (
    <section aria-label="智能运营助理页面" className="ops-page ops-smart-assistant-page">
      <div className={`ops-smart-assistant-workspace${isMobile ? ' ops-smart-assistant-workspace--mobile' : ''}`}>
        <ConversationSidebar
          activeConversationId={controller.activeConversationId}
          handleCreateConversation={controller.handleCreateConversation}
          handleDeleteConversation={controller.handleDeleteConversation}
          handleRenameCommit={controller.handleRenameCommit}
          handleRenameStart={controller.handleRenameStart}
          handleTogglePinned={controller.handleTogglePinned}
          exportConversation={controller.exportConversation}
          renameDraft={controller.renameDraft}
          renamingId={controller.renamingId}
          searchKeyword={controller.searchKeyword}
          setActiveConversationId={controller.setActiveConversationId}
          setRenameDraft={controller.setRenameDraft}
          setSearchKeyword={controller.setSearchKeyword}
          setSelectedBindingId={controller.setSelectedBindingId}
          visibleConversations={controller.visibleConversations}
        />

        <section className="ops-smart-assistant-detail-pane">
          <MessageStream
            activeConversation={controller.activeConversation}
            detailScrollRef={controller.detailScrollRef}
            handleCopyMessage={controller.handleCopyMessage}
            loading={controller.loading}
            messageVersionIndexMap={controller.messageVersionIndexMap}
            scrollToBottom={controller.scrollToBottom}
            sendMessage={controller.sendMessage}
            setMessageVersionIndexMap={controller.setMessageVersionIndexMap}
            showScrollButton={controller.showScrollButton}
            updateScrollState={controller.updateScrollState}
            handleBranchMessage={controller.handleBranchMessage}
          />

          <AssistantComposer
            activeConversation={controller.activeConversation}
            attachments={controller.attachments}
            availableBindings={controller.availableBindings}
            draft={controller.draft}
            fileInputRef={controller.fileInputRef}
            handleAttachFiles={controller.handleAttachFiles}
            handleCancelAction={controller.handleCancelAction}
            handleConfirmAction={controller.handleConfirmAction}
            handleSendMessage={controller.handleSendMessage}
            runtimeDisplayName={controller.runtimeDisplayName}
            selectedBindingId={controller.selectedBindingId}
            sending={controller.sending}
            setAttachments={controller.setAttachments}
            setDraft={controller.setDraft}
            setSelectedBindingId={controller.setSelectedBindingId}
            stopGeneration={controller.stopGeneration}
          />
        </section>
      </div>
    </section>
  );
};

export default SmartAssistant;
