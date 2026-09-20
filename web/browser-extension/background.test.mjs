import test from 'node:test';
import assert from 'node:assert/strict';
const event=()=>({listeners:[],addListener(fn){this.listeners.push(fn);}});
test('concurrent startup events own exactly one native connection',async()=>{
 let unblock,connections=0;
 const gate=new Promise(resolve=>unblock=resolve);
 const chrome={runtime:{id:'a'.repeat(32),getURL:p=>'chrome-extension://'+'a'.repeat(32)+'/'+p,
  onMessage:event(),onStartup:event(),onInstalled:event(),
  connectNative(){connections++;return {postMessage(){},onMessage:event(),onDisconnect:event()};}},
  storage:{local:{get:()=>gate,set:async()=>{}}},tabs:{get:async()=>{throw Error('not used');}},
  alarms:{create(){},onAlarm:event()}};
 globalThis.chrome=chrome;
 await import('./background.js?concurrent-startup-test');
 const duplicate=chrome.runtime.onInstalled.listeners[0]();
 chrome.runtime.onStartup.listeners[0]();
 unblock({});await duplicate;
 for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));
 assert.equal(connections,1,'parallel startup must never leave an older native host consuming requests without replying');
 delete globalThis.chrome;
});
