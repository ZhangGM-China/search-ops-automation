import { Type } from 'typebox';
import { defineTool } from '@earendil-works/pi-coding-agent';

const READ_ONLY_TOOLS = new Set([
  'knowledge-graph-get_kg_scene_context',
  'knowledge-graph-query_kg_tags',
  'knowledge-graph-query_kg_relationships',
  'knowledge-graph-query_kg_dimensions',
  'product-query-search_products',
  'product-query-list_products',
  'product-query-get_product_all_tags',
  'product-query-get_product_tag_trace',
  'product-query-get_product_stock',
  'product-query-get_product_tags',
]);

const TOOL_NAMES = {
  query_scene: 'knowledge-graph-get_kg_scene_context',
  query_tag: 'knowledge-graph-query_kg_tags',
  query_relationship: 'knowledge-graph-query_kg_relationships',
  query_dimension: 'knowledge-graph-query_kg_dimensions',
  search_products: 'product-query-search_products',
  list_products: 'product-query-list_products',
  get_product_all_tags: 'product-query-get_product_all_tags',
  get_product_tag_trace: 'product-query-get_product_tag_trace',
  get_product_stock: 'product-query-get_product_stock',
  get_product_tags: 'product-query-get_product_tags',
};

function parseSse(text) {
  if (String(text).trimStart().startsWith('{')) return JSON.parse(text);
  const line = String(text || '').split(/\r?\n/).find((item) => item.startsWith('data: '));
  if (!line) throw new Error('MCP response did not contain a JSON event');
  return JSON.parse(line.slice(6));
}

// Bound only the model-facing tool message. The full response remains in the
// evidence list for audit, digest calculation and execution-time replay.
export function compactForModel(value, budget = 12000) {
  const limit = Math.max(512, Number(budget) || 12000);
  const trim = (item, remaining) => {
    if (remaining <= 0) return '[truncated]';
    if (typeof item === 'string') return item.length > remaining ? `${item.slice(0, Math.max(0, remaining - 14))}[truncated]` : item;
    if (item === null || typeof item !== 'object') return item;
    if (Array.isArray(item)) {
      const out = [];
      let left = remaining;
      for (const row of item) {
        const next = trim(row, Math.min(left, 2400));
        const size = JSON.stringify(next).length;
        if (size + 2 > left && out.length) break;
        out.push(next); left -= size + 1;
      }
      if (out.length < item.length) out.push(`[${item.length - out.length} more rows omitted]`);
      return out;
    }
    const out = {};
    let left = remaining;
    for (const [key, child] of Object.entries(item)) {
      const keySize = key.length + 4;
      if (keySize >= left) break;
      const next = trim(child, Math.min(left - keySize, 1800));
      out[key] = next;
      left -= JSON.stringify(next).length + keySize;
    }
    if (Object.keys(out).length < Object.keys(item).length) out._omittedFields = true;
    return out;
  };
  return trim(value, limit);
}

export class McpReadClient {
  constructor({ url, token, fetchImpl = globalThis.fetch, maxToolCalls = 250, signal }) {
    this.url = String(url || '').trim();
    this.token = String(token || '').trim();
    this.fetchImpl = fetchImpl;
    this.maxToolCalls = maxToolCalls;
    this.calls = 0;
    this.nextId = 0;
    this.sessionId = '';
    this.tools = [];
    this.signal = signal;
  }

  async _request(method, params = {}) {
    const headers = {
      Authorization: `Bearer ${this.token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      ...(this.sessionId ? { 'Mcp-Session-Id': this.sessionId, 'MCP-Protocol-Version': '2025-06-18' } : {}),
    };
    const response = await this.fetchImpl(this.url, {
      method: 'POST', headers,
      body: JSON.stringify({ jsonrpc: '2.0', id: ++this.nextId, method, params }),
      signal: this.signal || AbortSignal.timeout(30000), redirect: 'error',
    });
    if (!response.ok) throw new Error(`KG MCP HTTP ${response.status}`);
    const sessionId = response.headers?.get?.('mcp-session-id');
    if (sessionId) this.sessionId = sessionId;
    const payload = parseSse(await response.text());
    if (payload.error) throw new Error('KG MCP protocol error');
    return payload.result || {};
  }

  async connect() {
    if (!this.url || !this.token) throw new Error('KG MCP configuration is missing');
    await this._request('initialize', {
      protocolVersion: '2025-06-18', capabilities: {},
      clientInfo: { name: 'aiops-kg-governance', version: '1.0' },
    });
    const notified = await this.fetchImpl(this.url, {method:'POST', headers:{Authorization:`Bearer ${this.token}`, 'Content-Type':'application/json', Accept:'application/json, text/event-stream', 'Mcp-Session-Id':this.sessionId, 'MCP-Protocol-Version':'2025-06-18'}, body:JSON.stringify({jsonrpc:'2.0',method:'notifications/initialized'}), signal:this.signal || AbortSignal.timeout(30000), redirect:'error'});
    if (!notified.ok) throw new Error('KG MCP initialization notification failed');
    let cursor; const seen=new Set();
    do {
      const result = await this._request('tools/list', cursor ? {cursor} : {});
      if (!Array.isArray(result.tools)) throw new Error('KG MCP tools list invalid');
      this.tools.push(...result.tools);
      cursor=result.nextCursor;
      if (cursor && seen.has(cursor)) throw new Error('KG MCP tools pagination incomplete');
      seen.add(cursor);
    } while(cursor);
    const writeTools = this.tools.filter((tool) => tool?.annotations?.readOnlyHint === false || !READ_ONLY_TOOLS.has(tool?.name));
    const required = ['query_scene','query_tag','query_relationship','query_dimension','search_products','list_products'].map(name=>TOOL_NAMES[name]);
    const missing = required.filter((name) => !this.tools.some((tool) => tool?.name === name && tool?.annotations?.readOnlyHint === true));
    if (missing.length) throw new Error(`KG MCP missing required read tools: ${missing.join(', ')}`);
    // Keep this explicit: the remote server may advertise writes, but this client never calls them.
    this.tools = this.tools.filter((tool) => READ_ONLY_TOOLS.has(tool?.name) && tool?.annotations?.readOnlyHint === true);
    return { tools: this.tools, ignoredWriteTools: writeTools.map((tool) => tool.name).filter(Boolean) };
  }

  async call(name, args) {
    if (!READ_ONLY_TOOLS.has(name)) throw new Error(`MCP tool is not allowed for KG governance: ${name}`);
    if (!this.tools.some(tool=>tool.name===name)) throw new Error('KG MCP read tool unavailable');
    if (this.calls >= this.maxToolCalls) throw new Error('KG MCP tool-call limit exceeded');
    this.calls += 1;
    const result = await this._request('tools/call', { name, arguments: args || {} });
    if (result.isError) {
      const message = String(result.structuredContent?.message || result.content?.[0]?.text || 'MCP tool failed').replaceAll(this.token,'[REDACTED]');
      return { error: { code: result.structuredContent?.code || 'MCP_TOOL_ERROR', message }, complete: false };
    }
    if (result.structuredContent) return result.structuredContent;
    const text=(result.content||[]).filter(c=>c.type==='text').map(c=>c.text).join('\n');
    try { return JSON.parse(text); } catch { throw new Error('KG MCP tool returned non-structured data'); }
  }
}

const string = (description) => Type.Optional(Type.String({ description }));
const queryArgs = Type.Object({ tag_name: Type.String({ minLength: 1 }), side: Type.Optional(Type.Union([Type.Literal('source'), Type.Literal('target'), Type.Literal('both')])) });

function schemaFor(name) {
  if (name === 'query_scene') return Type.Object({});
  if (name === 'query_tag') return Type.Object({ tag_name: Type.String({ minLength: 1 }) });
  if (name === 'query_relationship') return queryArgs;
  if (name === 'query_dimension') return Type.Object({ dimension_name: Type.String() });
  if (name === 'search_products') return Type.Object({ query: Type.String({ minLength: 1 }) });
  if (name === 'list_products') return Type.Object({ product_id:Type.String({minLength:1,description:'完整商品 SPU，多款可用逗号分隔；程序逐个查询并保留映射。'}) });
  if (name === 'get_product_all_tags' || name === 'get_product_tag_trace') return Type.Object({ record_id: Type.String({ minLength: 1 }) });
  if (name === 'get_product_stock' || name === 'get_product_tags') return Type.Object({ product_id: Type.String({ minLength: 1 }) });
  return Type.Object({});
}

function mcpArgs(name, args, context) {
  const { brand, sceneId, environment } = context;
  if (name === 'query_scene') return { brand, environment };
  if (name === 'query_tag') return { params: { nameAcc: args.tag_name, sceneId, pageNum: 1, pageSize: 30 } };
  if (name === 'query_relationship') {
    const common = { sceneId, pageNum: 1, pageSize: 30 };
    if (args.side === 'source') return { params: { ...common, tagNameAcc: args.tag_name } };
    if (args.side === 'target') return { params: { ...common, resTagName: args.tag_name } };
    return { params: { ...common, tagNameAcc: args.tag_name } };
  }
  if (name === 'query_dimension') return { params: { name: args.dimension_name, sceneId, pageNum: 1, pageSize: 30 } };
  if (name === 'search_products') return { brand, environment, keyword: args.query, pageNum: 1, pageSize: 10 };
  if (name === 'list_products') return { brand, environment, criteria: { productId: args.product_id, pageNo: 1, pageSize: args.page_size || 50 } };
  if (name === 'get_product_all_tags' || name === 'get_product_tag_trace') return { brand, environment, recordId: args.record_id };
  if(name==='get_product_tags') return {environment,productId:args.product_id};
  if(name==='get_product_stock') return {brand,productId:args.product_id};
  return { brand, environment, productId: args.product_id };
}

export function createKgReadTools(client, context) {
  const evidence = context.evidence || [];
  let callCount=0;
  async function pages(remoteName,params) {
    const rows=[];
    for(let page=1;page<=100;page++) {
      const result=await client.call(remoteName,{params:{...params,pageNum:page}});
      if(result.error) return result;
      if(!Array.isArray(result.data)) throw new Error('KG graph read lacks data array');
      rows.push(...result.data);
      const done=Number.isInteger(result.pages)?page>=result.pages:Number.isInteger(result.count)?rows.length>=result.count:result.data.length<params.pageSize;
      if(done)return {data:rows,complete:true};
      if(!result.data.length)break;
    }
    throw new Error('KG graph pagination incomplete');
  }
  const invoke = async (name, args) => {
    if (++callCount > (context.maxToolCalls ?? 24)) {context.onLimit?.();throw new Error('KG tool-call limit exceeded');}
    const remoteName = TOOL_NAMES[name];
    const remoteArgs = mcpArgs(name, args, context);
    try {
      let result;
      if(['query_tag','query_dimension','query_relationship'].includes(name)) {
        if(!context.sceneId)throw new Error('KG scene unavailable');
        if(name==='query_relationship') {
          const sides=args.side && args.side!=='both'?[args.side]:['source','target'];
          const rows=[];
          for(const side of sides) {
            const r=await pages(remoteName,mcpArgs(name,{...args,side},context).params);
            if(r.error){result=r;break;}
            rows.push(...r.data.filter(row=>(side==='source'?row.tagName:row.responseTagName)===args.tag_name));
          }
          result ||= {data:[...new Map(rows.map(r=>[r.id,r])).values()],complete:true};
        } else {
          result=await pages(remoteName,remoteArgs.params);
          if(!result.error)result.data=result.data.filter(row=>name==='query_tag'?(row.name||row.tagName)===args.tag_name:!args.dimension_name || (row.name||row.dimensionName)===args.dimension_name);
        }
        if(!result.error && result.data.some(row=>row.sceneId && String(row.sceneId)!==String(context.sceneId)))throw new Error('KG scene mismatch');
      } else if(name==='list_products') {
        const parts=[...new Set(args.product_id.split(/[,，]/).map(v=>v.trim()).filter(Boolean))];
        if(parts.length>30)throw new Error('Too many product IDs');
        const results=[];
        for(const spu of parts)results.push({spu,result:await client.call(remoteName,{brand:context.brand,environment:context.environment,criteria:{spu,pageNo:1,pageSize:100}})});
        result={requestedProductId:args.product_id,products:results};
      } else result=await client.call(remoteName,remoteArgs);
      const output = { ...result };
      const modelOutput = compactForModel(output);
      evidence.push({ name: name === 'query_tag' ? 'query_tag' : name === 'query_relationship' ? 'query_relationship' : name === 'query_dimension' ? 'query_dimension' : name, args, result: output });
      return { content: [{ type: 'text', text: JSON.stringify(modelOutput) }], details: output };
    } catch (error) {
      const output = { error: String(error.message || error), complete: false };
      evidence.push({ name, args, result: output });
      return { content: [{ type: 'text', text: JSON.stringify(output) }], details: output };
    }
  };
  return Object.keys(TOOL_NAMES).filter(name=>client.tools.some(t=>t.name===TOOL_NAMES[name]) && (name!=='get_product_tags'||context.brand==='summit') && (!['get_product_all_tags','get_product_tag_trace'].includes(name)||['atlas','harbor','riviera','lumen'].includes(context.brand)) && (name!=='get_product_stock'||['atlas','harbor','riviera'].includes(context.brand))).map((name) => defineTool({
    name, label: name, description: `${client.tools.find(t=>t.name===TOOL_NAMES[name])?.description || name}。经只读 MCP 调用，品牌和场景由任务绑定。query_relationship 默认双向查询，图谱查询自动读全并精确过滤。record_id 必须用 list_products 返回记录 id。`,
    parameters: schemaFor(name),
    execute: async (_toolCallId, args) => invoke(name, args),
  }));
}

export { READ_ONLY_TOOLS, TOOL_NAMES };
