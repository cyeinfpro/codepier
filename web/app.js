'use strict';
// No external scripts, telemetry, fonts or client-side token persistence.
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths={dashboard:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',device:'M3 4h18v13H3zM8 21h8M12 17v4',folder:'M3 6h6l2 2h10v12H3z',code:'m8 7-5 5 5 5m8-10 5 5-5 5m-3-12-2 20',audit:'M6 3h12v18H6zM9 7h6M9 11h6M9 15h4',plug:'M8 3v5m8-5v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v3',settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2',arrow:'m9 5 7 7-7 7',plus:'M12 5v14M5 12h14',refresh:'M20 7v5h-5M4 17v-5h5M19 9a7 7 0 0 0-12-4L4 8m1 7a7 7 0 0 0 12 4l3-3',logout:'M9 4H4v16h5M9 12h12m-4-4 4 4-4 4',shield:'m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6',close:'m6 6 12 12M6 18 18 6',menu:'M4 6h16M4 12h16M4 18h16',check:'m5 12 4 4L19 6',cloud:'M6 18a4 4 0 0 1-1-8 7 7 0 0 1 13-2 5 5 0 0 1 0 10z',ai:'M7 5h10v14H7zM10 9h4m-4 4h4M3 9h4m10 0h4M3 15h4m10 0h4M10 2v3m4-3v3m-4 14v3m4-3v3',activity:'M2 12h4l3-8 6 16 3-8h4',file:'M5 3h9l5 5v13H5zM14 3v6h5',search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14m5 12 6 6',save:'M4 3h13l3 3v15H4zM8 3v6h8V3M8 21v-7h8v7',copy:'M8 8h12v13H8zM4 16H2V2h13v3',download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',play:'m8 4 12 8-12 8z',stop:'M6 6h12v12H6z',history:'M4 4v5h5M5 7a9 9 0 1 1-2 8M12 7v5l4 2',key:'M8 4a5 5 0 1 0 0 10 5 5 0 0 0 0-10m4 9 9 8m-5-4 3-3m-6 0 3-3',warning:'m12 3 10 18H2zM12 9v5m0 3v1',git:'M7 3v11a4 4 0 0 0 4 4h4M7 3a2 2 0 1 0 0 4 2 2 0 0 0 0-4m10 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4m0 4v8M17 16a2 2 0 1 0 0 4 2 2 0 0 0 0-4',terminal:'m4 6 5 5-5 5m8 1h8',edit:'m4 15 12-12 5 5L9 20l-6 1zM13 6l5 5',trash:'M3 6h18M5 6l1 15h12l1-15M9 6V3h6v3M9 10v7m6-7v7',network:'M8 3h8v6H8zM2 16h7v5H2zM15 16h7v5h-7zM12 9v4M5 16v-3h14v3',lock:'M5 10h14v11H5zM8 10V6a4 4 0 0 1 8 0v4M12 14v3'};
const icon=(name,cls='')=>`<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]||paths.code}"/></svg>`;
const nav=[['native','terminal','CLI 会话','11'],['overview','dashboard','控制总览','01'],['devices','device','设备节点','02'],['projects','folder','项目映射','03'],['workbench','code','远程工作台','04'],['workflows','history','开发任务','05'],['audit','audit','操作审计','06'],['connect','plug','MCP 接入','07'],['diagnostics','activity','运行诊断','08'],['integrations','network','开发工具','12'],['artifacts','download','产物交付','09'],['settings','settings','系统设置','10']];
const S={session:null,page:'overview',overview:null,projects:[],devices:[],settings:null,events:null,renderSeq:0,auditMode:'operations',auditOffset:0,auditSource:'',auditStatus:'',auditQuery:'',auditNext:null,work:{project:'',path:'',sha:'',original:'',content:'',dirty:false,truncated:false,treePath:'.',treeOffset:0,treeNext:null,tree:[],task:'',operation:null,console:''},poll:null,modalCleanup:null};
const ACTIVE_STATES=['queued','running','reconnecting','cancelling','unknown'];
const TERMINAL_STATES=['succeeded','failed','cancelled','needs_review','interrupted'];
const stateNames={active:'进行中',blocked:'受阻',completed:'已验收',pending:'待处理',skipped:'已跳过',reconnecting:'重连恢复中',cancelling:'等待取消确认',needs_review:'需核实本机结果',succeeded:'已完成',failed:'失败',running:'执行中',queued:'排队中',unknown:'结果待核实',interrupted:'已中断',cancelled:'已取消',ok:'成功',denied:'已拒绝',started:'已开始'};
const badge=(state)=>`<span class="badge ${esc(state)}">${esc(stateNames[state]||state)}</span>`;
const onlineBadge=(v)=>`<span class="badge ${v?'':'offline'}"><i class="dot ${v?'online':'offline'}"></i>${v?'在线':'离线'}</span>`;
const timeText=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false}):'尚未连接';
const shortTime=t=>t?new Date(t*1000).toLocaleTimeString('zh-CN',{hour12:false}):'—';
const json=v=>JSON.stringify(v,null,2);
function sessionValue(key,value){try{if(value===undefined)return sessionStorage.getItem(key);if(value===null)sessionStorage.removeItem(key);else sessionStorage.setItem(key,value);}catch{/* Private browser storage may be disabled. In-memory task tracking still works. */}return null;}
function uid(){const b=new Uint8Array(16);crypto.getRandomValues(b);return Array.from(b,x=>x.toString(16).padStart(2,'0')).join('');}
function toast(message,error=false){
  const host=$('#toasts');if(!host)return;
  const text=String(message??'');
  const duplicate=$$('.toast',host).find(node=>node.classList.contains('error')===error&&$('.toast-message',node)?.textContent===text);
  if(duplicate)return duplicate;
  const origin=document.activeElement,node=document.createElement('div');
  node.className='toast'+(error?' error':'');
  node.setAttribute('role',error?'alert':'status');
  node.innerHTML=`${icon(error?'warning':'check')}<span class="toast-message"></span><button type="button" class="icon-btn toast-dismiss" aria-label="关闭提示">${icon('close')}</button>`;
  $('.toast-message',node).textContent=text;
  host.append(node);
  let timer=null,remaining=4700,started=0,hovered=false;
  const remove=()=>{
    clearTimeout(timer);const focused=node.contains(document.activeElement);node.remove();
    if(focused&&origin?.isConnected&&origin.getClientRects().length&&!origin.closest('[inert]'))origin.focus({preventScroll:true});
  };
  const stop=()=>{if(timer){clearTimeout(timer);timer=null;remaining=Math.max(0,remaining-(Date.now()-started));}};
  const resume=()=>{if(timer||error||hovered||node.contains(document.activeElement)||!node.isConnected)return;started=Date.now();timer=setTimeout(remove,remaining);};
  node.addEventListener('mouseenter',()=>{hovered=true;stop();});
  node.addEventListener('mouseleave',()=>{hovered=false;resume();});
  node.addEventListener('focusin',stop);
  node.addEventListener('focusout',()=>queueMicrotask(resume));
  $('.toast-dismiss',node).onclick=remove;
  resume();return node;
}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function networkState(text,online=false){if($('#event-state'))$('#event-state').textContent=text;if($('#event-dot'))$('#event-dot').className='dot '+(online?'online':'offline');}
function sessionChanged(){return Object.assign(new Error('登录状态已变化，请在当前会话中重试。'),{code:'SESSION_CHANGED'});}
function stopEvents(){S.events?.close();S.events=null;clearTimeout(S.eventTimer);S.eventTimer=null;}
function endSession(discard=false){
  window.CodePierIntegrations?.detach();S.integrations=null;sessionValue('codepier-integration-receipts',null);
  $('#toasts')?.replaceChildren();

  if(typeof chatDetach==='function')chatDetach(true);
  stopEvents();clearTimeout(S.poll);S.poll=null;S.session=null;S.renderSeq++;S.integrations=null;S.workflow=null;S.delivery=null;S.uiProjectQuery='';
  if(typeof stopComputerApprovals==='function')stopComputerApprovals();
  S.readGeneration=(S.readGeneration||0)+1;S.treeGeneration=(S.treeGeneration||0)+1;
  if(discard){resetWork('');S.work.operation=null;S.work.console='';S.taskSubmission=null;sessionValue('codepier-operation',null);sessionValue('codepier-task-submission',null);}
  renderLogin();
}
async function api(path,options={}){
  const {retrySafe=false,retryDelays=[500,1000,2000,4000],requestTimeout=15000,signal:externalSignal,...request}=options;
  const session=S.session,method=(request.method||'GET').toUpperCase();
  const safe=method==='GET'||retrySafe;
  const headers={...(request.body?{'Content-Type':'application/json'}:{}),...(session?.csrf?{'X-RD-CSRF':session.csrf}:{}),...request.headers};
  const delays=retryDelays;
  for(let attempt=0;;attempt++){
    if(session!==S.session)throw sessionChanged();
    let res,body;
    if(externalSignal?.aborted)throw new DOMException('Request cancelled','AbortError');
    const controller=new AbortController();
    const abort=()=>controller.abort();externalSignal?.addEventListener('abort',abort,{once:true});
    const timeout=setTimeout(()=>controller.abort(),requestTimeout);
    try{
      res=await fetch(path,{credentials:'same-origin',...request,headers,signal:controller.signal});
      try{body=await res.json();}catch{body=null;}
    }catch{/* A missing response is ambiguous for a submitted mutation. */}
    finally{clearTimeout(timeout);externalSignal?.removeEventListener('abort',abort);}
    if(externalSignal?.aborted)throw new DOMException('Request cancelled','AbortError');
    if(session!==S.session)throw sessionChanged();
    if(res){
      if(res.status===401&&path!='/api/login')endSession();
      if(res.ok&&body!==null&&body!==undefined)return body;
      if(!res.ok&&(!safe||![408,429,500,502,503,504].includes(res.status)||attempt>=delays.length)){
        const error=new Error(body?.error?.message||(typeof body?.error==='string'?body.error:'')||`请求失败（HTTP ${res.status}）`);
        if(body?.error&&typeof body.error==='object')Object.assign(error,body.error);
        error.status=res.status;throw error;
      }
    }
    if(!safe||attempt>=delays.length)throw Object.assign(new Error('网络暂时不可用；已提交的操作仍保留在云端。恢复连接后按原操作编号/幂等键核实，不要重复创建任务。'),{code:'NETWORK_UNCERTAIN'});
    networkState(`网络波动 · 正在恢复 ${attempt+1}/${delays.length}`);
    await pause(delays[attempt]);
  }
}
const post=(path,body={})=>api(path,{method:'POST',body:JSON.stringify(body)});
const tool=(name,args={})=>{
  const remote=!name.startsWith('operations_')&&!name.startsWith('projects_')&&!['workflows_get','workflows_list','diagnostics_get','artifacts_get','artifacts_list','activity_list','workflows_handoff'].includes(name);
  const arguments_={...args};
  if(remote&&!arguments_.idempotency_key)arguments_.idempotency_key='panel-'+uid();
  return api('/api/tools/call',{method:'POST',body:JSON.stringify({tool:name,arguments:arguments_}),retrySafe:true});
};
const workTarget=(project=S.work.project)=>({project,...project===S.work.project&&S.work.workspace_id?{workspace_id:S.work.workspace_id}:{}});
const wtool=(name,args={})=>tool(name,{...workTarget(),...args});
async function settled(promise){
  const session=S.session;
  const out=await promise;
  if(S.session!==session)throw sessionChanged();
  if(!out?.pending)return out;
  const id=out.operation_id;
  toast('操作已保存，正在等待本机或恢复连接；无需重新提交。');
  // Keep waiting on the receipt, not on the original POST. A failed GET can be
  // retried safely. No editor state is destroyed when connectivity changes.
  const deadline=Date.now()+31*60*1000;
  while(Date.now()<deadline&&S.session){
    if(S.session!==session)throw sessionChanged();
    let op;
    try{op=await tool('operations_wait',{operation_id:id,wait_seconds:8});}
    catch(error){
      if(!S.session||['OPERATION_NOT_FOUND','SESSION_CHANGED'].includes(error.code))throw error;
      networkState('操作 '+id.slice(0,8)+' · 网络恢复后继续');await pause(3000);continue;
    }
    if(!op.pending){
      if(op.state==='succeeded'&&op.result?.ok)return {operation_id:id,...op.result.data};
      const error=new Error((op.result?.error?.message||op.error||stateNames[op.state]||op.state)+' · 操作 '+id);error.code=op.result?.error?.code;error.operation_id=id;throw error;
    }
    networkState((stateNames[op.state]||op.state)+' · '+id.slice(0,8));
    await pause(400);
  }
  throw new Error('操作仍保存在审计中，编号 '+id+'；未重复提交。');
}
function download(name,text,type='application/json'){const u=URL.createObjectURL(new Blob([text],{type}));const a=document.createElement('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),3000);}
async function copy(text){try{if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(text);else{const el=document.createElement('textarea');el.value=text;el.style.cssText='position:fixed;opacity:0;';document.body.append(el);el.select();if(!document.execCommand('copy'))throw new Error('copy');el.remove();}toast('已复制');}catch{modal('复制内容',`<textarea id="copy-text" rows="5" readonly>${esc(text)}</textarea>`);$('#copy-text').select();}}
function closeModal(expected=null){
  if(expected&&$('.modal')!==expected)return;
  if(S.modalCleanup)S.modalCleanup();S.modalCleanup=null;
  $('#modal-root').innerHTML='';$('#app').inert=false;
  document.body.style.overflow=$('.sidebar.open')?'hidden':'';
  let focus=S.modalLastFocus;S.modalLastFocus=null;
  if(focus?.isConnected&&!focus.getClientRects().length)focus=focus.closest('.tool-menu')?.querySelector('summary');
  if(focus?.isConnected&&focus.getClientRects().length&&!focus.closest('[inert]'))focus.focus({preventScroll:true});
}
function modal(title,body,footer='',large=false,closable=true){
  const origin=$('.modal')?S.modalLastFocus:document.activeElement;
  closeModal();S.modalLastFocus=origin;
  $('#modal-root').innerHTML=`<div class="modal-backdrop"><section class="modal ${large?'large':''}" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1"><header class="modal-header"><h2 id="modal-title">${esc(title)}</h2>${closable?`<button class="icon-btn" data-action="close-modal" aria-label="关闭">${icon('close')}</button>`:''}</header><div class="modal-body">${body}</div>${footer?`<footer class="modal-footer">${footer}</footer>`:''}</section></div>`;
  document.body.style.overflow='hidden';$('#app').inert=true;
  const dialog=$('.modal');uiLabelFields(dialog);
  const key=e=>{if(e.key==='Escape'&&closable){e.preventDefault();closeModal(dialog);return;}uiTrapTab(e,dialog);};
  document.addEventListener('keydown',key);S.modalCleanup=()=>document.removeEventListener('keydown',key);
  // Establish focus while opening; a delayed callback could steal focus after
  // the user has already moved to another field or started submitting the form.
  const editable=$('input:not(:disabled),textarea:not(:disabled),select:not(:disabled)',dialog);
  (editable?.getClientRects().length?editable:uiFocusable(dialog)[0]||dialog).focus({preventScroll:true});
  return dialog;
}
function buttons(primary,label='保存'){return `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" id="${primary}">${esc(label)}</button>`;}
function notice(text,info=false){return `<div class="notice ${info?'info':''}">${icon(info?'shield':'warning')}<div>${text}</div></div>`;}
function empty(text,action='',label=''){return `<div class="empty">${icon('network')}<p>${esc(text)}</p>${action?`<button class="btn small" data-action="${action}">${esc(label)}</button>`:''}</div>`;}
function heading(title,eyebrow,subtitle='',actions=''){
  return `<header class="page-head"><div>${eyebrow?`<div class="eyebrow">${esc(eyebrow)}</div>`:''}<h1>${esc(title)}</h1>${subtitle?`<p>${esc(subtitle)}</p>`:''}</div>${actions?`<div class="actions">${actions}</div>`:''}</header>`;
}
const brand=`<div class="brand"><span class="brand-mark" aria-hidden="true">C</span><div><div class="brand-name">CodePier · 码头</div><span class="brand-sub">AI 与本地代码对接</span></div></div>`;
function appearanceControl(){
  const preference=window.CodePierAppearance?.getPreference()||'auto';
  const options=[['auto','跟随系统'],['light','浅色'],['dark','深色']].map(([value,label])=>`<option value="${value}"${value===preference?' selected':''}>${label}</option>`).join('');
  return `<label class="appearance-control"><span>外观</span><select class="appearance-select" data-ui-appearance aria-label="全站外观">${options}</select></label>`;
}
function renderLogin(configured=true){
  closeModal();document.title='登录 · CodePier';
  $('#app').innerHTML=`<main class="login-screen"><section class="login-art">${brand}<div class="eyebrow">CodePier / 远程开发</div><h1>连接你的<br><span>开发现场</span></h1><p>AI 与本地代码对接、任务停靠的地方。</p><div class="login-orbit" aria-hidden="true"><span class="brand-mark" aria-hidden="true">C</span><span class="login-orbit-caption">MCP · CodePier · AGENT</span></div></section><section class="login-panel"><div class="eyebrow">CodePier / 登录</div><h2>登录控制台</h2><p>使用管理员账号继续</p>${appearanceControl()}${!configured?notice('请先在服务器运行 <code>python -m hub init</code> 初始化账号。'):''}<form id="login-form"><div class="field"><label for="username">账号</label><input id="username" name="username" autocomplete="username" value="admin" required maxlength="80"></div><div class="field"><label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="256" placeholder="输入密码"></div><p id="login-error" class="error-text" role="alert"></p><button class="btn primary" type="submit">进入控制台 ${icon('arrow')}</button></form><div class="spacer"></div>${location.protocol==='http:'?notice('HTTP 连接未加密，请在可信网络或 SSH 转发中使用。'):'<span class="badge">HTTPS 连接</span>'}<div class="login-footer">自托管 · 由管理员控制</div></section></main>`;
  uiLabelFields($('#login-form'));
  $('#login-form').addEventListener('submit',async e=>{e.preventDefault();const b=$('button[type=submit]',e.target);b.disabled=true;$('#login-error').textContent='';try{const r=await post('/api/login',{username:$('#username').value,password:$('#password').value});S.session=r;await bootAuthenticated();}catch(err){if($('#login-error'))$('#login-error').textContent=err.message;else toast(err.message,true);}finally{b.disabled=false;}});
}
function renderShell(){
  const chosen=nav.find(x=>x[0]===S.page);
  const groups=[['工作区',['overview','devices','projects']],['开发',['native','workbench','integrations','workflows','artifacts','audit']],['系统',['connect','diagnostics','settings']]];
  const navigation=groups.map(([label,ids])=>`<div class="nav-label">${label}</div>${ids.map(id=>{const [,ico,title,n]=nav.find(row=>row[0]===id);return `<button class="${id===S.page?'active':''}" data-nav="${id}" ${id===S.page?'aria-current="page"':''}>${icon(ico)}<span>${title}</span><span class="nav-num">${n}</span></button>`;}).join('')}`).join('');
  const dock=[['overview','dashboard','总览'],['projects','folder','项目'],['native','terminal','CLI'],['workflows','history','任务']].map(([id,ico,label])=>`<button type="button" data-nav="${id}" aria-label="${label}">${icon(ico)}<span>${label}</span></button>`).join('');
  $('#app').innerHTML=`<a class="skip-link" href="#page">跳到主要内容</a><div class="shell"><aside class="sidebar" id="sidebar" aria-label="主导航"><button class="icon-btn mobile-close" data-action="toggle-menu" aria-label="收起菜单">${icon('close')}</button>${brand}<nav class="nav" aria-label="主导航">${navigation}</nav><div class="side-bottom">${appearanceControl()}<div class="transport"><div class="transport-top"><i class="dot offline" id="event-dot"></i><span id="event-state">正在连接实时通道…</span></div></div><div class="user-box"><span class="avatar">${esc(S.session.username?.[0]?.toUpperCase()||'A')}</span><div>${esc(S.session.username)}<br><small class="tiny">管理员</small></div><button class="icon-btn" data-action="logout" aria-label="退出登录">${icon('logout')}</button></div></div></aside><button class="sidebar-scrim" data-ui="close-menu" aria-label="关闭导航" tabindex="-1" hidden></button><main class="main"><header class="topbar"><div class="breadcrumb"><button class="icon-btn mobile-menu" data-action="toggle-menu" aria-controls="sidebar" aria-expanded="false" aria-label="展开菜单">${icon('menu')}</button><span class="breadcrumb-prefix">CodePier</span><span>/</span><span id="breadcrumb-page" aria-live="polite">${chosen[2]}</span></div><div class="top-right"><span class="clock" id="clock"></span><button class="quick-jump" data-ui="command" aria-label="快速前往页面或项目">${icon('search')}<span>快速前往</span><kbd>${/Mac|iPhone|iPad/.test(navigator.platform)?'⌘':'Ctrl'} K</kbd></button><button class="icon-btn" data-computer-use aria-label="桌面控制">${icon('device')}</button><button class="icon-btn" data-action="refresh" aria-label="刷新">${icon('refresh')}</button></div></header><div id="page" class="page" tabindex="-1"></div><nav class="mobile-dock" aria-label="移动快捷导航">${dock}<button type="button" data-action="toggle-menu" aria-controls="sidebar" aria-expanded="false" aria-label="更多页面">${icon('menu')}<span>更多</span></button></nav></main></div>`;
  updateClock();uiSetMenu(false,false);uiSyncNavigation();
}
function updateClock(){const n=$('#clock');if(n)n.textContent=new Date().toLocaleString('zh-CN',{hour12:false});}
async function loadBasics(){const [d,p]=await Promise.all([api('/api/devices'),api('/api/projects')]);S.devices=d.devices;S.projects=p.projects;}
function restoreTaskSubmission(){
  if(S.taskSubmission)return;
  try{const r=JSON.parse(sessionValue('codepier-task-submission')||'null');if(r&&typeof r.project==='string'&&typeof r.task==='string'&&typeof r.idempotency_key==='string')S.taskSubmission=r;}catch{/* Ignore corrupt browser storage. */}
}
async function bootAuthenticated(){
  S.work.operation=S.work.operation||sessionValue('codepier-operation');restoreTaskSubmission();
  renderShell();if(typeof startComputerApprovals==='function')startComputerApprovals();connectEvents();await renderPage();
  const authId=new URLSearchParams(location.search).get('authorize');if(authId&&S.session){await loadBasics();await consentModal(authId);}
}
function connectEvents(){
  stopEvents();const session=S.session,events=S.events=new EventSource('/api/events');
  const current=()=>S.events===events&&S.session===session;
  events.onopen=()=>{if(!current())return;networkState('实时通道已连接',true);if(typeof computerApprovalsConnection==='function')computerApprovalsConnection(true);loadBasics().then(()=>{if(current()&&['overview','devices','projects','audit','workflows'].includes(S.page)&&!$('.modal'))return renderPage(false);}).catch(()=>{});if(S.work.operation)pollTask().catch(()=>{});};
  events.onerror=()=>{if(current()){networkState('正在重新连接…');if(typeof computerApprovalsConnection==='function')computerApprovalsConnection(false);}};
  events.onmessage=e=>{
    if(!current())return;let m;try{m=JSON.parse(e.data);}catch{return;}
    if(m.type==='computer_approval'&&typeof refreshComputerApprovals==='function')refreshComputerApprovals();
    if(S.work.operation&&['output','operation'].includes(m.type)&&m.data?.id===S.work.operation)pollTask().catch(()=>{});
    if(['operation','device','project','workflow'].includes(m.type)){
      clearTimeout(S.eventTimer);S.eventTimer=setTimeout(()=>{if(current()&&['overview','devices','projects','workflows'].includes(S.page)&&!$('.modal'))renderPage(false).catch(error=>toast(error.message,true));},500);
    }
  };
}
async function navigate(page){
  if(page==='terminal')page='native';
  if(!S.session||!$('#page')||!nav.some(x=>x[0]===page))return;
  if(S.page==='workbench'&&S.work.dirty&&!confirm('文件尚未保存，草稿会保留在本标签页。继续离开？')){history.replaceState(null,'',location.pathname+location.search+'#'+S.page);return;}
  if(page==='integrations'&&S.page!==page)window.CodePierIntegrations?.inherit(S.page);
  const changed=page!==S.page;S.page=page;location.hash=page;
  $('#breadcrumb-page').textContent=nav.find(x=>x[0]===page)[2];uiSetMenu(false,false);uiSyncNavigation();
  await renderPage();
  if(changed&&S.page===page){window.scrollTo({top:0,behavior:'instant'});$('#page h1')?.focus({preventScroll:true});}
}
async function renderPage(showLoading=true){window.CodePierIntegrations?.detach();if(!S.session||!$('#page'))return;if(S.page==='native'){S.renderSeq++;try{await chatPage();}catch(e){if(S.session)$('#page').innerHTML=notice(esc(e.message))+`<button class="btn" data-action="refresh">重试</button>`;}return;}if(typeof chatDetach==='function')chatDetach();const seq=++S.renderSeq;const page=S.page;if(showLoading)$('#page').innerHTML='<div class="skeleton" role="status" aria-label="正在加载页面"></div>';try{let html='';if(page==='overview'){S.overview=await api('/api/overview');S.projects=S.overview.projects;S.devices=S.overview.devices;html=overviewHTML();}else if(page==='devices'){await loadBasics();html=devicesHTML();}else if(page==='projects'){await loadBasics();html=projectsHTML();}else if(page==='workbench'){await loadBasics();if(!S.work.project&&S.projects.length)S.work.project=S.projects[0].id;html=workbenchHTML();}else if(page==='workflows'){await loadBasics();html=await workflowsHTML(seq);}else if(page==='audit'){html=await auditHTML(seq);}else if(page==='connect'){S.settings=await api('/api/settings');await loadBasics();S.grants=(await api('/api/grants')).grants;html=connectHTML();}else if(page==='integrations'){await loadBasics();html=CodePierIntegrations.html();}else if(page==='diagnostics'){await loadBasics();html=await diagnosticsHTML(seq);}else if(page==='artifacts'){await loadBasics();html=await artifactsHTML(seq);}else if(page==='settings'){S.settings=await api('/api/settings');html=settingsHTML();}if(seq!==S.renderSeq||!S.session)return;const presentation=showLoading?null:uiCapturePage();$('#page').innerHTML=html;uiPageReady(showLoading,presentation);if(page==='workbench')bindWorkbench();if(page==='workflows')bindWorkflows();if(page==='audit')bindAudit();if(page==='integrations')CodePierIntegrations.bind();if(page==='diagnostics')bindDiagnostics();if(page==='artifacts')bindArtifacts();if(page==='settings')bindSettings();}catch(e){if(seq===S.renderSeq&&S.session)$('#page').innerHTML=notice(esc(e.message))+`<div class="spacer"></div><button class="btn" data-action="refresh">重试</button>`;}}
function operationsTable(rows){if(!rows.length)return empty('还没有操作记录。');return `<div class="table-scroll" role="region" aria-label="操作记录，可滚动查看更多" tabindex="0"><table class="data-table"><thead><tr><th>操作</th><th>项目 / 范围</th><th>调用来源</th><th>时间</th><th>状态</th><th></th></tr></thead><tbody>${rows.map(r=>{const lifecycle=r.tool?.startsWith('agent_');return `<tr><td><div class="table-tool">${icon(lifecycle?'device':r.tool?.startsWith('fs_')?'file':r.tool?.startsWith('tasks_')?'terminal':'code')}<span class="mono">${esc(r.tool)}</span></div></td><td>${esc(r.alias||(lifecycle?'节点管理':'目录验证'))}</td><td><span class="badge ${r.actor?.startsWith('mcp:')?'purple':'neutral'}">${r.actor?.startsWith('mcp:')?'MCP':'面板'}</span></td><td class="muted mono tiny">${esc(shortTime(r.created))}</td><td>${badge(r.state)}</td><td><button class="icon-btn" data-action="operation-detail" data-id="${esc(r.id)}" aria-label="查看操作详情">${icon('arrow')}</button></td></tr>`;}).join('')}</tbody></table></div>`;}
function overviewHTML(){
  const o=S.overview;
  const stats=[['在线节点',o.online_devices,`/ ${o.devices.length}`,'device',o.online_devices?'连接已建立':'等待设备上线','is-highlight'],['映射项目',o.projects.length,'','folder','本机项目',''],['今日操作',o.today_operations,'','activity',`成功 ${o.today_succeeded} · 失败 ${o.today_failed}`,''],['执行中',o.active_operations,'','terminal',`${o.tool_count} 项工具可用`,'']];
  return heading('控制总览','CodePier 工作区','',`<button class="btn ghost" data-nav="workbench">${icon('code')}工作台</button><button class="btn primary" data-action="add-device">${icon('plus')}接入设备</button>`)+
    (o.today_failed?`<div class="attention-strip"><span>${icon('warning')} 今日 ${o.today_failed} 项操作失败</span><button class="btn ghost small" data-nav="audit">查看记录 ${icon('arrow')}</button></div>`:'')+
    `<div class="overview-grid workspace-overview editorial-board">
      <section class="panel codepier-focus-card overview-summary" aria-label="工作区概览"><div class="overview-summary-heading"><h2>工作区概览</h2><span class="editorial-label">CodePier</span></div><div class="stats workspace-stats" aria-label="工作区统计">${stats.map(([label,value,suffix,ico,foot,tone])=>`<article class="stat workspace-stat ${tone}"><div class="stat-top"><span>${label}</span>${icon(ico)}</div><div class="stat-value">${esc(value)}<small>${esc(suffix)}</small></div><div class="stat-foot">${esc(foot)}</div></article>`).join('')}</div></section>
      <section class="panel workspace-operations"><div class="panel-head"><h2>最近操作</h2><button class="btn ghost small" data-nav="audit">查看全部 ${icon('arrow')}</button></div>${operationsTable(o.recent_operations.slice(0,6))}</section>
      <section class="panel overview-projects workspace-recent-projects"><div class="panel-head"><h2>项目入口</h2><button class="icon-btn" data-nav="projects" aria-label="查看全部项目">${icon('arrow')}</button></div>${o.projects.length?o.projects.slice(0,4).map((p,index)=>`<div class="project-mini"><span class="project-index">${String(index+1).padStart(2,'0')}</span><div class="project-mini-main"><h3>${esc(p.alias)}</h3><p title="${esc(p.root)}">${esc(p.root)}</p><small>${esc(p.device_name)} · ${p.online?'在线':'离线'}</small></div><button class="icon-btn" data-action="open-project" data-id="${esc(p.id)}" aria-label="打开 ${esc(p.alias)}">${icon('arrow')}</button></div>`).join(''):empty('添加项目，开始远程开发。','add-project','添加项目')}</section>
    </div>`+uiHelp('统计与连接说明',`按 ${esc(o.timezone)} 统计今日操作。结果以 Agent 回传为准；调用来源按凭据记录。${location.protocol==='http:'?'当前浏览器 HTTP 通道未加密，请在可信网络使用。':''}`);
}
function devicesHTML(){
  const cards=S.devices.map(d=>{
    const info=d.info||{},roots=Array.isArray(info.roots)?info.roots:[];
    const rootRows=roots.map(root=>{const item=typeof root==='string'?{path:root}:root||{},writable=item.writable===undefined?null:item.writable!==false;return `<div class="device-root-row"><code>${esc(item.path||'未声明路径')}</code><div>${writable===null?'<span class="badge neutral">权限未声明</span>':`<span class="badge ${writable?'purple':'neutral'}">${writable?'可编辑':'只读'}</span>`}${item.allow_tasks?'<span class="badge neutral">可执行</span>':''}</div></div>`;}).join('');
    const state=!d.enabled?'disabled':d.online?'online':'offline';
    return `<article class="panel device-card workspace-device" data-device-state="${state}"><header class="workspace-device-head"><div class="device-symbol workspace-device-symbol"><span>${icon('device')}</span></div><div class="workspace-device-title"><div>${d.enabled?onlineBadge(d.online):'<span class="badge failed">已停用</span>'}</div><h2>${esc(d.name)}</h2><span class="mono tiny">${esc(String(d.id||'').slice(0,12).toUpperCase())}</span></div></header><dl class="card-meta workspace-device-facts"><div><dt>平台 / 主机</dt><dd>${esc(info.platform||'等待上线')} · ${esc(info.hostname||'—')}</dd></div><div><dt>映射项目</dt><dd>${esc(d.project_count)} 个</dd></div><div><dt>最近心跳</dt><dd>${esc(d.last_seen?shortTime(d.last_seen):'未连接')}</dd></div><div><dt>授权目录</dt><dd>${roots.length} 个</dd></div></dl>${agentLifecycleSummary(d)}<details class="device-roots workspace-device-roots"><summary>目录与权限 · ${roots.length}</summary><div class="device-root-list">${rootRows||'<p>设备上线后显示授权目录</p>'}</div></details><details class="device-raw workspace-device-raw"><summary>完整本机信息</summary><div class="code-box"><pre>${esc(json(info))}</pre></div></details><div class="actions agent-card-actions workspace-device-actions">${agentLifecycleButtons(d,true)}<button class="btn ghost small" data-action="device-detail" data-id="${esc(d.id)}">${icon('settings')}完整管理</button><button class="btn ghost small" data-action="add-project" data-device="${esc(d.id)}">${icon('folder')}映射项目</button></div></article>`;
  }).join('');
  return heading('设备节点','本机连接','',`<button class="btn primary" data-action="add-device">${icon('plus')}接入设备</button>`)+
    `<section class="cards device-cards workspace-device-collection">${cards||empty('还没有设备。','add-device','接入设备')}</section>`+
    uiHelp('节点与 Agent 管理','节点主动连接 Hub，无需开放家里入站端口。受管 Agent 可从这里校验并更新、重启或卸载；更新只替换运行时并保留配置、授权目录、状态和日志。卸载 Agent 与移除面板节点是两个独立操作，都不会删除项目文件。');
}
function projectsHTML(){
  const rows=S.projects.map((p,index)=>`<article class="panel workspace-project-row" data-project-row data-project-search="${esc((p.alias+' '+p.root+' '+p.device_name+' '+(p.description||'')).toLocaleLowerCase())}"><header class="workspace-project-head"><span class="project-index" aria-hidden="true">${icon("folder")}</span><div><span class="project-cover-label">PROJECT</span><h2>${esc(p.alias)}</h2>${p.description?`<p>${esc(p.description)}</p>`:''}</div>${onlineBadge(p.online)}</header><div class="workspace-project-path"><span>本机路径</span><code title="${esc(p.root)}">${esc(p.root)}</code></div><dl class="workspace-project-facts"><div><dt>设备</dt><dd>${esc(p.device_name)}</dd></div><div><dt>文件权限</dt><dd><span class="badge ${p.mode==='write'?'purple':'neutral'}">${p.mode==='write'?'可编辑':'只读'}</span></dd></div><div><dt>任务执行</dt><dd><span class="badge neutral">${p.allow_tasks?'已授权':'未授权'}</span></dd></div></dl><footer class="workspace-project-actions"><div class="actions">${typeof chatProjectButtons==='function'?chatProjectButtons(p.id):''}<button class="btn primary small" data-action="open-project" data-id="${esc(p.id)}">工作台 ${icon('arrow')}</button></div><button class="icon-btn" data-action="edit-project" data-id="${esc(p.id)}" aria-label="编辑 ${esc(p.alias)} 项目映射">${icon('edit')}</button></footer></article>`).join('');
  return heading('项目映射','工作空间','',`<button class="btn primary" data-action="add-project">${icon('plus')}添加映射</button>`)+
    (S.projects.length?`<div class="project-search workspace-project-search"><label for="project-query">${icon('search')}<input id="project-query" type="search" aria-label="筛选项目" placeholder="搜索项目、设备或路径"></label><small id="project-count" role="status"></small></div>`:'')+
    `${S.projects.length?`<section class="workspace-project-collection">${rows}<div class="empty panel" id="project-no-match" hidden><p>没有匹配的项目</p><button type="button" class="btn ghost small" data-ui="clear-project-query">清除筛选</button></div></section>`:empty('接入设备后，映射本机项目目录。','add-project','添加映射')}`+
    uiHelp('别名与授权','通过别名访问项目，例如 Imago；不区分大小写。新增映射不会自动加入旧 MCP 授权，需要重新授权或创建凭据。');
}
async function addDevice(){return agentInstallSetup();}
function pairingModal(pairing){modal('设备密钥已生成',`${notice('这是设备连接凭据，不是 MCP 访问令牌。请保存配对文件；页面关闭后不能再次查看。')}<div class="spacer"></div><div class="code-box"><pre>${esc(json(pairing))}</pre></div><p class="form-note">家里电脑安装依赖后，使用下方命令导入；将授权目录换成你的实际路径。</p><div class="code-box"><pre>python -m agent init --pairing-file pairing.json --allow "D:/Projects"</pre></div>`,`<button class="btn ghost" data-action="close-modal">已保存</button><button class="btn primary" id="download-pairing">${icon('download')}下载 pairing.json</button>`);$('#download-pairing').onclick=()=>download('pairing.json',json(pairing));loadBasics().catch(()=>{});}
async function deviceDetail(id){
  const d=S.devices.find(device=>device.id===id);if(!d)return;
  const uninstall=d.agent?.can_uninstall?`<button class="btn danger small" data-action="agent-uninstall" data-id="${esc(d.id)}">${icon('trash')}卸载本机 Agent</button>`:'';
  const dialog=modal('管理节点 · '+d.name,`<div class="field"><label>节点名称</label><input id="device-rename" value="${esc(d.name)}" maxlength="80"></div><dl class="kv device-kv"><dt>节点编号</dt><dd><code>${esc(d.id)}</code></dd><dt>连接状态</dt><dd>${d.enabled?onlineBadge(d.online):'<span class="badge failed">已停用</span>'}</dd><dt>最近连接</dt><dd>${esc(timeText(d.last_seen))}</dd><dt>平台 / 主机</dt><dd>${esc(d.info.platform||'等待上线')} · ${esc(d.info.hostname||'—')}</dd><dt>项目映射</dt><dd>${d.project_count} 个</dd></dl>${agentLifecycleSummary(d)}<section class="node-manage-section"><div class="node-section-head"><div><h3>Agent 生命周期</h3><p>更新先校验完整新运行时，面板记录结果后才切换；节点有任务执行时不会强行更新或重启。</p></div></div><div class="actions">${agentLifecycleButtons(d,false)}</div></section><section class="node-manage-section"><h3>开发工具配置</h3><p class="form-note">按项目连接语言服务与后台浏览器。只进入配置引导，不自动安装、授权或重启。</p><div class="actions">${S.projects.filter(p=>p.device_id===d.id).map(p=>`<button class="btn ghost small" data-devtools="setup" data-project="${esc(p.id)}">${esc(p.alias)} · 配置引导</button>`).join('')||`<button class="btn ghost small" data-action="add-project" data-device="${esc(d.id)}">先添加项目映射</button>`}</div></section><details class="device-raw"><summary>本机声明与能力</summary><div class="code-box"><pre>${esc(json(d.info))}</pre></div></details><p class="form-note">更换 IP / 端口：在目标电脑运行 <code>python -m agent configure --hub http://新IP:新端口</code>。面板不能越过本机配置增加授权目录。</p><section class="danger-zone"><div><h3>节点与凭据</h3><p>卸载只清理目标电脑上的 Agent；“移除面板记录”只删除 Hub 中的节点身份。两者都不会删除项目文件。</p></div><div class="actions">${uninstall}<button class="btn small" id="toggle-device">${d.enabled?'停用节点':'启用节点'}</button><button class="btn small" id="rotate-device">轮换连接密钥</button><button class="btn danger small" id="delete-device">仅移除面板记录</button></div></section>`,buttons('rename-device','保存名称'),true);
  $('#rename-device',dialog).onclick=()=>busy($('#rename-device',dialog),async()=>{await api('/api/devices/'+id,{method:'PATCH',body:JSON.stringify({name:$('#device-rename',dialog).value})});closeModal(dialog);await renderPage();});
  $('#toggle-device',dialog).onclick=async()=>{if(d.enabled&&!confirm('停用会断开节点；已经开始的本机操作可能仍会继续。确认停用？'))return;await busy($('#toggle-device',dialog),async()=>{await api('/api/devices/'+id,{method:'PATCH',body:JSON.stringify({enabled:!d.enabled})});closeModal(dialog);await renderPage();});};
  $('#rotate-device',dialog).onclick=async()=>{if(!confirm('旧连接密钥会立刻失效，在线 Agent 将断开。需要在目标电脑更新配置或重新执行安装命令。继续？'))return;await busy($('#rotate-device',dialog),async()=>{const result=await post(`/api/devices/${id}/rotate`);pairingModal(result.pairing);});};
  $('#delete-device',dialog).onclick=async()=>{if(!confirm('仅移除面板中的节点身份？必须先移除它的项目映射；目标电脑上的 Agent 和项目文件都不会被删除。'))return;await busy($('#delete-device',dialog),async()=>{await api('/api/devices/'+id,{method:'DELETE'});closeModal(dialog);await renderPage();});};
}
async function projectModal(id=null,device=''){await loadBasics();if(!S.devices.length){toast('请先接入一台设备');return addDevice();}const p=S.projects.find(p=>p.id===id)||{};modal(id?'编辑项目映射':'添加项目映射',`<form id="project-form"><div class="form-row"><div class="field"><label>项目别名</label><input name="alias" required value="${esc(p.alias||'')}" placeholder="Imago" maxlength="64"></div><div class="field"><label>所属设备</label><select name="device_id">${S.devices.map(d=>`<option value="${d.id}" ${(p.device_id||device)===d.id?'selected':''}>${esc(d.name)} · ${d.online?'在线':'离线'}</option>`).join('')}</select></div></div><div class="field"><label>本机项目路径</label><input name="root" required value="${esc(p.root||'')}" placeholder="D:/Projects/Imago 或 /home/me/Projects/Imago"><small>必须在本机 allowed_roots 授权范围内，保存时将验证目录是否存在。</small></div><div class="field"><label>项目说明</label><input name="description" value="${esc(p.description||'')}" placeholder="可选，简要说明" maxlength="1000"></div><div class="field"><label>文件访问权限</label><select name="mode"><option value="write" ${p.mode==='read'?'':'selected'}>可读取和编辑</option><option value="read" ${p.mode==='read'?'selected':''}>只读</option></select></div><label class="check"><input name="allow_tasks" type="checkbox" ${p.allow_tasks?'checked':''}>允许本机任务及已授权的 Shell 执行</label><p class="form-note">执行还需要本机允许。若本机开启完整 Shell 权限，MCP 可执行任意命令，使用 Agent 系统用户的权限。</p></form>`,`${id?'<button class="btn danger" id="unmap-project">删除映射</button>':''}${buttons('save-project','验证并保存')}`);$('#save-project').onclick=async()=>{const f=$('#project-form');if(!f.reportValidity())return;await busy($('#save-project'),async()=>{const data=Object.fromEntries(new FormData(f));data.allow_tasks=f.elements.allow_tasks.checked;await api('/api/projects'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify(data)});closeModal();toast('映射已保存');await renderPage();});};if(id)$('#unmap-project').onclick=async()=>{if(!confirm('只删除映射，不会删除家里文件。已开始的操作不会自动撤销。继续？'))return;await busy($('#unmap-project'),async()=>{await api('/api/projects/'+id,{method:'DELETE'});closeModal();if(S.work.project===id)resetWork('');await renderPage();});};}
function resetWork(project,workspace_id=''){S.fileGeneration=(S.fileGeneration||0)+1;S.readGeneration=(S.readGeneration||0)+1;S.treeGeneration=(S.treeGeneration||0)+1;Object.assign(S.work,{project,workspace_id,pane:'files',path:'',sha:'',original:'',content:'',dirty:false,truncated:false,tree:[],treePath:'.',treeOffset:0,treeNext:null});}
function workbenchHTML(){
  const w=S.work,p=S.projects.find(p=>p.id===w.project);
  const more=`<button class="btn ghost" data-devtools="worktrees">${icon('folder')}隔离工作目录</button><button class="btn ghost" data-devtools="handoff">${icon('history')}任务衔接</button><button class="btn ghost" data-insight="symbols">${icon('code')}代码结构</button><button class="btn ghost" data-wf-action="context">${icon('file')}项目上下文</button><button class="btn ghost" data-action="checkpoint">${icon('history')}建立源码检查点</button>`;
  return heading('远程工作台','DEVELOPMENT / WORKBENCH','',`<button class="btn ghost" data-devtools="validation">${icon('check')}测试验收</button><button class="btn ghost" data-insight="search">${icon('search')}项目检索</button><button class="btn ghost" data-local-skills>${icon('file')}本地技能</button>${uiMenu('工具',more)}`)+
    `<div class="work-controls"><select id="work-project" aria-label="选择项目"><option value="">选择项目</option>${S.projects.map(p=>`<option value="${esc(p.id)}" ${p.id===w.project?'selected':''}>${esc(p.alias)} · ${p.online?'在线':'离线'}</option>`).join('')}</select>${p?onlineBadge(p.online):''}${w.workspace_id?`<span class="badge neutral" title="${esc(w.workspace_id)}">隔离目录 ${esc(w.workspace_id.slice(0,8))}</span><button class="btn ghost small" data-devtools="worktrees">管理工作目录</button>`:''}<span class="muted tiny">${esc(p?.device_name||'')} ${p?`/ ${p.mode==='write'?'可编辑':'只读'}`:''}</span><div class="actions">${S.work.project&&!S.work.workspace_id&&typeof chatProjectButtons==='function'?chatProjectButtons(S.work.project):''}<button class="btn ghost small" data-action="git-status">${icon('git')}Git 状态</button><button class="btn ghost small" data-action="git-diff">差异</button><button class="btn ghost small" data-action="history">${icon('history')}备份</button></div></div>
    <div class="tabs mobile-work-tabs" role="group" aria-label="工作区视图"><button data-ui-pane="files">${icon('folder')} 文件</button><button data-ui-pane="editor">${icon('code')} 编辑器</button></div>
    <section class="panel workspace" data-pane="${w.pane==='editor'?'editor':'files'}"><aside class="explorer"><div class="explorer-top"><span>文件目录</span><div><button class="icon-btn" data-action="new-file" aria-label="新建文件">${icon('plus')}</button><button class="icon-btn" data-action="tree-refresh" aria-label="刷新目录">${icon('refresh')}</button></div></div><form id="search-form" class="explorer-tools"><input name="query" placeholder="搜索文件内容" aria-label="搜索项目"><button class="icon-btn" aria-label="执行搜索">${icon('search')}</button></form><div id="file-tree" class="file-tree">${treeHTML()}</div></aside><div class="editor-wrap" id="editor-wrap">${editorHTML()}</div></section>
    <div class="taskbar"><select id="task-select" aria-label="选择本机任务"><option value="">选择本机任务</option></select><button class="btn ghost" data-action="load-tasks">刷新任务</button><button class="btn primary" data-action="run-task">${icon('play')}运行</button><button class="btn ghost" data-action="cancel-task">${icon('stop')}停止</button></div><section class="terminal"><div class="terminal-head"><span>${icon('terminal')}运行输出</span><span id="task-status">${w.operation?'操作 '+esc(w.operation.slice(0,12)):'等待任务'}</span></div><pre id="task-output">${esc(w.console||'运行任务后，输出将显示在这里。')}</pre></section>`+
    uiHelp('编辑与执行说明','文件保存前需确认差异并通过 SHA 检查，原内容自动备份。编辑器支持完整读取的 UTF-8 文本（上限 1 MiB）；分段读取的文件只读。此处运行本机具名任务；完整 Shell 通过已授权的 MCP <code>shell_exec</code> 调用。');
}
function treeHTML(){const w=S.work;return `<button class="file-node directory" data-action="tree-home">${icon('folder')}<span>${esc(w.treePath==='.'?'项目根目录':w.treePath)}</span></button>${w.tree.map(n=>`<button class="file-node ${n.type==='directory'?'directory':''} ${w.path===n.path?'selected':''}" style="padding-left:${10+Math.max(0,n.path.split('/').length-w.treePath.split('/').length-(w.treePath==='.'?0:0))*12}px" data-action="${n.type==='directory'?'tree-dir':'read-file'}" data-path="${esc(n.path)}">${icon(n.type==='directory'?'folder':'file')}<span>${esc(n.name)}</span></button>`).join('')}${w.treeNext!==null?`<button class="btn ghost small" data-action="tree-more">加载更多文件</button>`:''}${!w.tree.length?'<div class="empty tiny">选择项目后加载目录</div>':''}`;}
function editorHTML(){const w=S.work,p=S.projects.find(p=>p.id===w.project);if(!w.path)return `<div class="workspace-empty"><div>${icon('code')}<h3>选择一个文件</h3><p>浏览目录，或新建文件开始编辑。</p></div></div>`;return `<div class="editor-top"><div><span class="editor-path">${esc(w.path)}</span><span class="editor-status" id="dirty-state">${w.dirty?'未保存':w.truncated?'分段只读':'已同步'}</span></div><div class="actions"><button class="btn ghost small" data-devtools="navigation">代码导航</button><button class="btn ghost small" data-action="reload-file">重新读取</button><button class="btn ghost small" data-action="move-file">移动</button><button class="btn danger small" data-action="delete-file">删除</button><button class="btn primary small" data-action="preview-save" ${p?.mode!=='write'||w.truncated?'disabled':''}>${icon('save')}预览并保存</button></div></div>${w.truncated?notice('此文件超过编辑器读取行数上限，目前只显示前 20,000 行，已禁止保存。MCP 可继续按行读取。'):''}<div class="editor"><div class="editor-lines" id="editor-lines" aria-hidden="true">${Array.from({length:Math.min(w.content.split('\n').length,20001)},(_,i)=>i+1).join('\n')}</div><textarea id="code-editor" aria-label="源码编辑器" spellcheck="false" autocapitalize="off" autocomplete="off" ${w.truncated||p?.mode!=='write'?'readonly':''}>${esc(w.content)}</textarea></div><div class="editor-bottom"><span>UTF-8 · ${esc(p?.alias||'')} · ${w.content.split('\n').length} 行</span><span id="file-sha">SHA ${esc(w.sha==='new'?'NEW FILE':w.sha.slice(0,16))}</span></div>`;}
function bindEditor(){const el=$('#code-editor');if(!el)return;el.value=S.work.content;el.oninput=()=>{S.editRevision=(S.editRevision||0)+1;S.work.content=el.value;S.work.dirty=el.value!==S.work.original||S.work.sha==='new';$('#dirty-state').textContent=S.work.dirty?'未保存':'已同步';$('#editor-lines').textContent=Array.from({length:Math.min(el.value.split('\n').length,20001)},(_,i)=>i+1).join('\n');};el.onscroll=()=>{$('#editor-lines').scrollTop=el.scrollTop;};el.onkeydown=e=>{if(e.key==='Tab'&&!el.readOnly){e.preventDefault();const a=el.selectionStart,b=el.selectionEnd;el.setRangeText('  ',a,b,'end');el.dispatchEvent(new Event('input'));}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();previewSave().catch(err=>toast(err.message,true));}};}
function bindWorkbench(){bindEditor();$('#work-project').onchange=async e=>{if(S.work.dirty&&!confirm('切换项目将丢弃尚未保存的草稿，继续？')){e.target.value=S.work.project;return;}resetWork(e.target.value);await renderPage(false);};$('#search-form').onsubmit=async e=>{e.preventDefault();const q=new FormData(e.target).get('query').trim();if(q)await searchFiles(q);};if(S.work.project&&!S.work.tree.length)loadTree().catch(e=>toast(e.message,true));if(S.work.operation)pollTask().catch(()=>{});}
async function loadTree(more=false){if(!S.work.project)return;const project=S.work.project,generation=S.treeGeneration=(S.treeGeneration||0)+1;const r=await settled(wtool('fs_tree',{path:S.work.treePath,depth:2,offset:more?S.work.treeNext||0:0,limit:300}));if(project!==S.work.project||generation!==S.treeGeneration)return;S.work.tree=more?S.work.tree.concat(r.entries):r.entries;S.work.treeNext=r.next_offset;if($('#file-tree'))$('#file-tree').innerHTML=treeHTML();}
async function readFile(path,force=false){
  if(!S.work.project)throw new Error('请先选择项目');
  if(S.work.dirty&&!force&&!confirm('读取其他文件将丢弃尚未保存的草稿，继续？'))return false;
  const project=S.work.project,revision=S.editRevision||0,generation=S.readGeneration=(S.readGeneration||0)+1;
  const r=await settled(tool('fs_read',{...workTarget(project),path,max_lines:20000}));
  if(project!==S.work.project||generation!==S.readGeneration)return false;
  if(revision!==(S.editRevision||0)){toast('读取期间有新的编辑，已保留当前草稿。请保存后重新读取。');return false;}
  S.fileGeneration=(S.fileGeneration||0)+1;
  Object.assign(S.work,{path:r.path,sha:r.sha256,content:r.content,original:r.content,dirty:false,truncated:r.truncated});
  if($('#editor-wrap')){$('#editor-wrap').innerHTML=editorHTML();bindEditor();$('#file-tree').innerHTML=treeHTML();}
  uiSetPane('editor',true);
  return true;
}
async function newFile(){if(!S.work.project)throw new Error('请先选择项目');modal('新建文件',`<div class="field"><label>项目内相对路径</label><input id="new-path" placeholder="例如 src/example.py"><small>父目录必须已存在；需要新目录时可使用下方按钮。</small></div><button class="btn ghost small" id="mkdir">先创建目录</button>`,buttons('new-file-confirm','开始编辑'));$('#mkdir').onclick=()=>busy($('#mkdir'),async()=>{const path=$('#new-path').value.trim();if(!path)throw new Error('请在输入框填写要创建的目录路径');await settled(wtool('fs_mkdir',{path,idempotency_key:uid()}));toast('目录已创建');await loadTree();});$('#new-file-confirm').onclick=()=>{const path=$('#new-path').value.trim();if(!path)return;if(S.work.dirty&&!confirm('丢弃当前未保存草稿？'))return;S.fileGeneration=(S.fileGeneration||0)+1;S.readGeneration=(S.readGeneration||0)+1;Object.assign(S.work,{path,sha:'new',original:'',content:'',dirty:true,truncated:false});closeModal();$('#editor-wrap').innerHTML=editorHTML();bindEditor();uiSetPane('editor',true);$('#code-editor')?.focus();};}
function diffHTML(d){return `<div class="diff-stats"><span>+ ${d.added_lines||0} 行</span><span>− ${d.removed_lines||0} 行</span>${d.diff_truncated?'<small>差异已截断，保存前请在本机核对完整内容</small>':''}</div><div class="code-box diff">${d.diff?d.diff.split('\n').map(l=>`<span class="${l.startsWith('+')?'plus':l.startsWith('-')?'minus':l.startsWith('@@')?'hunk':''}">${esc(l)}</span>`).join(''):'<span>没有内容差异</span>'}</div>`;}
async function previewSave(){
  const w=S.work;if(!w.path||w.truncated||S.previewing)return;
  const snapshot={...workTarget(w.project),path:w.path,expected_sha256:w.sha,content:w.content,idempotency_key:uid()},generation=S.fileGeneration||0;
  S.previewing=true;
  try{
    const d=await settled(tool('fs_preview',{project:snapshot.project,workspace_id:snapshot.workspace_id,path:snapshot.path,content:snapshot.content}));
    if(S.work.project!==snapshot.project||S.work.path!==snapshot.path||generation!==(S.fileGeneration||0))return;
    if(S.work.content!==snapshot.content){toast('预览期间内容已更新，请重新预览当前草稿。');return;}
    const dialog=modal('确认写入 · '+snapshot.path,`${diffHTML(d)}<p class="form-note">只有当前文件 SHA 仍与读取时一致，才会执行写入。修改前会保存本机备份。${d.diff_truncated?'当前差异预览不完整，请取消并在本机检查。':''}</p>`,buttons('confirm-save','确认写入'),true);
    const button=$('#confirm-save');button.disabled=!!d.diff_truncated;
    button.onclick=()=>busy(button,async()=>{
      const r=await settled(tool('fs_write',snapshot));
      if(S.work.project===snapshot.project&&S.work.path===snapshot.path&&generation===(S.fileGeneration||0)){
        S.work.sha=r.sha256;S.work.original=snapshot.content;S.work.dirty=S.work.content!==snapshot.content;
        if($('#editor-wrap')){$('#editor-wrap').innerHTML=editorHTML();bindEditor();}
      }
      closeModal(dialog);toast('已写回本机，修改前内容已备份');await loadTree();
    });
  }finally{S.previewing=false;}
}
async function deleteFile(){
  const snapshot={...workTarget(),path:S.work.path,sha:S.work.sha,content:S.work.content},generation=S.fileGeneration||0;
  if(!snapshot.path||snapshot.sha==='new')throw new Error('请先读取一个已存在的文件');
  if(!confirm(`删除 ${snapshot.path}？仅删除该文件，修改前会保存备份。`))return;
  await settled(tool('fs_delete',{project:snapshot.project,workspace_id:snapshot.workspace_id,path:snapshot.path,expected_sha256:snapshot.sha,idempotency_key:uid()}));
  if(S.work.project===snapshot.project&&S.work.path===snapshot.path&&generation===(S.fileGeneration||0)){
    S.fileGeneration=(S.fileGeneration||0)+1;S.readGeneration=(S.readGeneration||0)+1;
    if(S.work.content===snapshot.content)Object.assign(S.work,{path:'',sha:'',content:'',original:'',dirty:false});
    else Object.assign(S.work,{sha:'new',original:'',dirty:true}); // retain text typed while waiting
    if($('#editor-wrap')){$('#editor-wrap').innerHTML=editorHTML();bindEditor();}
    await loadTree();
  }
  toast('文件已删除，本机备份可恢复');
}
async function moveFile(){
  const snapshot={...workTarget(),path:S.work.path,sha:S.work.sha,original:S.work.original},generation=S.fileGeneration||0;
  if(!snapshot.path||snapshot.sha==='new')throw new Error('请先读取一个已存在的文件');
  if(S.work.dirty)throw new Error('请先保存或放弃草稿后再移动');
  const dialog=modal('移动文件',`<div class="field"><label>目标相对路径（必须尚不存在）</label><input id="move-destination" value="${esc(snapshot.path)}"></div>`,buttons('confirm-move','移动'));
  const button=$('#confirm-move');
  button.onclick=()=>busy(button,async()=>{
    const destination=$('#move-destination',dialog).value;
    const r=await settled(tool('fs_move',{project:snapshot.project,workspace_id:snapshot.workspace_id,path:snapshot.path,destination,expected_sha256:snapshot.sha,idempotency_key:uid()}));
    closeModal(dialog);
    if(S.work.project===snapshot.project&&S.work.path===snapshot.path&&generation===(S.fileGeneration||0)){
      S.fileGeneration=(S.fileGeneration||0)+1;S.readGeneration=(S.readGeneration||0)+1;
      Object.assign(S.work,{path:r.destination,sha:r.sha256,original:snapshot.original,dirty:S.work.content!==snapshot.original});
      if($('#editor-wrap')){$('#editor-wrap').innerHTML=editorHTML();bindEditor();}
      await loadTree();
    }
    toast('文件已移动');
  });
}
async function searchFiles(query,offset=0){const project=S.work.project;try{const r=await settled(wtool('fs_search',{query,offset,limit:100}));if(S.work.project!==project)return;modal('搜索 · '+query,`<p class="form-note">${esc(json({matches:r.matches?.length,files_scanned:r.scanned_files,truncated:r.truncated}))}</p>${(r.matches||[]).map(x=>`<button class="search-hit" data-action="search-read" data-path="${esc(x.path)}"><code>${esc(x.path)} : ${x.line}</code><p>${esc(x.text)}</p></button>`).join('')||empty('没有找到匹配项。')}<p class="form-note">${r.truncated?'搜索已到达返回数或扫描预算限制，不代表全仓已扫描。':'此结果仅覆盖文件工具允许访问的文本文件。'}</p>`,r.next_offset!==null&&r.next_offset!==undefined?'<button class="btn" id="search-more">下一页结果</button>':'',true);if($('#search-more'))$('#search-more').onclick=()=>searchFiles(query,r.next_offset);}catch(e){toast(e.message,true);}}
async function historyModal(){const project=S.work.project;if(!S.work.project)throw new Error('请先选择项目');const r=await settled(wtool('history_list',{path:S.work.path||'',limit:50}));if(S.work.project!==project)return;S.historyProject=project;S.historyWorkspace=S.work.workspace_id||'';const rows=r.backups||r.entries||[];modal('本机修改备份',`<p class="form-note">恢复某次操作前的文件内容。恢复也会建立新的备份；移动文件的备份只恢复原路径，不会自动删除新路径。</p>${rows.length?rows.map(b=>`<div class="history-row"><div><code>${esc(b.path)}</code><p>${esc(timeText(b.at))} · ${esc(b.id.slice(0,10))}</p></div><button class="btn small" data-action="restore-backup" data-id="${esc(b.id)}" data-path="${esc(b.path)}">恢复前版本</button></div>`).join(''):empty('这个范围还没有文件备份。')}`,'',true);}
async function restoreBackup(id,path){
  if(S.restoring)return;
  const project=S.historyProject||S.work.project,workspace_id=S.historyWorkspace||'',dialog=$('.modal'),generation=S.fileGeneration||0,revision=S.editRevision||0;
  if(project!==S.work.project||workspace_id!==(S.work.workspace_id||''))throw new Error('当前项目或工作目录已经变化，请重新打开备份。');
  if(S.work.dirty&&!confirm('恢复操作将重新读取文件，当前草稿可能丢失。继续？'))return;
  S.restoring=true;
  try{
    let sha='new';
    try{sha=(await settled(tool('fs_read',{project,workspace_id,path,max_lines:1}))).sha256;}
    catch(error){if(error.code!=='NOT_FOUND')throw error;}
    if(project!==S.work.project||generation!==(S.fileGeneration||0)||revision!==(S.editRevision||0))throw new Error('编辑内容已经变化，已取消本次恢复，请重新核对。');
    if(!confirm(`把 ${path} 恢复到这次操作之前的内容？`))return;
    await settled(tool('history_restore',{project,workspace_id,backup_id:id,expected_sha256:sha,idempotency_key:uid()}));
    closeModal(dialog);
    if(S.work.project===project&&generation===(S.fileGeneration||0)&&revision===(S.editRevision||0)){
      try{await readFile(path,true);}
      catch(error){
        if(error.code!=='NOT_FOUND')throw error;
        if(S.work.project===project&&generation===(S.fileGeneration||0)&&revision===(S.editRevision||0)){
          S.fileGeneration=(S.fileGeneration||0)+1;
          Object.assign(S.work,{path:'',content:'',original:'',sha:'',dirty:false,truncated:false});
          if($('#editor-wrap'))$('#editor-wrap').innerHTML=editorHTML();
        }
      }
      await loadTree();
    }else toast('恢复已完成，等待期间的新草稿已保留；保存前请重新核对本机文件。');
    toast('恢复已完成');
  }finally{S.restoring=false;}
}
async function checkpoint(){if(!S.work.project)throw new Error('请先选择项目');if(!confirm('为当前项目建立本机源码 ZIP 检查点？不包含依赖、凭据、链接或超过限制的文件，也不是整个硬盘备份。'))return;const r=await settled(wtool('project_checkpoint',{label:'Panel checkpoint',idempotency_key:uid()}));modal('源码检查点已保存',`${notice('检查点保存于家里 Agent 的状态目录。它是尽力读取的源码归档，不是原子文件系统快照。',true)}<div class="spacer"></div><div class="code-box"><pre>${esc(json(r))}</pre></div>`,'',true);}
async function gitView(which){if(!S.work.project)throw new Error('请先选择项目');const r=await settled(wtool(which,which==='git_diff'?{staged:false}:{}));modal(which==='git_status'?'Git 状态':'Git 工作区差异',`<div class="code-box"><pre>${esc(r.output||'没有工作区改动')}</pre></div>`,'',true);}
async function loadTasks(){
  const project=S.work.project;
  const r=await settled(tool('tasks_list',workTarget(project)));
  if(project!==S.work.project||!$('#task-select'))return;
  const tasks=r.tasks||[];
  $('#task-select').innerHTML='<option value="">选择本机授权任务</option>'+tasks.map(t=>`<option value="${esc(t.name)}">${esc(t.name)}${t.executable_available===false?' · 本机命令未找到':''}${t.description?' · '+esc(t.description):''}</option>`).join('');
  if(!tasks.length)toast('没有可运行任务。请在本机配置 tasks，并同时开启本机目录和项目的任务权限。');
}
function trackTask(id){S.work.operation=id;sessionValue('codepier-operation',id);}
function clearTaskSubmission(submission){if(S.taskSubmission!==submission)return;S.taskSubmission=null;sessionValue('codepier-task-submission',null);}
async function runTask(){
  if(S.taskSubmitting)return;
  S.taskSubmitting=true;
  try{
    restoreTaskSubmission();
    let submission=S.taskSubmission;
    if(!submission){
      const name=$('#task-select')?.value,project=S.work.project;
      if(!name||!project)throw new Error('请先刷新并选择本机任务');
      if(S.work.operation){
        try{const old=await api('/api/operations/'+S.work.operation);if(old.pending)throw new Error('当前任务仍在执行或恢复连接，请查看原任务，不要重复运行。');}
        catch(error){if(error.code!=='OPERATION_NOT_FOUND')throw error;trackTask(null);}
      }
      if(!confirm(`运行本机任务 ${name}？这会执行本机预先定义的程序。`))return;
      submission={...workTarget(project),task:name,idempotency_key:uid()};S.taskSubmission=submission;
      // Keep the original key even when every reply is lost, including a reload.
      sessionValue('codepier-task-submission',JSON.stringify(submission));
    }else toast('正在核实上次提交的任务 '+submission.task+'，沿用原操作幂等键。');
    try{
      const r=await tool('tasks_run',submission);
      if(S.taskSubmission!==submission)return;
      trackTask(r.operation_id);clearTaskSubmission(submission);
      S.work.console='任务已保存，等待 Agent 输出…';
      if($('#task-output'))$('#task-output').textContent=S.work.console;
      await pollTask();
    }catch(error){
      if(S.taskSubmission===submission){
        if(error.operation_id){trackTask(error.operation_id);clearTaskSubmission(submission);await pollTask();}
        else if(error.code&&error.status>=400&&error.status<500&&![401,408,429].includes(error.status)&&!error.retryable){
          // A policy change may reject today's retry after yesterday's request
          // already started. Resolve its saved key before releasing the receipt.
          const found=await tool('operations_list',{idempotency_key:submission.idempotency_key,limit:1});
          if(found.operations?.length)trackTask(found.operations[0].id);
          clearTaskSubmission(submission);
          if(found.operations?.length)await pollTask();
        }
      }
      throw error;
    }
  }finally{S.taskSubmitting=false;}
}
async function pollTask(){
  const id=S.work.operation,session=S.session;
  if(!id||!session||(S.taskPolling?.id===id&&S.taskPolling?.session===session))return;
  const poll={id,session};S.taskPolling=poll;clearTimeout(S.poll);S.poll=null;
  const current=()=>S.work.operation===id&&S.session===session;
  try{
    const r=await api('/api/operations/'+id);
    if(!current())return;
    S.work.console=r.output||r.result?.data?.output||r.error||(r.pending?'等待本机输出；网络中断不代表任务失败…':'任务已结束，无输出。');
    if($('#task-output'))$('#task-output').textContent=S.work.console;
    if($('#task-status'))$('#task-status').textContent=(stateNames[r.state]||r.state)+' · '+r.id.slice(0,12);
    S.taskPollFailures=0;
    if(r.pending)S.poll=setTimeout(()=>pollTask().catch(()=>{}),1500);
  }catch(error){
    if(!current())return;
    if(error.code==='OPERATION_NOT_FOUND'){
      trackTask(null);S.work.console='已停止跟踪不存在的任务 '+id+'；请在审计中核实原执行结果。';
      if($('#task-output'))$('#task-output').textContent=S.work.console;
      if($('#task-status'))$('#task-status').textContent='原任务记录不存在';
      toast(S.work.console,true);return;
    }
    if(error.status>=400&&error.status<500&&![408,429].includes(error.status)){
      if($('#task-status'))$('#task-status').textContent=error.message;return;
    }
    S.taskPollFailures=(S.taskPollFailures||0)+1;
    if($('#task-status'))$('#task-status').textContent='连接恢复中 · 保留任务 '+id.slice(0,12);
    S.poll=setTimeout(()=>pollTask().catch(()=>{}),Math.min(10000,1500*2**Math.min(3,S.taskPollFailures)));
  }finally{if(S.taskPolling===poll)S.taskPolling=null;}
}
async function auditHTML(seq=S.renderSeq){const type=S.auditMode;const query=new URLSearchParams({limit:'40',offset:String(S.auditOffset),source:S.auditSource,status:S.auditStatus,...(type==='events'?{q:S.auditQuery}:{})});const r=await api('/api/'+(type==='operations'?'operations':'audit')+'?'+query);const rows=type==='operations'?r.operations:r.events;if(seq===S.renderSeq){S.auditNext=r.next_offset;S.auditRows=rows;}return heading('操作审计','AUDIT TRAIL / OBSERVABILITY','',`<button class="btn ghost" data-action="export-audit">${icon('download')}导出审计 CSV</button>`)+`<div class="filters"><div class="tabs"><button class="${type==='operations'?'active':''}" data-action="audit-mode" data-mode="operations">工具执行</button><button class="${type==='events'?'active':''}" data-action="audit-mode" data-mode="events">全部事件</button></div><select id="audit-source" aria-label="来源"><option value="">全部来源</option>${['mcp','panel',...(type==='events'?['device']:[])].map(v=>`<option value="${v}" ${S.auditSource===v?'selected':''}>${v.toUpperCase()}</option>`).join('')}</select><select id="audit-status" aria-label="操作状态"><option value="">全部状态</option>${(type==='operations'?['queued','running','reconnecting','cancelling','succeeded','failed','cancelled','needs_review','interrupted']:['ok','started','denied','failed']).map(v=>`<option value="${v}" ${S.auditStatus===v?'selected':''}>${stateNames[v]||v}</option>`).join('')}</select>${type==='events'?`<input id="audit-query" value="${esc(S.auditQuery)}" placeholder="搜索调用者 / 操作 / 目标"><button class="btn small" data-action="audit-search">搜索</button>`:''}</div><section class="panel">${type==='operations'?operationsTable(rows):rows.length?`<div class="table-scroll"><table class="data-table"><thead><tr><th>时间</th><th>调用来源</th><th>事件</th><th>目标</th><th>状态</th><th></th></tr></thead><tbody>${rows.map((r,i)=>`<tr><td class="mono tiny muted">${esc(timeText(r.at))}</td><td class="tiny">${esc(r.actor)}</td><td class="mono tiny">${esc(r.action)}</td><td>${esc(r.target||'—')}</td><td>${badge(r.status)}</td><td><button class="icon-btn" data-event-index="${i}" aria-label="查看审计事件">${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>`:empty('当前筛选范围没有记录。')}<div class="pagination"><span>${rows.length?`第 ${S.auditOffset+1}–${S.auditOffset+rows.length} 条`:'暂无记录'}</span><div class="actions"><button class="btn ghost small" data-action="audit-prev" ${S.auditOffset===0?'disabled':''}>上一页</button><button class="btn ghost small" data-action="audit-next" ${r.next_offset===null?'disabled':''}>下一页</button></div></div></section>${uiHelp('记录范围','日志保存工具调用与执行证据，不含完整对话；服务器管理员可修改记录，不是第三方审计存证。')}`;}
function bindAudit(){$('#audit-source').onchange=e=>{S.auditSource=e.target.value;S.auditOffset=0;renderPage(false);};$('#audit-status').onchange=e=>{S.auditStatus=e.target.value;S.auditOffset=0;renderPage(false);};$$('[data-event-index]').forEach(b=>b.onclick=()=>{const r=S.auditRows[Number(b.dataset.eventIndex)];modal(r.action,`<dl class="kv"><dt>调用者</dt><dd>${esc(r.actor)}</dd><dt>时间</dt><dd>${esc(timeText(r.at))}</dd><dt>目标</dt><dd>${esc(r.target)}</dd></dl><div class="code-box"><pre>${esc(json(r.detail))}</pre></div>`,'',true);});}
async function operationDetail(id){const r=await api('/api/operations/'+id);modal('操作详情 · '+r.tool,`<dl class="kv"><dt>操作编号</dt><dd><code>${esc(r.id)}</code></dd><dt>状态</dt><dd>${badge(r.state)}</dd><dt>调用者</dt><dd>${esc(r.actor)}</dd><dt>投递次数</dt><dd>${r.attempts||0}（重投不会创建新操作）</dd><dt>首次执行期限</dt><dd>${esc(timeText(r.deadline))}</dd><dt>恢复状态</dt><dd>${esc(r.transport_error||'无网络异常')}${r.cancel_requested?' · 已保存取消请求':''}</dd><dt>开始 / 更新</dt><dd>${esc(timeText(r.created))}<br>${esc(timeText(r.updated))}</dd></dl>${r.error?notice(esc(r.error)):''}<div class="audit-detail-title">参数摘要</div><div class="code-box"><pre>${esc(json(r.args_summary))}</pre></div>${r.result?.data?.diff!==undefined?`<div class="audit-detail-title">文件差异</div>${diffHTML(r.result.data)}`:''}${r.output?`<div class="audit-detail-title">任务输出（保留尾部）</div><div class="code-box"><pre>${esc(r.output)}</pre></div>`:''}<div class="audit-detail-title">实际返回结果</div><div class="code-box"><pre>${esc(json(r.result))}</pre></div>`,`<button class="btn ghost" data-insight="trace" data-id="${esc(id)}">${icon('activity')}执行链路</button><button class="btn ghost" id="refresh-operation">${icon('refresh')}刷新结果</button><button class="btn primary" data-action="close-modal">关闭</button>`,true);$('#refresh-operation').onclick=()=>busy($('#refresh-operation'),()=>operationDetail(id));}
function codingEndpoint(){const url=new URL(S.settings.mcp_url,location.href);url.searchParams.set('profile','coding');return url.href;}
function connectHTML(){
  const s=S.settings;
  const grantRow=g=>`<div class="grant-row ${g.revoked?'revoked':''}"><div><h3>${esc(g.label)} <span class="badge ${g.revoked?'neutral':'purple'}">${g.revoked?'已撤销':g.client_id?'OAuth':'PAT'}</span></h3><p>${esc(g.scopes.join(' / '))} · ${esc(g.projects.map(id=>S.projects.find(p=>p.id===id)?.alias||id).join(', '))}<br>到期 ${esc(timeText(g.expires))}</p></div>${!g.revoked?`<button class="btn danger small" data-action="revoke-grant" data-id="${esc(g.id)}">撤销</button>`:''}</div>`;
  const guide=`<div class="step"><span class="step-number">01</span><div><h3>设备上线，映射项目</h3><p>先在工作台读取文件，确认链路正常。</p></div></div><div class="step"><span class="step-number">02</span><div><h3>添加 MCP 连接</h3><p>公开 HTTPS 使用 OAuth；无域名时可选择官方 Secure MCP Tunnel，需账号具备相应权限。</p></div></div><div class="step"><span class="step-number">03</span><div><h3>选择项目与权限</h3><p>在客户端完成授权后，通过项目别名调用。详细步骤见项目内 <code>docs/CHATGPT.md</code>。</p></div></div>`;
  return heading('MCP 接入','CONNECTION / ACCESS','',`<button class="btn primary" data-action="new-grant">${icon('key')}创建凭据</button>`)+
    `<div class="connect-grid"><section class="panel"><div class="panel-head"><h2>${icon('plug')}连接地址</h2><span class="badge neutral">MCP</span></div><div class="panel-body"><div class="eyebrow">STREAMABLE HTTP</div><div class="endpoint"><code>${esc(s.mcp_url)}</code><button class="icon-btn" data-action="copy-endpoint" aria-label="复制 MCP 地址">${icon('copy')}</button></div><div class="mcp-coding-entry"><div><strong>精简编码模式</strong><small>专注读码、修改与审阅，沿用原有授权。</small></div><button class="btn ghost small" data-action="copy-coding-endpoint">复制编码地址</button></div><div class="connection-route"><span>客户端</span>${icon('arrow')}<span>CodePier</span>${icon('arrow')}<span>本机项目</span></div>${notice('面板与 Agent 支持 HTTP。ChatGPT 直连需公开 HTTPS，或使用已获授权的 Secure MCP Tunnel。')}${uiHelp('接入步骤',guide)}</div></section>
    <section class="panel"><div class="panel-head"><h2>${icon('key')}访问授权</h2><span class="badge neutral">${S.grants.filter(g=>!g.revoked).length} 未撤销</span></div><div class="panel-body">${S.grants.length?S.grants.map(grantRow).join(''):empty('还没有访问授权。','new-grant','创建凭据')}</div></section></div>`+
    uiHelp(`工具目录 · ${s.tools.length} 项`,`<div class="project-search"><input id="tool-query" type="search" aria-label="搜索工具" placeholder="搜索工具名称或权限"><small id="tool-count" role="status"></small></div><div class="tool-grid">${s.tools.map(t=>{const scope=t.scope||t._meta?.securitySchemes?.[0]?.scopes?.[0]||(t.annotations?.readOnlyHint?'read':'write');return `<div class="tool-chip" data-tool-name="${esc((t.name+' '+scope).toLowerCase())}" title="${esc(t.description)}"><span>${esc(t.name)}</span><span class="tool-scope ${esc(scope)}">${esc(scope)}</span></div>`;}).join('')}</div><p class="form-note">读取、写入、执行分别授权；项目范围同时由 Hub 和本机限制。</p>`);
}
function permissionsHTML(projects,scopes=['read','write','execute','computer']){return `<div class="field"><label>工具权限</label><div class="check-list">${scopes.map(s=>`<label class="check"><input type="checkbox" name="scope" value="${s}" ${s==='read'?'checked disabled':''}>${{read:'读取项目与文件（必须）',write:'编辑文件、恢复备份和建立检查点',execute:'运行 / 停止本机授权任务',computer:'读取屏幕与操作桌面应用（独立高权限）'}[s]}</label>`).join('')}</div></div><div class="field"><label>允许访问的项目（必须明确选择）</label><div class="check-list">${projects.map(p=>`<label class="check"><input type="checkbox" name="project" value="${p.id}"><span>${esc(p.alias)} <small> · ${esc(p.device_name)}</small></span></label>`).join('')||'<small>尚无项目。请先建立映射后重新连接。</small>'}</div></div>`;}
function checked(name){return $$(`.modal input[name="${name}"]:checked`).map(x=>x.value);}
async function newGrant(){await loadBasics();modal('创建限定范围访问凭据',`<form id="grant-form"><div class="form-row"><div class="field"><label>凭据名称</label><input name="label" required maxlength="80" placeholder="例如 ChatGPT Tunnel"></div><div class="field"><label>有效天数</label><input name="days" type="number" min="1" max="365" value="30" required></div></div>${permissionsHTML(S.projects)}${notice('凭据只显示一次，不会写入浏览器持久存储。适用于 stdio bridge、MCP Inspector 或支持 Bearer 的客户端；ChatGPT 直连使用 OAuth。')}</form>`,buttons('create-grant','创建凭据'));$('#create-grant').onclick=()=>busy($('#create-grant'),async()=>{const f=$('#grant-form');if(!f.reportValidity())return;const r=await post('/api/grants',{label:f.elements.label.value,days:Number(f.elements.days.value),scopes:checked('scope'),projects:checked('project')});modal('保存你的访问凭据',`${notice('复制后存入云端受权限保护的 token 文件。任何持有此凭据的人，都能执行已授权操作。')}<div class="spacer"></div><div class="code-box secret">${esc(r.token)}</div><p class="form-note">到期：${esc(timeText(r.expires))}。需要时可随时撤销。</p>`,`<button class="btn ghost" data-action="close-modal">完成</button><button class="btn" id="copy-token">复制</button><button class="btn primary" id="download-token">下载 token.txt</button>`);$('#copy-token').onclick=()=>copy(r.token);$('#download-token').onclick=()=>download('token.txt',r.token+'\n','text/plain');});}
async function consentModal(id){try{const r=await api('/api/oauth/requests/'+encodeURIComponent(id));modal('确认 MCP 应用授权',`<p class="form-note"><strong>${esc(r.client_name)}</strong> 正在申请访问你的本机项目。</p><dl class="kv"><dt>客户端</dt><dd><code>${esc(r.client_id)}</code></dd><dt>回调地址</dt><dd>${esc(r.redirect_uri)}</dd><dt>资源</dt><dd>${esc(r.resource)}</dd></dl>${permissionsHTML(S.projects,r.scopes)}${notice('只有勾选的现有项目会被授权。请核对应用名称和回调地址，不要因为页面内容或仓库中的文字指令授予不必要的权限。')}`,`<button class="btn ghost" id="deny-consent">拒绝</button><button class="btn primary" id="allow-consent">允许所选范围</button>`,false,false);const decide=allow=>busy(allow?$('#allow-consent'):$('#deny-consent'),async()=>{const out=await post(`/api/oauth/requests/${encodeURIComponent(id)}/decide`,{allow,scopes:checked('scope'),projects:checked('project')});location.assign(out.redirect);});$('#deny-consent').onclick=()=>decide(false);$('#allow-consent').onclick=()=>decide(true);}catch(e){toast(e.message,true);history.replaceState(null,'',location.pathname+location.hash);}}
function settingsHTML(){
  const s=S.settings;
  return heading('系统设置','SYSTEM / SETTINGS')+`<div class="settings-grid"><section class="panel"><div class="panel-head"><h2>${icon('plug')}对外地址</h2></div><div class="panel-body"><form id="settings-form"><div class="field"><label>基础地址</label><input name="public_url" value="${esc(s.public_url)}" required><small>HTTP / HTTPS 均可，不带 /mcp。</small></div>${notice('只修改 MCP / OAuth 标识，不改变监听端口或 Agent 地址。变更后请重新连接并授权。')}<div class="spacer"></div><button class="btn primary" type="submit">保存地址</button></form>${uiHelp('服务信息',`<dl class="kv"><dt>版本</dt><dd>${esc(s.version)}</dd><dt>监听端口</dt><dd>${s.listen_port}（容器内）</dd><dt>MCP 协议</dt><dd>${esc(s.protocol_versions.join(' / '))}</dd><dt>数据目录</dt><dd><code>${esc(s.data_dir)}</code></dd></dl>`)}</div></section><section class="panel"><div class="panel-head"><h2>${icon('lock')}管理员密码</h2></div><div class="panel-body"><form id="password-form"><div class="field"><label>当前密码</label><input name="current_password" type="password" required autocomplete="current-password"></div><div class="field"><label>新密码</label><input name="new_password" type="password" minlength="12" maxlength="256" required autocomplete="new-password" placeholder="至少 12 位"></div><div class="field"><label>确认新密码</label><input name="confirm_password" type="password" minlength="12" required autocomplete="new-password" aria-describedby="password-match-error"><p id="password-match-error" class="field-error" role="status" hidden></p></div><p class="form-note">修改后退出全部浏览器登录，并撤销 MCP 授权；设备密钥不变。</p><button class="btn" type="submit">更新密码并退出</button></form></div></section></div>`+
    uiHelp('更改 Agent 连接地址',`<p>在本机 Agent 目录执行：</p><div class="code-box"><pre>python -m agent configure --hub http://新的云端IP:新的端口</pre></div><p class="form-note">保持同一账号与配置文件。自定义配置需加 <code>--config /path/config.json</code>。更换端口前，先修改 Hub 的端口映射与防火墙；Agent 随配置变化自动重连。</p>`);
}
function bindSettings(){const passwordForm=$('#password-form');passwordForm.addEventListener('input',()=>{const error=$('#password-match-error',passwordForm);if(!error.hidden){error.hidden=true;error.textContent='';passwordForm.elements.confirm_password.removeAttribute('aria-invalid');}});$('#settings-form').onsubmit=e=>{e.preventDefault();busy($('button',e.target),async()=>{const r=await api('/api/settings',{method:'PUT',body:JSON.stringify({public_url:e.target.elements.public_url.value})});toast(r.note);await renderPage(false);});};$('#password-form').onsubmit=e=>{e.preventDefault();const f=e.target;if(f.elements.new_password.value!==f.elements.confirm_password.value){const field=f.elements.confirm_password;field.setAttribute('aria-invalid','true');const error=$('#password-match-error',f);error.hidden=false;error.textContent='两次新密码不一致';field.focus();return;}busy($('button',f),async()=>{await post('/api/account/password',{current_password:f.elements.current_password.value,new_password:f.elements.new_password.value});endSession(true);toast('密码已更新，请重新登录并重新建立 MCP 授权');});};}
async function busy(button,fn){
  if(button?.disabled)return;
  const children=button?[...button.childNodes]:[],priorBusy=button?.getAttribute('aria-busy');
  const priorWidth=button?.style.getPropertyValue('min-width'),priorPriority=button?.style.getPropertyPriority('min-width');
  if(button){
    const width=button.getBoundingClientRect().width;
    const label=document.createElement('span');label.className='ui-busy-label';label.append(...children);
    const indicator=document.createElement('span');indicator.className='ui-busy-indicator';indicator.setAttribute('aria-hidden','true');indicator.innerHTML='<i class="spinner"></i>';
    button.replaceChildren(label,indicator);button.disabled=true;button.classList.add('ui-busy');button.setAttribute('aria-busy','true');
    if(width)button.style.minWidth=width+'px';
  }
  try{return await fn();}catch(e){toast(e.message,true);}
  finally{if(button){
    button.replaceChildren(...children);button.disabled=false;button.classList.remove('ui-busy');
    if(priorBusy===null)button.removeAttribute('aria-busy');else button.setAttribute('aria-busy',priorBusy);
    if(priorWidth)button.style.setProperty('min-width',priorWidth,priorPriority);else button.style.removeProperty('min-width');
  }}
}
document.addEventListener('click',async e=>{const navButton=e.target.closest('[data-nav]');if(navButton){await navigate(navButton.dataset.nav);return;}const b=e.target.closest('[data-action]');if(!b||b.disabled)return;try{switch(b.dataset.action){case'close-modal':closeModal();break;case'toggle-menu':uiSetMenu(undefined,true,b);break;case'logout':if(S.work.dirty&&!confirm('退出会丢弃当前标签页的未保存草稿，继续？'))break;await post('/api/logout');endSession(true);break;case'refresh':await renderPage();break;case'add-device':await addDevice();break;case'device-detail':await deviceDetail(b.dataset.id);break;case'agent-update':case'agent-restart':case'agent-uninstall':{let device=S.devices.find(item=>item.id===b.dataset.id);if(!device){await loadBasics();device=S.devices.find(item=>item.id===b.dataset.id);}if(!device)throw new Error('节点信息已变化，请刷新后重试');agentLifecycleModal(device,b.dataset.action.replace('-','_'));break;}case'agent-commands':{let device=S.devices.find(item=>item.id===b.dataset.id);if(!device){await loadBasics();device=S.devices.find(item=>item.id===b.dataset.id);}if(!device)throw new Error('节点信息已变化，请刷新后重试');showAgentMaintenanceCommands(device);break;}case'agent-repair':{let device=S.devices.find(item=>item.id===b.dataset.id);if(!device){await loadBasics();device=S.devices.find(item=>item.id===b.dataset.id);}if(!device)throw new Error('节点信息已变化，请刷新后重试');await agentInstallSetup(device);break;}case'add-project':await projectModal(null,b.dataset.device);break;case'edit-project':await projectModal(b.dataset.id);break;case'open-project':if(S.work.dirty&&!confirm('打开项目将丢弃当前未保存草稿，继续？'))break;resetWork(b.dataset.id);await navigate('workbench');break;case'tree-refresh':await loadTree();break;case'tree-home':S.work.treePath='.';await loadTree();break;case'tree-dir':S.work.treePath=b.dataset.path;await loadTree();break;case'tree-more':await loadTree(true);break;case'read-file':await readFile(b.dataset.path);break;case'search-read':closeModal();await readFile(b.dataset.path);break;case'new-file':await newFile();break;case'reload-file':await readFile(S.work.path);break;case'preview-save':await previewSave();break;case'delete-file':await deleteFile();break;case'move-file':await moveFile();break;case'history':await historyModal();break;case'restore-backup':await restoreBackup(b.dataset.id,b.dataset.path);break;case'checkpoint':await checkpoint();break;case'git-status':await gitView('git_status');break;case'git-diff':await gitView('git_diff');break;case'load-tasks':await loadTasks();break;case'run-task':await runTask();break;case'cancel-task':if(S.work.operation){await tool('operations_cancel',{operation_id:S.work.operation});toast('已发送停止请求，请核对任务结果');}break;case'operation-detail':await operationDetail(b.dataset.id);break;case'audit-mode':S.auditMode=b.dataset.mode;S.auditOffset=0;S.auditStatus='';S.auditSource='';await renderPage(false);break;case'audit-search':S.auditQuery=$('#audit-query').value;S.auditOffset=0;await renderPage(false);break;case'audit-prev':S.auditOffset=Math.max(0,S.auditOffset-40);await renderPage(false);break;case'audit-next':if(S.auditNext!==null){S.auditOffset=S.auditNext;await renderPage(false);}break;case'export-audit':{const url='/api/audit-export?'+new URLSearchParams({source:S.auditSource,q:S.auditQuery,offset:'0'});const a=document.createElement('a');a.href=url;a.download='codepier-audit.csv';a.click();toast('导出全部事件中匹配来源/关键词的前 10,000 条；不是仅工具执行列表。');break;}case'copy-endpoint':await copy(S.settings.mcp_url);break;case'copy-coding-endpoint':await copy(codingEndpoint());break;case'new-grant':await newGrant();break;case'revoke-grant':if(confirm('撤销这个凭据的全部访问权限？正在执行的本机任务不会自动停止。')){await api('/api/grants/'+b.dataset.id,{method:'DELETE'});await renderPage(false);}break;}}catch(err){toast(err.message,true);}});
window.addEventListener('beforeunload',e=>{if(S.work.dirty){e.preventDefault();e.returnValue='';}});
window.addEventListener('hashchange',()=>{const page=location.hash.slice(1)==='terminal'?'native':location.hash.slice(1);if(page!==S.page&&nav.some(n=>n[0]===page))navigate(page);});
setInterval(updateClock,1000);
(async()=>{try{const page=location.hash.slice(1)==='terminal'?'native':location.hash.slice(1);if(nav.some(n=>n[0]===page))S.page=page;if(location.hash==='#terminal')history.replaceState(null,'',location.pathname+location.search+'#native');const s=await api('/api/session');if(s.authenticated){S.session=s;await bootAuthenticated();}else renderLogin(s.configured);}catch(e){$('#app').innerHTML=`<div class="boot">${brand}<p>${esc(e.message)}</p><button class="btn" id="retry-boot">重新连接</button></div>`;$('#retry-boot').onclick=()=>location.reload();}})();
