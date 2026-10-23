import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { apply, turnText } from './index.mjs';

const state = await mkdtemp(join(tmpdir(), 'dsh-memory-gate-'));
const handlers = new Map();
const tools = new Map();
const ctx = { on: (name, fn) => handlers.set(name, fn),
  tools: { register: tool => tools.set(tool.name, tool) }, systemPrompt: { section: () => {} } };
apply(ctx, { python: '/usr/bin/python3', bridge: join(dirname(fileURLToPath(import.meta.url)), 'bridge.py'), state,
  sdk: process.env.BIANBU_MEMORY_SDK_PATH });
const signal = new AbortController().signal;
// Read the same isolated daemon independently: handler completion alone cannot
// prove that cancellation or compaction was durably recorded.
const inspect = code => JSON.parse(execFileSync('/usr/bin/python3', ['-c',
  `import sys,json\nsys.path.insert(0,sys.argv[2])\nfrom bianbu_agent_sdk.memory import MemoryClient,InstructionScope\nwith MemoryClient.connect(sys.argv[1]) as c:\n` + code,
  join(state, 'memory.sock'), process.env.BIANBU_MEMORY_SDK_PATH || '/usr/lib/python3/dist-packages'],
  { encoding: 'utf8' }));
const message = content => ({ content: [{ type: 'text', text: content }], source: { kind: 'user' } });
const user = '请记住我的名字是青禾839，是C++程序员，以后说话要通俗易懂。';
const agent = { id: 'gate-a', session: { header: {}, events: [
  { type: 'turn/start', data: { turn: 1 } },
  { type: 'user/message', data: message(user) },
  { type: 'user/message', data: { ...message('must not save injected context'), source: { kind: 'plugin' } } },
  { type: 'assistant/message', data: { turn: 1, message: message('收到') } },
] } };
try {
  assert.ok(tools.has('memory_save') && tools.has('memory_search'));
  const saved = await tools.get('memory_save').execute({ content:'2026年10月14日早上9点项目会议，晚上19点瑜伽练习', target:'memory' }, {agent,signal});
  assert.equal(JSON.parse(saved.result).success, true);
  const searched = await tools.get('memory_search').execute({query:'10月14日 项目会议 瑜伽'}, {agent,signal});
  assert.match(searched.result, /瑜伽/);
  const date = new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'})
    .format(new Date(Date.now()+86400000));
  const relative = await tools.get('memory_save').execute({content:'请记住明天早上9点日期验收518会议，晚上7点瑜伽',target:'memory'},{agent,signal});
  assert.equal(JSON.parse(relative.result).success,true);
  const dateRecall = await tools.get('memory_search').execute({query:'日期验收518'},{agent,signal});
  assert.ok(dateRecall.result.includes(date));
  assert.match(dateRecall.result,/早上9点.*晚上7点/);
  const structured = JSON.parse(dateRecall.result).records.find(r => r.text.includes('日期验收518'));
  assert.ok(structured.memory_id && structured.revision && structured.source);
  const revised = await tools.get('memory_update').execute({memory_id:structured.memory_id,
    expected_revision:structured.revision,content:'明天日期验收518改到早上10点',target:'memory'},{agent,signal});
  assert.equal(JSON.parse(revised.result).success,true);
  await assert.rejects(tools.get('memory_update').execute({memory_id:structured.memory_id,
    expected_revision:structured.revision,content:'过期修改',target:'memory'},{agent,signal}));
  const pendingDelete = JSON.parse((await tools.get('memory_remove').execute({memory_id:structured.memory_id},{agent,signal})).result);
  assert.equal(pendingDelete.code,'confirmation_required');
  assert.equal(pendingDelete.confirmation_expires_in_seconds,300);
  const repeatedDelete = JSON.parse((await tools.get('memory_remove').execute({memory_id:structured.memory_id},{agent,signal})).result);
  assert.equal(repeatedDelete.code,'confirmation_required');
  assert.notEqual(repeatedDelete.confirmation_message, pendingDelete.confirmation_message);
  const stillPresent = JSON.parse((await tools.get('memory_search').execute({query:'日期验收518'},{agent,signal})).result);
  assert.ok(stillPresent.records.some(r=>r.memory_id===structured.memory_id));
  const attempt = async m => handlers.get('agent/pre-step')({agent,turn:2,step:2,signal},
    async()=>({kind:'enter',messages:[m]}));
  assert.doesNotMatch(JSON.stringify(await attempt(message(pendingDelete.confirmation_message))), /Deletion result/);
  assert.doesNotMatch(JSON.stringify(await attempt({...message(repeatedDelete.confirmation_message),source:{kind:'plugin'}})), /Deletion result/);
  const realNow = Date.now;
  try {
    Date.now = () => realNow() + 301000;
    assert.match(JSON.stringify(await attempt(message(repeatedDelete.confirmation_message))), /confirmation_expired/);
  } finally { Date.now = realNow; }
  const renewed = JSON.parse((await tools.get('memory_remove').execute({memory_id:structured.memory_id},{agent,signal})).result);
  const changed = await tools.get('memory_update').execute({memory_id:structured.memory_id,
    expected_revision:structured.revision+1,content:'日期验收518修改确认测试',target:'memory'},{agent,signal});
  assert.equal(JSON.parse(changed.result).success,true);
  await assert.rejects(attempt(message(renewed.confirmation_message)), /confirmation_stale/);
  const finalDelete = JSON.parse((await tools.get('memory_remove').execute({memory_id:structured.memory_id},{agent,signal})).result);
  const confirmationResult = await handlers.get('agent/pre-step')({agent,turn:2,step:2,signal},
    async()=>({kind:'enter',messages:[message(finalDelete.confirmation_message)]}));
  assert.match(JSON.stringify(confirmationResult),/Deletion result/);
  const removedRecall = JSON.parse((await tools.get('memory_search').execute({query:'日期验收518'},{agent,signal})).result);
  assert.ok(!removedRecall.records.some(r=>r.memory_id===structured.memory_id));
  await handlers.get('agent/session-start')({agent,source:'startup'});
  await handlers.get('agent/session-start')({agent,source:'compact'});
  await handlers.get('agent/pre-step')({agent,turn:2,step:1,signal},async()=>({kind:'enter',messages:[message('运行水位测试')]}));
  await handlers.get('session/event')({id:agent.id,header:{},events:[
    {type:'turn/start',data:{turn:2}}, {type:'user/message',data:message('取消测试')} ]},
    {type:'turn/end', data:{turn:2, reason:{kind:'aborted'}}});
  await handlers.get('session/flush')();
  const cancelled = inspect(" print(json.dumps(c.sessions.get_context('dsh:gate-a',include_events=True)))");
  assert.ok(cancelled.events.some(e => e.event_type === 'task_started'));
  assert.ok(cancelled.events.some(e => e.event_type === 'task_failed' && e.payload.status === 'cancelled'));
  assert.ok(cancelled.events.some(e => e.event_type === 'checkpoint_marker'));
  const compactState = {id:'gate-compact',header:{},events:[
    {type:'compaction/summary',data:{compactionId:'compact-1',summary:[{type:'text',text:'保留压缩摘要'}],shadowedSeqs:[1,2]}},
  ]};
  handlers.get('session/event')(compactState,{type:'compaction/end',data:{compactionId:'compact-1',error:null}});
  await handlers.get('session/flush')();
  const compacted = inspect(" print(json.dumps(c.sessions.get_context('dsh:gate-compact',include_events=True)))");
  assert.equal(compacted.summary.summary.current_status, '保留压缩摘要');
  assert.ok(compacted.summary.summary.context_pointers.includes('dsh:gate-compact:event:1'));
  assert.ok(compacted.events.some(e => e.event_type === 'checkpoint_marker'));
  inspect(" c.instructions.put(text='IsolatedInstructionGate739',scope=InstructionScope.AGENT)\n print(json.dumps(True))");
  const instructionRecall = await handlers.get('agent/pre-step')({agent:{id:'gate-instruction'},signal},
    async()=>({kind:'enter',messages:[message('instruction injection gate')]}));
  assert.match(JSON.stringify(instructionRecall), /IsolatedInstructionGate739/);
  const automatic = { id:'gate-date-auto', session:{header:{},events:[
    {type:'turn/start',data:{turn:1}},
    {type:'user/message',data:message('请记住明天早上9点自动日期验收926会议，晚上7点瑜伽')},
    {type:'assistant/message',data:{turn:1,message:message('收到')}},
  ]}};
  await handlers.get('agent/turn-stopping')({agent:automatic,turn:1,signal});
  const autoRecall = await tools.get('memory_search').execute({query:'自动日期验收926'},{agent,signal});
  assert.ok(autoRecall.result.includes(date));
  assert.match(autoRecall.result,/早上9点.*晚上7点/);
  assert.equal(turnText(agent.session.events, 1).user, user);
  await handlers.get('agent/turn-stopping')({ agent, turn: 1, signal });
  await handlers.get('agent/turn-stopping')({ agent, turn: 1, signal });
  const recall = async query => handlers.get('agent/pre-step')({ agent: {id:'gate-b'}, signal },
    async () => ({ kind: 'enter', messages: [message(query)] }));
  const result = await recall('解释什么是编译器');
  assert.match(JSON.stringify(result), /通俗易懂/);
  assert.match(JSON.stringify(result), /untrusted-recalled-content/);
  assert.doesNotMatch(JSON.stringify(result), /must not save/);
  const sub = { id:'gate-sub', session:{ ...agent.session, header:{origin:'subagent'}, events:[
    {type:'turn/start',data:{turn:1}}, {type:'user/message',data:message('请记住我的名字是禁止子代理957')},
    {type:'assistant/message',data:{turn:1,message:message('收到')}},
  ] } };
  await handlers.get('agent/turn-stopping')({ agent:sub,turn:1,signal });
  const subRecall = await recall('禁止子代理957');
  assert.doesNotMatch(JSON.stringify(subRecall.messages.filter(m => m.source?.plugin === 'bianbu-memory')), /禁止子代理957/);
  console.log(JSON.stringify({ status:'passed', dsh:'0.1.0-rc.6', model_requests:0,
    checks:['turn_pairing','plugin_context_excluded','automatic_preference','cross_session_recall',
      'untrusted_context','duplicate_turn','subagent_write_suppressed','explicit_save_tool','search_tool',
      'relative_date_saved_absolute','two_activity_clocks_preserved','automatic_relative_date',
      'structured_recall','update_revision_conflict','deletion_confirmation_required',
      'session_start','compact_boundary','run_started','interrupted_turn','compaction_summary_checkpoint',
      'persisted_cancel_checkpoint','persisted_compaction_summary','instruction_memory_injection'] },null,2));
} finally {
  handlers.get('dispose')();
  // Temporary files are left to the OS until the child finishes draining.
}
