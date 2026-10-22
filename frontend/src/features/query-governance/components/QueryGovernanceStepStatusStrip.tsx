import React from 'react';
import { Flex, Tag } from 'antd';
import type { TopQueryStepStatus } from '@/shared/types';
import { checkStateLabel, checkStateTone, hasStepIssueEvidence, resolveCheckState } from '../lib/queryGovernanceStatus';

const topQueryCheckNameMap: Record<string, string> = {
  recall: '召回数量',
  relevance: '相关性',
  open_factor: '重排序',
  inventory: '断码',
};

const orderedCheckKeys = ['recall', 'relevance', 'open_factor', 'inventory'];

const stepState = (step: TopQueryStepStatus) => resolveCheckState(step, hasStepIssueEvidence(step.evidence));

const normalizedStepKey = (step: TopQueryStepStatus) => (
  step.checkKey === 'reorder' ? 'open_factor' : step.checkKey
);

const QueryGovernanceStepStatusStrip: React.FC<{ steps?: TopQueryStepStatus[]; visibleCheckKeys?: string[]; compact?: boolean; ariaLabel?: string }> = ({
  steps = [],
  visibleCheckKeys,
  compact = false,
  ariaLabel,
}) => {
  const byKey = new Map(steps.map((step) => [normalizedStepKey(step), step]));
  const visible = visibleCheckKeys ? new Set(visibleCheckKeys) : null;
  const orderedSteps = orderedCheckKeys.filter((key) => !visible || visible.has(key)).map((key) => byKey.get(key) || {
    checkKey: key,
    status: 'normal',
    displayStatus: 'normal',
  } as TopQueryStepStatus);

  if (compact) {
    return (
      <Flex gap={8} wrap="wrap" aria-label={ariaLabel} className="ops-top-query-step-strip">
        {orderedSteps.map((step) => (
          <span
            key={normalizedStepKey(step)}
            className={`ops-top-query-step-tag ops-top-query-step-tag--${checkStateTone(stepState(step))}`}
            title={step.governanceStatus?.label
              ? `${topQueryCheckNameMap[normalizedStepKey(step)] ?? normalizedStepKey(step)}：检查${checkStateLabel(stepState(step))}；治理${step.governanceStatus.label}${step.governanceStatus.errorMessage ? `（${step.governanceStatus.errorMessage}）` : ''}${step.summary ? `；${step.summary}` : ''}`
              : `${topQueryCheckNameMap[normalizedStepKey(step)] ?? normalizedStepKey(step)}：${checkStateLabel(stepState(step))}${step.summary ? `；${step.summary}` : ''}`}
          >
            <span className="ops-top-query-step-tag__dot" aria-hidden="true" />
            <span>{topQueryCheckNameMap[normalizedStepKey(step)] ?? normalizedStepKey(step)}</span>
          </span>
        ))}
      </Flex>
    );
  }

  return (
    <Flex gap={6} wrap="wrap">
      {orderedSteps.map((step) => (
        <Tag key={step.checkKey} style={{ marginInlineEnd: 0 }}>
          {topQueryCheckNameMap[normalizedStepKey(step)] ?? normalizedStepKey(step)} · {checkStateLabel(stepState(step))}
        </Tag>
      ))}
    </Flex>
  );
};

export default QueryGovernanceStepStatusStrip;
