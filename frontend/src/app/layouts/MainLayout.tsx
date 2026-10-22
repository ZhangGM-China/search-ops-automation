import React, { useState } from 'react';
import { Dropdown, Form, Input, Layout, message, Modal } from 'antd';
import {
  ArrowDownOutlined,
  FileTextOutlined,
  HistoryOutlined,
  KeyOutlined,
  LogoutOutlined,
  RobotOutlined,
  SettingOutlined,
  StopOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom';

import { authApi } from '@/shared/api';
import type { UserInfo } from '@/shared/types';
import { getApiErrorMessage } from '@/shared/lib/error';
import { hasPagePermission, hasRouteActionPermission, roleDisplayLabel } from '../auth/permissions';

const { Header, Content } = Layout;

type AppMenuConfig = {
  key: string;
  icon: React.ReactNode;
  label: string;
  permission?: string;
  children?: AppMenuConfig[];
};

const menuConfigs: AppMenuConfig[] = [
  { key: '/ops-dashboard', icon: <HistoryOutlined />, label: '品牌总览', permission: '/ops-dashboard' },
  { key: '/smart-assistant', icon: <RobotOutlined />, label: '智能运营助理', permission: '/smart-assistant' },
  {
    key: '/operations-log',
    icon: <HistoryOutlined />,
    label: '运营日志',
    children: [
      { key: '/blacklist', icon: <StopOutlined />, label: '相关性检测', permission: '/blacklist' },
      { key: '/demotion', icon: <ArrowDownOutlined />, label: '断码检测', permission: '/demotion' },
      { key: '/stock-tracking', icon: <HistoryOutlined />, label: '断码追踪', permission: '/stock-tracking' },
      { key: '/no-result-governance', icon: <FileTextOutlined />, label: '无结果词', permission: '/no-result-governance' },
    ],
  },
  { key: '/account-management', icon: <TeamOutlined />, label: '账号管理', permission: '/account-management' },
  { key: '/config', icon: <SettingOutlined />, label: '系统配置', permission: '/config' },
];

export const MainLayout: React.FC = () => {
  const [passwordModalVisible, setPasswordModalVisible] = useState(false);
  const [form] = Form.useForm();
  const [messageApi, messageContextHolder] = message.useMessage();
  const navigate = useNavigate();
  const location = useLocation();

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

  const canAccess = (permission?: string) => !permission || (
    hasPagePermission(user, permission) && hasRouteActionPermission(user, permission)
  );
  const menuItems = menuConfigs
    .map((item) => {
      if (item.children) {
        const children = item.children.filter((child) => canAccess(child.permission));
        if (children.length === 0) return null;
        return { ...item, children };
      }
      if (!canAccess(item.permission)) return null;
      return item;
    })
    .filter((item): item is AppMenuConfig => Boolean(item));

  const isMenuActive = (item: AppMenuConfig) => {
    if (item.children) {
      return item.children.some((child) => child.key === location.pathname);
    }
    if (item.key === '/ops-dashboard') {
      return location.pathname.startsWith('/ops-dashboard');
    }
    return item.key === location.pathname;
  };

  const handleLogout = async () => {
    try {
      await authApi.logout();
      messageApi.success('已退出登录');
    } catch {
      messageApi.error('退出登录失败，已清理本地登录状态');
    } finally {
      localStorage.removeItem('token');
      localStorage.removeItem('user');
      navigate('/login');
    }
  };

  const handleChangePassword = async (values: { oldPassword: string; newPassword: string; confirmPassword: string }) => {
    if (values.newPassword !== values.confirmPassword) {
      return messageApi.error('两次新密码输入不一致');
    }
    try {
      await authApi.changePassword({
        oldPassword: values.oldPassword,
        newPassword: values.newPassword,
      });
      messageApi.success('密码修改成功');
      setPasswordModalVisible(false);
      form.resetFields();
    } catch (error: unknown) {
      messageApi.error(getApiErrorMessage(error, '密码修改失败'));
    }
  };

  const userMenuItems = [
    {
      key: 'change-password',
      icon: <KeyOutlined />,
      label: '修改密码',
      onClick: () => setPasswordModalVisible(true),
    },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: '退出登录',
      onClick: handleLogout,
    },
  ];

  return (
    <Layout className="app-shell">
      {messageContextHolder}
      <Header className="app-top-nav">
        <button type="button" className="app-brand" onClick={() => navigate('/ops-dashboard')} aria-label="返回品牌总览">
          <img src="/brand-logo.png" alt="logo" />
          <span className="app-brand__copy">
            <span className="app-brand__title">拾心 · 智能运营</span>
            <span className="app-brand__subtitle">智能发现 · 智能诊断 · 智能治理</span>
          </span>
        </button>
        <nav className="app-top-nav__menu" aria-label="主导航" role="menubar">
          {menuItems.map((item) => {
            const active = isMenuActive(item);
            return (
              <button
                type="button"
                key={item.key}
                role="menuitem"
                className={`app-top-nav__item app-top-nav__link${active ? ' app-top-nav__item--active' : ''}`}
                onClick={() => navigate(item.children?.[0]?.key || item.key)}
              >
                {item.icon}
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
        <Dropdown menu={{ items: userMenuItems }} placement="bottomRight" trigger={['click']}>
          <button type="button" className="app-user-trigger" aria-label="打开账号菜单">
            <UserOutlined style={{ marginRight: 8 }} />
            <span className="app-user-trigger__meta">
              <span className="app-user-trigger__name">{user.username}</span>
              <span className="app-user-trigger__role">{roleDisplayLabel(user)}</span>
            </span>
          </button>
        </Dropdown>
      </Header>
      <Layout className="app-main-layout">
        <Content className="app-content">
          <div className="app-content__inner">
            <Outlet />
          </div>
        </Content>
      </Layout>

      <Modal
        title="修改密码"
        open={passwordModalVisible}
        onCancel={() => setPasswordModalVisible(false)}
        onOk={() => form.submit()}
      >
        <Form form={form} onFinish={handleChangePassword} layout="vertical">
          <Form.Item name="oldPassword" label="旧密码" rules={[{ required: true, message: '请输入旧密码' }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="newPassword" label="新密码" rules={[{ required: true, message: '请输入新密码' }, { min: 6, message: '密码至少6位' }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="confirmPassword" label="确认新密码" rules={[{ required: true, message: '请确认新密码' }]}>
            <Input.Password />
          </Form.Item>
        </Form>
      </Modal>
    </Layout>
  );
};
