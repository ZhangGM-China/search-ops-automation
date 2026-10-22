import React from 'react';
import { Card, Empty, Statistic, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TopQueryExperimentActionAudit as ActionAudit, TopQueryExperimentOperations } from '@/shared/types';

interface Props { actions: ActionAudit[]; operations: TopQueryExperimentOperations; isMobile: boolean }

const statusMap: Record<string, { label: string; color: string }> = {
  planned: { label: '待执行', color: 'gold' }, pending: { label: '待执行', color: 'gold' },
  waiting_human_approval: { label: '待人工确认', color: 'gold' }, approved: { label: '已批准', color: 'blue' },
  executing: { label: '执行中', color: 'processing' }, verifying: { label: '验证中', color: 'processing' }, rollback_pending: { label: '等待回滚', color: 'warning' },
  executed: { label: '执行成功', color: 'success' }, verified: { label: '验证通过', color: 'success' }, success: { label: '执行成功', color: 'success' }, completed: { label: '执行成功', color: 'success' },
  dry_run_executed: { label: '演练已执行（非真实生效）', color: 'warning' },
  failed: { label: '执行失败', color: 'error' }, verification_failed: { label: '验证失败', color: 'error' },
  verification_inconclusive: { label: '验证未完成/未确定', color: 'warning' }, execution_unknown: { label: '执行结果未知', color: 'warning' },
  inconclusive: { label: '结果未决', color: 'warning' }, unresolved: { label: '尚未收敛', color: 'warning' }, manual_required: { label: '需要人工处理', color: 'warning' },
  skipped: { label: '已跳过', color: 'default' }, rejected: { label: '已拒绝', color: 'default' }, cancelled: { label: '已取消', color: 'default' },
};
const errorMap: Record<string, string> = { timeout: '执行超时', permission_denied: '无执行权限', source_failed: '数据源调用失败' };
const formatError = (value?: string | null) => value ? (errorMap[value] ?? `执行异常（${value}）`) : '-';
const statusTag = (value?: string | null) => {
  const status = statusMap[value ?? ''] ?? { label: '未知状态', color: 'default' };
  return <Tag color={status.color}>{status.label}</Tag>;
};

const ExperimentActionAudit: React.FC<Props> = ({ actions, operations, isMobile }) => {
  const details = actions.filter((item) => item.recordType === 'action');
  const summaries = actions.filter((item) => item.recordType === 'summary');
  const columns: ColumnsType<ActionAudit> = [
    { title: '日期', dataIndex: 'dataDate', key: 'date' },
    { title: 'Query', dataIndex: 'queryText', key: 'query' },
    { title: '检查项', dataIndex: 'checkKey', key: 'check' },
    { title: '动作', dataIndex: 'actionType', key: 'action' },
    { title: '模式', dataIndex: 'executionMode', key: 'mode', render: (value) => value === 'auto' ? '自动' : value === 'human_approval' ? '人工确认' : value ?? '-' },
    { title: '状态', dataIndex: 'status', key: 'status', render: statusTag },
    { title: '原因', dataIndex: 'reason', key: 'reason', render: (value) => value ?? '-' },
    { title: '错误', dataIndex: 'errorMessage', key: 'error', render: formatError },
  ];
  return <div className="experiment-audit-stack">
    <div className="experiment-stat-grid experiment-audit-summary">
      <Card size="small"><Statistic title="计划动作" value={operations.plannedActions} /></Card>
      <Card size="small" title="动作结果"><strong>已执行 {operations.executedActions} / 失败 {operations.failedActions} / 未确定 {operations.inconclusiveActions ?? 0} / 未收口 {operations.unresolvedActions ?? 0}</strong><small>已拒绝 {operations.rejectedActions ?? 0} / 已取消 {operations.cancelledActions ?? 0}</small></Card>
      <Card size="small"><Statistic title="Token" value={operations.tokenUsage} /></Card>
      <Card size="small"><Statistic title="耗时" value={operations.durationSeconds} suffix="秒" /></Card>
    </div>
    {summaries.length ? <Card size="small" title="历史动作汇总" extra={<Tag color="gold">历史动作明细不可用</Tag>}>
      <div className="experiment-action-summaries">{summaries.map((item, index) => <div key={`${item.dataDate}-${item.taskId}-${index}`}>
        <strong>{item.queryText ?? '未知 Query'}</strong>
        <span>计划 {item.planned ?? 0} / 成功 {item.executed ?? 0} / 失败 {item.failed ?? 0} / 未决 {(item.inconclusive ?? 0) + (item.unresolved ?? 0)}</span>
      </div>)}</div>
    </Card> : null}
    {!details.length && !summaries.length ? <Card size="small"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本实验暂无治理动作，保留质量审计结果" /></Card> : details.length && isMobile ? (
      <div className="experiment-mobile-list">{details.map((item, index) => <article className="experiment-mobile-query-card" key={`${item.id ?? index}`}>
        <div><strong>{item.queryText ?? '未知 Query'}</strong>{statusTag(item.status)}</div>
        <dl>
          <div><dt>动作 / 模式</dt><dd>{item.actionType ?? '-'} / {item.executionMode === 'auto' ? '自动' : '人工确认'}</dd></div>
          <div><dt>原因</dt><dd>{item.reason ?? '-'}</dd></div>
          <div><dt>错误</dt><dd>{formatError(item.errorMessage)}</dd></div>
        </dl>
      </article>)}</div>
    ) : details.length ? <Table rowKey={(item) => String(item.id ?? `${item.dataDate}-${item.taskId}-${item.actionType}`)} columns={columns} dataSource={details} size="small" pagination={false} scroll={{ x: 980 }} /> : null}
  </div>;
};

export default ExperimentActionAudit;
