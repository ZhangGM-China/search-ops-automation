import React from 'react'
import { Button, Result, Spin } from 'antd'

import { ExperimentWorkbenchContent } from './ExperimentWorkbenchContent'
import { ExperimentWorkbenchToolbar } from './ExperimentWorkbenchToolbar'
import {
  type ExperimentWorkbenchProps,
  useExperimentWorkbench,
} from './hooks/useExperimentWorkbench'

const ExperimentWorkbench: React.FC<ExperimentWorkbenchProps> = (props) => {
  const workbench = useExperimentWorkbench(props)
  const {
    loading,
    messageContextHolder,
    permissionDenied,
    requestError,
    retryCurrentReport,
  } = workbench

  if (loading) {
    return (
      <>
        {messageContextHolder}
        <div className="experiment-center-state"><Spin /><span>正在加载实验数据</span></div>
      </>
    )
  }

  if (permissionDenied) {
    return (
      <>
        {messageContextHolder}
        <Result status="403" title="暂无该品牌实验的查看权限" subTitle="请联系管理员分配品牌权限。" />
      </>
    )
  }

  if (requestError) {
    return (
      <>
        {messageContextHolder}
        <Result
          status="error"
          title="实验数据加载失败"
          subTitle={requestError}
          extra={<Button onClick={retryCurrentReport}>重试</Button>}
        />
      </>
    )
  }

  return (
    <>
      {messageContextHolder}
      <div className="experiment-workbench__content">
        <ExperimentWorkbenchToolbar brand={props.brand} workbench={workbench} />
        <ExperimentWorkbenchContent workbench={workbench} />
      </div>
    </>
  )
}

export default ExperimentWorkbench
