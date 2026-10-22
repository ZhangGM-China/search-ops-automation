import dayjs from 'dayjs';
import { describe, expect, it } from 'vitest';

import {
  aggregateMetricTrend,
  metricTrendSpecs,
} from './metricTrendSpec';

describe('metricTrendSpec', () => {
  it('aggregates relevance_accuracy using inspected_product_count when present', () => {
    const spec = metricTrendSpecs.relevance_accuracy;
    const days = [
      {
        date: '2026-09-01',
        metrics: {
          inspected_product_count: 100,
          detected_product_count: 50,
          irrelevant_product_count: 10,
        },
      },
      {
        date: '2026-09-02',
        metrics: {
          inspected_product_count: 200,
          detected_product_count: 100,
          irrelevant_product_count: 20,
        },
      },
    ];

    const result = aggregateMetricTrend(
      days as never,
      spec,
      'day',
      [dayjs('2026-09-01'), dayjs('2026-09-02')],
    );

    expect(result).toEqual([
      { period: '2026-09-01', value: 0.9 },
      { period: '2026-09-02', value: 0.9 },
    ]);
  });

  it('falls back to detected_product_count if inspected_product_count is missing', () => {
    const spec = metricTrendSpecs.relevance_accuracy;
    const days = [
      {
        date: '2026-09-01',
        metrics: {
          detected_product_count: 50,
          irrelevant_product_count: 5,
        },
      },
    ];

    const result = aggregateMetricTrend(
      days as never,
      spec,
      'day',
      [dayjs('2026-09-01'), dayjs('2026-09-01')],
    );

    expect(result).toEqual([
      { period: '2026-09-01', value: 0.9 },
    ]);
  });

  it('aggregates pass_rate properly over days', () => {
    const spec = metricTrendSpecs.pass_rate;
    const days = [
      {
        date: '2026-09-01',
        metrics: {
          query_count: 300,
          pass_count: 270,
        },
      },
    ];

    const result = aggregateMetricTrend(
      days as never,
      spec,
      'day',
      [dayjs('2026-09-01'), dayjs('2026-09-01')],
    );

    expect(result).toEqual([
      { period: '2026-09-01', value: 0.9 },
    ]);
  });
});
