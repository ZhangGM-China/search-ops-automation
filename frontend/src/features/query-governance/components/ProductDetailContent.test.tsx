import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import ProductDetailContent from './ProductDetailContent';

describe('ProductDetailContent', () => {
  it('displays description line breaks as text without HTML markup', () => {
    const { container } = render(<ProductDetailContent detail={{ description: '高腰-长版<br/><br />高腰牛仔裤，五口袋。' }} />);
    expect(container.textContent).toContain('高腰-长版\n\n高腰牛仔裤，五口袋。');
    expect(container.textContent).not.toContain('<br');
  });
  it('renders detail fields, tags, materials and score explanation', () => {
    render(<ProductDetailContent detail={{ productName: '外套', spuId: 'SPU-1', brand: 'Atlas', description: '品牌方原始描述', price: 100, discountPrice: 80, stock: 3, status: 1, isNew: 1, score: 0.9, sales: 12, gender: '女', color: '黑色', releaseDate: '2026-01-01T12:00:00Z', category: '[女装>外套]', tags: ['防水'], tagName: '秋季,防水', whiteList: '白名单', blackList: '黑名单', material: '{"a":{"materials":{"棉":"50","涤纶":"50.5"}}}', size: 'S/M', dimensions: { width: '50' }, attr1: '属性', ext: { bucket: 'A', scoreDetail: '说明' } } as never} />);
    expect(screen.getAllByText('外套').length).toBeGreaterThan(0);
    expect(screen.getByText('SPU SPU-1')).toBeInTheDocument();
    expect(screen.getByText('¥80')).toBeInTheDocument();
    expect(screen.getByText('品牌方原始描述')).toBeInTheDocument();
    expect(screen.queryByText('品牌')).not.toBeInTheDocument();
    expect(screen.getByText('棉 50% / 涤纶 50.5%')).toBeInTheDocument();
    expect(screen.getAllByText('白名单').length).toBeGreaterThan(0);
    expect(screen.getAllByText('黑名单').length).toBeGreaterThan(0);
    expect(screen.getByText('说明')).toBeInTheDocument();
  });
  it('uses fallback fields and hides optional score detail', () => {
    render(<ProductDetailContent fallback={{ title: '备用商品', productId: 'P1', priceText: '面议', category: '女装>上衣', description: '描述' }} showScoreDetail={false} />);
    expect(screen.getByText('备用商品')).toBeInTheDocument();
    expect(screen.getByText('SPU P1')).toBeInTheDocument();
    expect(screen.getByText('描述')).toBeInTheDocument();
  });
  it('handles malformed materials and optional flags', () => {
    render(<ProductDetailContent detail={{ productName: '商品', material: '棉; 涤纶', isNew: 0, isDiscount: true, isClearance: false, status: 0, releaseDate: 'bad', shelveStartTime: '2026-01-01' } as never} />);
    expect(screen.getByText('棉 / 涤纶')).toBeInTheDocument();
    expect(screen.getByText('下架')).toBeInTheDocument();
    expect(screen.getAllByText('否').length).toBeGreaterThan(0);
    expect(screen.getAllByText('是').length).toBeGreaterThan(0);
    expect(screen.getByText('bad')).toBeInTheDocument();
  });
});
