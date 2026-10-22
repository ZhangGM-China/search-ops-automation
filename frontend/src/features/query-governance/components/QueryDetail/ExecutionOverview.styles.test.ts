/// <reference types="node" />

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const styles = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../../../styles/index.css'),
  'utf8',
);

const readBackground = (selector: string) => {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const rule = styles.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`))?.[1];
  return rule?.match(/background:\s*([^;]+);/)?.[1].trim();
};

const readRule = (selector: string) => {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return styles.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`))?.[1] || '';
};

describe('ExecutionOverview styles', () => {
  it('uses the same background fill as yesterday metric cards', () => {
    const yesterdayCardBackground = readBackground('.ops-query-metric-row__item');
    expect(yesterdayCardBackground).toBe('#fbfcfe');
    expect(readBackground('.ops-execution-overview-card')).toBe(yesterdayCardBackground);
  });

  it('anchors sticky headings to the detail scroll container top', () => {
    expect(readRule('.ops-query-detail-drawer')).toContain('--ops-query-detail-sticky-offset: 0px');
    expect(readRule('.ops-query-detail-sticky-heading')).toContain('top: var(--ops-query-detail-sticky-offset, 0px)');
    expect(readRule('.ops-query-detail-tabs .ops-query-record-tabs > .ant-tabs-nav')).toContain('top: var(--ops-query-detail-sticky-offset, 0px)');
  });
});
