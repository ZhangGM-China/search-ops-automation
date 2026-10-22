import React from 'react';
import { Alert, Button, Empty, Flex, Modal, Spin, Tag, Typography } from 'antd';
import type { QueryDemotionExecutionPreview, QueryDemotionTargetProduct, TopQueryAction } from '@/shared/types';

const { Text } = Typography;

const blockedReasonMap: Record<string, string> = {
  authoritative_config_unavailable: '降桶配置回读能力不可用，已阻止执行',
  authoritative_config_request_failed: '降桶配置回读能力不可用，已阻止执行',
  authoritative_config_invalid_response: '降桶配置回读能力不可用，已阻止执行',
  invalid_authoritative_config_response: '降桶配置回读响应无效，已阻止执行',
  authoritative_config_version_missing: '降桶配置版本不可用，已阻止执行',
  server_atomic_compare_unavailable: '生产环境缺少原子版本条件写能力，已阻止执行',
  no_query_demote_actions: '当前没有可执行的 Query 降桶方案',
};

const checkKeyMap: Record<string, string> = {
  inventory: '断码',
};

const evidenceStatusText = (
  status: string | undefined,
  kind: 'config' | 'search' | 'rollback',
) => {
  if (status === 'passed') return kind === 'rollback' ? '已恢复' : '已通过';
  if (status === 'failed') return kind === 'rollback' ? '回滚失败' : '未通过';
  if (status === 'inconclusive') return '无结论';
  if (kind === 'search') return '观察中';
  if (kind === 'rollback') return '待核查';
  return '验证中';
};

const evidenceTone = (status: string | undefined) => {
  if (status === 'passed') return 'success';
  if (status === 'failed') return 'danger';
  return 'warning';
};

const countDetail = (label: string, ids: string[] | undefined) => (
  ids?.length ? `${label} ${ids.length} 个` : ''
);

export const QueryDemotionVerificationPanel: React.FC<{
  action: TopQueryAction;
  rollbackLoading?: boolean;
  onRollback?: (action: TopQueryAction) => void;
}> = ({ action, rollbackLoading = false, onRollback }) => {
  const config = action.configVerification;
  const search = action.searchEffect;
  const rollback = action.rollbackVerification;
  const rows = [
    config ? {
      key: 'config',
      label: '配置验证',
      status: config.status,
      statusText: evidenceStatusText(config.status, 'config'),
      detail: [
        config.version ? `版本 ${config.version}` : '',
        countDetail('缺失', config.missingIds),
        countDetail('多出', config.unexpectedIds),
        config.reason || '',
      ].filter(Boolean).join('·'),
    } : null,
    search ? {
      key: 'search',
      label: '搜索效果',
      status: search.status,
      statusText: evidenceStatusText(search.status, 'search'),
      detail: [
        search.resultCount === null || search.resultCount === undefined ? '' : `结果 ${search.resultCount} 件`,
        countDetail('仍观察到', search.observedRemainingIds),
        countDetail('未验证', search.unverifiedIds),
        search.reason || '',
      ].filter(Boolean).join('·'),
    } : null,
    rollback ? {
      key: 'rollback',
      label: '回滚验证',
      status: rollback.status,
      statusText: evidenceStatusText(rollback.status, 'rollback'),
      detail: [
        rollback.version ? `版本 ${rollback.version}` : '',
        countDetail('缺失', rollback.missingIds),
        countDetail('多出', rollback.unexpectedIds),
        rollback.reason || '',
      ].filter(Boolean).join('·'),
    } : null,
  ].filter((row): row is NonNullable<typeof row> => row !== null);

  return (
    <div className="ops-demotion-verification" data-testid={`query-demotion-evidence-${action.id}`}>
      {action.rollbackAvailable === true && onRollback ? (
        <Flex justify="flex-end">
          <Button className="ops-demotion-verification__rollback" size="small" danger loading={rollbackLoading} disabled={rollbackLoading} onClick={() => onRollback(action)}>
            回滚降桶
          </Button>
        </Flex>
      ) : null}
      {rows.length ? (
        <div className="ops-demotion-verification__rows">
          {rows.map((row) => (
            <div className="ops-demotion-verification__row" key={row.key}>
              <span>{row.label}</span>
              <strong className={`ops-demotion-verification__status ops-demotion-verification__status--${evidenceTone(row.status)}`}>
                {row.statusText}
              </strong>
              <small>{row.detail || '--'}</small>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
};

const priceText = (value: QueryDemotionTargetProduct['currentPrice']) => (
  value === null || value === undefined || value === '' ? '--' : `¥${value}`
);

const blockedReasonText = (reason: string | null) => {
  if (!reason) return '当前条件不允许执行 Query 降桶';
  return blockedReasonMap[reason] ?? `当前条件不允许执行 Query 降桶（${reason}）`;
};

const IdGroup: React.FC<{ label: string; ids: string[]; tone?: string }> = ({ label, ids, tone }) => (
  <div className="ops-demotion-preview__id-group">
    <Text type="secondary">{label}</Text>
    <Flex gap={6} wrap>
      {ids.length
        ? ids.map((id) => <Tag key={id} color={tone}>{id}</Tag>)
        : <Text type="secondary">--</Text>}
    </Flex>
  </div>
);

const ProductItem: React.FC<{ product: QueryDemotionTargetProduct }> = ({ product }) => (
  <li className="ops-demotion-preview__product" data-testid="demotion-preview-product">
    <Flex justify="space-between" gap={12} align="flex-start" wrap>
      <div>
        <div className="ops-demotion-preview__product-name">{product.productName || '未命名商品'}</div>
        <Text type="secondary">ID: {product.productId}</Text>
      </div>
      <Flex gap={6} wrap>
        {product.checkKey.map((key) => <Tag key={key}>{checkKeyMap[key] ?? key}</Tag>)}
      </Flex>
    </Flex>
    <Flex gap={8} wrap className="ops-demotion-preview__product-facts">
      <Tag color="volcano">当前价 {priceText(product.currentPrice)}</Tag>
      <Tag color="blue">参考价 {priceText(product.referencePrice)}</Tag>
      <Tag>{product.rank === null || product.rank === undefined || product.rank === '' ? 'rank --' : `rank #${product.rank}`}</Tag>
    </Flex>
  </li>
);

export interface QueryDemotionExecutionPanelProps {
  open: boolean;
  preview: QueryDemotionExecutionPreview | null;
  loading: boolean;
  executing: boolean;
  error: string | null;
  conflictMessage: string | null;
  onConfirm: (expectedConfigVersion: string, actionIds: number[]) => void;
  onClose: () => void;
}

const QueryDemotionExecutionPanel: React.FC<QueryDemotionExecutionPanelProps> = ({
  open,
  preview,
  loading,
  executing,
  error,
  conflictMessage,
  onConfirm,
  onClose,
}) => {
  const targetCount = preview?.targetIds.length ?? 0;
  const canConfirm = Boolean(
    preview?.executable
    && preview.configVersion
    && !preview.blockedReason
    && !loading
    && !executing,
  );

  return (
    <Modal
      open={open}
      rootClassName="ops-query-demotion-modal"
      title="确认执行 Query 降桶"
      width={760}
      okText={`确认执行降桶（${targetCount} 个商品）`}
      confirmLoading={executing}
      okButtonProps={{ disabled: !canConfirm }}
      cancelButtonProps={{ disabled: executing }}
      closable={!executing}
      mask={{ closable: !executing }}
      keyboard={!executing}
      onOk={() => {
        if (canConfirm && preview?.configVersion) {
          onConfirm(preview.configVersion, [...preview.actionIds]);
        }
      }}
      onCancel={() => {
        if (!executing) onClose();
      }}
      destroyOnHidden
    >
      <div className="ops-demotion-preview">
        {conflictMessage ? <Alert type="warning" showIcon title={conflictMessage} /> : null}
        {error ? <Alert type="error" showIcon title={error} /> : null}
        {loading ? (
          <div className="ops-demotion-preview__loading">
            <Spin />
            <Text type="secondary">正在回读权威降桶配置并计算影响范围…</Text>
          </div>
        ) : preview ? (
          <>
            <div className="ops-demotion-preview__identity">
              <div><Text type="secondary">品牌</Text><strong>{preview.brand}</strong></div>
              <div><Text type="secondary">Query</Text><strong>{preview.query}</strong></div>
              <div><Text type="secondary">配置版本</Text><strong>{`当前配置版本：${preview.configVersion || '--'}`}</strong></div>
            </div>

            {!preview.executable || !preview.configVersion || preview.blockedReason ? (
              <Alert
                type="error"
                showIcon
                title={blockedReasonText(preview.blockedReason)}
              />
            ) : null}

            <div className="ops-demotion-preview__summary" aria-label="Query 降桶影响统计">
              <div><span>目标商品</span><strong>{targetCount}</strong></div>
              <div><span>原有降桶</span><strong>{preview.beforeIds.length}</strong></div>
              <div><span>新增降桶</span><strong>{preview.addedIds.length}</strong></div>
              <div><span>执行后总数</span><strong>{preview.desiredIds.length}</strong></div>
            </div>

            <div className="ops-demotion-preview__diff">
              <IdGroup label="新增降桶集合" ids={preview.addedIds} tone="volcano" />
              <IdGroup label="保留降桶集合" ids={preview.retainedIds} tone="blue" />
              <IdGroup label="移除降桶集合" ids={preview.removedIds} />
            </div>

            <Alert
              type={preview.rollbackAvailable ? 'info' : 'warning'}
              showIcon
              title={preview.rollbackAvailable
                ? '已保留执行前完整快照，满足配置条件时可精确回滚。'
                : '当前预览不具备精确回滚条件。'}
            />

            <section aria-label="全部影响商品">
              <div className="ops-demotion-preview__section-title">全部影响商品（{preview.targetProducts.length}）</div>
              {preview.targetProducts.length ? (
                <ul className="ops-demotion-preview__products">
                  {preview.targetProducts.map((product) => (
                    <ProductItem key={product.productId} product={product} />
                  ))}
                </ul>
              ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无影响商品" />}
            </section>
          </>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可用预览" />
        )}
      </div>
    </Modal>
  );
};

export default QueryDemotionExecutionPanel;
