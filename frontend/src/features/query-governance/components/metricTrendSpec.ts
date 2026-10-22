import dayjs, { type Dayjs } from 'dayjs';

import type { TopQueryBrandMetricTrendDay } from '@/shared/types';

export type TrendGranularity = 'day' | 'week' | 'month' | 'year';

export const trendGranularityOptions: Array<{ label: string; value: TrendGranularity }> = [
  { label: '按天', value: 'day' },
  { label: '按周', value: 'week' },
  { label: '按月', value: 'month' },
  { label: '按年', value: 'year' },
];

/** Server-side window limit for `/metric-trend`: at most 180 days per request. */
export const METRIC_TREND_WINDOW_DAYS = 180;

export const trendRangeLimit: Record<TrendGranularity, number> = { day: 180, week: 26, month: 6, year: 2 };
export const trendRangeUnit: Record<TrendGranularity, 'day' | 'week' | 'month' | 'year'> = { day: 'day', week: 'week', month: 'month', year: 'year' };

type MetricTrendUnit = 'integer' | 'token' | 'duration';

export type MetricTrendSpec =
  | { kind: 'count'; field: string; unit: MetricTrendUnit }
  | { kind: 'ratio'; numerator: string; denominator: string }
  | { kind: 'inverseRatio'; base: string; subtract: string };

/**
 * Every drawer metric that has a persisted daily series. Metrics missing here
 * (start/completion timestamps, "错误商品") stay tips-only and are not clickable.
 */
export const metricTrendSpecs: Record<string, MetricTrendSpec> = {
  query_count: { kind: 'count', field: 'query_count', unit: 'integer' },
  pass_rate: { kind: 'ratio', numerator: 'pass_count', denominator: 'query_count' },
  no_recall_rate: { kind: 'ratio', numerator: 'no_recall_count', denominator: 'query_count' },
  ai_processed_count: { kind: 'count', field: 'ai_processed_count', unit: 'integer' },
  pending_count: { kind: 'count', field: 'pending_count', unit: 'integer' },
  irrelevant_query_count: { kind: 'count', field: 'irrelevant_query_count', unit: 'integer' },
  preference_adjusted_query_count: { kind: 'count', field: 'preference_adjusted_query_count', unit: 'integer' },
  stock_bucket_reduction_product_count: { kind: 'count', field: 'stock_bucket_reduction_product_count', unit: 'integer' },
  relevance_accuracy: { kind: 'inverseRatio', base: 'inspected_product_count', subtract: 'irrelevant_product_count' },
  relevance_detection_accuracy: { kind: 'ratio', numerator: 'blacklisted_product_count', denominator: 'irrelevant_product_pair_count' },
  avg_search_recall_rate: { kind: 'ratio', numerator: 'search_recall_rate_sum', denominator: 'recall_rate_query_count' },
  token_usage: { kind: 'count', field: 'token_usage', unit: 'token' },
  duration_seconds: { kind: 'count', field: 'duration_seconds', unit: 'duration' },
  top_query_limit: { kind: 'count', field: 'top_query_limit', unit: 'integer' },
  rapid_pv_growth_query_count: { kind: 'count', field: 'rapid_pv_growth_query_count', unit: 'integer' },
  new_query_count: { kind: 'count', field: 'new_query_count', unit: 'integer' },
  ctr_decline_query_count: { kind: 'count', field: 'ctr_decline_query_count', unit: 'integer' },
  cvr_decline_query_count: { kind: 'count', field: 'cvr_decline_query_count', unit: 'integer' },
  model_call_count: { kind: 'count', field: 'model_call_count', unit: 'integer' },
};

export const metricTrendKeys = Object.keys(metricTrendSpecs);

export interface MetricTrendPoint {
  period: string;
  value: number | null;
}

export const isRatioSpec = (spec: MetricTrendSpec) => spec.kind !== 'count';

const numberField = (metrics: Record<string, number | null>, field: string) => {
  const value = metrics[field];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
};

const specTotals = (spec: MetricTrendSpec, metrics: Record<string, number | null>): { numerator: number; denominator: number } | null => {
  if (spec.kind === 'count') {
    const value = numberField(metrics, spec.field);
    return value == null ? null : { numerator: value, denominator: 1 };
  }
  if (spec.kind === 'ratio') {
    const numerator = numberField(metrics, spec.numerator);
    const denominator = numberField(metrics, spec.denominator);
    if (numerator == null || denominator == null || denominator <= 0) return null;
    return { numerator, denominator };
  }
  const base = numberField(metrics, spec.base) ?? (spec.base === 'inspected_product_count' ? numberField(metrics, 'detected_product_count') : null);
  const subtract = numberField(metrics, spec.subtract);
  if (base == null || subtract == null || base <= 0) return null;
  return { numerator: Math.max(0, base - subtract), denominator: base };
};

const bucketKey = (date: Dayjs, granularity: TrendGranularity) => {
  if (granularity === 'day') return date.format('YYYY-MM-DD');
  if (granularity === 'week') return `${date.startOf('week').format('YYYY-MM-DD')} 周`;
  if (granularity === 'month') return date.format('YYYY-MM');
  return date.format('YYYY');
};

const inRange = (date: Dayjs, range: [Dayjs, Dayjs] | null) => {
  if (!range) return true;
  return !date.isBefore(range[0], 'day') && !date.isAfter(range[1], 'day');
};

/**
 * Bucket the daily snapshots into the requested granularity. Counts are summed,
 * ratios are rebuilt from their numerator/denominator sums, and a bucket that
 * only contains missing (null) snapshots stays a gap instead of a fake zero.
 */
export const aggregateMetricTrend = (
  days: TopQueryBrandMetricTrendDay[],
  spec: MetricTrendSpec,
  granularity: TrendGranularity,
  range: [Dayjs, Dayjs] | null,
): MetricTrendPoint[] => {
  const buckets = new Map<string, { numerator: number; denominator: number; hasData: boolean }>();
  days.forEach((day) => {
    const date = dayjs(day.date);
    if (!date.isValid() || !inRange(date, range)) return;
    const key = bucketKey(date, granularity);
    const bucket = buckets.get(key) ?? { numerator: 0, denominator: 0, hasData: false };
    const metrics = day.metrics;
    const totals = metrics ? specTotals(spec, metrics) : null;
    if (totals) {
      bucket.numerator += totals.numerator;
      bucket.denominator += totals.denominator;
      bucket.hasData = true;
    }
    buckets.set(key, bucket);
  });
  return Array.from(buckets.entries()).map(([period, bucket]) => {
    if (!bucket.hasData) return { period, value: null };
    if (spec.kind === 'count') return { period, value: bucket.numerator };
    return { period, value: bucket.denominator > 0 ? bucket.numerator / bucket.denominator : null };
  });
};

export const defaultMetricTrendRange = (anchor: Dayjs, granularity: TrendGranularity): [Dayjs, Dayjs] => {
  const windowStart = anchor.subtract(METRIC_TREND_WINDOW_DAYS - 1, 'day');
  if (granularity === 'day') return [anchor.subtract(6, 'day'), anchor];
  if (granularity === 'week') return [anchor.startOf('week').subtract(3, 'week'), anchor];
  return [windowStart, anchor];
};

const integerText = (value: number) => new Intl.NumberFormat('zh-CN').format(Math.round(value));
const tokenText = (value: number) => {
  const units = ['', 'K', 'M', 'B', 'T', 'P', 'E'];
  let scaled = Math.max(0, value);
  let unitIndex = 0;
  while (scaled >= 1000 && unitIndex < units.length - 1) {
    scaled /= 1000;
    unitIndex += 1;
  }
  return `${scaled.toFixed(1)}${units[unitIndex]}`;
};
const durationText = (value: number) => {
  let remaining = Math.round(Math.max(0, value));
  const hours = Math.floor(remaining / 3600);
  remaining -= hours * 3600;
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining - minutes * 60;
  const parts: string[] = [];
  if (hours) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  if (seconds || parts.length === 0) parts.push(`${seconds}s`);
  return parts.join(' ');
};

export const formatMetricTrendValue = (spec: MetricTrendSpec, value: number) => {
  if (isRatioSpec(spec)) return `${value.toFixed(1)}%`;
  if (spec.unit === 'token') return tokenText(value);
  if (spec.unit === 'duration') return durationText(value);
  return integerText(value);
};