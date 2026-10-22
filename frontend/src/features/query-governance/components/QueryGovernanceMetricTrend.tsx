import React from 'react';
import { DualAxes, type DualAxesConfig } from '@ant-design/charts';
import { Empty } from 'antd';
import type { TopQueryTrendPoint } from '@/shared/types';
import { trendMetricDefinitions } from './queryGovernanceTrendMetrics';
import type { TrendMetricKey } from '../constants/queryGovernanceTrend';

const chartColors = ['#2563eb', '#0f766e', '#d97706', '#7c3aed', '#dc2626', '#0891b2', '#16a34a', '#db2777'];
const denseDataThreshold = 12;

type ChartDatum = { date: string; metric: string; value: number };
type DualAxesChartConfig = DualAxesConfig;

const formatAxisValue = (value: number, rate?: boolean) => rate
  ? `${Math.round(value * 100)}%`
  : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value);

const resolveDomain = (values: number[], rate?: boolean): [number, number] => {
  if (!values.length) return [0, rate ? 1 : 0];
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  if (maxValue === minValue) {
    if (rate) return [Math.max(0, minValue - 0.01), Math.min(1, maxValue + 0.01)];
    const offset = Math.max(1, Math.abs(maxValue) * 0.1);
    return [Math.max(0, minValue - offset), maxValue + offset];
  }
  const padding = (maxValue - minValue) * 0.1;
  return [Math.max(0, minValue - padding), rate ? Math.min(1, maxValue + padding) : maxValue + padding];
};

type QueryGovernanceMetricTrendProps = {
  data?: TopQueryTrendPoint[];
  query: string;
  metrics?: TrendMetricKey[];
  legendMetrics?: TrendMetricKey[];
  onToggleMetric?: (metric: TrendMetricKey) => void;
};

const QueryGovernanceMetricTrend: React.FC<QueryGovernanceMetricTrendProps> = ({ data = [], query, metrics, legendMetrics, onToggleMetric }) => {
  const enabledMetrics = React.useMemo(() => new Set(metrics ?? trendMetricDefinitions.map((metric) => metric.key)), [metrics]);
  const availableLegendMetrics = React.useMemo(() => new Set(legendMetrics ?? trendMetricDefinitions.map((metric) => metric.key)), [legendMetrics]);
  const activeDefinitions = React.useMemo(() => trendMetricDefinitions.filter((metric) => enabledMetrics.has(metric.key)), [enabledMetrics]);
  const leftDefinitions = React.useMemo(() => activeDefinitions.filter((metric) => !metric.rate), [activeDefinitions]);
  const rightDefinitions = React.useMemo(() => activeDefinitions.filter((metric) => metric.rate), [activeDefinitions]);
  const dateLabels = React.useMemo(() => data.map((point) => point.date), [data]);
  const leftData = React.useMemo<ChartDatum[]>(() => leftDefinitions.flatMap((metric) => data.flatMap((point) => {
    const value = point[metric.key];
    return typeof value === 'number' && Number.isFinite(value) ? [{ date: point.date, metric: metric.label, value }] : [];
  })), [data, leftDefinitions]);
  const rightData = React.useMemo<ChartDatum[]>(() => rightDefinitions.flatMap((metric) => data.flatMap((point) => {
    const value = point[metric.key];
    return typeof value === 'number' && Number.isFinite(value) ? [{ date: point.date, metric: metric.label, value }] : [];
  })), [data, rightDefinitions]);
  const activeSeriesCount = new Set([...leftData, ...rightData].map((point) => point.metric)).size;

  if (!activeSeriesCount || !dateLabels.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无趋势数据" />;

  const leftDomain = resolveDomain(leftData.map((point) => point.value));
  const rightDomain = resolveDomain(rightData.map((point) => point.value), true);
  const isDense = dateLabels.length > denseDataThreshold;
  const xLabelStep = Math.max(1, Math.ceil(dateLabels.length / 8));
  const xLabelFilter = (_value: string, index: number) => index % xLabelStep === 0 || index === dateLabels.length - 1;
  const slider = isDense ? {
    x: {
      values: [0, 1] as [number, number],
      labelFormatter: (value: string | number) => String(value).slice(5),
    },
  } : undefined;
  const chartConfig: DualAxesChartConfig = {
    data: [leftData, rightData],
    xField: 'date',
    autoFit: true,
    height: 300,
    padding: [18, 52, 42, 52],
    legend: false,
    axis: {
      x: {
        title: false,
        labelAutoHide: true,
        labelAutoRotate: false,
        labelFilter: xLabelFilter,
        labelFormatter: (value: string) => value.slice(5),
      },
      y: { title: '', titleText: '', showTitle: false },
    },
    interaction: { tooltip: { shared: true, crosshairs: true } },
    children: [
      {
        type: 'line',
        data: leftData,
        xField: 'date',
        yField: 'value',
        colorField: 'metric',
        title: '',
        scale: { y: { domain: leftDomain, nice: false, title: '' } },
        style: { lineWidth: 2.5 },
        slider: leftData.length ? slider : undefined,
        axis: { y: { position: 'left', title: '', titleText: '', showTitle: false, labelFormatter: (value: string) => formatAxisValue(Number(value)) } },
      },
      {
        type: 'line',
        data: rightData,
        xField: 'date',
        yField: 'value',
        colorField: 'metric',
        title: '',
        scale: { y: { domain: rightDomain, nice: false, title: '' } },
        style: { lineWidth: 2.5 },
        slider: leftData.length ? undefined : slider,
        axis: { y: { position: 'right', title: '', titleText: '', showTitle: false, labelFormatter: (value: string) => formatAxisValue(Number(value), true) } },
      },
    ],
  };

  return (
    <div role="region" aria-label={`${query} ${activeSeriesCount}项指标数据变化`} className="ops-trend-chart">
      <div className="ops-trend-chart__viewport">
        <div className="ops-trend-chart__canvas" role="img" aria-label={`${query} 趋势折线图`}>
          <DualAxes {...chartConfig} />
        </div>
      </div>
      <div className="ops-trend-chart__legend">
        {trendMetricDefinitions.filter((metric) => availableLegendMetrics.has(metric.key)).map((metric) => {
          const isVisible = enabledMetrics.has(metric.key) && [...leftData, ...rightData].some((point) => point.metric === metric.label);
          return <button key={metric.key} type="button" className={`ops-trend-chart__legend-item${isVisible ? '' : ' ops-trend-chart__legend-item--hidden'}`} aria-label={`切换 ${metric.label}`} aria-pressed={isVisible} onClick={() => onToggleMetric?.(metric.key)}><span data-testid={`trend-legend-swatch-${metric.key}`} className={`ops-trend-chart__legend-swatch${isVisible ? '' : ' ops-trend-chart__legend-swatch--hidden'}`} style={{ background: chartColors[trendMetricDefinitions.indexOf(metric) % chartColors.length] }} />{metric.label}</button>;
        })}
      </div>
    </div>
  );
};

export default QueryGovernanceMetricTrend;
