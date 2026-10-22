import React from 'react';
import { isPlatformAdmin } from '@/shared/lib/permissions';
import { Result, Spin } from 'antd';
import ConfigPrimaryTabs, { type ConfigPrimaryTab } from './components/ConfigPrimaryTabs';
import ModelConfigTab from './components/ModelConfigTab';
import PageConfigTab from './components/PageConfigTab';
const ExperimentTab = React.lazy(() => import('./components/ExperimentTab'));
type ExperimentView = 'config' | 'effect';

const brandOrder = [
  { name: 'ATLAS', key: 'atlas' },
  { name: 'Meadow', key: 'meadow' },
  { name: 'Harbor', key: 'harbor' },
  { name: 'Summit', key: 'summit' },
  { name: 'Riviera', key: 'riviera' },
  { name: 'Lumen', key: 'lumen' },
];

interface Props {
  renderSystem: (props: { onDirtyChange: (dirty: boolean) => void }) => React.ReactNode;
  renderSafety: (props: { onDirtyChange: (dirty: boolean) => void }) => React.ReactNode;
}

const SystemConfigRenderer: React.FC<{ renderSystem: Props['renderSystem']; onDirtyChange: (dirty: boolean) => void }> = ({ renderSystem, onDirtyChange }) => (
  <>{renderSystem({ onDirtyChange })}</>
);

const readCurrentUser = () => {
  const raw = localStorage.getItem('user');
  if (!raw) return null;
  try {
    return JSON.parse(raw) as { username?: string; isAdmin?: boolean; role?: string; actionPermissions?: string[]; brandPermissions?: string[] };
  } catch {
    return null;
  }
};

const updateUrlParams = (values: Record<string, string | undefined>, mode: 'push' | 'replace' = 'push') => {
  const url = new URL(window.location.href);
  Object.entries(values).forEach(([key, value]) => value ? url.searchParams.set(key, value) : url.searchParams.delete(key));
  const nextLocation = `${url.pathname}${url.search}${url.hash}`;
  window.history[mode === 'push' ? 'pushState' : 'replaceState']({}, '', nextLocation);
  return nextLocation;
};

const currentLocation = () => `${window.location.pathname}${window.location.search}${window.location.hash}`;

const ConfigWorkspaceShell: React.FC<Props> = ({ renderSystem, renderSafety }) => {
  const params = new URLSearchParams(window.location.search);
  const currentUser = readCurrentUser();
  const hasBrandPermissionField = Array.isArray(currentUser?.brandPermissions);
  const platformAdmin = Boolean(currentUser && isPlatformAdmin(currentUser));
  const availableBrands = hasBrandPermissionField && !platformAdmin ? brandOrder.filter((brand) => currentUser?.brandPermissions?.includes(brand.key)) : brandOrder;
  const initialBrand = params.get('brand');
  const canManage = !currentUser || Boolean(platformAdmin || currentUser.actionPermissions?.includes('manage_brand_config'));
  const canManageModel = !currentUser || Boolean(platformAdmin || currentUser.actionPermissions?.includes('manage_platform_config'));
  const canManageSafety = canManageModel;
  const resolveTab = React.useCallback((requestedTab: string | null): ConfigPrimaryTab => {
    if (requestedTab === 'model-config' && canManageModel) return 'model-config';
    if (requestedTab === 'safety' && canManageSafety) return 'safety';
    if (requestedTab === 'page-config' && canManageModel) return 'page-config';
    if (requestedTab === 'ab-test') return 'ab-test';
    if (canManage) return 'system';
    if (canManageModel) return 'model-config';
    return 'ab-test';
  }, [canManage, canManageModel, canManageSafety]);
  const [activeTab, setActiveTab] = React.useState<ConfigPrimaryTab>(() => resolveTab(params.get('tab')));
  const [brand, setBrand] = React.useState(availableBrands.some((item) => item.key === initialBrand) ? String(initialBrand) : (availableBrands[0]?.key ?? 'atlas'));
  const rawExperimentId = Number(params.get('experimentId'));
  const [experimentId, setExperimentId] = React.useState<number | undefined>(Number.isFinite(rawExperimentId) && rawExperimentId > 0 ? rawExperimentId : undefined);
  const [experimentView, setExperimentView] = React.useState<ExperimentView>(params.get('view') === 'effect' ? 'effect' : 'config');
  const brandRef = React.useRef(brand);
  const acceptedLocationRef = React.useRef(currentLocation());
  const experimentDirtyRef = React.useRef(false);
  const systemDirtyRef = React.useRef(false);
  const safetyDirtyRef = React.useRef(false);
  const modelDirtyRef = React.useRef(false);
  const pageDirtyRef = React.useRef(false);

  const handleExperimentDirtyChange = React.useCallback((dirty: boolean) => { experimentDirtyRef.current = dirty; }, []);
  const handleSystemDirtyChange = React.useCallback((dirty: boolean) => { systemDirtyRef.current = dirty; }, []);
  const handleSafetyDirtyChange = React.useCallback((dirty: boolean) => { safetyDirtyRef.current = dirty; }, []);
  const handleModelDirtyChange = React.useCallback((dirty: boolean) => { modelDirtyRef.current = dirty; }, []);
  const handlePageDirtyChange = React.useCallback((dirty: boolean) => { pageDirtyRef.current = dirty; }, []);
  const confirmNavigation = React.useCallback((scope: 'system' | 'safety' | 'model' | 'page' | 'experiment', messageText: string) => {
    const dirty = scope === 'system'
      ? systemDirtyRef.current
      : scope === 'safety'
        ? safetyDirtyRef.current
        : scope === 'model'
          ? modelDirtyRef.current
          : scope === 'page'
            ? pageDirtyRef.current
            : experimentDirtyRef.current;
    return !dirty || window.confirm(messageText);
  }, []);
  const clearDirty = React.useCallback((scope: 'system' | 'safety' | 'model' | 'page' | 'experiment') => {
    if (scope === 'system') systemDirtyRef.current = false;
    else if (scope === 'safety') safetyDirtyRef.current = false;
    else if (scope === 'model') modelDirtyRef.current = false;
    else if (scope === 'page') pageDirtyRef.current = false;
    else experimentDirtyRef.current = false;
  }, []);

  const acceptUrlUpdate = React.useCallback((values: Record<string, string | undefined>, mode: 'push' | 'replace' = 'push') => {
    acceptedLocationRef.current = updateUrlParams(values, mode);
  }, []);

  React.useEffect(() => {
    const handlePopState = () => {
      const scope = activeTab === 'system' ? 'system' : activeTab === 'safety' ? 'safety' : activeTab === 'model-config' ? 'model' : activeTab === 'page-config' ? 'page' : 'experiment';
      if (!confirmNavigation(scope, '当前页面存在未保存的修改，离开后将丢失。是否继续？')) {
        window.history.pushState({}, '', acceptedLocationRef.current);
        return;
      }
      clearDirty(scope);
      const nextParams = new URLSearchParams(window.location.search);
      const nextTab = resolveTab(nextParams.get('tab'));
      const requestedBrand = nextParams.get('brand');
      const nextBrand = availableBrands.some((item) => item.key === requestedBrand) ? String(requestedBrand) : (availableBrands[0]?.key ?? 'atlas');
      const nextRawExperimentId = Number(nextParams.get('experimentId'));
      const nextExperimentId = Number.isFinite(nextRawExperimentId) && nextRawExperimentId > 0 ? nextRawExperimentId : undefined;
      brandRef.current = nextBrand;
      setActiveTab(nextTab);
      setBrand(nextBrand);
      setExperimentId(nextExperimentId);
      setExperimentView(nextParams.get('view') === 'effect' ? 'effect' : 'config');
      acceptedLocationRef.current = currentLocation();
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, [activeTab, availableBrands, clearDirty, confirmNavigation, resolveTab]);

  const handleTabChange = (nextTab: ConfigPrimaryTab) => {
    if (nextTab === activeTab) return;
    const scope = activeTab === 'system' ? 'system' : activeTab === 'safety' ? 'safety' : activeTab === 'model-config' ? 'model' : activeTab === 'page-config' ? 'page' : 'experiment';
    if (!confirmNavigation(scope, '当前页面存在未保存的修改，离开后将丢失。是否继续？')) return;
    clearDirty(scope);
    setActiveTab(nextTab);
    acceptUrlUpdate({ tab: nextTab, brand: nextTab === 'ab-test' ? brand : undefined, experimentId: nextTab === 'ab-test' && experimentId ? String(experimentId) : undefined, view: nextTab === 'ab-test' && experimentView === 'effect' ? 'effect' : undefined });
  };

  const handleBrandChange = (nextBrand: string) => {
    if (!confirmNavigation('experiment', '实验草稿存在未保存的修改，切换品牌后将丢失。是否继续？')) return;
    clearDirty('experiment');
    brandRef.current = nextBrand;
    setBrand(nextBrand);
    setExperimentId(undefined);
    setExperimentView('config');
    acceptUrlUpdate({ tab: 'ab-test', brand: nextBrand, experimentId: undefined, view: undefined });
  };

  const handleExperimentChange = React.useCallback((nextExperimentId?: number, mode: 'push' | 'replace' = 'push') => {
    setExperimentId(nextExperimentId);
    setExperimentView('config');
    acceptUrlUpdate({ tab: 'ab-test', brand: brandRef.current, experimentId: nextExperimentId ? String(nextExperimentId) : undefined, view: undefined }, mode);
  }, [acceptUrlUpdate, setExperimentId, setExperimentView]);

  const handleExperimentViewChange = React.useCallback((nextView: ExperimentView) => {
    setExperimentView(nextView);
    acceptUrlUpdate({ view: nextView === 'effect' ? 'effect' : undefined }, 'replace');
  }, [acceptUrlUpdate, setExperimentView]);

  const confirmExperimentNavigation = React.useCallback(() => confirmNavigation(
    'experiment',
    '实验草稿存在未保存的修改，离开后将丢失。是否继续？',
  ), [confirmNavigation]);

  return (
    <div className="config-workspace-shell">
      <nav className="config-primary-header" aria-label="配置页次级导航">
        <div id="config-context-brand" className="config-primary-header__brand" />
        <ConfigPrimaryTabs activeKey={activeTab} onChange={handleTabChange} showSystem={canManage} showModel={canManageModel} showSafety={canManageSafety} showPage={canManageModel} />
        <div id="config-context-actions" className="config-primary-header__actions" />
      </nav>
      <div className="ops-page config-workspace">
        {availableBrands.length === 0 && activeTab !== 'model-config' && activeTab !== 'safety' && activeTab !== 'page-config' ? <Result status="403" title="当前账号未分配品牌权限" /> : activeTab === 'system' ? <SystemConfigRenderer renderSystem={renderSystem} onDirtyChange={handleSystemDirtyChange} /> : activeTab === 'safety' ? <SystemConfigRenderer renderSystem={renderSafety} onDirtyChange={handleSafetyDirtyChange} /> : (
          activeTab === 'model-config' ? <ModelConfigTab onDirtyChange={handleModelDirtyChange} /> : activeTab === 'page-config' ? <PageConfigTab onDirtyChange={handlePageDirtyChange} /> : <React.Suspense fallback={<div className="experiment-center-state"><Spin /></div>}><ExperimentTab brand={brand} brands={availableBrands} canManage={canManage} experimentId={experimentId} view={experimentView} onBrandChange={handleBrandChange} onExperimentChange={handleExperimentChange} onViewChange={handleExperimentViewChange} onDraftDirtyChange={handleExperimentDirtyChange} confirmNavigation={confirmExperimentNavigation} /></React.Suspense>
        )}
      </div>
    </div>
  );
};

export default ConfigWorkspaceShell;
