import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type React from 'react';
import { QueryDetailOverlays } from './QueryDetailOverlays';

const base = {
  onCloseProduct: vi.fn(), onCloseTrendDrawer: vi.fn(),
  productDetailLoading: false, productModalOpen: true, selectedProduct: { name: '商品', brand: 'atlas', productId: 'p1' },
  selectedProductDetail: null,
  task: { queryText: '连衣裙', dataDate: '2026-01-01' }, trendDrawerOpen: true, trend: [],
};

describe('QueryDetailOverlays', () => {
  it('renders product and trend overlays without retired governance-record modals', () => {
    render(<QueryDetailOverlays {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)} />);
    expect(screen.getAllByText('商品').length).toBeGreaterThan(0); expect(screen.getByText('商品详情')).toBeInTheDocument();
    expect(screen.queryByText('事件输出')).not.toBeInTheDocument(); expect(screen.queryByText('动作详情')).not.toBeInTheDocument();
    expect(screen.queryByText('当前展示任务历史快照')).not.toBeInTheDocument();
  });

  it('does not render snapshot metadata banners for live detail', () => {
    render(<QueryDetailOverlays {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)} productDetailLoading={false} />);
    expect(screen.queryByText('实时详情已由任务快照补全')).not.toBeInTheDocument();
  });

  it('centers execution detail modal and does not repeat the recall card conclusion', () => {
    render(
      <QueryDetailOverlays
        {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)}
        executionOverviewModal={{
          key: 'recall',
          label: '召回数量',
          state: 'normal',
          summary: '本次召回 8 件，数量充足，无需调整。',
          details: [{ label: '执行结果', value: '本次召回 8 件，数量充足，无需调整。' }],
        }}
        productModalOpen={false}
        trendDrawerOpen={false}
      />,
    );

    expect(document.querySelector('.ant-modal-centered')).toBeInTheDocument();
    expect(screen.getAllByText('本次召回 8 件，数量充足，无需调整。')).toHaveLength(1);
    expect(screen.queryByText('执行结果')).not.toBeInTheDocument();
  });

  it('puts the recall conclusion first and counts on one form row', () => {
    render(
      <QueryDetailOverlays
        {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)}
        executionOverviewModal={{
          key: 'recall', label: '召回数量', state: 'normal', summary: '召回不足',
          details: [
            { label: '当前召回数量', value: '2 件' },
            { label: '上一次召回数量', value: '暂无' },
            { label: '耗时', value: '1.20s' },
            { label: 'Token', value: '120' },
            { label: '结论', value: '本次召回 2 件，数量不足。' },
            { label: '执行结果', value: '已生成异步任务' },
          ],
        }}
        productModalOpen={false}
        trendDrawerOpen={false}
      />,
    );

    const body = document.querySelector('.ops-execution-overview-modal__body');
    const rows = [...document.querySelectorAll('.ops-execution-overview-modal__row')];
    expect(body?.firstElementChild).toHaveAttribute('data-section-label', '结论');
    expect(body?.firstElementChild).toHaveClass('ops-execution-overview-modal__section--conclusion');
    expect(rows[0]).toHaveTextContent('当前召回数量2 件上一次召回数量暂无耗时1.20sToken120');
    expect(rows[0]).toHaveClass('ops-execution-overview-modal__row--columns-4');
    expect(rows).toHaveLength(1);
  });

  it('renders KG actions and involved tags one item per line', () => {
    render(
      <QueryDetailOverlays
        {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)}
        executionOverviewModal={{
          key: 'recall', label: '召回数量', state: 'normal',
          summary: '本次召回 2 件，数量不足，需要交由 KG Agent 生成治理策略。',
          details: [],
          kgStrategy: {
            items: ['新增标签：轻薄', '将轻薄与外套建立从属关系'],
            tags: [
              { name: '轻薄', status: 'pending_create', resultCount: null },
              { name: '外套', status: 'exists', resultCount: 12 },
            ],
          },
        }}
        productModalOpen={false}
        trendDrawerOpen={false}
      />,
    );

    expect(screen.getByText('1. 新增标签：轻薄')).toBeInTheDocument();
    expect(screen.getByText('2. 将轻薄与外套建立从属关系')).toBeInTheDocument();
    expect(screen.getByText('轻薄：待创建')).toBeInTheDocument();
    expect(screen.getByText('外套：召回 12 件商品')).toBeInTheDocument();
  });

  it('renders execution details as fixed-height forms and product cards', () => {
    const onOpenProduct = vi.fn();
    render(
      <QueryDetailOverlays
        {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)}
        onOpenProduct={onOpenProduct}
        executionOverviewModal={{
          key: 'open_factor', label: '重排序', state: 'failed', summary: '需要进行重排序。',
          details: [
            { label: '当前 CTR', value: '47.94%' }, { label: '近期 CTR', value: '54.12%' },
            { label: 'CTR 变化', value: '下降 6.18%' }, { label: 'CTR 告警阈值', value: '下降 ≥ 5 %' },
            { label: '当前 CVR', value: '2.00%' }, { label: '近期 CVR', value: '3.00%' },
            { label: 'CVR 变化', value: '下降 1.00%' }, { label: 'CVR 告警阈值', value: '下降 ≥ 2 %' },
            { label: '当前召回商品数', value: '12 件' }, { label: '低召回豁免阈值', value: '≤ 4 件' },
            { label: '耗时', value: '1.20s' }, { label: 'Token', value: '120' },
            { label: '结论', value: '需要进行重排序。' },
            { label: 'AI 重排序策略', value: '优先展示轻薄款' },
            { label: '向量重排序商品（前 16）', value: '共 1 件商品', rawValue: [{ productId: 'p1', productName: '商品一' }] },
            { label: '重排序执行结果', value: '失败' }, { label: '错误原因', value: '排序结果与预期不一致，系统已撤销本次排序。' },
          ],
        }}
        productModalOpen={false}
        trendDrawerOpen={false}
      />,
    );

    expect(document.querySelector('.ops-execution-overview-modal__form')).toBeInTheDocument();
    expect(screen.getByText('商品一')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '商品 商品一' }));
    expect(onOpenProduct).toHaveBeenCalledWith(expect.objectContaining({ productId: 'p1' }));
    expect(screen.queryByText('执行状态')).not.toBeInTheDocument();
    expect(document.querySelectorAll('.ops-execution-overview-modal__row')).toHaveLength(3);
    expect(document.querySelectorAll('.ops-execution-overview-modal__row')[2]).toHaveClass('ops-execution-overview-modal__row--columns-4');
    expect(document.querySelectorAll('.ops-execution-overview-modal__row')[2]).toHaveTextContent('当前召回商品数12 件低召回豁免阈值≤ 4 件耗时1.20sToken120');
    expect(document.querySelectorAll('.ops-execution-overview-modal__row')[2].querySelectorAll('.ops-execution-overview-modal__field--empty')).toHaveLength(0);
    expect(document.querySelector('.ant-modal-wrap .ant-modal')).toHaveStyle({ width: '840px' });
    expect(document.querySelector('.ops-execution-overview-modal__body')).toHaveStyle({ height: '760px' });
  });

  it('renders relevance metrics in the form and product lists as standalone sections', () => {
    const onOpenProduct = vi.fn();
    render(
      <QueryDetailOverlays
        {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)}
        onOpenProduct={onOpenProduct}
        executionOverviewModal={{
          key: 'relevance', label: '相关性', state: 'normal',
          summary: '检测 2 件：相关 1 件，本轮准确率 50.00%',
          details: [
            { label: '准确率', value: '50.00%' }, { label: 'AI 检测', value: '2 件' },
            { label: '历史复用', value: '1 件' }, { label: '正确商品', value: '1 件' },
            { label: '错误商品', value: '1 件' },
            { label: '耗时', value: '1.20s' }, { label: 'Token', value: '120' },
            { label: '错误商品', value: '共 1 件商品', rawValue: [{ productId: 'new-2', productName: '黑色外套', failureReason: '图片不可用，无法确认商品是否为白色' }] },
            { label: '正确商品', value: '共 1 件商品', rawValue: [{ productId: 'new-1', productName: '白色衬衫' }] },
            { label: '结论', value: '检测 2 件：相关 1 件，本轮准确率 50.00%' },
          ],
        }}
        productModalOpen={false}
        trendDrawerOpen={false}
      />,
    );

    const body = document.querySelector('.ops-execution-overview-modal__body');
    const rows = [...document.querySelectorAll('.ops-execution-overview-modal__row')];
    expect(body?.firstElementChild).toHaveAttribute('data-section-label', '结论');
    expect([...rows[0].querySelectorAll('.ops-execution-overview-modal__field-title')].map((node) => node.textContent)).toEqual([
      'AI 检测', '历史复用', '正确商品', '错误商品',
    ]);
    expect(rows[0]).toHaveClass('ops-execution-overview-modal__row--columns-4');
    expect(rows[1]).toHaveClass('ops-execution-overview-modal__row--columns-4');
    expect(rows[1].querySelectorAll('.ops-execution-overview-modal__field--empty')).toHaveLength(1);
    expect(rows).toHaveLength(2);
    const productSections = [...(body?.querySelectorAll('[data-section-label]') ?? [])]
      .filter((section) => ['正确商品', '错误商品'].includes(section.getAttribute('data-section-label') ?? ''));
    expect(productSections.map((section) => section.getAttribute('data-section-label'))).toEqual(['错误商品', '正确商品']);
    expect(productSections[0]).toHaveTextContent('黑色外套');
    expect(productSections[1]).toHaveTextContent('白色衬衫');
    expect(screen.getByText('图片不可用，无法确认商品是否为白色')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '商品 白色衬衫' }));
    expect(onOpenProduct).toHaveBeenCalledWith(expect.objectContaining({ productId: 'new-1' }));
  });

  it('hides retired relevance remediation fields and keeps verification fields', () => {
    const hiddenLabels = ['拉黑状态', '根因分组', 'KG 策略', '重新打标签'];
    render(
      <QueryDetailOverlays
        {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)}
        executionOverviewModal={{
          key: 'relevance', label: '相关性', state: 'normal',
          summary: '检测 2 件：相关 1 件，本轮准确率 50.00%',
          details: [
            { label: 'AI 检测', value: '2 件' }, { label: '历史复用', value: '0 件' },
            { label: '正确商品', value: '1 件' }, { label: '错误商品', value: '1 件' },
            { label: '拉黑状态', value: 'AI 已执行' }, { label: '根因分组', value: '1 组' },
            { label: 'KG 策略', value: '无需处理' }, { label: '重新打标签', value: '未触发' },
            { label: '验证时间', value: '2026-09-20 10:30' }, { label: '最终结论', value: '验证通过' },
            { label: '结论', value: '检测 2 件：相关 1 件，本轮准确率 50.00%' },
          ],
        }}
        productModalOpen={false}
        trendDrawerOpen={false}
      />,
    );

    const sectionLabels = [...document.querySelectorAll('[data-section-label]')]
      .map((section) => section.getAttribute('data-section-label'));
    hiddenLabels.forEach((label) => {
      expect(sectionLabels).not.toContain(label);
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    });
    expect(screen.queryByText('AI 已执行')).not.toBeInTheDocument();
    expect(screen.getByText('验证时间')).toBeInTheDocument();
    expect(screen.getByText('2026-09-20 10:30')).toBeInTheDocument();
    expect(screen.getByText('最终结论')).toBeInTheDocument();
    expect(screen.getByText('验证通过')).toBeInTheDocument();
  });

  it('uses an enlarged execution modal and an adaptive product detail modal', () => {
    render(
      <QueryDetailOverlays
        {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)}
        executionOverviewModal={{ key: 'relevance', label: '相关性', state: 'normal', summary: '完成', details: [] }}
      />,
    );

    const executionModal = document.querySelector('.ops-execution-overview-modal');
    const productModal = document.querySelector('.ops-product-detail-modal');
    expect(executionModal).toHaveStyle({ width: '840px' });
    expect(executionModal?.querySelector('.ant-modal-body')).toHaveStyle({ height: '760px' });
    expect(productModal).toHaveStyle({ width: '840px' });
    expect(productModal?.querySelector('.ant-modal-body')).toHaveStyle({ height: 'auto' });
  });

  it('puts the conclusion first and keeps metric fields in one continuous form', () => {
    render(
      <QueryDetailOverlays
        {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)}
        executionOverviewModal={{
          key: 'relevance', label: '相关性', state: 'normal', summary: '检测 2 件：相关 1 件，本轮准确率 50.00%',
          details: [
            { label: '准确率', value: '50.00%' }, { label: 'AI 检测', value: '2 件' },
            { label: '历史复用', value: '1 件' }, { label: '正确商品', value: '1 件' },
            { label: '错误商品', value: '0 件' },
            { label: '耗时', value: '1.20s' }, { label: 'Token', value: '120' },
            { label: '正确商品', value: '共 1 件商品', rawValue: [{ productId: 'new-1', productName: '白色衬衫' }] },
            { label: '错误商品', value: '暂无错误商品', rawValue: [] },
            { label: '结论', value: '检测 2 件：相关 1 件，本轮准确率 50.00%' },
          ],
        }}
        productModalOpen={false}
        trendDrawerOpen={false}
      />,
    );

    const body = document.querySelector('.ops-execution-overview-modal__body');
    const form = body?.querySelector('.ops-execution-overview-modal__form');
    const conclusion = body?.querySelector('[data-section-label="结论"]');
    expect(body?.firstElementChild).toBe(conclusion);
    expect(conclusion).toHaveClass('ops-execution-overview-modal__section--conclusion');
    expect(conclusion?.querySelector('.ops-execution-overview-modal__section-title')).toHaveClass('ops-execution-overview-modal__section-title--conclusion');
    expect(conclusion).toHaveTextContent('检测 2 件：相关 1 件，本轮准确率 50.00%');
    expect(conclusion).not.toHaveClass('ops-execution-overview-modal__field');
    expect(form).toBeInTheDocument();
    expect(form?.querySelectorAll('.ops-execution-overview-modal__row')).toHaveLength(2);
    expect(form?.querySelector('.ops-execution-overview-modal__field-title')).toHaveTextContent('AI 检测');
    expect(form?.querySelector('.ops-execution-overview-modal__field-content')).toHaveTextContent('2 件');
    expect(body?.querySelectorAll('.ops-execution-overview-modal__field-title')).toHaveLength(7);
    expect(form?.querySelector('.ops-execution-overview-modal__row')).toHaveClass('ops-execution-overview-modal__row--columns-4');
    expect(form?.querySelectorAll('.ops-execution-overview-modal__row')[1]).toHaveClass('ops-execution-overview-modal__row--columns-4');
    expect(form?.querySelectorAll('.ops-execution-overview-modal__row')[1].querySelectorAll('.ops-execution-overview-modal__field--empty')).toHaveLength(1);
  });

  it('keeps reorder strategy, products, result and error as standalone sections', () => {
    render(
      <QueryDetailOverlays
        {...(base as unknown as React.ComponentProps<typeof QueryDetailOverlays>)}
        executionOverviewModal={{
          key: 'open_factor', label: '重排序', state: 'failed', summary: '需要进行重排序。',
          details: [
            { label: '当前 CTR', value: '47.94%' }, { label: '近期 CTR', value: '54.12%' },
            { label: 'CTR 变化', value: '下降 6.18%' }, { label: 'CTR 告警阈值', value: '下降 ≥ 5 %' },
            { label: '结论', value: '需要进行重排序。' },
            { label: 'AI 重排序策略', value: '优先展示轻薄款' },
            { label: '向量重排序商品（前 16）', value: '共 1 件商品', rawValue: [{ productId: 'p1', productName: '商品一' }] },
            { label: '重排序执行结果', value: '失败' },
            { label: '错误原因', value: '排序结果暂时无法确认，请稍后检查搜索结果。' },
          ],
        }}
        productModalOpen={false}
        trendDrawerOpen={false}
      />,
    );

    const body = document.querySelector('.ops-execution-overview-modal__body');
    const labels = [...(body?.querySelectorAll('[data-section-label]') ?? [])].map((node) => node.getAttribute('data-section-label'));
    expect(labels).toEqual(['结论', 'AI 重排序策略', '向量重排序商品（前 16）', '重排序执行结果', '错误原因']);
    expect(body?.querySelectorAll('.ops-execution-overview-modal__field')).toHaveLength(4);
    expect(body?.querySelectorAll('.ops-execution-overview-modal__form')).toHaveLength(1);
    expect(body?.querySelector('[data-section-label="向量重排序商品（前 16）"]')).not.toHaveClass('ops-execution-overview-modal__field');
  });
});
