import React from 'react'
import { Alert, Button, Card, DatePicker, Empty, Space, Typography } from 'antd'

import ExperimentDraftBuilder from './ExperimentDraftBuilder'
import ExperimentHistoryList from './ExperimentHistoryList'
import ExperimentReport from './ExperimentReport'
import ExperimentSampleSelector from './ExperimentSampleSelector'
import type { ExperimentWorkbenchModel } from './hooks/useExperimentWorkbench'

type ExperimentWorkbenchContentProps = {
  workbench: ExperimentWorkbenchModel
}

export const ExperimentWorkbenchContent: React.FC<ExperimentWorkbenchContentProps> = ({ workbench }) => {
  const {
    canManage,
    controlQueries,
    detail,
    dirty,
    handleControlQueriesChange,
    handlePlannedEndDateChange,
    handleSaveDraft,
    handleTreatmentQueriesChange,
    historyFilters,
    historyListing,
    inlineError,
    isConfiguration,
    isMobile,
    navigateExperiment,
    overlap,
    plannedEndDate,
    plannedStart,
    report,
    sampleError,
    samplePool,
    setHistoryFilters,
    setInlineError,
    staleSamplePool,
    submitting,
    treatmentQueries,
    updateLifecycle,
  } = workbench
  const historyItems = historyListing.items

  return (
    <>
      {inlineError ? <Alert type="error" showIcon title={inlineError} closable onClose={() => setInlineError('')} /> : null}

      {isConfiguration ? (
        <ExperimentDraftBuilder treatmentCount={treatmentQueries.length} controlCount={controlQueries.length} sampleDataDate={samplePool?.sampleDataDate}>
          {(!detail || detail.status === 'draft') && (sampleError || !samplePool?.items.length) ? (
            <Alert type="warning" showIcon title="样本池不可用" description={sampleError || '请刷新样本池或检查生产数据是否已完成同步。'} />
          ) : null}
          {staleSamplePool ? (
            <Alert type="warning" showIcon title="样本池已更新，请重新保存草稿" description={`草稿基准日 ${detail?.sampleDataDate}，当前样本池 ${samplePool?.sampleDataDate}`} />
          ) : null}
          <div className="experiment-selector-grid">
            <ExperimentSampleSelector
              label="实验组"
              value={treatmentQueries}
              blockedQueries={controlQueries}
              items={samplePool?.items ?? []}
              disabled={!canManage || detail?.status === 'scheduled'}
              onChange={handleTreatmentQueriesChange}
            />
            <ExperimentSampleSelector
              label="对照组"
              value={controlQueries}
              blockedQueries={treatmentQueries}
              items={samplePool?.items ?? []}
              disabled={!canManage || detail?.status === 'scheduled'}
              onChange={handleControlQueriesChange}
            />
          </div>
          <div className="experiment-draft-footer">
            <div>
              <Typography.Text type="secondary">计划周期</Typography.Text>
              <DatePicker
                aria-label="计划结束日期"
                value={plannedEndDate}
                minDate={plannedStart}
                disabled={!canManage || Boolean(detail && detail.status !== 'draft')}
                onChange={handlePlannedEndDateChange}
              />
              <small>默认 7 天，开始日 {plannedStart.format('YYYY-MM-DD')}</small>
            </div>
            {detail?.status === 'scheduled' ? (
              <Typography.Text type="secondary">实验已排期，样本配置只读。</Typography.Text>
            ) : (
              <Space wrap>
                <Button
                  type="primary"
                  loading={submitting}
                  disabled={submitting || !canManage || detail?.status === 'scheduled' || !samplePool?.items.length || overlap.length > 0}
                  onClick={() => void handleSaveDraft()}
                >
                  保存草稿
                </Button>
                <Button
                  loading={submitting}
                  disabled={submitting || !canManage || dirty || staleSamplePool || detail?.status !== 'draft' || treatmentQueries.length !== 20 || controlQueries.length !== 20 || overlap.length > 0}
                  onClick={() => void updateLifecycle('schedule')}
                >
                  确认排期
                </Button>
              </Space>
            )}
          </div>
          {dirty ? <Typography.Text type="warning">请先保存草稿后再排期</Typography.Text> : null}
          {overlap.length ? <Alert type="error" showIcon title="实验组和对照组存在治理等价 Query" description={overlap.join('、')} /> : null}
          {!canManage ? <Alert type="info" showIcon title="当前为只读模式" description="可查看实验和导出数据，建立、排期和结束需要品牌配置管理权限。" /> : null}
        </ExperimentDraftBuilder>
      ) : report ? (
        <ExperimentReport report={report} isMobile={isMobile} />
      ) : <Card><Empty description="该实验暂无可展示的效果数据" /></Card>}

      <ExperimentHistoryList
        items={historyItems}
        activeExperimentId={detail?.id}
        total={historyListing.total ?? historyItems.length}
        page={historyListing.page ?? historyFilters.page}
        pageSize={historyListing.pageSize ?? historyFilters.pageSize}
        filters={historyFilters}
        onChange={setHistoryFilters}
        onOpen={navigateExperiment}
      />
    </>
  )
}
