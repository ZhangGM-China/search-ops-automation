import test from 'node:test';
import assert from 'node:assert/strict';
import { runKgTask } from '../src/kg-task.mjs';

test('KG task isolates tools/context and retains final JSON plus evidence',async()=>{
  const tools=['get_kg_scene_context','query_kg_tags','query_kg_relationships','query_kg_dimensions'].map(n=>({name:`knowledge-graph-${n}`,annotations:{readOnlyHint:true}}));
  let options;
  const session={messages:[],subscribe:()=>()=>{},dispose(){},abort(){},async prompt(){this.messages=[{role:'assistant',content:[{type:'text',text:'{"groups":[]}'}],usage:{totalTokens:12},stopReason:'stop'}];}};
  const client={tools,async connect(){},async call(name){return name.endsWith('get_kg_scene_context')?{sceneId:'s1'}:{data:[],pages:1};}};
  const result=await runKgTask({brand:'atlas',sceneId:'s1',systemPrompt:'ONLY KG',outputSchema:{type:'object'},userInput:{query:'花衬衫'},modelConfig:{model:'m',apiKey:'secret',baseUrl:'https://invalid.example/v1'}},{client,sessionFactory:async opts=>{options=opts;return session;}});
  assert.deepEqual(result.output,{groups:[]});assert.equal(result.usage.totalTokens,12);
  assert(options.tools.every(t=>!['bash','write','read','prepare_action'].includes(t.name)));
  assert.equal(result.toolResults.filter(c=>c.name==='query_relationship').length,1);
  assert(!JSON.stringify(result).includes('secret'));
});

test('missing trusted scene fails before model invocation',async()=>{
  let invoked=false;
  await assert.rejects(runKgTask({brand:'atlas',systemPrompt:'x',outputSchema:{},userInput:{},modelConfig:{model:'m',apiKey:'k',baseUrl:'https://a'}},{client:{tools:[],connect:async()=>{},call:async()=>({sceneId:''})},sessionFactory:async()=>{invoked=true;}}),/scene/i);
  assert.equal(invoked,false);
});

for (const stopReason of ['length','error','aborted','stop']) {
  test(`empty model ${stopReason} result cannot become a manual-review plan`,async()=>{
    const tools=['query_kg_tags','query_kg_relationships','query_kg_dimensions'].map(n=>({name:`knowledge-graph-${n}`,annotations:{readOnlyHint:true}}));
    const client={tools,async connect(){},async call(){return {data:[],pages:1};}};
    const session={messages:[{role:'assistant',content:[],stopReason,errorMessage:'private-provider-detail'}],async prompt(){},abort(){},dispose(){}};
    await assert.rejects(runKgTask({brand:'atlas',sceneId:'s1',systemPrompt:'x',outputSchema:{},userInput:{query:'羊绒衫'},modelConfig:{}},{client,sessionFactory:async()=>session}),e=>{
      assert(e.message.startsWith('PI KG task'));
      assert(!e.message.includes('private-provider-detail'));
      return true;
    });
  });
}
