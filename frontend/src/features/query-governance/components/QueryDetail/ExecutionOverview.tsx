import React from 'react';
import { Flex, Tag, Typography } from 'antd';
import { checkStateMeta, type CheckStateTone } from '../../lib/queryGovernanceStatus';
import type { ExecutionOverviewNode } from './executionOverviewModel';

const { Text } = Typography;

const statusColor: Record<CheckStateTone, string> = {
  green: 'green',
  blue: 'blue',
  yellow: 'gold',
  red: 'red',
  default: 'default',
};

type Props = { nodes: ExecutionOverviewNode[]; onOpen: (node: ExecutionOverviewNode) => void };

export const ExecutionOverview: React.FC<Props> = ({ nodes, onOpen }) => (
  <section className="ops-query-detail-section ops-query-detail-section--diagnostics" aria-label="执行概览">
    <div className="ops-query-detail-section__title ops-query-detail-sticky-heading">执行概览</div>
    <div className="ops-query-metric-row ops-execution-overview-panel" data-testid="execution-overview-panel">
      {nodes.map((node) => {
        const meta = checkStateMeta[node.state];
        return (
          <button
            type="button"
            className="ops-execution-overview-card"
            key={node.key}
            onClick={() => onOpen(node)}
            aria-label={`查看${node.label}详情`}
          >
            <Flex justify="space-between" align="center" gap={8}>
              <Text strong>{node.label}</Text>
              <Tag color={statusColor[meta.tone]}>{meta.label}</Tag>
            </Flex>
            <div className={`ops-execution-overview-card__summary${node.state === 'failed' && node.key !== 'open_factor' ? ' ops-execution-overview-card__summary--error' : ''}`}>
              {node.summary}
            </div>
            <span className="ops-execution-overview-card__action">查看详情</span>
          </button>
        );
      })}
    </div>
  </section>
);

export default ExecutionOverview;
