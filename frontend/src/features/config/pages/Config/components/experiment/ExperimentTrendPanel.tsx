import React from 'react';
import { Line, type LineConfig } from '@ant-design/charts';
import { Card, Empty } from 'antd';
import type { TopQueryExperimentTrendData } from '@/shared/types';

interface Props { trend: TopQueryExperimentTrendData; qualityPassed?: boolean }

type ExperimentChartDatum = { date: string; metric: string; value: number | null };
type LineChartConfig = LineConfig;

const metricDefinitions = [
  { key: 'ctrNetChange' as const, label: 'CTR 净变化', color: '#2563eb' },
  { key: 'cvrNetChange' as const, label: 'CVR 净变化', color: '#0f766e' },
];

const ExperimentTrendChart: React.FC<{ dates: string[]; data: ExperimentChartDatum[] }> = ({ dates, data }) => {
  const values = data.flatMap((item) => typeof item.value === 'number' ? [item.value] : []);
  const rawMin = Math.min(0, ...values);
  const rawMax = Math.max(0, ...values);
  const span = rawMax === rawMin ? 1 : rawMax - rawMin;
  const xLabelStep = Math.max(1, Math.ceil(dates.length / 8));
  const chartConfig: LineChartConfig = {
    data,
    encode: { x: 'date', y: 'value', color: 'metric' },
    xField: 'date',
    yField: 'value',
    colorField: 'metric',
    autoFit: true,
    height: 240,
    padding: [18, 20, 38, 48],
    legend: false,
    scale: {
      x: { domain: dates },
      y: { domain: [rawMin - span * 0.1, rawMax + span * 0.1], nice: false },
      color: { domain: metricDefinitions.map((metric) => metric.label), range: metricDefinitions.map((metric) => metric.color) },
    },
    axis: {
      x: { title: false, labelAutoHide: true, labelAutoRotate: false, labelFilter: (_value: string, index: number) => index % xLabelStep === 0 || index === dates.length - 1, labelFormatter: (value: string) => value.slice(5) },
      y: { title: false, labelFormatter: (value: string) => `${Number(value).toFixed(2)}%` },
    },
    interaction: { tooltip: { shared: true, crosshairs: true } },
    annotations: [{ type: 'lineY', encode: { y: 0 }, style: { lineDash: [6, 5], stroke: '#94a3b8' } }],
    line: dates.length > 12 ? { slider: { x: { values: [0, 1], labelFormatter: (value: string | number) => String(value).slice(5) } } } : undefined,
    style: { lineWidth: 2.5 },
    point: { size: 3 },
  };

  return (
    <div className="ops-chart-library experiment-chart" role="img" aria-label="实验效果趋势图">
      <Line {...chartConfig} />
      <div className="experiment-chart__legend" aria-label="图例">
        {metricDefinitions.map((metric) => <span key={metric.key}><i aria-hidden="true" style={{ background: metric.color }} />{metric.label}</span>)}
      </div>
    </div>
  );
};

const ExperimentTrendPanel: React.FC<Props> = ({ trend, qualityPassed = true }) => {
  const dates = React.useMemo(() => [...new Set(trend.items.map((item) => item.dataDate))], [trend.items]);
  const data = React.useMemo(() => {
    const chartData = dates.flatMap((date) => {
      const item = trend.items.find((trendItem) => trendItem.dataDate === date);
      return metricDefinitions.map((metric) => {
        const rawValue = qualityPassed && item?.quality.status === 'complete' ? item.effect?.[metric.key] : null;
        return { date, metric: metric.label, value: typeof rawValue === 'number' && Number.isFinite(rawValue) ? rawValue * 100 : null };
      });
    });
    return metricDefinitions.some((metric) => chartData.some((point) => point.metric === metric.label && point.value !== null)) ? chartData : [];
  }, [dates, qualityPassed, trend.items]);

  return (
    <Card size="small" title="效果趋势">
      {data.length ? <ExperimentTrendChart dates={dates} data={data} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={qualityPassed ? '无有效效果数据，质量证据不完整' : '质量未通过，不输出净效果'} />}
    </Card>
  );
};

export default ExperimentTrendPanel;
