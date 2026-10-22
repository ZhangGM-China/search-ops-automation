import React from 'react';
import { Button, Input, InputNumber, Modal, Space, Spin, Tag, Tooltip, Typography, message } from 'antd';
import { EditOutlined, ReloadOutlined, RollbackOutlined, SaveOutlined } from '@ant-design/icons';
import { adminApi } from '@/shared/api';
import type { NodeModelConfigNode } from '@/shared/types';

interface Props {
  onDirtyChange?: (dirty: boolean) => void;
}

interface EditableNodeModelConfig {
  nodeKey: string;
  channel: string;
  primaryModel: string;
  fallbackModel: string;
  primarySource?: string;
  fallbackSource?: string;
  requiredCapabilities?: string[];
}

const channelMeta: Record<string, { label: string; color: string }> = {
  rd_llm: { label: 'RD-LLM', color: 'blue' },
  pi_agent: { label: 'PI Agent', color: 'green' },
};

const nodeLabels: Record<string, string> = {
  'query_governance.kg_agent': 'KG Agent',
  'query_governance.preference_generation': 'Query 偏好生成 Agent',
  'query_governance.relevance_multimodal': '相关性多模态 Agent（图文）',
  'query_governance.relevance_rules': '相关性规则 AI 整理',
  'top_query.planner': '旧 Top Query 规划（已退役）',
  'top_query.world_knowledge': '世界知识判断',
  'no_result.intent': '无结果词意图识别',
  'no_result.diagnosis': '无结果词诊断',
  'assistant.chat': '智能助理对话',
};

const sourceLabels: Record<string, string> = {
  published: '已发布',
  env: '环境变量',
  default: '系统默认',
};

const toEditableNode = (node: NodeModelConfigNode): EditableNodeModelConfig => ({
  ...node,
  fallbackModel: node.fallbackModel ?? '',
});

const comparableRows = (rows: EditableNodeModelConfig[]) => rows.map((row) => ({
  nodeKey: row.nodeKey,
  channel: row.channel,
  primaryModel: row.primaryModel,
  fallbackModel: row.fallbackModel,
}));

const rowsEqual = (left: EditableNodeModelConfig[], right: EditableNodeModelConfig[]) => (
  JSON.stringify(comparableRows(left)) === JSON.stringify(comparableRows(right))
);

const errorMessage = (error: unknown, fallback: string) => {
  if (error && typeof error === 'object' && 'response' in error) {
    const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
  }
  return fallback;
};

const NodeModelConfigSection: React.FC<Props> = ({ onDirtyChange }) => {
  const [activeVersion, setActiveVersion] = React.useState<number | null>(null);
  const [nodes, setNodes] = React.useState<EditableNodeModelConfig[]>([]);
  const [baselineNodes, setBaselineNodes] = React.useState<EditableNodeModelConfig[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [editing, setEditing] = React.useState(false);
  const [changeNote, setChangeNote] = React.useState('');
  const [rollbackOpen, setRollbackOpen] = React.useState(false);
  const [rollbackTargetVersion, setRollbackTargetVersion] = React.useState<number | null>(null);
  const modelsChanged = React.useMemo(() => !rowsEqual(nodes, baselineNodes), [baselineNodes, nodes]);
  const isDirty = editing && (modelsChanged || changeNote.trim().length > 0);

  React.useEffect(() => {
    onDirtyChange?.(isDirty);
  }, [isDirty, onDirtyChange]);

  const applyConfig = React.useCallback((version: number | null, nextNodes: NodeModelConfigNode[]) => {
    const editableNodes = nextNodes.map(toEditableNode);
    setActiveVersion(version);
    setNodes(editableNodes);
    setBaselineNodes(editableNodes.map((node) => ({ ...node })));
    setEditing(false);
    setChangeNote('');
    onDirtyChange?.(false);
  }, [onDirtyChange]);

  const loadConfig = React.useCallback(async () => {
    setLoading(true);
    try {
      const response = await adminApi.getNodeModelConfig();
      applyConfig(response.data.activeVersion, response.data.nodes);
    } catch (error) {
      message.error(errorMessage(error, '获取节点模型配置失败'));
    } finally {
      setLoading(false);
    }
  }, [applyConfig]);

  React.useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadConfig();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadConfig]);

  const groupedNodes = React.useMemo(() => {
    const groups = new Map<string, EditableNodeModelConfig[]>();
    nodes.forEach((node) => {
      const current = groups.get(node.channel) ?? [];
      current.push(node);
      groups.set(node.channel, current);
    });
    return Array.from(groups.entries());
  }, [nodes]);

  const updateNode = (nodeKey: string, field: 'primaryModel' | 'fallbackModel', value: string) => {
    setNodes((current) => current.map((node) => (
      node.nodeKey === nodeKey ? { ...node, [field]: value } : node
    )));
  };

  const normalizedEntries = () => {
    const entries: Record<string, { channel: string; primaryModel: string; fallbackModel: string | null }> = {};
    for (const node of nodes) {
      const primaryModel = node.primaryModel.trim();
      const fallbackModel = node.fallbackModel.trim();
      if (!primaryModel || primaryModel.length > 120 || /\s/.test(primaryModel)) {
        message.error(`${node.nodeKey} 主模型格式无效`);
        return null;
      }
      if (fallbackModel && (fallbackModel.length > 120 || /\s/.test(fallbackModel))) {
        message.error(`${node.nodeKey} 备用模型格式无效`);
        return null;
      }
      if (fallbackModel && fallbackModel === primaryModel) {
        message.error(`${node.nodeKey} 主模型与备用模型不能相同`);
        return null;
      }
      entries[node.nodeKey] = {
        channel: node.channel,
        primaryModel,
        fallbackModel: fallbackModel || null,
      };
    }
    return entries;
  };

  const handlePublish = async () => {
    const entries = normalizedEntries();
    if (!entries) return;
    const operationId = crypto.randomUUID();
    setSaving(true);
    try {
      const draftResponse = await adminApi.createNodeModelConfigDraft({
        entries,
        ...(changeNote.trim() ? { changeNote: changeNote.trim() } : {}),
        requestId: `node-model-config-draft:${operationId}`,
      });
      const publishResponse = await adminApi.publishNodeModelConfig(
        draftResponse.data.version,
        {
          expectedVersion: activeVersion,
          requestId: `node-model-config-publish:${operationId}`,
        },
      );
      applyConfig(publishResponse.data.activeVersion, publishResponse.data.nodes);
      message.success(`节点模型配置 v${publishResponse.data.activeVersion} 已发布`);
    } catch (error) {
      message.error(errorMessage(error, '发布节点模型配置失败'));
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setNodes(baselineNodes.map((node) => ({ ...node })));
    setChangeNote('');
    setEditing(false);
    onDirtyChange?.(false);
  };

  const handleRollback = async () => {
    if (!rollbackTargetVersion) {
      message.error('请输入回滚目标版本');
      return;
    }
    setSaving(true);
    try {
      const response = await adminApi.rollbackNodeModelConfig({
        targetVersion: rollbackTargetVersion,
        expectedVersion: activeVersion,
        requestId: `node-model-config-rollback:${crypto.randomUUID()}`,
      });
      applyConfig(response.data.activeVersion, response.data.nodes);
      setRollbackOpen(false);
      setRollbackTargetVersion(null);
      message.success(`已回滚至 v${rollbackTargetVersion} 的配置`);
    } catch (error) {
      message.error(errorMessage(error, '回滚节点模型配置失败'));
    } finally {
      setSaving(false);
    }
  };

  if (loading && nodes.length === 0) {
    return (
      <section className="ops-config-section ops-node-model-panel" aria-label="节点模型配置加载中">
        <div className="ops-config-embedded-panel__body ops-node-model-center-state">
          <Spin />
        </div>
      </section>
    );
  }

  return (
    <section id="node-model-config" className="ops-config-section ops-node-model-panel">
      <div className="ops-config-embedded-panel__header">
        <div className="ops-config-embedded-panel__title">
          <Typography.Text strong>节点模型配置</Typography.Text>
          <Tag color={activeVersion === null ? 'default' : 'green'}>
            {activeVersion === null ? '尚未发布' : `当前生效 v${activeVersion}`}
          </Tag>
        </div>
        <Space size="small" wrap>
          {editing ? (
            <>
              <Button size="small" onClick={handleCancel}>取消</Button>
              <Button
                aria-label="发布配置"
                type="primary"
                size="small"
                icon={<SaveOutlined />}
                loading={saving}
                disabled={!modelsChanged}
                onClick={handlePublish}
              >
                发布配置
              </Button>
            </>
          ) : (
            <>
              <Tooltip title="刷新配置">
                <Button
                  aria-label="刷新配置"
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={loading}
                  onClick={() => void loadConfig()}
                />
              </Tooltip>
              <Button aria-label="编辑模型" size="small" icon={<EditOutlined />} disabled={nodes.length === 0} onClick={() => setEditing(true)}>
                编辑模型
              </Button>
              <Button
                aria-label="回滚版本"
                size="small"
                icon={<RollbackOutlined />}
                disabled={activeVersion === null}
                onClick={() => {
                  setRollbackTargetVersion(activeVersion && activeVersion > 1 ? activeVersion - 1 : null);
                  setRollbackOpen(true);
                }}
              >
                回滚版本
              </Button>
            </>
          )}
        </Space>
      </div>

      <div className="ops-config-embedded-panel__body ops-node-model-body">
        {editing ? (
          <div className="ops-node-model-change-note">
            <label htmlFor="node-model-change-note">变更说明</label>
            <Input.TextArea
              id="node-model-change-note"
              aria-label="变更说明"
              value={changeNote}
              maxLength={240}
              autoSize={{ minRows: 2, maxRows: 4 }}
              onChange={(event) => setChangeNote(event.target.value)}
            />
          </div>
        ) : null}

        {groupedNodes.map(([channel, channelNodes]) => {
          const meta = channelMeta[channel] ?? { label: channel, color: 'default' };
          return (
            <div key={channel} className="ops-node-model-group">
              <div className="ops-config-section__title ops-node-model-group__title">
                <span>{meta.label}</span>
                <Tag color={meta.color}>{channelNodes.length} 个节点</Tag>
              </div>
              <div className="ops-node-model-list">
                {channelNodes.map((node) => (
                  <div key={node.nodeKey} className="ops-node-model-row">
                    <div className="ops-node-model-cell ops-node-model-node">
                      <span className="ops-node-model-cell__label">节点</span>
                      <strong>{node.nodeKey}</strong>
                      <Typography.Text type="secondary">{nodeLabels[node.nodeKey] ?? node.nodeKey}</Typography.Text>
                      {node.requiredCapabilities?.includes('image') ? (
                        <Tag color="purple">需要图文输入</Tag>
                      ) : null}
                    </div>
                    <div className="ops-node-model-cell">
                      <span className="ops-node-model-cell__label">通道</span>
                      <Tag color={meta.color}>{meta.label}</Tag>
                    </div>
                    <div className="ops-node-model-cell">
                      <label className="ops-node-model-cell__label" htmlFor={`${node.nodeKey}-primary`}>主模型</label>
                      <Input
                        id={`${node.nodeKey}-primary`}
                        aria-label={`${node.nodeKey} 主模型`}
                        value={node.primaryModel}
                        disabled={!editing}
                        onChange={(event) => updateNode(node.nodeKey, 'primaryModel', event.target.value)}
                      />
                      {!editing && node.primarySource ? (
                        <span className="ops-node-model-source">来源：{sourceLabels[node.primarySource] ?? node.primarySource}</span>
                      ) : null}
                    </div>
                    <div className="ops-node-model-cell">
                      <label className="ops-node-model-cell__label" htmlFor={`${node.nodeKey}-fallback`}>备用模型</label>
                      <Input
                        id={`${node.nodeKey}-fallback`}
                        aria-label={`${node.nodeKey} 备用模型`}
                        value={node.fallbackModel}
                        disabled={!editing}
                        placeholder="无备用模型"
                        onChange={(event) => updateNode(node.nodeKey, 'fallbackModel', event.target.value)}
                      />
                      {!editing && node.fallbackSource ? (
                        <span className="ops-node-model-source">来源：{sourceLabels[node.fallbackSource] ?? node.fallbackSource}</span>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      <Modal
        title="回滚节点模型配置"
        open={rollbackOpen}
        okText="确认回滚"
        cancelText="取消"
        confirmLoading={saving}
        onOk={() => void handleRollback()}
        onCancel={() => {
          setRollbackOpen(false);
          setRollbackTargetVersion(null);
        }}
      >
        <div className="ops-node-model-rollback-field">
          <label htmlFor="node-model-rollback-version">回滚目标版本</label>
          <InputNumber
            id="node-model-rollback-version"
            aria-label="回滚目标版本"
            min={1}
            precision={0}
            value={rollbackTargetVersion}
            onChange={(value) => setRollbackTargetVersion(value)}
          />
        </div>
      </Modal>
    </section>
  );
};

export default NodeModelConfigSection;
