export function normalizeRuntimeError(error) {
  const raw = error instanceof Error ? error.message : String(error || 'PI Agent runtime failed');
  if (/No API key found|not authenticated|selected model/i.test(raw)) {
    return 'PI Agent 模型未配置或未认证，请配置 PI_AGENT_PROVIDER 与 PI_AGENT_MODEL，或先完成对应模型提供方登录。';
  }
  if (/model not found|ambiguous across providers/i.test(raw)) {
    return 'PI Agent 模型配置不明确，请同时配置匹配的 PI_AGENT_PROVIDER 与 PI_AGENT_MODEL。';
  }
  return raw.replace(/(?:[A-Za-z]:)?\/[^\s]+/g, '[path]');
}
