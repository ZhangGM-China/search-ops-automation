import test from 'node:test';
import assert from 'node:assert/strict';
import { McpReadClient, createKgReadTools } from '../src/kg-mcp.mjs';

const names = ['knowledge-graph-get_kg_scene_context','knowledge-graph-query_kg_tags','knowledge-graph-query_kg_relationships','knowledge-graph-query_kg_dimensions','product-query-search_products','product-query-list_products','product-query-get_product_all_tags','product-query-get_product_tag_trace'];
const defs = names.map(name => ({name, annotations:{readOnlyHint:true}, inputSchema:{type:'object'}}));
test('transport retains session, handles SSE and refuses unlisted/write tools', async () => {
  const requests=[];
  const fetchImpl=async (_url,options) => {
    const req=JSON.parse(options.body);requests.push({req,headers:options.headers});
    const result=req.method==='initialize'?{protocolVersion:'2025-06-18'}:req.method==='tools/list'?{tools:[...defs,{name:'product-recall-control-add_products_to_blacklist',annotations:{readOnlyHint:false}}]}:{structuredContent:{data:[]}};
    return new Response(`event: message\ndata: ${JSON.stringify({jsonrpc:'2.0',id:req.id,result})}\n\n`,{headers:{'mcp-session-id':'session-test','content-type':'text/event-stream'}});
  };
  const client=new McpReadClient({url:'https://mcp.example/read',token:'secret',fetchImpl});
  await client.connect();
  assert.equal(requests.at(-1).headers['Mcp-Session-Id'],'session-test');
  await assert.rejects(client.call('product-recall-control-add_products_to_blacklist',{}),/not allowed/);
  await client.call(names[1],{params:{}});
  assert.equal(requests.at(-1).req.params.name,names[1]);
});

test('queries collect pages, exact-filter names and bind scene outside model arguments',async()=>{
  const calls=[];
  const client={tools:defs,call:async(name,args)=>{calls.push({name,args});return {data:[{id:'t'+args.params.pageNum,tagName:args.params.pageNum===1?'花衬衫':'花衬衫长款',sceneId:'s1'}],pages:2};}};
  const evidence=[];const tools=createKgReadTools(client,{brand:'atlas',sceneId:'s1',environment:'staging',evidence});
  const r=await tools.find(t=>t.name==='query_tag').execute('c',{tag_name:'花衬衫'});
  assert.equal(r.details.data.length,1);assert.equal(calls.length,2);
  assert(calls.every(c=>c.args.params.sceneId==='s1'));
  assert.equal(evidence[0].result.complete,true);
});

test('relationship lookup queries both directions and exact-filters responses',async()=>{
  const calls=[];const client={tools:defs,call:async(name,args)=>{calls.push(args);return {data:[{id:'r',tagName:'花衬衫',responseTagName:'印花衬衫',relationships:['902227138815852544']}],pages:1};}};
  const evidence=[];const tools=createKgReadTools(client,{brand:'atlas',sceneId:'s1',evidence});
  const result=await tools.find(t=>t.name==='query_relationship').execute('c',{tag_name:'花衬衫',side:'both'});
  assert.equal(calls.length,2);assert.equal(result.details.data.length,1);
  assert.deepEqual(evidence[0].args,{tag_name:'花衬衫',side:'both'});
});

test('failed graph reads and tool limit cannot become empty success',async()=>{
  const client={tools:defs,call:async()=>{throw Error('upstream unavailable');}};
  const evidence=[];const tools=createKgReadTools(client,{brand:'atlas',sceneId:'s',evidence,maxToolCalls:1});
  const tool=tools.find(t=>t.name==='query_tag');
  const first=await tool.execute('c',{tag_name:'花衬衫'});
  assert.equal(first.details.complete,false);assert(first.details.error);
  await assert.rejects(tool.execute('d',{tag_name:'裙子'}),/limit/);
});

test('product lookup respects upstream page limit and retains composite SPU mapping',async()=>{
  const calls=[];
  const client={tools:defs,call:async(name,args)=>{
    calls.push({name,args});
    assert(args.criteria.pageSize<=100);
    return {code:0,data:{list:[{id:args.criteria.spu,productName:'针织衫'}],total:1}};
  }};
  const evidence=[];const tools=createKgReadTools(client,{brand:'atlas',sceneId:'s1',environment:'production',evidence});
  const r=await tools.find(t=>t.name==='list_products').execute('c',{product_id:'C1,2,C1'});
  assert.deepEqual(calls.map(c=>c.args.criteria.spu),['C1','2']);
  assert.equal(r.details.products[1].result.data.list[0].id,'2');
  assert.equal(evidence[0].result.requestedProductId,'C1,2,C1');
});

test('product search samples ten rows without truncating graph evidence',async()=>{
  let sent;
  const client={tools:defs,call:async(name,args)=>{sent=args;return {code:0,data:{items:[],total:500}};}};
  const tools=createKgReadTools(client,{brand:'atlas',sceneId:'s1',environment:'production'});
  const r=await tools.find(t=>t.name==='search_products').execute('c',{query:'羊绒衫'});
  assert.equal(sent.pageSize,10);
  assert.equal(r.details.data.total,500);
});
