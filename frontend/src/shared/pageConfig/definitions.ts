import type { PageConfigDefinition, PageConfigKey, PageConfigValue } from '@/shared/types';
import { defaultPageConfigValues } from './context';

export interface MetricOption {
  value: string;
  label: string;
  tip?: string;
}

export const overviewMetricOptions: MetricOption[] = [
  ['query_count', 'Query 数量', '本批次纳入治理流程的 Query 总数量。'],
  ['no_recall_rate', '无召回率', '召回商品为 0 件的 Query 数 ÷ 检查 Query 总数。'],
  ['pass_rate', '通过率', '相关性检测通过的 Query 数 ÷ 检查 Query 总数；仅相关性检测发现不相关商品的 Query 计为未通过。'],
  ['ai_processed_count', 'AI 处理', 'AI 已完成且无需人工处理的 Query 数量。'],
  ['pending_count', '待处理', '仍需人工确认或人工处理的 Query 数量。'],
  ['irrelevant_query_count', '不相关 Query 数', '相关性检测发现不相关商品的 Query 数量。'],
  ['preference_adjusted_query_count', '重排序 Query 数', '产生重排序策略调整建议的 Query 数量。'],
  ['stock_bucket_reduction_product_count', '断码商品数', '断码降级独立流程当天执行的商品总数量，不纳入主流程相关性检测口径。'],
  ['relevance_accuracy', '商品准确率', '(应检商品数 - 不相关商品数) ÷ 应检商品数（含送检商品与历史复用商品）。'],
  ['relevance_detection_accuracy', '相关性检测准确率', '当前仍处于拉黑状态的不相关商品数 ÷ 大模型判定不相关商品数；随人工取消/确认拉黑实时变化。'],
  ['avg_search_recall_rate', '平均召回率', 'AI 召回率检测跑出报告的 Query 的补全后搜索引擎召回率均值；未启用检测的 Query 不计入。'],
  ['token_usage', 'Token 消耗', '本批次模型调用消耗的 Token 总量。'],
  ['duration_seconds', '耗时', '本批次从开始到完成的总耗时。'],
].map(([value, label, tip]) => ({ value, label, tip }));

export const drawerMetricOptions: MetricOption[] = [
  ...overviewMetricOptions,
  ...[
    ['started_at', '开始时间', '本批次任务开始执行的时间。'],
    ['completed_at', '完成时间', '本批次任务完成或终止的时间。'],
    ['error_products', '错误商品', '数字格式为“不相关商品数/应检商品总数”（含送检商品与历史复用商品）。'],
    ['top_query_limit', 'Top Query', '系统配置的每日最多治理 Query 数，是 Top Query 取词上限。'],
    ['rapid_pv_growth_query_count', '快速增长', '命中快速增长规则并被选中进入本批次治理的 Query 数量。'],
    ['new_query_count', '新词 Query', '本批次识别出的新词 Query 数量。'],
    ['ctr_decline_query_count', 'CTR 下降 Query', 'CTR 低于基线阈值的 Query 数量。'],
    ['cvr_decline_query_count', 'CVR 下降 Query', 'CVR 低于基线阈值的 Query 数量。'],
    ['model_call_count', '模型调用次数', '本批次模型调用总次数。'],
  ].map(([value, label, tip]) => ({ value, label, tip })),
];

export const drawerMetricOptionMap = new Map(drawerMetricOptions.map((option) => [option.value, option]));
const cardMetricOptions = overviewMetricOptions.filter(({ value }) => !['token_usage', 'duration_seconds'].includes(value));
export const queryCardMetricOptions = [
  ['pv', 'PV'], ['uv', 'UV'], ['ctr', 'CTR'], ['cvr', 'CVR'],
  ['duration_seconds', '处理耗时'], ['token_usage', 'Token 消耗'],
].map(([value, label]) => ({ value, label }));
export const yesterdayMetricOptions = [
  ['pv', 'PV'], ['uv', 'UV'], ['ctr', 'CTR'], ['cvr', 'CVR'], ['atc', 'ATC'],
  ['clickUv', '点击 UV'], ['addToCartUv', '加购 UV'], ['purchaseUv', '购买 UV'],
].map(([value, label]) => ({ value, label }));
const trendMetricOptions = [
  ['uv', '搜索 UV'], ['pv', 'PV'], ['clickUv', '点击 UV'], ['addToCartUv', '加购 UV'],
  ['purchaseUv', '购买 UV'], ['ctr', 'CTR'], ['atc', 'ATC'], ['cvr', 'CVR'],
].map(([value, label]) => ({ value, label }));

const define = (
  key: PageConfigKey, groupKey: string, groupLabel: string, label: string,
  description: string, order: number, options?: MetricOption[],
  minItems?: number, maxItems?: number,
): PageConfigDefinition => ({
  key, groupKey, groupLabel, label, description,
  type: options ? 'metric_list' : 'boolean', scope: 'global',
  defaultValue: defaultPageConfigValues[key] as PageConfigValue,
  order, options, minItems, maxItems,
});

export const fallbackDefinitions: PageConfigDefinition[] = [
  define('brand_overview.show_global_metrics', 'overview_global', '数据看板', '数据看板', '控制品牌总览顶部数据看板是否展示。', 10),
  define('brand_overview.global_metrics', 'overview_global', '数据看板', '看板指标', '所选日期全部品牌合计数据；比率按合计分子与分母计算。', 11, overviewMetricOptions, 3, 6),
  define('brand_overview.card.primary_metric', 'overview_card', '品牌卡片', '主指标', '配置品牌卡片右上角主指标。', 19, cardMetricOptions, 1, 1),
  define('brand_overview.card.metrics', 'overview_card', '品牌卡片', '卡片指标', '配置品牌卡片下方指标，可增减与排序。', 20, cardMetricOptions, 2, 4),
  define('brand_overview.card.show_runtime', 'overview_card', '品牌卡片', '耗时/Token', '显示卡片底部运行数据。', 21),
  define('brand_overview.card.show_operational_data', 'overview_card', '品牌卡片', '运营数据', '控制运营数据入口。', 22),
  define('brand_overview.drawer.summary_metrics', 'overview_drawer', '运营数据', '基础数据', '可为空，最多展示 20 项。', 30, drawerMetricOptions, 0, 20),
  define('brand_overview.drawer.trend_metrics', 'overview_drawer', '运营数据', '转化率', '选择图表与表格指标。', 31, trendMetricOptions, 1, 8),
  define('brand_overview.drawer.show_chart', 'overview_drawer', '运营数据', '转化率图表', '控制趋势图表。', 32),
  define('brand_overview.drawer.show_table', 'overview_drawer', '运营数据', '转化率表单', '控制趋势表格。', 33),
  define('brand_detail.query_card.show_metrics', 'detail_list', 'Query 列表', '转化指标', '控制 Query 卡片上的统计数据。', 40),
  define('brand_detail.query_card.metrics', 'detail_list', 'Query 列表', '卡片指标', '选择 2～6 个指标。', 41, queryCardMetricOptions, 2, 6),
  define('brand_detail.show_execution_overview', 'detail_right', 'Query 详情', '执行概览', '控制执行概览板块。', 50),
  define('brand_detail.yesterday.show', 'detail_right', 'Query 详情', '昨日数据', '控制昨日数据及其数据明细入口。', 51),
  define('brand_detail.yesterday.metrics', 'detail_right', 'Query 详情', '昨日数据指标', '选择 2～6 个指标。', 52, yesterdayMetricOptions, 2, 6),
  define('brand_detail.tabs.show_governance_plans', 'detail_right', 'Query 详情', '治理方案', '控制治理方案 Tab。', 53),
  define('brand_detail.tabs.show_result_comparison', 'detail_right', 'Query 详情', '效果对比', '控制效果对比 Tab。', 54),
  define('brand_detail.tabs.show_relevance_rules', 'detail_right', 'Query 详情', '相关性规则', '控制相关性规则 Tab。', 55),
];
