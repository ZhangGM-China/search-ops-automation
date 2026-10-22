import React from 'react';
import { Button, Card, DatePicker, Empty, Flex, Modal, Skeleton, Space, Tag } from 'antd';
import { LeftOutlined, MenuFoldOutlined, MenuUnfoldOutlined, RightOutlined, RobotOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import QueryDetail from '../../components/QueryDetail';
import QueryDemotionExecutionPanel from '../../components/QueryDetail/QueryDemotionExecutionPanel';
import BrandAssistantDrawer from '../../components/BrandAssistantDrawer';
import { BrandQueryListPane } from './components/BrandQueryListPane';
import { isInvalidatedTask } from './brandDetailUtils';
import { useBrandDetailWorkspace } from './hooks/useBrandDetailWorkspace';
import { isQueryDemotionInteractionCurrent } from './queryDemotion';

const BrandDetail: React.FC = () => {
  const workspace = useBrandDetailWorkspace();
  const {
    activeLoadError,
    assistantDrawerOpen,
    assistantTask,
    brand,
    brandName,
    canExecuteGovernanceActions,
    canGoNext,
    closePeekList,
    currentDate,
    currentRouteKey,
    demotionConflictMessage,
    demotionPreview,
    demotionPreviewError,
    demotionPreviewLoading,
    demotionPreviewOpen,
    demotionPreviewRouteKey,
    detail,
    detailLoading,
    detailOffset,
    detailTitle,
    effectiveDate,
    events,
    executingTaskIds,
    filter,
    handleFilterChange,
    handleCloseDemotionPreview,
    handleConfirmDemotion,
    handleExecuteTaskActions,
    handleExecuteKGPlan,
    handleCancelGovernancePlan,
    handleBlacklistProduct,
    handleBatchBlacklistProducts,
    handleCorrectProduct,
    handleRollbackAction,
    handleSelectTask,
    hasAnyQuery,
    isDesktop,
    isMobile,
    listCollapsed,
    listPaneRef,
    listPeekOpen,
    listPinnedOpen,
    listVisible,
    loading,
    messageContextHolder,
    navigate,
    openRollbackCandidate,
    openPeekList,
    queries,
    queryData,
    queryListError,
    queryListLoading,
    rollbackCandidate,
    rollbackCandidateRouteKey,
    rollingBackActionId,
    routeContentLoading,
    selectedTask,
    selectedTaskId,
    setAssistantDrawerOpen,
    retryQueryList,
    loadMoreQueries,
    setListCollapsed,
    setRollbackCandidate,
    setRollbackCandidateRouteKey,
    shiftDate,
    summary,
    switchDate,
    todayDate,
    toggleListCollapsed,
    unlockPeekOnRailExit,
    workspaceStyle,
    workflowSteps,
    visibleCheckKeys,
  } = workspace;

  const desktopRail = isDesktop ? (
    <div
      className={`ops-brand-detail-overlay-rail-hitbox${listCollapsed ? ' ops-brand-detail-overlay-rail-hitbox--collapsed' : ''}`}
      onPointerEnter={openPeekList}
      onPointerLeave={(event) => {
        unlockPeekOnRailExit();
        const nextTarget = event.relatedTarget;
        if (nextTarget instanceof Node && listPaneRef.current?.contains(nextTarget)) return;
        closePeekList();
      }}
    >
      <div
        className={`ops-brand-detail-overlay-rail${listCollapsed ? ' ops-brand-detail-overlay-rail--collapsed' : ''}`}
        aria-label={`${brandName} 工作台导航`}
      >
        <Button
          type="text"
          size="small"
          className="ops-brand-detail-overlay-rail__icon-button"
          icon={<LeftOutlined />}
          onClick={() => navigate(`/ops-dashboard?date=${encodeURIComponent(effectiveDate)}`)}
          aria-label="返回品牌总览"
        />
        <Button
          type="text"
          size="small"
          className="ops-brand-detail-overlay-rail__icon-button"
          icon={listCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
          onClick={toggleListCollapsed}
          aria-label={listCollapsed ? '展开 Query 列表' : '收起 Query 列表'}
        />
      </div>
    </div>
  ) : undefined;
  const detailHeader = (detailTitle || detailLoading || loading) ? (
    <div className={`ops-query-detail-title-bar ops-query-detail-title-bar--brand-detail${isDesktop && detailOffset === 0 ? ' ops-query-detail-title-bar--prefix-inset' : ''}`}>
      <div className="ops-query-detail-title-bar__identity">
        {isMobile && listCollapsed ? (
          <Button
            type="text"
            size="small"
            icon={<MenuUnfoldOutlined />}
            aria-label="返回 Query 列表"
            onClick={() => setListCollapsed(false)}
          />
        ) : null}
        <div className="ops-query-detail-title-bar__name">{detailTitle}</div>
        {isInvalidatedTask(selectedTask) ? <Tag>已失效</Tag> : null}
      </div>
      <div className="ops-query-detail-title-bar__actions">
        <Button
          type="text"
          size="small"
          className="ops-query-detail-inline-action ops-query-detail-assistant-trigger"
          icon={<RobotOutlined />}
          aria-label="打开 AI运营领队"
          onClick={() => setAssistantDrawerOpen(true)}
        />
      </div>
    </div>
  ) : null;

  if (loading || routeContentLoading) {
    return (
      <div className="ops-page ops-brand-detail-page">
        {messageContextHolder}
        <div
          className={`ops-brand-detail-workspace${isMobile ? ' ops-brand-detail-workspace--mobile' : ''}${isDesktop ? ' ops-brand-detail-workspace--desktop' : ''}${listCollapsed ? ' ops-brand-detail-workspace--collapsed' : ''}${listPeekOpen ? ' ops-brand-detail-workspace--peek-open' : ''}${listPinnedOpen ? ' ops-brand-detail-workspace--list-pinned' : ''}`}
          style={workspaceStyle}
        >
          {desktopRail ? (
            <div className="ops-brand-detail-overlay-rail-anchor">
              {desktopRail}
            </div>
          ) : null}
          {isDesktop ? (
            <aside className="ops-brand-detail-list-pane ops-brand-detail-list-pane--visible">
              <div className="ops-brand-detail-list-shell">
                <div className="ops-brand-detail-list-toolbar" aria-label={`${brandName} 工作台导航`}>
                  <div className="ops-brand-detail-nav-controls" />
                  <Space.Compact className="ops-brand-date-switcher">
                    <Button type="text" size="small" icon={<LeftOutlined />} aria-label="前一天" onClick={() => shiftDate(-1)} />
                    <DatePicker
                      allowClear={false}
                      inputReadOnly
                      disabledDate={(value) => Boolean(value && value.format('YYYY-MM-DD') > todayDate)}
                      value={currentDate ? dayjs(currentDate) : undefined}
                      onChange={(value) => switchDate(value?.format('YYYY-MM-DD') || todayDate)}
                      aria-label="选择监测日期"
                    />
                    <Button type="text" size="small" icon={<RightOutlined />} aria-label="后一天" disabled={!canGoNext} onClick={() => shiftDate(1)} />
                  </Space.Compact>
                </div>
                <div className="ops-query-filter-panel" aria-hidden="true">
                  <Skeleton.Button active block style={{ height: 68, borderRadius: 12 }} />
                </div>
                <div className="ops-brand-detail-list-scroll" aria-hidden="true">
                  <Flex vertical gap={0} style={{ minHeight: '100%' }}>
                    {Array.from({ length: 5 }).map((_, index) => (
                      <div
                        key={index}
                        style={{
                          padding: 16,
                          borderTop: index === 0 ? 0 : '1px solid #d6dee9',
                          background: index === 0 ? '#dbe6f4' : '#f3f6f9',
                        }}
                      >
                        <Skeleton active title={{ width: '48%' }} paragraph={{ rows: 2, width: ['72%', '92%'] }} />
                      </div>
                    ))}
                  </Flex>
                </div>
              </div>
            </aside>
          ) : null}
          <section className={`ops-brand-detail-detail-pane${detailOffset === 0 ? ' ops-brand-detail-detail-pane--flush' : ''}`}>
            {detailHeader}
            <div className="ops-brand-detail-detail-scroll">
              <div className="ops-surface" style={{ padding: 16 }}><Skeleton active paragraph={{ rows: 8 }} /></div>
            </div>
          </section>
        </div>
      </div>
    );
  }

  if (activeLoadError) {
    return <>{messageContextHolder}<Card size="small"><Empty description={activeLoadError} /></Card></>;
  }

  if (!summary || !queryData) {
    return <>{messageContextHolder}<Card size="small"><Empty description="暂无品牌数据" /></Card></>;
  }

  return (
    <div className="ops-page ops-brand-detail-page">
      {messageContextHolder}
      <div
        className={`ops-brand-detail-workspace${isMobile ? ' ops-brand-detail-workspace--mobile' : ''}${isDesktop ? ' ops-brand-detail-workspace--desktop' : ''}${listCollapsed ? ' ops-brand-detail-workspace--collapsed' : ''}${listPeekOpen ? ' ops-brand-detail-workspace--peek-open' : ''}${listPinnedOpen ? ' ops-brand-detail-workspace--list-pinned' : ''}`}
        style={workspaceStyle}
      >
        {desktopRail ? (
          <div className="ops-brand-detail-overlay-rail-anchor">
            {desktopRail}
          </div>
        ) : null}
        <BrandQueryListPane
          brandName={brandName}
          canGoNext={canGoNext}
          closePeekList={closePeekList}
          currentDate={currentDate}
          filter={filter}
          isDesktop={isDesktop}
          isMobile={isMobile}
          listCollapsed={listCollapsed}
          listPaneRef={listPaneRef}
          listPeekOpen={listPeekOpen}
          listVisible={listVisible}
          onNavigateOverview={() => navigate(`/ops-dashboard?date=${encodeURIComponent(effectiveDate)}`)}
          onSelectTask={handleSelectTask}
          openPeekList={openPeekList}
          queries={queries}
          queryListError={queryListError}
          queryListLoading={queryListLoading}
          retryQueryList={retryQueryList}
          loadMoreQueries={loadMoreQueries}
          selectedTaskId={selectedTaskId}
          setFilter={handleFilterChange}
          shiftDate={shiftDate}
          summary={summary}
          switchDate={switchDate}
          todayDate={todayDate}
          toggleListCollapsed={toggleListCollapsed}
          workflowSteps={workflowSteps}
          visibleCheckKeys={visibleCheckKeys}
        />
        <section className={`ops-brand-detail-detail-pane${detailOffset === 0 ? ' ops-brand-detail-detail-pane--flush' : ''}`}>
          {detailHeader}
          <div className="ops-brand-detail-detail-scroll">
            {!hasAnyQuery ? (
              <div className="ops-brand-detail-empty-state">
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={queryData?.filter && queryData.filter !== 'all' && queryData.total === 0
                    ? '当前筛选下暂无 Query'
                    : queryData?.reason === 'no_candidates'
                      ? '当日无符合条件的 Query，已正常跳过治理'
                      : '当前日期暂无 Query 治理数据'}
                />
              </div>
            ) : detailLoading ? <div className="ops-surface" style={{ padding: 16 }}><Skeleton active paragraph={{ rows: 8 }} /></div> : (
              <QueryDetail
                detail={detail}
                events={events}
                onRollbackAction={canExecuteGovernanceActions ? openRollbackCandidate : undefined}
                rollingBackActionId={rollingBackActionId}
                onExecuteTaskActions={canExecuteGovernanceActions ? handleExecuteTaskActions : undefined}
                onExecuteKGPlan={canExecuteGovernanceActions ? handleExecuteKGPlan : undefined}
                onCancelPlan={canExecuteGovernanceActions ? handleCancelGovernancePlan : undefined}
                onBlacklistProduct={canExecuteGovernanceActions ? handleBlacklistProduct : undefined}
                onBatchBlacklistProducts={canExecuteGovernanceActions ? handleBatchBlacklistProducts : undefined}
                onCorrectProduct={canExecuteGovernanceActions ? handleCorrectProduct : undefined}
                taskActionPreviewLoading={demotionPreviewLoading}
                taskActionExecuting={Boolean(selectedTaskId && executingTaskIds.has(selectedTaskId))}
                hideTitleBar
              />
            )}
          </div>
        </section>
      </div>
      <QueryDemotionExecutionPanel
        open={demotionPreviewOpen && isQueryDemotionInteractionCurrent(demotionPreviewRouteKey, currentRouteKey)}
        preview={isQueryDemotionInteractionCurrent(demotionPreviewRouteKey, currentRouteKey) ? demotionPreview : null}
        loading={demotionPreviewLoading}
        executing={Boolean(demotionPreview?.taskId && executingTaskIds.has(demotionPreview.taskId))}
        error={demotionPreviewError}
        conflictMessage={demotionConflictMessage}
        onConfirm={handleConfirmDemotion}
        onClose={handleCloseDemotionPreview}
      />
      <Modal
        open={Boolean(
          rollbackCandidate
          && isQueryDemotionInteractionCurrent(rollbackCandidateRouteKey, currentRouteKey)
        )}
        rootClassName="ops-query-demotion-modal"
        title="确认回滚 Query 降桶"
        okText="确认回滚"
        okButtonProps={{ danger: true }}
        confirmLoading={Boolean(rollbackCandidate && rollingBackActionId === rollbackCandidate.id)}
        cancelButtonProps={{ disabled: Boolean(rollbackCandidate && rollingBackActionId === rollbackCandidate.id) }}
        closable={!rollbackCandidate || rollingBackActionId !== rollbackCandidate.id}
        mask={{ closable: !rollbackCandidate || rollingBackActionId !== rollbackCandidate.id }}
        keyboard={!rollbackCandidate || rollingBackActionId !== rollbackCandidate.id}
        onOk={() => {
          if (rollbackCandidate) void handleRollbackAction(rollbackCandidate);
        }}
        onCancel={() => {
          if (!rollbackCandidate || rollingBackActionId !== rollbackCandidate.id) {
            setRollbackCandidate(null);
            setRollbackCandidateRouteKey(null);
          }
        }}
        destroyOnHidden
      >
        <p>回滚会把当前 Query 的降桶配置恢复到执行前完整快照。</p>
        <p>系统仍会在写入后执行权威配置回读；只有确认恢复一致才会标记为已回滚。</p>
      </Modal>
      <BrandAssistantDrawer
        open={assistantDrawerOpen}
        onClose={() => setAssistantDrawerOpen(false)}
        brand={brand}
        brandLabel={brandName}
        date={effectiveDate}
        task={assistantTask}
        detail={detail}
      />
    </div>
  );
};

export default BrandDetail;
