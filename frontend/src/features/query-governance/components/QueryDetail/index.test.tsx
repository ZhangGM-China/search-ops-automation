import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import QueryDetail from './index';

describe('QueryDetail snapshot and metric data', () => {
  it('opens governance plans first, removes governance records, and places batch execution in the tab bar', () => {
    const onExecuteTaskActions = vi.fn();
    const { container } = render(<QueryDetail detail={{
      task: { id: 10, brand: 'atlas', dataDate: '2026-09-08', queryText: '连衣裙', queryRank: 1, searchPv: 1, searchUv: 1, ctr: 0, cvr: 0, avgCtr: 0, avgCvr: 0, status: 'completed', displayStatus: 'normal', resultCount: 1 },
      actions: [],
      governancePlans: [{ id: 'kg-1', taskId: 10, type: 'kg', title: '召回数量治理策略', status: 'pending', statusLabel: '待执行', summary: '召回数量不足', actionIds: [1], executable: true, cancelable: false, executionMode: 'human_approval', steps: [], evidence: [], products: [] }],
      stepStatuses: [], trend: [],
    } as never} onExecuteTaskActions={onExecuteTaskActions} />);
    const tabs = Array.from(container.querySelectorAll('.ant-tabs-tab-btn')).map((item) => item.textContent);
    expect(tabs).toEqual(['治理方案', '效果对比', '相关性规则']);
    expect(container.querySelector('.ant-tabs-tab-active .ant-tabs-tab-btn')?.textContent).toBe('治理方案');
    expect(screen.queryByText('治理记录')).not.toBeInTheDocument();
    expect(container.querySelector('.ops-query-detail-tabs')).toHaveClass('ops-query-detail-tabs--sticky');
    const executeAll = screen.getByRole('button', { name: '一键执行' });
    expect(executeAll.closest('.ant-tabs-nav')).not.toBeNull();
    fireEvent.click(executeAll);
    expect(onExecuteTaskActions).toHaveBeenCalledWith(10);
  });
  it('uses 错误商品 as the fallback title for legacy product blacklist actions', () => {
    render(<QueryDetail detail={{
      task: { id: 11, brand: 'atlas', dataDate: '2026-09-08', queryText: '外套', queryRank: 1, searchPv: 1, searchUv: 1, ctr: 0, cvr: 0, avgCtr: 0, avgCvr: 0, status: 'completed', displayStatus: 'normal', resultCount: 1 },
      actions: [{ id: 31, taskId: 11, checkKey: 'relevance', toolKey: 'blacklist', actionType: 'product_blacklist', executionMode: 'human_approval', status: 'pending' }],
      stepStatuses: [], trend: [],
    } as never} />);

    expect(screen.getByText('错误商品（0）')).toBeInTheDocument();
    expect(screen.queryByText(/拉黑商品/)).not.toBeInTheDocument();
  });

  it('routes an executable KG plan through the dedicated governed endpoint handler', () => {
    const onExecuteTaskActions = vi.fn();
    const onExecuteKGPlan = vi.fn();
    const plan = { id: 'kg-12', taskId: 12, actionId: 41, actionIds: [41], type: 'kg', actionType: 'kg_plan', title: '召回数量治理策略', objective: '', summary: '建立牛仔裤从属于牛仔', status: 'pending', statusLabel: '待执行', executionMode: 'human_approval', executable: true, cancelable: false, steps: [{ index: 1, title: '新增从属关系', description: '牛仔裤从属于牛仔' }], evidence: [] };
    render(<QueryDetail detail={{
      task: { id: 12, brand: 'atlas', dataDate: '2026-09-08', queryText: '牛仔', queryRank: 1, searchPv: 1, searchUv: 1, ctr: 0, cvr: 0, avgCtr: 0, avgCvr: 0, status: 'completed', displayStatus: 'normal', resultCount: 0 },
      actions: [], governancePlans: [plan], stepStatuses: [], trend: [],
    } as never} onExecuteTaskActions={onExecuteTaskActions} onExecuteKGPlan={onExecuteKGPlan} />);

    fireEvent.click(screen.getAllByRole('button', { name: /执\s*行/ })[1]);
    expect(onExecuteKGPlan).toHaveBeenCalledWith(expect.objectContaining({ actionId: 41 }));
    expect(onExecuteTaskActions).not.toHaveBeenCalled();
  });
  it('renders the persisted S0 result as the governance-before snapshot', () => {
    render(
      <QueryDetail
        detail={{
          task: {
            id: 1,
            brand: 'atlas',
            dataDate: '2026-09-05',
            queryText: '帽子 女士',
            queryRank: 1,
            searchPv: 114,
            searchUv: 81,
            ctr: 0.42,
            cvr: 0,
            avgCtr: 0.5,
            avgCvr: 0.01,
            status: 'completed',
            displayStatus: 'normal',
            resultCount: 2,
          },
          actions: [],
          stepStatuses: [],
          trend: [],
          s0: {
            resultCount: 2,
            products: [{ productId: 'p1', productName: '治理前商品' }],
          },
        } as never}
      />,
    );

    fireEvent.click(screen.getByRole('tab', { name: '效果对比' }));
    expect(screen.getAllByText(/治理前商品快照/)[0]).toHaveTextContent('2');
    expect(screen.getByText('治理前商品')).toBeInTheDocument();
  });

  it('renders governance plans inside the tab instead of a duplicate hero module', () => {
    render(
      <QueryDetail
        detail={{
          task: {
            id: 2,
            brand: 'atlas',
            dataDate: '2026-09-08',
            queryText: '牛仔 女士',
            queryRank: 1,
            searchPv: 152,
            searchUv: 81,
            ctr: 0.3182,
            cvr: 0,
            avgCtr: 0.328,
            avgCvr: 0,
            status: 'completed',
            displayStatus: 'normal',
            resultCount: 2,
          },
          actions: [],
          governancePlans: [{ id: 'plan-1', taskId: 2, type: 'reorder', title: 'AI 重排序策略', summary: '调整排序策略', status: 'verified', statusLabel: 'AI 已执行', executorType: 'ai_auto', actionIds: [2], steps: [], evidence: [], executable: false, cancelable: false, executionMode: 'auto', products: [] }] as never,
          stepStatuses: [],
          trend: [{ date: '2026-09-08', pv: 152, uv: 81, ctr: 0.3182, cvr: 0, atc: 0 }],
        } as never}
      />,
    );

    expect(screen.getByRole('tab', { name: '治理方案' })).toBeInTheDocument();
    expect(screen.queryByText('诊断建议')).not.toBeInTheDocument();
    expect(screen.queryByText('诊断结论')).not.toBeInTheDocument();
    expect(screen.getByText('AI 重排序策略')).toBeInTheDocument();
    expect(screen.getByText('调整排序策略')).toBeInTheDocument();
    expect(screen.getByText('UV')).toBeInTheDocument();
    expect(screen.getByText('ATC')).toBeInTheDocument();
  });

  it('shows recent changes for CVR and ATC and keeps UV next to PV', () => {
    const { container } = render(
      <QueryDetail
        detail={{
          task: {
            id: 3,
            brand: 'atlas',
            dataDate: '2026-09-08',
            queryText: '牛仔 女士',
            queryRank: 1,
            searchPv: 152,
            searchUv: 81,
            ctr: 0.3182,
            cvr: 0,
            avgCtr: 0.328,
            avgCvr: 0,
            status: 'completed',
            displayStatus: 'normal',
            resultCount: 2,
          },
          actions: [],
          stepStatuses: [],
          trend: [
            { date: '2026-09-06', pv: 100, uv: 50, clickUv: 10, addToCartUv: 1, purchaseUv: 1, ctr: 0.2, cvr: 0.01, atc: 0.02 },
            { date: '2026-09-07', pv: 120, uv: 60, clickUv: 15, addToCartUv: 2, purchaseUv: 1, ctr: 0.25, cvr: 0.02, atc: 0.03 },
            { date: '2026-09-08', pv: 152, uv: 81, ctr: 0.3182, cvr: 0, atc: 0 },
        ]
        } as never}
      />,
    );

    const items = Array.from(container.querySelectorAll('.ops-query-metric-row__item'));
    expect(items).toHaveLength(5);
    expect(items.map((item) => item.querySelector('small')?.textContent)).toEqual(['PV', 'UV', 'CTR', 'CVR', 'ATC']);
    expect(screen.getAllByText(/较近期/).length).toBeGreaterThanOrEqual(4);
    expect(screen.getAllByText(/较近期 -100\.00%/).length).toBeGreaterThanOrEqual(2);
  });

  it('uses weighted recent ratios from raw numerators instead of averaging daily rates', () => {
    render(
      <QueryDetail
        detail={{
          task: {
            id: 4, brand: 'atlas', dataDate: '2026-09-08', queryText: '连衣裙',
            searchPv: 200, searchUv: 100, ctr: 0.2, cvr: 0.1,
            avgCtr: 0, avgCvr: 0, status: 'completed', displayStatus: 'normal', resultCount: 1,
          },
          actions: [], stepStatuses: [],
          trend: [
            { date: '2026-09-06', pv: 100, uv: 10, clickUv: 9, addToCartUv: 5, purchaseUv: 2, ctr: 0.9, atc: 0.5, cvr: 0.2 },
            { date: '2026-09-07', pv: 100, uv: 90, clickUv: 9, addToCartUv: 5, purchaseUv: 3, ctr: 0.1, atc: 5 / 90, cvr: 3 / 90 },
            { date: '2026-09-08', pv: 200, uv: 100, clickUv: 20, addToCartUv: 10, purchaseUv: 10, ctr: 0.2, atc: 0.1, cvr: 0.1 },
          ],
        } as never}
      />,
    );

    expect(screen.getByText('较近期 +11.11%')).toBeInTheDocument();
    expect(screen.getAllByText('较近期 +100.00%').length).toBeGreaterThanOrEqual(2);
  });

  it('uses only the configured N days before the candidate date as the recent baseline', () => {
    render(
      <QueryDetail
        detail={{
          historyDays: 1,
          task: {
            id: 5, brand: 'atlas', dataDate: '2026-09-08', queryText: '连衣裙',
            searchPv: 200, searchUv: 100, ctr: 0.2, cvr: 0.1,
            avgCtr: 0, avgCvr: 0, status: 'completed', displayStatus: 'normal', resultCount: 1,
          },
          actions: [], stepStatuses: [],
          trend: [
            { date: '2026-09-06', pv: 80, uv: 100, clickUv: 5, purchaseUv: 5, ctr: 0.05, cvr: 0.05 },
            { date: '2026-09-07', pv: 100, uv: 100, clickUv: 10, purchaseUv: 5, ctr: 0.1, cvr: 0.05 },
            { date: '2026-09-08', pv: 200, uv: 100, clickUv: 20, purchaseUv: 10, ctr: 0.2, cvr: 0.1 },
            { date: '2026-09-09', pv: 300, uv: 100, clickUv: 90, purchaseUv: 50, ctr: 0.9, cvr: 0.5 },
          ],
        } as never}
      />,
    );

    expect(screen.getAllByText('较近期 +100.00%').length).toBeGreaterThanOrEqual(2);
  });
});
