import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Form } from 'antd';
import { Form as MobileForm } from 'antd-mobile';
import { describe, expect, it, vi } from 'vitest';

import BrandConfigSection from './BrandConfigSection';

const mobileState = vi.hoisted(() => ({ enabled: false }));

vi.mock('@/shared/hooks/useMobile', () => ({
  default: () => mobileState.enabled,
}));

vi.mock('antd-mobile', () => {
  const Form = Object.assign(
    ({ children }: { children: React.ReactNode }) => <form>{children}</form>,
    {
      Item: ({ children, label }: { children: React.ReactNode; label?: string }) => <label>{label}{children}</label>,
    },
  );
  const Collapse = Object.assign(
    ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    {
      Panel: ({ children, title }: { children: React.ReactNode; title: string }) => <section><h4>{title}</h4>{children}</section>,
    },
  );
  const Tabs = Object.assign(
    ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    {
      Tab: ({ children, title }: { children: React.ReactNode; title: string }) => <section><h4>{title}</h4>{children}</section>,
    },
  );
  return {
    Collapse,
    Form,
    Selector: ({ disabled, options }: { disabled?: boolean; options?: Array<{ value: string; label: string }> }) => (
      <select disabled={disabled}>{options?.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select>
    ),
    Stepper: ({ disabled }: { disabled?: boolean }) => <input type="number" disabled={disabled} />,
    Switch: ({ disabled, ...props }: { disabled?: boolean; 'aria-label'?: string }) => <input type="checkbox" disabled={disabled} {...props} />,
    Tabs,
  };
});

const brands = [
  { name: 'ATLAS', key: 'atlas' },
  { name: 'Harbor', key: 'harbor' },
];

const brandValues = {
  brand_configs: {
    atlas: {
      enabled: true,
      top_query_governance_enabled: true,
      query_governance_reorder_enabled: true,
      query_governance_relevance_enabled: true,
      no_result_governance_enabled: true,
      top_query_check_modes: { recall: 'human_approval', relevance: 'auto', inventory: 'auto' },
      query_governance_extract_limit: 150,
      top_query_check_product_limit: 16,
      top_query_world_knowledge_config: {
        weather_cities: [{ city: '上海', city_code: 101020100 }],
        social_trends: [{ keywords: ['连衣裙'], tags: ['轻薄'] }],
      },
    },
    harbor: {
      enabled: false,
      top_query_governance_enabled: true,
      query_governance_reorder_enabled: true,
      query_governance_relevance_enabled: true,
      no_result_governance_enabled: false,
      top_query_check_modes: { recall: 'disabled', relevance: 'auto', inventory: 'disabled' },
      query_governance_extract_limit: 80,
      top_query_check_product_limit: 12,
    },
  },
};

describe('BrandConfigSection', () => {
  it('renders desktop fields, falls back to the first brand, and reports brand changes', async () => {
    mobileState.enabled = false;
    const onSelectedBrandKeyChange = vi.fn();

    render(
      <Form initialValues={brandValues}>
        <BrandConfigSection
          brands={brands}
          selectedBrandKey="missing"
          selectedBrandConfig={brandValues.brand_configs.atlas}
          onSelectedBrandKeyChange={onSelectedBrandKeyChange}
          experimentPanel={<div>desktop experiment panel</div>}
        />
      </Form>,
    );

    expect(screen.getByText('智能运维', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getByText('desktop experiment panel')).toBeInTheDocument();
    expect(screen.getByLabelText('ATLAS智能运维开关')).toBeChecked();
    expect(screen.getByLabelText('每日最多治理 Query 数')).toHaveValue('150');
    expect(screen.getByText('取词配置')).toBeInTheDocument();
    expect(screen.getByText('偏好生成并发数')).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByLabelText('当前品牌'));
    fireEvent.click(await screen.findByText('Harbor', { exact: true }));
    expect(onSelectedBrandKeyChange).toHaveBeenCalledWith('harbor');
    expect(document.querySelector('.ops-brand-switcher__status.is-enabled')).toBeInTheDocument();
    expect(document.querySelector('.ops-brand-switcher__status.is-disabled')).toBeInTheDocument();
    expect(document.querySelector('.ops-brand-switcher__select .ant-select-arrow')).not.toBeInTheDocument();
    expect(document.querySelector('.ops-brand-switcher')).toBeInTheDocument();
    expect(document.querySelectorAll('.ops-brand-switcher__separator')).toHaveLength(0);
  });

  it('uses arrow controls for adjacent brands', () => {
    mobileState.enabled = false;
    const onSelectedBrandKeyChange = vi.fn();

    render(
      <Form initialValues={brandValues}>
        <BrandConfigSection
          brands={brands}
          selectedBrandKey="atlas"
          selectedBrandConfig={brandValues.brand_configs.atlas}
          onSelectedBrandKeyChange={onSelectedBrandKeyChange}
        />
      </Form>,
    );

    expect(screen.getByRole('button', { name: '上一个品牌' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '下一个品牌' }));
    expect(onSelectedBrandKeyChange).toHaveBeenCalledWith('harbor');
  });

  it('keeps the current brand when a dirty switch is cancelled or save fails', async () => {
    mobileState.enabled = false;
    const onSelectedBrandKeyChange = vi.fn();
    const onSaveBeforeBrandChange = vi.fn().mockResolvedValue(false);

    render(
      <Form initialValues={brandValues}>
        <BrandConfigSection
          brands={brands}
          selectedBrandKey="atlas"
          selectedBrandConfig={brandValues.brand_configs.atlas}
          isEditing
          isDirty
          onSelectedBrandKeyChange={onSelectedBrandKeyChange}
          onSaveBeforeBrandChange={onSaveBeforeBrandChange}
        />
      </Form>,
    );

    fireEvent.mouseDown(screen.getByLabelText('当前品牌'));
    fireEvent.click(await screen.findByText('Harbor', { exact: true }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('配置有未保存变更');
    fireEvent.click(screen.getByRole('button', { name: '取 消' }));
    expect(onSaveBeforeBrandChange).not.toHaveBeenCalled();
    expect(onSelectedBrandKeyChange).not.toHaveBeenCalled();

    fireEvent.mouseDown(screen.getByLabelText('当前品牌'));
    fireEvent.click(await screen.findByText('Harbor', { exact: true }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('配置有未保存变更');
    fireEvent.click(screen.getByRole('button', { name: '保存并切换' }));
    await waitFor(() => expect(onSaveBeforeBrandChange).toHaveBeenCalledWith('harbor'));
    expect(onSelectedBrandKeyChange).not.toHaveBeenCalled();
  });

  it('renders the complete mobile configuration path with an optional panel', () => {
    mobileState.enabled = true;

    render(
      <MobileForm initialValues={brandValues}>
        <BrandConfigSection
          disabled
          brands={brands}
          selectedBrandKey="atlas"
          selectedBrandConfig={brandValues.brand_configs.atlas}
          onSelectedBrandKeyChange={vi.fn()}
          experimentPanel={<div>mobile experiment panel</div>}
        />
      </MobileForm>,
    );

    expect(screen.queryByText('基础配置')).not.toBeInTheDocument();
    expect(screen.getByText('智能运维', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getAllByText('取词配置')).toHaveLength(1);
    expect(screen.getByText('相关性', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getAllByText('偏好生成并发数')).toHaveLength(1);
    expect(screen.queryByText('世界知识配置')).not.toBeInTheDocument();
    expect(screen.getAllByText('mobile experiment panel')).toHaveLength(1);
    expect(screen.getByLabelText('atlas智能运维开关')).toBeDisabled();
  });

  it('organizes the main flow in governance order and explains every configurable field', () => {
    mobileState.enabled = false;

    render(
      <Form initialValues={brandValues}>
        <BrandConfigSection
          brands={brands}
          selectedBrandKey="atlas"
          selectedBrandConfig={brandValues.brand_configs.atlas}
          onSelectedBrandKeyChange={vi.fn()}
        />
      </Form>,
    );

    const titles = Array.from(document.querySelectorAll('.ops-config-section__title')).map(node => node.textContent);
    expect(titles).toEqual([
      '智能运维',
      '召回数量',
      '重排序',
      '相关性',
      '断码',
      '无结果词',
      '取词配置',
      'Agent执行模式',
      '运营工具执行模式',
      '知识图谱 Agent 参考',
    ]);
    const primarySwitchLabels = Array.from(
      document.querySelector('.ops-config-section')?.querySelectorAll('.ant-form-item-label label') ?? [],
    ).map(node => node.textContent);
    expect(primarySwitchLabels).toEqual([
      '智能运维',
      '召回数量',
      '重排序',
      '相关性',
      'AI召回率',
      '断码',
      '无结果词',
    ]);
    expect(screen.queryByText('Query 治理')).not.toBeInTheDocument();
    expect(screen.queryByText('数据分类配置')).not.toBeInTheDocument();
    expect(screen.queryByText('世界知识配置')).not.toBeInTheDocument();
    expect(screen.queryByText('Query 级商品降桶')).not.toBeInTheDocument();
    expect(screen.getByText('智能运维', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getByText('最低日搜索指标')).toBeInTheDocument();
    expect(screen.queryByText('最低指标数量')).not.toBeInTheDocument();
    expect(screen.getByText('取高增长 Query 规则')).toBeInTheDocument();
    expect(document.querySelectorAll('.ops-config-growth-formula__row')).toHaveLength(1);
    expect(screen.getByLabelText('增长比例下限')).toBeInTheDocument();
    expect(screen.getByLabelText('显著性放大系数')).toBeInTheDocument();
    expect(screen.getByLabelText('绝对增长下限')).toBeInTheDocument();
    expect(screen.getByText(/PV比例 ≥ max/)).toBeInTheDocument();
    expect(screen.getByText(/ΔPV ≥ max/)).toBeInTheDocument();
    expect(screen.getByText('相关性 AI 并发数')).toBeInTheDocument();
    expect(screen.getByText('相关性检测分桶')).toBeInTheDocument();
    expect(screen.getByText('商品降桶')).toBeInTheDocument();
    expect(screen.getByText('知识图谱调整')).toBeInTheDocument();
    expect(screen.getByLabelText('最低日搜索指标提示')).toBeInTheDocument();
    expect(screen.getByLabelText('候选 Query 并发处理数提示')).toBeInTheDocument();
    expect(screen.getByText('排除词')).toBeInTheDocument();
    expect(screen.getByLabelText('排除词提示')).toBeInTheDocument();
    expect(screen.getByText('Query 精确排序')).toBeInTheDocument();
    expect(screen.queryByText('Query 精确排序（未接入）')).not.toBeInTheDocument();
  });

  it('hides the corresponding detail section when a governance switch is disabled', () => {
    mobileState.enabled = false;
    const onCheckModeChange = vi.fn();

    render(
      <Form initialValues={brandValues}>
        <BrandConfigSection
          brands={brands}
          selectedBrandKey="atlas"
          selectedBrandConfig={brandValues.brand_configs.atlas}
          onSelectedBrandKeyChange={vi.fn()}
          onCheckModeChange={onCheckModeChange}
          isEditing
        />
      </Form>,
    );

    fireEvent.click(screen.getByLabelText('重排序'));
    expect(screen.queryByText('向量重排并发数')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('相关性'));
    expect(screen.queryByText('相关性检测分桶')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('无结果词'));
    expect(screen.queryByText('无结果词拉取数量')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('召回数量'));
    expect(screen.queryByText('少结果判定阈值')).not.toBeInTheDocument();
    expect(onCheckModeChange).toHaveBeenCalledWith('recall', 'disabled');

    fireEvent.click(screen.getByLabelText('断码'));
    expect(screen.queryByText('断码检查间隔（分钟）')).not.toBeInTheDocument();
    expect(onCheckModeChange).toHaveBeenCalledWith('inventory', 'disabled');
  });
  it('shows the AI recall-rate section and remediation tool modes when enabled', () => {
    mobileState.enabled = false;

    const recallRateEnabledValues = {
      ...brandValues,
      brand_configs: {
        ...brandValues.brand_configs,
        atlas: { ...brandValues.brand_configs.atlas, query_governance_recall_rate_enabled: true },
      },
    };
    render(
      <Form initialValues={recallRateEnabledValues}>
        <BrandConfigSection
          brands={brands}
          selectedBrandKey="atlas"
          selectedBrandConfig={recallRateEnabledValues.brand_configs.atlas}
          onSelectedBrandKeyChange={vi.fn()}
        />
      </Form>,
    );

    expect(screen.getByLabelText('AI召回率')).toBeChecked();
    expect(screen.getByText('AI召回率', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getByLabelText('LLM 商品目录批量大小')).toBeInTheDocument();
    expect(screen.getByLabelText('LLM 召回分数阈值')).toBeInTheDocument();
    expect(screen.getByLabelText('JEV 标签判定阈值')).toBeInTheDocument();
    expect(screen.getByText('白名单标签补召回')).toBeInTheDocument();
    expect(screen.getByText('Query 白名单补召回')).toBeInTheDocument();
  });

  it('hides the AI recall-rate runtime section when the switch is off', () => {
    mobileState.enabled = false;

    render(
      <Form initialValues={brandValues}>
        <BrandConfigSection
          brands={brands}
          selectedBrandKey="atlas"
          selectedBrandConfig={brandValues.brand_configs.atlas}
          onSelectedBrandKeyChange={vi.fn()}
        />
      </Form>,
    );

    expect(screen.getByLabelText('AI召回率')).not.toBeChecked();
    expect(screen.queryByLabelText('LLM 商品目录批量大小')).not.toBeInTheDocument();
    // 两个补召回执行模式选项始终可见，便于先配置再启用检测。
    expect(screen.getByText('白名单标签补召回')).toBeInTheDocument();
    expect(screen.getByText('Query 白名单补召回')).toBeInTheDocument();
  });
});
