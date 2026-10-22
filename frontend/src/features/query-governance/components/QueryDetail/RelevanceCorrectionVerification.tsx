import React from 'react';
import { Button, Empty, Flex, Modal, Typography, message } from 'antd';

import { dashboardApi } from '@/shared/api';
import type { ManualRelevanceCheckResponse, ManualRelevanceProductResult } from '@/shared/types';
import { formatBackendDateTime } from '@/shared/lib/backendTime';
import { getApiErrorMessage } from '@/shared/lib/error';
import { getProductPrimaryId } from './productPresentation';
import { ProductCardGrid } from './ProductComparison';

const { Text } = Typography;

type Props = {
  brand: string;
  query: string;
  products: Array<Record<string, unknown>>;
  displayLimit: number;
  inventoryBrokenRateThreshold?: number;
  onOpenProduct: (product: Record<string, unknown>) => void;
  check: ManualRelevanceCheckResponse | null;
  onCheckChange: (check: ManualRelevanceCheckResponse | null) => void;
};

const activeStatuses = new Set(['queued', 'running']);

const requestKey = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return `manual-${Date.now()}-${Math.random().toString(16).slice(2)}`;
};

const formatVerificationTime = (value?: string | null) => {
  if (!value) return '暂无验证';
  const formatted = formatBackendDateTime(value, 'YYYY-MM-DD HH:mm:ss');
  return formatted ? `最近验证：${formatted}` : '暂无验证';
};

const productKey = (product: Record<string, unknown>) => String(
  getProductPrimaryId(product)
  || product.sku
  || product.skuId
  || product.sku_id
  || product.productId
  || product.product_id
  || '',
);

const productLookupKeys = (product: Record<string, unknown>) => [
  product.productId, product.product_id, product.sku, product.skuId, product.sku_id,
  product.spu, product.spuId, product.spu_id,
].map((value) => String(value || '').trim()).filter(Boolean);

const resultProducts = (
  response: ManualRelevanceCheckResponse | null,
  query: string,
  sourceProducts: Array<Record<string, unknown>>,
) => {
  const queryResult = response?.queries?.find((item) => item.query === query);
  if (!queryResult) return [];
  const sourceByKey = new Map(sourceProducts.flatMap((product) => productLookupKeys(product).map((key) => [key, product] as const)));
  return queryResult.products
    .filter((product): product is ManualRelevanceProductResult & { isRelevant: boolean } => typeof product.isRelevant === 'boolean')
    .sort((left, right) => Number(right.isRelevant === false) - Number(left.isRelevant === false))
    .map((product) => ({
      ...(sourceByKey.get(String(product.sku)) || {}),
      productId: sourceByKey.get(String(product.sku))?.productId || sourceByKey.get(String(product.sku))?.product_id || product.sku,
      productName: sourceByKey.get(String(product.sku))?.productName || sourceByKey.get(String(product.sku))?.product_name || sourceByKey.get(String(product.sku))?.name || `商品 ${product.sku}`,
      failureReason: product.isRelevant === false ? product.reason : undefined,
      relevance: product.isRelevant,
      sku: product.sku,
    }));
};

export const RelevanceCorrectionVerification: React.FC<Props> = ({
  brand,
  query,
  products,
  displayLimit,
  inventoryBrokenRateThreshold = 0.25,
  onOpenProduct,
  check,
  onCheckChange,
}) => {
  const [open, setOpen] = React.useState(false);
  const [starting, setStarting] = React.useState(false);

  const startVerification = async () => {
    if (starting || (check && activeStatuses.has(check.status))) return;
    const skus = [...new Set(products.slice(0, displayLimit).map(productKey).filter(Boolean))];
    if (!skus.length) {
      message.warning('当前 Query 暂无可验证商品');
      return;
    }
    setStarting(true);
    try {
      const response = await dashboardApi.submitManualRelevanceCheck(brand, { query, products: skus }, requestKey());
      onCheckChange({
        checkId: response.data.checkId,
        brand,
        status: response.data.status,
        createdAt: check?.createdAt,
        updatedAt: check?.updatedAt,
        queries: [],
      });
    } catch (error: unknown) {
      message.error(getApiErrorMessage(error, '提交相关性验证失败'));
    } finally {
      setStarting(false);
    }
  };

  const active = Boolean(check && activeStatuses.has(check.status));
  const latestTime = check?.updatedAt || check?.createdAt;
  const verificationProducts = resultProducts(check, query, products);
  const incorrectProducts = verificationProducts.filter((product) => product.relevance === false);
  const correctProducts = verificationProducts.filter((product) => product.relevance === true);

  return (
    <>
      <Button type="link" size="small" className="ops-relevance-verification-trigger" onClick={() => setOpen(true)}>
        纠正验证
      </Button>
      <Modal
        title="纠正验证"
        open={open}
        centered
        width={720}
        onCancel={() => setOpen(false)}
        footer={null}
        className="ops-fixed-detail-modal ops-relevance-verification-modal"
        destroyOnHidden
      >
        <Flex vertical gap={12}>
          <div className="ops-relevance-verification-modal__toolbar">
            <div>
              <Button type="primary" loading={starting} disabled={active || !products.length} onClick={() => void startVerification()}>
                {active ? '执行中' : '开始验证'}
              </Button>
              <div className="ops-relevance-verification-modal__time">{formatVerificationTime(latestTime)}</div>
            </div>
          </div>
          {verificationProducts.length ? (
            <Flex vertical gap={14}>
              {incorrectProducts.length ? (
                <section>
                  <Text strong>错误商品（{incorrectProducts.length}）</Text>
                  <ProductCardGrid products={incorrectProducts} emptyDescription="暂无错误商品" displayLimit={displayLimit} inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} showReason />
                </section>
              ) : null}
              {correctProducts.length ? (
                <section>
                  <Text strong>正确商品（{correctProducts.length}）</Text>
                  <ProductCardGrid products={correctProducts} emptyDescription="暂无正确商品" displayLimit={displayLimit} inventoryBrokenRateThreshold={inventoryBrokenRateThreshold} onOpenProduct={onOpenProduct} showReason />
                </section>
              ) : null}
            </Flex>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无最近验证结果" />
          )}
        </Flex>
      </Modal>
    </>
  );
};

export default RelevanceCorrectionVerification;
