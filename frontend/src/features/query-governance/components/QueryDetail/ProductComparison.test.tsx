import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ProductCardGrid, SnapshotComparison } from './ProductComparison';

describe('ProductComparison', () => {
  it('renders an empty state and a bounded product card list', () => {
    render(<ProductCardGrid products={[]} emptyDescription="没有结果" />);
    expect(screen.getByText('没有结果')).toBeInTheDocument();
    const onOpen = vi.fn();
    render(<ProductCardGrid products={[{ productId: 'p1', name: '商品一', rank: 2, price: 100, discountPrice: 80, broken_size_rate: 0.5, relevance: { isRelevant: false, reason: 'TextReason: 不相关' }, image: 'a' }]} emptyDescription="x" onOpenProduct={onOpen} />);
    expect(screen.getByText('商品一')).toBeInTheDocument();
    expect(screen.getByText('SPU: p1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '商品 商品一' }));
    expect(onOpen).toHaveBeenCalled();
  });

  it('uses a human-readable error reason with an auto-height card', () => {
    render(<ProductCardGrid products={[{ productId: 'p1', name: '错误商品', failureReason: '图片不可用', status: 'blacklisted' }]} emptyDescription="x" />);
    expect(screen.getByText('错误原因：')).toBeInTheDocument();
    expect(screen.queryByText('不相关原因：')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '商品 错误商品' })).toHaveClass('ops-current-product-card--fixed');
    const status = screen.getByText('已拉黑');
    const name = screen.getByText('错误商品');
    const nameLine = status.closest('.ops-current-product-card__name-line');
    expect(nameLine).toBe(name.closest('.ops-current-product-card__name-line'));
    expect(nameLine).toHaveClass('ops-current-product-card__name-line');
    expect(status.compareDocumentPosition(name) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('places the selection control inline before the blacklist status', () => {
    render(<ProductCardGrid
      products={[{ productId: 'p1', name: '可选商品', status: 'not_blacklisted' }]}
      emptyDescription="x"
      selectedProductIds={new Set()}
      onToggleProductSelection={() => undefined}
    />);

    const checkbox = screen.getByRole('checkbox', { name: '选择 可选商品' });
    const nameLine = screen.getByText('未拉黑').closest('.ops-current-product-card__name-line');
    expect(nameLine).toContainElement(checkbox);
    expect(checkbox.closest('.ops-current-product-card__checkbox')).not.toBeNull();
    expect(checkbox.compareDocumentPosition(screen.getByText('未拉黑')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(nameLine?.textContent?.indexOf('未拉黑')).toBeGreaterThan(-1);
    expect(screen.getByRole('button', { name: '商品 可选商品' })).toHaveClass('ops-current-product-card--selectable');
  });

  it('shows inventory demotion/recovery tags and the broken-rate transition', () => {
    render(<ProductCardGrid products={[
      { productId: 'p1', name: '已降桶商品', inventoryStatus: '已停止', broken_size_rate: 0.83 },
      { productId: 'p2', name: '已恢复商品', inventoryStatus: '已恢复', broken_size_rate: 0.13, brokenRateTransition: '83% → 13%' },
    ]} emptyDescription="x" />);

    expect(screen.getByText('已降桶')).toBeInTheDocument();
    expect(screen.getByText('已恢复')).toBeInTheDocument();
    expect(screen.getByText('断码率 83% → 13%')).toBeInTheDocument();
  });

  it('places blacklist controls on the matching product card and stops the card click', () => {
    const onOpen = vi.fn();
    const onBlacklistAction = vi.fn();
    render(<ProductCardGrid
      products={[
        { productId: 'p1', name: '已拉黑商品', status: 'blacklisted' },
        { productId: 'p2', name: '已取消商品', status: 'not_blacklisted' },
      ]}
      emptyDescription="x"
      onOpenProduct={onOpen}
      onBlacklistAction={onBlacklistAction}
    />);

    fireEvent.click(screen.getByRole('button', { name: '取消拉黑 已拉黑商品' }));
    expect(onBlacklistAction).toHaveBeenCalledWith(expect.objectContaining({ productId: 'p1' }), 'cancel');
    expect(onOpen).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '拉黑 已取消商品' }));
    expect(onBlacklistAction).toHaveBeenCalledWith(expect.objectContaining({ productId: 'p2' }), 'blacklist');
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('uses the resolved blacklist fallback for the action when the product status is a display label', () => {
    const onBlacklistAction = vi.fn();
    render(<ProductCardGrid
      products={[{ productId: 'p1', name: '已拉黑商品', status: 'AI 已执行' }]}
      emptyDescription="x"
      onBlacklistAction={onBlacklistAction}
      defaultBlacklistStatus="blacklisted"
    />);

    expect(screen.getByText('已拉黑')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '取消拉黑 已拉黑商品' }));
    expect(onBlacklistAction).toHaveBeenCalledWith(expect.objectContaining({ productId: 'p1' }), 'cancel');
  });

  it('keeps a long SPU on one line and copies it from the card', () => {
    const spu = 'C08727223406,3607223406,6688224406,5359224406,8727228406';
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    render(<ProductCardGrid products={[{ productId: spu, name: '商品一' }]} emptyDescription="x" />);

    expect(screen.getByTitle(`SPU: ${spu}`)).toHaveClass('ops-current-product-card__spu');
    fireEvent.click(screen.getByRole('button', { name: `复制 SPU ${spu}` }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(spu);
  });

  it('renders live and before snapshots with counts and fallback images', () => {
    render(<SnapshotComparison afterSnapshot={{ after: { resultCount: 1, products: [{ productId: 'a', title: '后商品' }] } }} beforeSnapshot={{ before: { result_count: 0, products: [] } }} />);
    expect(screen.getByText(/当前实时召回/)).toHaveTextContent('1');
    expect(screen.getByText('后商品')).toBeInTheDocument();
    expect(screen.getAllByText(/治理前商品快照/)[0]).toHaveTextContent('0');
  });

  it('uses the authoritative total when the snapshot only contains a Top N page', () => {
    const products = Array.from({ length: 24 }, (_, index) => ({ productId: `p-${index}`, name: `商品${index}` }));
    render(<SnapshotComparison
      afterSnapshot={{ after: { resultCount: 102, products } }}
      beforeSnapshot={{ before: { resultCount: 102, products } }}
      displayLimit={24}
    />);

    expect(screen.getByText(/当前实时召回/)).toHaveTextContent('总计 102 个商品，仅展示前 24 个');
    expect(screen.getByText(/治理前商品快照/)).toHaveTextContent('总计 102 个商品，仅展示前 24 个');
    expect(screen.getAllByRole('button', { name: /商品 商品/ })).toHaveLength(48);
  });

  it('reports the immutable snapshot size when it is smaller than the current display limit', () => {
    const liveProducts = Array.from({ length: 24 }, (_, index) => ({ productId: `live-${index}`, name: `实时商品${index}` }));
    const snapshotProducts = Array.from({ length: 16 }, (_, index) => ({ productId: `snapshot-${index}`, name: `快照商品${index}` }));
    render(<SnapshotComparison
      afterSnapshot={{ after: { resultCount: 480, products: liveProducts } }}
      beforeSnapshot={{ before: { resultCount: 480, products: snapshotProducts } }}
      displayLimit={24}
    />);

    expect(screen.getByText(/当前实时召回/)).toHaveTextContent('总计 480 个商品，仅展示前 24 个');
    expect(screen.getByText(/治理前商品快照/)).toHaveTextContent('总计 480 个商品，快照仅保存前 16 个');
    expect(screen.getAllByRole('button', { name: /商品 实时商品/ })).toHaveLength(24);
    expect(screen.getAllByRole('button', { name: /商品 快照商品/ })).toHaveLength(16);
  });
});
