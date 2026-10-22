import type { TopQueryExperimentSamplePoolItem } from '@/shared/types';

export const formatPercent = (value: number | null | undefined, digits = 2) => (
  value == null ? '-' : `${(value * 100).toFixed(digits)}%`
);

export const formatSignedPercent = (value: number | null | undefined, digits = 2) => (
  value == null ? '-' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`
);

export const renderQueryOption = (item: TopQueryExperimentSamplePoolItem) => (
  <div className="experiment-query-option">
    <span className="experiment-query-option__name">{item.queryText}</span>
    <span className="experiment-query-option__metrics">
      <span>#{item.queryRank}</span><span>PV {item.searchPv}</span><span>UV {item.searchUv}</span>
      <span>CTR {formatPercent(item.ctr, 1)}</span><span>CVR {formatPercent(item.cvr, 1)}</span>
    </span>
  </div>
);
