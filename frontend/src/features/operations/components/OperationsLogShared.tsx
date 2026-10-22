import React, { useState } from 'react';
import { Button, DatePicker, Form, Input, Select, Space, Tabs, message } from 'antd';
import type { FormInstance } from 'antd';
import { CopyOutlined, LeftOutlined, RightOutlined } from '@ant-design/icons';
import dayjs, { Dayjs } from 'dayjs';
import { useInRouterContext, useLocation, useNavigate } from 'react-router-dom';
import type { QueryFilters } from '@/shared/types';
import { OPERATIONS_LOG_TABS } from './operationsLogConstants';
import { writeOperationsLogFilters } from './operationsLogFilters';

export const OperationsLogTabs: React.FC = () => {
  if (!useInRouterContext()) {
    return <div className="operations-log-secondary-nav"><Tabs activeKey="/blacklist" items={OPERATIONS_LOG_TABS} className="operations-log-tabs" /></div>;
  }
  return <OperationsLogTabsRouted />;
};

const OperationsLogTabsRouted: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const activeKey = OPERATIONS_LOG_TABS.some((item) => item.key === location.pathname) ? location.pathname : '/blacklist';
  return <div className="operations-log-secondary-nav"><Tabs activeKey={activeKey} items={OPERATIONS_LOG_TABS} onChange={(key) => navigate({ pathname: key, search: location.search })} className="operations-log-tabs" /></div>;
};

type DateSwitcherProps = { value?: Dayjs; onChange?: (value: Dayjs) => void };
export const OperationsLogDateSwitcher: React.FC<DateSwitcherProps> = ({ value, onChange }) => {
  const current = value || dayjs();
  const change = (next: Dayjs) => onChange?.(next);
  return (
    <Space.Compact className="operations-date-switcher">
      <Button aria-label="前一天" icon={<LeftOutlined />} onClick={() => change(current.subtract(1, 'day'))} />
      <DatePicker value={current} allowClear={false} onChange={(next) => next && change(next)} />
      <Button aria-label="后一天" icon={<RightOutlined />} onClick={() => change(current.add(1, 'day'))} />
    </Space.Compact>
  );
};

type FilterBarProps = {
  form: FormInstance<QueryFilters>;
  allowedBrands: Array<{ label: string; value: string }>;
  values?: QueryFilters;
  onChange: (values: QueryFilters) => void;
  includeStatus?: boolean;
  trailing?: React.ReactNode;
};
export const OperationsLogFilterBar: React.FC<FilterBarProps> = ({ form, allowedBrands, onChange, includeStatus, trailing }) => {
  const submit = (values: QueryFilters) => { writeOperationsLogFilters(values); onChange({ ...values, page: 1 }); };
  return (
    <div className="operations-log-filter-bar">
      <div className="operations-log-filter-controls">
        <Form form={form} layout="inline" onFinish={submit} size="small" initialValues={{ date: dayjs() }}>
          <Form.Item name="date" noStyle>
            <OperationsLogDateSwitcher onChange={(date) => { form.setFieldValue('date', date); submit(form.getFieldsValue()); }} />
          </Form.Item>
          <Form.Item name="brand" noStyle>
            <Select
              allowClear
              placeholder="选择品牌"
              options={allowedBrands}
              style={{ width: 150 }}
              onChange={() => submit(form.getFieldsValue())}
            />
          </Form.Item>
          <Form.Item name="search" noStyle>
            <Input
              allowClear
              placeholder="SKU/商品名称/Query"
              style={{ width: 240 }}
              onPressEnter={() => submit(form.getFieldsValue())}
            />
          </Form.Item>
          {includeStatus ? (
            <Form.Item name="status" noStyle>
              <Select placeholder="追踪状态" style={{ width: 130 }} onChange={() => submit(form.getFieldsValue())} options={[
                { value: 'monitoring', label: '追踪中' },
                { value: 'stopped', label: '已停止' },
                { value: 'recovered', label: '已恢复' },
              ]} />
            </Form.Item>
          ) : null}
        </Form>
      </div>
      {trailing ? <div className="operations-log-filter-actions">{trailing}</div> : null}
    </div>
  );
};

const SKU_ONLY_NAME_PATTERN = /^(?:[A-Z]?\d{8,})(?:\s*,\s*[A-Z]?\d{8,})*$/i;

type ProductIdentityCellProps = { name?: string | null; sku?: string | null; imageUrl?: string | null; onClick?: () => void };

export const ProductIdentityCell: React.FC<ProductIdentityCellProps> = ({ name, sku, imageUrl, onClick }) => {
  const [failedImageUrl, setFailedImageUrl] = useState<string | null>(null);
  const normalizedName = name?.trim() || '';
  const normalizedSku = sku?.trim() || '';
  const hasProductName = Boolean(normalizedName)
    && normalizedName !== normalizedSku
    && !SKU_ONLY_NAME_PATTERN.test(normalizedName);
  const displayName = hasProductName ? normalizedName : '暂无商品名称';

  const showImage = Boolean(imageUrl) && failedImageUrl !== imageUrl;

  return (
    <div className="product-identity-cell" role={onClick ? 'button' : undefined} tabIndex={onClick ? 0 : undefined} onClick={onClick} onKeyDown={(event) => { if (onClick && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); onClick(); } }}>
      {showImage ? (
        <img className="product-identity-cell__image" src={imageUrl || undefined} alt={displayName} loading="lazy" onError={() => setFailedImageUrl(imageUrl || null)} />
      ) : (
        <div className="product-identity-cell__placeholder" aria-label="暂无商品图片" />
      )}
      <span title={displayName}>{displayName}</span>
      {sku ? <span className="product-identity-cell__sku"><SkuCopyCell sku={sku} /></span> : null}
    </div>
  );
};

export const SkuCopyCell: React.FC<{ sku: string }> = ({ sku }) => {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(sku);
      message.success('SKU 已复制');
    } catch {
      message.error('复制失败');
    }
  };
  return <span className="sku-copy-cell"><span title={sku}>{sku}</span><Button className="sku-copy-button" type="text" size="small" aria-label={`复制 SKU ${sku}`} icon={<CopyOutlined />} onClick={(event) => { event.stopPropagation(); void copy(); }} onKeyDown={(event) => event.stopPropagation()} /></span>;
};
