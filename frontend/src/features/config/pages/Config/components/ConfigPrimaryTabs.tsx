import React from 'react';
import { Tabs } from 'antd';
import { Tabs as MobileTabs } from 'antd-mobile';
import useMobile from '@/shared/hooks/useMobile';

export type ConfigPrimaryTab = 'system' | 'model-config' | 'safety' | 'page-config' | 'ab-test';

interface Props {
  activeKey: ConfigPrimaryTab;
  onChange: (key: ConfigPrimaryTab) => void;
  showSystem: boolean;
  showModel: boolean;
  showSafety: boolean;
  showPage: boolean;
}

const ConfigPrimaryTabs: React.FC<Props> = ({ activeKey, onChange, showSystem, showModel, showSafety, showPage }) => {
  const isMobile = useMobile();
  if (isMobile) {
    return (
      <MobileTabs activeKey={activeKey} onChange={(key) => onChange(key as ConfigPrimaryTab)}>
        {showSystem ? <MobileTabs.Tab title="品牌配置" key="system" /> : null}
        {showModel ? <MobileTabs.Tab title="模型配置" key="model-config" /> : null}
        {showPage ? <MobileTabs.Tab title="页面配置" key="page-config" /> : null}
        {showSafety ? <MobileTabs.Tab title="安全配置" key="safety" /> : null}
        <MobileTabs.Tab title="A/B Test" key="ab-test" />
      </MobileTabs>
    );
  }
  return (
    <Tabs
      activeKey={activeKey}
      onChange={(key) => onChange(key as ConfigPrimaryTab)}
      items={[
        ...(showSystem ? [{ key: 'system', label: '品牌配置' }] : []),
        ...(showModel ? [{ key: 'model-config', label: '模型配置' }] : []),
        ...(showPage ? [{ key: 'page-config', label: '页面配置' }] : []),
        ...(showSafety ? [{ key: 'safety', label: '安全配置' }] : []),
        { key: 'ab-test', label: 'A/B Test' },
      ]}
    />
  );
};

export default ConfigPrimaryTabs;
