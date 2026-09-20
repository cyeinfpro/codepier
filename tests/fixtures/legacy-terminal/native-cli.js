'use strict';
// Native TUI: no native output becomes HTML and no CLI transcript enters browser storage.
const NativeCLIUI={generation:0,selected:null,writer:null,term:null,offset:0,project:'',provider:'codex',draft:'',files:[],timers:new Set(),pageOffset:0,seen:new Map(),chain:Promise.resolve(),inputQueue:'',lease:false};
function nativeButtons(project){return `<button class="btn small" data-native-launch="pi" data-project="${esc(project)}">Pi</button><button class="btn small" data-native-launch="codex" data-project="${esc(project)}">Codex</button>`;}
function nativeAPI(action,args={},project=NativeCLIUI.project){return api('/api/native/'+action,{method:'POST',body:JSON.stringify({project,args}),retrySafe:true});}
function nativeStatus(text){const el=$('#native-status');if(el)el.textContent=text;}
function nativeTimer(fn,ms){const n=setTimeout(()=>{NativeCLIUI.timers.delete(n);fn();},ms);NativeCLIUI.timers.add(n);return n;}
function nativeDetach(){
  const n=NativeCLIUI;n.generation++;for(const t of n.timers)clearTimeout(t);n.timers.clear();
  if(n.selected&&n.lease&&S.session)nativeAPI('detach',{id:n.selected.id,writer:n.writer},n.selected.project_id).catch(()=>{});
  n.lease=false;n.term?.dispose();n.term=null;n.resize?.disconnect();n.listeners?.abort();n.selected=null;n.offset=0;n.inputQueue='';
  n.leaseTimer=null;n.leasePending=false;n.lastSize='';n.inputBlocked=false;n.chain=Promise.resolve();
  for(const item of n.inputBatch||[])item.reject(new Error('会话已分离，未发送的输入仍需重新确认'));
  n.inputBatch=[];n.inputFlushing=false;
}
async function nativePage(){
  if($('#native-root'))return; // Refresh does not destroy drafts or terminal input.
  nativeDetach();const n=NativeCLIUI,gen=n.generation,session=S.session;
  await loadBasics();if(gen!==n.generation||session!==S.session||S.page!=='terminal')return;
  n.project=n.project||S.work.project||S.projects[0]?.id||'';
  $('#page').innerHTML=heading('CLI 会话','NATIVE / TERMINAL','')+`<div id="native-root" class="native-layout">
  <aside class="panel native-sidebar"><label>项目<select id="native-project">${S.projects.map(p=>`<option value="${esc(p.id)}" ${p.id===n.project?'selected':''}>${esc(p.alias)}</option>`).join('')}</select></label>
  <label>查找历史<input id="native-filter" placeholder="标题或已同步输出"></label><select id="native-provider-filter" aria-label="筛选 CLI"><option value="">全部 CLI</option><option>pi</option><option>codex</option></select><select id="native-state-filter" aria-label="筛选状态"><option value="">全部状态</option><option value="running">运行中</option><option value="exited">已退出</option><option value="interrupted">已中断</option></select>
  <div id="native-list"></div><div class="native-pages"><button class="btn small" id="native-prev">上一页</button><button class="btn small" id="native-next">下一页</button></div></aside>
  <section class="panel native-main"><details id="native-launch-details" open><summary>启动 / 原生恢复</summary><div class="native-launch">
  <label>CLI<select id="native-cli"><option value="codex" ${n.provider==='codex'?'selected':''}>Codex</option><option value="pi" ${n.provider==='pi'?'selected':''}>Pi</option></select></label>
  <label>项目子目录<input id="native-cwd" value="." placeholder="."></label><label>模型<input id="native-model" placeholder="沿用本机默认 / 原生选择器"></label>
  <label>Provider<input id="native-provider" placeholder="沿用本机默认"></label><label>思考强度<input id="native-effort" placeholder="原生默认；例如 high"></label><label>恢复<select id="native-resume"><option value="">新会话</option><option value="yes">原生历史选择器</option></select></label>
  <label class="wide">高级启动参数 · JSON argv 数组<textarea id="native-argv" spellcheck="false" placeholder='["--no-alt-screen"]'>[]</textarea></label></div>
  <p class="native-note">完整本机进程权限等同 shell，超出项目文件工具边界；沿用原生 HOME、认证、技能与审批。不会自动跳过审批。目录固定来自项目映射。浏览器离开只分离；机器重启不保证继续，需原生恢复。</p>
  <div class="native-tools"><button class="btn primary" id="native-start">启动原生 CLI</button><button class="btn" id="native-detect">检测安装</button></div><div id="native-detection" class="native-note"></div></details>
  <div class="native-tools"><button class="btn" id="native-write">取得输入权</button><button class="btn" id="native-detach">仅分离</button><button class="btn danger" id="native-stop">停止进程</button><button class="btn" id="native-rename">重命名</button><button class="btn" id="native-resume-selected">原生恢复</button><button class="btn" id="native-history">原生历史</button><button class="btn danger" id="native-history-delete">清理原生历史</button><button class="btn" id="native-export">导出记录</button><button class="btn" id="native-clear-screen">清屏</button><button class="btn danger" id="native-clear">清除 RELAY 记录</button><button class="btn danger" id="native-delete">删除会话</button></div>
  <div id="native-status" class="native-status" role="status">选择历史会话，或启动本机 CLI。离线仍可查看已同步历史。</div><div id="native-terminal" class="native-terminal" aria-label="原生交互终端"></div>
  <div class="native-tools" id="native-keys">${[['Esc','\x1b'],['Tab','\t'],['↑','\x1b[A'],['↓','\x1b[B'],['←','\x1b[D'],['→','\x1b[C'],['Enter','\r'],['Ctrl+C','\x03'],['Ctrl+D','\x04'],['Shift+Tab','\x1b[Z']].map(([label,key])=>`<button class="btn small" data-native-key="${btoa(key)}">${label}</button>`).join('')}<button class="btn small" data-native-command="/model">/model</button><button class="btn small" data-native-command="/thinking">Pi /thinking</button><button class="btn small" data-native-command="/settings">/settings</button><button class="btn small" data-native-command="/login">Pi /login</button></div>
  <div class="native-tools"><input id="native-search" aria-label="搜索终端回放" placeholder="搜索终端回放"><button class="btn small" id="native-find">查找</button><button class="btn small" id="native-copy">复制选区</button><button class="btn small" id="native-focus">聚焦终端</button><button class="btn small" id="native-bottom">滚动到底部</button></div>
  <div class="native-upload" id="native-drop"><label>文件 / 图片 · 粘贴或拖放<input type="file" multiple id="native-files"></label><div id="native-attachments" class="native-files"></div><p class="native-note">图片可在当前会话附加，不自动发送。Codex 沿用原生图片粘贴，Pi 使用原生输入扩展；普通文件插入路径。勾选附件用于下次启动，单文件最多 20 MiB。刷新后可继续上传或清理。</p></div>
  <label>输入草稿<textarea id="native-compose" placeholder="发送到原生编辑器，不自动按 Enter"></textarea></label><div class="native-tools"><button class="btn" id="native-insert">插入终端</button></div></section></div>`;
  $('#native-compose').value=n.draft;$('#native-compose').oninput=e=>n.draft=e.target.value;
  const bind=(id,fn)=>{$('#'+id).onclick=()=>Promise.resolve().then(fn).catch(e=>nativeStatus(e.message));};
  $('#native-project').onchange=async e=>{nativeDetach();n.project=e.target.value;n.pageOffset=0;n.files=[];n.draft='';n.pendingStart=null;$('#native-compose').value='';$('#native-terminal').replaceChildren();nativeAttachments();nativeListLoop(n.generation);nativeLoadAttachments().catch(e=>nativeStatus(e.message));};
  for(const id of ['native-filter','native-provider-filter','native-state-filter'])$('#'+id).oninput=()=>{n.pageOffset=0;nativeList(n.generation).catch(e=>nativeStatus(e.message));};
  bind('native-prev',()=>{n.pageOffset=Math.max(0,n.pageOffset-40);return nativeList(n.generation);});bind('native-next',()=>{n.pageOffset+=40;return nativeList(n.generation);});
  bind('native-detect',async()=>{const ticket=n.generation,project=n.project,r=await nativeAPI('discover',{},project);if(ticket!==n.generation||project!==n.project||!$('#native-detection'))return;$('#native-detection').textContent=Object.entries(r.programs).map(([k,v])=>k+': '+(v.available?v.version+' · '+v.path:v.message)).join(' | ')+' · '+r.message;});
  bind('native-start',async()=>{
    const options={id:uid(),cli:$('#native-cli').value,cwd:$('#native-cwd').value,model:$('#native-model').value,provider:$('#native-provider').value,effort:$('#native-effort').value,resume:!!$('#native-resume').value,argv:JSON.parse($('#native-argv').value),attachments:n.files.filter(x=>x.selected&&x.ready&&($('#native-cli').value==='pi'||/\.(png|jpe?g|webp|gif)$/i.test(x.path))).map(x=>x.file)};
    const project=n.project,ticket=n.generation;
    if(n.pendingStart&&n.pendingStart.project!==project)n.pendingStart=null;
    n.pendingStart=n.pendingStart||{project,options}; // Ambiguous start retains its original project and argv.
    const pending=n.pendingStart;$('#native-start').disabled=true;
    try{
      const row=await nativeAPI('start',pending.options,pending.project);
      if(n.pendingStart===pending)n.pendingStart=null;
      if(ticket!==n.generation||project!==n.project||S.page!=='terminal')return;
      await nativeSelect(row);$('#native-launch-details').open=false;
    }catch(e){
      if(!['CLI_UNCERTAIN','NETWORK_UNCERTAIN'].includes(e.code)&&n.pendingStart===pending)n.pendingStart=null;
      if(ticket===n.generation)throw e;
    }finally{if($('#native-start'))$('#native-start').disabled=false;}
  });
  bind('native-resume-selected',()=>{if(!n.selected)return;const row=n.selected;$('#native-cli').value=row.provider;n.provider=row.provider;$('#native-cwd').value=row.cwd===row.root?'.':row.cwd.slice(row.root.length+1);$('#native-resume').value='yes';$('#native-launch-details').open=true;nativeProviderControls();$('#native-start').focus();});
  bind('native-history',()=>nativeHistory(false));bind('native-history-delete',()=>nativeHistory(true));
  bind('native-write',()=>nativeLease(false));bind('native-detach',()=>{nativeDetach();nativeStatus('已分离；本机 CLI 继续运行。');nativeListLoop(n.generation);});
  bind('native-stop',async()=>{if(!n.selected||!confirm('停止此原生进程？未完成的模型工作可能中断。'))return;await nativeControl('stop');});
  bind('native-rename',async()=>{if(!n.selected)return;const title=prompt('会话名称',n.selected.title);if(title)await nativeAPI('rename',{id:n.selected.id,title},n.selected.project_id);});
  for(const action of ['clear','delete'])bind('native-'+action,async()=>{if(!n.selected||!confirm('仅清理此会话的 RELAY 输出/输入记录。须先停止；不会删除 ~/.codex 或 ~/.pi 原生历史。继续？'))return;await nativeAPI(action,{id:n.selected.id,confirm:n.selected.id},n.selected.project_id);nativeDetach();$('#native-terminal').replaceChildren();nativeListLoop(n.generation);});
  bind('native-clear-screen',()=>n.term?.clear());bind('native-export',nativeExport);bind('native-find',()=>n.search?.findNext($('#native-search').value));bind('native-copy',()=>copy(n.term?.getSelection()||''));bind('native-focus',()=>n.term?.focus());bind('native-bottom',()=>n.term?.scrollToBottom());
  bind('native-insert',async()=>{
    const ticket=n.generation,text=$('#native-compose').value;
    if(!text)return;
    if(!n.selected||!n.lease)throw new Error('先取得输入权');
    for(const file of n.files.filter(f=>f.ready&&text.includes(f.path)))
      await nativeAPI('upload_bind',{id:n.selected.id,writer:n.writer,file:file.file},n.selected.project_id);
    if(ticket!==n.generation)return;
    await nativeInput('\x1b[200~'+text+'\x1b[201~');
    if(ticket===n.generation&&$('#native-compose')?.value===text){n.draft='';$('#native-compose').value='';}
  });
  $('#native-keys').onclick=e=>{const key=e.target.closest('[data-native-key]'),cmd=e.target.closest('[data-native-command]');if(key)nativeInput(atob(key.dataset.nativeKey)).catch(e=>nativeStatus(e.message));if(cmd){if(!confirm('将命令插入原生编辑器并按 Enter；请先确认编辑器没有未提交输入。继续？'))return;nativeInput(cmd.dataset.nativeCommand+'\r').catch(e=>nativeStatus(e.message));}};
  $('#native-files').onchange=e=>nativeUpload([...e.target.files]).catch(e=>nativeStatus(e.message));
  $('#native-drop').ondragover=e=>e.preventDefault();$('#native-drop').ondrop=e=>{e.preventDefault();nativeUpload([...e.dataTransfer.files]).catch(e=>nativeStatus(e.message));};
  $('#native-root').addEventListener('paste',e=>{const files=[...(e.clipboardData?.files||[])];if(files.length){e.preventDefault();e.stopImmediatePropagation();nativeUpload(files).catch(e=>nativeStatus(e.message));}},true);
  nativeAttachments();nativeListLoop(n.generation);nativeLoadAttachments().catch(e=>nativeStatus(e.message));
  $('#native-cli').onchange=()=>{n.provider=$('#native-cli').value;nativeProviderControls();};
  nativeProviderControls();
}
async function nativeList(gen){
  const n=NativeCLIUI,query=new URLSearchParams({project:n.project,q:$('#native-filter')?.value||'',provider:$('#native-provider-filter')?.value||'',status:$('#native-state-filter')?.value||'',offset:n.pageOffset,limit:40,mode:'terminal'});
  const ticket=n.listTicket=(n.listTicket||0)+1;
  const data=await api('/api/native/sessions?'+query);if(gen!==n.generation||ticket!==n.listTicket||!$('#native-list'))return;
  $('#native-list').replaceChildren();
  for(const row of data.sessions.filter(r=>r.mode!=='chat')){const button=document.createElement('button');button.className='native-session'+(n.selected?.id===row.id?' active':'');button.textContent=row.title;const meta=document.createElement('small');meta.textContent=row.provider+' · '+row.status+' · '+(row.online?'在线':'离线缓存')+(row.cached_bytes>(n.seen.get(row.id)||0)?' · 新输出':'');button.append(meta);button.onclick=()=>nativeSelect(row).catch(e=>nativeStatus(e.message));$('#native-list').append(button);}
  $('#native-prev').disabled=!n.pageOffset;$('#native-next').disabled=n.pageOffset+40>=data.total;
}
async function nativeListLoop(gen){try{await nativeList(gen);}catch(e){if(gen===NativeCLIUI.generation)nativeStatus(e.message);}if(gen===NativeCLIUI.generation&&S.page==='terminal')nativeTimer(()=>nativeListLoop(gen),2500);}
async function nativeSelect(row){
  nativeDetach();const n=NativeCLIUI;n.selected=row;n.project=row.project_id;n.writer=uid();n.offset=0;const gen=n.generation;n.replaying=true;
  $('#native-terminal').replaceChildren();
  n.term=new Terminal({allowProposedApi:true,allowTransparency:false,convertEol:false,scrollback:10000,fontSize:13,fontFamily:'ui-monospace, Menlo, monospace',cursorBlink:true,disableStdin:true,theme:{background:'#0c111b',foreground:'#dce5f2'},linkHandler:{activate:()=>{}}});
  n.fit=new FitAddon.FitAddon();n.search=new SearchAddon.SearchAddon();n.term.loadAddon(n.fit);n.term.loadAddon(n.search);
  // Consume title, hyperlink and clipboard escape sequences; never expose OSC52 to clipboard.
  for(const code of [0,1,2,8,10,11,52])n.term.parser.registerOscHandler(code,()=>true);
  // The independent worker answers terminal queries even with no browser.
  // Suppress xterm's reply input so replay and other tabs never duplicate them.
  for(const id of [{final:'n'},{prefix:'?',final:'n'},{final:'c'},{prefix:'>',final:'c'},
                  {prefix:'=',final:'c'},{prefix:'?',final:'u'},{final:'t'},
                  {prefix:'?',intermediates:'$',final:'p'}])
    n.term.parser.registerCsiHandler(id,()=>true);
  n.term.open($('#native-terminal'));n.fit.fit();
  n.term.onData(data=>{if(n.lease&&!n.replaying&&gen===n.generation)nativeInput(data).catch(e=>nativeStatus(e.message));});
  n.resize=new ResizeObserver(()=>{if(gen!==n.generation)return;n.fit.fit();nativeResize().catch(e=>nativeStatus(e.message));});n.resize.observe($('#native-terminal'));
  n.provider=row.provider;nativeProviderControls();
  nativeListLoop(gen);nativeOutputLoop(gen);
}
async function nativeOutputLoop(gen){
  const n=NativeCLIUI;if(gen!==n.generation||!n.selected)return;
  try{
    const data=await api('/api/native/sessions/'+n.selected.id+'/output?offset='+n.offset);
    if(gen!==n.generation)return;
    if(data.reset){n.term.reset();n.offset=0;n.replaying=false;}
    for(const chunk of data.chunks){const bytes=Uint8Array.from(atob(chunk),c=>c.charCodeAt(0));await new Promise(resolve=>n.term.write(bytes,resolve));if(gen!==n.generation)return;}
    n.offset=data.next;n.seen.set(n.selected.id,n.offset);n.selected=data.session;
    if(n.offset>=data.session.size)n.replaying=false;
    if(!['starting','running'].includes(data.session.status)){n.lease=false;n.term.options.disableStdin=true;}
    else n.term.options.disableStdin=!n.lease||n.replaying||n.inputBlocked;
    nativeStatus(`${data.online?'节点在线':'节点离线 · 已同步历史'} · ${data.session.status} · ${n.lease?'可输入':'只读，点击取得输入权'} · ${n.offset}/${data.session.size} 字节 ${data.session.error||''}`);
  }catch(e){if(gen===n.generation)nativeStatus(e.message);}
  if(gen===n.generation)nativeTimer(()=>nativeOutputLoop(gen),400);
}
async function nativeLease(renew=false){
  const n=NativeCLIUI;if(!n.selected||n.leasePending)return;const gen=n.generation;
  n.leasePending=true;
  try{
    await nativeAPI('lease',{id:n.selected.id,writer:n.writer},n.selected.project_id);
    if(gen!==n.generation)return;
    if(!renew&&n.inputBlocked){if(!confirm('确认已经核对终端输出？恢复输入不会重放上一次不确定的内容。'))return;n.inputBlocked=false;}
    n.lease=true;n.term.options.disableStdin=n.replaying||n.inputBlocked;
    await nativeResize();if(!renew)n.term.focus();
    if(n.leaseTimer){clearTimeout(n.leaseTimer);n.timers.delete(n.leaseTimer);}
    n.leaseTimer=nativeTimer(async()=>{
      n.leaseTimer=null;if(gen!==n.generation||!n.lease)return;
      try{await nativeLease(true);}catch(e){if(gen!==n.generation)return;n.lease=false;n.term.options.disableStdin=true;nativeStatus(e.message);}
    },5000);
  }finally{if(gen===n.generation)n.leasePending=false;}
}
async function nativeResize(){
  const n=NativeCLIUI;if(!n.lease||!n.term)return;
  const size=n.term.rows+'x'+n.term.cols;if(size===n.lastSize)return;
  n.lastSize=size;
  try{await nativeControl('resize',{rows:n.term.rows,cols:n.term.cols});}catch(e){n.lastSize='';throw e;}
}
function nativeControl(action,args={}){
  const n=NativeCLIUI;if(!n.selected||!n.lease)return Promise.reject(new Error('先取得输入权'));
  const id=n.selected.id,project=n.selected.project_id,payload={id,writer:n.writer,receipt:uid(),...args},gen=n.generation;
  const execute=async()=>{
    if(gen!==n.generation)throw new Error('会话已分离');let last;
    for(let i=0;i<3;i++){
      try{
        const result=await nativeAPI(action,payload,project);
        if(['uncertain','cancelled'].includes(result.state))throw Object.assign(new Error('输入未确认，不能自动重放 · 回执 '+payload.receipt),{code:'CLI_INPUT_UNCERTAIN'});
        return result;
      }catch(e){last=e;if(!['CLI_UNCERTAIN','CLI_OFFLINE','NETWORK_UNCERTAIN'].includes(e.code))throw e;if(gen!==n.generation)throw new Error('会话已分离');await pause(500*(i+1));}
    }
    throw Object.assign(new Error(last.message+' · 回执 '+payload.receipt),{code:'CLI_INPUT_UNCERTAIN'});
  };
  const result=n.chain.then(execute);n.chain=result.catch(()=>{});return result;
}
function nativeInput(text){
  const n=NativeCLIUI;
  if(!n.selected||!n.lease||n.inputBlocked)return Promise.reject(new Error(n.inputBlocked?'上次输入结果不确定，请核对终端后重新取得输入权':'先取得输入权'));
  if(!text)return Promise.resolve();
  return new Promise((resolve,reject)=>{
    n.inputBatch=n.inputBatch||[];n.inputBatch.push({text,resolve,reject,generation:n.generation});
    if(!n.inputFlushing){n.inputFlushing=true;nativeTimer(nativeFlushInput,20);}
  });
}
async function nativeFlushInput(){
  const n=NativeCLIUI,gen=n.generation,encoder=new TextEncoder();
  try{
    while(gen===n.generation&&n.inputBatch?.length){
      const group=n.inputBatch.splice(0),text=group.map(x=>x.text).join('');
      try{
        let chunk='',bytes=0;
        for(const char of text){
          const width=encoder.encode(char).length;
          if(bytes+width>16000){await nativeControl('input',{text:chunk});chunk='';bytes=0;}
          chunk+=char;bytes+=width;
        }
        if(chunk)await nativeControl('input',{text:chunk});
        group.forEach(x=>x.resolve());
      }catch(e){
        group.forEach(x=>x.reject(e));
        if(gen===n.generation){
          n.inputBlocked=true;n.term.options.disableStdin=true;
          for(const item of n.inputBatch.splice(0))item.reject(new Error('后续输入已暂停，未重复发送'));
        }
        break;
      }
    }
  }finally{if(gen===n.generation)n.inputFlushing=false;}
}
async function nativeUpload(files){
  const n=NativeCLIUI,project=n.project,gen=n.generation;
  for(const file of files){
    if(!file.size||file.size>20*1024*1024)throw new Error('每个附件须为 1 字节–20 MiB');
    const raw=new Uint8Array(await file.arrayBuffer()),hash=nativeSha256,sha256=await hash(raw);
    const catalog=await nativeAPI('upload_list',{},project);
    const existing=catalog.files.find(x=>x.name===file.name&&x.size===raw.length&&x.sha256===sha256);
    const id=existing?.file||uid();
    let reply=await nativeAPI('upload_begin',{file:id,name:file.name,size:raw.length,sha256},project);
    while(reply.received<raw.length){const offset=reply.received,part=raw.slice(offset,offset+65536);let binary='';for(const c of part)binary+=String.fromCharCode(c);const args={file:id,offset,data:btoa(binary),sha256:await hash(part)};let error;
      for(let tries=0;tries<3;tries++){try{reply=await nativeAPI('upload_chunk',args,project);error=null;break;}catch(e){error=e;await pause(500);}}if(error)throw error;
    }
    reply=await nativeAPI('upload_finish',{file:id},project);if(gen!==n.generation||project!==n.project)continue;
    n.files=n.files.filter(x=>x.file!==id);n.files.push({...reply,size:file.size,sha256,selected:true});nativeAttachments();nativeStatus('附件已校验：'+file.name);
  }
}
async function nativeLoadAttachments(){
  const n=NativeCLIUI,gen=n.generation,project=n.project;
  if(!project)return;
  const data=await nativeAPI('upload_list',{},project);
  if(gen!==n.generation||project!==n.project)return;
  const choices=new Map(n.files.map(x=>[x.file,x.selected]));
  n.files=data.files.map(x=>({...x,selected:choices.get(x.file)||false}));nativeAttachments();
}
function nativeProviderControls(){
  const n=NativeCLIUI,provider=n.selected?.provider||$('#native-cli')?.value||n.provider;
  for(const button of document.querySelectorAll('[data-native-command]')){
    const command=button.dataset.nativeCommand;
    button.hidden=(command==='/thinking'||command==='/login'||command==='/settings')&&provider!=='pi';
  }
  if($('#native-effort'))$('#native-effort').placeholder=provider==='pi'?'off / minimal / low / medium / high / xhigh / max':'none / minimal / low / medium / high / xhigh';
}
function nativeAttachments(){
  const n=NativeCLIUI,el=$('#native-attachments');if(!el)return;el.replaceChildren();
  for(const f of n.files){
    const wrap=document.createElement('span');wrap.className='native-file-card';
    const check=document.createElement('input');check.type='checkbox';check.checked=f.selected;check.disabled=!f.ready;
    check.onchange=()=>f.selected=check.checked;
    const label=document.createElement('label');label.append(check,document.createTextNode(f.name+(f.ready?'':' · 待续传 '+f.received+'/'+f.size)));
    const image=/\.(png|jpe?g|webp|gif)$/i.test(f.path||f.name);
    const insert=document.createElement('button');insert.className='btn small';insert.textContent=image?'附到当前对话':'插入路径';insert.disabled=!f.ready;
    insert.onclick=async()=>{
      try{
        const ticket=n.generation;
        if(!image){n.draft=($('#native-compose').value+' '+JSON.stringify(f.path)).trim();$('#native-compose').value=n.draft;return;}
        if(!n.selected||!n.lease)throw new Error('先打开会话并取得输入权；也可勾选图片随新会话启动');
        const result=await nativeAPI('upload_bind',{id:n.selected.id,writer:n.writer,file:f.file},n.selected.project_id);
        if(ticket!==n.generation)return;
        const token=n.selected.provider==='pi'?result.image_token:JSON.stringify(result.path);
        await nativeInput('\x1b[200~'+token+'\x1b[201~');
        if(ticket===n.generation)nativeStatus('图片已附到原生编辑器，请继续输入要求并按 Enter 发送');
      }catch(e){nativeStatus(e.message);}
    };
    const del=document.createElement('button');del.className='btn small';del.textContent='删除附件';
    del.onclick=async()=>{
      if(!confirm('删除此 RELAY 上传附件？运行会话引用中的文件不能删除，原文件不受影响。'))return;
      try{await nativeAPI('upload_delete',{file:f.file,confirm:f.file});n.files=n.files.filter(x=>x.file!==f.file);nativeAttachments();}catch(e){nativeStatus(e.message);}
    };
    wrap.append(label,insert,del);el.append(wrap);
  }
}

async function nativeExport(){
  const n=NativeCLIUI;if(!n.selected)return;const sid=n.selected.id,parts=[];let offset=0,limit=null;
  while(true){
    const data=await api('/api/native/sessions/'+sid+'/output?offset='+offset);
    if(limit===null)limit=data.session.size;
    for(const chunk of data.chunks){const bytes=Uint8Array.from(atob(chunk),c=>c.charCodeAt(0)),part=bytes.slice(0,Math.max(0,limit-offset));parts.push(part);offset+=part.length;}
    if(offset>=limit||!data.chunks.length)break;
  }
  const url=URL.createObjectURL(new Blob(parts,{type:'application/octet-stream'})),a=document.createElement('a');
  a.href=url;a.download='relay-'+sid+'.ansi';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-native-launch]');if(!b)return;if(typeof chatOpenProject==='function'){chatOpenProject(b.dataset.project,b.dataset.nativeLaunch);return;}NativeCLIUI.project=b.dataset.project;NativeCLIUI.provider=b.dataset.nativeLaunch;navigate('terminal').catch(e=>toast(e.message,true));});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&NativeCLIUI.lease){const n=NativeCLIUI;nativeAPI('detach',{id:n.selected.id,writer:n.writer},n.selected.project_id).catch(()=>{});n.lease=false;if(n.term)n.term.options.disableStdin=true;}});

async function nativeHistory(remove){
  const n=NativeCLIUI,cli=n.selected?.provider||$('#native-cli').value,project=n.project,gen=n.generation;
  let argv=[],resume=true;
  if(remove&&cli==='codex'){
    const id=prompt('输入要清理的 Codex 原生会话 ID 或名称。命令由本机 codex delete 执行；不是删除 RELAY 回放。');
    if(!id)return;
    if(id.length>200||/[\x00-\x1f]/.test(id))throw new Error('无效的原生会话 ID / 名称');
    if(!confirm('请求永久删除这条 Codex 原生历史：'+id+'\n请确认对应任务已停止。随后还可在原生终端核对命令结果。'))return;
    argv=['delete','--',id];resume=false;
  }
  const cwd=n.selected?(n.selected.cwd===n.selected.root?'.':n.selected.cwd.slice(n.selected.root.length+1)):$('#native-cwd').value;
  const row=await nativeAPI('start',{id:uid(),cli,cwd,resume,argv},project);
  if(gen!==n.generation||project!==n.project)return;
  await nativeSelect(row);$('#native-launch-details').open=false;
  if(remove&&cli==='pi')nativeStatus('已打开 Pi 原生历史选择器。取得输入权后，按 Ctrl+D 请求删除所选记录，按原生提示确认；仅清理你明确选择的记录。');
}
