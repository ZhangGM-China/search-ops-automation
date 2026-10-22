import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeRuntimeError } from '../src/runtime-errors.mjs';

test('model auth errors are actionable and do not expose local paths', () => {
  const result = normalizeRuntimeError(new Error(
    'No API key found for the selected model. See: /private/tmp/project/node_modules/docs/providers.md',
  ));

  assert.match(result, /PI_AGENT_PROVIDER/);
  assert.match(result, /PI_AGENT_MODEL/);
  assert.doesNotMatch(result, /private\/tmp/);
});
