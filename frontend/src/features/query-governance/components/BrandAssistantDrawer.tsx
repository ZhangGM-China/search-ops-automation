import React from 'react'
import { Drawer } from 'antd'
import { RobotOutlined } from '@ant-design/icons'

import useMobile from '@/shared/hooks/useMobile'
import type { TopQueryDetailData, TopQueryTask } from '@/shared/types'

import { BrandAssistantDrawerContent } from './BrandAssistantDrawerContent'
import { useBrandAssistantSession } from './hooks/useBrandAssistantSession'

type BrandAssistantDrawerProps = {
  open: boolean
  onClose: () => void
  brand?: string
  brandLabel: string
  date?: string
  task?: TopQueryTask | null
  detail?: TopQueryDetailData | null
  pageKey?: 'brand_detail' | 'brand_overview'
}

const BrandAssistantDrawer: React.FC<BrandAssistantDrawerProps> = ({
  open,
  onClose,
  brand,
  brandLabel,
  date,
  task,
  detail,
  pageKey = 'brand_detail',
}) => {
  const isMobile = useMobile()
  const session = useBrandAssistantSession({
    open,
    brand,
    date,
    task,
    detail,
    pageKey,
  })

  return (
    <Drawer
      open={open}
      onClose={onClose}
      placement="right"
      size={isMobile ? 'default' : 'large'}
      className="ops-brand-assistant-drawer"
      title={(
        <div className="ops-brand-assistant-drawer__title">
          <RobotOutlined />
          <span>AI运营领队</span>
        </div>
      )}
      destroyOnClose={false}
    >
      <BrandAssistantDrawerContent
        brandLabel={brandLabel}
        onClose={onClose}
        session={session}
        task={task}
      />
    </Drawer>
  )
}

export default BrandAssistantDrawer
