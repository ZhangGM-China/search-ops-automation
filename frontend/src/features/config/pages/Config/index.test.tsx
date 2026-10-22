import { render, screen, waitFor, fireEvent, cleanup, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import ConfigPage from './index';
import {
  formatSocialTrendsText,
  normalizeSocialTrendsText,
  normalizeWeatherCityCodes,
} from './components/brandConfigUtils';
import { adminApi, dashboardApi } from '@/shared/api';
import { axiosResponse } from '@/testUtils/axios';

vi.mock('@/shared/api', () => ({
  adminApi: {
    getConfig: vi.fn(),
    updateConfig: vi.fn(),
    getNodeModelConfig: vi.fn(),
    createNodeModelConfigDraft: vi.fn(),
    publishNodeModelConfig: vi.fn(),
    rollbackNodeModelConfig: vi.fn(),
    getPageConfig: vi.fn(),
    updatePageConfig: vi.fn(),
  },
  dashboardApi: {
    getTopQueryExperimentBrandStatus: vi.fn(),
    getTopQueryExperimentHistory: vi.fn(),
    getTopQueryExperimentSamplePool: vi.fn(),
    createTopQueryExperimentDraft: vi.fn(),
    getTopQueryExperimentDetail: vi.fn(),
    scheduleTopQueryExperiment: vi.fn(),
    endTopQueryExperiment: vi.fn(),
    getTopQueryExperimentReport: vi.fn(),
    exportTopQueryExperiment: vi.fn(),
  },
}));

const baseModes = {
  recall: 'human_approval',
  relevance: 'auto',
  open_factor: 'auto',
  inventory: 'auto',
};

const toolModes = {
  product_blacklist: 'auto',
  query_demote: 'auto',
  tag_bucket_config: 'auto',
  kg_tag: 'human_approval',
  kg_relation: 'human_approval',
  query_reorder: 'auto',
};

const mockConfig = {
  safety_config: {
    circuit_breaker_threshold: 10,
    max_daily_adjustments: 1000,
    max_retry_count: 3,
    api_delay_threshold: 5,
    assistant_launcher_mode: 'floating_mascot',
    health_check_stale_minutes: 90,
    health_check_error_rate_threshold_percent: 20,
    health_check_api_p95_threshold_seconds: 5,
  },
  brand_configs: {
    atlas: {
      enabled: true,
      no_result_governance_enabled: true,
      no_result_extract_limit: 25,
      no_result_search_pv_threshold: 5,
      no_result_search_uv_threshold: 3,
      no_result_excluded_keywords: ['积分', '门店'],
      no_result_execution_mode: 'dry_run',
      no_result_retag_cooldown_hours: 2,
      no_result_review_wait_minutes: 30,
      no_result_top_n: 16,
      no_result_agent_max_rounds: 4,
      no_result_ignore_ttl_days: 14,
      top_query_governance_enabled: true,
      query_governance_extract_limit: 150,
      query_governance_reorder_enabled: true,
      top_query_check_product_limit: 16,
      query_governance_preference_top_purchase_product_limit: 8,
      query_governance_preference_top_click_product_limit: 8,
      top_query_low_result_threshold: 4,
      top_query_result_drop_threshold: 0.3,
      top_query_inventory_broken_rate_threshold: 50,
      top_query_inventory_check_interval_minutes: 60,
      top_query_inventory_max_cycles: 3,
      query_governance_relevance_multimodal_concurrency: 5,
      query_governance_relevance_batch_size: 240,
      query_governance_relevance_request_max_bytes: 20971520,
      query_governance_relevance_model_timeout_seconds: 1200,
      query_governance_relevance_image_download_timeout_seconds: 30,
      query_governance_relevance_max_completion_tokens: 131072,
      top_query_check_modes: baseModes,
      top_query_tool_modes: toolModes,
      top_query_world_knowledge_config: {
        weather_cities: [{ city: '上海', city_code: 101020100 }, { city: '杭州', city_code: 101210101 }],
        weather_mcp: { command: 'weather-mcp', tool_name: 'get_weather_by_city_code' },
        festival_mcp: { command: 'festival-mcp', tool_name: 'get_festival' },
        social_trends: [{ enabled: true, keywords: ['连衣裙'], tags: ['无袖', '轻薄'] }],
      },
    },
    harbor: {
      enabled: true,
      no_result_governance_enabled: false,
      no_result_extract_limit: 40,
      no_result_search_pv_threshold: 8,
      no_result_search_uv_threshold: 4,
      no_result_excluded_keywords: ['门店'],
      no_result_execution_mode: 'dry_run',
      no_result_retag_cooldown_hours: 3,
      no_result_review_wait_minutes: 45,
      no_result_top_n: 20,
      no_result_agent_max_rounds: 5,
      no_result_ignore_ttl_days: 21,
      top_query_governance_enabled: true,
      query_governance_extract_limit: 80,
      top_query_check_product_limit: 20,
      query_governance_preference_top_purchase_product_limit: 9,
      query_governance_preference_top_click_product_limit: 10,
      top_query_low_result_threshold: 5,
      top_query_result_drop_threshold: 0.35,
      top_query_inventory_broken_rate_threshold: 65,
      top_query_inventory_check_interval_minutes: 120,
      top_query_inventory_max_cycles: 4,
      top_query_check_modes: baseModes,
      top_query_tool_modes: toolModes,
      top_query_world_knowledge_config: {
        weather_cities: [{ city: '上海', city_code: 101020100 }],
        social_trends: [],
      },
    },
  },
};

const mockNodeModelConfig = {
  activeVersion: 12,
  nodes: [
    {
      nodeKey: 'no_result.intent',
      channel: 'rd_llm',
      primaryModel: 'qwen3.6-plus',
      fallbackModel: null,
      primarySource: 'published',
      fallbackSource: 'published',
    },
    {
      nodeKey: 'assistant.chat',
      channel: 'pi_agent',
      primaryModel: 'MiniMax-M2.5',
      fallbackModel: 'qwen3.7-max',
      primarySource: 'published',
      fallbackSource: 'published',
    },
  ],
};

describe('ConfigPage Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    window.history.replaceState({}, '', '/config');
    vi.mocked(adminApi.getConfig).mockImplementation(async () => axiosResponse(structuredClone(mockConfig)));
    vi.mocked(adminApi.getNodeModelConfig).mockResolvedValue(axiosResponse(mockNodeModelConfig));
    vi.mocked(adminApi.createNodeModelConfigDraft).mockResolvedValue(axiosResponse({
      status: 'success',
      version: 13,
      state: 'draft',
      nodes: mockNodeModelConfig.nodes,
    }));
    vi.mocked(adminApi.publishNodeModelConfig).mockResolvedValue(axiosResponse({
      status: 'success',
      activeVersion: 13,
      nodes: mockNodeModelConfig.nodes,
    }));
    vi.mocked(adminApi.rollbackNodeModelConfig).mockResolvedValue(axiosResponse({
      status: 'success',
      activeVersion: 14,
      nodes: mockNodeModelConfig.nodes,
      rolledBackFromVersion: 13,
      rolledBackToVersion: 12,
    }));
    vi.mocked(adminApi.getPageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 1,
      version: 0,
      values: {
        'brand_overview.card.show_operational_data': true,
        'brand_detail.yesterday.show': true,
      },
      definitions: [],
    }));
    vi.mocked(adminApi.updatePageConfig).mockResolvedValue(axiosResponse({
      schemaVersion: 1,
      version: 1,
      values: {
        'brand_overview.card.show_operational_data': false,
        'brand_detail.yesterday.show': true,
      },
      definitions: [],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({
      brand: 'atlas',
      current: null,
      items: [],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentHistory).mockResolvedValue(axiosResponse({ brand: 'atlas', current: null, items: [], total: 0, page: 1, pageSize: 20 }));
    vi.mocked(dashboardApi.getTopQueryExperimentSamplePool).mockResolvedValue(axiosResponse({
      brand: 'atlas',
      sampleDataDate: '2026-07-07',
      items: [
        { queryText: '连衣裙', governanceKey: '连衣裙', queryRank: 1, searchUv: 200, searchPv: 500, ctr: 0.3, cvr: 0.05, avgCtr: 0.29, avgCvr: 0.04, queryPvShare: 0.25 },
        { queryText: '牛仔裤 女士', governanceKey: '牛仔裤', queryRank: 2, searchUv: 180, searchPv: 460, ctr: 0.28, cvr: 0.04, avgCtr: 0.27, avgCvr: 0.039, queryPvShare: 0.23 },
      ],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentReport).mockResolvedValue(axiosResponse({
      reportSnapshotToken: 'snapshot-1-v1',
      experiment: { experimentId: 1, brand: 'atlas', status: 'running', calculationVersion: 'top-query-ab-test-v2' },
      calculationVersion: 'top-query-ab-test-v2', generatedAt: '2026-07-12T08:00:00+08:00', timezone: 'Asia/Shanghai',
      baselineQuality: { status: 'complete', calculationVersion: 'top-query-ab-test-v2', sampleCount: 40, treatmentSamples: 20, controlSamples: 20, expectedDays: 7, windowValid: true, locked: true },
      quality: { status: 'complete', latestDayQuality: { expectedSamples: 40, observedSamples: 40, treatmentSamples: 20, controlSamples: 20, treatmentTaskCount: 20, controlTaskCount: 0, controlActionCount: 0, contaminationCount: 0, failedTaskCount: 0, unresolvedTaskCount: 0, failedActionCount: 0, unresolvedActionCount: 0 } },
      groups: {}, effect: null,
      operations: { governanceCoverage: 0, actionSuccessRate: null, plannedActions: 0, executedActions: 0, failedActions: 0, tokenUsage: 0, durationSeconds: 0 },
      trend: [], queries: [], queryDailyMetrics: [], baselineEvidence: [], actions: [], dataDays: 0,
      overview: {
        experimentId: 1, brand: 'atlas', status: 'running',
        quality: { status: 'complete', latestDayQuality: { expectedSamples: 40, observedSamples: 40, treatmentSamples: 20, controlSamples: 20, treatmentTaskCount: 20, controlTaskCount: 0, controlActionCount: 0, contaminationCount: 0, failedTaskCount: 0, unresolvedTaskCount: 0, failedActionCount: 0, unresolvedActionCount: 0 } },
        groups: {}, effect: null,
        operations: { governanceCoverage: 0, actionSuccessRate: null, plannedActions: 0, executedActions: 0, failedActions: 0, tokenUsage: 0, durationSeconds: 0 },
      },
    }));
  });

  afterEach(async () => {
    cleanup();
    await act(async () => {
      await new Promise(resolve => window.setTimeout(resolve, 0));
    });
  });

  it('renders brand-only business configuration and system safety', async () => {
    render(<ConfigPage />);

    expect(await screen.findByText('智能运维', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '品牌配置' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '模型配置' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '安全配置' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '页面配置' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'A/B Test' })).toBeInTheDocument();
    expect(screen.queryByText('节点模型配置')).not.toBeInTheDocument();
    expect(screen.queryByText('品牌 A/B 实验')).not.toBeInTheDocument();
    expect(screen.queryByText('安全配置', { selector: '.ops-config-section__title' })).not.toBeInTheDocument();
    expect(screen.queryByText('Top Query 执行演练')).not.toBeInTheDocument();
    expect(screen.queryByText('全局默认配置')).not.toBeInTheDocument();
    expect(screen.queryByText('全局安全配置')).not.toBeInTheDocument();
    expect(screen.queryByText('启用品牌治理')).not.toBeInTheDocument();
    expect(screen.queryByText('基础信息')).not.toBeInTheDocument();
    expect(screen.queryByText('自动运维')).not.toBeInTheDocument();
    expect(screen.queryByText('基础配置')).not.toBeInTheDocument();
    expect(screen.getByLabelText('重排序')).toBeChecked();
    expect(screen.getByText('召回数量', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getByText('断码', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getByLabelText('断码补位最多轮数')).toHaveValue('3');
    expect(screen.getByText('相关性', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getByLabelText('相关性 AI 并发数')).toHaveValue('5');
    expect(screen.getByLabelText('相关性单批商品数')).toHaveValue('240');
    expect(screen.getByLabelText('商品图片下载超时（秒）')).toHaveValue('30');
    expect(screen.getByLabelText('相关性 AI 超时（秒）')).toHaveValue('1200');
    expect(screen.queryByText('数据分类配置')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('搜索 UV下降阈值（%）')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('点击 UV下降阈值（%）')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('加购 UV下降阈值（%）')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('购买 UV下降阈值（%）')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('CTR下降阈值（%）')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('ATC下降阈值（%）')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('CVR下降阈值（%）')).not.toBeInTheDocument();
    expect(screen.getByText('Agent执行模式')).toBeInTheDocument();
    expect(screen.getByText('运营工具执行模式')).toBeInTheDocument();
    expect(screen.queryByText('Agent专家团配置')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('启用专家')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('单专家总 Token 预算')).not.toBeInTheDocument();
    expect(screen.queryByText('世界知识配置')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('天气城市')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('社媒趋势背景知识')).not.toBeInTheDocument();
    expect(screen.getByText('无结果词', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.queryByText('传统自动运维')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('旧任务单 Query 商品数')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('旧任务 Top Query 数')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('描述不符自动拉黑')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('价格偏高自动分桶')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('价格偏高阈值(%)')).not.toBeInTheDocument();
    expect(screen.queryByText('价格配置')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('断码自动分桶')).not.toBeInTheDocument();
  });

  it('restores the A/B Test workspace from URL without loading system config', async () => {
    window.history.replaceState({}, '', '/config?tab=ab-test&brand=atlas');

    render(<ConfigPage />);

    expect(await screen.findByRole('tab', { name: 'A/B Test' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByText('实验工作台', undefined, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText('安全配置', { selector: '.ops-config-section__title' })).not.toBeInTheDocument();
    expect(adminApi.getConfig).not.toHaveBeenCalled();
    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledWith('atlas', { page: 1, pageSize: 20 }, expect.any(AbortSignal));
  });

  it('loads a current experiment once when URL starts without an experiment id', async () => {
    window.history.replaceState({}, '', '/config?tab=ab-test&brand=atlas');
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValue(axiosResponse({
      brand: 'atlas', current: { id: 1, brand: 'atlas', status: 'running', treatmentCount: 20, controlCount: 20 }, items: [], total: 1, page: 1, pageSize: 20,
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValue(axiosResponse({ id: 1, brand: 'atlas', experimentName: 'ATLAS A/B Test', status: 'running', samples: [] }));
    render(<ConfigPage />);

    expect(await screen.findByText('实验质量正常')).toBeInTheDocument();
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 20)); });
    expect(new URLSearchParams(window.location.search).get('experimentId')).toBe('1');
    expect(new URLSearchParams(window.location.search).get('view')).toBe('effect');
    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentDetail).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentReport).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getTopQueryExperimentSamplePool).not.toHaveBeenCalled();
    expect(dashboardApi.getTopQueryExperimentHistory).not.toHaveBeenCalled();
  });

  it('restores the effect report from the URL', async () => {
    window.history.replaceState({}, '', '/config?tab=ab-test&brand=atlas&experimentId=1&view=effect');
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({
      brand: 'atlas', current: { id: 1, brand: 'atlas', status: 'running', treatmentCount: 20, controlCount: 20 }, items: [],
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValueOnce(axiosResponse({ id: 1, brand: 'atlas', status: 'running', samples: [] }));

    render(<ConfigPage />);

    expect(await screen.findByRole('heading', { name: '1. 效果结论' })).toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get('view')).toBe('effect');
    expect(dashboardApi.getTopQueryExperimentReport).toHaveBeenCalledTimes(1);
  });

  it('switches primary tabs and keeps the selected tab in the URL', async () => {
    window.history.replaceState({}, '', '/config');
    const pushState = vi.spyOn(window.history, 'pushState');
    render(<ConfigPage />);

    fireEvent.click(await screen.findByRole('tab', { name: 'A/B Test' }));

    expect(new URLSearchParams(window.location.search).get('tab')).toBe('ab-test');
    expect(pushState).toHaveBeenCalledTimes(1);
    expect(await screen.findByText('实验工作台')).toBeInTheDocument();
  });

  it('restores the standalone model configuration tab from URL and does not load system config', async () => {
    window.history.replaceState({}, '', '/config?tab=model-config');
    render(<ConfigPage />);

    expect(await screen.findByRole('tab', { name: '模型配置' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByText('节点模型配置')).toBeInTheDocument();
    expect(screen.queryByText('安全配置', { selector: '.ops-config-section__title' })).not.toBeInTheDocument();
    expect(adminApi.getNodeModelConfig).toHaveBeenCalledTimes(1);
    expect(adminApi.getConfig).not.toHaveBeenCalled();
  });

  it('restores the page configuration tab from URL for platform admins', async () => {
    window.history.replaceState({}, '', '/config?tab=page-config');
    render(<ConfigPage />);

    expect(await screen.findByRole('tab', { name: '页面配置' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('品牌总览')).toBeInTheDocument();
    expect(screen.getByText('品牌详情')).toBeInTheDocument();
    expect(adminApi.getConfig).not.toHaveBeenCalled();
  });

  it('rejects a model configuration deep link for users without platform config permission', async () => {
    localStorage.setItem('user', JSON.stringify({ role: 'brand_member', pagePermissions: ['/config'], brandPermissions: ['atlas'], actionPermissions: ['manage_brand_config'] }));
    window.history.replaceState({}, '', '/config?tab=model-config&brand=atlas');
    render(<ConfigPage />);

    expect(await screen.findByRole('tab', { name: '品牌配置' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('tab', { name: '模型配置' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: '页面配置' })).not.toBeInTheDocument();
    expect(adminApi.getNodeModelConfig).not.toHaveBeenCalled();
  });

  it('protects an unsaved model draft when switching primary tabs', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<ConfigPage />);

    fireEvent.click(await screen.findByRole('tab', { name: '模型配置' }));
    await screen.findByText('节点模型配置');
    fireEvent.click(screen.getByRole('button', { name: '编辑模型' }));
    fireEvent.change(screen.getByLabelText('no_result.intent 主模型'), { target: { value: 'deepseek-v3.1' } });
    fireEvent.click(screen.getByRole('tab', { name: '品牌配置' }));

    expect(confirmSpy).toHaveBeenCalledWith('当前页面存在未保存的修改，离开后将丢失。是否继续？');
    expect(screen.getByRole('tab', { name: '模型配置' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByDisplayValue('deepseek-v3.1')).toBeInTheDocument();
    confirmSpy.mockRestore();
  });

  it('restores URL-driven navigation and keeps a dirty draft when browser back is rejected', async () => {
    window.history.replaceState({}, '', '/config?tab=ab-test&brand=atlas');
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<ConfigPage />);

    fireEvent.mouseDown(await screen.findByLabelText('实验组 Query'));
    const option = (await screen.findAllByText('连衣裙')).find((node) => node.closest('.ant-select-item-option'))?.closest('.ant-select-item-option');
    fireEvent.click(option!);
    window.history.replaceState({}, '', '/config?tab=system');
    fireEvent.popState(window);

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole('tab', { name: 'A/B Test' })).toHaveAttribute('aria-selected', 'true');
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('ab-test');
    expect(screen.getByText('连衣裙', { selector: '.experiment-query-tag' })).toBeInTheDocument();
  });

  it('defaults read-only brand users to A/B Test and never mounts system config', async () => {
    localStorage.setItem('user', JSON.stringify({ role: 'brand_member', pagePermissions: ['/config'], brandPermissions: ['atlas'], actionPermissions: [] }));
    render(<ConfigPage />);
    expect(await screen.findByRole('tab', { name: 'A/B Test' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('tab', { name: '品牌配置' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: '安全配置' })).not.toBeInTheDocument();
    expect(adminApi.getConfig).not.toHaveBeenCalled();
    expect(adminApi.getNodeModelConfig).not.toHaveBeenCalled();
    await waitFor(() => expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledWith('atlas', expect.any(Object), expect.anything()));
  });

  it('treats an explicit empty brand permission list as no access and sends no experiment request', async () => {
    localStorage.setItem('user', JSON.stringify({ role: 'brand_member', pagePermissions: ['/config'], brandPermissions: [], actionPermissions: [] }));
    render(<ConfigPage />);
    expect(await screen.findByText('当前账号未分配品牌权限')).toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentBrandStatus).not.toHaveBeenCalled();
    expect(dashboardApi.getTopQueryExperimentSamplePool).not.toHaveBeenCalled();
    expect(adminApi.getConfig).not.toHaveBeenCalled();
  });

  it('keeps model configuration available to platform admins without brand assignments', async () => {
    localStorage.setItem('user', JSON.stringify({ role: 'platform_admin', pagePermissions: ['/config'], brandPermissions: [], actionPermissions: ['manage_platform_config'] }));
    window.history.replaceState({}, '', '/config?tab=model-config');
    render(<ConfigPage />);

    expect(await screen.findByText('节点模型配置')).toBeInTheDocument();
    expect(adminApi.getNodeModelConfig).toHaveBeenCalledTimes(1);
    expect(adminApi.getConfig).not.toHaveBeenCalled();
  });

  it('renders A/B contextual actions inside the same sticky primary header', async () => {
    window.history.replaceState({}, '', '/config?tab=ab-test&brand=atlas');
    render(<ConfigPage />);
    const header = document.querySelector('.config-primary-header');
    await waitFor(() => expect(header).toContainElement(screen.getByRole('button', { name: /刷新/ })));
    expect(header).toContainElement(screen.getByRole('tab', { name: 'A/B Test' }));
  });

  it('renders configuration tabs as a compact secondary navigation without contextual description text', async () => {
    render(<ConfigPage />);

    const secondaryNav = document.querySelector<HTMLElement>('nav.config-primary-header');
    const workspaceShell = document.querySelector<HTMLElement>('.config-workspace-shell');
    expect(secondaryNav).toBeInTheDocument();
    expect(workspaceShell).toContainElement(secondaryNav);
    expect(workspaceShell?.querySelector('.ops-page.config-workspace')).toBeInTheDocument();
    expect(secondaryNav).toContainElement(screen.getByRole('tab', { name: '品牌配置' }));
    expect(secondaryNav).toContainElement(screen.getByRole('tab', { name: '安全配置' }));
    expect(secondaryNav).toContainElement(screen.getByRole('tab', { name: '模型配置' }));
    expect(screen.queryByText('调整品牌治理参数与系统安全策略')).not.toBeInTheDocument();
    expect(screen.queryByText('管理模型节点的主备模型与版本发布')).not.toBeInTheDocument();
    expect(screen.queryByText('查看固定样本实验的运行质量和净效果')).not.toBeInTheDocument();
  });

  it('places the brand switcher in the left slot of the secondary navigation', async () => {
    render(<ConfigPage />);

    const secondaryNav = document.querySelector<HTMLElement>('nav.config-primary-header');
    const brandSlot = document.querySelector<HTMLElement>('#config-context-brand');
    const actionsSlot = document.querySelector<HTMLElement>('#config-context-actions');

    expect(secondaryNav).toContainElement(brandSlot);
    expect(secondaryNav).toContainElement(actionsSlot);
    await waitFor(() => expect(brandSlot).toContainElement(screen.getByLabelText('当前品牌')));
    expect(actionsSlot).not.toContainElement(screen.getByLabelText('当前品牌'));
  });

  it('renders node model config section for platform admins', async () => {
    render(<ConfigPage />);

    fireEvent.click(await screen.findByRole('tab', { name: '模型配置' }));
    expect(await screen.findByText('节点模型配置')).toBeInTheDocument();
    expect(screen.getByText('当前生效 v12')).toBeInTheDocument();
    expect(screen.getByText('no_result.intent')).toBeInTheDocument();
    expect(screen.getByText('assistant.chat')).toBeInTheDocument();
    expect(screen.getByDisplayValue('qwen3.6-plus')).toBeInTheDocument();
    expect(screen.getByDisplayValue('MiniMax-M2.5')).toBeInTheDocument();
    expect(adminApi.getNodeModelConfig).toHaveBeenCalledTimes(1);
  });

  it('publishes node model config changes through draft then publish', async () => {
    render(<ConfigPage />);

    fireEvent.click(await screen.findByRole('tab', { name: '模型配置' }));
    await screen.findByText('节点模型配置');
    fireEvent.click(screen.getByRole('button', { name: '编辑模型' }));
    fireEvent.change(screen.getByLabelText('no_result.intent 主模型'), { target: { value: 'deepseek-v3.1' } });
    fireEvent.change(screen.getByLabelText('变更说明'), { target: { value: '切换无结果词意图节点主模型' } });
    fireEvent.click(screen.getByRole('button', { name: '发布配置' }));

    await waitFor(() => expect(adminApi.createNodeModelConfigDraft).toHaveBeenCalledWith({
      entries: {
        'no_result.intent': {
          channel: 'rd_llm',
          primaryModel: 'deepseek-v3.1',
          fallbackModel: null,
        },
        'assistant.chat': {
          channel: 'pi_agent',
          primaryModel: 'MiniMax-M2.5',
          fallbackModel: 'qwen3.7-max',
        },
      },
      changeNote: '切换无结果词意图节点主模型',
      requestId: expect.stringMatching(/^node-model-config-draft:/),
    }));
    const draftRequestId = vi.mocked(adminApi.createNodeModelConfigDraft).mock.calls[0][0].requestId!;
    expect(adminApi.publishNodeModelConfig).toHaveBeenCalledWith(13, {
      expectedVersion: 12,
      requestId: draftRequestId.replace('node-model-config-draft:', 'node-model-config-publish:'),
    });
  });

  it('rolls back node model config to a chosen version', async () => {
    render(<ConfigPage />);

    fireEvent.click(await screen.findByRole('tab', { name: '模型配置' }));
    await screen.findByText('节点模型配置');
    fireEvent.click(screen.getByRole('button', { name: '回滚版本' }));
    fireEvent.change(screen.getByLabelText('回滚目标版本'), { target: { value: '12' } });
    fireEvent.click(screen.getByRole('button', { name: '确认回滚' }));

    await waitFor(() => expect(adminApi.rollbackNodeModelConfig).toHaveBeenCalledWith({
      expectedVersion: 12,
      targetVersion: 12,
      requestId: expect.stringMatching(/^node-model-config-rollback:/),
    }));
  });

  it('protects an unsaved experiment draft when switching brands', async () => {
    window.history.replaceState({}, '', '/config?tab=ab-test&brand=atlas');
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<ConfigPage />);

    fireEvent.mouseDown(await screen.findByLabelText('实验组 Query'));
    const option = (await screen.findAllByText('连衣裙')).find((node) => node.closest('.ant-select-item-option'))?.closest('.ant-select-item-option');
    fireEvent.click(option!);
    fireEvent.mouseDown(screen.getByLabelText('A/B Test 品牌'));
    fireEvent.click((await screen.findAllByText('Harbor')).at(-1)!);

    expect(confirmSpy).toHaveBeenCalled();
    expect(new URLSearchParams(window.location.search).get('brand')).toBe('atlas');
  });

  it('keeps dirty sample selections without reloading the experiment workspace', async () => {
    window.history.replaceState({}, '', '/config?tab=ab-test&brand=atlas');
    render(<ConfigPage />);

    fireEvent.mouseDown(await screen.findByLabelText('实验组 Query'));
    const option = (await screen.findAllByText('连衣裙')).find((node) => node.closest('.ant-select-item-option'))?.closest('.ant-select-item-option');
    fireEvent.click(option!);
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 20)); });

    expect(screen.getByText('连衣裙', { selector: '.experiment-query-tag' })).toBeInTheDocument();
    expect(dashboardApi.getTopQueryExperimentBrandStatus).toHaveBeenCalledTimes(1);
  });

  it('saves a dirty draft once, updates the URL, and does not confirm its own save navigation', async () => {
    window.history.replaceState({}, '', '/config?tab=ab-test&brand=atlas');
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(dashboardApi.createTopQueryExperimentDraft).mockResolvedValueOnce(axiosResponse({
      id: 10,
      brand: 'atlas',
      status: 'draft',
      sampleDataDate: '2026-07-07',
      samples: [{ queryText: '连衣裙', groupType: 'treatment', sampleRank: 1, sampleSearchUv: 200, sampleSearchPv: 500, sampleCtr: 0.3, sampleCvr: 0.05 }],
    }));
    render(<ConfigPage />);

    fireEvent.mouseDown(await screen.findByLabelText('实验组 Query'));
    const option = (await screen.findAllByText('连衣裙')).find((node) => node.closest('.ant-select-item-option'))?.closest('.ant-select-item-option');
    fireEvent.click(option!);
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));

    await waitFor(() => expect(new URLSearchParams(window.location.search).get('experimentId')).toBe('10'));
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(dashboardApi.createTopQueryExperimentDraft).toHaveBeenCalledTimes(1);
  });

  it('confirms dirty history navigation exactly once', async () => {
    window.history.replaceState({}, '', '/config?tab=ab-test&brand=atlas');
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(dashboardApi.getTopQueryExperimentBrandStatus).mockResolvedValueOnce(axiosResponse({
      brand: 'atlas', current: null, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20,
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentHistory).mockResolvedValueOnce(axiosResponse({
      brand: 'atlas', current: null, items: [{ id: 88, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }], total: 1, page: 1, pageSize: 20,
    }));
    vi.mocked(dashboardApi.getTopQueryExperimentDetail).mockResolvedValue(axiosResponse({ id: 88, brand: 'atlas', status: 'ended', samples: [] }));
    render(<ConfigPage />);

    fireEvent.mouseDown(await screen.findByLabelText('实验组 Query'));
    const option = (await screen.findAllByText('连衣裙')).find((node) => node.closest('.ant-select-item-option'))?.closest('.ant-select-item-option');
    fireEvent.click(option!);
    fireEvent.click(screen.getByText('ATLAS A/B Test #88'));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(new URLSearchParams(window.location.search).get('experimentId')).toBe('88'));
  });

  it('hides platform safety section when config payload only contains brand configs', async () => {
    vi.mocked(adminApi.getConfig).mockResolvedValueOnce(axiosResponse({
      brand_configs: {
        atlas: mockConfig.brand_configs.atlas,
      },
    }));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    expect(screen.queryByText('安全配置', { selector: '.ops-config-section__title' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('工作流重试次数')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: '安全配置' }));
    expect(screen.queryByLabelText('工作流重试次数')).not.toBeInTheDocument();
  });

  it('only shows owned brand tabs when config payload is already filtered by backend', async () => {
    vi.mocked(adminApi.getConfig).mockResolvedValueOnce(axiosResponse({
      brand_configs: {
        atlas: mockConfig.brand_configs.atlas,
      },
    }));

    render(<ConfigPage />);

    expect(await screen.findByText('智能运维', { selector: '.ops-config-section__title' })).toBeInTheDocument();
    expect(screen.getByText('ATLAS', { selector: '.ops-brand-switcher__option span:last-child' })).toBeInTheDocument();
  });

  it('loads selected brand values without global defaults', async () => {
    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    expect(screen.queryByLabelText('单个 Query 治理商品数默认值')).not.toBeInTheDocument();
    expect(screen.queryByText('尺码')).not.toBeInTheDocument();
    expect(screen.queryByText('治理评分')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('通过率阈值')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('结果追踪上限天数')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Token 上限')).not.toBeInTheDocument();
    expect(screen.getAllByLabelText('每日最多治理 Query 数')[0]).toHaveValue('150');
    expect(screen.getAllByLabelText('每个 Query 最多检查商品数')[0]).toHaveValue('16');
    expect(screen.queryByLabelText('工作流重试次数')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: '安全配置' }));
    expect(await screen.findByLabelText('工作流重试次数')).toHaveValue('3');
  });

  it('allows legacy admin sessions to enter config edit mode', async () => {
    localStorage.setItem('user', JSON.stringify({
      username: 'admin',
      isAdmin: true,
      pagePermissions: ['/config'],
      brandPermissions: ['atlas'],
    }));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    expect(screen.getByRole('button', { name: '修改配置' })).toBeEnabled();
  });

  it('does not resubmit platform safety configuration while saving brand configuration', async () => {
    vi.mocked(adminApi.updateConfig).mockResolvedValue(axiosResponse(mockConfig));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.click(screen.getByRole('button', { name: '确认修改' }));

    await waitFor(() => {
      expect(adminApi.updateConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          brand_configs: expect.any(Object),
        }),
      );
      expect(adminApi.updateConfig).toHaveBeenCalledWith(expect.not.objectContaining({ safety_config: expect.anything() }));
    });
  });

  it('saves platform safety configuration from its own tab only', async () => {
    vi.mocked(adminApi.updateConfig).mockResolvedValue(axiosResponse(mockConfig));
    render(<ConfigPage />);

    fireEvent.click(await screen.findByRole('tab', { name: '安全配置' }));
    await screen.findByLabelText('工作流重试次数');
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.change(screen.getByLabelText('工作流重试次数'), { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: '确认修改' }));

    await waitFor(() => expect(adminApi.updateConfig).toHaveBeenCalledWith(expect.objectContaining({
      safety_config: expect.objectContaining({ max_retry_count: 4 }),
      brand_configs: {},
    })));
  });

  it('switches brand single-point configuration', async () => {
    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    expect(screen.getByLabelText('当前品牌')).toBeInTheDocument();
    expect(screen.getAllByLabelText('每日最多治理 Query 数')[0]).toHaveValue('150');

    fireEvent.click(screen.getByRole('button', { name: '下一个品牌' }));

    await waitFor(() => expect(screen.getAllByLabelText('每日最多治理 Query 数')[0]).toHaveValue('80'));
    expect(screen.getAllByLabelText('每日最多治理 Query 数')[0]).toHaveValue('80');
    expect(screen.getAllByLabelText('断码检查间隔（分钟）')[0]).toHaveValue('120');
  });

  it('asks to save before switching brands when the draft is dirty', async () => {
    vi.mocked(adminApi.updateConfig).mockResolvedValue(axiosResponse(mockConfig));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.change(screen.getAllByLabelText('每日最多治理 Query 数')[0], { target: { value: '20' } });

    fireEvent.mouseDown(screen.getByLabelText('当前品牌'));
    fireEvent.click(await screen.findByText('Harbor'));
    expect(await screen.findByRole('dialog')).toHaveTextContent('配置有未保存变更');
    fireEvent.click(screen.getByRole('button', { name: '保存并切换' }));

    await waitFor(() => expect(adminApi.updateConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        brand_configs: expect.objectContaining({
          atlas: expect.objectContaining({ query_governance_extract_limit: 20 }),
        }),
      }),
    ));

    await waitFor(() => expect(adminApi.getConfig).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getAllByLabelText('每日最多治理 Query 数')[0]).toHaveValue('80'));
  });

  it('submits only authorized brand drafts for brand-scoped config payloads', async () => {
    vi.mocked(adminApi.getConfig).mockResolvedValue(axiosResponse({
      brand_configs: {
        atlas: mockConfig.brand_configs.atlas,
      },
    }));
    vi.mocked(adminApi.updateConfig).mockResolvedValue(axiosResponse({ msg: 'ok' }));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    expect(screen.getByLabelText('当前品牌')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.change(screen.getAllByLabelText('每日最多治理 Query 数')[0], { target: { value: '20' } });
    fireEvent.click(screen.getByRole('button', { name: '确认修改' }));

    await waitFor(() => {
      expect(adminApi.updateConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          brand_configs: {
            atlas: expect.objectContaining({
              query_governance_extract_limit: 20,
            }),
          },
        }),
      );
    });
    expect(adminApi.updateConfig).toHaveBeenCalledWith(
      expect.not.objectContaining({
        safety_config: expect.anything(),
      }),
    );
    const payload = vi.mocked(adminApi.updateConfig).mock.calls[0][0];
    expect(Object.keys(payload.brand_configs)).toEqual(['atlas']);
  });


  it('submits only brand configs from the brand configuration tab', async () => {
    vi.mocked(adminApi.updateConfig).mockResolvedValue(axiosResponse(mockConfig));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.change(screen.getAllByLabelText('无结果词拉取数量')[0], { target: { value: '33' } });
    fireEvent.click(screen.getByRole('button', { name: '确认修改' }));

    await waitFor(() => {
      expect(adminApi.updateConfig).toHaveBeenCalledWith(
        expect.not.objectContaining({ global_config: expect.anything() })
      );
      expect(adminApi.updateConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          brand_configs: expect.objectContaining({
            atlas: expect.objectContaining({
              no_result_extract_limit: 33,
            }),
          }),
        })
      );
      expect(adminApi.updateConfig).toHaveBeenCalledWith(expect.not.objectContaining({ safety_config: expect.anything() }));
    });
  });

  it('submits the selected brand automation switch value', async () => {
    vi.mocked(adminApi.updateConfig).mockResolvedValue(axiosResponse(mockConfig));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.click(screen.getByLabelText('ATLAS智能运维开关'));
    fireEvent.click(screen.getByRole('button', { name: '确认修改' }));

    await waitFor(() => {
      expect(adminApi.updateConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          brand_configs: expect.objectContaining({
            atlas: expect.objectContaining({
              enabled: false,
            }),
          }),
        })
      );
    });
  });

  it('shows and submits deterministic Query selection thresholds', async () => {
    vi.mocked(adminApi.updateConfig).mockResolvedValue(axiosResponse({ msg: 'ok' }));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.change(screen.getByLabelText('每日最多治理 Query 数'), { target: { value: '42' } });
    fireEvent.change(screen.getByLabelText('最低指标数量'), { target: { value: '8' } });
    fireEvent.click(screen.getByRole('button', { name: '确认修改' }));

    await waitFor(() => expect(adminApi.updateConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        brand_configs: expect.objectContaining({
          atlas: expect.objectContaining({
            query_governance_extract_limit: 42,
            query_governance_query_min_metric_type: 'uv',
            query_governance_query_min_metric_value: 8,
          }),
        }),
      }),
    ));
  });

  it('shows and submits Query behavior evidence limits', async () => {
    vi.mocked(adminApi.updateConfig).mockResolvedValue(axiosResponse({ msg: 'ok' }));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    expect(screen.getByLabelText('高购买商品数')).toHaveValue('8');
    expect(screen.getByLabelText('高点击商品数')).toHaveValue('8');
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.change(screen.getByLabelText('高购买商品数'), { target: { value: '12' } });
    fireEvent.change(screen.getByLabelText('高点击商品数'), { target: { value: '10' } });
    fireEvent.click(screen.getByRole('button', { name: '确认修改' }));

    await waitFor(() => expect(adminApi.updateConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        brand_configs: expect.objectContaining({
          atlas: expect.objectContaining({
            query_governance_preference_top_purchase_product_limit: 12,
            query_governance_preference_top_click_product_limit: 10,
          }),
        }),
      }),
    ));
  });

  it('keeps the form available when loading or saving fails', async () => {
    vi.mocked(adminApi.getConfig).mockRejectedValueOnce(new Error('network down'));

    const { unmount } = render(<ConfigPage />);

    await waitFor(() => {
      expect(adminApi.getConfig).toHaveBeenCalled();
    });
    unmount();

    vi.mocked(adminApi.getConfig).mockResolvedValue(axiosResponse(mockConfig));
    vi.mocked(adminApi.updateConfig).mockRejectedValueOnce(new Error('save failed'));

    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.click(screen.getByRole('button', { name: '确认修改' }));

    await waitFor(() => {
      expect(adminApi.updateConfig).toHaveBeenCalled();
    });
    expect(screen.getByRole('button', { name: '确认修改' })).toBeInTheDocument();
  });

  it('cancels edits by restoring the loaded config', async () => {
    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    fireEvent.click(screen.getByRole('button', { name: '修改配置' }));
    fireEvent.click(screen.getAllByRole('button', { name: '取 消' })[0]);

    expect(screen.getAllByLabelText('每日最多治理 Query 数')[0]).toHaveValue('150');
  });

  it('does not load experiment data inside the system config tab', async () => {
    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    expect(dashboardApi.getTopQueryExperimentBrandStatus).not.toHaveBeenCalled();
    expect(dashboardApi.getTopQueryExperimentSamplePool).not.toHaveBeenCalled();
  });

  it('keeps A/B Test out of the system configuration form', async () => {
    render(<ConfigPage />);

    await screen.findByLabelText('当前品牌');
    const brandConfigContainer = document.getElementById('brand-config');

    expect(screen.queryByText('样本池')).not.toBeInTheDocument();
    expect(brandConfigContainer).not.toHaveTextContent('品牌 A/B 实验');
  });
});

describe('world knowledge config presentation helpers', () => {
  it('maps selected weather city codes to backend city objects', () => {
    expect(normalizeWeatherCityCodes(['101020100', '101210101'])).toEqual([
      { city: '上海', city_code: 101020100 },
      { city: '杭州', city_code: 101210101 },
    ]);
  });

  it('round-trips social trend json as natural language text', () => {
    const socialTrends = [{ enabled: true, keywords: ['连衣裙'], tags: ['无袖', '轻薄'] }];

    expect(formatSocialTrendsText(socialTrends)).toBe('连衣裙：无袖、轻薄');
    expect(normalizeSocialTrendsText('连衣裙：无袖、轻薄\n防晒衣：轻量')).toEqual([
      { enabled: true, keywords: ['连衣裙'], tags: ['无袖', '轻薄'] },
      { enabled: true, keywords: ['防晒衣'], tags: ['轻量'] },
    ]);
  });
});
