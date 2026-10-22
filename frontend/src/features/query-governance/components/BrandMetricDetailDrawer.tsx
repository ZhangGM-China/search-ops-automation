import React from 'react';
import { DatePicker, Drawer, Flex, Radio, Skeleton, Table, Tooltip } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import dayjs, { type Dayjs } from 'dayjs';
import type { ColumnsType } from 'antd/es/table';
import type { TopQueryBrandStats, TopQueryTrendPoint } from '@/shared/types';
import { trendMetricDefinitions, type TrendMetricKey } from '../constants/queryGovernanceTrend';

const QueryGovernanceMetricTrend = React.lazy(() => import('./QueryGovernanceMetricTrend'));

const { RangePicker } = DatePicker;

const numberText = (value?: number | null) => new Intl.NumberFormat('zh-CN').format(value ?? 0);
const percent = (value?: number | null) => `${(((value ?? 0) <= 1 ? (value ?? 0) * 100 : (value ?? 0))).toFixed(2)}%`;

type TrendGranularity = 'day' | 'week' | 'month' | 'year';

const trendGranularityOptions: Array<{ label: string; value: TrendGranularity }> = [
  { label: '按天', value: 'day' },
  { label: '按周', value: 'week' },
  { label: '按月', value: 'month' },
  { label: '按年', value: 'year' },
];

const trendRangeLimit: Record<TrendGranularity, number> = {
  day: 30,
  week: 52,
  month: 36,
  year: 10,
};

const trendRangeUnit: Record<TrendGranularity, 'day' | 'week' | 'month' | 'year'> = {
  day: 'day',
  week: 'week',
  month: 'month',
  year: 'year',
};

const parseTrendDate = (value: string) => {
  if (/^\d{2}-\d{2}$/.test(value)) {
    return dayjs(`2026-${value}`);
  }
  const parsed = dayjs(value);
  if (parsed.isValid()) return parsed;
  return dayjs(`2026-${value}`);
};

const parseAnchorDate = (value?: string | null) => {
  const parsed = value ? dayjs(value) : dayjs();
  return parsed.isValid() ? parsed : dayjs();
};

const defaultTrendRange = (anchorDate: Dayjs, granularity: TrendGranularity, points: TopQueryTrendPoint[]): [Dayjs, Dayjs] => {
  const latestPointDate = points
    .map((point) => parseTrendDate(point.date))
    .filter((date) => date.isValid())
    .reduce<Dayjs | null>((latest, date) => (!latest || date.isAfter(latest, 'day') ? date : latest), null);
  const endDate = latestPointDate && latestPointDate.isAfter(anchorDate, 'day') ? latestPointDate : anchorDate;
  if (granularity === 'day') return [anchorDate.subtract(29, 'day'), endDate];
  if (granularity === 'week') return [anchorDate.startOf('week').subtract(51, 'week'), endDate.endOf('week')];
  if (granularity === 'month') return [anchorDate.startOf('month').subtract(35, 'month'), endDate.endOf('month')];
  return [anchorDate.startOf('year').subtract(9, 'year'), endDate.endOf('year')];
};

const inRange = (date: Dayjs, range: [Dayjs, Dayjs] | null) => {
  if (!range) return true;
  return !date.isBefore(range[0], 'day') && !date.isAfter(range[1], 'day');
};

const trendBucketKey = (date: Dayjs, granularity: TrendGranularity) => {
  if (granularity === 'day') return date.format('YYYY-MM-DD');
  if (granularity === 'week') return `${date.startOf('week').format('YYYY-MM-DD')} 周`;
  if (granularity === 'month') return date.format('YYYY-MM');
  return date.format('YYYY');
};

const aggregateTrendData = (
  points: TopQueryTrendPoint[],
  granularity: TrendGranularity,
  range: [Dayjs, Dayjs] | null,
): TopQueryTrendPoint[] => {
  const buckets = new Map<string, { count: number; point: TopQueryTrendPoint }>();
  points.forEach((point) => {
    const date = parseTrendDate(point.date);
    if (!date.isValid() || !inRange(date, range)) return;
    const key = trendBucketKey(date, granularity);
    const existing = buckets.get(key);
    const nextPoint = existing?.point ?? { ...point, date: key };
    const count = (existing?.count ?? 0) + 1;
    nextPoint.uv = (nextPoint.uv ?? 0) + (existing ? point.uv ?? 0 : 0);
    nextPoint.clickUv = (nextPoint.clickUv ?? 0) + (existing ? point.clickUv ?? 0 : 0);
    nextPoint.addToCartUv = (nextPoint.addToCartUv ?? 0) + (existing ? point.addToCartUv ?? 0 : 0);
    nextPoint.purchaseUv = (nextPoint.purchaseUv ?? 0) + (existing ? point.purchaseUv ?? 0 : 0);
    nextPoint.ctr = ((existing?.point.ctr ?? 0) * (count - 1) + (point.ctr ?? 0)) / count;
    nextPoint.atc = ((existing?.point.atc ?? 0) * (count - 1) + (point.atc ?? 0)) / count;
    nextPoint.cvr = ((existing?.point.cvr ?? 0) * (count - 1) + (point.cvr ?? 0)) / count;
    buckets.set(key, { count, point: nextPoint });
  });
  return Array.from(buckets.values()).map((item) => item.point);
};

export interface DrawerSummaryMetric {
  key: string;
  label: string;
  value: React.ReactNode;
  tip?: string;
  /** Clickable metrics open the per-metric trend modal; tips-only metrics leave this unset. */
  clickable?: boolean;
}

interface Props {
  brandName: string;
  open: boolean;
  onClose: () => void;
  trend: TopQueryTrendPoint[];
  stats?: TopQueryBrandStats | null;
  loading?: boolean;
  anchorDate?: string | null;
  chartQueryLabel?: string;
  drawerTitle?: string;
  availableMetrics?: TrendMetricKey[];
  lockMetrics?: boolean;
  summaryMetrics?: DrawerSummaryMetric[];
  onMetricClick?: (metric: DrawerSummaryMetric) => void;
  showChart?: boolean;
  showTable?: boolean;
}

const BrandMetricDetailDrawer: React.FC<Props> = ({
  brandName,
  open,
  onClose,
  trend,
  stats = null,
  loading = false,
  anchorDate,
  chartQueryLabel,
  drawerTitle = '数据明细',
  availableMetrics,
  lockMetrics = false,
  summaryMetrics,
  onMetricClick,
  showChart = true,
  showTable = true,
}) => {
  const [trendGranularity, setTrendGranularity] = React.useState<TrendGranularity>('day');
  const metricOptions = React.useMemo(
    () => trendMetricDefinitions.filter((metric) => !availableMetrics || availableMetrics.includes(metric.key)),
    [availableMetrics],
  );
  const [selectedMetrics, setSelectedMetrics] = React.useState<TrendMetricKey[]>(metricOptions.map((metric) => metric.key));
  const [trendRange, setTrendRange] = React.useState<{
    anchorKey: string;
    granularity: TrendGranularity;
    value: [Dayjs, Dayjs];
  } | null>(null);
  const parsedAnchorDate = React.useMemo(() => parseAnchorDate(anchorDate), [anchorDate]);
  const anchorKey = parsedAnchorDate.format('YYYY-MM-DD');
  const fallbackTrendPoint = React.useMemo<TopQueryTrendPoint[]>(() => {
    if (!stats) return [];
    return [{
      date: parsedAnchorDate.format('YYYY-MM-DD'),
      uv: stats.brandUv ?? 0,
      pv: stats.brandPv ?? 0,
      clickUv: stats.clickUv ?? 0,
      addToCartUv: stats.addToCartUv ?? 0,
      purchaseUv: stats.purchaseUv ?? 0,
      ctr: stats.ctr ?? 0,
      atc: stats.atc ?? 0,
      cvr: stats.cvr ?? 0,
    }];
  }, [parsedAnchorDate, stats]);
  const trendSource = trend.length ? trend : fallbackTrendPoint;
  const fallbackTrendRange = React.useMemo(
    () => defaultTrendRange(parsedAnchorDate, trendGranularity, trendSource),
    [parsedAnchorDate, trendGranularity, trendSource],
  );
  const effectiveTrendRange = trendRange?.anchorKey === anchorKey && trendRange.granularity === trendGranularity
    ? trendRange.value
    : fallbackTrendRange;
  const trendData = React.useMemo(
    () => aggregateTrendData(trendSource, trendGranularity, effectiveTrendRange),
    [trendSource, trendGranularity, effectiveTrendRange],
  );
  const effectiveSelectedMetrics = React.useMemo(() => {
    const optionKeys = metricOptions.map((metric) => metric.key);
    if (lockMetrics) return optionKeys;
    const filtered = selectedMetrics.filter((metric) => optionKeys.includes(metric));
    return filtered.length ? filtered : optionKeys;
  }, [lockMetrics, metricOptions, selectedMetrics]);

  const handleToggleMetric = React.useCallback((metric: TrendMetricKey) => {
    if (lockMetrics) return;
    setSelectedMetrics((current) => {
      if (current.includes(metric)) {
        return current.length > 1 ? current.filter((item) => item !== metric) : current;
      }
      return [...current, metric];
    });
  }, [lockMetrics]);

  const trendColumns = React.useMemo<ColumnsType<TopQueryTrendPoint>>(() => {
    const base: ColumnsType<TopQueryTrendPoint> = [
      { title: '日期', dataIndex: 'date', key: 'date', width: 112, fixed: 'left' },
    ];
    const metricColumnMap: Record<TrendMetricKey, ColumnsType<TopQueryTrendPoint>[number]> = {
      uv: { title: '搜索UV', dataIndex: 'uv', key: 'uv', width: 112, align: 'right', render: (value?: number) => numberText(value) },
      pv: { title: 'PV', dataIndex: 'pv', key: 'pv', width: 112, align: 'right', render: (value?: number) => numberText(value) },
      clickUv: { title: '点击UV', dataIndex: 'clickUv', key: 'clickUv', width: 112, align: 'right', render: (value?: number) => numberText(value) },
      addToCartUv: { title: '加购UV', dataIndex: 'addToCartUv', key: 'addToCartUv', width: 112, align: 'right', render: (value?: number) => numberText(value) },
      purchaseUv: { title: '购买UV', dataIndex: 'purchaseUv', key: 'purchaseUv', width: 112, align: 'right', render: (value?: number) => numberText(value) },
      ctr: { title: 'CTR', dataIndex: 'ctr', key: 'ctr', width: 112, align: 'right', render: (value?: number) => percent(value) },
      atc: { title: 'ATC', dataIndex: 'atc', key: 'atc', width: 112, align: 'right', render: (value?: number) => percent(value) },
      cvr: { title: 'CVR', dataIndex: 'cvr', key: 'cvr', width: 112, align: 'right', render: (value?: number) => percent(value) },
    };
    return [...base, ...effectiveSelectedMetrics.map((key) => metricColumnMap[key])];
  }, [effectiveSelectedMetrics]);

  const handleTrendRangeChange = (dates: null | [Dayjs | null, Dayjs | null]) => {
    if (!dates?.[0] || !dates?.[1]) {
      setTrendRange(null);
      return;
    }
    const [start, end] = dates;
    const unit = trendRangeUnit[trendGranularity];
    const limit = trendRangeLimit[trendGranularity];
    const span = end.diff(start, unit) + 1;
    setTrendRange({
      anchorKey,
      granularity: trendGranularity,
      value: span > limit ? [end.subtract(limit - 1, unit), end] : [start, end],
    });
  };

  return (
    <Drawer rootClassName="ops-brand-metric-drawer-root" className="ops-brand-metric-drawer" title={drawerTitle} open={open} onClose={onClose} size="min(92vw, 1180px)" destroyOnHidden>
      {summaryMetrics?.length ? (
        <div className="ops-diagnosis-metric-grid" style={{ marginBottom: 20 }}>
          {summaryMetrics.map((item) => {
            const body = (
              <>
                <span className="ops-diagnosis-metric__label">
                  <span>{item.label}</span>
                  {item.tip ? <Tooltip title={item.tip}><InfoCircleOutlined aria-label={`${item.label}说明`} className="ops-tooltip-icon" /></Tooltip> : null}
                </span>
                <strong>{item.value ?? '-'}</strong>
              </>
            );
            return item.clickable && onMetricClick ? (
              <button
                type="button"
                className="ops-diagnosis-metric ops-metric-card ops-metric-card--clickable"
                key={item.key}
                aria-label={`查看${item.label}趋势`}
                onClick={() => onMetricClick(item)}
              >
                {body}
              </button>
            ) : (
              <div className="ops-diagnosis-metric" key={item.key}>{body}</div>
            );
          })}
        </div>
      ) : null}
      <Flex vertical gap={16}>
        {showChart || showTable ? <Flex justify="space-between" align="center" gap={12} wrap="wrap" className="ops-trend-filter">
          <Radio.Group
            aria-label="时间粒度"
            options={trendGranularityOptions}
            optionType="button"
            buttonStyle="solid"
            value={trendGranularity}
            onChange={(event) => {
              setTrendGranularity(event.target.value);
              setTrendRange(null);
            }}
          />
          <RangePicker
            aria-label="选择数据范围"
            value={effectiveTrendRange}
            picker={trendGranularity === 'day' ? 'date' : trendGranularity}
            allowClear={false}
            onChange={handleTrendRangeChange}
          />
        </Flex> : null}
        {loading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : (
          <>
            {showChart ? <React.Suspense fallback={<Skeleton active paragraph={{ rows: 8 }} />}>
              <QueryGovernanceMetricTrend
                data={trendData}
                query={chartQueryLabel || `${brandName} 品牌整体`}
                metrics={effectiveSelectedMetrics}
                legendMetrics={metricOptions.map((metric) => metric.key)}
                onToggleMetric={handleToggleMetric}
              />
            </React.Suspense> : null}
            {showTable ? <div className="ops-trend-table">
              <Table
                size="small"
                rowKey="date"
                columns={trendColumns}
                dataSource={trendData}
                pagination={false}
                sticky
                scroll={{ x: 'max-content', y: 420 }}
              />
            </div> : null}
          </>
        )}
      </Flex>
    </Drawer>
  );
};

export default BrandMetricDetailDrawer;
