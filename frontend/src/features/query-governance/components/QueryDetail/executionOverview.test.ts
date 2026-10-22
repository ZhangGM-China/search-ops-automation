import { describe, expect, it } from 'vitest';
import { checkStateLabel, checkStateTone } from '../../lib/queryGovernanceStatus';
import { buildExecutionOverview } from './executionOverviewModel';

describe('buildExecutionOverview', () => {
  it('omits checks hidden by the server for this Query', () => {
    const nodes = buildExecutionOverview({
      steps: [
        { checkKey: 'recall', status: 'executed', displayStatus: 'executed' },
        { checkKey: 'inventory', status: 'skipped', displayStatus: 'skipped' },
      ],
      visibleCheckKeys: ['recall'],
    });
    expect(nodes.map((node) => node.key)).toEqual(['recall']);
  });
  it('normalizes node states and keeps the four-node order', () => {
    const nodes = buildExecutionOverview({
      steps: [
        { checkKey: 'relevance', status: 'pass', displayStatus: 'normal', summary: '检测完成', evidence: { mismatch_count: 3, checked_count: 16 } },
        { checkKey: 'open_factor', status: 'running', displayStatus: 'running', summary: '' },
        { checkKey: 'recall', status: 'skipped', displayStatus: 'skipped', summary: '无需检查' },
        { checkKey: 'inventory', status: 'error', displayStatus: 'error', summary: '库存接口超时' },
      ],
      events: [],
      actions: [],
    });

    expect(nodes.map((node) => node.key)).toEqual(['recall', 'open_factor', 'relevance', 'inventory']);
    expect(nodes.map((node) => checkStateLabel(node.state))).toEqual(['未执行', '待处理', '需关注', '异常']);
    expect(nodes.map((node) => node.state)).toEqual(['not_run', 'pending', 'attention', 'failed']);
    expect(nodes.map((node) => checkStateTone(node.state))).toEqual(['default', 'yellow', 'blue', 'red']);
    expect(nodes[3].summary).toContain('库存接口超时');
  });

  it('does not label a missing inventory execution fact as executed', () => {
    const nodes = buildExecutionOverview({
      steps: [],
      inventory: [],
      events: [],
      actions: [],
    });

    expect(checkStateLabel(nodes[3].state)).toBe('未执行');
    expect(checkStateTone(nodes[3].state)).toBe('default');
    expect(nodes[3].summary).toBe('暂无巡检记录');
  });

  it('shows a completed inventory scan with zero broken products', () => {
    const nodes = buildExecutionOverview({
      steps: [{
        checkKey: 'inventory', status: 'executed', displayStatus: 'executed',
        summary: '已巡检，未发现断码商品',
        evidence: { issueTypes: [], checkedAt: '2026-09-29T06:05:00' },
      }],
      inventory: [],
    });

    expect(checkStateLabel(nodes[3].state)).toBe('正常');
    expect(checkStateTone(nodes[3].state)).toBe('green');
    expect(nodes[3].summary).toContain('未发现断码商品');
    expect(nodes[3].details).toContainEqual({ label: '断码商品数量', value: '0 件' });
  });

  it('shows a failed independent inventory action instead of treating it as not executed', () => {
    const nodes = buildExecutionOverview({
      steps: [],
      inventory: [],
      inventoryExecution: {
        available: false,
        source: 'independent_job',
        status: 'failed',
        errorCode: 'INVENTORY_ACTION_FAILED',
        errorMessage: '断码动作执行失败，请查看动作事实',
      },
    });

    expect(checkStateLabel(nodes[3].state)).toBe('异常');
    expect(checkStateTone(nodes[3].state)).toBe('red');
    expect(nodes[3].summary).toBe('执行失败：断码动作执行失败，请查看动作事实');
    expect(nodes[3].details).toContainEqual({
      label: '错误原因',
      value: 'INVENTORY_ACTION_FAILED：断码动作执行失败，请查看动作事实',
    });
  });

  it('shows the preference strategy and promoted products for reorder', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'executed', displayStatus: 'executed', summary: '' }],
      events: [{
        id: 1, agentName: 'PreferenceAgent', stage: 'reorder', eventType: 'executor_completed', summary: '',
        payload: { preference_description: '优先展示轻薄、通勤款式' }, createdAt: '2026-09-08T01:02:03Z',
      }],
      actions: [{
        id: 2, taskId: 1, checkKey: 'open_factor', toolKey: 'query_reorder', actionType: 'query_reorder',
        executionMode: 'auto', status: 'executed',
        requestPayload: { preference_description: '优先展示轻薄、通勤款式', products: [{ productId: 'p1', productName: '轻薄针织衫', oldRank: 8, newRank: 2 }] },
      }],
    });

    expect(nodes[1].summary).toBe('优先展示轻薄、通勤款式');
    expect(nodes[1].details.some((entry) => entry.label === '调整商品' && entry.value.includes('轻薄针织衫'))).toBe(true);
    expect(nodes[1].details.find((entry) => entry.label === '调整商品')?.rawValue).toEqual([
      { productId: 'p1', productName: '轻薄针织衫', oldRank: 8, newRank: 2 },
    ]);
  });

  it('keeps an executed check green when it has no issue evidence', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'recall', status: 'executed', displayStatus: 'executed', summary: '', evidence: { issueTypes: [] } }],
    });

    expect(checkStateLabel(nodes[0].state)).toBe('正常');
    expect(checkStateTone(nodes[0].state)).toBe('green');
  });

  it('marks an executed check blue when its evidence requires improvement', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'inventory', status: 'executed', displayStatus: 'executed', summary: '检测到断码商品', evidence: { issueTypes: ['inventory'] } }],
    });

    expect(checkStateTone(nodes[3].state)).toBe('blue');
  });

  it('marks the overview abnormal when downstream governance generation fails', () => {
    const nodes = buildExecutionOverview({
      steps: [{
        checkKey: 'recall', status: 'executed', displayStatus: 'executed',
        evidence: { issueTypes: ['result_count_drop'] },
        governanceStatus: {
          status: 'failed', label: '生成失败',
          errorCode: 'KG_AGENT_INVALID_JSON', errorMessage: 'agent response must be JSON',
        },
      }],
      recall: { currentResultCount: 4, previousResultCount: 20, comparisonStatus: 'drop', conclusion: '结果骤降' },
    });

    expect(checkStateLabel(nodes[0].state)).toBe('异常');
    expect(checkStateTone(nodes[0].state)).toBe('red');
    expect(nodes[0].summary).toContain('治理方案生成失败：agent response must be JSON');
    expect(nodes[0].details).toContainEqual({ label: '治理方案状态', value: '生成失败' });
  });

  it('shows structured reorder evidence with alert thresholds and execution output', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'executed', displayStatus: 'executed', summary: '' }],
      reorder: {
        currentCtr: 0.062,
        recentCtr: 0.124,
        ctrChange: -6.2,
        ctrAlertThreshold: 5,
        currentCvr: 0.011,
        recentCvr: 0.038,
        cvrChange: -2.7,
        cvrAlertThreshold: 2,
        recallCount: 24,
        lowRecallThreshold: 4,
        conclusion: '当前 CTR、CVR 较近期分别下降 6.2、2.7 个百分点，均已达到告警阈值，触发重排序。',
        requiresReorder: true,
      },
      nodes: [{
        nodeKey: 'reorder', status: 'executed', result: {
          result: {
            preference_description: '优先展示轻薄、通勤款式',
            requested_page_size: 16,
            products: [{ productId: 'p1', productName: '轻薄针织衫' }],
          },
        },
      }],
      actions: [{
        id: 2, taskId: 1, checkKey: 'open_factor', toolKey: 'query_reorder', actionType: 'query_reorder',
        executionMode: 'auto', status: 'verified', response: { verification: { status: 'success' } },
      }],
    });

    expect(nodes[1].details).toEqual(expect.arrayContaining([
      { label: '当前 CTR', value: '6.20%' },
      { label: '近期 CTR', value: '12.40%' },
      { label: 'CTR 变化', value: '下降 6.20%' },
      { label: 'CTR 告警阈值', value: '下降 ≥ 5 %' },
      { label: '当前 CVR', value: '1.10%' },
      { label: '近期 CVR', value: '3.80%' },
      { label: 'CVR 变化', value: '下降 2.70%' },
      { label: 'CVR 告警阈值', value: '下降 ≥ 2 %' },
      { label: '当前召回商品数', value: '24 件' },
      { label: '低召回豁免阈值', value: '≤ 4 件' },
      { label: '结论', value: '当前 CTR、CVR 较近期分别下降 6.2、2.7 个百分点，均已达到告警阈值，触发重排序。AI 重排序策略已生成，重排序执行成功。' },
      { label: 'AI 重排序策略', value: '优先展示轻薄、通勤款式' },
      { label: '向量重排序商品（前 16）', value: '共 1 件商品' },
      { label: '重排序执行结果', value: '成功' },
    ]));
    expect(nodes[1].details.find((entry) => entry.label === '向量重排序商品（前 16）')?.rawValue).toEqual([
      { productId: 'p1', productName: '轻薄针织衫' },
    ]);
    expect(nodes[1].details.some((entry) => entry.label === '执行状态')).toBe(false);
  });

  it('shows partial position matches as a successful reorder verification', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'executed', displayStatus: 'executed', summary: '' }],
      reorder: { recallCount: 24, lowRecallThreshold: 4, requiresReorder: true },
      actions: [{
        id: 7, taskId: 1, checkKey: 'open_factor', toolKey: 'query_reorder', actionType: 'query_reorder',
        executionMode: 'auto', status: 'verified', responsePayload: {
          verification: {
            status: 'success',
            verification: { matched: true, partial: true, matched_count: 11, expected_count: 12 },
          },
        },
      }],
    });

    expect(nodes[1].details).toContainEqual({ label: '排序回验命中', value: '11/12 个目标坑位（部分生效）' });
    expect(nodes[1].details).toContainEqual({ label: '重排序执行结果', value: '成功' });
  });

  it('shows an unreadable Mixed result as pending confirmation', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'error', displayStatus: 'error', summary: '' }],
      reorder: { recallCount: 24, lowRecallThreshold: 4, requiresReorder: true, executionStatus: 'inconclusive' },
      actions: [{
        id: 8, taskId: 1, checkKey: 'open_factor', toolKey: 'query_reorder', actionType: 'query_reorder',
        executionMode: 'auto', status: 'execution_unknown', responsePayload: {
          verification: { status: 'inconclusive', reason: 'readback_unavailable' },
        },
      }],
    });

    expect(nodes[1].details).toContainEqual({ label: '重排序执行结果', value: '结果待确认' });
    expect(nodes[1].details).toContainEqual({ label: '错误原因', value: '排序结果暂时无法确认，请稍后检查搜索结果。' });
  });

  it('appends independent strategy and execution outcomes to a reorder conclusion', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'failed', displayStatus: 'failed', summary: '' }],
      reorder: {
        currentCtr: 0.04, recentCtr: 0.1, ctrChange: -6, ctrAlertThreshold: 5,
        currentCvr: 0.02, recentCvr: 0.02, cvrChange: 0, cvrAlertThreshold: 2,
        recallCount: 24, lowRecallThreshold: 4,
        conclusion: '当前 CTR 较近期下降 6 个百分点，已达到告警阈值，触发重排序。',
        requiresReorder: true,
      },
      nodes: [{
        nodeKey: 'reorder', status: 'error', result: { result: { preference_description: '优先展示轻薄款' } },
      }],
      actions: [{
        id: 9, taskId: 1, checkKey: 'open_factor', toolKey: 'query_reorder', actionType: 'query_reorder',
        executionMode: 'auto', status: 'verification_failed',
      }],
    });

    expect(nodes[1].summary).toBe('当前 CTR 较近期下降 6 个百分点，已达到告警阈值，触发重排序。AI 重排序策略已生成，但重排序执行失败。');
    expect(nodes[1].details).toContainEqual({
      label: '结论',
      value: '当前 CTR 较近期下降 6 个百分点，已达到告警阈值，触发重排序。AI 重排序策略已生成，但重排序执行失败。',
    });
  });

  it('reports strategy generation failure and skips execution in the conclusion', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'error', displayStatus: 'error', summary: '' }],
      reorder: {
        recallCount: 24,
        lowRecallThreshold: 4,
        conclusion: '当前 CVR 较近期下降 2 个百分点，已达到告警阈值，触发重排序。',
        requiresReorder: true,
      },
      actions: [],
    });

    expect(nodes[1].summary).toBe('当前 CVR 较近期下降 2 个百分点，已达到告警阈值，触发重排序。AI 重排序策略生成失败，重排序未执行。');
  });

  it('reports both failures when an action exists without a generated strategy', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'error', displayStatus: 'error', summary: '' }],
      reorder: {
        recallCount: 24,
        lowRecallThreshold: 4,
        conclusion: '当前 CTR 较近期下降 6 个百分点，已达到告警阈值，触发重排序。',
        requiresReorder: true,
        strategyStatus: 'failed',
        executionStatus: 'failed',
      },
      actions: [{
        id: 10, taskId: 1, checkKey: 'open_factor', toolKey: 'query_reorder', actionType: 'query_reorder',
        executionMode: 'auto', status: 'error',
      }],
    });

    expect(nodes[1].summary).toBe('当前 CTR 较近期下降 6 个百分点，已达到告警阈值，触发重排序。AI 重排序策略生成失败，重排序执行失败。');
  });

  it('keeps no-reorder conclusions unchanged', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'skipped', displayStatus: 'skipped', summary: '' }],
      reorder: {
        recallCount: 24,
        lowRecallThreshold: 4,
        conclusion: 'CTR、CVR 均在正常范围，无需进行重排序。',
        requiresReorder: false,
      },
      actions: [],
    });

    expect(nodes[1].summary).toBe('CTR、CVR 均在正常范围，无需进行重排序。');
    expect(nodes[1].details).toContainEqual({ label: '结论', value: 'CTR、CVR 均在正常范围，无需进行重排序。' });
  });

  it('uses the recall conclusion for low-result skips without a separate reason field', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'skipped', displayStatus: 'skipped', summary: 'S0 召回商品数≤4' }],
      reorder: {
        currentCtr: 0.1, recentCtr: 0.1, ctrChange: 0, ctrAlertThreshold: 5,
        currentCvr: 0.02, recentCvr: 0.02, cvrChange: 0, cvrAlertThreshold: 2,
        recallCount: 4, lowRecallThreshold: 4,
        conclusion: '当前召回商品数为 4 件，候选集过小，无需进行告警。',
        requiresReorder: false,
      },
      actions: [],
    });

    expect(nodes[1].details).toEqual(expect.arrayContaining([
      { label: '当前召回商品数', value: '4 件' },
      { label: '结论', value: '当前召回商品数为 4 件，候选集过小，无需进行告警。' },
    ]));
    expect(nodes[1].details.some((entry) => entry.label === '跳过原因')).toBe(false);
  });

  it('turns reorder failures into human-readable reasons and a simple result', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'failed', displayStatus: 'failed', summary: '' }],
      reorder: { recallCount: 12, lowRecallThreshold: 4, conclusion: '需要进行重排序。' },
      actions: [{
        id: 2, taskId: 1, checkKey: 'open_factor', toolKey: 'query_reorder', actionType: 'query_reorder',
        executionMode: 'auto', status: 'verification_failed',
        responsePayload: { gateway: { status: 'failed', reason: 'verification_failed' } },
      }],
    });

    expect(nodes[1].details).toEqual(expect.arrayContaining([
      { label: '重排序执行结果', value: '失败' },
      { label: '错误原因', value: '排序结果与预期不一致，系统已撤销本次排序。' },
    ]));
    expect(nodes[1].details.some((entry) => entry.value.includes('verification_failed'))).toBe(false);
  });

  it('does not leak non-terminal action status into the execution result', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'executing', displayStatus: 'executing', summary: '' }],
      reorder: { recallCount: 12, lowRecallThreshold: 4, conclusion: '需要进行重排序。' },
      actions: [{
        id: 3, taskId: 1, checkKey: 'open_factor', toolKey: 'query_reorder', actionType: 'query_reorder',
        executionMode: 'auto', status: 'pending',
      }],
    });

    expect(nodes[1].details).toEqual(expect.arrayContaining([
      { label: '重排序执行结果', value: '失败' },
    ]));
    expect(nodes[1].details.some((entry) => entry.value === 'pending')).toBe(false);
  });

  it('treats a rolled-back reorder as a failed execution', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'open_factor', status: 'failed', displayStatus: 'failed', summary: '' }],
      reorder: { recallCount: 12, lowRecallThreshold: 4, conclusion: '需要进行重排序。' },
      actions: [{
        id: 4, taskId: 1, checkKey: 'open_factor', toolKey: 'query_reorder', actionType: 'query_reorder',
        executionMode: 'auto', status: 'rolled_back',
      }],
    });

    expect(nodes[1].details).toEqual(expect.arrayContaining([
      { label: '重排序执行结果', value: '失败' },
    ]));
  });

  it('highlights irrelevant count, inventory latest time, and failure reason', () => {
    const nodes = buildExecutionOverview({
      steps: [
        { checkKey: 'relevance', status: 'executed', displayStatus: 'executed', summary: '', evidence: { mismatch_count: 3, checked_count: 16 } },
        { checkKey: 'inventory', status: 'executed', displayStatus: 'executed', summary: '', evidence: { checked_at: '2026-09-08T03:04:05Z', added_count: 1 } },
        { checkKey: 'recall', status: 'failed', displayStatus: 'failed', summary: 'Mixed 服务不可用', evidence: { error_code: 'MIXED_TIMEOUT' } },
      ], events: [], actions: [],
    });

    expect(nodes[2].summary).toBe('3 件不相关');
    // checked_at carries an explicit UTC marker and is displayed in Beijing time.
    expect(nodes[3].summary).toContain('2026-09-08 11:04');
    expect(nodes[0].summary).toContain('Mixed 服务不可用');
    expect(nodes[0].details.some((entry) => entry.label === '错误原因' && entry.value.includes('MIXED_TIMEOUT'))).toBe(true);
  });

  it('uses persisted inventory records when the step has no checked_at evidence', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'inventory', status: 'executed', displayStatus: 'executed', summary: '检测到断码商品' }],
      inventory: [{
        id: 14322,
        productId: 'C04764201704',
        productName: '皮革效果迷你连衣裙',
        query: '连衣裙 女士',
        lastBrokenRate: 83,
        status: 'auto_stopped',
        createdAt: '2026-09-18T12:19:52',
        updatedAt: '2026-09-19T19:03:34',
        checkedAt: '2026-09-21T10:00:00',
      }],
      events: [],
      actions: [],
    });

    expect(nodes[3].summary).toBe('巡检 2026-09-21 10:00；检测到断码商品');
    expect(nodes[3].details).toEqual(expect.arrayContaining([
      { label: '最近巡检', value: '2026-09-21 10:00' },
      { label: '断码商品数量', value: '1 件' },
      { label: '断码商品明细', value: '共 1 件商品' },
    ]));
    expect(nodes[3].details.find((entry) => entry.label === '断码商品明细')?.rawValue).toEqual([{
      productId: 'C04764201704',
      productName: '皮革效果迷你连衣裙',
      broken_size_rate: 0.83,
      inventoryStatus: '已停止',
      inventoryCheckedAt: '2026-09-21T10:00:00',
    }]);
  });

  it('shows recovered count, configured interval, and rate transition in inventory details', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'inventory', status: 'executed', displayStatus: 'executed' }],
      inventory: [
        {
          id: 1, productId: 'active', productName: '仍断码', query: '连衣裙', lastBrokenRate: 83,
          status: 'monitoring', checkedAt: '2026-09-21T11:00:00', imageUrl: 'https://img/active.jpg',
        },
        {
          id: 2, productId: 'recovered', productName: '已恢复', query: '连衣裙', lastBrokenRate: 13,
          status: 'recovered', checkedAt: '2026-09-21T12:00:00', recoveredAt: '2026-09-21T12:00:00',
          brokenRateBefore: 83, imageUrl: 'https://img/recovered.jpg',
        },
      ],
      inventoryCheckIntervalMinutes: 60,
      recoveredTodayCount: 1,
    });

    expect(nodes[3].summary).toBe('巡检 2026-09-21 12:00；检测到断码商品');
    expect(nodes[3].details).toEqual(expect.arrayContaining([
      { label: '断码商品数量', value: '1 件' },
      { label: '今日恢复商品', value: '1 件' },
      { label: '监测间隔', value: '每 60 分钟' },
    ]));
    expect(nodes[3].details.find((entry) => entry.label === '断码商品明细')?.rawValue).toEqual(expect.arrayContaining([
      expect.objectContaining({ productId: 'recovered', inventoryStatus: '已恢复', brokenRateTransition: '83% → 13%', imageUrl: 'https://img/recovered.jpg' }),
    ]));
  });

  it('renders structured relevance counts and accuracy from the current-round set', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'relevance', status: 'executed', displayStatus: 'executed' }],
      relevance: {
        query: '香水 女士',
        productCount: 23,
        requestedProductIds: ['reused-1', 'new-1', 'new-2'],
        reusedProductIds: ['reused-1'],
        detectedProductIds: ['new-1', 'new-2'],
        relevantProductIds: ['reused-1', 'new-1'],
        blacklistCandidates: [],
        warnings: [],
        detectedProductCount: 2,
      },
      events: [], actions: [],
    });

    expect(nodes[2].summary).toBe('检测 2 件：相关 1 件，本轮准确率 50.00%');
    expect(nodes[2].details).toEqual(expect.arrayContaining([
      { label: 'AI 检测', value: '2 件' },
      { label: '历史复用', value: '1 件' },
      { label: '正确商品', value: '1 件' },
      { label: '错误商品', value: '0 件' },
      { label: '正确商品', value: '共 1 件商品' },
      { label: '错误商品', value: '暂无错误商品' },
      { label: '准确率', value: '50.00%' },
      { label: '耗时', value: '未记录' },
      { label: 'Token', value: '未记录' },
      { label: '结论', value: '检测 2 件：相关 1 件，本轮准确率 50.00%' },
    ]));
  });

  it('does not fabricate accuracy when there are no newly detected products', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'relevance', status: 'executed', displayStatus: 'executed' }],
      relevance: {
        query: '裙子', productCount: 2, requestedProductIds: ['a', 'b'], reusedProductIds: ['a', 'b'],
        detectedProductIds: [], relevantProductIds: ['a', 'b'], blacklistCandidates: [], warnings: [], detectedProductCount: 0,
      },
      events: [], actions: [],
    });

    expect(nodes[2].summary).toBe('检测 0 件：相关 0 件，暂无本轮准确率');
    expect(nodes[2].details).toContainEqual({ label: '准确率', value: '暂无准确率' });
  });

  it('lists blacklist candidates and warnings in relevance details', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'relevance', status: 'executed', displayStatus: 'executed' }],
      relevance: {
        query: '白色', productCount: 2, requestedProductIds: ['a', 'b'], reusedProductIds: [], detectedProductIds: ['a', 'b'],
        relevantProductIds: ['a'],
        blacklistCandidates: [{ productId: 'b', reason: '图片与 Query 不符', detectionTypes: ['image'] }],
        warnings: [{ code: 'IMAGE_FAILED', message: '图片不可用' }], detectedProductCount: 2,
      },
      events: [], actions: [],
    });

    expect(nodes[2].details).toContainEqual({ label: '错误商品', value: '共 1 件商品' });
    expect(nodes[2].details.find((entry) => entry.label === '错误商品' && entry.rawValue != null)?.rawValue).toEqual([
      { productId: 'b', productName: 'b', failureReason: '图片与 Query 不符' },
    ]);
    expect(nodes[2].details).toContainEqual({ label: '检测警告', value: 'IMAGE_FAILED：图片不可用' });
  });

  it('adds relevance remediation and verification details without replacing detection metrics', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'relevance', status: 'executed', displayStatus: 'executed' }],
      relevance: {
        query: '轻薄西装', productCount: 10, requestedProductIds: ['a'], reusedProductIds: [],
        detectedProductIds: ['a'], relevantProductIds: [], blacklistCandidates: [{ productId: 'a' }], warnings: [], detectedProductCount: 1,
        remediation: {
          blacklistStatus: '已拉黑 10 件',
          rootCauseGroupCount: 3,
          kgStrategyStatus: '2 组已执行',
          retagStatus: '已统一触发',
          verifiedAt: '2026-09-20T10:30:00+08:00',
          incorrectProductsDisappeared: true,
          correctProductsAffected: false,
          beforeRecallCount: 120,
          afterRecallCount: 112,
          finalConclusion: '验证通过',
        },
      },
      events: [], actions: [],
    });

    expect(nodes[2].details).toEqual(expect.arrayContaining([
      { label: '拉黑状态', value: '已拉黑 10 件' },
      { label: '根因分组', value: '3 组' },
      { label: 'KG 策略', value: '2 组已执行' },
      { label: '重新打标签', value: '已统一触发' },
      { label: '验证时间', value: '2026-09-20 10:30' },
      { label: '错误商品是否消失', value: '是' },
      { label: '正确商品是否误伤', value: '否' },
      { label: '验证前后召回数量', value: '120 件 → 112 件' },
      { label: '最终结论', value: '验证通过' },
      { label: '错误商品', value: '1 件' },
    ]));
  });

  it('omits relevance remediation details when the backend has not provided them', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'relevance', status: 'executed', displayStatus: 'executed' }],
      relevance: {
        query: '裙子', productCount: 0, requestedProductIds: [], reusedProductIds: [], detectedProductIds: [],
        relevantProductIds: [], blacklistCandidates: [], warnings: [], detectedProductCount: 0,
      },
      events: [], actions: [],
    });

    expect(nodes[2].details.some((entry) => ['拉黑状态', '根因分组', 'KG 策略', '重新打标签', '最终结论'].includes(entry.label))).toBe(false);
  });

  it('builds correct and incorrect product cards from the current-round relevance result', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'relevance', status: 'executed', displayStatus: 'executed' }],
      s0: {
        resultCount: 3,
        products: [
          { productId: 'new-1', productName: '白色衬衫', imageUrl: 'https://img.test/new-1.jpg' },
          { productId: 'new-2', productName: '黑色外套', imageUrl: 'https://img.test/new-2.jpg' },
          { productId: 'reused-1', productName: '历史复用商品' },
        ],
      },
      relevance: {
        query: '白色', productCount: 3,
        requestedProductIds: ['reused-1', 'new-1', 'new-2'],
        reusedProductIds: ['reused-1'], detectedProductIds: ['new-1', 'new-2'],
        relevantProductIds: ['reused-1', 'new-1'],
        blacklistCandidates: [{ productId: 'new-2', reason: '图片不可用，无法确认商品是否为白色' }],
        warnings: [], detectedProductCount: 2,
      },
      events: [], actions: [],
    });

    expect(nodes[2].details).toEqual(expect.arrayContaining([
      { label: '正确商品', value: '1 件' },
      { label: '错误商品', value: '1 件' },
      { label: '结论', value: '检测 2 件：相关 1 件，本轮准确率 50.00%' },
    ]));
    expect(nodes[2].details.some((entry) => entry.label === '相关商品')).toBe(false);
    expect(nodes[2].details.some((entry) => entry.label === '本轮新检')).toBe(false);
    expect(nodes[2].details.find((entry) => entry.label === '正确商品' && entry.rawValue != null)?.rawValue).toEqual([
      { productId: 'new-1', productName: '白色衬衫', imageUrl: 'https://img.test/new-1.jpg' },
    ]);
    expect(nodes[2].details.find((entry) => entry.label === '错误商品' && entry.rawValue != null)?.rawValue).toEqual([
      { productId: 'new-2', productName: '黑色外套', imageUrl: 'https://img.test/new-2.jpg', failureReason: '图片不可用，无法确认商品是否为白色' },
    ]);
  });

  it('shows persisted current and historical result counts from deterministic node output', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'recall', status: 'executed', displayStatus: 'executed' }],
      nodes: [{
        nodeKey: 'recall',
        status: 'executed',
        result: {
          result: {
            summary: '召回8件，数量充足',
            evidence: {
              result_count: 8,
              previous_result_count: 7,
            },
          },
        },
      }],
      s0: { resultCount: 8 },
      events: [],
      actions: [],
    });

    expect(nodes[0].summary).toBe('查询到 8 件商品，较上一次 7 件；结论：正常');
    expect(nodes[0].details).toEqual(expect.arrayContaining([
      { label: '当前召回数量', value: '8 件' },
      { label: '上一次召回数量', value: '7 件' },
      { label: '结论', value: '正常' },
    ]));
  });

  it('uses the immediately previous result as the recall comparison baseline', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'recall', status: 'executed', displayStatus: 'executed' }],
      nodes: [{
        nodeKey: 'recall', status: 'executed', result: {
          result: { conclusion: '结果骤降', evidence: { result_count: 8, previous_result_count: 20, comparison_baseline: 'previous_result' } },
        },
      }],
      s0: { resultCount: 8 },
      events: [], actions: [],
    });

    expect(nodes[0].summary).toBe('查询到 8 件商品，较上一次 20 件；结论：结果骤降');
    expect(nodes[0].details).toEqual(expect.arrayContaining([
      { label: '上一次召回数量', value: '20 件' },
      { label: '结论', value: '结果骤降' },
    ]));
  });

  it('uses the recall contract and exposes its KG strategy', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'recall', status: 'executed', displayStatus: 'executed' }],
      recall: {
        currentResultCount: 2,
        previousResultCount: 8,
        comparisonStatus: 'drop',
        conclusion: '结果少',
        kgStrategy: {
          items: ['新增标签：轻薄'],
          tags: [{ name: '轻薄', status: 'pending_create', resultCount: null }],
        },
      },
      events: [],
      actions: [],
    });

    expect(nodes[0].summary).toBe('查询到 2 件商品，较上一次 8 件；结论：结果少');
    expect(nodes[0].kgStrategy).toEqual({
      items: ['新增标签：轻薄'],
      tags: [{ name: '轻薄', status: 'pending_create', resultCount: null }],
    });
  });

  it('hides the duplicated recall echo when the execution result repeats a shown conclusion', () => {
    const conclusion = '本次召回 24 件，数量充足，无需调整。';
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'recall', status: 'executed', displayStatus: 'executed', summary: conclusion }],
      recall: { currentResultCount: 24, previousResultCount: null, comparisonStatus: 'not_comparable', conclusion },
      events: [], actions: [],
    });

    expect(nodes[0].details.some((entry) => entry.label === '执行结果')).toBe(false);
    expect(nodes[0].details).toEqual(expect.arrayContaining([
      { label: '当前召回数量', value: '24 件' },
      { label: '结论', value: conclusion },
    ]));
  });

  it('keeps the recall execution result when it adds information beyond the conclusion', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'recall', status: 'executed', displayStatus: 'executed', summary: 'Mixed 返回 24 件，已写入快照。' }],
      recall: { currentResultCount: 24, previousResultCount: null, comparisonStatus: 'not_comparable', conclusion: '正常' },
      events: [], actions: [],
    });

    expect(nodes[0].details).toEqual(expect.arrayContaining([
      { label: '结论', value: '正常' },
      { label: '执行结果', value: 'Mixed 返回 24 件，已写入快照。' },
    ]));
  });

  it('exposes recall rate metrics and operator alert when recall rate is below 100%', () => {
    const nodes = buildExecutionOverview({
      steps: [{ checkKey: 'recall', status: 'executed', displayStatus: 'needs_attention', summary: '召回率未达100%' }],
      recall: {
        currentResultCount: 2,
        previousResultCount: 5,
        comparisonStatus: 'normal',
        conclusion: '召回率未达100%',
        recallRate: {
          initialSearchRecallRate: 0.3333,
          postSearchRecallRate: 0.6667,
          llmRecallRate: 1.0,
          isFullRecall: false,
          unrecalledProductIds: ['P3'],
          operatorAlert: '白名单/标签补全后仍有 1 件商品未被召回（P3），当前搜索引擎召回率为 66.7%。',
        },
      },
      events: [],
      actions: [],
    });

    expect(nodes[0].summary).toContain('白名单/标签补全后仍有 1 件商品未被召回（P3），当前搜索引擎召回率为 66.7%。');
    expect(nodes[0].details).toEqual(expect.arrayContaining([
      { label: '治理前搜索召回率', value: '33.33%' },
      { label: '当前搜索引擎召回率', value: '66.67%' },
      { label: '大模型召回率', value: '100.00%' },
      { label: '仍未召回商品', value: 'P3' },
      { label: '召回率复检提示', value: '白名单/标签补全后仍有 1 件商品未被召回（P3），当前搜索引擎召回率为 66.7%。' },
    ]));
  });
});
