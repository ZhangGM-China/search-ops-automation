import type { QueryFilters } from '@/shared/types';

const OPERATIONS_LOG_FILTERS_KEY = 'operations-log-filters';

export const readOperationsLogFilters = (): QueryFilters => {
  try {
    const raw = sessionStorage.getItem(OPERATIONS_LOG_FILTERS_KEY);
    return raw ? JSON.parse(raw) as QueryFilters : {};
  } catch { return {}; }
};

export const writeOperationsLogFilters = (values: QueryFilters) => {
  const next = { ...readOperationsLogFilters(), ...values };
  if (next.date && typeof next.date !== 'string') {
    const value = next.date as { format?: (pattern: string) => string };
    next.date = typeof value.format === 'function' ? value.format('YYYY-MM-DD') : undefined;
  }
  Object.keys(next).forEach((key) => { if (next[key] === undefined || next[key] === null || next[key] === '') delete next[key]; });
  sessionStorage.setItem(OPERATIONS_LOG_FILTERS_KEY, JSON.stringify(next));
};
