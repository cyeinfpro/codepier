import {pageCommand} from './page.js';

export function failure(code,message){return Object.assign(new Error(message),{code});}
export function siteOf(value){
  if(typeof value!=='string'||value.length>4096||/[\s\\\u0000-\u001f\u007f]/u.test(value))throw failure('BROWSER_ORIGIN_DENIED','网址格式无效');
  let u;try{u=new URL(value);}catch{throw failure('BROWSER_ORIGIN_DENIED','网址格式无效');}
  if(!['http:','https:'].includes(u.protocol)||u.username||u.password)throw failure('BROWSER_ORIGIN_DENIED','只支持无凭据的 HTTP(S) 网址');
  return u.origin;
}
const uuid=()=>crypto.randomUUID().replaceAll('-','');
const idPattern=/^[a-f0-9]{32}$/;

/** Remote calls never create, activate or select a tab. Only the local popup
 * may provision an idle pool, and only in a naturally focused browser window. */
export class BrowserWorkspace{
  constructor(api=chrome,clock=()=>Date.now()){
    this.api=api;this.clock=clock;this.tail=Promise.resolve();this.state=null;
  }
  serial(fn){const job=this.tail.then(fn,fn);this.tail=job.catch(()=>{});return job;}
  async load(){
    if(this.state)return this.state;
    let saved=(await this.api.storage.local.get('codepierWorkspace')).codepierWorkspace;
    let migrated=false;
    if(saved==null){saved=(await this.api.storage.local.get('relayWorkspace')).relayWorkspace;migrated=saved!=null;}
    this.state=saved&&saved.version===1?saved:{version:1,profile_id:uuid(),origins:[],pool:[],seen:[]};
    if(!idPattern.test(this.state.profile_id)||!Array.isArray(this.state.pool)||!Array.isArray(this.state.origins)||!Array.isArray(this.state.seen))throw failure('BROWSER_DOCUMENT_UNAVAILABLE','扩展状态无效，请在本机重置扩展');
    await this.save();
    if(migrated&&this.api.storage.local.remove)await this.api.storage.local.remove('relayWorkspace');
    return this.state;
  }
  async save(){await this.api.storage.local.set({codepierWorkspace:this.state});}
  async counts(){
    await this.load();const live=[];
    for(const row of this.state.pool){try{
      const tab=await this.api.tabs.get(row.tab_id);
      // Keep active lease records for explicit, honest cleanup. Idle entries
      // no longer owned are relinquished, never navigated or closed remotely.
      // Chrome exposes pendingUrl until a newly created/reset idle page commits.
      // A pending user navigation takes precedence over the previous idle URL.
      if(row.lease_id||(!tab.active&&tab.windowId===row.window_id&&(tab.pendingUrl||tab.url)===this.api.runtime.getURL('idle.html')))live.push(row);
    }catch{/* Closed by user. */}}
    this.state.pool=live;await this.save();
    return {pool_size:live.length,available:live.filter(r=>!r.lease_id).length,leased:live.filter(r=>r.lease_id).length};
  }
  async localStatus(){await this.load();return {...await this.counts(),profile_id:this.state.profile_id,extension_id:this.api.runtime.id,origins:[...this.state.origins]};}
  async allow(origin){
    const site=siteOf(origin);if(new URL(origin).pathname!=='/'||new URL(origin).search||new URL(origin).hash)throw failure('BROWSER_ORIGIN_DENIED','授权只填写网站 origin，不包含路径');
    // Permission must already have been requested by the popup's trusted gesture.
    if(!await this.api.permissions.contains({origins:[site+'/*']}))throw failure('BROWSER_PERMISSION_REQUIRED','请先在扩展窗口确认该站点权限');
    await this.load();this.state.origins=[...new Set([...this.state.origins,site])].sort();await this.save();return this.localStatus();
  }
  async revoke(site){
    await this.load();this.state.origins=this.state.origins.filter(s=>s!==site);await this.save();
    // Revocation closes only owned, still-background leases; never user tabs.
    for(const row of [...this.state.pool])if(row.site===site)await this.release(row.lease_id);
    // Chrome match patterns do not distinguish ports: only remove the coarse
    // permission when no other exact allowed origin still requires it.
    const u=new URL(site);
    if(!this.state.origins.some(s=>{const v=new URL(s);return v.protocol===u.protocol&&v.hostname===u.hostname;}))await this.api.permissions.remove({origins:[site+'/*']});
    return this.localStatus();
  }
  async prepare(size){
    await this.load();if(!Number.isInteger(size)||size<1||size>16)throw failure('BROWSER_POOL_EMPTY','标签页数量应为 1–16');
    const window=await this.api.windows.getLastFocused();
    if(!window.focused||window.type!=='normal')throw failure('BROWSER_POOL_EMPTY','请在当前 Chrome 窗口中点击准备标签页');
    await this.counts();let group;
    const existing=this.state.pool.find(r=>r.window_id===window.id);
    if(existing){try{const tab=await this.api.tabs.get(existing.tab_id);if(tab.groupId>=0)group=tab.groupId;}catch{}}
    while(this.state.pool.length<size){
      const current=await this.api.windows.get(window.id);if(!current.focused)throw failure('BROWSER_POOL_EMPTY','焦点已经离开 Chrome，未继续创建标签页');
      const tab=await this.api.tabs.create({windowId:window.id,url:this.api.runtime.getURL('idle.html'),active:false});
      this.state.pool.push({tab_id:tab.id,window_id:window.id,lease_id:null,expires:0});await this.save();
      group=await this.api.tabs.group({...Number.isInteger(group)?{groupId:group}:{},tabIds:[tab.id]});
    }
    if(Number.isInteger(group))await this.api.tabGroups.update(group,{title:'CodePier',color:'grey',collapsed:true});
    return this.localStatus();
  }
  async authorized(url,origins){
    const site=siteOf(url);await this.load();
    if(!Array.isArray(origins)||!origins.includes(site)||!this.state.origins.includes(site))throw failure('BROWSER_ORIGIN_DENIED','该网站未同时获得 Agent 与本机扩展授权');
    if(!await this.api.permissions.contains({origins:[site+'/*']}))throw failure('BROWSER_PERMISSION_REQUIRED','浏览器尚未授予这个站点的访问权限');
    return site;
  }
  async owned(row){
    let tab;try{tab=await this.api.tabs.get(row.tab_id);}catch{throw failure('BROWSER_LEASE_MISSING','标签页已关闭');}
    if(tab.active)throw failure('BROWSER_TAB_CHANGED','该标签页已被用户选中，远程操作已让出控制');
    if(tab.windowId!==row.window_id)throw failure('BROWSER_TAB_CHANGED','标签页已移动到另一窗口');
    return tab;
  }
  async lease(id,origins){
    await this.load();const row=this.state.pool.find(r=>r.lease_id===id);
    if(!row)throw failure('BROWSER_LEASE_MISSING','租约不存在');
    if(row.expires<=this.clock()){await this.release(id);throw failure('BROWSER_LEASE_MISSING','租约已过期');}
    const tab=await this.owned(row);await this.authorized(tab.url,origins);
    if(tab.status!=='complete')throw failure('BROWSER_DOCUMENT_UNAVAILABLE','页面仍在加载，请重新读取页面');
    return {row,tab};
  }
  async release(id){
    await this.load();const row=this.state.pool.find(r=>r.lease_id===id);
    if(!row)return {released:true,tab_cleanup_confirmed:true};
    let confirmed=false;
    try{
      await this.owned(row);
      await this.api.tabs.update(row.tab_id,{url:this.api.runtime.getURL('idle.html')});
      Object.assign(row,{lease_id:null,expires:0,site:null});confirmed=true;
    }catch{
      // Do not touch a foreground, moved or manually closed tab. Remove our
      // ownership instead; status explicitly distinguishes this from cleanup.
      this.state.pool=this.state.pool.filter(r=>r!==row);
    }
    await this.save();return {released:true,tab_cleanup_confirmed:confirmed};
  }
  async expire(){await this.load();for(const row of [...this.state.pool])if(row.lease_id&&row.expires<=this.clock())await this.release(row.lease_id);}
  async page(tab,body,origins){
    const values=await this.api.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},world:'ISOLATED',func:pageCommand,args:[body,origins]});
    if(values.length!==1||!values[0].result)throw failure('BROWSER_DOCUMENT_UNAVAILABLE','页面未返回可验证结果');
    const reply=values[0].result;
    if(!reply.ok)throw failure(reply.code||'BROWSER_DOCUMENT_UNAVAILABLE',reply.message||'页面状态变化，请重新观察');
    return reply.data;
  }
  async execute(request){return this.serial(async()=>{
    await this.load();await this.expire();
    if(!request||!idPattern.test(request.request_id)||!idPattern.test(request.lease_id))throw failure('BROWSER_ACTION_UNSUPPORTED','请求编号无效');
    if(this.state.seen.includes(request.request_id))throw failure('BROWSER_ACTION_UNCERTAIN','这个请求已经接收过，不会重复操作');
    this.state.seen.push(request.request_id);this.state.seen=this.state.seen.slice(-256);await this.save();
    if(request.action==='close')return this.release(request.lease_id);
    if(request.action==='open'){
      const site=await this.authorized(request.url,request.origins);
      if(!Number.isFinite(request.expires)||request.expires*1000<=this.clock()||request.expires*1000>this.clock()+3601000)throw failure('BROWSER_LEASE_MISSING','租约期限无效');
      if(this.state.pool.some(r=>r.lease_id===request.lease_id))throw failure('BROWSER_ACTION_UNCERTAIN','租约已经存在');
      let row;
      for(const candidate of this.state.pool){
        if(candidate.lease_id)continue;
        try{const tab=await this.owned(candidate);if((tab.pendingUrl||tab.url)===this.api.runtime.getURL('idle.html')){row=candidate;break;}}catch{}
      }
      if(!row)throw failure('BROWSER_POOL_EMPTY','没有可用的后台标签页，请在扩展中准备标签页；远程调用不会创建窗口');
      Object.assign(row,{lease_id:request.lease_id,expires:request.expires*1000,ttl:request.expires*1000-this.clock(),site});await this.save();
      await this.api.tabs.update(row.tab_id,{url:request.url});return {opened:true,focus_changed:false};
    }
    if(!['snapshot','action'].includes(request.action))throw failure('BROWSER_ACTION_UNSUPPORTED','不支持的浏览器动作');
    const {row,tab}=await this.lease(request.lease_id,request.origins);
    const data=await this.page(tab,request,request.origins);
    if(request.action==='action'&&request.operation?.action==='navigate'){
      await this.authorized(request.operation.value,request.origins);await this.owned(row);
      await this.api.tabs.update(tab.id,{url:request.operation.value});
    }
    row.expires=this.clock()+Math.min(Math.max(row.ttl||30000,30000),3600000);await this.save();return data;
  });}
}
