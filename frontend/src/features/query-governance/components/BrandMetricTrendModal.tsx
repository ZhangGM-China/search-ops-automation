import React from 'react';
import { Alert, DatePicker, Empty, Flex, Modal, Radio, Skeleton } from 'antd';
import { Line, type LineConfig } from '@ant-design/charts';
import dayjs, { type Dayjs } from 'dayjs';

import { dashboardApi } from '@/shared/api';
import type { TopQueryBrandMetricTrendDay } from '@/shared/types';
import { getApiErrorMessage } from '@/shared/lib/error';
import {
  METRIC_TREND_WINDOW_DAYS,
  aggregateMetricTrend,
  defaultMetricTrendRange,
  formatMetricTrendValue,
  isRatioSpec,
  metricTrendSpecs,
  trendGranularityOptions,
  trendRangeLimit,
  trendRangeUnit,
  type MetricTrendPoint,
  type MetricTrendSpec,
  type TrendGranularity,
} from './metricTrendSpec';

const { RangePicker } = DatePicker;

const BucketTrendChart: React.FC<{ points: MetricTrendPoint[]; spec: MetricTrendSpec }> = ({ points, spec }) => {
  const data = points.map((point) => ({ period: point.period, value: point.value == null ? null : isRatioSpec(spec) ? point.value * 100 : point.value }));
  const values = data.flatMap((item) => (typeof item.value === 'number' ? [item.value] : []));
  const rawMin = Math.min(0, ...values);
  const rawMax = Math.max(0, ...values);
  const span = rawMax === rawMin ? 1 : rawMax - rawMin;
  const chartConfig: LineConfig = {
    data,
    encode: { x: 'period', y: 'value' },
    xField: 'period',
    yField: 'value',
    autoFit: true,
    height: 300,
    padding: [18, 20, 38, 48],
    legend: false,
    scale: { y: { domain: [rawMin - span * 0.1, rawMax + span * 0.1], nice: false } },
    axis: {
      x: { title: false, labelAutoHide: true, labelAutoRotate: false },
      y: { title: false, labelFormatter: (value: string) => formatMetricTrendValue(spec, Number(value)) },
    },
    interaction: { tooltip: { shared: true, crosshairs: true } },
    style: { lineWidth: 2.5 },
    point: { size: 3 },
  };

  return (
    <div className="ops-chart-library ops-metric-trend-chart" role="img" aria-label="指标趋势图">
      <Line {...chartConfig} />
    </div>
  );
};

interface Props {
  open: boolean;
  brand: string;
  brandName: string;
  metricKey: string | null;
  metricLabel?: string;
  anchorDate?: string | null;
  onClose: () => void;
}

const BrandMetricTrendModal: React.FC<Props> = ({ open, brand, brandName, metricKey, metricLabel, anchorDate, onClose }) => {
  const spec = metricKey ? metricTrendSpecs[metricKey] : undefined;
  const anchor = React.useMemo(() => {
    const parsed = anchorDate ? dayjs(anchorDate) : dayjs();
    return parsed.isValid() ? parsed : dayjs();
  }, [anchorDate]);
  const anchorKey = anchor.format('YYYY-MM-DD');
  const windowStartKey = anchor.subtract(METRIC_TREND_WINDOW_DAYS - 1, 'day').format('YYYY-MM-DD');
  const [granularity, setGranularity] = React.useState<TrendGranularity>('day');
  const [rangeState, setRangeState] = React.useState<{ anchorKey: string; granularity: TrendGranularity; value: [Dayjs, Dayjs] } | null>(null);
  const [days, setDays] = React.useState<TopQueryBrandMetricTrendDay[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const windowStart = React.useMemo(() => dayjs(windowStartKey), [windowStartKey]);
  const fallbackRange = React.useMemo(() => defaultMetricTrendRange(anchor, granularity), [anchor, granularity]);
  const effectiveRange = rangeState?.anchorKey === anchorKey && rangeState.granularity === granularity
    ? rangeState.value
    : fallbackRange;

  const controllerRef = React.useRef<AbortController | null>(null);

  const load = React.useCallback(() => {
    const request = dashboardApi.getTopQueryBrandMetricTrend;
    if (typeof request !== 'function') return undefined;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.resolve(request(brand, { startDate: windowStartKey, endDate: anchorKey }, { signal: controller.signal }))
      .then((response) => {
        if (controller.signal.aborted) return;
        setDays(response?.data?.days ?? []);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(getApiErrorMessage(reason, '获取指标趋势失败'));
        setDays([]);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return controller;
  }, [brand, anchorKey, windowStartKey]);

  React.useEffect(() => {
    if (!open || !metricKey) return undefined;
    const timer = window.setTimeout(() => { controllerRef.current = load() ?? null; }, 0);
    return () => {
      window.clearTimeout(timer);
      controllerRef.current?.abort();
      controllerRef.current = null;
    };
  }, [load, open, metricKey]);

  const points = React.useMemo(
    () => (spec ? aggregateMetricTrend(days, spec, granularity, effectiveRange) : []),
    [days, spec, granularity, effectiveRange],
  );
  const hasData = points.some((point) => point.value != null);

  const handleRangeChange = (dates: null | [Dayjs | null, Dayjs | null]) => {
    if (!dates?.[0] || !dates?.[1]) {
      setRangeState(null);
      return;
    }
    const [start, end] = dates;
    const unit = trendRangeUnit[granularity];
    const limit = trendRangeLimit[granularity];
    const span = end.diff(start, unit) + 1;
    setRangeState({
      anchorKey,
      granularity,
      value: span > limit ? [end.subtract(limit - 1, unit), end] : [start, end],
    });
  };

  return (
    <Modal
      rootClassName="ops-brand-metric-trend-modal-root"
      open={open}
      title={`${brandName} · ${metricLabel || '指标'}趋势`}
      width={760}
      footer={null}
      destroyOnHidden
      onCancel={onClose}
    >
      <Flex vertical gap={16}>
        <Flex justify="space-between" align="center" gap={12} wrap="wrap" className="ops-trend-filter">
          <Radio.Group
            aria-label="时间粒度"
            options={trendGranularityOptions}
            optionType="button"
            buttonStyle="solid"
            value={granularity}
            onChange={(event) => {
              setGranularity(event.target.value);
              setRangeState(null);
            }}
          />
          <RangePicker
            aria-label="选择数据范围"
            value={effectiveRange}
            picker={granularity === 'day' ? 'date' : granularity}
            allowClear={false}
            disabledDate={(value) => Boolean(value && (value.isAfter(anchor, 'day') || value.isBefore(windowStart, 'day')))}
            onChange={handleRangeChange}
          />
        </Flex>
        {error ? <Alert type="error" showIcon message={error} /> : null}
        {loading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : hasData && spec ? (
          <BucketTrendChart points={points} spec={spec} />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无趋势数据" />
        )}
      </Flex>
    </Modal>
  );
};

export default BrandMetricTrendModal;