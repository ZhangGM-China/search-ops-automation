import React from 'react';
import { NavBar, TabBar, Popup, List, Button, Dialog, Form, Input, Toast } from 'antd-mobile';
import {
  ClockCircleOutline,
  UserOutline,
  TeamOutline,
  SetOutline,
  UndoOutline,
  KeyOutline,
  ChatCheckOutline,
} from 'antd-mobile-icons';
import { useNavigate, useLocation, Navigate, Outlet } from 'react-router-dom';
import { hasPagePermission, roleDisplayLabel } from '@/app/auth/permissions';
import { authApi } from '@/shared/api';
import type { UserInfo } from '@/shared/types';
import { getApiErrorMessage } from '@/shared/lib/error';

const MobileLayout: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const [userVisible, setUserVisible] = React.useState(false);
  const [pwdVisible, setPwdVisible] = React.useState(false);
  const [form] = Form.useForm();

  const userStr = localStorage.getItem('user');
  const user: UserInfo | null = userStr ? JSON.parse(userStr) : null;

  if (!user) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}${location.hash}` }}
      />
    );
  }

  const canAccess = (path: string) => {
    return hasPagePermission(user, path);
  };

  const tabs = [
    { key: '/ops-dashboard', icon: <ClockCircleOutline />, title: '总览', permission: '/ops-dashboard' },
    { key: '/smart-assistant', icon: <ChatCheckOutline />, title: '助理', permission: '/smart-assistant' },
    { key: '/account-management', icon: <TeamOutline />, title: '账号', permission: '/account-management' },
  ].filter(item => canAccess(item.permission));

  const operationsLogRoutes = [
    { key: '/blacklist', permission: '/blacklist' },
    { key: '/demotion', permission: '/demotion' },
    { key: '/stock-tracking', permission: '/stock-tracking' },
    { key: '/no-result-governance', permission: '/no-result-governance' },
  ];
  const defaultOperationsLogRoute = operationsLogRoutes.find(item => canAccess(item.permission));

  const extraMenuItems = [
    { key: '/ops-dashboard', icon: <ClockCircleOutline />, title: '品牌总览', permission: '/ops-dashboard' },
    { key: '/smart-assistant', icon: <ChatCheckOutline />, title: '智能运营助理', permission: '/smart-assistant' },
    ...(defaultOperationsLogRoute ? [{ key: defaultOperationsLogRoute.key, icon: <ClockCircleOutline />, title: '运营日志', permission: defaultOperationsLogRoute.permission }] : []),
    { key: '/account-management', icon: <TeamOutline />, title: '账号管理', permission: '/account-management' },
    { key: '/config', icon: <SetOutline />, title: '系统配置', permission: '/config' },
  ].filter(item => canAccess(item.permission));

  const handleLogout = async () => {
    try {
      await authApi.logout();
      Toast.show({
        icon: 'success',
        content: '已退出登录',
      });
    } catch {
      Toast.show({
        icon: 'fail',
        content: '退出登录失败，已清理本地登录状态',
      });
    } finally {
      localStorage.removeItem('token');
      localStorage.removeItem('user');
      navigate('/login');
    }
  };

  const handleChangePassword = async () => {
    try {
      const values = await form.validateFields();
      if (values.newPassword !== values.confirmPassword) {
        Toast.show('两次输入的密码不一致');
        return;
      }
      await authApi.changePassword({
        oldPassword: values.oldPassword,
        newPassword: values.newPassword
      });
      Toast.show({
        icon: 'success',
        content: '密码修改成功',
      });
      setPwdVisible(false);
      form.resetFields();
    } catch (error: unknown) {
      console.error('Password change failed:', error);
      Toast.show({
        icon: 'fail',
        content: getApiErrorMessage(error, '修改失败'),
      });
    }
  };

  const navTitle: Record<string, string> = {
    '/': '品牌总览',
    '/blacklist': '相关性检测',
    '/demotion': '断码检测',
    '/stock-tracking': '断码追踪',
    '/ops-dashboard': '品牌总览',
    '/ops-dashboard/atlas': 'Atlas Query 治理',
    '/smart-assistant': '智能运营助理',
    '/account-management': '账号管理',
    '/config': '系统配置',
  };

  return (
    <div style={{ minHeight: '100dvh', display: 'flex', flexDirection: 'column', background: 'var(--app-bg)', paddingTop: 'var(--safe-top)' }}>
      <div style={{ background: 'rgba(255,255,255,0.96)', borderBottom: '1px solid var(--surface-border)', backdropFilter: 'blur(10px)' }}>
        <NavBar 
          backArrow={false}
          right={
            <button
              type='button'
              aria-label='打开账号菜单'
              onClick={() => setUserVisible(true)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                width: 44,
                height: 44,
                border: '1px solid var(--surface-border)',
                borderRadius: 8,
                background: 'var(--brand-primary-soft)',
                color: 'var(--brand-primary)',
              }}
            >
              <UserOutline fontSize={22} />
            </button>
          }
        >
          {navTitle[location.pathname] || '拾心智能运营平台'}
        </NavBar>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 12px 16px' }}>
        <Outlet />
      </div>

      <div style={{ background: 'rgba(255,255,255,0.96)', borderTop: '1px solid var(--surface-border)', paddingBottom: 'var(--safe-bottom)', backdropFilter: 'blur(10px)' }}>
        <TabBar activeKey={location.pathname} onChange={value => navigate(value)}>
          {tabs.map(item => (
            <TabBar.Item key={item.key} icon={item.icon} title={item.title} />
          ))}
        </TabBar>
      </div>

      <Popup
        visible={userVisible}
        onMaskClick={() => setUserVisible(false)}
        position='right'
        bodyStyle={{ width: '72vw', maxWidth: '320px' }}
      >
        <div style={{ padding: `20px 18px calc(24px + var(--safe-bottom))` }}>
          <div style={{ marginBottom: '24px', textAlign: 'center' }}>
            <UserOutline fontSize={44} style={{ color: 'var(--text-tertiary)' }} />
            <div style={{ marginTop: '8px', fontWeight: 'bold' }}>{user.username}</div>
            <div style={{ fontSize: '12px', color: 'var(--brand-primary)' }}>{roleDisplayLabel(user)}</div>
          </div>
          <List header='更多操作'>
            {extraMenuItems.map(item => (
              <List.Item 
                key={item.key} 
                prefix={item.icon} 
                onClick={() => {
                  navigate(item.key);
                  setUserVisible(false);
                }}
              >
                {item.title}
              </List.Item>
            ))}
            <List.Item prefix={<KeyOutline />} onClick={() => setPwdVisible(true)}>
              修改密码
            </List.Item>
            <List.Item prefix={<UndoOutline />} onClick={handleLogout} style={{ color: 'var(--danger)' }}>
              退出登录
            </List.Item>
          </List>
          <div style={{ marginTop: '40px' }}>
            <Button block color='default' onClick={() => setUserVisible(false)}>
              关闭
            </Button>
          </div>
        </div>
      </Popup>

      <Dialog
        visible={pwdVisible}
        content={
          <Form form={form} layout='vertical'>
            <Form.Item name='oldPassword' label='旧密码' rules={[{ required: true, message: '请输入旧密码' }]}>
              <Input placeholder='请输入旧密码' type='password' />
            </Form.Item>
            <Form.Item name='newPassword' label='新密码' rules={[{ required: true, message: '请输入新密码' }]}>
              <Input placeholder='请输入新密码' type='password' />
            </Form.Item>
            <Form.Item name='confirmPassword' label='确认新密码' rules={[{ required: true, message: '请确认新密码' }]}>
              <Input placeholder='请再次输入新密码' type='password' />
            </Form.Item>
          </Form>
        }
        actions={[
          [
            { key: 'cancel', text: '取消', onClick: () => setPwdVisible(false) },
            { key: 'confirm', text: '确认', onClick: handleChangePassword, bold: true },
          ],
        ]}
      />
    </div>
  );
};

export default MobileLayout;
