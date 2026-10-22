import React from 'react';
import { Form as AntForm, Input as AntInput, Button as AntButton, Card, message, Layout } from 'antd';
import { Form as MobForm, Input as MobInput, Button as MobButton } from 'antd-mobile';
import { UserOutlined, LockOutlined } from '@ant-design/icons';
import { useLocation, useNavigate } from 'react-router-dom';
import { authApi } from '@/shared/api';
import useMobile from '@/shared/hooks/useMobile';
import { getApiErrorMessage } from '@/shared/lib/error';
import '@/styles/App.css';

const { Content } = Layout;
const AUTH_RETURN_TO_KEY = 'search_ops_auth_return_to';

type LoginFormValues = {
  username: string;
  password: string;
};

const safeReturnTo = (value: unknown) => {
  if (typeof value !== 'string') return null;
  if (!value.startsWith('/') || value.startsWith('//') || value.startsWith('/login')) return null;
  return value;
};

const Login: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const [loading, setLoading] = React.useState(false);
  const [messageApi, messageContextHolder] = message.useMessage();
  const isMobile = useMobile();

  const onFinish = async (values: LoginFormValues) => {
    setLoading(true);
    try {
      const response = await authApi.login(values);
      const { access_token, user } = response.data;
      localStorage.setItem('token', access_token);
      localStorage.setItem('user', JSON.stringify(user));
      messageApi.success('登录成功');
      const stateReturnTo = safeReturnTo((location.state as { from?: unknown } | null)?.from);
      const storedReturnTo = safeReturnTo(sessionStorage.getItem(AUTH_RETURN_TO_KEY));
      sessionStorage.removeItem(AUTH_RETURN_TO_KEY);
      navigate(stateReturnTo || storedReturnTo || '/ops-dashboard', { replace: true });
    } catch (error: unknown) {
      messageApi.error(getApiErrorMessage(error, '登录失败'));
    } finally {
      setLoading(false);
    }
  };

  if (isMobile) {
    return (
      <>
        {messageContextHolder}
        <div className='login-shell login-mobile-panel'>
          <div className='login-mobile-card'>
            <div className='login-hero'>
              <div className='login-brand-row'>
                <img src="/brand-logo.png" alt="拾心AI logo" style={{ height: 48 }} />
                <div>
                  <div className='login-hero__eyebrow'>AI OPS</div>
                  <h2 className='login-hero__title' style={{ fontSize: '24px', marginTop: '4px' }}>拾心 · 智能运营平台</h2>
                </div>
              </div>
              <p className='login-hero__desc'>智能发现、智能诊断、智能治理</p>
            </div>
            <div style={{ height: 1, background: 'rgba(15,23,42,0.08)', margin: '0 0 20px' }} />
            <div aria-label='mobile-login-form'>
              <MobForm
                layout='vertical'
                onFinish={onFinish}
                footer={
                  <MobButton block type='submit' color='primary' size='large' loading={loading}>
                    登录
                  </MobButton>
                }
              >
                <MobForm.Item
                  name='username'
                  label='用户名'
                  rules={[{ required: true, message: '请输入用户名' }]}
                >
                  <MobInput placeholder='请输入用户名' clearable />
                </MobForm.Item>
                <MobForm.Item
                  name='password'
                  label='密码'
                  rules={[{ required: true, message: '请输入密码' }]}
                >
                  <MobInput placeholder='请输入密码' type='password' clearable />
                </MobForm.Item>
              </MobForm>
            </div>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      {messageContextHolder}
      <Layout className='login-shell' style={{ minHeight: '100vh' }}>
        <Content style={{ display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
          <Card style={{ width: 440, borderRadius: 8, boxShadow: 'var(--surface-shadow)', borderColor: 'var(--surface-border)' }}>
            <div className='login-hero'>
              <div className='login-brand-row'>
                <img src="/brand-logo.png" alt="拾心AI logo" />
                <div>
                  <div className='login-hero__eyebrow'>AI OPS</div>
                  <h1 className='login-hero__title'>拾心 · 智能运营平台</h1>
                </div>
              </div>
              <p className='login-hero__desc'>智能发现、智能诊断、智能治理</p>
            </div>
            <AntForm
              name="login"
              initialValues={{ remember: true }}
              onFinish={onFinish}
              size="large"
              layout='vertical'
            >
              <AntForm.Item
                name="username"
                label="用户名"
                rules={[{ required: true, message: '请输入用户名' }]}
              >
                <AntInput prefix={<UserOutlined />} placeholder="用户名" />
              </AntForm.Item>
              <AntForm.Item
                name="password"
                label="密码"
                rules={[{ required: true, message: '请输入密码' }]}
              >
                <AntInput.Password prefix={<LockOutlined />} placeholder="密码" />
              </AntForm.Item>
              <AntForm.Item>
                <AntButton type="primary" htmlType="submit" style={{ width: '100%' }} loading={loading}>
                  登录
                </AntButton>
              </AntForm.Item>
            </AntForm>
          </Card>
        </Content>
      </Layout>
    </>
    );
  };

export default Login;
