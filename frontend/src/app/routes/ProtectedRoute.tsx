import React from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import type { UserInfo } from '@/shared/types';
import { hasPagePermission, hasRouteActionPermission } from '../auth/permissions';

export const ProtectedRoute: React.FC<{ children: React.ReactNode; path: string }> = ({ children, path }) => {
  const location = useLocation();
  const authValue = localStorage.getItem('token');
  const userStr = localStorage.getItem('user');

  if (!authValue || !userStr) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}${location.hash}` }}
      />
    );
  }

  const user: UserInfo = JSON.parse(userStr);
  if (!hasPagePermission(user, path) || !hasRouteActionPermission(user, path)) {
    return (
      <section className="app-empty-state" aria-live="polite">
        <h2>当前账号无权限访问</h2>
        <p>请联系管理员为该账号分配对应页面权限后，再重新进入此功能。</p>
      </section>
    );
  }

  return <>{children}</>;
};
