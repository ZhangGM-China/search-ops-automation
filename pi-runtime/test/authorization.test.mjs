import test from 'node:test';
import assert from 'node:assert/strict';

import { assertToolCallAllowed, normalizePermissionScope } from '../src/authorization.mjs';

test('normalizes the account permission scope without exposing brand selectors', () => {
  assert.deepEqual(
    normalizePermissionScope({ brands: [' atlas ', 'harbor', ''], tools: ['read_today_context', 'search_products'] }),
    { brands: ['atlas', 'harbor'], tools: ['read_today_context', 'search_products'] },
  );
});

test('blocks a business tool when the account has no matching brand permission', () => {
  assert.throws(
    () => assertToolCallAllowed(
      { toolName: 'search_products', input: { brand: 'meadow' } },
      { brands: ['atlas'], tools: ['search_products'] },
    ),
    /not accessible/i,
  );
});

test('allows a shell command only inside the account workspace', () => {
  assert.doesNotThrow(() => assertToolCallAllowed(
    { toolName: 'bash', input: { command: 'pytest tests/test_pi_agent_assistant.py' } },
    { brands: ['atlas'], tools: ['bash'], workdir: '/tmp/aiops' },
  ));
  assert.throws(
    () => assertToolCallAllowed(
      { toolName: 'bash', input: { command: 'cat /etc/passwd' } },
      { brands: ['atlas'], tools: ['bash'], workdir: '/tmp/aiops' },
    ),
    /workspace/i,
  );
});
