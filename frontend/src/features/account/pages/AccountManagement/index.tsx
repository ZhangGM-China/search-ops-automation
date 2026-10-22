import React from 'react'
import { Form, message } from 'antd'
import { Form as MobForm } from 'antd-mobile'

import { isPlatformAdmin } from '@/shared/lib/permissions'
import { adminApi } from '@/shared/api'
import useMobile from '@/shared/hooks/useMobile'
import { getApiErrorMessage } from '@/shared/lib/error'
import type { UserInfo } from '@/shared/types'

import { UserEditor } from './components/UserEditor'
import { UserManagementSection } from './components/UserManagementSection'
import {
  createDefaultAccountValues,
  getRoleOptions,
  getVisibleActionPermissionOptions,
  normalizeActionPermissionsForRole,
} from './accountPermissions'

const AccountManagement: React.FC = () => {
  const [users, setUsers] = React.useState<UserInfo[]>([])
  const [loading, setLoading] = React.useState(false)
  const [pageSize, setPageSize] = React.useState(10)
  const [modalVisible, setModalVisible] = React.useState(false)
  const [editingUser, setEditingUser] = React.useState<UserInfo | null>(null)
  const [modalRole, setModalRole] = React.useState('brand_member')
  const [form] = Form.useForm<Partial<UserInfo>>()
  const [mobileForm] = MobForm.useForm()
  const isMobile = useMobile()
  const currentUser = (() => {
    const raw = localStorage.getItem('user')
    if (!raw) return null
    try {
      return JSON.parse(raw) as UserInfo
    } catch {
      return null
    }
  })()
  const canAssignPrivilegedRoles = !currentUser || isPlatformAdmin(currentUser)
  const roleOptions = getRoleOptions(canAssignPrivilegedRoles)
  const visibleActionPermissionOptions = getVisibleActionPermissionOptions(modalRole)

  const fetchUsers = React.useCallback(async () => {
    setLoading(true)
    try {
      const response = await adminApi.getUsers()
      setUsers(response.data)
    } catch {
      message.error('获取用户列表失败')
    } finally {
      setLoading(false)
    }
  }, [])

  React.useEffect(() => {
    const timer = window.setTimeout(() => {
      void fetchUsers()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [fetchUsers])

  const handleCreateOrUpdate = React.useCallback(async (values: Partial<UserInfo>) => {
    try {
      const payload: Partial<UserInfo> = {
        ...values,
        actionPermissions: normalizeActionPermissionsForRole(values.actionPermissions, values.role),
      }
      if (values.role === 'platform_admin') {
        delete payload.pagePermissions
        delete payload.brandPermissions
        delete payload.actionPermissions
      }
      if (editingUser) {
        await adminApi.updateUser(editingUser.id!, payload)
        message.success('更新成功')
      } else {
        const response = await adminApi.createUser(payload)
        const temporaryPassword = response.data?.temporaryPassword
        message.success(temporaryPassword ? `创建成功，临时密码为 ${temporaryPassword}` : '创建成功')
      }
      setModalVisible(false)
      void fetchUsers()
    } catch (error: unknown) {
      message.error(getApiErrorMessage(error, '操作失败'))
    }
  }, [editingUser, fetchUsers])

  const handleResetPassword = React.useCallback(async (id: number) => {
    try {
      const response = await adminApi.resetPassword(id)
      const temporaryPassword = response.data?.temporaryPassword
      message.success(temporaryPassword ? `密码已重置，临时密码为 ${temporaryPassword}` : '密码已重置')
    } catch {
      message.error('重置密码失败')
    }
  }, [])

  const handleDelete = React.useCallback(async (id: number) => {
    try {
      await adminApi.deleteUser(id)
      message.success('删除成功')
      void fetchUsers()
    } catch {
      message.error('删除失败')
    }
  }, [fetchUsers])

  const openModal = React.useCallback((user?: UserInfo) => {
    setEditingUser(user ?? null)
    const initialValues = user
      ? { ...user, role: user.role || (isPlatformAdmin(user) ? 'platform_admin' : 'brand_member') }
      : createDefaultAccountValues()
    if (isMobile) {
      mobileForm.resetFields()
      mobileForm.setFieldsValue(initialValues)
    } else {
      form.resetFields()
      form.setFieldsValue(initialValues)
    }
    setModalRole(initialValues.role ?? 'brand_member')
    setModalVisible(true)
  }, [form, isMobile, mobileForm])

  const handleRoleChange = React.useCallback((roleValue: string) => {
    setModalRole(roleValue)
    const actionPermissions = isMobile
      ? mobileForm.getFieldValue('actionPermissions')
      : form.getFieldValue('actionPermissions')
    const nextActions = normalizeActionPermissionsForRole(actionPermissions, roleValue)
    if (isMobile) mobileForm.setFieldValue('actionPermissions', nextActions)
    else form.setFieldValue('actionPermissions', nextActions)
  }, [form, isMobile, mobileForm])

  return (
    <>
      <UserManagementSection
        isMobile={isMobile}
        loading={loading}
        pageSize={pageSize}
        users={users}
        onCreate={() => openModal()}
        onDelete={handleDelete}
        onEdit={openModal}
        onPageSizeChange={setPageSize}
        onResetPassword={handleResetPassword}
      />
      <UserEditor
        canAssignPrivilegedRoles={canAssignPrivilegedRoles}
        editingUser={editingUser}
        form={form}
        isMobile={isMobile}
        mobileForm={mobileForm}
        role={modalRole}
        roleOptions={roleOptions}
        visible={modalVisible}
        visibleActionPermissionOptions={visibleActionPermissionOptions}
        onClose={() => setModalVisible(false)}
        onRoleChange={handleRoleChange}
        onSubmit={handleCreateOrUpdate}
      />
    </>
  )
}

export default AccountManagement
