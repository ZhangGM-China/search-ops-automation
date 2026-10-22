import test from 'node:test';
import assert from 'node:assert/strict';

import {
  mergeModelsConfig,
  resolveRuntimeModelConfig,
  resolveSessionModelConfig,
} from '../src/model-config.mjs';

test('uses the configured PI provider/model when both are present', () => {
  assert.deepEqual(
    resolveRuntimeModelConfig({
      PI_AGENT_PROVIDER: 'aiops-gateway',
      PI_AGENT_MODEL: 'qwen3.6-plus',
      PI_AGENT_MODELS_PATH: '/tmp/models.json',
    }),
    {
      provider: 'aiops-gateway',
      model: 'qwen3.6-plus',
      modelsPath: '/tmp/models.json',
    },
  );
});

test('derives the local gateway provider when the existing RD gateway is configured', () => {
  assert.deepEqual(
    resolveRuntimeModelConfig({
      RD_LLM_BASE_URL: 'https://api-hub.example.test/v1',
      RD_LLM_API_KEY: 'secret-is-not-returned',
      RD_LLM_MODEL: 'qwen3.6-plus',
    }),
    {
      provider: 'aiops-gateway',
      model: 'qwen3.6-plus',
      modelsPath: undefined,
    },
  );
});

test('does not select a model when no provider/model or gateway credentials exist', () => {
  assert.deepEqual(resolveRuntimeModelConfig({}), {
    provider: '',
    model: '',
    modelsPath: undefined,
  });
});

test('uses the model requested by the AIOPS session with the configured RD gateway', () => {
  assert.deepEqual(
    resolveSessionModelConfig(
      {
        RD_LLM_BASE_URL: 'https://api-hub.example.test/v1',
        RD_LLM_API_KEY: 'secret-is-not-returned',
      },
      '',
      'deepseek-v4.1-flash',
      '/tmp/models.json',
    ),
    {
      provider: 'aiops-gateway',
      model: 'deepseek-v4.1-flash',
      modelsPath: '/tmp/models.json',
    },
  );
});

test('merges a published session model into an existing gateway models file', () => {
  assert.deepEqual(
    mergeModelsConfig(
      {
        providers: {
          'aiops-gateway': {
            baseUrl: 'https://old.example/v1',
            api: 'openai-completions',
            apiKey: '$RD_LLM_API_KEY',
            models: [{ id: 'qwen3.6-plus', name: 'qwen3.6-plus' }],
          },
        },
      },
      {
        providers: {
          'aiops-gateway': {
            baseUrl: 'https://new.example/v1',
            api: 'openai-completions',
            apiKey: '$RD_LLM_API_KEY',
            models: [{ id: 'deepseek-v4.1-flash', name: 'deepseek-v4.1-flash' }],
          },
        },
      },
    ),
    {
      providers: {
        'aiops-gateway': {
          baseUrl: 'https://new.example/v1',
          api: 'openai-completions',
          apiKey: '$RD_LLM_API_KEY',
          models: [
            { id: 'qwen3.6-plus', name: 'qwen3.6-plus' },
            { id: 'deepseek-v4.1-flash', name: 'deepseek-v4.1-flash' },
          ],
        },
      },
    },
  );
});
