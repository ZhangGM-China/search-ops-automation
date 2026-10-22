import React from 'react';
import { Modal, Skeleton, message } from 'antd';
import { dashboardApi } from '@/shared/api';
import type { OpsTaskProductApiDetail } from '@/shared/types';
import { ProductDetailContent } from '../../query-governance/public';

type ProductTarget = {
  brand: string;
  sku: string;
  productName?: string | null;
  imageUrl?: string | null;
};

type Props = {
  open: boolean;
  product: ProductTarget | null;
  onClose: () => void;
};

const OperationsProductDetailModal: React.FC<Props> = ({ open, product, onClose }) => {
  const [detail, setDetail] = React.useState<OpsTaskProductApiDetail | null>(null);
  const [loading, setLoading] = React.useState(false);

  React.useEffect(() => {
    if (!open || !product) return;
    let active = true;
    queueMicrotask(() => { if (active) { setDetail(null); setLoading(true); } });
    dashboardApi.getTopQueryProductDetail(product.brand, product.sku)
      .then((response) => { if (active) setDetail(response.data.product); })
      .catch(() => { if (active) message.error('获取商品详情失败，已展示列表信息'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [open, product]);

  return (
    <Modal title="商品详情" open={open} centered footer={null} onCancel={onClose} destroyOnHidden styles={{ body: { maxHeight: '70vh', overflow: 'auto' } }}>
      {product ? loading ? <><Skeleton.Image active style={{ width: 132, height: 164, marginBottom: 16 }} /><Skeleton active paragraph={{ rows: 8 }} /></> : (
        <ProductDetailContent detail={detail} fallback={{ title: product.productName, productId: product.sku, skuId: product.sku, imageUrl: product.imageUrl }} showScoreDetail={false} />
      ) : null}
    </Modal>
  );
};

export default OperationsProductDetailModal;
