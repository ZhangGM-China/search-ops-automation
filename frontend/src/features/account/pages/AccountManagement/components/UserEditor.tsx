import React from 'react'
import { Alert, Checkbox, Form, Input, Modal, Select, type FormInstance } from 'antd'
import {
  Button as MobButton,
  Form as MobForm,
  Input as MobInput,
  Popup as MobPopup,
  Selector as MobSelector,
  Space as MobSpace,
} from 'antd-mobile'

import type { UserInfo } from '@/shared/types'

import {
  accountBrands,
  accountPages,
  type AccountOption,
} from '../accountPermissions'

type UserEditorProps = {
  canAssignPrivilegedRoles: boolean
  editingUser: UserInfo | null
  form: FormInstance<Partial<UserInfo>>
  isMobile: boolean
  mobileForm: ReturnType<typeof MobForm.useForm>[0]
  role: string
  roleOptions: AccountOption[]
  visible: boolean
  visibleActionPermissionOptions: AccountOption[]
  onClose: () => void
  onRoleChange: (role: string) => void
  onSubmit: (values: Partial<UserInfo>) => Promise<void>
}

export const UserEditor: React.FC<UserEditorProps> = ({
  canAssignPrivilegedRoles,
  editingUser,
  form,
  isMobile,
  mobileForm,
  role,
  roleOptions,
  visible,
  visibleActionPermissionOptions,
  onClose,
  onRoleChange,
  onSubmit,
}) => {
  const allPermissions = role === 'platform_admin'
  const permissionNotice = <Alert type="info" showIcon title="超级管理员自动拥有全部页面、品牌和动作权限，包含后续新增模块，无需逐项授权。" />
  const title = editingUser ? '修改账号权限' : '新建账号'
  const handleSubmit = (values: Partial<UserInfo>) => {
    void onSubmit(values)
  }

  if (isMobile) {
    return (
      <MobPopup
        visible={visible}
        onMaskClick={onClose}
        position="bottom"
        bodyStyle={{ height: '80vh', overflowY: 'auto' }}
      >
        <div style={{ padding: '20px' }}>
          <h3>{title}</h3>
          <MobForm
            form={mobileForm}
            layout="vertical"
            onFinish={(values) => handleSubmit(values as Partial<UserInfo>)}
            footer={(
              <MobSpace block direction="vertical">
                <MobButton block type="submit" color="primary">提交</MobButton>
                <MobButton block onClick={onClose}>取消</MobButton>
              </MobSpace>
            )}
          >
            <MobForm.Item name="username" label="账号名" rules={[{ required: true, message: '请输入账号名' }]}>
              <MobInput disabled={Boolean(editingUser)} placeholder="请输入账号名" />
            </MobForm.Item>
            <MobForm.Item name="role" label="角色" rules={[{ required: true, message: '请选择角色' }]} getValueProps={(value) => ({ value: [value] })} getValueFromEvent={(value: string[]) => value[0]}>
              <MobSelector
                disabled={!canAssignPrivilegedRoles}
                options={roleOptions}
                onChange={(value) => onRoleChange(String(value[0] ?? 'brand_member'))}
              />
            </MobForm.Item>
            {allPermissions ? permissionNotice : <>
            <MobForm.Item name="pagePermissions" label="页面权限" rules={[{ required: true, message: '请选择页面权限' }]}>
              <MobSelector options={accountPages} multiple />
            </MobForm.Item>
            <MobForm.Item name="brandPermissions" label="品牌权限" rules={[{ required: true, message: '请选择品牌权限' }]}>
              <MobSelector options={accountBrands} multiple />
            </MobForm.Item>
            {canAssignPrivilegedRoles ? (
              <MobForm.Item name="actionPermissions" label="动作权限">
                <MobSelector options={visibleActionPermissionOptions} multiple />
              </MobForm.Item>
            ) : null}
            </>}
          </MobForm>
        </div>
      </MobPopup>
    )
  }

  return (
    <Modal
      title={title}
      open={visible}
      onCancel={onClose}
      onOk={() => form.submit()}
      width={600}
    >
      <Form<Partial<UserInfo>> form={form} onFinish={handleSubmit} layout="vertical">
        <Form.Item name="username" label="账号名" rules={[{ required: true, message: '请输入账号名' }]}>
          <Input disabled={Boolean(editingUser)} />
        </Form.Item>
        <Form.Item name="role" label="角色" rules={[{ required: true, message: '请选择角色' }]}>
          <Select options={roleOptions} disabled={!canAssignPrivilegedRoles} onChange={onRoleChange} />
        </Form.Item>
        {allPermissions ? permissionNotice : <>
        <Form.Item name="pagePermissions" label="页面权限" rules={[{ required: true, message: '请选择页面权限' }]}>
          <Checkbox.Group options={accountPages} />
        </Form.Item>
        <Form.Item name="brandPermissions" label="品牌权限" rules={[{ required: true, message: '请选择品牌权限' }]}>
          <Checkbox.Group options={accountBrands} />
        </Form.Item>
        {canAssignPrivilegedRoles ? (
          <Form.Item name="actionPermissions" label="动作权限">
            <Checkbox.Group options={visibleActionPermissionOptions} />
          </Form.Item>
        ) : null}
        </>}
      </Form>
    </Modal>
  )
}
