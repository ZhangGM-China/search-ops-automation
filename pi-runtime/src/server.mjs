import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { Type } from 'typebox';

import {
  createAgentSession,
  DefaultResourceLoader,
  SessionManager,
  ModelRuntime,
} from '@earendil-works/pi-coding-agent';

import { assertToolCallAllowed, normalizePermissionScope } from './authorization.mjs';
import { normalizeRuntimeError } from './runtime-errors.mjs';
import { runKgTask } from './kg-task.mjs';
import { timingSafeEqual } from 'node:crypto';
import {
  buildGatewayModelsConfig,
  defaultGatewayModelsPath,
  mergeModelsConfig,
  resolveSessionModelConfig,
} from './model-config.mjs';

const port = Number(process.env.PI_RUNTIME_PORT || 8124);
const defaultWorkdir = path.resolve(process.env.PI_AGENT_WORKDIR || process.cwd());
const defaultSessionDir = path.resolve(process.env.PI_AGENT_SESSION_DIR || path.join(defaultWorkdir, '.pi', 'assistant-sessions'));
const defaultAgentDir = path.resolve(process.env.PI_AGENT_DIR || path.join(defaultWorkdir, '.pi', 'agent'));

async function ensureModelsConfig(modelsPath, config) {
  if (!config || !modelsPath) return;
  let existing = {};
  try {
    existing = JSON.parse(await fs.readFile(modelsPath, 'utf8'));
  } catch {
    existing = {};
  }
  const merged = mergeModelsConfig(existing, config);
  await fs.mkdir(path.dirname(modelsPath), { recursive: true });
  const temporaryPath = `${modelsPath}.${process.pid}.${randomUUID()}.tmp`;
  await fs.writeFile(temporaryPath, `${JSON.stringify(merged, null, 2)}\n`, { mode: 0o600 });
  await fs.rename(temporaryPath, modelsPath);
}

function json(res, status, payload) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

async function readBody(req) {
  let raw = '';
  for await (const chunk of req) raw += chunk;
  if (!raw) return {};
  return JSON.parse(raw);
}

function promptEnvelope(body) {
  const operator = String(body.operator || 'operator');
  const scope = normalizePermissionScope(body.permissionScope || {});
  return [
    '你是 AIOPS 智能运营 Agent。你可以使用当前启用的 PI 原生工具完成诊断和执行。',
    '工具调用必须遵守账号权限范围；不得猜测或扩大权限。涉及业务写操作时必须先调用现有 prepare/confirm/cancel 链路。',
    '先给结论，再给数据证据、已执行步骤和未闭环事项；证据不足时明确转人工。',
    `当前操作员：${operator}`,
    `账号授权范围（仅供工具权限判断，不是前端选择器）：${JSON.stringify(scope, null, 2)}`,
    `运营上下文：${JSON.stringify(body.context || {}, null, 2)}`,
    `用户请求：${String(body.prompt || '')}`,
  ].join('\n');
}

function toolResultText(result) {
  if (typeof result === 'string') return result;
  if (!result || typeof result !== 'object') return '';
  if (Array.isArray(result.content)) {
    const text = result.content.map((item) => item && typeof item === 'object' ? item.text || item.content || '' : '').filter(Boolean).join('\n');
    if (text) return text;
  }
  try { return JSON.stringify(result.details ?? result); } catch { return String(result); }
}

const BUSINESS_TOOL_SCHEMAS = {
  read_today_context: Type.Object({}),
  read_top_query_summary: Type.Object({ query: Type.Optional(Type.String()) }),
  read_brand_metrics: Type.Object({ brand: Type.String() }),
  read_top_query_detail: Type.Object({ brand: Type.String(), query: Type.String() }),
  read_top_query_events: Type.Object({ brand: Type.String(), query: Type.String() }),
  read_abnormal_query_logs: Type.Object({ brand: Type.Optional(Type.String()) }),
  read_inventory_monitoring: Type.Object({ query: Type.Optional(Type.String()) }),
  search_products: Type.Object({ brand: Type.String(), query: Type.String() }),
  get_product_detail: Type.Object({ brand: Type.String(), productId: Type.String() }),
  prepare_action: Type.Object({ actionType: Type.String(), brand: Type.Optional(Type.String()), query: Type.Optional(Type.String()), reason: Type.Optional(Type.String()) }),
};

function createBusinessTools(body) {
  const apiBaseUrl = String(body.apiBaseUrl || process.env.AIOPS_INTERNAL_API_URL || 'http://127.0.0.1:8014').replace(/\/$/, '');
  const internalToken = String(body.internalToken || process.env.PI_AGENT_INTERNAL_TOKEN || 'local-pi-runtime');
  const userId = Number(body.userId);
  return Object.entries(BUSINESS_TOOL_SCHEMAS).map(([name, parameters]) => ({
    name,
    label: name,
    description: `在当前账号授权范围内${name}，返回结构化 AIOPS 运营数据。`,
    parameters,
    async execute(_toolCallId, params, signal) {
      const response = await fetch(`${apiBaseUrl}/api/internal/assistant/tools/${encodeURIComponent(name)}`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-PI-Internal-Token': internalToken,
        },
        body: JSON.stringify({
          userId,
          arguments: params || {},
          context: body.context || {},
        }),
        signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || payload.message || `AIOPS tool ${name} failed`);
      return {
        content: [{ type: 'text', text: String(payload.message || JSON.stringify(payload)) }],
        details: payload,
      };
    },
  }));
}

async function createSession(body) {
  const cwd = path.resolve(String(body.workdir || defaultWorkdir));
  const sessionDir = path.resolve(String(body.sessionDir || defaultSessionDir));
  const sessionId = String(body.sessionId || '').trim();
  if (!sessionId) throw new Error('sessionId is required');
  const sessionPath = path.join(sessionDir, `${sessionId}.jsonl`);
  const sessionManager = await import('node:fs/promises').then(async ({ access }) => {
    try {
      await access(sessionPath);
      return SessionManager.open(sessionPath, sessionDir, cwd);
    } catch {
      return SessionManager.create(cwd, sessionDir, { id: sessionId });
    }
  });
  const scope = normalizePermissionScope({
    ...(body.permissionScope || {}),
    workdir: cwd,
  });
  const modelConfig = resolveSessionModelConfig(
    process.env,
    body.provider,
    body.model,
    defaultGatewayModelsPath(defaultAgentDir),
  );
  const generatedModelsConfig = modelConfig.provider === 'aiops-gateway'
    ? buildGatewayModelsConfig(process.env, modelConfig.provider, modelConfig.model)
    : undefined;
  await ensureModelsConfig(modelConfig.modelsPath, generatedModelsConfig);
  const modelRuntime = await ModelRuntime.create({ modelsPath: modelConfig.modelsPath, authPath: path.join(defaultAgentDir, 'auth.json') });
  const provider = String(body.provider || modelConfig.provider || '').trim();
  const modelName = String(body.model || modelConfig.model || '').trim();
  const model = provider && modelName ? modelRuntime.getModel(provider, modelName) : undefined;
  if (provider && modelName && !model) throw new Error(`PI model not found: ${provider}/${modelName}`);
  const resourceLoader = new DefaultResourceLoader({
    cwd,
    agentDir: defaultAgentDir,
    systemPrompt: '你是 AIOPS 智能运营 Agent。只在账号授权范围内工作。',
    extensionFactories: [
      (pi) => {
        pi.on('tool_call', async (event) => {
          try {
            assertToolCallAllowed({ toolName: event.toolName, input: event.input }, scope);
            return undefined;
          } catch (error) {
            return {
              block: true,
              terminate: true,
              reason: error instanceof Error ? error.message : 'Tool call blocked by account policy',
            };
          }
        });
      },
    ],
  });
  await resourceLoader.reload();
  const { session } = await createAgentSession({
    cwd,
    agentDir: defaultAgentDir,
    model,
    modelRuntime,
    tools: ['read', 'write', 'edit', 'bash', 'grep', 'find', 'ls', ...Object.keys(BUSINESS_TOOL_SCHEMAS)],
    customTools: createBusinessTools(body),
    resourceLoader,
    sessionManager,
  });
  return session;
}

async function handleStream(req, res, body) {
  res.writeHead(200, {
    'content-type': 'application/x-ndjson; charset=utf-8',
    'cache-control': 'no-cache',
    connection: 'keep-alive',
    'transfer-encoding': 'chunked',
  });
  let session;
  const send = (event) => res.write(`${JSON.stringify(event)}\n`);
  try {
    session = await createSession(body);
    let sawAgentEnd = false;
    const toolStartedAt = new Map();
    send({ type: 'phase', phase: 'thinking', label: '正在思考' });
    const unsubscribe = session.subscribe((event) => {
      if (event.type === 'message_update' && event.assistantMessageEvent?.type === 'text_delta') {
        send({ type: 'phase', phase: 'waiting', label: '正在生成回复' });
        send({ type: 'delta', content: event.assistantMessageEvent.delta });
      } else if (event.type === 'tool_execution_start') {
        toolStartedAt.set(event.toolCallId, Date.now());
        send({ type: 'phase', phase: 'tool_running', label: `正在执行 ${event.toolName}` });
        send({
          type: 'tool_call',
          id: event.toolCallId,
          name: event.toolName,
          arguments: JSON.stringify(event.args || event.input || {}),
        });
      } else if (event.type === 'tool_execution_end') {
        const output = toolResultText(event.result);
        const details = event.result?.details || event.result || {};
        const durationMs = toolStartedAt.has(event.toolCallId) ? Date.now() - toolStartedAt.get(event.toolCallId) : null;
        send({
          type: 'tool_result',
          id: event.toolCallId,
          toolName: event.toolName,
          summary: output.slice(0, 1000) || (event.isError ? '工具执行失败' : '工具执行完成'),
          status: event.isError ? 'failed' : 'completed',
          output,
          error: event.isError ? output || '工具执行失败' : '',
          exitCode: Number.isInteger(details.exitCode) ? details.exitCode : null,
          durationMs,
        });
        send({ type: 'phase', phase: event.isError ? 'failed' : 'waiting', label: event.isError ? '工具执行失败' : '等待 Agent 响应' });
      } else if (event.type === 'agent_end') {
        sawAgentEnd = true;
        send({ type: 'agent_end' });
        send({ type: 'phase', phase: 'done', label: '执行完成' });
      } else if (event.type === 'error') {
        send({ type: 'phase', phase: 'failed', label: '执行失败' });
        send({ type: 'error', content: normalizeRuntimeError(event.error?.message || event.message || 'PI Agent 执行失败') });
      }
    });
    await session.prompt(promptEnvelope(body));
    unsubscribe();
    if (!sawAgentEnd) send({ type: 'error', content: 'PI Agent 未返回 agent_end 事件' });
    send({ type: 'done', content: '' });
  } catch (error) {
    send({ type: 'error', content: normalizeRuntimeError(error) });
    send({ type: 'done', content: '' });
  } finally {
    session?.dispose();
    res.end();
  }
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    const kgConfigured = Boolean(process.env.KG_AGENT_MCP_URL && process.env.KG_AGENT_MCP_TOKEN && process.env.PI_AGENT_INTERNAL_TOKEN);
    const ready = process.env.PI_KG_ONLY !== 'true' || kgConfigured;
    return json(res, ready ? 200 : 503, { status: ready ? 'ok' : 'unavailable', runtime: 'pi-sdk', kgConfigured });
  }
  if(req.method==='POST' && req.url==='/v1/kg/relevance/run') {
    const expected=Buffer.from(process.env.PI_AGENT_INTERNAL_TOKEN||'local-pi-runtime');
    const actual=Buffer.from(String(req.headers['x-pi-internal-token']||''));
    if(actual.length!==expected.length || !timingSafeEqual(actual,expected))return json(res,403,{detail:'PI internal authentication required'});
    try{return json(res,200,await runKgTask(await readBody(req)));}
    catch(error){
      const message=String(error?.message||'');
      const detail=message.startsWith('Missing MCPHub environment configuration for ')?message:
        message.startsWith('KG MCP') || message.startsWith('KG scene') || message.startsWith('PI KG task')?message:'PI KG task failed';
      return json(res,502,{detail});
    }
  }
  if (process.env.PI_KG_ONLY === 'true') return json(res, 404, {detail: 'Not found'});
  const match = req.url?.match(/^\/v1\/sessions\/([^/]+)\/stream$/);
  if (req.method === 'POST' && match) {
    try {
      const body = await readBody(req);
      body.sessionId ||= decodeURIComponent(match[1]);
      return await handleStream(req, res, body);
    } catch (error) {
      return json(res, 400, { detail: error instanceof Error ? error.message : 'Invalid request' });
    }
  }
  return json(res, 404, { detail: 'Not found' });
});

server.listen(port, process.env.PI_RUNTIME_HOST || '127.0.0.1', () => {
  process.stdout.write(`PI SDK runtime listening on ${process.env.PI_RUNTIME_HOST || '127.0.0.1'}:${port}\n`);
});
