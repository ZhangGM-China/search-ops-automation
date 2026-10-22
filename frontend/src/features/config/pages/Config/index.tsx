import React, { useState, useEffect } from 'react';
import { isPlatformAdmin } from '@/shared/lib/permissions';
import { Form, Button, message, Spin, Space } from 'antd';
import {
  Form as MobForm,
  Button as MobButton,
  Space as MobSpace,
  DotLoading
} from 'antd-mobile';
import { adminApi } from '@/shared/api';
import type { AppConfig, BrandConfig } from '@/shared/types';
import SafetyConfigSection from './components/SafetyConfigSection';
import BrandConfigSection from './components/BrandConfigSection';
import useMobile from '@/shared/hooks/useMobile';
import ConfigWorkspaceShell from './ConfigWorkspaceShell';
import ConfigContextActions from './components/ConfigContextActions';
import { defaultRelevanceBuckets } from './components/brandConfigOptions';

const brandOrder = [
  { name: 'ATLAS', key: 'atlas' },
  { name: 'Meadow', key: 'meadow' },
  { name: 'Harbor', key: 'harbor' },
  { name: 'Summit', key: 'summit' },
  { name: 'Riviera', key: 'riviera' },
  { name: 'Lumen', key: 'lumen' },
];
const brandLabelMap = new Map(brandOrder.map((item) => [item.key, item.name]));

const normalizeConfig = (value: AppConfig): AppConfig => ({
  ...value,
  safety_config: value.safety_config ? { ...value.safety_config } : undefined,
  brand_configs: Object.fromEntries(
    Object.entries(value.brand_configs ?? {}).map(([brandKey, brandConfig]) => {
      return [
        brandKey,
        {
          ...brandConfig,
          query_governance_query_min_metric_type: brandConfig.query_governance_query_min_metric_type ?? 'uv',
          query_governance_query_min_metric_value: brandConfig.query_governance_query_min_metric_value ?? brandConfig.query_governance_query_min_pv ?? 5,
          query_governance_excluded_keywords: brandConfig.query_governance_excluded_keywords?.length
            ? Array.from(new Set(brandConfig.query_governance_excluded_keywords.map((item) => item.trim()).filter(Boolean)))
            : [],
          query_governance_relevance_enabled: brandConfig.query_governance_relevance_enabled ?? true,
          query_governance_relevance_buckets: brandConfig.query_governance_relevance_buckets?.length
            ? Array.from(new Set(brandConfig.query_governance_relevance_buckets))
            : [...defaultRelevanceBuckets],
          query_governance_preference_generation_timeout_seconds: brandConfig.query_governance_preference_generation_timeout_seconds ?? 1200,
          query_governance_kg_agent_timeout_seconds: brandConfig.query_governance_kg_agent_timeout_seconds ?? 1200,
          query_governance_relevance_model_timeout_seconds: brandConfig.query_governance_relevance_model_timeout_seconds ?? 1200,
          top_query_inventory_max_cycles: brandConfig.top_query_inventory_max_cycles ?? 3,
        },
      ];
    }),
  ),
});

const buildSubmitConfig = (
  baseConfig: AppConfig,
  draftConfig: AppConfig,
  visibleBrandKeys: string[],
): AppConfig => ({
  ...(baseConfig.safety_config ? {
    safety_config: (() => {
      const safetyConfig = normalizeConfig({
        safety_config: draftConfig.safety_config ?? baseConfig.safety_config,
        brand_configs: {},
      }).safety_config;
      if (!safetyConfig) return safetyConfig;
      return Object.fromEntries(
        Object.entries(safetyConfig).filter(([key]) => !['max_daily_adjustments', 'assistant_launcher_mode'].includes(key)),
      ) as typeof safetyConfig;
    })(),
  } : {}),
  brand_configs: Object.fromEntries(
    visibleBrandKeys.map((brandKey) => {
      const mergedBrandConfig = {
        ...(baseConfig.brand_configs?.[brandKey] ?? {}),
        ...(draftConfig.brand_configs?.[brandKey] ?? {}),
      };
      const retiredConfigKeys = new Set([
        'query_governance_query_min_pv',
        'query_governance_preference_behavior_evidence_fallback_days',
        'query_governance_preference_generation_max_completion_tokens',
        'query_governance_vector_reorder_timeout_seconds',
        'query_governance_relevance_request_max_bytes',
        'query_governance_relevance_max_completion_tokens',
        'top_query_metric_drop_threshold_percent',
        'top_query_metric_drop_thresholds',
        'top_query_world_knowledge_config',
      ]);
      const editableBrandConfig = Object.fromEntries(
        Object.entries(mergedBrandConfig).filter(([key]) => !retiredConfigKeys.has(key)),
      );
      const normalizedBrandConfig = normalizeConfig({
        brand_configs: {
          [brandKey]: editableBrandConfig as unknown as BrandConfig,
        },
      }).brand_configs[brandKey];
      return [
        brandKey,
        {
          ...normalizedBrandConfig,
        },
      ];
    }),
  ),
});

const buildSafetySubmitConfig = (baseConfig: AppConfig, draftConfig: AppConfig): AppConfig => ({
  safety_config: buildSubmitConfig(baseConfig, draftConfig, []).safety_config,
  brand_configs: {},
});

const LegacySystemConfigPage: React.FC<{ onDirtyChange: (dirty: boolean) => void; scope?: 'brand' | 'safety' }> = ({ onDirtyChange, scope = 'brand' }) => {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [legacyDirty, setLegacyDirty] = useState(false);
  const [selectedBrandKey, setSelectedBrandKey] = useState('atlas');
  const [form] = Form.useForm();
  const [mobForm] = MobForm.useForm();
  const isMobile = useMobile();
  const currentUser = (() => {
    const raw = localStorage.getItem('user');
    if (!raw) return null;
    try {
      return JSON.parse(raw) as { username?: string; isAdmin?: boolean; role?: string; actionPermissions?: string[] };
    } catch {
      return null;
    }
  })();
  const canEditConfig = !currentUser || Boolean(
    isPlatformAdmin(currentUser)
      || currentUser?.actionPermissions?.includes('manage_platform_config')
      || currentUser?.actionPermissions?.includes('manage_brand_config'),
  );
  const hasSafetyConfig = Boolean(config?.safety_config && Object.keys(config.safety_config).length > 0);

  useEffect(() => {
    onDirtyChange(legacyDirty);
  }, [legacyDirty, onDirtyChange]);

  const fetchConfig = React.useCallback(async (preferredBrandKey?: string) => {
    setLoading(true);
    try {
      const response = await adminApi.getConfig();
      const nextConfig = normalizeConfig(response.data);
      setConfig(nextConfig);
      const firstBrandKey = brandOrder.find(brand => nextConfig.brand_configs?.[brand.key])?.key ?? Object.keys(nextConfig.brand_configs ?? {})[0] ?? 'atlas';
      const nextBrandKey = preferredBrandKey && nextConfig.brand_configs?.[preferredBrandKey] ? preferredBrandKey : firstBrandKey;
      setSelectedBrandKey(nextBrandKey);
      if (isMobile) mobForm.setFieldsValue(nextConfig);
      else form.setFieldsValue(nextConfig);
      setLegacyDirty(false);
    } catch {
      message.error('获取配置失败');
    } finally {
      setLoading(false);
    }
  }, [form, isMobile, mobForm]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      fetchConfig();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [fetchConfig]);

  const saveConfig = async (preferredBrandKey = selectedBrandKey, keepEditing = false): Promise<boolean> => {
    if (!config) return false;
    const rawValues = (isMobile ? mobForm.getFieldsValue(true) : form.getFieldsValue(true)) as AppConfig;
    const visibleBrandKeys = [
      ...brandOrder.filter((brand) => config.brand_configs?.[brand.key]).map((brand) => brand.key),
      ...Object.keys(config.brand_configs ?? {}).filter((brandKey) => !brandLabelMap.has(brandKey)),
    ];
    const nextConfig = scope === 'safety'
      ? buildSafetySubmitConfig(config, rawValues)
      : buildSubmitConfig({ brand_configs: config.brand_configs }, rawValues, visibleBrandKeys);
    setSaving(true);
    try {
      await adminApi.updateConfig(nextConfig);
      message.success('配置已更新并即时生效');
      setIsEditing(keepEditing);
      setLegacyDirty(false);
      await fetchConfig(preferredBrandKey);
      return true;
    } catch {
      message.error('更新配置失败');
      return false;
    } finally {
      setSaving(false);
    }
  };

  const onFinish = async () => { await saveConfig(selectedBrandKey); };
  const saveBeforeBrandChange = async () => saveConfig(selectedBrandKey, true);

  const handleCancel = () => {
    if (isMobile) mobForm.setFieldsValue(config);
    else form.setFieldsValue(config);
    setIsEditing(false);
    setLegacyDirty(false);
  };

  const handleSelectedBrandChange = (nextBrand: string) => {
    setSelectedBrandKey(nextBrand);
  };

  if (loading || !config) {
    return (
      <div style={{ textAlign: 'center', padding: '50px' }}>
        {isMobile ? <DotLoading color='primary' /> : <Spin size="large" />}
      </div>
    );
  }

  const visibleBrands = [
    ...brandOrder.filter((brand) => config.brand_configs?.[brand.key]),
    ...Object.keys(config.brand_configs ?? {})
      .filter((brandKey) => !brandLabelMap.has(brandKey))
      .map((brandKey) => ({ key: brandKey, name: brandKey })),
  ];
  const activeBrandKey = visibleBrands.some((brand) => brand.key === selectedBrandKey)
    ? selectedBrandKey
    : (visibleBrands[0]?.key ?? 'atlas');
  const updateCheckMode = (checkKey: 'recall' | 'inventory', mode: string) => {
    const formInstance = isMobile ? mobForm : form;
    const brandConfigs = formInstance.getFieldValue('brand_configs') ?? {};
    const brandConfig = brandConfigs[activeBrandKey] ?? {};
    const checkModes = brandConfig.top_query_check_modes ?? {};
    formInstance.setFieldsValue({
      brand_configs: {
        ...brandConfigs,
        [activeBrandKey]: {
          ...brandConfig,
          top_query_check_modes: { ...checkModes, [checkKey]: mode },
        },
      },
    });
    if (isEditing) setLegacyDirty(true);
  };

  const renderMobileView = () => (
    <div className="ops-page" style={{ paddingBottom: '20px' }}>
      <ConfigContextActions>
        {!isEditing ? (
          <MobButton size='small' color='primary' disabled={!canEditConfig} onClick={() => { setIsEditing(true); setLegacyDirty(false); }}>修改配置</MobButton>
        ) : (
          <MobSpace>
            <MobButton size='small' onClick={handleCancel}>取消</MobButton>
            <MobButton size='small' color='primary' loading={saving} onClick={() => mobForm.submit()}>保存</MobButton>
          </MobSpace>
        )}
      </ConfigContextActions>
        <MobForm
          form={mobForm}
          layout='vertical'
          onFinish={onFinish}
          initialValues={config}
          onValuesChange={() => { if (isEditing) setLegacyDirty(true); }}
        >
          {scope === 'brand' ? <div id="brand-config" style={{ scrollMarginTop: 120 }}>
          <BrandConfigSection
            key={activeBrandKey}
            disabled={!isEditing}
            isEditing={isEditing}
            isDirty={legacyDirty}
            brands={visibleBrands}
            enabledByBrand={Object.fromEntries(visibleBrands.map((brand) => [brand.key, Boolean(config.brand_configs?.[brand.key]?.enabled)]))}
            selectedBrandKey={activeBrandKey}
            selectedBrandConfig={config.brand_configs?.[activeBrandKey]}
            onCheckModeChange={updateCheckMode}
            onSelectedBrandKeyChange={handleSelectedBrandChange}
            onSaveBeforeBrandChange={saveBeforeBrandChange}
          />
          </div> : hasSafetyConfig ? <SafetyConfigSection disabled={!isEditing} /> : null}
      </MobForm>
    </div>
  );

  const renderDesktopView = () => (
    <div className="ops-page" style={{ maxWidth: 1360, margin: '0 auto', position: 'relative' }}>
      <div style={{ minWidth: 0 }}>
          <ConfigContextActions>
            {!isEditing ? (
              <Button type="primary" size="small" disabled={!canEditConfig} onClick={() => { setIsEditing(true); setLegacyDirty(false); }}>修改配置</Button>
            ) : (
              <Space size="small">
                <Button size="small" onClick={handleCancel}>取消</Button>
                <Button type="primary" size="small" loading={saving} onClick={() => form.submit()}>确认修改</Button>
              </Space>
            )}
          </ConfigContextActions>

          <Form
            form={form}
            layout="vertical"
            onFinish={onFinish}
            initialValues={config}
            size="small"
            onValuesChange={() => { if (isEditing) setLegacyDirty(true); }}
          >
            {scope === 'brand' ? <div id="brand-config" style={{ scrollMarginTop: 120 }}>
              <BrandConfigSection
                key={activeBrandKey}
                disabled={!isEditing}
                isEditing={isEditing}
                isDirty={legacyDirty}
                brands={visibleBrands}
                selectedBrandKey={activeBrandKey}
                selectedBrandConfig={config.brand_configs?.[activeBrandKey]}
                onCheckModeChange={updateCheckMode}
                onSelectedBrandKeyChange={handleSelectedBrandChange}
                onSaveBeforeBrandChange={saveBeforeBrandChange}
              />
            </div> : <div id="system-safety" style={{ scrollMarginTop: 120 }}>
              {hasSafetyConfig ? <SafetyConfigSection disabled={!isEditing} /> : null}
            </div>}
          </Form>
      </div>
    </div>
  );

  return isMobile ? renderMobileView() : renderDesktopView();
};

const ConfigPage: React.FC = () => (
  <ConfigWorkspaceShell
    renderSystem={({ onDirtyChange }) => <LegacySystemConfigPage onDirtyChange={onDirtyChange} />}
    renderSafety={({ onDirtyChange }) => <LegacySystemConfigPage scope="safety" onDirtyChange={onDirtyChange} />}
  />
);

export default ConfigPage;
