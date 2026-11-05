import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateRecovery } from '../extensions/stream-stall-recovery.mjs';
const usr = { role:'user', content:[{type:'text', text:'please do the task'}] };
const txt = (t='partial')=>({type:'text',text:t});
const er = (content, errorMessage='stream_incomplete: Upstream websocket closed before response.completed') => ({role:'assistant',stopReason:'error',errorMessage,content});
const tr = (id)=>({role:'toolResult',toolCallId:id,content:[txt('done')]});
const ev = (messages, outcome='error',pendingMessages=[]) => ({outcome,context:{contextMessages:messages,pendingMessages}});
const marker = {role:'custom',customType:'stream-stall-recovery',content:'resume'};
const cases = [
 ['partial text proceeds once', ev([usr,er([txt()])]),'text'],
 ['partial text persisted in replay prompt', ev([usr,er([txt('UNIQUE_PARTIAL_PREFIX')])]),'text'],
 ['completed tools proceed', ev([usr,er([{type:'toolCall',id:'call-a'}]),tr('call-a')]),'resolved-tools'],
 ['no previous user',ev([er([txt()])]),null],
 ['success is not retried',ev([usr,{role:'assistant',stopReason:'stop',content:[txt()]}]),null],
 ['aborted run is not retried',ev([usr,er([txt()])],'aborted'),null],
 ['pending queued messages veto',ev([usr,er([txt()])],'error',[usr]),null],
 ['no visible text veto',ev([usr,er([{type:'thinking',thinking:'hidden'}])]),null],
 ['non-transient errors veto',ev([usr,er([txt()],'401 unauthorized')]),null],
 ['missing tool result veto',ev([usr,er([{type:'toolCall',id:'call-a'}])]),null],
 ['unknown tool result veto',ev([usr,er([{type:'toolCall',id:'call-a'}]),tr('call-b')]),null],
 ['duplicate tool result veto',ev([usr,er([{type:'toolCall',id:'call-a'}]),tr('call-a'),tr('call-a')]),null],
 ['images veto',ev([usr,er([txt(),{type:'image',data:'...'}])]),null],
 ['last error must be terminal',ev([usr,er([txt()]),{role:'custom',customType:'other',content:'z'}]),null],
 ['cap reached',ev([usr,marker,marker,marker,er([txt()])]),null],
 ['two rescues still allowed',ev([usr,marker,marker,er([txt()])]),'text'],
 ['new user resets cap',ev([usr,marker,marker,marker,usr,er([txt()])]),'text'],
 ['never continue on unrelated errors',ev([usr,er([txt()],'context_length_exceeded')]),null],
];
for(const [name,event,expected] of cases){
 test(name,()=>{const result=evaluateRecovery(event);assert.equal(result?.kind??null,expected);if(result){assert.equal(result.continue,true);assert.equal(result.entries[0].display,false);assert.equal(result.entries[0].customType,'stream-stall-recovery');} if(name==='partial text persisted in replay prompt')assert.match(result.entries[0].content,/UNIQUE_PARTIAL_PREFIX/);})
}