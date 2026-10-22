import React from 'react';
import { Alert, Button, Card, DatePicker, Empty, Flex, Skeleton, Space, Typography, message } from 'antd';
import { LeftOutlined, RightOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { dashboardApi } from '@/shared/api';
import type { QueryGovernanceOverviewBrand, QueryGovernanceStatus } from '@/shared/types';
import { formatBackendDateTime } from '@/shared/lib/backendTime';
import { getApiErrorMessage } from '@/shared/lib/error';
import { usePageConfigBoolean, usePageConfigMetrics } from '@/shared/pageConfig/context';
import { drawerMetricOptionMap, overviewMetricOptions } from '@/shared/pageConfig/definitions';
import type { DrawerSummaryMetric } from '../../components/BrandMetricDetailDrawer';
import { metricTrendKeys } from '../../components/metricTrendSpec';

const BrandMetricDetailDrawer = React.lazy(() => import('../../components/BrandMetricDetailDrawer'));
const BrandMetricTrendModal = React.lazy(() => import('../../components/BrandMetricTrendModal'));

const { Text, Title } = Typography;
const brandNames: Record<string, string> = { atlas: 'Atlas', harbor: 'Harbor', meadow: 'Meadow', summit: 'Summit', riviera: 'Riviera', lumen: 'Lumen' };
const governanceStatusLabel: Record<QueryGovernanceStatus, string> = { pending: '待治理', running: '治理中', executed: '治理完成', skipped: '治理完成', error: '治理异常' };
const governanceCopy = (brand: QueryGovernanceOverviewBrand) => {
  if (brand.reason === 'no_data') return '暂无治理记录';
  if (brand.reason === 'no_candidates') return '无符合条件的 Query';
  return governanceStatusLabel[brand.status];
};
const integerText = (value: number) => new Intl.NumberFormat('zh-CN').format(value);
const tokenText = (value: number | null | undefined) => {
  if (value == null || !Number.isFinite(value)) return '-';
  const units = ['', 'K', 'M', 'B', 'T', 'P', 'E'];
  let scaled = Math.max(0, value);
  let unitIndex = 0;
  while (scaled >= 1000 && unitIndex < units.length - 1) {
    scaled /= 1000;
    unitIndex += 1;
  }
  return `${scaled.toFixed(1)}${units[unitIndex]}`;
};
const durationText = (value: number | null | undefined) => {
  if (value == null || !Number.isFinite(value)) return '-';
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

type CardMetricKey = 'query_count' | 'no_recall_rate' | 'pass_rate' | 'ai_processed_count' | 'pending_count' | 'irrelevant_query_count' | 'preference_adjusted_query_count' | 'stock_bucket_reduction_product_count' | 'relevance_accuracy' | 'relevance_detection_accuracy' | 'avg_search_recall_rate' | 'token_usage' | 'duration_seconds';
const metricLabel: Record<CardMetricKey, string> = { query_count: 'Query', no_recall_rate: '无召回率', pass_rate: '通过率', ai_processed_count: 'AI 处理', pending_count: '待处理', irrelevant_query_count: '不相关', preference_adjusted_query_count: '重排序', stock_bucket_reduction_product_count: '断码', relevance_accuracy: '商品准确率', relevance_detection_accuracy: '相关性检测准确率', avg_search_recall_rate: '平均召回率', token_usage: 'Token', duration_seconds: '耗时' };
const metricValue = (brand: QueryGovernanceOverviewBrand, key: CardMetricKey) => {
  const telemetry = brand.telemetry;
  const value = { query_count: brand.queryCount, no_recall_rate: telemetry?.noRecallRate, pass_rate: telemetry?.passRate, ai_processed_count: telemetry?.aiProcessedCount, pending_count: telemetry?.pendingCount, irrelevant_query_count: telemetry?.irrelevantQueryCount, preference_adjusted_query_count: telemetry?.preferenceAdjustedQueryCount, stock_bucket_reduction_product_count: telemetry?.stockBucketReductionProductCount, relevance_accuracy: telemetry?.relevanceAccuracy, relevance_detection_accuracy: telemetry?.relevanceDetectionAccuracy, avg_search_recall_rate: telemetry?.avgSearchRecallRate, token_usage: telemetry?.tokenUsage, duration_seconds: brand.durationSeconds }[key];
  if (value == null) return '—';
  if (key === 'no_recall_rate' || key === 'pass_rate' || key === 'relevance_accuracy' || key === 'relevance_detection_accuracy' || key === 'avg_search_recall_rate') return `${(Number(value) * 100).toFixed(1)}%`;
  if (key === 'token_usage') return tokenText(Number(value));
  if (key === 'duration_seconds') return durationText(Number(value));
  return integerText(Number(value));
};

const totalMetricValue = (brands: QueryGovernanceOverviewBrand[], key: CardMetricKey): string => {
  const dataBrands = brands.filter((brand) => brand.queryCount > 0 && brand.reason !== 'no_data');
  if (key === 'query_count') return integerText(dataBrands.reduce((total, brand) => total + brand.queryCount, 0));
  const sum = (field: keyof NonNullable<QueryGovernanceOverviewBrand['telemetry']>, eligible = dataBrands) =>
    eligible.reduce<number>((total, brand) => total + Number(brand.telemetry?.[field] ?? 0), 0);
  const brandsWith = (...fields: (keyof NonNullable<QueryGovernanceOverviewBrand['telemetry']>)[]) =>
    dataBrands.every((brand) => fields.every((field) => typeof brand.telemetry?.[field] === 'number'));
  if (key === 'no_recall_rate' || key === 'pass_rate') {
    const numerator = key === 'no_recall_rate' ? 'noRecallCount' : 'passCount';
    const count = dataBrands.reduce((total, brand) => total + brand.queryCount, 0);
    return dataBrands.length && brandsWith(numerator) && count
      ? `${(sum(numerator) / count * 100).toFixed(1)}%` : '—';
  }
  if (key === 'relevance_accuracy') {
    const hasInspected = dataBrands.every((brand) =>
      typeof brand.telemetry?.inspectedProductCount === 'number' || typeof brand.telemetry?.detectedProductCount === 'number'
    );
    const inspected = dataBrands.reduce((total, brand) =>
      total + Number(brand.telemetry?.inspectedProductCount ?? brand.telemetry?.detectedProductCount ?? 0), 0
    );
    return dataBrands.length && brandsWith('irrelevantProductCount') && hasInspected && inspected
      ? `${((1 - sum('irrelevantProductCount') / inspected) * 100).toFixed(1)}%` : '—';
  }
  if (key === 'relevance_detection_accuracy') {
    const pairCount = sum('irrelevantProductPairCount');
    return dataBrands.length && brandsWith('blacklistedProductCount', 'irrelevantProductPairCount') && pairCount
      ? `${(sum('blacklistedProductCount') / pairCount * 100).toFixed(1)}%` : '—';
  }
  if (key === 'avg_search_recall_rate') {
    // 跨品牌按跑了召回率检测的 Query 数加权，避免直接平均品牌均值。
    const rateSum = sum('searchRecallRateSum');
    const queryCount = sum('recallRateQueryCount');
    return dataBrands.length && brandsWith('searchRecallRateSum', 'recallRateQueryCount') && queryCount
      ? `${(rateSum / queryCount * 100).toFixed(1)}%` : '—';
  }
  if (key === 'duration_seconds') {
    return dataBrands.length && dataBrands.every((brand) => typeof brand.durationSeconds === 'number')
      ? durationText(dataBrands.reduce((total, brand) => total + Number(brand.durationSeconds), 0)) : '—';
  }
  const fields: Record<Exclude<CardMetricKey, 'query_count' | 'no_recall_rate' | 'pass_rate' | 'relevance_accuracy' | 'relevance_detection_accuracy' | 'avg_search_recall_rate' | 'duration_seconds'>, keyof NonNullable<QueryGovernanceOverviewBrand['telemetry']>> = {
    ai_processed_count: 'aiProcessedCount', pending_count: 'pendingCount',
    irrelevant_query_count: 'irrelevantQueryCount', preference_adjusted_query_count: 'preferenceAdjustedQueryCount',
    stock_bucket_reduction_product_count: 'stockBucketReductionProductCount', token_usage: 'tokenUsage',
  };
  const field = fields[key];
  if (!dataBrands.length || !brandsWith(field)) return '—';
  return key === 'token_usage' ? tokenText(sum(field)) : integerText(sum(field));
};

const BrandCard: React.FC<{ brand: QueryGovernanceOverviewBrand; onOpen: () => void; onOpenData: () => void; showOperationalData: boolean; showRuntime: boolean; primaryMetric: CardMetricKey; actionMetrics: CardMetricKey[] }> = ({ brand, onOpen, onOpenData, showOperationalData, showRuntime, primaryMetric, actionMetrics }) => {
  const name = brandNames[brand.brand] ?? brand.brand;
  return (
    <div className="ops-diagnosis-brand-card" role="link" tabIndex={0} aria-label={`进入 ${name} 详情`} onClick={onOpen} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onOpen(); } }}>
      <div className="ops-diagnosis-brand-card__main">
        <Flex justify="space-between" align="flex-start" gap={12}>
          <div style={{ minWidth: 0 }}><div className="ops-diagnosis-brand-card__name">{name}</div><Flex align="center" gap={8} wrap="wrap"><Text type="secondary">{governanceCopy(brand)}</Text></Flex></div>
          <div className="ops-diagnosis-score"><span>{metricValue(brand, primaryMetric)}</span><small>{metricLabel[primaryMetric]}</small></div>
        </Flex>
      </div>
      <div className="ops-diagnosis-action-grid" style={{ gridTemplateColumns: `repeat(${actionMetrics.length}, minmax(0, 1fr))` }}>{actionMetrics.map((metric, index) => <div className={`ops-diagnosis-action ops-diagnosis-action--${index === 0 ? 'warning' : index === 1 ? 'normal' : 'fixed'}`} key={`${metric}-${index}`}><span>{metricValue(brand, metric)}</span><small>{metricLabel[metric]}</small></div>)}</div>
      {showRuntime || showOperationalData ? <Flex gap={12} align="center" justify={showOperationalData ? 'space-between' : 'flex-start'} wrap="nowrap" className="ops-diagnosis-card-actions">{showRuntime ? <Text type="secondary">耗时 {durationText(brand.durationSeconds)} / Token {tokenText(brand.telemetry?.tokenUsage)}</Text> : null}{showOperationalData ? <Button size="small" onClick={(event) => { event.stopPropagation(); onOpenData(); }}>运营数据</Button> : null}</Flex> : null}
    </div>
  );
};

const BrandOverview: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const showGlobalMetrics = usePageConfigBoolean('brand_overview.show_global_metrics');
  const showOperationalData = usePageConfigBoolean('brand_overview.card.show_operational_data');
  const showRuntime = usePageConfigBoolean('brand_overview.card.show_runtime');
  const globalMetrics = usePageConfigMetrics('brand_overview.global_metrics') as CardMetricKey[];
  const primaryMetric = (usePageConfigMetrics('brand_overview.card.primary_metric')[0] ?? 'query_count') as CardMetricKey;
  const actionMetrics = usePageConfigMetrics('brand_overview.card.metrics') as CardMetricKey[];
  const summaryMetrics = usePageConfigMetrics('brand_overview.drawer.summary_metrics');
  const showDrawerChart = usePageConfigBoolean('brand_overview.drawer.show_chart');
  const showDrawerTable = usePageConfigBoolean('brand_overview.drawer.show_table');
  const drawerTrendMetrics = usePageConfigMetrics('brand_overview.drawer.trend_metrics') as import('../../constants/queryGovernanceTrend').TrendMetricKey[];
  const todayDate = dayjs().format('YYYY-MM-DD');
  const selectedDate = searchParams.get('date');
  const displayDate = selectedDate || todayDate;
  const [detailBrand, setDetailBrand] = React.useState<string | null>(null);
  const [trendMetric, setTrendMetric] = React.useState<DrawerSummaryMetric | null>(null);
  const [detailData, setDetailData] = React.useState<Awaited<ReturnType<typeof dashboardApi.getTopQueryBrandSummary>>['data'] & { trend?: Awaited<ReturnType<typeof dashboardApi.getTopQueryBrandTrend>>['data']['trend'] } | null>(null);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const detailRequestRef = React.useRef<AbortController | null>(null);
  const [result, setResult] = React.useState<{ key: string; brands: QueryGovernanceOverviewBrand[]; error: string | null } | null>(null);
  const requestKey = selectedDate || 'latest';
  const loading = result?.key !== requestKey;
  React.useEffect(() => {
    let mounted = true;
    const controller = new AbortController();
    dashboardApi.getTopQueryOverview({ date: displayDate }, { signal: controller.signal }).then((response) => { if (mounted) setResult({ key: requestKey, brands: response.data.brands, error: null }); }).catch((reason: unknown) => {
      const errorRecord = typeof reason === 'object' && reason !== null ? reason as { code?: unknown; name?: unknown } : {};
      if (!mounted || controller.signal.aborted || errorRecord.code === 'ERR_CANCELED' || errorRecord.name === 'CanceledError') return;
      setResult({ key: requestKey, brands: [], error: getApiErrorMessage(reason, '获取 Query 治理品牌总览失败') });
    });
    return () => { mounted = false; controller.abort(); };
  }, [displayDate, requestKey]);
  const switchDate = (date: string) => setSearchParams(date === todayDate ? {} : { date }, { replace: true });
  const shiftDate = (days: number) => { const next = dayjs(displayDate).add(days, 'day').format('YYYY-MM-DD'); if (next <= todayDate) switchDate(next); };
  const openData = (brand: QueryGovernanceOverviewBrand) => {
    detailRequestRef.current?.abort();
    const controller = new AbortController();
    detailRequestRef.current = controller;
    setDetailBrand(brand.brand);
    setDetailData(null);
    setDetailLoading(true);
    const params = { date: displayDate };
    const trendRequest = typeof dashboardApi.getTopQueryBrandTrend === 'function'
      ? Promise.resolve(dashboardApi.getTopQueryBrandTrend(brand.brand, params, { signal: controller.signal }))
        .then((response) => response?.data?.trend ?? [])
        .catch(() => [])
      : Promise.resolve([]);
    Promise.all([dashboardApi.getTopQueryBrandSummary(brand.brand, params, { signal: controller.signal }), trendRequest])
      .then(([response, trend]) => {
        if (controller.signal.aborted) return;
        setDetailData({ ...response.data, trend });
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        message.error(getApiErrorMessage(reason, '获取品牌数据详情失败'));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
  };
  React.useEffect(() => () => detailRequestRef.current?.abort(), []);
  const summaryMetricItems: DrawerSummaryMetric[] | undefined = detailData && detailBrand ? (() => {
    const brand = result?.brands.find((item) => item.brand === detailBrand);
    const telemetry = detailData.batch?.telemetry;
    const summaryBrand: QueryGovernanceOverviewBrand | null = brand ? {
      ...brand,
      queryCount: detailData.summary?.queryCount ?? brand.queryCount,
      durationSeconds: telemetry?.wallClockSeconds ?? brand.durationSeconds,
      telemetry: telemetry ?? brand.telemetry,
    } : null;
    const inspectedProductCount = telemetry?.inspectedProductCount ?? (
      telemetry?.detectedProductCount != null
        ? telemetry.detectedProductCount + (telemetry.reusedProductCount ?? 0)
        : null
    );
    const additional: Record<string, React.ReactNode> = {
      started_at: detailData.batch?.startedAt ? formatBackendDateTime(detailData.batch.startedAt, 'HH:mm:ss') : '—',
      completed_at: detailData.batch?.completedAt ? formatBackendDateTime(detailData.batch.completedAt, 'HH:mm:ss') : '—',
      error_products: telemetry?.irrelevantProductCount == null || inspectedProductCount == null ? '—' : `${telemetry.irrelevantProductCount}/${inspectedProductCount}`,
      top_query_limit: telemetry?.topQueryLimit ?? '—',
      rapid_pv_growth_query_count: telemetry?.rapidPvGrowthQueryCount ?? '—',
      new_query_count: telemetry?.newQueryCount ?? '—',
      ctr_decline_query_count: telemetry?.ctrDeclineQueryCount ?? '—',
      cvr_decline_query_count: telemetry?.cvrDeclineQueryCount ?? '—',
      model_call_count: telemetry?.modelCallCount ?? '—',
    };
    return summaryMetrics.map((key) => {
      const option = drawerMetricOptionMap.get(key);
      return {
        key,
        label: option?.label ?? metricLabel[key as CardMetricKey] ?? key,
        tip: option?.tip,
        clickable: metricTrendKeys.includes(key),
        value: summaryBrand && key in metricLabel ? metricValue(summaryBrand, key as CardMetricKey) : additional[key] ?? '—',
      };
    });
  })() : undefined;
  const configuredSummaryMetrics = summaryMetrics.length ? summaryMetricItems : undefined;
  return <div className="ops-page">
    {loading ? <Card size="small"><Skeleton active paragraph={{ rows: 6 }} /></Card> : null}
    {!loading && result?.error ? <Alert type="error" showIcon message={result.error} /> : null}
    {!loading && !result?.error && result?.brands.length === 0 ? <Card size="small"><Empty description="暂无 Query 治理品牌数据" /></Card> : null}
    {!loading && !result?.error && result?.brands.length ? <Flex vertical gap={16}><section className="ops-diagnosis-section"><Flex justify="space-between" align="center" gap={12} wrap="wrap" style={{ marginBottom: 12 }}><Title level={4} style={{ margin: 0 }}>品牌总览</Title><Space.Compact className="ops-brand-date-switcher"><Button icon={<LeftOutlined />} aria-label="前一天" onClick={() => shiftDate(-1)} /><DatePicker allowClear={false} inputReadOnly value={dayjs(displayDate)} disabledDate={(value) => Boolean(value && value.format('YYYY-MM-DD') > todayDate)} onChange={(value) => switchDate(value?.format('YYYY-MM-DD') || todayDate)} aria-label="选择监测日期" /><Button icon={<RightOutlined />} aria-label="后一天" disabled={displayDate >= todayDate} onClick={() => shiftDate(1)} /></Space.Compact></Flex>{showGlobalMetrics ? <div className="ops-diagnosis-metric-grid" aria-label="全局数据" style={{ marginBottom: 16, gridTemplateColumns: `repeat(${globalMetrics.length}, minmax(0, 1fr))` }}>{globalMetrics.map((key) => <div className="ops-diagnosis-metric" key={key}><span className="ops-diagnosis-metric__label">{overviewMetricOptions.find((item) => item.value === key)?.label ?? metricLabel[key]}</span><strong>{totalMetricValue(result.brands, key)}</strong></div>)}</div> : null}<div className="ops-diagnosis-brand-grid">{result.brands.map((brand) => <BrandCard key={brand.brand} brand={brand} primaryMetric={primaryMetric} actionMetrics={actionMetrics} showRuntime={showRuntime} showOperationalData={showOperationalData} onOpen={() => navigate(`/ops-dashboard/${encodeURIComponent(brand.brand)}${displayDate === todayDate ? '' : `?date=${encodeURIComponent(displayDate)}`}`)} onOpenData={() => openData(brand)} />)}</div></section></Flex> : null}
    {detailBrand ? (
      <React.Suspense fallback={<Card size="small"><Skeleton active paragraph={{ rows: 6 }} /></Card>}>
        <BrandMetricDetailDrawer
          brandName={brandNames[detailBrand] ?? detailBrand}
          open
          onClose={() => { setDetailBrand(null); setTrendMetric(null); }}
          trend={detailData?.trend ?? []}
          stats={detailData?.stats ?? null}
          loading={detailLoading}
          anchorDate={displayDate}
          summaryMetrics={configuredSummaryMetrics}
          onMetricClick={setTrendMetric}
          showChart={showDrawerChart}
          showTable={showDrawerTable}
          availableMetrics={drawerTrendMetrics}
          lockMetrics
        />
      </React.Suspense>
    ) : null}
    {detailBrand && trendMetric ? (
      <React.Suspense fallback={null}>
        <BrandMetricTrendModal
          open
          brand={detailBrand}
          brandName={brandNames[detailBrand] ?? detailBrand}
          metricKey={trendMetric.key}
          metricLabel={trendMetric.label}
          anchorDate={displayDate}
          onClose={() => setTrendMetric(null)}
        />
      </React.Suspense>
    ) : null}
  </div>;
};

export default BrandOverview;
