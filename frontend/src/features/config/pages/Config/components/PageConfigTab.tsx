import React from 'react';
import { HolderOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { Alert, Button, Checkbox, Modal, Select, Space, Spin, Switch, Tooltip, Typography, message } from 'antd';
import { adminApi } from '@/shared/api';
import type { PageConfigDefinition, PageConfigKey, PageConfigValue } from '@/shared/types';
import { defaultPageConfigValues, usePageConfigContext } from '@/shared/pageConfig/context';
import { fallbackDefinitions } from '@/shared/pageConfig/definitions';
import ConfigContextActions from './ConfigContextActions';

interface Props {
  onDirtyChange: (dirty: boolean) => void;
}

const sameValue = (left: PageConfigValue | undefined, right: PageConfigValue | undefined) =>
  JSON.stringify(left) === JSON.stringify(right);

const getMetricLabel = (options: Array<{ value: string; label: string }>, metric: string) =>
  options.find((option) => option.value === metric)?.label ?? metric;

const SettingTitle: React.FC<{ label: string; tip: string }> = ({ label, tip }) => (
  <div className="page-config-tab__setting-title">
    <Typography.Text strong>{label}</Typography.Text>
    <Tooltip title={tip} mouseEnterDelay={0.1}>
      <InfoCircleOutlined aria-label={`${label}提示`} className="page-config-tab__setting-tip ops-tooltip-icon" />
    </Tooltip>
  </div>
);

const MetricListEditor: React.FC<{
  definition: PageConfigDefinition;
  value: string[];
  editing: boolean;
  onChange: (next: string[]) => void;
}> = ({ definition, value, editing, onChange }) => {
  const options = React.useMemo(() => definition.options ?? [], [definition.options]);
  const minItems = definition.minItems ?? 0;
  const maxItems = definition.maxItems ?? options.length;
  const [open, setOpen] = React.useState(false);
  const [modalOrder, setModalOrder] = React.useState<string[]>([]);
  const [modalEnabled, setModalEnabled] = React.useState<string[]>([]);
  const [draggedMetric, setDraggedMetric] = React.useState<string | null>(null);

  const allMetrics = React.useMemo(() => [
    ...value,
    ...options.map((option) => option.value).filter((metric) => !value.includes(metric)),
  ], [options, value]);

  const openEditor = () => {
    setModalOrder(allMetrics);
    setModalEnabled(value);
    setOpen(true);
  };

  const moveMetric = (fromMetric: string, toMetric: string) => {
    if (fromMetric === toMetric) return;
    setModalOrder((current) => {
      const next = [...current];
      const fromIndex = next.indexOf(fromMetric);
      const toIndex = next.indexOf(toMetric);
      if (fromIndex < 0 || toIndex < 0) return current;
      next.splice(fromIndex, 1);
      next.splice(toIndex, 0, fromMetric);
      return next;
    });
  };

  const toggleMetric = (metric: string, checked: boolean) => {
    setModalEnabled((current) => {
      if (checked) {
        if (current.includes(metric) || current.length >= maxItems) return current;
        return [...current, metric];
      }
      if (!current.includes(metric) || current.length <= minItems) return current;
      return current.filter((item) => item !== metric);
    });
  };

  const applyModal = () => {
    onChange(modalOrder.filter((metric) => modalEnabled.includes(metric)));
    setOpen(false);
  };

  const summary = value.length
    ? value.map((metric) => getMetricLabel(options, metric)).join('；')
    : '未选择指标';

  return (
    <div className="page-config-tab__metric-editor">
      <Button
        type="default"
        className="page-config-tab__metric-summary"
        disabled={!editing}
        aria-label={`${definition.label}：调整`}
        onClick={openEditor}
      >
        <span className="page-config-tab__metric-summary-text">{summary}</span>
        <Typography.Text type="secondary">{value.length}/{maxItems} 项</Typography.Text>
        <span className="page-config-tab__metric-summary-action">调整</span>
      </Button>
      <Modal
        title={definition.label}
        open={open}
        okText="应用"
        cancelText="取消"
        onOk={applyModal}
        onCancel={() => setOpen(false)}
        width={560}
        destroyOnHidden
      >
        <div className="page-config-tab__metric-modal-intro">
          <Typography.Text type="secondary">拖动左侧手柄调整顺序，使用右侧开关控制是否展示。</Typography.Text>
          <Typography.Text type="secondary">已启用 {modalEnabled.length}/{maxItems} 项，至少 {minItems} 项</Typography.Text>
        </div>
        <div className="page-config-tab__metric-modal-list">
          {modalOrder.map((metric) => {
            const enabled = modalEnabled.includes(metric);
            return <div
              key={metric}
              className={`page-config-tab__metric-modal-row${enabled ? '' : ' is-disabled'}`}
              draggable
              onDragStart={() => setDraggedMetric(metric)}
              onDragOver={(event) => event.preventDefault()}
              onDrop={() => {
                if (draggedMetric) moveMetric(draggedMetric, metric);
                setDraggedMetric(null);
              }}
              onDragEnd={() => setDraggedMetric(null)}
            >
              <HolderOutlined className="page-config-tab__metric-drag-handle" aria-label={`${getMetricLabel(options, metric)}拖动排序`} />
              <Typography.Text className="page-config-tab__metric-modal-label">{getMetricLabel(options, metric)}</Typography.Text>
              <Switch
                size="small"
                checked={enabled}
                disabled={(enabled && modalEnabled.length <= minItems) || (!enabled && modalEnabled.length >= maxItems)}
                aria-label={`${definition.label}：${getMetricLabel(options, metric)}`}
                onChange={(checked) => toggleMetric(metric, checked)}
              />
            </div>;
          })}
        </div>
      </Modal>
    </div>
  );
};

const PageConfigTab: React.FC<Props> = ({ onDirtyChange }) => {
  const [messageApi, contextHolder] = message.useMessage();
  const { loading, payload, replacePayload } = usePageConfigContext();
  const definitions = payload.definitions.length ? payload.definitions : fallbackDefinitions;
  const [editing, setEditing] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [draft, setDraft] = React.useState<Record<PageConfigKey, PageConfigValue>>(defaultPageConfigValues);
  const dirty = editing && definitions.some((definition) => !sameValue(
    draft[definition.key], payload.values[definition.key] ?? definition.defaultValue,
  ));

  React.useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);

  const setValue = (key: PageConfigKey, value: PageConfigValue) => setDraft((current) => ({ ...current, [key]: value }));
  const handleStartEditing = () => {
    setDraft({ ...defaultPageConfigValues, ...payload.values });
    setEditing(true);
  };
  const handleSave = async () => {
    setSaving(true);
    try {
      const response = await adminApi.updatePageConfig({ expectedVersion: payload.version, values: draft });
      replacePayload(response.data);
      setEditing(false);
      messageApi.success('页面配置已保存');
    } catch {
      messageApi.error('页面配置保存失败，请刷新后重试');
    } finally {
      setSaving(false);
    }
  };
  const groups = Array.from(new Set(definitions.map((definition) => definition.groupKey))).map((groupKey) => ({
    key: groupKey,
    label: definitions.find((definition) => definition.groupKey === groupKey)?.groupLabel ?? groupKey,
    items: definitions.filter((definition) => definition.groupKey === groupKey).sort((a, b) => a.order - b.order),
  }));
  const overviewGroups = groups.filter((group) => group.key.startsWith('overview_'));
  const detailGroups = groups.filter((group) => group.key.startsWith('detail_'));

  const getValue = (definition: PageConfigDefinition) =>
    (editing ? draft[definition.key] : payload.values[definition.key]) ?? definition.defaultValue;

  const renderBooleanSetting = (definition: PageConfigDefinition) => {
    const value = getValue(definition);
    return <div className="page-config-tab__toggle" key={definition.key}>
      <SettingTitle label={definition.label} tip={definition.description} />
      <div className="page-config-tab__setting-control page-config-tab__toggle-control">
        <Switch
          checked={value === true}
          disabled={!editing}
          aria-label={definition.label}
          onChange={(checked) => setValue(definition.key, checked)}
        />
      </div>
    </div>;
  };

  const renderMetricSetting = (definition: PageConfigDefinition) => {
    const value = getValue(definition);
    return <div className="page-config-tab__metric-setting" key={definition.key}>
      <SettingTitle label={definition.label} tip={definition.description} />
      <div className="page-config-tab__setting-control">
        {Array.isArray(value) ? definition.maxItems === 1 ? <Select
          id={`${definition.key}-select`}
          aria-label={definition.label}
          value={value[0]}
          options={definition.options}
          disabled={!editing}
          onChange={(next) => setValue(definition.key, [next])}
          style={{ width: '100%' }}
        /> : <MetricListEditor
          definition={definition}
          value={value}
          editing={editing}
          onChange={(next) => setValue(definition.key, next)}
        /> : null}
      </div>
    </div>;
  };

  const renderTrendMetricSetting = (
    metricDefinition: PageConfigDefinition,
    chartDefinition: PageConfigDefinition,
    tableDefinition: PageConfigDefinition,
  ) => {
    const value = getValue(metricDefinition);
    return <div className="page-config-tab__metric-setting" key={metricDefinition.key}>
      <SettingTitle label={metricDefinition.label} tip={metricDefinition.description} />
      <div className="page-config-tab__setting-control page-config-tab__composite-control">
        <Checkbox
          checked={getValue(chartDefinition) === true}
          disabled={!editing}
          aria-label="图表"
          onChange={(event) => setValue(chartDefinition.key, event.target.checked)}
        >图表</Checkbox>
        <Checkbox
          checked={getValue(tableDefinition) === true}
          disabled={!editing}
          aria-label="表单"
          onChange={(event) => setValue(tableDefinition.key, event.target.checked)}
        >表单</Checkbox>
        {Array.isArray(value) ? <MetricListEditor
          definition={metricDefinition}
          value={value}
          editing={editing}
          onChange={(next) => setValue(metricDefinition.key, next)}
        /> : null}
      </div>
    </div>;
  };

  const renderQueryCardMetricSetting = (
    visibilityDefinition: PageConfigDefinition,
    metricDefinition: PageConfigDefinition,
  ) => {
    const value = getValue(metricDefinition);
    const visible = getValue(visibilityDefinition) === true;
    return <div className="page-config-tab__metric-setting" key={visibilityDefinition.key}>
      <SettingTitle label={visibilityDefinition.label} tip={visibilityDefinition.description} />
      <div className="page-config-tab__setting-control page-config-tab__composite-control">
        <Switch
          checked={visible}
          disabled={!editing}
          aria-label={visibilityDefinition.label}
          onChange={(checked) => setValue(visibilityDefinition.key, checked)}
        />
        {visible && Array.isArray(value) ? <MetricListEditor
          definition={metricDefinition}
          value={value}
          editing={editing}
          onChange={(next) => setValue(metricDefinition.key, next)}
        /> : null}
      </div>
    </div>;
  };

  const renderGroups = (sectionGroups: typeof groups) => sectionGroups.flatMap((group) => {
    if (group.key === 'overview_drawer') {
      const visibilityDefinition = definitions.find((definition) => definition.key === 'brand_overview.card.show_operational_data');
      if (visibilityDefinition && getValue(visibilityDefinition) !== true) return [];
    }

    const renderedItems = group.items.map((definition) => {
      if (definition.key === 'brand_overview.drawer.show_chart' || definition.key === 'brand_overview.drawer.show_table') return null;
      if (definition.key === 'brand_detail.query_card.metrics') return null;
      if (definition.key === 'brand_overview.drawer.trend_metrics') {
        const chartDefinition = group.items.find((item) => item.key === 'brand_overview.drawer.show_chart');
        const tableDefinition = group.items.find((item) => item.key === 'brand_overview.drawer.show_table');
        if (chartDefinition && tableDefinition) return renderTrendMetricSetting(definition, chartDefinition, tableDefinition);
      }
      if (definition.key === 'brand_detail.query_card.show_metrics') {
        const metricDefinition = group.items.find((item) => item.key === 'brand_detail.query_card.metrics');
        if (metricDefinition) return renderQueryCardMetricSetting(definition, metricDefinition);
      }
      return definition.type === 'metric_list' ? renderMetricSetting(definition) : renderBooleanSetting(definition);
    });

    return <section className="ops-config-section" key={group.key} aria-labelledby={`page-config-group-${group.key}`}>
      <div className="ops-config-section__title" id={`page-config-group-${group.key}`}>{group.label}</div>
      <div className="page-config-tab__group-body">{renderedItems}</div>
    </section>;
  });

  return <div className="ops-page page-config-tab" style={{ paddingBottom: 20 }}>
    {contextHolder}
    <ConfigContextActions>
      {!editing ? <Button type="primary" size="small" onClick={handleStartEditing}>修改配置</Button> : <Space size="small">
        <Button size="small" onClick={() => setEditing(false)} disabled={saving}>取消</Button>
        <Button type="primary" size="small" loading={saving} disabled={!dirty} onClick={() => void handleSave()}>保存</Button>
      </Space>}
    </ConfigContextActions>
    {loading && payload.version === 0 ? <Spin spinning style={{ display: 'block', margin: '24px auto' }} /> : null}
    {!loading && !definitions.length ? <Alert type="info" showIcon message="暂无可配置的页面内容" /> : null}
    <div className="page-config-tab__major-title">品牌总览</div>
    {renderGroups(overviewGroups)}
    <div className="page-config-tab__major-title">品牌详情</div>
    {renderGroups(detailGroups)}
  </div>;
};

export default PageConfigTab;
