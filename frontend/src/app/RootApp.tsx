import React, { useState } from 'react';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';

import '../styles/App.css';
import { AUTH_EXPIRED_EVENT } from '@/shared/api';
import { AppRouter } from './AppRouter';
import { appTheme } from './theme';

dayjs.locale('zh-cn');

const RootApp: React.FC = () => {
  const [authRevision, setAuthRevision] = useState(0);

  React.useEffect(() => {
    const refreshAuthState = () => setAuthRevision((current) => current + 1);
    const handleStorage = (event: StorageEvent) => {
      if (event.storageArea && event.storageArea !== localStorage) return;
      if (event.key === null || event.key === 'token' || event.key === 'user') {
        refreshAuthState();
      }
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, refreshAuthState);
    window.addEventListener('storage', handleStorage);
    return () => {
      window.removeEventListener(AUTH_EXPIRED_EVENT, refreshAuthState);
      window.removeEventListener('storage', handleStorage);
    };
  }, []);

  return (
    <ConfigProvider locale={zhCN} theme={appTheme}>
      <AntApp>
        <AppRouter revision={authRevision} />
      </AntApp>
    </ConfigProvider>
  );
};

export default RootApp;
