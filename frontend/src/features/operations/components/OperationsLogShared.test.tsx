import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import dayjs from 'dayjs';
import { OperationsLogTabs, OperationsLogDateSwitcher, ProductIdentityCell } from './OperationsLogShared';

describe('Operations log shared controls', () => {
  it('renders the four operation tabs and navigates by route', () => {
    render(<MemoryRouter initialEntries={['/blacklist']}><OperationsLogTabs /></MemoryRouter>);
    expect(screen.getByRole('tab', { name: '相关性检测' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '断码检测' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '无结果词' })).toBeInTheDocument();
  });

  it('supports adjacent date switching', () => {
    const onChange = vi.fn();
    render(<OperationsLogDateSwitcher value={dayjs('2026-09-10')} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: '前一天' }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ $D: 9 }));
    fireEvent.click(screen.getByRole('button', { name: '后一天' }));
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it('shows product identity and copies SKU', async () => {
    const onOpen = vi.fn();
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    render(<ProductIdentityCell name="商品一" sku="SKU-1" imageUrl="https://img.test/a.jpg" onClick={onOpen} />);
    expect(screen.getByText('商品一')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '复制 SKU SKU-1' }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('SKU-1');
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('商品一'));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('opens details from the image and SKU text, but not from the copy icon', () => {
    const onOpen = vi.fn();
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    render(<ProductIdentityCell name="商品一" sku="SKU-1" imageUrl="https://img.test/a.jpg" onClick={onOpen} />);

    fireEvent.click(screen.getByAltText('商品一'));
    fireEvent.click(screen.getByText('SKU-1'));
    expect(onOpen).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: '复制 SKU SKU-1' }));
    expect(onOpen).toHaveBeenCalledTimes(2);
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('SKU-1');

    fireEvent.keyDown(screen.getByRole('button', { name: '复制 SKU SKU-1' }), { key: 'Enter' });
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it('uses a stable placeholder when the product name is missing or only contains SKU values', () => {
    const { container } = render(
      <ProductIdentityCell name="C01618450044,1063451044" sku="C01618450044,1063451044" />,
    );

    expect(screen.getByText('暂无商品名称')).toBeInTheDocument();
    expect(container.querySelector('.product-identity-cell__placeholder')).toBeInTheDocument();
    expect(screen.getByText('C01618450044,1063451044')).toBeInTheDocument();
  });
});
