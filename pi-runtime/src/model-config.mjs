import path from 'node:path';

export function resolveRuntimeModelConfig(env = process.env, defaultModelsPath) {
  const explicitProvider = String(env.PI_AGENT_PROVIDER || '').trim();
  const explicitModel = String(env.PI_AGENT_MODEL || '').trim();
  const gatewayConfigured = Boolean(String(env.RD_LLM_BASE_URL || '').trim() && String(env.RD_LLM_API_KEY || '').trim());
  const provider = explicitProvider || (gatewayConfigured ? 'aiops-gateway' : '');
  const model = explicitModel || (gatewayConfigured ? String(env.RD_LLM_MODEL || 'qwen3.6-plus').trim() : '');
  const modelsPath = String(env.PI_AGENT_MODELS_PATH || defaultModelsPath || '').trim() || undefined;
  return { provider, model, modelsPath };
}

export function resolveSessionModelConfig(
  env = process.env,
  requestedProvider = '',
  requestedModel = '',
  defaultModelsPath,
) {
  const base = resolveRuntimeModelConfig(env, defaultModelsPath);
  const provider = String(requestedProvider || base.provider || '').trim();
  const model = String(requestedModel || base.model || '').trim();
  return { provider, model, modelsPath: base.modelsPath };
}

export function buildGatewayModelsConfig(env = process.env, provider = 'aiops-gateway', model = 'qwen3.6-plus') {
  const baseUrl = String(env.RD_LLM_BASE_URL || '').trim();
  if (!baseUrl) return undefined;
  return {
    providers: {
      [provider]: {
        baseUrl,
        api: 'openai-completions',
        apiKey: '$RD_LLM_API_KEY',
        compat: { supportsDeveloperRole: false, supportsReasoningEffort: false },
        models: [{ id: model, name: model, reasoning: false, input: ['text'], contextWindow: 128000, maxTokens: 32768 }],
      },
    },
  };
}

export function mergeModelsConfig(existing = {}, generated = {}) {
  const existingProviders = existing && typeof existing.providers === 'object' && existing.providers
    ? existing.providers
    : {};
  const generatedProviders = generated && typeof generated.providers === 'object' && generated.providers
    ? generated.providers
    : {};
  const providers = { ...existingProviders };
  for (const [provider, generatedConfig] of Object.entries(generatedProviders)) {
    const existingConfig = existingProviders[provider] && typeof existingProviders[provider] === 'object'
      ? existingProviders[provider]
      : {};
    const existingModels = Array.isArray(existingConfig.models) ? existingConfig.models : [];
    const generatedModels = Array.isArray(generatedConfig?.models) ? generatedConfig.models : [];
    const seen = new Set();
    const models = [...existingModels, ...generatedModels].filter((model) => {
      const id = String(model?.id || '');
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return true;
    });
    providers[provider] = { ...existingConfig, ...generatedConfig, models };
  }
  return { ...existing, providers };
}

export function defaultGatewayModelsPath(agentDir) {
  return path.join(agentDir, 'models.json');
}
