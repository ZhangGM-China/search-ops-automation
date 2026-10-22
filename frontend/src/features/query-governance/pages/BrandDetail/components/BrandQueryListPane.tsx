import React from 'react';
import { Button, DatePicker, Empty, Flex, Space, Tag } from 'antd';
import { LeftOutlined, MenuFoldOutlined, MenuUnfoldOutlined, RightOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import type { TopQueryBrandSummaryData, TopQueryTask } from '@/shared/types';
import QueryGovernanceStepStatusStrip from '../../../components/QueryGovernanceStepStatusStrip';
import { usePageConfig, usePageConfigBoolean } from '@/shared/pageConfig/context';
import {
  type FilterKey,
  isInvalidatedTask,
  numberText,
  percent,
} from '../brandDetailUtils';

const queryCardMetricLabels: Record<string, string> = {
  pv: 'PV', uv: 'UV', ctr: 'CTR', cvr: 'CVR', duration_seconds: '处理耗时', token_usage: 'Token',
};
const queryCardMetricValue = (task: TopQueryTask, key: string): string => {
  const value: number | null | undefined = {
    pv: task.searchPv, uv: task.searchUv, ctr: task.ctr, cvr: task.cvr,
    duration_seconds: task.telemetry?.endToEndSeconds, token_usage: task.telemetry?.tokenUsage,
  }[key];
  if (value == null) return '—';
  if (key === 'ctr' || key === 'cvr') return percent(value);
  if (key === 'duration_seconds') return `${value.toFixed(1)}s`;
  return numberText(value);
};

const QueryCard: React.FC<{ task: TopQueryTask; displayRank: number; active: boolean; showMetrics: boolean; metrics: string[]; visibleCheckKeys?: string[]; onSelect: () => void }> = React.memo(({ task, displayRank, active, showMetrics, metrics, visibleCheckKeys, onSelect }) => (
  <button
    type="button"
    className={`ops-clickable ops-diagnosis-query-card ops-diagnosis-query-card--flat${active ? ' ops-diagnosis-query-card--active' : ''}`}
    onClick={onSelect}
    style={{ width: '100%', textAlign: 'left' }}
  >
    <Flex justify="space-between" align="center" gap={8}>
      <Flex gap={6} align="center" wrap="wrap" style={{ minWidth: 0 }}>
        <div style={{ fontWeight: 700, fontSize: 15 }}>{task.queryText}</div>
        {isInvalidatedTask(task) ? <Tag>已失效</Tag> : null}
      </Flex>
      <span className="ops-query-card-rank">#{displayRank}</span>
    </Flex>
    {showMetrics ? <Flex gap={0} wrap="wrap" className="ops-query-card-metrics">
      {metrics.map((key) => <span key={key}>{queryCardMetricLabels[key] ?? key} {queryCardMetricValue(task, key)}</span>)}
    </Flex> : null}
    <div style={{ marginTop: 12 }}>
      <QueryGovernanceStepStatusStrip steps={task.stepStatuses} visibleCheckKeys={task.visibleCheckKeys ?? visibleCheckKeys} compact ariaLabel={`Query ${task.queryText} 检查项状态`} />
    </div>
  </button>
));

type BrandQueryListPaneProps = {
  brandName: string;
  canGoNext: boolean;
  closePeekList: () => void;
  currentDate?: string | null;
  filter: FilterKey;
  isDesktop: boolean;
  isMobile: boolean;
  listCollapsed: boolean;
  listPaneRef: React.RefObject<HTMLElement | null>;
  listPeekOpen: boolean;
  listVisible: boolean;
  onNavigateOverview: () => void;
  onSelectTask: (task: TopQueryTask) => void;
  openPeekList: () => void;
  queries: TopQueryTask[];
  queryListError: string | null;
  queryListLoading: boolean;
  retryQueryList: () => void;
  loadMoreQueries: () => void;
  selectedTaskId: number | null;
  setFilter: (nextFilter: FilterKey) => void;
  shiftDate: (days: number) => void;
  summary: TopQueryBrandSummaryData;
  switchDate: (date: string) => void;
  todayDate: string;
  toggleListCollapsed: () => void;
  workflowSteps: Array<{ label: string; value: number; key: FilterKey }>;
  visibleCheckKeys?: string[];
};

export const BrandQueryListPane: React.FC<BrandQueryListPaneProps> = ({
  brandName,
  canGoNext,
  closePeekList,
  currentDate,
  filter,
  isDesktop,
  isMobile,
  listCollapsed,
  listPaneRef,
  listPeekOpen,
  listVisible,
  onNavigateOverview,
  onSelectTask,
  openPeekList,
  queries,
  queryListError,
  queryListLoading,
  retryQueryList,
  loadMoreQueries,
  selectedTaskId,
  setFilter,
  shiftDate,
  summary,
  switchDate,
  todayDate,
  toggleListCollapsed,
  workflowSteps,
  visibleCheckKeys,
}) => {
    const showMetrics = usePageConfigBoolean('brand_detail.query_card.show_metrics');
    const metrics = usePageConfig('brand_detail.query_card.metrics');
    const cardMetrics = Array.isArray(metrics) ? metrics : [];
    return (
        <aside
          ref={listPaneRef}
          aria-label="Query 列表"
          className={`ops-brand-detail-list-pane${listCollapsed ? ' ops-brand-detail-list-pane--collapsed' : ''}${listPeekOpen ? ' ops-brand-detail-list-pane--peek-open' : ''}${listVisible ? ' ops-brand-detail-list-pane--visible' : ''}`}
          onMouseEnter={isDesktop && listCollapsed ? openPeekList : undefined}
          onMouseLeave={isDesktop && listCollapsed ? closePeekList : undefined}
        >
          <div className="ops-brand-detail-list-shell">
            <div className="ops-brand-detail-list-toolbar" aria-label={`${brandName} 工作台导航`}>
              <div className="ops-brand-detail-nav-controls">
                {!isDesktop ? (
                  <>
                    <Button
                      type="text"
                      size="small"
                      icon={<LeftOutlined />}
                      onClick={onNavigateOverview}
                      aria-label="返回品牌总览"
                    />
                    <Button
                      type="text"
                      size="small"
                      icon={listCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                      onClick={toggleListCollapsed}
                      aria-label={listCollapsed ? '展开 Query 列表' : '收起 Query 列表'}
                    />
                  </>
                ) : null}
              </div>
              {isMobile || !listCollapsed ? (
                <Space.Compact className="ops-brand-date-switcher">
                  <Button type="text" size="small" icon={<LeftOutlined />} aria-label="前一天" onClick={() => shiftDate(-1)} />
                  <DatePicker
                    allowClear={false}
                    inputReadOnly
                    disabledDate={(value) => Boolean(value && value.format('YYYY-MM-DD') > todayDate)}
                    value={currentDate ? dayjs(currentDate) : undefined}
                    onChange={(value) => switchDate(value?.format('YYYY-MM-DD') || todayDate)}
                    aria-label="选择监测日期"
                  />
                  <Button type="text" size="small" icon={<RightOutlined />} aria-label="后一天" disabled={!canGoNext} onClick={() => shiftDate(1)} />
                </Space.Compact>
              ) : null}
            </div>
            {summary?.experiment ? (
              <div style={{ padding: '0 16px 12px', color: '#1677ff', fontSize: 13, fontWeight: 600 }}>
                当前处于实验模式，仅展示实验组治理结果
              </div>
            ) : null}
            {isMobile || !listCollapsed || listPeekOpen ? (
              <>
                <div className="ops-query-filter-panel">
                  <div className="ops-query-filter-tags" aria-label="Query 筛选">
                    {workflowSteps.map((step) => (
                      <button
                        type="button"
                        key={step.label}
                        aria-label={`筛选${step.label} Query`}
                        className={`ops-clickable ops-query-filter-tag${filter === step.key ? ' ops-query-filter-tag--active' : ''}`}
                        onClick={() => setFilter(step.key)}
                        aria-pressed={filter === step.key}
                      >
                        <span>{step.label}</span>
                        <strong>{numberText(step.value)}</strong>
                      </button>
                    ))}
                  </div>
                </div>
                <div
                  className="ops-brand-detail-list-scroll"
                  onScroll={(event) => {
                    const element = event.currentTarget;
                    if (element.scrollHeight - element.scrollTop - element.clientHeight < 160) {
                      loadMoreQueries();
                    }
                  }}
                  aria-busy={queryListLoading}
                >
                  <Flex vertical gap={0} style={{ minHeight: '100%' }}>
                    {queries.length ? queries.map((task, index) => (
                        <QueryCard
                          key={task.id}
                          task={task}
                          displayRank={index + 1}
                          active={task.id === selectedTaskId}
                          showMetrics={showMetrics}
                          metrics={cardMetrics}
                          visibleCheckKeys={visibleCheckKeys}
                          onSelect={() => onSelectTask(task)}
                        />
                    )) : (
                      <div className="ops-brand-detail-empty-state ops-brand-detail-empty-state--list">
                        <Empty description="当前筛选下暂无 Query" />
                      </div>
                    )}
                  </Flex>
                </div>
                {queryListError ? (
                  <div className="ops-brand-detail-list-load-status" role="status" aria-live="polite">
                    <span>{queryListError}</span>
                    <Button type="link" size="small" onClick={retryQueryList}>重试</Button>
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
        </aside>
    );
};
