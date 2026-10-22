import React from 'react'
import { Button, Card, Flex, Popconfirm, Space, Table } from 'antd'
import {
  ActionSheet,
  Button as MobButton,
  Card as MobCard,
  Dialog as MobDialog,
} from 'antd-mobile'

import { isPlatformAdmin } from '@/shared/lib/permissions'
import type { UserInfo } from '@/shared/types'

import { roleLabel } from '../accountPermissions'

type UserManagementSectionProps = {
  isMobile: boolean
  loading: boolean
  pageSize: number
  users: UserInfo[]
  onCreate: () => void
  onDelete: (id: number) => Promise<void>
  onEdit: (user: UserInfo) => void
  onPageSizeChange: (size: number) => void
  onResetPassword: (id: number) => Promise<void>
}

const MobileUserManagement: React.FC<Omit<UserManagementSectionProps, 'isMobile' | 'loading' | 'pageSize' | 'onPageSizeChange'>> = ({
  users,
  onCreate,
  onDelete,
  onEdit,
  onResetPassword,
}) => (
  <div className="ops-page" style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
    <div>
      <div className="ops-page-title">账号管理</div>
    </div>
    <div style={{ padding: '4px 0' }}>
      <MobButton block color="primary" onClick={onCreate}>新建账号</MobButton>
    </div>
    {users.map((user) => (
      <MobCard
        key={user.id}
        title={user.username}
        extra={roleLabel(user)}
        headerStyle={{ fontWeight: 'bold' }}
        onClick={() => {
          ActionSheet.show({
            actions: [
              { text: '修改权限', key: 'edit' },
              ...(isPlatformAdmin(user) ? [] : [
                { text: '重置密码', key: 'reset' },
                { text: '删除账号', key: 'delete', danger: true },
              ]),
            ],
            onAction: (action) => {
              if (action.key === 'edit') onEdit(user)
              else if (action.key === 'reset' && user.id !== undefined) {
                void MobDialog.confirm({
                  content: '确定重置该账号密码吗？',
                  onConfirm: () => onResetPassword(user.id!),
                })
              } else if (action.key === 'delete' && user.id !== undefined) {
                void MobDialog.confirm({
                  content: '确定删除该账号吗？',
                  onConfirm: () => onDelete(user.id!),
                })
              }
            },
          })
        }}
      >
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          <div>权限范围: {isPlatformAdmin(user) ? '全部页面 / 全部品牌（含新增）' : `${(user.pagePermissions || []).length} 页面 / ${(user.brandPermissions || []).length} 品牌`}</div>
        </div>
      </MobCard>
    ))}
  </div>
)

const DesktopUserManagement: React.FC<Omit<UserManagementSectionProps, 'isMobile'>> = ({
  loading,
  pageSize,
  users,
  onCreate,
  onDelete,
  onEdit,
  onPageSizeChange,
  onResetPassword,
}) => {
  const columns = [
    { title: '账号', dataIndex: 'username', key: 'username', width: 200 },
    { title: '类型', key: 'type', width: 120, render: (user: UserInfo) => roleLabel(user) },
    {
      title: '操作',
      key: 'action',
      width: 250,
      fixed: 'right' as const,
      render: (user: UserInfo) => (
        <Space size="middle">
          <Button type="link" onClick={() => onEdit(user)}>修改权限</Button>
          {!isPlatformAdmin(user) && (
            <>
              <Popconfirm title="确定重置该账号密码吗？" onConfirm={() => onResetPassword(user.id!)}>
                <Button type="link">重置密码</Button>
              </Popconfirm>
              <Popconfirm title="确定删除该账号吗？" onConfirm={() => onDelete(user.id!)}>
                <Button type="link" danger>删除</Button>
              </Popconfirm>
            </>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Flex vertical gap="small" className="ops-page">
      <Flex justify="space-between" align="center" gap={12} wrap>
        <div>
          <div className="ops-page-title">账号管理</div>
        </div>
        <Button type="primary" size="small" onClick={onCreate}>新建账号</Button>
      </Flex>

      <Card size="small">
        <Table<UserInfo>
          columns={columns}
          dataSource={users}
          rowKey="id"
          loading={loading}
          size="small"
          pagination={{
            pageSize,
            showSizeChanger: true,
            onShowSizeChange: (_, size) => onPageSizeChange(size),
            pageSizeOptions: ['10', '20', '50', '100'],
            size: 'small',
          }}
          scroll={{ x: 'max-content', y: 'calc(100vh - 180px)' }}
          sticky
        />
      </Card>
    </Flex>
  )
}

export const UserManagementSection: React.FC<UserManagementSectionProps> = ({ isMobile, ...props }) => (
  isMobile ? <MobileUserManagement {...props} /> : <DesktopUserManagement {...props} />
)
