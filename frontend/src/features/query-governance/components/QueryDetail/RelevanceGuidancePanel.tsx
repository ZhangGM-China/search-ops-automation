import React from 'react';
import { Button, Flex, Input, Space, Typography, message } from 'antd';
import { dashboardApi } from '@/shared/api';
import type { RelevanceGuidance } from '@/shared/types';
import { getApiErrorMessage } from '@/shared/lib/error';
import { RELEVANCE_GUIDANCE_UPDATED_EVENT } from './relevanceGuidanceEvents';

const { Text } = Typography;

const RelevanceGuidancePanel: React.FC<{ brand: string; query: string; refreshToken?: number }> = ({ brand, query, refreshToken = 0 }) => {
  const [guidance, setGuidance] = React.useState<RelevanceGuidance | null>(null);
  const [rules, setRules] = React.useState('');
  const [preview, setPreview] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  const applyGuidance = React.useCallback((next: RelevanceGuidance) => {
    setGuidance((current) => ({ ...next, reasonHistory: next.reasonHistory ?? current?.reasonHistory ?? [] }));
    setRules(next.rules || '');
  }, []);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const response = await dashboardApi.getRelevanceGuidance(brand, query);
      applyGuidance(response.data);
    } catch (error: unknown) {
      message.error(getApiErrorMessage(error, '获取相关性规则失败'));
    } finally {
      setLoading(false);
    }
  }, [applyGuidance, brand, query]);

  React.useEffect(() => {
    const timer = window.setTimeout(() => { void load(); }, 0);
    return () => window.clearTimeout(timer);
  }, [load, refreshToken]);

  React.useEffect(() => {
    const onUpdated = (event: Event) => {
      const updated = (event as CustomEvent<RelevanceGuidance>).detail;
      if (updated?.brand === brand && updated?.query === query) {
        applyGuidance(updated);
      }
    };
    window.addEventListener(RELEVANCE_GUIDANCE_UPDATED_EVENT, onUpdated);
    return () => window.removeEventListener(RELEVANCE_GUIDANCE_UPDATED_EVENT, onUpdated);
  }, [applyGuidance, brand, query]);

  const save = async () => {
    setSaving(true);
    try {
      const response = await dashboardApi.saveRelevanceGuidance(brand, { query, rules });
      applyGuidance(response.data);
      message.success('相关性规则已保存');
    } catch (error: unknown) {
      message.error(getApiErrorMessage(error, '保存相关性规则失败'));
    } finally {
      setSaving(false);
    }
  };

  const organize = async () => {
    setSaving(true);
    try {
      const response = await dashboardApi.previewRelevanceGuidanceOrganization(brand, query);
      setPreview(response.data.rules);
    } catch (error: unknown) {
      message.error(getApiErrorMessage(error, 'AI 整理失败'));
    } finally {
      setSaving(false);
    }
  };

  const confirmPreview = async () => {
    if (preview === null) return;
    setSaving(true);
    try {
      const response = await dashboardApi.confirmRelevanceGuidanceOrganization(brand, { query, rules: preview });
      applyGuidance(response.data);
      setPreview(null);
      message.success('整理后的规则已确认');
    } catch (error: unknown) {
      message.error(getApiErrorMessage(error, '确认整理结果失败'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Flex vertical gap={12}>
      <Input.TextArea value={rules} onChange={(event) => setRules(event.target.value)} autoSize={{ minRows: 5, maxRows: 12 }} disabled={loading || saving} placeholder="填写该 Query 的相关性纠正规则" />
      <Text type="secondary">填写该Query的纠正规则，纠正AI在相关性检测中产生的问题，规则会在该 Query 的下一次相关性检测中生效。</Text>
      <Space wrap>
        <Button type="primary" onClick={() => void save()} loading={saving}>保存</Button>
        <Button onClick={() => void organize()} loading={saving}>AI 整理</Button>
      </Space>
      {preview !== null ? (
        <Flex vertical gap={8} style={{ borderTop: '1px solid #eee', paddingTop: 12 }}>
          <Text strong>AI 整理预览</Text>
          <Input.TextArea value={preview} onChange={(event) => setPreview(event.target.value)} autoSize={{ minRows: 4, maxRows: 10 }} />
          <Space>
            <Button type="primary" onClick={() => void confirmPreview()} loading={saving}>确认覆盖</Button>
            <Button onClick={() => setPreview(null)} disabled={saving}>取消</Button>
          </Space>
        </Flex>
      ) : null}
      {guidance?.reasonHistory?.length ? (
        <Flex vertical gap={6} style={{ borderTop: '1px solid #eee', paddingTop: 12 }}>
          <Text strong>规则变更记录</Text>
          {guidance.reasonHistory.map((item) => (
            <div key={item.id}>
              <Text>{item.reason}</Text>
              <div><Text type="secondary">{item.operator} · {item.createdAt || '-'}</Text></div>
            </div>
          ))}
        </Flex>
      ) : null}
    </Flex>
  );
};

export default RelevanceGuidancePanel;
