import React from 'react';
import { Select, Typography } from 'antd';
import type { TopQueryExperimentSamplePoolItem } from '@/shared/types';
import { renderQueryOption } from './experimentUtils';

interface Props {
  label: string;
  value: string[];
  blockedQueries: string[];
  items: TopQueryExperimentSamplePoolItem[];
  disabled: boolean;
  onChange: (value: string[]) => void;
}

const ExperimentSampleSelector: React.FC<Props> = ({ label, value, blockedQueries, items, disabled, onChange }) => {
  const keyByQuery = React.useMemo(() => new Map(items.map((item) => [item.queryText, item.governanceKey])), [items]);
  const blockedKeys = React.useMemo(
    () => new Set(blockedQueries.map((query) => keyByQuery.get(query)).filter(Boolean)),
    [blockedQueries, keyByQuery],
  );
  const options = React.useMemo(() => items.map((item) => ({
    value: item.queryText,
    label: renderQueryOption(item),
    disabled: blockedKeys.has(item.governanceKey),
  })), [blockedKeys, items]);

  return (
    <div className="experiment-selector">
      <div className="experiment-selector__label">
        <Typography.Text strong>{label}</Typography.Text>
        <Typography.Text type={value.length === 20 ? 'success' : 'secondary'}>{value.length}/20</Typography.Text>
      </div>
      <Select
        aria-label={`${label} Query`}
        mode="multiple"
        maxCount={20}
        allowClear
        showSearch
        optionFilterProp="value"
        placeholder={`选择 20 个${label} Query`}
        options={options}
        value={value}
        onChange={onChange}
        tagRender={({ value: tagValue, closable, onClose }) => (
          <span className="experiment-query-tag">
            {String(tagValue)}
            {closable ? <button type="button" aria-label={`移除 ${String(tagValue)}`} onMouseDown={(event) => event.preventDefault()} onClick={onClose}>×</button> : null}
          </span>
        )}
        style={{ width: '100%' }}
        disabled={disabled}
      />
    </div>
  );
};

export default ExperimentSampleSelector;
