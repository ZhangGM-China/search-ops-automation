import React from 'react';
import type { PageConfigKey, PageConfigPayload, PageConfigValue } from '@/shared/types';

export const defaultPageConfigValues: Record<PageConfigKey, PageConfigValue> = {
  'brand_overview.show_global_metrics': true,
  'brand_overview.global_metrics': ['query_count', 'pass_rate', 'pending_count'],
  'brand_overview.card.primary_metric': ['query_count'],
  'brand_overview.card.metrics': ['irrelevant_query_count', 'preference_adjusted_query_count', 'stock_bucket_reduction_product_count'],
  'brand_overview.card.show_runtime': true,
  'brand_overview.card.show_operational_data': true,
  'brand_overview.drawer.summary_metrics': [],
  'brand_overview.drawer.show_chart': true,
  'brand_overview.drawer.show_table': true,
  'brand_overview.drawer.trend_metrics': ['uv', 'pv', 'clickUv', 'addToCartUv', 'purchaseUv', 'ctr', 'atc', 'cvr'],
  'brand_detail.query_card.show_metrics': true,
  'brand_detail.query_card.metrics': ['pv', 'uv', 'ctr', 'cvr'],
  'brand_detail.show_execution_overview': true,
  'brand_detail.yesterday.show': true,
  'brand_detail.yesterday.metrics': ['pv', 'uv', 'ctr', 'cvr', 'atc'],
  'brand_detail.tabs.show_governance_plans': true,
  'brand_detail.tabs.show_result_comparison': true,
  'brand_detail.tabs.show_relevance_rules': true,
};

const legacyValues = (payload: PageConfigPayload): Partial<Record<PageConfigKey, PageConfigValue>> => {
  const raw = payload.values as Record<string, unknown>;
  if (payload.schemaVersion >= 2) return {};
  const migrated: Partial<Record<PageConfigKey, PageConfigValue>> = {};
  if (typeof raw['brand_overview.show_operational_data'] === 'boolean') {
    migrated['brand_overview.card.show_operational_data'] = raw['brand_overview.show_operational_data'];
  }
  if (typeof raw['brand_detail.show_query_metrics'] === 'boolean') {
    migrated['brand_detail.yesterday.show'] = raw['brand_detail.show_query_metrics'];
  }
  const actionMetrics = [1, 2, 3].map((index) => raw[`brand_overview.action_metric_${index}`]).filter((item): item is string => typeof item === 'string');
  if (actionMetrics.length >= 2) migrated['brand_overview.card.metrics'] = actionMetrics;
  if (typeof raw['brand_overview.primary_metric'] === 'string') {
    migrated['brand_overview.card.primary_metric'] = [raw['brand_overview.primary_metric']];
  }
  return migrated;
};

export const normalizePageConfigPayload = (payload: PageConfigPayload): PageConfigPayload => {
  const values = { ...payload.values } as Record<string, unknown>;
  [
    'brand_overview.show_operational_data', 'brand_detail.show_query_metrics',
    'brand_overview.primary_metric', 'brand_overview.action_metric_1',
    'brand_overview.action_metric_2', 'brand_overview.action_metric_3',
  ].forEach((key) => delete values[key]);
  return {
    ...payload,
    values: { ...defaultPageConfigValues, ...legacyValues(payload), ...values } as PageConfigPayload['values'],
  };
};

export type PageConfigContextValue = {
  loading: boolean;
  payload: PageConfigPayload;
  replacePayload: (payload: PageConfigPayload) => void;
};

export const defaultPageConfigPayload: PageConfigPayload = {
  schemaVersion: 2,
  version: 0,
  values: defaultPageConfigValues,
  definitions: [],
};

export const PageConfigContext = React.createContext<PageConfigContextValue>({
  loading: false,
  payload: defaultPageConfigPayload,
  replacePayload: () => undefined,
});

export const usePageConfigContext = () => React.useContext(PageConfigContext);

export function usePageConfig(key: PageConfigKey): PageConfigValue {
  const { payload } = usePageConfigContext();
  return payload.values[key] ?? defaultPageConfigValues[key];
}

export const usePageConfigBoolean = (key: PageConfigKey): boolean => usePageConfig(key) === true;
export const usePageConfigMetrics = (key: PageConfigKey): string[] => {
  const value = usePageConfig(key);
  return Array.isArray(value) ? value : [];
};
