import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import PageConfigTab from '../../features/config/pages/Config/components/PageConfigTab';
import { PageConfigContext } from './context';
import type { PageConfigPayload } from '@/shared/types';

const payload: PageConfigPayload = {
  schemaVersion: 2,
  version: 1,
  values: {
    'brand_overview.global_metrics': ['query_count', 'pass_rate', 'pending_count'],
    'brand_overview.card.metrics': ['pass_rate', 'pending_count'],
    'brand_overview.card.show_runtime': true,
    'brand_overview.card.show_operational_data': true,
    'brand_overview.drawer.summary_metrics': [],
    'brand_overview.drawer.show_chart': true,
    'brand_overview.drawer.show_table': true,
    'brand_overview.drawer.trend_metrics': ['uv', 'ctr'],
    'brand_detail.query_card.show_metrics': true,
    'brand_detail.query_card.metrics': ['pv', 'uv'],
    'brand_detail.show_execution_overview': true,
    'brand_detail.yesterday.show': true,
    'brand_detail.yesterday.metrics': ['pv', 'ctr'],
    'brand_detail.tabs.show_governance_plans': true,
    'brand_detail.tabs.show_result_comparison': true,
    'brand_detail.tabs.show_relevance_rules': true,
  },
  definitions: [],
};

describe('PageConfigTab v2', () => {
  it('renders page sections and metric list editors from registered definitions', () => {
    render(
      <PageConfigContext.Provider value={{ loading: false, payload, replacePayload: () => undefined }}>
        <PageConfigTab onDirtyChange={() => undefined} />
      </PageConfigContext.Provider>,
    );

    expect(screen.getByText('品牌总览')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '数据看板' })).toBeInTheDocument();
    expect(screen.getByText('品牌卡片')).toBeInTheDocument();
    expect(screen.getByText('品牌详情')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '看板指标：调整' })).toBeInTheDocument();
    expect(screen.queryByText('品牌总览运营数据')).not.toBeInTheDocument();
  });
});
