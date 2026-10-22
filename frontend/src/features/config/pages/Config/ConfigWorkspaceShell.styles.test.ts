/// <reference types="node" />

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const styles = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../../../styles/index.css'),
  'utf8',
);

describe('ConfigWorkspaceShell styles', () => {
  it('removes the secondary tab navigation baseline', () => {
    const rule = styles.match(/\.config-primary-header \.ant-tabs-nav::before\s*\{([^}]*)\}/)?.[1];

    expect(rule).toMatch(/border-bottom:\s*0(?:px)?\s*(?:!important\s*)?;/);
  });
});
