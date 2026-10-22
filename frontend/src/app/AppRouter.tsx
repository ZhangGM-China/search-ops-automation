import React, { Suspense } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

import { ResponsiveLayout } from './layouts/ResponsiveLayout';
import { ProtectedRoute } from './routes/ProtectedRoute';

const BlacklistLogs = React.lazy(() => import('../features/operations/pages/BlacklistLogs'));
const DemotedLogs = React.lazy(() => import('../features/operations/pages/DemotedLogs'));
const StockTasks = React.lazy(() => import('../features/operations/pages/StockTasks'));
const NoResultGovernance = React.lazy(() => import('../features/no-result/pages/NoResultGovernance'));
const BrandOverview = React.lazy(() => import('../features/query-governance/pages/BrandOverview'));
const BrandDetail = React.lazy(() => import('../features/query-governance/pages/BrandDetail'));
const SmartAssistant = React.lazy(() => import('../features/assistant/pages/SmartAssistant'));
const AccountManagement = React.lazy(() => import('../features/account/pages/AccountManagement'));
const ConfigPage = React.lazy(() => import('../features/config/pages/Config'));
const Login = React.lazy(() => import('../features/auth/pages/Login'));

export const AppRouter: React.FC<{ revision?: number }> = ({ revision = 0 }) => (
  <BrowserRouter key={revision}>
    <Suspense
      fallback={(
        <div className="app-loading-shell">
          <div className="app-loading-panel">页面加载中...</div>
        </div>
      )}
    >
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<ResponsiveLayout />}>
          <Route index element={<Navigate to="/ops-dashboard" replace />} />
          <Route path="blacklist" element={<ProtectedRoute path="/blacklist"><BlacklistLogs /></ProtectedRoute>} />
          <Route path="demotion" element={<ProtectedRoute path="/demotion"><DemotedLogs /></ProtectedRoute>} />
          <Route path="stock-tracking" element={<ProtectedRoute path="/stock-tracking"><StockTasks /></ProtectedRoute>} />
          <Route path="no-result-governance" element={<ProtectedRoute path="/no-result-governance"><NoResultGovernance /></ProtectedRoute>} />
          <Route path="ops-dashboard" element={<ProtectedRoute path="/ops-dashboard"><BrandOverview /></ProtectedRoute>} />
          <Route path="ops-dashboard/:brand" element={<ProtectedRoute path="/ops-dashboard"><BrandDetail /></ProtectedRoute>} />
          <Route path="smart-assistant" element={<ProtectedRoute path="/smart-assistant"><SmartAssistant /></ProtectedRoute>} />
          <Route path="account-management" element={<ProtectedRoute path="/account-management"><AccountManagement /></ProtectedRoute>} />
          <Route path="config" element={<ProtectedRoute path="/config"><ConfigPage /></ProtectedRoute>} />
          <Route path="*" element={<Navigate to="/ops-dashboard" replace />} />
        </Route>
      </Routes>
    </Suspense>
  </BrowserRouter>
);
