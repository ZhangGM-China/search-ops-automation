import React from 'react';
import dayjs from 'dayjs';
import { Image, Tag } from 'antd';
import type { OpsTaskProductApiDetail } from '@/shared/types';

export interface ProductDetailFallback {
  title?: string | null;
  productId?: string | null;
  skuId?: string | null;
  spuId?: string | null;
  imageUrl?: string | null;
  priceText?: string | null;
  priceValue?: number | null;
  category?: string | null;
  stock?: number | null;
  description?: string | null;
}

export interface ProductDetailContentProps {
  detail?: OpsTaskProductApiDetail | null;
  fallback?: ProductDetailFallback | null;
  showScoreDetail?: boolean;
}

const parseCategoryText = (value?: string | null) => {
  if (!value) return [];
  return value
    .replace(/^\[|\]$/g, '')
    .split(/[>,|]/)
    .flatMap((item) => item.split(','))
    .map((item) => item.trim())
    .filter(Boolean);
};

const parseTagText = (value?: string | null) => {
  if (!value) return [];
  return value
    .split(/[;,，、|]/)
    .map((item) => item.trim())
    .filter(Boolean);
};

const parseMaterialText = (value?: string | null) => {
  if (!value) return '';
  try {
    const parsed = JSON.parse(value) as Record<string, { materials?: Record<string, string> }>;
    const pairs = Object.values(parsed)
      .flatMap((section) => Object.entries(section.materials || {}))
      .map(([name, percent]) => `${name} ${Number(percent).toFixed(Number(percent) % 1 === 0 ? 0 : 1)}%`);
    return pairs.join(' / ');
  } catch {
    return value.split(/[;,，]/).map((item) => item.trim()).filter(Boolean).join(' / ');
  }
};

const formatProductDate = (value?: string | null) => {
  if (!value) return '';
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm') : value;
};

const formatProductPrice = (primary?: number | null, fallback?: string | null) => {
  if (typeof primary === 'number') return `¥${primary.toFixed(primary % 1 === 0 ? 0 : 2)}`;
  return fallback || '-';
};

const formatBooleanFlag = (value?: number | boolean | null) => {
  if (value === undefined || value === null) return undefined;
  if (typeof value === 'boolean') return value ? '是' : '否';
  return value === 1 ? '是' : value === 0 ? '否' : undefined;
};

const formatNumberValue = (value?: number | null) => (typeof value === 'number' ? String(value) : undefined);

const formatProductDescription = (value?: string | null) => (
  value ? value.replace(/<br\s*\/?>/gi, '\n') : ''
);

const mergeTagValues = (...sources: Array<string | string[] | null | undefined>) => [
  ...new Set(
    sources.flatMap((source) => {
      if (!source) return [];
      if (Array.isArray(source)) return source.map((item) => item.trim()).filter(Boolean);
      return parseTagText(source);
    }),
  ),
];

const FieldGrid: React.FC<{ title?: string; columns?: number; items: Array<{ label: string; value: string }> }> = ({
  title,
  columns = 4,
  items,
}) => {
  if (!items.length) return null;
  return (
    <div>
      {title ? <div style={{ color: 'var(--text-secondary)', fontSize: 13, marginBottom: 8 }}>{title}</div> : null}
      <div style={{ display: 'grid', gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`, gap: 10 }}>
        {items.map((item) => (
          <div key={item.label} className="ops-subtle-box" style={{ padding: '8px 10px' }}>
            <div style={{ color: 'var(--text-tertiary)', fontSize: 12, marginBottom: 4 }}>{item.label}</div>
            <div style={{ color: 'var(--text-primary)', fontWeight: 650, wordBreak: 'break-word' }}>{item.value}</div>
          </div>
        ))}
      </div>
    </div>
  );
};

const TagSection: React.FC<{ title: string; items: string[]; color?: string }> = ({ title, items, color }) => {
  if (!items.length) return null;
  return (
    <div>
      <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{title}</div>
      <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {items.map((item) => (
          <Tag key={`${title}-${item}`} color={color} style={{ marginInlineEnd: 0 }}>
            {item}
          </Tag>
        ))}
      </div>
    </div>
  );
};

const ProductDetailContent: React.FC<ProductDetailContentProps> = ({
  detail,
  fallback,
  showScoreDetail = true,
}) => {
  const title = detail?.productName || fallback?.title || '未命名商品';
  const imageUrl = detail?.mainImage
    || detail?.imageUrl
    || (Array.isArray(detail?.imageUrls) ? detail?.imageUrls[0] : undefined)
    || fallback?.imageUrl
    || '';
  const spuText = detail?.spuId || detail?.spu || fallback?.spuId || detail?.productId || fallback?.productId || '';
  const categories = [
    ...new Set([
      ...(detail?.categories || []),
      ...parseCategoryText(detail?.category),
      ...parseCategoryText(detail?.multiCategory),
      ...parseCategoryText(fallback?.category),
    ]),
  ].filter(Boolean).slice(0, 12);
  const tags = [
    ...new Set([
      ...(detail?.tags || []).filter((item) => item.trim()),
      ...parseTagText(detail?.tagName),
    ]),
  ].slice(0, 24);
  const whiteListTags = mergeTagValues(detail?.whiteList, detail?.whiteListAi);
  const blackListTags = mergeTagValues(detail?.blackList, detail?.blackListAi);
  const material = parseMaterialText(detail?.material);
  const stockValue = detail?.stock ?? detail?.inventory ?? detail?.ext?.stock ?? fallback?.stock;
  const basicFields = [
    { label: '主类目', value: detail?.mainCategory || fallback?.category || undefined },
    { label: '价格', value: formatProductPrice(detail?.discountPrice ?? detail?.price ?? fallback?.priceValue, fallback?.priceText) },
    { label: '库存', value: stockValue != null ? String(stockValue) : undefined },
    { label: '状态', value: detail?.status === 1 ? '上架' : detail?.status === 0 ? '下架' : undefined },
    { label: '新品', value: formatBooleanFlag(detail?.isNew) },
    { label: '分数', value: typeof detail?.score === 'number' ? detail.score.toFixed(2) : undefined },
    { label: '销量', value: formatNumberValue(detail?.sales ?? detail?.salesVolume) },
    { label: '性别', value: detail?.gender },
    { label: '颜色', value: detail?.color },
    { label: '发布日期', value: formatProductDate(detail?.releaseDate) },
    { label: '召回桶', value: detail?.ext?.bucket },
  ].filter((item): item is { label: string; value: string } => Boolean(item.value));
  const secondaryFields = [
    { label: '折扣商品', value: formatBooleanFlag(detail?.isDiscount) },
    { label: '清仓商品', value: formatBooleanFlag(detail?.isClearance) },
  ].filter((item): item is { label: string; value: string } => Boolean(item.value));
  const timeFields = [
    { label: '上架开始', value: formatProductDate(detail?.shelveStartTime) },
    { label: '上架结束', value: formatProductDate(detail?.shelveEndTime) },
    { label: '上架时间', value: formatProductDate(detail?.listTime) },
    { label: '创建时间', value: formatProductDate(detail?.createTime) },
    { label: '更新时间', value: formatProductDate(detail?.updateTime) },
    { label: '修改时间', value: formatProductDate(detail?.modifyTime) },
    { label: '新鲜度日期', value: formatProductDate(detail?.freshnessDate) },
  ].filter((item): item is { label: string; value: string } => Boolean(item.value));
  const dimensionFields = Object.entries(detail?.dimensions || {})
    .filter(([, value]) => Boolean(value && String(value).trim()))
    .map(([label, value]) => ({ label, value: String(value).trim() }));
  const attrFields = ([
    ['ATTR1', detail?.attr1],
    ['ATTR2', detail?.attr2],
    ['ATTR3', detail?.attr3],
    ['ATTR4', detail?.attr4],
    ['ATTR5', detail?.attr5],
    ['ATTR6', detail?.attr6],
    ['ATTR7', detail?.attr7],
    ['ATTR8', detail?.attr8],
    ['ATTR9', detail?.attr9],
    ['ATTR10', detail?.attr10],
  ] as Array<[string, string | null | undefined]>)
    .filter(([, value]) => Boolean(value && String(value).trim()))
    .map(([label, value]) => ({ label, value: String(value).trim() }));
  const description = formatProductDescription(detail?.description || fallback?.description);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        <div className="ops-current-product-card__image" style={{ width: 132, height: 164, flexShrink: 0 }}>
          {imageUrl ? (
            <Image
              src={imageUrl}
              alt={title}
              width={132}
              height={164}
              style={{ objectFit: 'cover' }}
              preview={{ mask: '点击放大' }}
            />
          ) : <span>商品</span>}
        </div>
        <div style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <h3 style={{ margin: 0, color: 'var(--text-primary)', fontSize: 20, lineHeight: 1.3, fontWeight: 650 }}>{title}</h3>
          {spuText ? <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{`SPU ${spuText}`}</div> : null}
          {description ? <p style={{ margin: 0, color: 'var(--text-primary)', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>{description}</p> : null}
        </div>
      </div>

      <FieldGrid title="基础信息" items={basicFields} />
      <FieldGrid items={secondaryFields} />

      {detail?.size || material ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10 }}>
          {detail?.size ? (
            <div className="ops-subtle-box" style={{ padding: 10 }}>
              <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>尺码</div>
              <p style={{ margin: '6px 0 0', color: 'var(--text-primary)', lineHeight: 1.55 }}>{detail.size}</p>
            </div>
          ) : null}
          {material ? (
            <div className="ops-subtle-box" style={{ padding: 10 }}>
              <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>材质</div>
              <p style={{ margin: '6px 0 0', color: 'var(--text-primary)', lineHeight: 1.55 }}>{material}</p>
            </div>
          ) : null}
        </div>
      ) : null}

      <FieldGrid title="维度信息" columns={2} items={dimensionFields} />

      <TagSection title="白名单" items={whiteListTags} />
      <TagSection title="黑名单" items={blackListTags} color="volcano" />

      {categories.length ? (
        <div>
          <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>类目</div>
          <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {categories.map((item) => (
              <Tag key={item} style={{ marginInlineEnd: 0 }}>
                {item}
              </Tag>
            ))}
          </div>
        </div>
      ) : null}

      {tags.length ? (
        <div>
          <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>标签</div>
          <div style={{ marginTop: 8, maxHeight: 112, overflow: 'auto', display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {tags.map((item) => (
              <Tag key={item} color="blue" style={{ marginInlineEnd: 0 }}>
                {item}
              </Tag>
            ))}
          </div>
        </div>
      ) : null}

      <FieldGrid title="时间信息" items={timeFields} />
      <FieldGrid title="扩展属性" columns={4} items={attrFields} />

      {showScoreDetail && detail?.ext?.scoreDetail ? (
        <div className="ops-subtle-box" style={{ padding: 10 }}>
          <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>分数说明</div>
          <p style={{ margin: '6px 0 0', color: 'var(--text-primary)', whiteSpace: 'pre-wrap', lineHeight: 1.55 }}>{detail.ext.scoreDetail}</p>
        </div>
      ) : null}
    </div>
  );
};

export default ProductDetailContent;
