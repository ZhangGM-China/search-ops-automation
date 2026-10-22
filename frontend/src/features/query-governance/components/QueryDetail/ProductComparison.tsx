import React from 'react';
import { CopyOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { Button, Checkbox, Col, Empty, Flex, Row, Space, Tag, Tooltip, Typography, message } from 'antd';
import { getProductImageCandidates } from './productImage';
import {
  buildSnapshotTitle,
  getProductBrokenRateStatus,
  getProductPriceDisplay,
  getProductPriceNumber,
  getProductPrimaryId,
  getProductRelevance,
  getProductRelevancePresentation,
  getSnapshotCount,
  getSnapshotProducts,
} from './productPresentation';
import { getProductBlacklistStatus, type ProductBlacklistStatus } from './productBlacklistStatus';

const { Text } = Typography;

export type { ProductBlacklistStatus } from './productBlacklistStatus';

const ProductSpu: React.FC<{ value: string }> = ({ value }) => {
  const copy = async (event: React.MouseEvent<HTMLElement>) => {
    event.stopPropagation();
    try {
      await navigator.clipboard.writeText(value);
      message.success('SPU 已复制');
    } catch {
      message.error('复制失败');
    }
  };

  return (
    <div className="ops-current-product-card__spu" title={`SPU: ${value}`}>
      <Text type="secondary">SPU: {value}</Text>
      <Tooltip title="复制 SPU">
        <Button
          type="text"
          size="small"
          className="ops-current-product-card__spu-copy"
          aria-label={`复制 SPU ${value}`}
          icon={<CopyOutlined />}
          onClick={copy}
        />
      </Tooltip>
    </div>
  );
};

const ProductCardImage: React.FC<{ product: Record<string, unknown>; rank: unknown; name: unknown }> = ({ product, rank, name }) => {
  const candidates = getProductImageCandidates(product);
  const candidateKey = candidates.join('|');
  const [imageState, setImageState] = React.useState({ key: candidateKey, index: 0 });
  const imageIndex = imageState.key === candidateKey ? imageState.index : 0;
  const image = candidates[imageIndex];

  if (image) {
    return (
      <img
        src={image}
        alt={String(name)}
        onError={() => setImageState((current) => ({
          key: candidateKey,
          index: (current.key === candidateKey ? current.index : 0) + 1,
        }))}
      />
    );
  }
  return <span>#{String(rank)}</span>;
};

export const ProductCardGrid: React.FC<{
  products: Record<string, unknown>[];
  emptyDescription: string;
  displayLimit?: number;
  variant?: 'live' | 'snapshot';
  inventoryBrokenRateThreshold?: number;
  onOpenProduct?: (product: Record<string, unknown>) => void;
  onBlacklistAction?: (product: Record<string, unknown>, action: 'blacklist' | 'cancel') => void;
  onCorrectProduct?: (product: Record<string, unknown>) => void;
  selectedProductIds?: ReadonlySet<string>;
  onToggleProductSelection?: (product: Record<string, unknown>, selected: boolean) => void;
  selectionStatus?: ProductBlacklistStatus | null;
  selectionDisabled?: boolean;
  defaultBlacklistStatus?: 'blacklisted' | 'not_blacklisted';
  blacklistActionLoadingProductId?: string | null;
  showReason?: boolean;
}> = ({ products, emptyDescription, displayLimit = 16, variant = 'snapshot', inventoryBrokenRateThreshold = 0.25, onOpenProduct, onBlacklistAction, onCorrectProduct, selectedProductIds, onToggleProductSelection, selectionStatus, selectionDisabled = false, defaultBlacklistStatus, blacklistActionLoadingProductId, showReason = false }) => {
  const isLiveVariant = variant === 'live';
  return (
    <Flex vertical gap={10}>
      {products.length ? (
        <Row gutter={[10, 10]}>
          {products.slice(0, displayLimit).map((product, index) => {
            const productId = getProductPrimaryId(product) || '-';
            const name = product.productName ?? product.name ?? product.title ?? product.product_name ?? '未命名商品';
            const rank = product.rank ?? index + 1;
            const relevance = getProductRelevance(product);
            const relevanceMeta = getProductRelevancePresentation(relevance);
            const brokenRateStatus = getProductBrokenRateStatus(product, inventoryBrokenRateThreshold);
            const priceDisplay = getProductPriceDisplay(product);
            const actionStatus = String(product.blacklistStatus ?? product.blacklist_status ?? product.status ?? '').trim().toLowerCase();
            const blacklistStatus = getProductBlacklistStatus(product, defaultBlacklistStatus);
            const actionStatusMeta = blacklistStatus === 'blacklisted'
              ? { text: '已拉黑', tone: 'neutral' }
              : actionStatus === 'not_blacklisted' || actionStatus === 'cancelled' || actionStatus === 'canceled'
                ? { text: '未拉黑', tone: 'warning' }
              : actionStatus === 'failed' || actionStatus === 'failure'
                ? { text: '拉黑失败', tone: 'danger' }
                : actionStatus === 'pending' || actionStatus === 'pending_create' || actionStatus === '待拉黑'
                  ? { text: '未拉黑', tone: 'warning' }
                  : null;
            const blacklistStatusMeta = actionStatusMeta ?? (onCorrectProduct || onBlacklistAction ? { text: '未拉黑', tone: 'warning' } : null);
            const inventoryStatus = String(product.inventoryStatus ?? '').trim();
            const inventoryStatusMeta = inventoryStatus === '已恢复'
              ? { text: '已恢复', tone: 'success' }
              : inventoryStatus === '已降桶' || inventoryStatus === '已停止' || inventoryStatus === '监测中'
                ? { text: '已降桶', tone: 'warning' }
                : null;
            const statusTagMeta = actionStatusMeta
              ?? (defaultBlacklistStatus
                ? defaultBlacklistStatus === 'blacklisted' ? { text: '已拉黑', tone: 'neutral' } : { text: '未拉黑', tone: 'warning' }
                : onCorrectProduct ? { text: '未拉黑', tone: 'warning' } : inventoryStatusMeta ?? blacklistStatusMeta);
            const failureReason = product.failureReason ?? product.failure_reason ?? product.errorMessage ?? product.error_message
              ?? (showReason ? product.reason : null);
            return (
              <Col span={24} key={`${String(productId)}-${index}`}>
                <div
                  role="button"
                  tabIndex={0}
                  aria-label={`商品 ${String(name)}`}
                  className={`ops-current-product-card ops-current-product-card--fixed ops-clickable${isLiveVariant ? ' ops-current-product-card--live' : ' ops-current-product-card--snapshot'}${onToggleProductSelection ? ' ops-current-product-card--selectable' : ''}`}
                  onClick={() => onOpenProduct?.(product)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      onOpenProduct?.(product);
                    }
                  }}
                  style={{ width: '100%', textAlign: 'left' }}
                >
                  <div className="ops-current-product-card__image">
                    <ProductCardImage product={product} rank={rank} name={name} />
                  </div>
                  <Flex vertical gap={6} className="ops-current-product-card__content">
                    <Flex justify="space-between" gap={10} align="flex-start">
                      <Flex align="center" gap={8} className="ops-current-product-card__name-line">
                        {onToggleProductSelection ? (
                          <Checkbox
                            className="ops-current-product-card__checkbox"
                            checked={selectedProductIds?.has(String(productId)) ?? false}
                            disabled={selectionDisabled || (selectionStatus != null && getProductBlacklistStatus(product, defaultBlacklistStatus) !== selectionStatus)}
                            aria-label={`选择 ${String(name)}`}
                            onClick={(event) => event.stopPropagation()}
                            onChange={(event) => onToggleProductSelection(product, event.target.checked)}
                          />
                        ) : null}
                        {!isLiveVariant && statusTagMeta ? (
                          <Tag className={`ops-product-status-tag ops-product-status-tag--${statusTagMeta.tone}`} style={{ marginInlineEnd: 0 }}>
                            {statusTagMeta.text}
                            {actionStatusMeta?.tone === 'danger' && failureReason ? (
                              <Tooltip title={String(failureReason)}>
                                <span tabIndex={0} aria-label={`拉黑失败原因：${String(failureReason)}`} style={{ marginInlineStart: 4, display: 'inline-flex' }}>
                                  <InfoCircleOutlined className="ops-tooltip-icon" />
                                </span>
                              </Tooltip>
                            ) : null}
                          </Tag>
                        ) : null}
                        <Text type="secondary" className="ops-current-product-card__rank">#{String(rank)}</Text>
                        <Text strong style={{ color: 'var(--text-primary)' }}>{String(name)}</Text>
                      </Flex>
                      <Flex gap={8} align="center" className="ops-current-product-card__actions">
                        {onCorrectProduct ? <Button type="link" size="small" className="ops-product-action-button" aria-label={`纠正 ${String(name)}`} onClick={(event) => { event.stopPropagation(); onCorrectProduct(product); }} onKeyDown={(event) => event.stopPropagation()}>纠正</Button> : null}
                        {onBlacklistAction ? (
                          <>
                            {onCorrectProduct ? <span className="ops-product-action-divider" aria-hidden="true" /> : null}
                            <Button
                              type="link"
                              size="small"
                              className="ops-product-action-button"
                              loading={blacklistActionLoadingProductId === String(productId)}
                              aria-label={`${blacklistStatus === 'blacklisted' ? '取消拉黑' : '拉黑'} ${String(name)}`}
                              onClick={(event) => {
                                event.stopPropagation();
                                onBlacklistAction(product, blacklistStatus === 'blacklisted' ? 'cancel' : 'blacklist');
                              }}
                              onKeyDown={(event) => event.stopPropagation()}
                            >
                              {blacklistStatus === 'blacklisted' ? '取消拉黑' : '拉黑'}
                            </Button>
                          </>
                        ) : null}
                      </Flex>
                    </Flex>
                    <ProductSpu value={String(productId)} />
                    {getProductPriceNumber(product) != null ? (
                      <div className="ops-current-product-card__price-line">
                        <span className={`ops-current-product-card__price-original${priceDisplay.hasDiscount ? ' ops-current-product-card__price-original--strikethrough' : ''}`}>
                          {priceDisplay.originalText}
                        </span>
                        {priceDisplay.hasDiscount ? (
                          <>
                            <span className="ops-current-product-card__price-discount">{priceDisplay.discountText}</span>
                            <span className="ops-current-product-card__price-sale">{priceDisplay.saleText}</span>
                          </>
                        ) : null}
                      </div>
                    ) : null}
                    {failureReason ? (
                      <div className="ops-current-product-card__reason">
                        <span className="ops-current-product-card__reason-label">错误原因：</span>
                        <span>{String(failureReason)}</span>
                      </div>
                    ) : null}
                    <Space size={6} wrap className="ops-current-product-card__meta">
                      {!isLiveVariant && product.brokenRateTransition ? (
                        <Tag className="ops-product-status-tag ops-product-status-tag--neutral" style={{ marginInlineEnd: 0 }}>
                          断码率 {String(product.brokenRateTransition)}
                        </Tag>
                      ) : null}
                      {!isLiveVariant && brokenRateStatus ? (
                        <Tag className={`ops-product-status-tag ops-product-status-tag--${brokenRateStatus.tone}`} style={{ marginInlineEnd: 0 }}>
                          {brokenRateStatus.text}
                        </Tag>
                      ) : null}
                      {!isLiveVariant && relevanceMeta && !showReason ? (
                        <Tag className={`ops-product-status-tag ops-product-status-tag--${relevanceMeta.tone}`} style={{ marginInlineEnd: 0 }}>
                          {relevanceMeta.text}
                        </Tag>
                      ) : null}
                    </Space>
                  </Flex>
                </div>
              </Col>
            );
          })}
        </Row>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyDescription} />
      )}
    </Flex>
  );
};

const ProductResultList: React.FC<{
  snapshot: unknown;
  emptyDescription: string;
  displayLimit?: number;
  variant?: 'live' | 'snapshot';
  inventoryBrokenRateThreshold?: number;
  onOpenProduct?: (product: Record<string, unknown>) => void;
  onBlacklistAction?: (product: Record<string, unknown>, action: 'blacklist' | 'cancel') => void;
  blacklistActionLoadingProductId?: string | null;
}> = ({ snapshot, emptyDescription, displayLimit = 16, variant = 'snapshot', inventoryBrokenRateThreshold = 0.25, onOpenProduct }) => {
  const snapshotBranch = variant === 'live' ? 'after' : 'before';
  const products = getSnapshotProducts(snapshot, snapshotBranch);

  return (
    <ProductCardGrid
      products={products}
      emptyDescription={emptyDescription}
      displayLimit={displayLimit}
      variant={variant}
      inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
      onOpenProduct={onOpenProduct}
    />
  );
};

export const SnapshotComparison: React.FC<{
  afterSnapshot: unknown;
  beforeSnapshot: unknown;
  fallbackCount?: number | null;
  displayLimit?: number;
  inventoryBrokenRateThreshold?: number;
  onOpenProduct?: (product: Record<string, unknown>) => void;
}> = ({ afterSnapshot, beforeSnapshot, fallbackCount, displayLimit = 16, inventoryBrokenRateThreshold = 0.25, onOpenProduct }) => {
  const afterProductCount = getSnapshotProducts(afterSnapshot, 'after').length;
  const beforeProductCount = getSnapshotProducts(beforeSnapshot, 'before').length;

  return <div className="ops-query-detail-compare">
    <section className="ops-query-detail-compare__column">
      <div className="ops-query-detail-compare__title">
        {buildSnapshotTitle('当前实时召回', getSnapshotCount(afterSnapshot, 'after') ?? fallbackCount, displayLimit, afterProductCount)}
      </div>
      <ProductResultList
        snapshot={afterSnapshot}
        emptyDescription="暂无当前实时召回商品"
        displayLimit={displayLimit}
        variant="live"
        inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
        onOpenProduct={onOpenProduct}
      />
    </section>
    <section className="ops-query-detail-compare__column">
      <div className="ops-query-detail-compare__title">
        {buildSnapshotTitle('治理前商品快照', getSnapshotCount(beforeSnapshot, 'before'), displayLimit, beforeProductCount, true)}
      </div>
      <ProductResultList
        snapshot={beforeSnapshot}
        emptyDescription="暂无治理前商品快照"
        displayLimit={displayLimit}
        variant="snapshot"
        inventoryBrokenRateThreshold={inventoryBrokenRateThreshold}
        onOpenProduct={onOpenProduct}
      />
    </section>
  </div>;
};
