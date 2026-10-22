import React from 'react'
import { Button, Dropdown, Popconfirm, Space, Tag, Typography } from 'antd'
import { DownOutlined, DownloadOutlined, ReloadOutlined } from '@ant-design/icons'

import ConfigContextActions from '../ConfigContextActions'
import type { ExperimentWorkbenchModel } from './hooks/useExperimentWorkbench'

const statusLabels: Record<string, string> = {
  draft: '草稿',
  scheduled: '已排期',
  running: '运行中',
  ended: '已结束',
  cancelled: '已取消',
}

type ExperimentWorkbenchToolbarProps = {
  brand: string
  workbench: ExperimentWorkbenchModel
}

export const ExperimentWorkbenchToolbar: React.FC<ExperimentWorkbenchToolbarProps> = ({ brand, workbench }) => {
  const {
    canManage,
    currentExperimentId,
    detail,
    exporting,
    handleExport,
    handleRefresh,
    navigateExperiment,
    submitting,
    updateLifecycle,
  } = workbench
  const actions = (
    <Space wrap>
      <Button icon={<ReloadOutlined />} disabled={submitting} onClick={() => void handleRefresh()}>刷新</Button>
      {detail ? (
        <>
          <Button type="primary" icon={<DownloadOutlined />} loading={exporting} disabled={exporting} onClick={() => void handleExport('xlsx')}>导出 Excel</Button>
          <Dropdown
            disabled={exporting}
            menu={{
              items: [{ key: 'csv', label: '导出 CSV' }, { key: 'zip', label: '导出 ZIP' }],
              onClick: ({ key }) => void handleExport(key as 'csv' | 'zip'),
            }}
            trigger={['click']}
          >
            <Button aria-label="更多导出格式" disabled={exporting}>更多 <DownOutlined /></Button>
          </Dropdown>
        </>
      ) : null}
      {detail && ['ended', 'cancelled'].includes(detail.status) ? (
        <Button onClick={() => navigateExperiment(currentExperimentId)}>{currentExperimentId ? '返回当前实验' : '新建实验'}</Button>
      ) : null}
      {detail && ['draft', 'scheduled'].includes(detail.status) && canManage ? (
        <Popconfirm
          title="确认取消实验？"
          description="取消后该实验不会进入运行。"
          okText="确认"
          cancelText="暂不"
          onConfirm={() => void updateLifecycle('cancel')}
        >
          <Button danger loading={submitting} disabled={submitting}>取消实验</Button>
        </Popconfirm>
      ) : null}
      {detail?.status === 'running' && canManage ? (
        <Popconfirm
          title="确认结束实验？"
          description="结束后将停止后续观测与治理。"
          okText="确认"
          cancelText="暂不"
          onConfirm={() => void updateLifecycle('end')}
        >
          <Button danger loading={submitting} disabled={submitting}>结束实验</Button>
        </Popconfirm>
      ) : null}
    </Space>
  )

  return (
    <>
      <ConfigContextActions>{actions}</ConfigContextActions>
      <div className="experiment-toolbar">
        <div>
          <Space wrap>
            <Typography.Text strong>{detail?.experimentName || `${brand.toUpperCase()} A/B Test`}</Typography.Text>
            {detail ? (
              <Tag color={detail.status === 'running' ? 'green' : 'default'}>{statusLabels[detail.status] ?? detail.status}</Tag>
            ) : <Tag>未建立</Tag>}
          </Space>
          <Typography.Text type="secondary" className="experiment-toolbar__subtitle">
            {detail ? `${detail.plannedStartDate || '-'} 至 ${detail.plannedEndDate || '-'}` : '选择 20+20 固定 Query 建立新实验'}
          </Typography.Text>
        </div>
      </div>
    </>
  )
}
