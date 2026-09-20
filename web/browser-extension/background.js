import {BrowserWorkspace} from './workspace.js';
const workspace=new BrowserWorkspace();let port=null,connection='未连接',timer=null,connecting=false;
async function state(){return workspace.serial(()=>workspace.localStatus());}
async function announce(){if(port){const s=await state();port?.postMessage({type:'state',state:s});}}
async function connect(){
  if(port||connecting)return;
  connecting=true;
  try{
    const s=await state();port=chrome.runtime.connectNative('com.codepier.browser');const current=port;connection='已连接本机桥接';
    current.postMessage({type:'hello',profile_id:s.profile_id,state:s});
    current.onMessage.addListener(message=>{
      if(message.type==='status'){connection=message.message||message.state;return;}
      if(message.type!=='request')return;
      workspace.execute(message).then(data=>({ok:true,data}),error=>({ok:false,code:error.code||'BROWSER_DOCUMENT_UNAVAILABLE',message:error.code?error.message:'浏览器未返回确定结果'})).then(result=>{
        if(port===current){current.postMessage({type:'result',request_id:message.request_id,result});announce().catch(()=>{});}
      });
    });
    current.onDisconnect.addListener(()=>{
      void chrome.runtime.lastError;
      if(port===current){port=null;connection='未连接：请检查本机安装与档案绑定';clearTimeout(timer);timer=setTimeout(connect,5000);}
    });
  }catch{port=null;connection='本机桥接不可用';clearTimeout(timer);timer=setTimeout(connect,5000);}
  finally{connecting=false;}
}
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(sender.id!==chrome.runtime.id||sender.url!==chrome.runtime.getURL('popup.html'))return false;
  workspace.serial(async()=>{
    if(message.action==='status')return {...await workspace.localStatus(),connection};
    if(message.action==='prepare')return workspace.prepare(message.size);
    if(message.action==='allow')return workspace.allow(message.origin);
    if(message.action==='revoke')return workspace.revoke(message.origin);
    if(message.action==='release-all'){for(const row of [...(await workspace.load()).pool])if(row.lease_id)await workspace.release(row.lease_id);return workspace.localStatus();}
    throw new Error('Unknown popup action');
  }).then(data=>{respond({ok:true,data});announce().catch(()=>{});},error=>respond({ok:false,message:error.message}));
  return true;
});
chrome.alarms.create('codepier-maintenance',{periodInMinutes:1});
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='codepier-maintenance'){workspace.serial(()=>workspace.expire()).then(()=>announce()).catch(()=>{});connect();}});
chrome.runtime.onStartup.addListener(connect);chrome.runtime.onInstalled.addListener(connect);
connect();
