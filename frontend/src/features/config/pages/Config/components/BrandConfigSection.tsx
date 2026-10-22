import React from 'react';
import { Button, Collapse, Form, Input, InputNumber, Modal, Select, Switch, Tooltip } from 'antd';
import { InfoCircleOutlined, LeftOutlined, RightOutlined } from '@ant-design/icons';
import { Form as MobForm, Selector as MobSelector, Stepper as MobStepper, Switch as MobSwitch } from 'antd-mobile';
import useMobile from '@/shared/hooks/useMobile';
import type { BrandConfig } from '@/shared/types';
import ConfigContextActions from './ConfigContextActions';
import {
  basicFields,
  recallRateRuntimeFields,
  relevanceBucketOptions,
  executionModeOptions,
  inventoryFields,
  kgAgentRuntimeFields,
  noResultFields,
  queryBehaviorEvidenceFields,
  querySelectionFields,
  recallFields,
  relevanceRuntimeFields,
  topQueryChecks,
  topQueryModeOptions,
  topQueryTools,
  vectorReorderRuntimeFields,
} from './brandConfigOptions';

interface BrandItem { name: string; key: string }
interface Props {
  disabled?: boolean;
  brands: BrandItem[];
  selectedBrandKey: string;
  enabledByBrand?: Record<string, boolean>;
  selectedBrandConfig?: BrandConfig;
  onCheckModeChange?: (checkKey: 'recall' | 'inventory', mode: string) => void;
  onSelectedBrandKeyChange: (brandKey: string) => void;
  isEditing?: boolean;
  isDirty?: boolean;
  onSaveBeforeBrandChange?: (brandKey: string) => Promise<boolean>;
  experimentPanel?: React.ReactNode;
}

const fieldLabel = (label: string, tip: string) => (
  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
    <span>{label}</span>
    <Tooltip title={tip}>
      <InfoCircleOutlined aria-label={`${label}提示`} className="ops-tooltip-icon" style={{ color: '#8c8c8c' }} />
    </Tooltip>
  </span>
);

interface BrandSwitcherProps {
  brands: BrandItem[];
  selectedBrandKey: string;
  enabledByBrand: Record<string, boolean>;
  onChange: (brandKey: string) => void;
}

const BrandSwitcher: React.FC<BrandSwitcherProps> = ({ brands, selectedBrandKey, enabledByBrand, onChange }) => {
  const selectedIndex = Math.max(0, brands.findIndex((brand) => brand.key === selectedBrandKey));
  const selectedBrand = brands[selectedIndex] ?? brands[0];
  if (!selectedBrand) return null;

  const renderOptionLabel = (brand: BrandItem) => (
    <span className="ops-brand-switcher__option">
      <span className={`ops-brand-switcher__status${enabledByBrand[brand.key] ? ' is-enabled' : ' is-disabled'}`} aria-hidden="true" />
      <span>{brand.name}</span>
    </span>
  );

  return (
    <div className="ops-brand-switcher" aria-label="品牌配置切换">
      <Button
        type="text"
        size="small"
        icon={<LeftOutlined />}
        aria-label="上一个品牌"
        disabled={selectedIndex <= 0}
        onClick={() => { const previous = brands[selectedIndex - 1]; if (previous) onChange(previous.key); }}
      />
      <Select
        size="small"
        aria-label="当前品牌"
        value={selectedBrand.key}
        suffixIcon={null}
        options={brands.map((brand) => ({ value: brand.key, label: renderOptionLabel(brand) }))}
        onChange={onChange}
        optionRender={(option) => option.label}
        labelRender={() => renderOptionLabel(selectedBrand)}
        className="ops-brand-switcher__select"
      />
      <Button
        type="text"
        size="small"
        icon={<RightOutlined />}
        aria-label="下一个品牌"
        disabled={selectedIndex >= brands.length - 1}
        onClick={() => { const next = brands[selectedIndex + 1]; if (next) onChange(next.key); }}
      />
    </div>
  );
};

const BrandConfigSection: React.FC<Props> = ({ disabled, brands, selectedBrandKey, enabledByBrand = {}, selectedBrandConfig, onCheckModeChange, onSelectedBrandKeyChange, isEditing = false, isDirty = false, onSaveBeforeBrandChange, experimentPanel }) => {
  const isMobile = useMobile();
  const selectedBrand = brands.find(brand => brand.key === selectedBrandKey) ?? brands[0];
  const [featureState, setFeatureState] = React.useState(() => ({
    brand: selectedBrandConfig?.enabled !== false,
    query: selectedBrandConfig?.top_query_governance_enabled !== false,
    reorder: selectedBrandConfig?.query_governance_reorder_enabled !== false,
    relevance: selectedBrandConfig?.query_governance_relevance_enabled !== false,
    noResult: selectedBrandConfig?.no_result_governance_enabled === true,
    recall: selectedBrandConfig?.top_query_check_modes?.recall !== 'disabled',
    recallRate: selectedBrandConfig?.query_governance_recall_rate_enabled === true,
    inventory: selectedBrandConfig?.top_query_check_modes?.inventory !== 'disabled',
    relevanceMode: selectedBrandConfig?.top_query_check_modes?.relevance !== 'disabled',
  }));
  const previousCheckModes = React.useRef({
    recall: selectedBrandConfig?.top_query_check_modes?.recall ?? 'human_approval',
    inventory: selectedBrandConfig?.top_query_check_modes?.inventory ?? 'human_approval',
  });

  const setCheckMode = (checkKey: 'recall' | 'inventory', enabled: boolean) => {
    const mode = enabled ? (previousCheckModes.current[checkKey] === 'disabled' ? 'human_approval' : previousCheckModes.current[checkKey]) : 'disabled';
    if (enabled) previousCheckModes.current[checkKey] = mode;
    setFeatureState((current) => ({ ...current, [checkKey]: enabled }));
    onCheckModeChange?.(checkKey, mode);
  };

  const renderNumberField = (brandKey: string, item: { key: string; label: string; tip: string; min?: number; max?: number; step?: number; path?: string[]; legacyLabel?: string }, mobile: boolean) => {
    const Item = mobile ? MobForm.Item : Form.Item;
    return (
      <Item key={item.key} name={['brand_configs', brandKey, ...(item.path ?? [item.key])]} label={fieldLabel(item.label, item.tip)}>
        {mobile ? <MobStepper aria-label={item.label} disabled={disabled} min={item.min} max={item.max} step={item.step} /> : <InputNumber aria-label={item.label} data-legacy-label={item.legacyLabel} disabled={disabled} min={item.min} max={item.max} step={item.step} style={{ width: '100%' }} />}
      </Item>
    );
  };

  const renderModeField = (brandKey: string, item: { key: string; label: string; tip: string }, options: Array<{ value: string; label: string }>, mobile: boolean, modePath: string) => {
    const Item = mobile ? MobForm.Item : Form.Item;
    const handleChange = modePath === 'top_query_check_modes' && (item.key === 'recall' || item.key === 'inventory')
      ? (value: string) => {
        previousCheckModes.current[item.key as 'recall' | 'inventory'] = value;
        setFeatureState((current) => ({ ...current, [item.key]: value !== 'disabled' }));
      }
      : modePath === 'top_query_check_modes' && item.key === 'relevance'
        ? (value: string) => setFeatureState((current) => ({ ...current, relevanceMode: value !== 'disabled' }))
      : undefined;
    return (
      <Item key={item.key} name={['brand_configs', brandKey, modePath, item.key]} label={fieldLabel(item.label, item.tip)}>
        {mobile ? <MobSelector disabled={disabled} options={options} onChange={(values) => handleChange?.(String(values[0] ?? 'disabled'))} /> : <Select disabled={disabled} options={options} onChange={handleChange} />}
      </Item>
    );
  };

  const renderMetricFloor = (brandKey: string, mobile: boolean) => {
    const Item = mobile ? MobForm.Item : Form.Item;
    const tip = '取词时使用的最低日搜索指标。默认选择 UV：只有当天搜索用户数达到阈值的 Query 才进入候选；如业务更关注访问次数，可切换为 PV。数量示例：选择 UV 并填写 5，表示当天至少有 5 个搜索用户的 Query 才会进入取词候选。';
    return (
      <div className="ops-config-composite-field">
        {mobile ? <Item label={fieldLabel('最低日搜索指标', tip)}>
          <div className="ops-config-metric-controls">
            <Item name={['brand_configs', brandKey, 'query_governance_query_min_metric_type']} label={null}>
              <Select aria-label="最低日搜索指标" disabled={disabled} options={[{ value: 'uv', label: 'UV' }, { value: 'pv', label: 'PV' }]} />
            </Item>
            <Item name={['brand_configs', brandKey, 'query_governance_query_min_metric_value']} label={null}>
              <MobStepper aria-label="最低指标数量" disabled={disabled} min={0} max={1000000} />
            </Item>
          </div>
        </Item> : <Form.Item label={fieldLabel('最低日搜索指标', tip)}>
          <div className="ops-config-metric-controls">
            <Form.Item noStyle name={['brand_configs', brandKey, 'query_governance_query_min_metric_type']}>
              <Select aria-label="最低日搜索指标" disabled={disabled} options={[{ value: 'uv', label: 'UV' }, { value: 'pv', label: 'PV' }]} style={{ width: 112 }} />
            </Form.Item>
            <Form.Item noStyle name={['brand_configs', brandKey, 'query_governance_query_min_metric_value']}>
              <InputNumber aria-label="最低指标数量" disabled={disabled} min={0} max={1000000} style={{ flex: 1, width: '100%' }} />
            </Form.Item>
          </div>
        </Form.Item>}
      </div>
    );
  };

  const renderGrowthRules = (brandKey: string, mobile: boolean) => {
    const Item = mobile ? MobForm.Item : Form.Item;
  const input = (key: string, label: string, tip: string, max: number, step: number, placeholder: string) => (
      <Tooltip key={key} title={tip}>
        <Item name={['brand_configs', brandKey, key]} noStyle>
          {mobile ? <MobStepper aria-label={label} disabled={disabled} min={0} max={max} step={step} /> : <InputNumber aria-label={label} placeholder={placeholder} disabled={disabled} min={0} max={max} step={step} className="ops-config-growth-input" />}
        </Item>
      </Tooltip>
    );
    return (
      <Item className="ops-config-growth-field" label={fieldLabel('取高增长 Query 规则', '当前日 PV / 历史平均日 PV ≥ max(比例下限，1 + 显著性放大系数 ÷ √历史平均日 PV)，且当前日 PV − 历史平均日 PV ≥ max(绝对增长下限，显著性放大系数 × √历史平均日 PV)。例：比例 1.2、系数 2、绝对增长 5。')}>
        <div className="ops-config-growth-formula">
          <div className="ops-config-growth-formula__row">
            <span>PV比例 ≥ max(</span>
            {input('query_governance_candidate_pv_growth_ratio_floor', '增长比例下限', '当前日 PV ÷ 历史平均日 PV 的最低比例。', 100, 0.1, '比例')}
            <span>，1 +</span>
            {input('query_governance_candidate_pv_growth_sqrt_multiplier', '显著性放大系数', '用历史波动平方根放大显著性判断的系数。', 100, 0.1, '系数')}
            <span>÷ √历史 PV)；ΔPV ≥ max(</span>
            {input('query_governance_candidate_pv_growth_abs_floor', '绝对增长下限', '当前日 PV 至少比历史平均多出的绝对数量。', 1000000, 1, '绝对值')}
            <span>，系数 × √历史 PV)</span>
          </div>
        </div>
      </Item>
    );
  };

  const requestBrandChange = (nextBrand: string) => {
    if (!nextBrand || nextBrand === selectedBrand?.key) return;
    if (!isEditing || !isDirty) {
      onSelectedBrandKeyChange(nextBrand);
      return;
    }

    Modal.confirm({
      title: '配置有未保存变更',
      content: '当前品牌的配置项已被修改，切换品牌前是否先保存？',
      okText: '保存并切换',
      cancelText: '取消',
      onOk: async () => {
        const saved = await onSaveBeforeBrandChange?.(nextBrand);
        if (saved !== false) onSelectedBrandKeyChange(nextBrand);
      },
    });
  };

  const renderBrandFields = (brandKey: string, mobile: boolean) => {
    const Item = mobile ? MobForm.Item : Form.Item;
    const SwitchControl = mobile ? MobSwitch : Switch;
    const section = (title: string, children: React.ReactNode, id?: string) => (
      <div id={id} className="ops-config-section" style={id ? { scrollMarginTop: 120 } : undefined}>
        <div className="ops-config-section__title">{title}</div>
        <div className="ops-config-field-grid">{children}</div>
      </div>
    );
    const modeFields = (fields: readonly { key: string; label: string; tip: string }[], options: Array<{ value: string; label: string }>, modePath: string) => fields.map(item => renderModeField(brandKey, item, options, mobile, modePath));

    const queryEnabled = featureState.brand && featureState.query;
    const sections = (
      <>
        {section('智能运维', <>
          <Item name={['brand_configs', brandKey, 'enabled']} label={fieldLabel('智能运维', '控制该品牌是否启用智能运营主流程。关闭后，当前品牌的治理配置不会执行。')} valuePropName="checked"><SwitchControl aria-label={`${mobile ? brandKey : selectedBrand?.name ?? brandKey}智能运维开关`} disabled={disabled} onChange={(checked: boolean) => setFeatureState((current) => ({ ...current, brand: checked }))} /></Item>
          <Item label={fieldLabel('召回数量', '启用后展示并运行召回数量检查配置。')}><SwitchControl aria-label="召回数量" checked={featureState.recall} disabled={disabled} onChange={(checked: boolean) => setCheckMode('recall', checked)} /></Item>
          <Item name={['brand_configs', brandKey, 'query_governance_reorder_enabled']} label={fieldLabel('重排序', '控制召回结果是否进入向量重排序节点。关闭后会跳过重排序，直接使用召回结果。')} valuePropName="checked"><SwitchControl aria-label="重排序" disabled={disabled} onChange={(checked: boolean) => setFeatureState((current) => ({ ...current, reorder: checked }))} /></Item>
          <Item name={['brand_configs', brandKey, 'query_governance_relevance_enabled']} label={fieldLabel('相关性', '控制是否调用相关性 AI 检查商品图文与 Query 的匹配程度。关闭后跳过该节点。')} valuePropName="checked"><SwitchControl aria-label="相关性" disabled={disabled} onChange={(checked: boolean) => setFeatureState((current) => ({ ...current, relevance: checked }))} /></Item>
          <Item name={['brand_configs', brandKey, 'query_governance_recall_rate_enabled']} label={fieldLabel('AI召回率', '控制是否用大模型全目录召回对照搜索引擎召回，检测漏召回商品并补打白名单标签 / Query 白名单。需服务端配置商品目录数据源。')} valuePropName="checked"><SwitchControl aria-label="AI召回率" disabled={disabled} onChange={(checked: boolean) => setFeatureState((current) => ({ ...current, recallRate: checked }))} /></Item>
          <Item label={fieldLabel('断码', '启用后展示并运行断码检测配置。')}><SwitchControl aria-label="断码" checked={featureState.inventory} disabled={disabled} onChange={(checked: boolean) => setCheckMode('inventory', checked)} /></Item>
          <Item name={['brand_configs', brandKey, 'no_result_governance_enabled']} label={fieldLabel('无结果词', '控制无结果词是否进入发现、诊断和治理流程。')} valuePropName="checked"><SwitchControl aria-label="无结果词" disabled={disabled} onChange={(checked: boolean) => setFeatureState((current) => ({ ...current, noResult: checked }))} /></Item>
        </>)}
        {queryEnabled && featureState.recall ? section('召回数量', recallFields.map(item => renderNumberField(brandKey, item, mobile))) : null}
        {queryEnabled && featureState.recallRate ? section('AI召回率', recallRateRuntimeFields.map(item => renderNumberField(brandKey, item, mobile))) : null}
        {queryEnabled && featureState.reorder ? section('重排序', <>
          {vectorReorderRuntimeFields.map(item => renderNumberField(brandKey, item, mobile))}
          {queryBehaviorEvidenceFields.map(item => renderNumberField(brandKey, item, mobile))}
        </>) : null}
        {queryEnabled && featureState.relevance && featureState.relevanceMode ? section('相关性', <>
          <Item name={['brand_configs', brandKey, 'query_governance_relevance_buckets']} label={fieldLabel('相关性检测分桶', '仅选中的 Mixed 召回分桶商品进入相关性检测；默认检测 L1、L2 分桶，也支持追加其他分桶。全选表示不按分桶过滤，适用于没有分桶字段的品牌。页面显示中文名称。')}>
            {mobile ? <MobSelector multiple disabled={disabled} options={[...relevanceBucketOptions]} /> : <Select mode="multiple" maxTagCount={1} maxTagTextLength={8} disabled={disabled} options={[...relevanceBucketOptions]} />}
          </Item>
          {relevanceRuntimeFields.map(item => renderNumberField(brandKey, { ...item, legacyLabel: item.key === 'query_governance_relevance_multimodal_concurrency' ? '相关性多模态并发' : item.key === 'query_governance_relevance_batch_size' ? '相关性批量大小' : item.key === 'query_governance_relevance_model_timeout_seconds' ? '模型超时（秒）' : item.key === 'query_governance_relevance_image_download_timeout_seconds' ? '图片下载超时（秒）' : undefined }, mobile))}
        </>) : null}
        {queryEnabled && featureState.inventory ? section('断码', inventoryFields.map(item => renderNumberField(brandKey, { ...item, legacyLabel: item.key === 'top_query_inventory_max_cycles' ? '断码补位最大轮数' : item.key === 'top_query_inventory_check_interval_minutes' ? '断码监测执行频次' : undefined }, mobile))) : null}
        {featureState.brand && featureState.noResult ? section('无结果词', <>
          {noResultFields.slice(0, 3).map(item => renderNumberField(brandKey, item, mobile))}
          <Item className="ops-config-tags-field" name={['brand_configs', brandKey, 'no_result_excluded_keywords']} label={fieldLabel('无结果词排除词', '输入不需要治理的词，例如“客服、订单、优惠券”；输入后按回车保存为一个词。')}><Select mode="tags" disabled={disabled} tokenSeparators={[',', '，']} /></Item>
          <Item name={['brand_configs', brandKey, 'no_result_execution_mode']} label={fieldLabel('无结果词执行模式', '确认后执行会先生成方案并等待人工确认；自动执行会直接执行已允许的运营动作。')}><Select disabled={disabled} options={executionModeOptions} /></Item>
          <Collapse items={[{ key: 'advanced', label: '高级配置', children: <>{noResultFields.slice(3).map(item => renderNumberField(brandKey, item, mobile))}</> }]} />
        </>) : null}
        {queryEnabled ? section('取词配置', <>
          {basicFields.map(item => renderNumberField(brandKey, { ...item, legacyLabel: item.key === 'query_governance_extract_limit' ? '每日最多治理 Query 数量' : item.key === 'top_query_check_product_limit' ? '每个 Query 最多检查前 N 个商品' : undefined }, mobile))}
          {renderMetricFloor(brandKey, mobile)}
          {querySelectionFields.map(item => renderNumberField(brandKey, item, mobile))}
          {renderGrowthRules(brandKey, mobile)}
          <Item className="ops-config-tags-field" name={['brand_configs', brandKey, 'query_governance_excluded_keywords']} label={fieldLabel('排除词', '配置不希望进入检测的词（例如“折扣”）；取词阶段会精准过滤该词（Atlas 下会先去掉词尾的男女装等人群后缀再匹配，如“折扣 女士”会被过滤，“男士折扣”不会过滤）。输入后按回车或逗号保存。')}><Select aria-label="排除词" mode="tags" disabled={disabled} tokenSeparators={[',', '，']} /></Item>
        </>) : null}
        {queryEnabled ? section('Agent执行模式', modeFields(topQueryChecks, topQueryModeOptions, 'top_query_check_modes')) : null}
        {featureState.brand ? section('运营工具执行模式', <>{modeFields(topQueryTools, topQueryModeOptions, 'top_query_tool_modes')}{kgAgentRuntimeFields.map(item => renderNumberField(brandKey, item, mobile))}</>) : null}
        {featureState.brand ? section('知识图谱 Agent 参考', <>
          <Item name={['brand_configs', brandKey, 'query_governance_kg_reference']} label={fieldLabel('治理参考', '补充知识图谱 Agent 判断错误商品和生成策略时需要遵循的业务案例。保存后默认追加到该品牌的 System Prompt。')}>
            {mobile ? <textarea aria-label="知识图谱 Agent 治理参考" disabled={disabled} rows={8} style={{ width: '100%' }} /> : <Input.TextArea aria-label="知识图谱 Agent 治理参考" disabled={disabled} rows={8} placeholder="例如：花衬衫与印花衬衫是同一实体的别名，应建立同义关系。" />}
          </Item>
          {!mobile ? <Form.Item noStyle shouldUpdate>
            {({ getFieldValue }) => {
              const updatedBy = getFieldValue(['brand_configs', brandKey, 'query_governance_kg_reference_updated_by']);
              const updatedAt = getFieldValue(['brand_configs', brandKey, 'query_governance_kg_reference_updated_at']);
              return <div className="ops-config-reference-meta">最近修改：{updatedBy || '—'} {updatedAt ? `（${updatedAt}）` : ''}</div>;
            }}
          </Form.Item> : null}
        </>) : null}
        {experimentPanel ? <div className="ops-config-section">{experimentPanel}</div> : null}
      </>
    );
    return mobile ? <>{sections}</> : <div className="ops-config-section-stack">{sections}</div>;
  };

  if (isMobile) {
    return (
      <div style={{ marginBottom: 16 }}>
        <ConfigContextActions targetId="config-context-brand">
          <BrandSwitcher
            brands={brands}
            selectedBrandKey={selectedBrand?.key ?? selectedBrandKey}
            enabledByBrand={enabledByBrand}
            onChange={requestBrandChange}
          />
        </ConfigContextActions>
        {selectedBrand ? <div style={{ padding: '12px 0' }}>{renderBrandFields(selectedBrand.key, true)}</div> : null}
      </div>
    );
  }

  return (
    <Form.Item noStyle shouldUpdate>
      {({ getFieldValue }) => (
        <div className="ops-brand-config-card">
          <ConfigContextActions targetId="config-context-brand">
            <BrandSwitcher
              brands={brands}
              selectedBrandKey={selectedBrand?.key ?? selectedBrandKey}
              enabledByBrand={Object.fromEntries(brands.map((brand) => [brand.key, Boolean(enabledByBrand[brand.key] ?? getFieldValue(['brand_configs', brand.key, 'enabled']))]))}
              onChange={requestBrandChange}
            />
          </ConfigContextActions>
          {selectedBrand ? renderBrandFields(selectedBrand.key, false) : null}
        </div>
      )}
    </Form.Item>
  );
};

export default BrandConfigSection;
