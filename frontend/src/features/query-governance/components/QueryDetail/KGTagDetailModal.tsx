import React from 'react';
import { Alert, Button, Descriptions, Modal, Spin, Tabs } from 'antd';
import { dashboardApi } from '@/shared/api';

export type KGTagReference = {
  name: string;
  proposed?: Record<string, unknown>;
};

type TagDetails = Record<string, unknown>;

const labels: Array<[string, string]> = [
  ['name', '标签名称'],
  ['viewTag', '展示名称'],
  ['isRoot', '根标签'],
  ['dimensions', '维度信息'],
  ['weight', '权重'],
  ['isEnglish', '英文分词'],
  ['segmentingAgain', '二次分词'],
  ['isCompose', '合成词'],
  ['nameAnd', '合成词的包含词'],
  ['nameNot', '合成词的排除词'],
];

const boolText = (value: unknown) => {
  if (value === true || value === 1 || value === '1' || value === 'true') return '是';
  if (value === false || value === 0 || value === '0' || value === 'false') return '否';
  return '--';
};
const valueText = (key: string, value: unknown) => {
  if (value === null || value === undefined || value === '') return '--';
  if (['isRoot', 'isEnglish', 'segmentingAgain', 'isCompose'].includes(key)) return boolText(value);
  if (Array.isArray(value)) return value.length ? value.join('、') : '无';
  if (typeof value === 'object') return Object.values(value as Record<string, unknown>).join('、');
  return String(value);
};

export const KGTagDetailModal: React.FC<{
  brand: string;
  tags: KGTagReference[];
  open: boolean;
  onClose: () => void;
}> = ({ brand, tags, open, onClose }) => {
  if (!open) return null;
  return <Modal title="标签属性" open={open} onCancel={onClose} footer={null} width={620} destroyOnHidden>
    <KGTagDetailContent key={JSON.stringify([brand, tags])} brand={brand} tags={tags} />
  </Modal>;
};

const KGTagDetailContent: React.FC<{ brand: string; tags: KGTagReference[] }> = ({ brand, tags }) => {
  const [activeName, setActiveName] = React.useState(tags[0]?.name || '');
  const [details, setDetails] = React.useState<Record<string, TagDetails>>({});
  const [error, setError] = React.useState<string | null>(null);
  const [retry, setRetry] = React.useState(0);
  const proposed = tags.find((tag) => tag.name === activeName)?.proposed;
  const hasProposed = Boolean(proposed);

  React.useEffect(() => {
    if (!activeName || details[activeName] || hasProposed) return;
    let active = true;
    void dashboardApi.getKnowledgeGraphTagDetails(brand, activeName).then((response) => {
      if (active) setDetails((current) => ({ ...current, [activeName]: response.data }));
    }).catch(() => { if (active) setError('读取标签属性失败，请稍后重试'); });
    return () => { active = false; };
  }, [activeName, brand, details, hasProposed, retry]);

  const current = details[activeName] || proposed;
  const tabItems = tags.map((tag) => ({ key: tag.name, label: tag.name }));
  return <>
    {tags.length > 1 ? <Tabs activeKey={activeName} items={tabItems} onChange={(name) => { setActiveName(name); setError(null); }} /> : null}
    {activeName && !current && !error ? <Spin /> : null}
    {error && !current ? <Alert type="error" showIcon title={error} action={<Button size="small" autoInsertSpace={false} onClick={() => { setError(null); setRetry((value) => value + 1); }}>重试</Button>} /> : null}
    {current ? <Descriptions bordered column={1} size="small">
      {labels.map(([key, label]) => <Descriptions.Item key={key} label={label}>{valueText(key, key === 'name' ? current[key] || activeName : current[key])}</Descriptions.Item>)}
    </Descriptions> : null}
  </>;
};
