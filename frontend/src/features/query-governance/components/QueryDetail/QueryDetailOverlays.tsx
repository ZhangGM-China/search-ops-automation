import React from 'react';
import { Flex, Modal, Skeleton, Table, Tag, Typography } from 'antd';

import type { InventoryScanRecord, OpsTaskProductApiDetail, TopQueryDetailData } from '@/shared/types';
import ProductDetailContent from '../ProductDetailContent';
import BrandMetricDetailDrawer from '../BrandMetricDetailDrawer';
import { getProductImage } from './productImage';
import type { EventResultEntry, ExecutionOverviewNode } from './executionOverviewModel';
import { ProductCardGrid } from './ProductComparison';

const { Text } = Typography;

const DETAIL_MODAL_WIDTH = 840;
const DETAIL_MODAL_BODY_HEIGHT = 760;
const DETAIL_MODAL_BODY_MAX_HEIGHT = 'calc(100dvh - 140px)';

const fieldLabels: Record<string, string> = {
  status: '状态', reason: '原因', summary: '摘要', query: 'Query', result_count: '结果数量', resultCount: '结果数量',
  previous_result_count: '上一次结果数量', previousResultCount: '上一次结果数量', checked_product_count: '检测商品数量',
  checkedProductCount: '检测商品数量', product_count: '商品数量', productCount: '商品数量', products: '商品结果',
  product_results: '商品结果', productResults: '商品结果', is_relevant: '相关性结论', isRelevant: '相关性结论',
  failure_reason: '失败原因', failureReason: '失败原因', error_code: '错误码', errorCode: '错误码',
  inventoryStatus: '监测状态', inventoryCheckedAt: '巡检时间', brokenRateTransition: '断码率变化', imageUrl: '商品图片',
  error_message: '错误原因', errorMessage: '错误原因', strategy: '治理策略', actions: '动作', preference: '排序偏好',
};

const statusLabels: Record<string, { text: string; color?: string }> = {
  executed: { text: '已执行', color: 'green' }, completed: { text: '已完成', color: 'green' }, verified: { text: '已验证', color: 'green' },
  skipped: { text: '已跳过' }, pending: { text: '待执行', color: 'gold' }, running: { text: '执行中', color: 'blue' },
  failed: { text: '执行失败', color: 'red' }, error: { text: '异常', color: 'red' }, blacklisted: { text: '已拉黑' },
};

const asRecord = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
);

const isProduct = (value: unknown) => {
  const record = asRecord(value);
  return Boolean(record.productId || record.product_id || record.spuId || record.spu || record.skuId || record.sku_id);
};

const StructuredValue: React.FC<{
  value: unknown;
  fallback: string;
  inventoryBrokenRateThreshold: number;
  onOpenProduct: (product: Record<string, unknown>) => void;
  fieldKey?: string;
  displayLimit?: number;
}> = ({ value, fallback, inventoryBrokenRateThreshold, onOpenProduct, fieldKey = '', displayLimit = 16 }) => {
  if (value == null || value === '') return <div>{fallback}</div>;
  if (typeof value === 'boolean') return <div>{value ? '是' : '否'}</div>;
  if (typeof value === 'string' || typeof value === 'number') {
    const status = statusLabels[String(value).toLowerCase()];
    if (fieldKey.toLowerCase().includes('status') && status) return <Tag color={status.color}>{status.text}</Tag>;
    return <div>{String(value)}</div>;
  }
  if (Array.isArray(value)) {
    const products = value.filter(isProduct).map(asRecord);
    if (products.length === value.length && products.length) {
      return <ProductCardGrid products={products} emptyDescription="暂无商品" displayLimit={displayLimit} variant="snapshot" inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} />;
    }
    if (!value.length) return <div>{fallback}</div>;
    return (
      <ol className="ops-structured-detail-list">
        {value.map((item, index) => (
          <li key={index}>
            <StructuredValue value={item} fallback="-" inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} displayLimit={displayLimit} />
          </li>
        ))}
      </ol>
    );
  }
  const record = asRecord(value);
  const productList = [record.products, record.product_results, record.productResults, record.items, record.results]
    .find((item) => Array.isArray(item) && item.some(isProduct)) as unknown[] | undefined;
  return (
    <Flex vertical gap={8} className="ops-structured-detail-object">
      {Object.entries(record)
        .filter(([key]) => !['products', 'product_results', 'productResults', 'items', 'results'].includes(key) || !productList)
        .map(([key, item]) => (
          <div key={key} className="ops-health-evidence-entry">
            <Text strong>{fieldLabels[key] ?? key}</Text>
            {key === 'imageUrl' && typeof item === 'string' ? <img src={item} alt="商品图片" style={{ width: 64, height: 80, objectFit: 'cover' }} /> : <StructuredValue value={item} fallback="-" inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} fieldKey={key} displayLimit={displayLimit} />}
          </div>
        ))}
      {productList ? (
        <div className="ops-health-evidence-entry">
          <Text strong>商品结果</Text>
          <ProductCardGrid products={productList.map(asRecord)} emptyDescription="暂无商品" displayLimit={displayLimit} variant="snapshot" inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} />
        </div>
      ) : null}
    </Flex>
  );
};

const executionMetricRows = (node: ExecutionOverviewNode) => {
  const entries = node.details;
  if (node.key === 'relevance') {
    return [
      entries.filter((entry) => ['AI 检测', '历史复用', '正确商品', '错误商品'].includes(entry.label) && entry.rawValue == null),
      entries.filter((entry) => ['准确率', '耗时', 'Token'].includes(entry.label) && entry.rawValue == null),
    ]
      .filter((row) => row.length);
  }
  if (node.key === 'recall') {
    return [
      entries.filter((entry) => ['当前召回数量', '上一次召回数量', '耗时', 'Token'].includes(entry.label)),
    ]
      .filter((row) => row.length);
  }
  if (node.key === 'inventory') {
    return [entries.filter((entry) => ['最近巡检', '断码商品数量', '今日恢复商品', '监测间隔'].includes(entry.label))]
      .filter((row) => row.length);
  }

  return [
    entries.filter((entry) => ['当前 CTR', '近期 CTR', 'CTR 变化', 'CTR 告警阈值'].includes(entry.label)),
    entries.filter((entry) => ['当前 CVR', '近期 CVR', 'CVR 变化', 'CVR 告警阈值'].includes(entry.label)),
    entries.filter((entry) => ['当前召回商品数', '低召回豁免阈值', '耗时', 'Token'].includes(entry.label)),
  ].filter((row) => row.length);
};

// 相关性详情弹窗不展示以下治理明细字段，后端仍返回，仅前端隐藏。
const RELEVANCE_HIDDEN_DETAIL_LABELS = new Set(['拉黑状态', '根因分组', 'KG 策略', '重新打标签']);

const executionStandaloneEntries = (node: ExecutionOverviewNode, metricRows: EventResultEntry[][]) => {
  const metricEntries = new Set(metricRows.flat());
  return node.details.filter((entry) => entry.label !== '结论'
    && !metricEntries.has(entry)
    && !(node.key === 'relevance' && RELEVANCE_HIDDEN_DETAIL_LABELS.has(entry.label)));
};

const ExecutionStandaloneSection: React.FC<{
  entry: EventResultEntry;
  inventoryBrokenRateThreshold: number;
  onOpenProduct: (product: Record<string, unknown>) => void;
  displayLimit: number;
}> = ({ entry, inventoryBrokenRateThreshold, onOpenProduct, displayLimit }) => (
  <section
    className={`ops-execution-overview-modal__section${entry.label === '结论' ? ' ops-execution-overview-modal__section--conclusion' : ''}`}
    data-section-label={entry.label}
  >
    <Text
      strong
      className={`ops-execution-overview-modal__section-title${entry.label === '结论' ? ' ops-execution-overview-modal__section-title--conclusion' : ''}`}
    >
      {entry.label}
    </Text>
    <div className="ops-execution-overview-modal__section-content">
      {entry.rawValue != null ? (
        <StructuredValue
          value={entry.rawValue}
          fallback={entry.value}
          inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
          onOpenProduct={onOpenProduct}
          displayLimit={displayLimit}
        />
      ) : <div>{entry.value}</div>}
    </div>
  </section>
);

type QueryDetailOverlaysProps = {
  executionOverviewModal?: ExecutionOverviewNode | null;
  onCloseExecutionOverview?: () => void;
  onCloseProduct: () => void;
  onOpenProduct?: (product: Record<string, unknown>) => void;
  productDetailLoading: boolean;
  productModalOpen: boolean;
  selectedProduct: Record<string, unknown> | null;
  selectedProductDetail: OpsTaskProductApiDetail | null;
  task: TopQueryDetailData['task'];
  trendDrawerOpen: boolean;
  trend: TopQueryDetailData['trend'];
  inventoryBrokenRateThreshold?: number;
  onCloseTrendDrawer: () => void;
};

export const QueryDetailOverlays: React.FC<QueryDetailOverlaysProps> = ({
  executionOverviewModal,
  onCloseExecutionOverview = () => undefined,
  onCloseProduct,
  onOpenProduct = () => undefined,
  productDetailLoading,
  productModalOpen,
  selectedProduct,
  selectedProductDetail,
  task,
  trendDrawerOpen,
  trend,
  inventoryBrokenRateThreshold = 0.25,
  onCloseTrendDrawer,
}) => (
  <>
    <BrandMetricDetailDrawer
      brandName={task.queryText}
      chartQueryLabel={task.queryText}
      drawerTitle="数据明细"
      open={trendDrawerOpen}
      onClose={onCloseTrendDrawer}
      trend={trend ?? []}
      anchorDate={task.dataDate}
      availableMetrics={[
        'pv',
        ...((trend ?? []).some((point) => point.uv != null) ? ['uv' as const] : []),
        'ctr',
        ...((trend ?? []).some((point) => point.atc != null) ? ['atc' as const] : []),
        'cvr',
      ]}
      lockMetrics
    />
    <Modal
      title={executionOverviewModal ? `${executionOverviewModal.label}详情` : '节点详情'}
      open={Boolean(executionOverviewModal)}
      centered
      width={DETAIL_MODAL_WIDTH}
      footer={null}
      onCancel={onCloseExecutionOverview}
      className="ops-fixed-detail-modal ops-execution-overview-modal"
      styles={{ body: { height: DETAIL_MODAL_BODY_HEIGHT, maxHeight: DETAIL_MODAL_BODY_MAX_HEIGHT, overflow: 'auto' } }}
      destroyOnHidden
    >
      <div className="ops-execution-overview-modal__body" style={{ height: DETAIL_MODAL_BODY_HEIGHT, maxHeight: DETAIL_MODAL_BODY_MAX_HEIGHT, overflow: 'auto' }}>
        {executionOverviewModal ? (() => {
          const metricRows = executionMetricRows(executionOverviewModal);
          const conclusion = executionOverviewModal.details.find((entry) => entry.label === '结论') ?? {
            label: '结论',
            value: executionOverviewModal.summary || '暂无执行摘要',
          };
          const standaloneEntries = executionStandaloneEntries(executionOverviewModal, metricRows)
            .filter((entry) => entry.value !== conclusion.value);
          const displayLimit = executionOverviewModal.productsLimit ?? 16;
          return (
            <>
              <ExecutionStandaloneSection
                entry={conclusion}
                inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
                onOpenProduct={onOpenProduct}
                displayLimit={displayLimit}
              />
              {metricRows.length ? <div className="ops-execution-overview-modal__form">
                {metricRows.map((row, rowIndex) => {
                  const isFourColumnRow = executionOverviewModal.key === 'open_factor'
                    || executionOverviewModal.key === 'recall'
                    || executionOverviewModal.key === 'relevance';
                  const shouldPad = isFourColumnRow;
                  const columnCount = isFourColumnRow ? 4 : Math.min(row.length, 4);
                  const fields: Array<EventResultEntry | null> = shouldPad
                    ? [...row, ...Array.from({ length: Math.max(0, columnCount - row.length) }, () => null)]
                    : row;
                  return (
                    <div className={`ops-execution-overview-modal__row ops-execution-overview-modal__row--columns-${columnCount}`} key={`row-${rowIndex}`}>
                      {fields.map((entry, index) => entry ? (
                        <div key={`${entry.label}-${index}`} className="ops-execution-overview-modal__field">
                          <Text strong className="ops-execution-overview-modal__field-title">{entry.label}</Text>
                          <div className="ops-execution-overview-modal__field-content">
                            {entry.rawValue != null ? (
                              <StructuredValue
                                value={entry.rawValue}
                                fallback={entry.value}
                                inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
                                onOpenProduct={onOpenProduct}
                                displayLimit={displayLimit}
                              />
                            ) : <div>{entry.value}</div>}
                          </div>
                        </div>
                      ) : <div key={`empty-${index}`} className="ops-execution-overview-modal__field ops-execution-overview-modal__field--empty" aria-hidden="true" />)}
                    </div>
                  );
                })}
              </div> : null}
              {standaloneEntries.map((entry, index) => (
                <ExecutionStandaloneSection
                  key={`${entry.label}-${index}`}
                  entry={entry}
                  inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
                  onOpenProduct={onOpenProduct}
                  displayLimit={displayLimit}
                />
              ))}
              {executionOverviewModal.key === 'inventory' ? (
                <section className="ops-execution-overview-modal__section" data-section-label="当天巡检记录">
                  <Text strong className="ops-execution-overview-modal__section-title">当天巡检记录</Text>
                  <div className="ops-execution-overview-modal__section-content" style={{ marginTop: 8 }}>
                    <Table<InventoryScanRecord>
                      size="small"
                      pagination={false}
                      rowKey={(record, index) => `${record.checkedAt}-${index}`}
                      dataSource={executionOverviewModal.inventoryScans ?? []}
                      locale={{ emptyText: '今日暂无巡检记录' }}
                      columns={[
                        {
                          title: '巡检时间',
                          dataIndex: 'checkedAt',
                          key: 'checkedAt',
                          render: (value: string) => value || '-',
                        },
                        {
                          title: '断码商品数量',
                          dataIndex: 'brokenProductCount',
                          key: 'brokenProductCount',
                          render: (value: number) => (
                            <span style={{ color: Number(value) > 0 ? '#ff4d4f' : 'inherit', fontWeight: Number(value) > 0 ? 500 : 400 }}>
                              {Number(value)} 件
                            </span>
                          ),
                        },
                        {
                          title: '正常商品数量',
                          dataIndex: 'normalProductCount',
                          key: 'normalProductCount',
                          render: (value: number) => `${Number(value)} 件`,
                        },
                        {
                          title: '恢复商品数量',
                          dataIndex: 'recoveredProductCount',
                          key: 'recoveredProductCount',
                          render: (value: number) => (
                            <span style={{ color: Number(value) > 0 ? '#52c41a' : 'inherit', fontWeight: Number(value) > 0 ? 500 : 400 }}>
                              {Number(value)} 件
                            </span>
                          ),
                        },
                      ]}
                    />
                  </div>
                </section>
              ) : null}
            </>
          );
        })() : null}
        {executionOverviewModal?.key === 'recall' && executionOverviewModal.kgStrategy ? (
          <div className="ops-execution-overview-modal__kg-strategy">
            <Text strong className="ops-execution-overview-modal__section-title">KG 治理方案</Text>
            <div className="ops-execution-overview-modal__kg-actions">
              {executionOverviewModal.kgStrategy.items.map((item, index) => <div key={`${item}-${index}`}>{index + 1}. {item}</div>)}
            </div>
            <Text strong className="ops-execution-overview-modal__section-title">涉及标签</Text>
            <div className="ops-execution-overview-modal__kg-tags">
              {executionOverviewModal.kgStrategy.tags.map((tag) => (
                <div key={tag.name}>{tag.name}：{tag.status === 'pending_create' ? '待创建' : tag.resultCount != null ? `召回 ${tag.resultCount} 件商品` : '未获取召回数'}</div>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </Modal>
    <Modal
      title="商品详情"
      open={productModalOpen}
      centered
      width={DETAIL_MODAL_WIDTH}
      onCancel={onCloseProduct}
      footer={null}
      className="ops-fixed-detail-modal ops-product-detail-modal"
      styles={{ body: { height: 'auto', maxHeight: DETAIL_MODAL_BODY_MAX_HEIGHT, overflow: 'auto' } }}
      destroyOnHidden
    >
      {selectedProduct ? (
        <div data-testid="top-query-product-detail-scroll-container">
          {productDetailLoading ? (
            <div data-testid="top-query-product-detail-skeleton">
              <Skeleton.Image active style={{ width: 132, height: 164, marginBottom: 16 }} />
              <Skeleton active paragraph={{ rows: 10 }} />
            </div>
          ) : (
            <Flex vertical gap={12}>
              <ProductDetailContent
                detail={selectedProductDetail}
                fallback={{
                  title: String(selectedProduct.productName ?? selectedProduct.name ?? ''),
                  productId: String(selectedProduct.productId ?? selectedProduct.product_id ?? ''),
                  skuId: String(selectedProduct.skuId ?? selectedProduct.sku_id ?? ''),
                  spuId: String(selectedProduct.spuId ?? selectedProduct.spu ?? ''),
                  imageUrl: String(getProductImage(selectedProduct) || ''),
                  priceText: typeof selectedProduct.price === 'string' ? selectedProduct.price : undefined,
                  priceValue: typeof selectedProduct.price === 'number' ? selectedProduct.price : null,
                  category: String(selectedProduct.category ?? selectedProduct.categoryName ?? ''),
                  description: String(selectedProduct.description ?? ''),
                }}
                showScoreDetail={false}
              />
            </Flex>
          )}
        </div>
      ) : null}
    </Modal>
  </>
);
