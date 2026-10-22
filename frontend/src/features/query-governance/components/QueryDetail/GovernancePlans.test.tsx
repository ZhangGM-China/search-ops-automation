import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { GovernancePlans } from './GovernancePlans';

const basePlan = {
  id: 'plan-1', taskId: 1, actionId: 10, actionIds: [10], actionType: 'query_reorder',
  title: 'AI 重排序策略', objective: '', summary: '优先展示轻薄款', reason: 'CTR 下降',
  status: 'pending', statusLabel: '待执行', executionMode: 'human_approval', executable: true,
  cancelable: false, steps: [], evidence: [], metrics: {},
};

describe('GovernancePlans', () => {
  it('allows selecting products with one blacklist state and exposes one matching batch action', async () => {
    let resolveBatch!: (value: boolean) => void;
    const batch = new Promise<boolean>((resolve) => { resolveBatch = resolve; });
    const onBatchBlacklistProducts = vi.fn(() => batch);
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'blacklist-batch', type: 'blacklist', title: '错误商品', actionType: 'product_blacklist',
        products: [
          { productId: 'P1', productName: '已拉黑商品', status: 'blacklisted' },
          { productId: 'P2', productName: '未拉黑商品', status: 'not_blacklisted' },
        ],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onBatchBlacklistProducts={onBatchBlacklistProducts}
    />);

    const batchButton = screen.getByRole('button', { name: '批量取消拉黑' });
    expect(batchButton).toBeDisabled();
    const blacklistedCheckbox = screen.getByRole('checkbox', { name: '选择 已拉黑商品' });
    const unblacklistedCheckbox = screen.getByRole('checkbox', { name: '选择 未拉黑商品' });
    fireEvent.click(blacklistedCheckbox);
    expect(screen.getByRole('button', { name: '批量取消拉黑' })).not.toBeDisabled();
    expect(unblacklistedCheckbox).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '批量取消拉黑' }));
    expect(onBatchBlacklistProducts).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'blacklist' }),
      [expect.objectContaining({ productId: 'P1' })],
      'cancel',
    );
    expect(screen.getByRole('button', { name: /批量取消拉黑/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /批量取消拉黑/ })).toHaveClass('ant-btn-loading');
    expect(blacklistedCheckbox).toBeDisabled();
    await act(async () => {
      resolveBatch(true);
      await batch;
    });
    expect(screen.getByRole('button', { name: /批量取消拉黑/ })).toBeDisabled();
    expect(blacklistedCheckbox).not.toBeDisabled();
    expect(unblacklistedCheckbox).not.toBeDisabled();
  });

  it('changes the batch action to blacklist when an unblacklisted product is selected', () => {
    const onBatchBlacklistProducts = vi.fn().mockResolvedValue(true);
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'blacklist-batch-unblacklisted', type: 'blacklist', title: '错误商品', actionType: 'product_blacklist',
        products: [{ productId: 'P3', productName: '待拉黑商品', status: 'not_blacklisted' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onBatchBlacklistProducts={onBatchBlacklistProducts}
    />);

    fireEvent.click(screen.getByRole('checkbox', { name: '选择 待拉黑商品' }));
    const batchButton = screen.getByRole('button', { name: '批量拉黑' });
    expect(batchButton).not.toBeDisabled();
    fireEvent.click(batchButton);
    expect(onBatchBlacklistProducts).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'blacklist' }),
      [expect.objectContaining({ productId: 'P3' })],
      'blacklist',
    );
  });

  it('places the batch action beside the error-product title without a selection summary', () => {
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'blacklist-header-action', type: 'blacklist', title: '错误商品', actionType: 'product_blacklist',
        status: 'pending', statusLabel: '待执行',
        products: [{ productId: 'P4', productName: '错误商品', status: 'not_blacklisted' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onBatchBlacklistProducts={vi.fn().mockResolvedValue(true)}
    />);

    const title = screen.getByText('错误商品（1）');
    const header = title.closest('.ops-governance-cause-group__products-header');
    expect(header).toContainElement(screen.getByRole('button', { name: '批量取消拉黑' }));
    expect(header).not.toHaveTextContent('待执行');
    expect(screen.queryByText(/已选择/)).not.toBeInTheDocument();
  });

  it('keeps a failed strategy title and status separate from the error-product list', () => {
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'failed-strategy', type: 'kg', title: '相关性治理调整方案', actionType: 'relevance_plan',
        status: 'failed', statusLabel: '生成失败', summary: '生成失败', products: [],
      }, {
        ...basePlan,
        id: 'error-products', type: 'blacklist', title: '错误商品', actionType: 'product_blacklist',
        status: 'pending', statusLabel: '待执行', products: [{ productId: 'P5', productName: '错误商品', status: 'not_blacklisted' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onBatchBlacklistProducts={vi.fn().mockResolvedValue(true)}
    />);

    const strategy = screen.getByText('相关性治理调整方案').closest('section');
    const errors = screen.getByText('错误商品（1）').closest('section');
    expect(strategy).toHaveTextContent('生成失败');
    expect(errors).not.toHaveTextContent('生成失败');
    expect(errors).not.toHaveTextContent('待执行');
  });

  it('opens reordered products from the detail link and forwards product clicks', () => {
    const onOpenProduct = vi.fn();
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        type: 'reorder',
        products: [
          { productId: 'P1', productName: '轻薄外套' },
          { productId: 'P2', productName: '休闲外套' },
        ],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={onOpenProduct}
    />);

    expect(screen.queryByText('轻薄外套')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看详情' }));
    expect(screen.getByRole('dialog', { name: '重排序商品列表' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '商品 轻薄外套' }));
    expect(onOpenProduct).toHaveBeenCalledWith(expect.objectContaining({ productId: 'P1' }));
  });

  it('opens reordered products when the content card itself is clicked', () => {
    render(<GovernancePlans
      plans={[{ ...basePlan, type: 'reorder', products: [{ productId: 'P1', productName: '轻薄外套' }] }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    fireEvent.click(screen.getByRole('button', { name: '重排序商品卡片，共 1 个' }));
    expect(screen.getByRole('dialog', { name: '重排序商品列表' })).toBeInTheDocument();
  });

  it('centers the taller reorder modal and keeps its strategy above the scrolling product list', () => {
    render(<GovernancePlans
      plans={[{ ...basePlan, type: 'reorder', products: [{ productId: 'P1', productName: '轻薄外套' }] }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    fireEvent.click(screen.getByRole('button', { name: '重排序商品卡片，共 1 个' }));
    const dialog = screen.getByRole('dialog', { name: '重排序商品列表' });
    const modal = dialog.closest('.ant-modal');
    expect(modal).toHaveClass('ops-fixed-detail-modal', 'ops-reorder-products-modal');
    expect(modal?.closest('.ant-modal-wrap')).toHaveClass('ant-modal-centered');
    const strategy = dialog.querySelector('.ops-reorder-products-modal__strategy');
    const productList = dialog.querySelector('.ops-reorder-products-modal__list');
    expect(strategy).toHaveTextContent('AI 重排序策略');
    expect(strategy).toHaveTextContent('优先展示轻薄款');
    expect(productList).toContainElement(screen.getByRole('button', { name: '商品 轻薄外套' }));
    expect(strategy?.nextElementSibling).toBe(productList);
  });

  it('moves blacklist cancellation from the plan header onto each product card', () => {
    const onCancelPlan = vi.fn();
    const onBlacklistProduct = vi.fn().mockResolvedValue(true);
    const onCorrectProduct = vi.fn().mockResolvedValue(true);
    render(<GovernancePlans
      plans={[
        {
          ...basePlan, id: 'blacklist-1', type: 'blacklist', title: '拉黑商品', actionType: 'product_blacklist',
          summary: '共 1 个商品将被拉黑', reason: '相关性检测判定商品不相关',
          status: 'verified', statusLabel: 'AI 已执行', executorType: 'ai_auto', executionMode: 'auto', executable: false, cancelable: true,
          products: [{ productId: 'P2', productName: '错误商品', reason: '与 Query 意图不相关', status: 'blacklisted' }],
        },
        {
          ...basePlan, id: 'reorder-1', type: 'reorder', products: [],
          summary: '', reason: '',
          status: 'verified', statusLabel: 'AI 已执行', executorType: 'ai_auto', executionMode: 'auto', executable: false, cancelable: false,
        },
      ] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onCancelPlan={onCancelPlan}
      onBlacklistProduct={onBlacklistProduct}
      onCorrectProduct={onCorrectProduct}
      query="穆勒鞋 女士"
    />);

    expect(screen.getByText('错误商品（1）')).toBeInTheDocument();
    expect(screen.getByText('错误商品（1）').closest('.ops-governance-plan')).toHaveClass('ops-governance-plan--flat');
    expect(screen.queryByText('共 1 个商品将被拉黑')).not.toBeInTheDocument();
    expect(screen.queryByText('原因：相关性检测判定商品不相关')).not.toBeInTheDocument();
    expect(screen.getByText('错误原因：')).toBeInTheDocument();
    expect(screen.getByText('与 Query 意图不相关')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^取\s*消$/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '取消拉黑 错误商品' }));
    expect(onBlacklistProduct).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'blacklist' }),
      expect.objectContaining({ productId: 'P2' }),
      'cancel',
      undefined,
    );
  });

  it('uses one grouped error-product list and labels each root cause directly', () => {
    render(<GovernancePlans
      plans={[
        {
          ...basePlan,
          id: 'blacklist-duplicate', type: 'blacklist', title: '错误商品', actionType: 'product_blacklist',
          status: 'verified', statusLabel: 'AI 已执行', executorType: 'ai_auto', products: [{ productId: 'P1', productName: '错误商品', imageUrl: 'https://img/P1.jpg', status: 'blacklisted' }],
        },
        {
          ...basePlan,
          id: 'relevance-grouped', type: 'kg', title: '相关性治理 Agent 调整方案', actionType: 'relevance_plan', products: [],
          causeGroups: [
            { groupId: 'individual-1', rootCause: 'individual', conclusion: '个例，需拉黑', productIds: ['P1'], status: 'pending', statusLabel: '待执行' },
            { groupId: 'kg-1', rootCause: 'kg_issue', productIds: [], actions: [{ step: 1, actionType: 'add_tag', title: '创建标签', description: '调整标签' }], status: 'pending', statusLabel: '待执行' },
          ],
        },
      ] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    expect(screen.getByText('错误商品（1）')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '商品 错误商品' })).toHaveLength(1);
    expect(screen.getByText('策略 1')).toBeInTheDocument();
    expect(screen.getByText('个例，需拉黑')).toBeInTheDocument();
    expect(screen.getByText('策略 2')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: '错误商品' })).toHaveAttribute('src', 'https://img/P1.jpg');
    expect(screen.queryByText('product_blacklist')).not.toBeInTheDocument();
  });

  it('shows a per-product blacklist entry after a previous cancellation', () => {
    const onBlacklistProduct = vi.fn().mockResolvedValue(true);
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'blacklist-cancelled',
        type: 'blacklist',
        title: '拉黑商品',
        actionType: 'product_blacklist',
        status: 'verified',
        statusLabel: 'AI 已执行',
        executorType: 'ai_auto',
        executionMode: 'auto',
        executable: false,
        cancelable: true,
        products: [{ productId: 'P3', productName: '已恢复商品', status: 'not_blacklisted' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onBlacklistProduct={onBlacklistProduct}
    />);

    fireEvent.click(screen.getByRole('button', { name: '拉黑 已恢复商品' }));
    expect(onBlacklistProduct).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'blacklist' }),
      expect.objectContaining({ productId: 'P3' }),
      'blacklist',
      undefined,
    );
  });

  it('opens a correction modal for an unblacklisted error product and confirms the rule', () => {
    const onCorrectProduct = vi.fn().mockResolvedValue(true);
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'blacklist-correction',
        type: 'blacklist',
        title: '错误商品',
        products: [{ productId: 'P4', productName: '待确认商品', status: 'pending', reason: '相关性不符' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onCorrectProduct={onCorrectProduct}
      query="夹克 女士"
    />);

    expect(screen.getByText('未拉黑')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '纠正 待确认商品' }));
    expect(screen.getByRole('dialog', { name: '补充Query相关性检测规则' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认' })).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('请输入相关性纠正规则'), { target: { value: '该商品符合夹克 Query' } });
    fireEvent.click(screen.getByRole('button', { name: '确认' }));
    expect(onCorrectProduct).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'blacklist' }),
      expect.objectContaining({ productId: 'P4' }),
      '该商品符合夹克 Query',
      'only',
    );
  });

  it('offers cancellation together with correction for a blacklisted error product', () => {
    const onCorrectProduct = vi.fn().mockResolvedValue(true);
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'blacklist-correction-blacklisted',
        type: 'blacklist',
        title: '错误商品',
        status: 'verified',
        statusLabel: 'AI 已执行',
        products: [{ productId: 'P5', productName: '已拉黑商品', status: 'blacklisted', reason: '相关性不符' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onCorrectProduct={onCorrectProduct}
    />);

    fireEvent.click(screen.getByRole('button', { name: '纠正 已拉黑商品' }));
    expect(screen.getByRole('button', { name: '纠正并取消拉黑' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '仅纠正' })).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('请输入相关性纠正规则'), { target: { value: '商品与 Query 相关' } });
    fireEvent.click(screen.getByRole('button', { name: '仅纠正' }));
    expect(onCorrectProduct).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'blacklist' }),
      expect.objectContaining({ productId: 'P5' }),
      '商品与 Query 相关',
      'only',
    );
  });

  it('uses a verified blacklist plan when the product has no status field', () => {
    const onCorrectProduct = vi.fn().mockResolvedValue(true);
    render(<GovernancePlans
      plans={[{
        ...basePlan, id: 'verified-without-product-status', type: 'blacklist',
        status: 'verified', statusLabel: 'AI 已执行',
        products: [{ productId: 'P6', productName: '已执行商品' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onCorrectProduct={onCorrectProduct}
    />);

    expect(screen.getByText('已拉黑')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '纠正 已执行商品' }));
    expect(screen.getByRole('button', { name: '仅纠正' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '纠正并取消拉黑' })).toBeInTheDocument();
  });

  it('renders grouped root causes with structured actions, folded evidence, and group execution', () => {
    const onExecutePlan = vi.fn();
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'blacklist-grouped',
        type: 'blacklist',
        title: '拉黑商品',
        actionType: 'product_blacklist',
        actionId: null,
        actionIds: [21, 22],
        products: [],
        errorProductIds: ['P1', 'P2'],
        executable: false,
        causeGroups: [
          {
            groupId: 'individual-1',
            rootCause: 'individual',
            conclusion: '个例，需拉黑',
            productIds: ['P1'],
            needKgAdjustment: false,
            executable: false,
            status: 'verified',
            statusLabel: 'AI 已执行',
          },
          {
            groupId: 'kg-1',
            title: '新增标签并建立标签关系',
            rootCause: 'kg_issue',
            conclusion: '标签关系过宽',
            productIds: ['P2'],
            needKgAdjustment: true,
            executable: true,
            status: 'pending',
            statusLabel: '待执行',
            actionIds: [22],
            actions: [
              { step: 1, actionType: 'add_tag', params: { tagName: '轻薄西装' } },
              { step: 2, actionType: 'add_relationship', dependsOn: [1], params: { tagName: '西装', responseTagName: '轻薄西装', relationships: '902227092548485120' } },
            ],
            analysisEvidence: ['轻薄、西装', '西装 -> 羊毛西装'],
            reasons: ['西装与羊毛西装关系过宽'],
            confidence: 0.92,
          },
        ],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onExecutePlan={onExecutePlan}
    />);

    expect(screen.getAllByText('错误商品（1）')).toHaveLength(2);
    expect(screen.getByText('策略 1')).toBeInTheDocument();
    expect(screen.getByText('策略 2')).toBeInTheDocument();
    expect(screen.getByText('创建标签：')).toBeInTheDocument();
    expect(screen.getByText('创建标签关系：')).toBeInTheDocument();
    expect(screen.queryByText('轻薄、西装')).not.toBeInTheDocument();
    expect(screen.queryByText('查看分析依据')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '一键执行' }));
    expect(onExecutePlan).toHaveBeenCalledWith(expect.objectContaining({
      id: 'blacklist-grouped:kg-1',
      actionIds: [22],
    }));
  });

  it('shows only pending KG actions under a strategy and keeps cause text in product cards', () => {
    const { container } = render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'strategy-copy',
        type: 'kg',
        actionType: 'relevance_plan',
        products: [{ productId: 'P1', productName: '针织帽子', reason: '商品不是女士针织服装' }],
        causeGroups: [{
          groupId: 'kg-copy',
          rootCause: 'kg_issue',
          productIds: ['P1'],
          reasons: ['商品不是女士针织服装'],
          conclusion: '商品不是女士针织服装',
          actions: [{ actionType: 'add_tag', params: { name: '针织' } }],
          executable: true,
          status: 'pending',
          statusLabel: '待执行',
        }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    expect(container.querySelector('.ops-governance-plan__reason')).not.toBeInTheDocument();
    expect(container.querySelector('.ops-governance-cause-group__conclusion')).not.toBeInTheDocument();
    expect(screen.getByText('创建标签：')).toBeInTheDocument();
    expect(screen.getByText('错误原因：')).toBeInTheDocument();
    expect(screen.getByText('商品不是女士针织服装')).toBeInTheDocument();
  });

  it('keeps each strategy with only its products and its batch controls', () => {
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'grouped-local-products',
        type: 'kg',
        title: '相关性治理 Agent 调整方案',
        actionType: 'relevance_plan',
        products: [],
        causeGroups: [
          {
            groupId: 'kg-1', rootCause: 'kg_issue', productIds: ['P1'], executable: true,
            status: 'pending', statusLabel: '待执行',
            actions: [{ step: 1, actionType: 'add_tag', params: { name: '羊毛' } }],
          },
          {
            groupId: 'individual-2', rootCause: 'individual', productIds: ['P2'], executable: false,
            status: 'pending', statusLabel: '待执行', actions: [],
          },
        ],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onBatchBlacklistProducts={vi.fn().mockResolvedValue(true)}
    />);

    const groups = screen.getAllByTestId('governance-cause-group');
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveTextContent('策略 1');
    expect(groups[0]).toHaveTextContent('羊毛');
    expect(groups[0]).toHaveTextContent('错误商品（1）');
    expect(groups[1]).toHaveTextContent('策略 2');
    expect(groups[1]).toHaveTextContent('个例，需拉黑');
    expect(groups[1]).toHaveTextContent('错误商品（1）');
    expect(screen.queryByText('错误商品（2）')).not.toBeInTheDocument();
  });

  it('runs a group batch only for its selected products and retains the other group selection', async () => {
    const onBatch = vi.fn().mockResolvedValue(true);
    render(<GovernancePlans plans={[{
      ...basePlan, type: 'kg', id: 'group-batch', products: [],
      causeGroups: [
        { groupId: 'g1', rootCause: 'individual', products: [{ productId: 'P1', productName: '商品一', status: 'blacklisted' }] },
        { groupId: 'g2', rootCause: 'individual', products: [{ productId: 'P2', productName: '商品二', status: 'blacklisted' }] },
      ],
    }] as never} displayLimit={16} inventoryBrokenRateThreshold={0.25} onOpenProduct={() => undefined} onBatchBlacklistProducts={onBatch} />);
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 商品一' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 商品二' }));
    const groups = screen.getAllByTestId('governance-cause-group');
    await act(async () => { fireEvent.click(within(groups[0]).getByRole('button', { name: '批量取消拉黑' })); });
    expect(onBatch).toHaveBeenCalledWith(expect.objectContaining({ causeGroupId: 'g1' }), [expect.objectContaining({ productId: 'P1' })], 'cancel');
    expect(screen.getByRole('checkbox', { name: '选择 商品一' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: '选择 商品二' })).toBeChecked();
    expect(within(groups[1]).getByRole('button', { name: '批量取消拉黑' })).not.toBeDisabled();
  });

  it('opens the selected tag attributes from a KG action', () => {
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'tag-detail', type: 'kg', title: '相关性治理 Agent 调整方案', actionType: 'relevance_plan', products: [],
        causeGroups: [{
          groupId: 'kg-detail', rootCause: 'kg_issue', productIds: [], executable: false,
          status: 'pending', statusLabel: '待执行', actions: [{
            step: 1, actionType: 'add_tag', params: {
              name: '羊毛', viewTag: '羊毛', isRoot: '1', dimensions: ['材质'], weight: 10,
              isEnglish: 0, segmentingAgain: '1', isCompose: '1', nameAnd: ['羊绒'], nameNot: ['羊'],
            },
          }],
        }],
      }] as never}
      brand="atlas"
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    fireEvent.click(screen.getByRole('button', { name: '查看标签 羊毛' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('标签名称');
    expect(screen.getByRole('dialog')).toHaveTextContent('英文分词');
    expect(screen.getByRole('dialog')).toHaveTextContent('二次分词');
    expect(screen.getByRole('dialog')).toHaveTextContent('合成词的包含词');
    expect(screen.getByRole('dialog')).not.toHaveTextContent('标签 ID');
  });

  it('keeps rendering legacy KG plan steps without grouped causes', () => {
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'legacy-kg',
        type: 'kg',
        title: '召回数量治理策略',
        products: [],
        steps: [{ index: 1, title: '创建新标签', description: '创建轻薄西装标签' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    expect(screen.getByText('创建新标签')).toBeInTheDocument();
    expect(screen.getByText('创建轻薄西装标签')).toBeInTheDocument();
  });

  it('shows only a safe status for a failed KG generation', () => {
    const onExecutePlan = vi.fn();
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'failed-kg',
        type: 'kg',
        title: '召回数量治理策略',
        status: 'failed',
        statusLabel: '生成失败',
        summary: 'pymysql.err.DataError: Data too long for column response_json',
        reason: 'INSERT INTO query_governance_action_plan VALUES (...)',
        failureStage: 'generation',
        executable: true,
        steps: [{ index: 1, title: '不应展示的步骤', description: '原始 JSON' }],
        evidence: [{ label: '原始错误', value: 'SQL 参数' }],
        products: [{ productId: 'P1', productName: '不应展示的商品' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onExecutePlan={onExecutePlan}
    />);

    const section = screen.getByText('召回数量治理策略').closest('section');
    expect(section).toHaveTextContent('召回数量治理策略生成失败');
    expect(section).not.toHaveTextContent('pymysql.err.DataError');
    expect(section).not.toHaveTextContent('response_json');
    expect(section).not.toHaveTextContent('INSERT INTO');
    expect(section).not.toHaveTextContent('generation');
    expect(section).not.toHaveTextContent('不应展示的步骤');
    expect(section).not.toHaveTextContent('原始错误');
    expect(section).not.toHaveTextContent('不应展示的商品');
    expect(section?.querySelectorAll('button')).toHaveLength(0);
  });

  it('uses the same flat layout for non-blacklist governance plans', () => {
    render(<GovernancePlans
      plans={[{ ...basePlan, type: 'reorder', products: [] }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    expect(screen.getByText('AI 重排序策略').closest('section')).toHaveClass('ops-governance-plan--flat');
  });

  it('keeps reorder execution status in the title row and only shows generated strategy content', () => {
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        type: 'reorder',
        statusLabel: 'AI 已执行',
        summary: '优先展示轻薄、通勤款式',
        reason: 'CTR 告警',
        products: [{ productId: 'P1', productName: '轻薄外套' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    const section = screen.getByText('AI 重排序策略').closest('section');
    const header = section?.querySelector('.ops-governance-plan__header');
    const statusControls = header?.querySelector('.ant-space');
    expect(header).toContainElement(screen.getByText('AI 已执行'));
    expect(header).not.toHaveClass('ant-flex-wrap-wrap');
    expect(statusControls).not.toHaveStyle({ flexWrap: 'wrap' });
    expect(screen.getByText('优先展示轻薄、通勤款式')).toHaveClass('ops-governance-plan__strategy');
    expect(screen.queryByText('原因：CTR 告警')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看详情' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重排序商品卡片，共 1 个' })).toHaveClass('ops-governance-plan__content');
  });

  it('shows a reorder failure reason only when execution fails', () => {
    const { rerender } = render(<GovernancePlans
      plans={[{ ...basePlan, type: 'reorder', statusLabel: 'AI 已执行', reason: 'CTR/CVR 指标达到重排序条件', products: [{ productId: 'P1', productName: '轻薄外套' }] }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    expect(screen.queryByText('原因：CTR/CVR 指标达到重排序条件')).not.toBeInTheDocument();

    rerender(<GovernancePlans
      plans={[{ ...basePlan, type: 'reorder', statusLabel: 'AI 执行失败', reason: '排序接口执行失败', products: [{ productId: 'P1', productName: '轻薄外套' }] }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    expect(screen.getByText(/排序接口执行失败/)).toBeInTheDocument();
  });

  it('hides reorder strategy content and products when the strategy was not generated', () => {
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        type: 'reorder',
        statusLabel: 'AI 执行失败',
        summary: 'AI 生成排序策略',
        reason: 'CTR 告警',
        products: [{ productId: 'P1', productName: '轻薄外套' }],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    expect(screen.getByText('AI 重排序策略')).toBeInTheDocument();
    expect(screen.getByText('AI 执行失败')).toBeInTheDocument();
    expect(screen.queryByText('AI 生成排序策略')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /查看 .* 个重排序商品/ })).not.toBeInTheDocument();
    expect(screen.getByText(/CTR 告警/)).toBeInTheDocument();
  });

  it('uses semantic colors for all governance plan display statuses', () => {
    const statuses = [
      '待执行', '执行中', 'AI 已执行', '手动已执行', 'AI 执行失败', '手动执行失败',
      '结果待确认', '回滚中', '已回滚', '回滚失败', '已取消',
      '生成中', '生成失败', '无需调整', '需人工复核',
    ];
    render(<GovernancePlans
      plans={statuses.map((status, index) => ({
        ...basePlan,
        id: `plan-${index}`,
        title: `方案 ${index}`,
        statusLabel: status,
        executable: false,
      })) as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
    />);

    for (const status of statuses) expect(screen.getByText(status)).toBeInTheDocument();
    expect(screen.getByText('AI 已执行')).toHaveClass('ant-tag-green');
    expect(screen.getByText('手动已执行')).toHaveClass('ant-tag-green');
    expect(screen.getByText('AI 执行失败')).toHaveClass('ant-tag-red');
    expect(screen.getByText('手动执行失败')).toHaveClass('ant-tag-red');
    expect(screen.getByText('结果待确认')).toHaveClass('ant-tag-gold');
    expect(screen.getByText('回滚失败')).toHaveClass('ant-tag-red');
    expect(screen.getByText('生成中')).toHaveClass('ant-tag-blue');
    expect(screen.getByText('生成失败')).toHaveClass('ant-tag-red');
    expect(screen.getByText('无需调整')).toHaveClass('ant-tag-green');
    expect(screen.getByText('需人工复核')).toHaveClass('ant-tag-gold');
  });
});

 describe('relevance strategy partition', () => {
  const product = (id: string) => ({ productId: id, productName: `商品${id}`, reason: '非羊绒', blacklistStatus: 'blacklisted' });
  const renderPlans = (plans: unknown[], callbacks = {}) => render(<GovernancePlans plans={plans as never} displayLimit={60} inventoryBrokenRateThreshold={0.25} onOpenProduct={() => undefined} {...callbacks} />);
  const kg = (groups: unknown[], products: unknown[] = []) => ({ ...basePlan, id: 'relevance', type: 'kg', actionType: 'relevance_plan', actionIds: [60], products, causeGroups: groups });
  const blacklist = (products: unknown[], groups: unknown[] = []) => ({ ...basePlan, id: 'blacklist', type: 'blacklist', actionType: 'product_blacklist', actionIds: [90, 91], products, causeGroups: groups, status: 'verified', statusLabel: 'AI 已执行' });

  it('uses group-only real blacklist facts for product status and action identity', () => {
    const onBlacklistProduct = vi.fn().mockResolvedValue(true);
    renderPlans([kg([{ groupId: 'kg', rootCause: 'kg_issue', productIds: ['P1'] }]), blacklist([], [{ rootCause: 'individual', product_ids: ['P1'], products: [], affected_products: [{ product_id: 'P1', productName: '已执行商品P1', status: 'blacklisted' }] }])], { onBlacklistProduct });
    expect(screen.getByText('已拉黑')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '商品 已执行商品P1' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '取消拉黑 已执行商品P1' }));
    expect(onBlacklistProduct).toHaveBeenCalledWith(expect.objectContaining({ id: 'blacklist', actionIds: [90, 91] }), expect.objectContaining({ product_id: 'P1', blacklistStatus: 'blacklisted' }), 'cancel', undefined);
  });

  it('shows all 50 products only under the final KG strategy despite stale uncertain candidates', () => {
    const products = Array.from({ length: 50 }, (_, i) => product(`P${i}`));
    renderPlans([blacklist(products, [{ root_cause: 'uncertain', products }]), kg([{ rootCause: 'kg_issue', productIds: products.map(p => p.productId), reasons: ['材质标签关系混淆'], conclusion: '调整羊绒标签关系' }], products)]);
    expect(screen.getAllByText('错误商品（50）')).toHaveLength(1);
    expect(screen.getAllByText(/^策略 \d+$/)).toHaveLength(1);
    expect(screen.queryByText('材质标签关系混淆')).not.toBeInTheDocument();
    expect(screen.queryByText('调整羊绒标签关系')).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /^商品 商品P\d+$/ }).map(element => element.getAttribute('aria-label'))).toEqual(products.map(p => `商品 ${p.productName}`));
  });

  it('partitions overlapping groups and partial candidates once with continuous strategy numbers', () => {
    const products = ['P1', 'P2', 'P3', 'P4'].map(product);
    const { container } = renderPlans([kg([
      { group_id: 'one', root_cause: 'kg_issue', products: [product('P1')], product_ids: ['P1', 'P2'], reasons: ['标签错误'] },
      { group_id: 'two', root_cause: 'individual', productIds: [], products: [product('P2'), product('P3')], conclusion: '个别商品不相关' },
    ], products.slice(0, 3)), blacklist(products, [{ rootCause: 'uncertain', products }])]);
    products.forEach(p => expect(screen.getAllByRole('button', { name: `商品 ${p.productName}` })).toHaveLength(1));
    expect(screen.getAllByText(/^策略 \d+$/).map(e => e.textContent)).toEqual(['策略 1', '策略 2', '策略 3']);
    const groups = container.querySelectorAll('.ops-governance-cause-group');
    expect(groups[0]).toHaveTextContent('商品P1');
    expect(groups[0]).toHaveTextContent('商品P2');
    expect(groups[1]).toHaveTextContent('商品P3');
    expect(groups[2]).toHaveTextContent('商品P4');
    expect(groups[2]).toHaveTextContent('需人工复核');
  });

  it('keeps standalone grouped candidates visible and explains genuine uncertain strategies', () => {
    renderPlans([blacklist([product('P1')], [{ rootCause: 'uncertain', productIds: [], products: [product('P1')], reasons: ['缺少材质标签证据'], conclusion: '需人工确认材质' }])]);
    expect(screen.getByRole('button', { name: '商品 商品P1' })).toBeInTheDocument();
    expect(screen.getByText('策略 1')).toBeInTheDocument();
    expect(screen.queryByText('缺少材质标签证据')).not.toBeInTheDocument();
    expect(screen.queryByText('需人工确认材质')).not.toBeInTheDocument();
    expect(screen.getByText('需人工复核')).toBeInTheDocument();
  });

  it('retains real blacklist status and single/batch and KG action identities', () => {
    const onBlacklistProduct = vi.fn().mockResolvedValue(true);
    const onBatchBlacklistProducts = vi.fn().mockResolvedValue(true);
    const onExecutePlan = vi.fn();
    renderPlans([kg([{ groupId: 'kg', rootCause: 'kg_issue', productIds: ['P1'], executable: true, actionIds: [60], actions: [{ actionType: 'update_tag', params: { name: '羊绒' } }] }], [{ productId: 'P1', productName: '商品P1', status: 'pending' }]), blacklist([product('P1')])], { onBlacklistProduct, onBatchBlacklistProducts, onExecutePlan });
    fireEvent.click(screen.getByRole('button', { name: '取消拉黑 商品P1' }));
    expect(onBlacklistProduct).toHaveBeenCalledWith(expect.objectContaining({ id: 'blacklist', actionIds: [90, 91] }), expect.objectContaining({ productId: 'P1', blacklistStatus: 'blacklisted' }), 'cancel', undefined);
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 商品P1' }));
    fireEvent.click(screen.getByRole('button', { name: '批量取消拉黑' }));
    expect(onBatchBlacklistProducts).toHaveBeenCalledWith(expect.objectContaining({ taskId: 1 }), [expect.objectContaining({ productId: 'P1', blacklistStatus: 'blacklisted' })], 'cancel');
    fireEvent.click(screen.getByRole('button', { name: '一键执行' }));
    expect(onExecutePlan).toHaveBeenCalledWith(expect.objectContaining({ causeGroupId: 'kg', actionIds: [60] }));
  });

  it('toggles select-all checkbox before the error-product title for the current group only', async () => {
    const onBatchBlacklistProducts = vi.fn().mockResolvedValue(true);
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'select-all-groups',
        type: 'kg',
        actionType: 'relevance_plan',
        products: [],
        causeGroups: [
          {
            groupId: 'g1',
            rootCause: 'kg_issue',
            products: [
              { productId: 'P1', productName: '商品一', status: 'blacklisted' },
              { productId: 'P2', productName: '商品二', status: 'blacklisted' },
            ],
          },
          {
            groupId: 'g2',
            rootCause: 'individual',
            products: [
              { productId: 'P3', productName: '商品三', status: 'blacklisted' },
            ],
          },
        ],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onBatchBlacklistProducts={onBatchBlacklistProducts}
    />);

    const groups = screen.getAllByTestId('governance-cause-group');
    const selectAllG1 = within(groups[0]).getByRole('checkbox', { name: '全选当前错误商品' });
    const selectAllG2 = within(groups[1]).getByRole('checkbox', { name: '全选当前错误商品' });
    const titleG1 = within(groups[0]).getByText('错误商品（2）');
    expect(selectAllG1.compareDocumentPosition(titleG1) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const checkboxP1 = screen.getByRole('checkbox', { name: '选择 商品一' });
    const checkboxP2 = screen.getByRole('checkbox', { name: '选择 商品二' });
    const checkboxP3 = screen.getByRole('checkbox', { name: '选择 商品三' });

    // Partial selection sets indeterminate on group 1
    fireEvent.click(checkboxP1);
    expect(selectAllG1).not.toBeChecked();
    expect(selectAllG1.closest('.ant-checkbox')).toHaveClass('ant-checkbox-indeterminate');

    // Clicking select-all on group 1 selects all in group 1, leaving group 2 unselected
    fireEvent.click(selectAllG1);
    expect(selectAllG1).toBeChecked();
    expect(selectAllG1.closest('.ant-checkbox')).not.toHaveClass('ant-checkbox-indeterminate');
    expect(checkboxP1).toBeChecked();
    expect(checkboxP2).toBeChecked();
    expect(checkboxP3).not.toBeChecked();
    expect(selectAllG2).not.toBeChecked();

    // Clicking select-all again deselects group 1
    fireEvent.click(selectAllG1);
    expect(selectAllG1).not.toBeChecked();
    expect(checkboxP1).not.toBeChecked();
    expect(checkboxP2).not.toBeChecked();
    expect(within(groups[0]).getByRole('button', { name: '批量取消拉黑' })).toBeDisabled();

    // Select all in group 1 and execute batch action
    fireEvent.click(selectAllG1);
    await act(async () => {
      fireEvent.click(within(groups[0]).getByRole('button', { name: '批量取消拉黑' }));
    });
    expect(onBatchBlacklistProducts).toHaveBeenCalledWith(
      expect.objectContaining({ causeGroupId: 'g1' }),
      [
        expect.objectContaining({ productId: 'P1' }),
        expect.objectContaining({ productId: 'P2' }),
      ],
      'cancel',
    );
    expect(selectAllG1).not.toBeChecked();
  });

  it('selects only products matching the active or first product status when a group has mixed statuses', () => {
    const onBatchBlacklistProducts = vi.fn().mockResolvedValue(true);
    render(<GovernancePlans
      plans={[{
        ...basePlan,
        id: 'select-all-mixed',
        type: 'blacklist',
        title: '错误商品',
        actionType: 'product_blacklist',
        products: [
          { productId: 'P1', productName: '已拉黑一', status: 'blacklisted' },
          { productId: 'P2', productName: '未拉黑一', status: 'not_blacklisted' },
          { productId: 'P3', productName: '已拉黑二', status: 'blacklisted' },
        ],
      }] as never}
      displayLimit={16}
      inventoryBrokenRateThreshold={0.25}
      onOpenProduct={() => undefined}
      onBatchBlacklistProducts={onBatchBlacklistProducts}
    />);

    const selectAll = screen.getByRole('checkbox', { name: '全选当前错误商品' });
    fireEvent.click(selectAll);
    expect(selectAll).toBeChecked();
    expect(screen.getByRole('checkbox', { name: '选择 已拉黑一' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: '选择 已拉黑二' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: '选择 未拉黑一' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: '选择 未拉黑一' })).toBeDisabled();
  });
});

it('shows the KG validation failure beside a non-executable strategy', () => {
  render(<GovernancePlans
    plans={[{ ...basePlan, id: 'invalid-kg', type: 'kg', actionType: 'relevance_plan', title: '相关性治理 Agent',
      causeGroups: [{ groupId: 'g1', rootCause: 'kg_issue', productIds: ['p1'], actions: [], validationErrors: ['动作缺少 actionType'], executable: false }],
    }] as never}
    displayLimit={16} inventoryBrokenRateThreshold={0.25} onOpenProduct={() => undefined}
  />);
  expect(screen.getByText(/动作缺少 actionType/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '一键执行' })).not.toBeInTheDocument();
});
