'use strict';
// One view per project/native conversation. Model selection never sends a prompt.
const ChatUI={generation:0,project:'',provider:'pi',cwd:'.',selected:null,views:new Map(),source:null,listeners:null,frame:0,dirty:new Set(),cursor:0,listTicket:0,query:'',historyOffset:0,historyProject:'',historyLoading:false,historyLoaded:false,recentProjects:[],filter:'',inspector:'',catalogTicket:0,catalogCache:new Map(),rows:[],connection:'idle',focus:false};
const chatLabels={off:'关闭',none:'不启用',minimal:'最少',low:'低',medium:'中',high:'高',xhigh:'极高',max:'最高'};
function chatIcon(name){const paths={model:'M12 3 3 8l9 5 9-5-9-5ZM3 12l9 5 9-5M3 16l9 5 9-5',brain:'M9 18H7a4 4 0 0 1-3-6 4 4 0 0 1 3-6 3 3 0 0 1 5-2 3 3 0 0 1 5 2 4 4 0 0 1 3 6 4 4 0 0 1-3 6h-2M12 4v16M8 8l2 2M16 8l-2 2M7 14l3-1M17 14l-3-1',plus:'M12 5v14M5 12h14',send:'M12 19V5m-6 6 6-6 6 6',menu:'M4 6h16M4 12h16M4 18h16',search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 6 6',folder:'M3 6h6l2 2h10v12H3z',close:'m6 6 12 12M6 18 18 6',chevron:'m7 10 5 5 5-5',settings:'M4 7h16M4 17h16M9 4v6M15 14v6',file:'M5 3h9l5 5v13H5zM14 3v6h5',command:'M8 5a3 3 0 1 0-3 3h14a3 3 0 1 0-3-3v14a3 3 0 1 0 3-3H5a3 3 0 1 0 3 3V5',terminal:'m4 6 5 5-5 5m8 1h8',refresh:'M20 7v5h-5M4 17v-5h5M19 9a7 7 0 0 0-12-4L4 8m1 7a7 7 0 0 0 12 4l3-3',expand:'M8 3H3v5M16 3h5v5M21 16v5h-5M3 16v5h5',queue:'M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1',stop:'M6 6h12v12H6z',check:'m5 12 4 4L19 6',copy:'M8 8h12v13H8zM4 16H2V2h13v3'};return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${(paths[name]||paths.command).split('|||').map(d=>`<path d="${d}"></path>`).join('')}</svg>`;}
function chatKey(project=ChatUI.project,id=ChatUI.selected?.id){return project+'\n'+(id||'new:'+ChatUI.provider+':'+ChatUI.cwd);}
function chatView(){const key=chatKey();if(!ChatUI.views.has(key))ChatUI.views.set(key,{draft:'',files:[],pending:null,busy:false,items:new Map(),order:[],limit:100,settings:null,catalog:null,catalogError:'',catalogLoading:false,selectedModel:null,selectedEffort:null,active:null,cursor:0,outbox:new Map(),pendingSettings:null,settingOp:null,stats:null,commands:null,mode:'followup',search:'',match:0,queue:[],sessionStatus:ChatUI.selected?.status||'new'});return ChatUI.views.get(key);}
function chatAPI(action,args={},project=ChatUI.project){return api('/api/native/'+action,{method:'POST',body:JSON.stringify({project,args}),retrySafe:true});}
function chatCurrent(gen,view){return !!S.session&&S.page==='native'&&gen===ChatUI.generation&&(!view||view===chatView());}
function chatViewVisible(view,identity=S.session){return !!identity&&S.session===identity&&S.page==='native'&&!!$('#chat-root')&&ChatUI.views.get(chatKey())===view;}
function chatStateName(s){return ({sending:'发送中',queued:'等待执行',claimed:'进行中',starting:'连接中',running:'已连接',stopping:'正在停止',exited:'已停止',interrupted:'已中断',completed:'已完成',start:'开始',started:'执行中',delta:'执行中',end:'完成',error:'失败',cancelled:'已取消',cleared:'已清理',orphaned:'需检查节点',uncertain:'待核实'})[s]||s||'';}
function chatStatus(text,error=false){const el=$('#chat-status');if(el){el.textContent=text;el.classList.toggle('is-error',error);el.dataset.quiet=String(!error&&(!text||text==='Enter 发送 · Shift+Enter 换行'||text==='已连接 · Enter 发送'));}}
function chatDetach(clear=false){
  chatRememberView();chatUnmountChrome();
  const c=ChatUI;c.generation++;c.catalogTicket++;c.source?.close();c.source=null;
  c.listeners?.abort();c.listeners=null;cancelAnimationFrame(c.frame);c.frame=0;c.dirty.clear();
  document.body.classList.remove('chat-focus');
  if(clear){chatCatalogAbortAll();Object.assign(c,{historyProject:'',query:'',filter:'',historyOffset:0,historyLoaded:false,targetsLoading:false,rows:[],recentProjects:[]});for(const v of c.views.values())for(const f of v.files)if(f.preview)URL.revokeObjectURL(f.preview);c.views.clear();c.catalogCache.clear();c.selected=null;c.project='';}
}
async function chatOpenProject(project,provider='pi'){
  if(S.page==='native'&&$('#chat-root'))return chatSwitch(null,project,{provider,cwd:'.'});
  chatDetach();Object.assign(ChatUI,{project,provider,cwd:'.',selected:null,inspector:''});await navigate('native');
}
async function chatSwitch(row,project=row?.project_id||ChatUI.project,options={}){
  const c=ChatUI;chatRememberView();c.dialogCancel?.();chatClosePopover();chatOptions(false);
  c.source?.close();c.source=null;c.generation++;c.catalogTicket++;c.listTicket++;
  cancelAnimationFrame(c.frame);c.frame=0;c.dirty.clear();c.cursor=0;
  c.selected=row;c.project=project;
  if(row){c.provider=row.provider;const root=row.root||S.projects.find(p=>p.id===project)?.root||'',cwd=row.cwd||root;const normalize=p=>p.replaceAll('\\','/').replace(/\/$/,'');const r=normalize(root),d=normalize(cwd);c.cwd=d===r?'.':r&&d.startsWith(r+'/')?d.slice(r.length+1):'.';}
  else{if(options.provider)c.provider=options.provider;if(options.cwd!==undefined)c.cwd=options.cwd;}
  if(!$('#chat-root')||!c.listeners){$('#page').replaceChildren();await chatPage();return;}
  $('#chat-slash').hidden=true;$('#chat-find-bar').hidden=true;$('.chat-overflow').open=false;
  chatDrawer(false,true);chatConnection('idle','就绪');chatStatus('Enter 发送 · Shift+Enter 换行');
  chatRestore();chatSyncChrome();chatList();chatLoadCatalog();
  if(row){chatConnect();const view=chatView();if(view.settingOp&&['sending','queued','claimed','uncertain'].includes(view.settingOp.state))chatWatchReceipt(row.id,view.settingOp.receipt,c.generation,view,view.settingOp);}if(c.inspector)chatInspector(c.inspector);
  const input=$('#chat-compose'),v=chatView();input.focus({preventScroll:true});
  if(v.selection)input.setSelectionRange(...v.selection);
}
async function chatPage(){
  if($('#chat-root')){chatList();return;}
  chatDetach();const c=ChatUI,gen=c.generation;await loadBasics();if(!chatCurrent(gen))return;
  c.project=S.projects.some(p=>p.id===c.project)?c.project:S.work.project||S.projects[0]?.id||'';c.listeners=new AbortController();
  $('#page').innerHTML=`<section id="chat-root" class="chat-workspace" aria-label="CLI 对话工作区">
  <header class="chat-header chat-workspace-bar">${chatWorkspaceBar()}<button class="chat-square chat-drawer-toggle" id="chat-history-toggle" aria-label="切换会话历史" title="会话历史 · ⌘ B" aria-expanded="false">${chatIcon('menu')}</button>
  <div class="chat-heading"><h1 tabindex="-1" id="chat-title">新对话</h1><div class="chat-subline"><span id="chat-subtitle"></span><span id="chat-connection" class="chat-connection" title="连接状态"><i></i><span>就绪</span></span></div></div><div class="chat-header-right">
  <button id="chat-find-toggle" class="chat-square" aria-label="搜索当前对话" title="搜索当前对话">${chatIcon('search')}</button><button id="chat-focus" class="chat-square chat-focus-button" aria-label="切换专注模式" title="专注模式">${chatIcon('expand')}</button><button id="chat-inspector-toggle" class="chat-square" aria-label="会话详情" title="会话详情">${chatIcon('settings')}</button>
  <details class="chat-overflow"><summary aria-label="更多操作">···</summary><div><button type="button" data-chat-action="chat-new">新对话</button><button type="button" class="chat-mobile-action" data-chat-action="chat-find-toggle">搜索当前对话</button><button type="button" class="chat-mobile-action" data-chat-action="chat-inspector-toggle">会话详情</button><button data-devtools="overview">当前项目开发工具</button><button data-devtools="validation">测试与验收</button><button data-devtools="handoff">任务衔接</button><button id="chat-rename">重命名</button><button id="chat-resume">恢复会话</button><button id="chat-export">导出完整 Markdown</button><button id="chat-export-json">导出完整 JSON</button><button id="chat-stop">停止会话进程</button><button id="chat-delete" class="danger">删除网页记录</button></div></details></div></header>
  <div class="chat-body">
  <button id="chat-shade" class="chat-shade" aria-label="关闭会话历史"></button>
  <aside class="chat-sidebar" aria-label="会话历史"><div class="chat-sidebar-head"><span class="chat-sidebar-label">${chatIcon('terminal')}会话空间</span><span class="chat-sidebar-hint">Pi / Codex</span></div><button type="button" id="chat-new" class="chat-new-button" title="新对话" aria-label="新对话">${chatIcon('plus')}<span>新对话</span><kbd data-chat-shortcut="new">⌘ ⇧ O</kbd></button>
  <div class="chat-history-heading"><strong>全部会话</strong><button type="button" id="chat-history-clear" class="chat-text-button" hidden>清除筛选</button></div>
  <label class="chat-history-scope">筛选项目<select id="chat-history-project" aria-label="筛选历史项目"><option value="">所有项目</option>${S.projects.map(p=>`<option value="${esc(p.id)}">${esc(p.alias)}</option>`).join('')}</select></label>
  <div class="chat-history-filter"><input id="chat-history-search" type="search" placeholder="搜索会话、项目或内容" aria-label="查找对话"><select id="chat-history-filter" aria-label="筛选会话"><option value="">全部</option><option value="running">运行中</option><option value="exited">已停止</option><option value="interrupted">已中断</option></select></div>
  <div id="chat-history-notice" class="chat-history-notice" role="alert" hidden><span></span><button type="button" id="chat-history-retry">重试列表</button></div>
  <div id="chat-history" class="chat-history" aria-label="历史对话列表" aria-busy="false"></div><div class="chat-history-pages"><button id="chat-history-prev" hidden>较新</button><span id="chat-history-count"></span><button id="chat-history-next" hidden>较早</button></div>
  <footer class="chat-sidebar-footer"><button type="button" id="chat-command-menu" class="chat-sidebar-action" aria-label="快捷操作" title="快捷操作">${chatIcon('command')}<span>快捷操作</span><kbd data-chat-shortcut="command">⌘ K</kbd></button><div class="chat-sidebar-preferences"><button type="button" data-nav="projects">${chatIcon('folder')}项目管理</button><select id="chat-appearance" aria-label="外观"><option value="auto">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></div></footer></aside>
  <div class="chat-main">
  <div id="chat-find-bar" class="chat-find-bar" hidden><input id="chat-find-input" type="search" placeholder="在已载入消息中查找" aria-label="消息关键词"><span id="chat-find-count"></span><button id="chat-find-prev" aria-label="上一个匹配">↑</button><button id="chat-find-next" aria-label="下一个匹配">↓</button><button id="chat-find-close" aria-label="关闭查找">×</button></div>
  <div id="chat-scroll" class="chat-scroll" tabindex="0" aria-label="对话消息"><button id="chat-older" class="chat-text-button" hidden>显示更早消息</button><div id="chat-empty" class="chat-empty"><div class="chat-empty-mark">${chatIcon('command')}</div><span class="chat-eyebrow">CLI 工作空间</span><h2>今天，想推进什么？</h2><p>选择项目并描述任务，与 ${c.provider==='codex'?'Codex':'Pi'} 一起推进工作。</p><div class="chat-starters"><button data-chat-starter="梳理这个项目的结构，指出值得优先处理的问题。">${chatIcon('folder')}<span><strong>了解项目</strong><small>梳理结构与关键入口</small></span></button><button data-chat-starter="检查当前改动，找出可复现的问题并给出修复建议。">${chatIcon('check')}<span><strong>检查改动</strong><small>找出问题与潜在风险</small></span></button><button data-chat-starter="先阅读项目说明和已有实现，再制定实施计划。">${chatIcon('command')}<span><strong>制定计划</strong><small>把想法拆成具体步骤</small></span></button></div></div><div id="chat-messages" class="chat-messages"></div></div>
  <button id="chat-latest" class="chat-latest" hidden>↓ 回到最新</button>
  <div class="chat-composer-wrap"><div id="chat-activity" class="chat-activity" hidden role="status"><i aria-hidden="true"></i><span id="chat-activity-text"></span><button type="button" id="chat-activity-queue"></button><div class="chat-run-controls"><select id="chat-send-mode" aria-label="发送方式" title="正在执行时如何发送新消息"><option value="followup">排队跟进</option><option value="steer">立即补充</option></select><button type="button" id="chat-interrupt" class="chat-stop-turn" aria-label="中断当前" title="中断当前回复" hidden>${chatIcon('stop')}<span>中断当前</span></button></div></div><div id="chat-outbox" class="chat-outbox" aria-live="polite"></div><div id="chat-files" class="chat-files" aria-live="polite"></div>
  <div id="chat-target-notice" class="chat-catalog-notice" role="status" hidden><span></span><button type="button" id="chat-target-refresh">检查连接</button></div>
  <div id="chat-catalog-notice" class="chat-catalog-notice" hidden><span></span><button id="chat-catalog-retry">重新加载</button></div>


  <form id="chat-form" class="chat-composer"><textarea id="chat-compose" rows="1" aria-label="消息" aria-describedby="chat-status" placeholder="描述任务，输入 / 查看指令…" maxlength="65536"></textarea>
  <div class="chat-compose-bottom"><div class="chat-compose-controls"><button class="chat-square" type="button" id="chat-attach" aria-label="添加文件或图片" title="上传图片与文件">${chatIcon('plus')}</button><input type="file" id="chat-file-input" multiple hidden>  <button type="button" id="chat-model-picker" class="chat-model-picker" aria-label="选择模型" aria-haspopup="dialog" aria-expanded="false">${chatIcon('model')}<span id="chat-model-name">选择模型</span>${chatIcon('chevron')}</button></div>
  <div class="chat-send-actions"><button type="button" id="chat-options-toggle" class="chat-square" aria-label="会话设置" aria-controls="chat-options" aria-expanded="false" title="项目、思考强度与工具">${chatIcon('settings')}</button><button type="submit" id="chat-send" class="chat-send" aria-label="发送消息">${chatIcon('send')}</button></div></div></form>
  <button type="button" id="chat-options-shade" class="chat-options-shade" aria-label="关闭会话设置" tabindex="-1" hidden></button>
  <section id="chat-options" class="chat-options" role="region" aria-label="会话设置"><header class="chat-options-head"><div><strong>会话设置</strong><span>按你的工作方式调整</span></div><button type="button" id="chat-options-close" class="chat-square" aria-label="关闭会话设置">${chatIcon('close')}</button></header>
  <div id="chat-new-context" class="chat-new-context"><label>工作项目<select id="chat-project" aria-label="项目">${S.projects.map(p=>`<option value="${esc(p.id)}" ${p.id===c.project?'selected':''}>${esc(p.alias)}</option>`).join('')}</select></label><button type="button" id="chat-change-project" class="chat-text-button" aria-label="搜索并选择工作项目">选择项目</button><span id="chat-target-info"></span></div>
  <div class="chat-config-bar"><label class="chat-cli-label"><span>CLI</span><select id="chat-provider" aria-label="原生 CLI"><option value="pi">Pi</option><option value="codex">Codex</option></select></label>  <label class="chat-effort-label">${chatIcon('brain')}<span>思考</span><select id="chat-effort-select" aria-label="思考强度"><option value="">本机默认</option></select></label>
  <button type="button" id="chat-cwd-button" class="chat-cwd-button" title="工作目录">${chatIcon('folder')}<span>.</span></button></div>
  <div class="chat-option-tools"><button type="button" id="chat-commands" aria-label="指令与技能">${chatIcon('command')}<span>指令与技能</span></button><button type="button" id="chat-file-library" aria-label="附件库">${chatIcon('file')}<span>附件库</span></button><button type="button" id="chat-usage" title="查看用量">上下文</button></div></section>
  <div class="chat-setting-feedback"><span id="chat-setting-state" class="chat-setting-state"></span><button type="button" id="chat-settings-retry" class="chat-text-button" hidden>重试设置</button></div>
  <div class="chat-composer-note"><span id="chat-status" role="status" data-quiet="true">Enter 发送 · Shift+Enter 换行</span><button id="chat-retry" hidden>重试原请求</button></div></div>
  </div>
  <aside id="chat-inspector" class="chat-inspector" hidden><header><strong>会话详情</strong><button id="chat-inspector-close" class="chat-square" aria-label="关闭会话详情">${chatIcon('close')}</button></header><nav class="chat-inspector-tabs">${[['overview','概览'],['changes','改动'],['files','文件'],['commands','指令'],['queue','队列']].map(([id,label])=>`<button data-chat-tab="${id}">${label}</button>`).join('')}</nav><div id="chat-inspector-body"></div></aside>
  </div><div id="chat-popover" class="chat-popover" role="dialog" aria-label="模型选择" hidden></div><div id="chat-slash" class="chat-slash" role="listbox" aria-label="可用指令" hidden></div></section>`;
  const on=(id,event,fn)=>$('#'+id).addEventListener(event,e=>{const gen=c.generation;try{const r=fn(e);if(r?.catch)r.catch(err=>{if(chatCurrent(gen))chatStatus(err.message,true);});}catch(err){if(chatCurrent(gen))chatStatus(err.message,true);}},{signal:c.listeners.signal});
  on('chat-compose','input',e=>{chatView().draft=e.target.value;chatResizeComposer();chatSlash();chatSyncChrome();});
  on('chat-compose','keydown',e=>{if(e.isComposing||e.keyCode===229)return;if(!$('#chat-slash').hidden&&['ArrowDown','ArrowUp','Tab','Enter','Escape'].includes(e.key)){e.preventDefault();chatSlashKey(e.key);return;}if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();chatSend();}});
  on('chat-form','submit',e=>{e.preventDefault();chatSend();});
  on('chat-new','click',()=>chatNewSession());
  on('chat-options-toggle','click',()=>chatOptions(!$('#chat-options').classList.contains('is-open')));on('chat-options-close','click',()=>chatOptions(false,true));on('chat-options-shade','click',()=>chatOptions(false,true));
  on('chat-change-project','click',()=>chatNewSession());
  on('chat-target-refresh','click',async()=>{const b=$('#chat-target-refresh');b.disabled=true;try{await chatRefreshTargets();}finally{b.disabled=false;}});
  on('chat-history-clear','click',chatHistoryClear);on('chat-history-retry','click',chatList);
  on('chat-history-project','change',e=>{c.historyProject=e.target.value;c.historyOffset=0;chatList();});
  on('chat-project','change',e=>chatSwitch(null,e.target.value,{cwd:'.'}));
  on('chat-provider','change',e=>chatSwitch(null,c.project,{provider:e.target.value}));
  $('#chat-history-search').value=c.query;$('#chat-history-filter').value=c.filter;
  let searchTimer;on('chat-history-search','input',e=>{c.query=e.target.value;c.historyOffset=0;c.listTicket++;chatHistoryControls();clearTimeout(searchTimer);const ticket=c.generation;searchTimer=setTimeout(()=>{if(chatCurrent(ticket))chatList();},200);});on('chat-history-filter','change',e=>{c.filter=e.target.value;c.historyOffset=0;chatList();});
  on('chat-history-prev','click',()=>{c.historyOffset=Math.max(0,c.historyOffset-50);$('#chat-history').scrollTop=0;chatList();});on('chat-history-next','click',()=>{c.historyOffset+=50;$('#chat-history').scrollTop=0;chatList();});
  on('chat-history-toggle','click',()=>chatDrawer(innerWidth>760?!!c.historyCollapsed:!$('#chat-root').classList.contains('drawer-open')));on('chat-shade','click',()=>chatDrawer(false));
  on('chat-root','keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='f'&&e.target.closest('#chat-root')&&!e.target.closest('dialog')){e.preventDefault();chatFindToggle(true);}});
  on('chat-root','click',e=>{const starter=e.target.closest('[data-chat-starter]'),tab=e.target.closest('[data-chat-tab]');if(starter)chatReplaceDraft(starter.dataset.chatStarter);if(tab)chatInspector(tab.dataset.chatTab);if(!e.target.closest('#chat-popover,#chat-model-picker,#chat-cwd-button'))chatClosePopover();});
  on('chat-scroll','scroll',()=>{const v=chatView();v.follow=chatAtBottom();v.scrollTop=$('#chat-scroll').scrollTop;$('#chat-latest').hidden=v.follow;});on('chat-latest','click',chatBottom);on('chat-older','click',()=>{const v=chatView(),el=$('#chat-scroll'),h=el.scrollHeight,t=el.scrollTop;v.limit+=100;chatRenderHistory(false);el.scrollTop=t+el.scrollHeight-h;});
  on('chat-attach','click',()=>$('#chat-file-input').click());on('chat-file-input','change',e=>{chatUpload([...e.target.files]);e.target.value='';});on('chat-root','dragover',e=>{if(e.dataTransfer?.types.includes('Files')){e.preventDefault();$('#chat-root').classList.add('is-dragging');}});on('chat-root','dragleave',e=>{if(!e.currentTarget.contains(e.relatedTarget))e.currentTarget.classList.remove('is-dragging');});on('chat-root','drop',e=>{e.preventDefault();$('#chat-root').classList.remove('is-dragging');chatUpload([...e.dataTransfer.files]);});on('chat-compose','paste',e=>{const files=[...(e.clipboardData?.files||[])];if(files.length){e.preventDefault();chatUpload(files);}});
  on('chat-send-mode','change',e=>{chatView().mode=e.target.value;chatSyncChrome();});on('chat-interrupt','click',chatInterrupt);on('chat-retry','click',()=>chatSend(true));
  on('chat-model-picker','click',()=>chatModelPopover());on('chat-effort-select','change',e=>chatChooseEffort(e.target.value));on('chat-effort-select','focus',()=>{if(!chatModelCurrent())chatLoadCatalog(false,undefined,true);});on('chat-catalog-retry','click',()=>chatLoadCatalog(true));on('chat-settings-retry','click',()=>chatSubmitSettings(null,true));on('chat-cwd-button','click',chatCwdPopover);
  on('chat-commands','click',()=>chatInspector(c.inspector==='commands'?'':'commands'));on('chat-file-library','click',()=>chatInspector(c.inspector==='files'?'':'files'));on('chat-inspector-toggle','click',()=>chatInspector(c.inspector?'':'overview'));on('chat-inspector-close','click',()=>chatInspector(''));on('chat-usage','click',()=>chatInspector('overview'));
  on('chat-focus','click',()=>{c.focus=!c.focus;document.body.classList.toggle('chat-focus',c.focus);chatSyncChrome();chatViewport();});on('chat-activity-queue','click',()=>chatInspector('queue'));
  on('chat-find-toggle','click',()=>chatFindToggle($('#chat-find-bar').hidden));on('chat-find-close','click',()=>chatFindToggle(false));on('chat-find-input','input',e=>{chatView().search=e.target.value;chatView().match=0;chatFind();});on('chat-find-input','keydown',e=>{if(e.key==='Enter'&&!e.isComposing&&e.keyCode!==229){e.preventDefault();chatView().match+=e.shiftKey?-1:1;chatFind();}});on('chat-find-prev','click',()=>{chatView().match--;chatFind();});on('chat-find-next','click',()=>{chatView().match++;chatFind();});
  on('chat-rename','click',chatRename);on('chat-resume','click',chatResume);on('chat-export','click',()=>chatExport('md'));on('chat-export-json','click',()=>chatExport('json'));on('chat-stop','click',chatStop);on('chat-delete','click',chatDelete);
  for(const target of [window,window.visualViewport].filter(Boolean))target.addEventListener('resize',chatViewport,{signal:c.listeners.signal});
  chatMountChrome();chatMountHistory();chatDrawer(false,true);if(c.focus)document.body.classList.add('chat-focus');chatRestore();chatViewport();chatList();chatLoadCatalog();if(c.selected)chatConnect();if(c.inspector)chatInspector(c.inspector);
}
function chatViewport(){
  const root=$('#chat-root');if(!root)return;const viewport=window.visualViewport;
  const gap=innerWidth<=760?0:14;
  // Focusing a control during a resize can scroll the still-tall document.
  // A negative viewport-relative top is NOT extra layout space: counting it
  // keeps the old oversized height and strands controls below the new viewport.
  const top=Math.max(gap,root.getBoundingClientRect().top);
  root.style.height=Math.max(220,(viewport?.height||innerHeight)+(viewport?.offsetTop||0)-top-gap)+'px';
  chatResizeComposer();
}
function chatDrawer(open,switching=false){
  const root=$('#chat-root');if(!root)return;
  const sidebar=$('.chat-sidebar'),hidden=innerWidth<=760&&!open;sidebar.inert=hidden;sidebar.setAttribute('aria-hidden',String(hidden));
  if(innerWidth>760){if(!switching)ChatUI.historyCollapsed=!open;root.classList.toggle('history-collapsed',!!ChatUI.historyCollapsed);root.classList.remove('drawer-open');}
  else {root.classList.toggle('drawer-open',open);$('.chat-main').inert=open;if(open){chatOptions(false);$('#chat-new').focus();}else if(!switching)$('#chat-history-toggle').focus({preventScroll:true});}
  if(innerWidth>760)$('.chat-main').inert=false;
  $('#chat-history-toggle').setAttribute('aria-expanded',String(innerWidth>760?!ChatUI.historyCollapsed:open));chatScheduleLayout();
}
function chatResizeComposer(){const el=$('#chat-compose');if(el){el.style.height='auto';el.style.height=Math.min(Math.max(44,(window.visualViewport?.height||innerHeight)*.18),el.scrollHeight)+'px';chatScheduleLayout();}}
async function chatReplaceDraft(text){const v=chatView(),gen=ChatUI.generation,draft=v.draft;if(draft.trim()&&draft!==text){const answer=await chatDialog({title:'替换当前草稿？',body:'输入框已有未发送内容。取消会保留原草稿，现有附件不会被删除。',confirmLabel:'替换草稿'});if(!answer||!chatCurrent(gen,v)||v.draft!==draft)return false;}chatSetDraft(text);return true;}
function chatSetDraft(text){const v=chatView();v.draft=text;$('#chat-compose').value=text;chatResizeComposer();$('#chat-compose').focus();chatSyncChrome();}
function chatRestore(history=true){const c=ChatUI,v=chatView();v.sessionStatus=c.selected?.status||'new';$('#chat-compose').value=v.draft;$('#chat-provider').value=c.selected?.provider||c.provider;$('#chat-provider').disabled=!!c.selected;$('#chat-title').textContent=c.selected?.title||'新对话';$('#chat-subtitle').textContent=S.projects.find(p=>p.id===c.project)?.alias||'选择项目';$('#chat-empty p').textContent='描述目标，与 '+(c.provider==='codex'?'Codex':'Pi')+' 一起把想法变成进展。';$('#chat-cwd-button span').textContent=c.cwd;$('#chat-cwd-button').title='工作目录：'+(c.selected?.cwd||c.cwd);$('#chat-send').disabled=v.busy;$('#chat-retry').hidden=!v.pending;$('#chat-interrupt').hidden=!v.active;$('#chat-send-mode').value=v.mode;chatResizeComposer();chatFiles();chatOutbox();chatSettings(v.settings);if(history)chatRenderHistory();chatStats();$('#chat-find-input').value=v.search||'';$('#chat-find-count').textContent='';chatTargetSync();chatSyncChrome();}
function chatModelKey(model){if(typeof model==='string')return model;return model?.provider?model.provider+'/'+model.id:model?.model||model?.id||'';}
function chatModelCurrent(v=chatView()){return (!['error','uncertain'].includes(v.settingOp?.state)?v.settingOp?.patch?.model:'')||v.pendingSettings?.model||v.selectedModel||chatModelKey(v.configForResume?v.catalog?.model:v.settings?.model||v.catalog?.model);}
function chatModels(v=chatView()){const rows=v.catalog?.models||v.settings?.models||[];const seen=new Set();return rows.filter(m=>m&&typeof m==='object').filter(m=>{const key=chatModelKey(m);if(!key||seen.has(key))return false;seen.add(key);return true;});}
function chatLevels(v=chatView()){
  const current=chatModelCurrent(v),model=chatModels(v).find(m=>chatModelKey(m)===current||m.id===current||m.model===current);
  const source=ChatUI.selected&&!v.configForResume?(v.settings||v.catalog):(v.catalog||v.settings);
  const confirmed=ChatUI.selected&&!v.configForResume&&chatModelKey(v.settings?.model)===current?v.settings?.thinking_levels:null;
  return (confirmed||model?.thinkingLevels||model?.supportedReasoningEfforts||(chatModelKey(source?.model)===current?source?.thinking_levels:[])||[]).map(x=>typeof x==='string'?x:x.reasoningEffort||x.level).filter(Boolean);
}
function chatSettings(settings){const v=chatView();if(settings)v.settings=settings;const current=chatModelCurrent(v),model=chatModels(v).find(m=>chatModelKey(m)===current||m.model===current||m.id===current);const name=$('#chat-model-name');if(!name)return;name.textContent=model?.name||model?.displayName||current||(v.catalog?'本机默认':'选择模型');$('#chat-model-picker').title=(model?.provider?model.provider+' / ':'')+(model?.id||current||'发送前可选择模型');
  const source=v.configForResume?v.catalog:v.settings||v.catalog;
  const select=$('#chat-effort-select'),levels=chatLevels(v),chosen=(!['error','uncertain'].includes(v.settingOp?.state)?v.settingOp?.patch?.effort:null)??v.pendingSettings?.effort??v.selectedEffort??source?.effort??source?.thinkingLevel??source?.reasoningEffort??'';select.replaceChildren();const opt=document.createElement('option');opt.value='';opt.textContent=v.catalogLoading&&!levels.length?'正在加载…':!levels.length?'本机默认':'默认';select.append(opt);for(const level of [...new Set(levels)]){const o=document.createElement('option');o.value=level;o.textContent=(chatLabels[level]||level)+' · '+level;select.append(o);}if(chosen&&!levels.includes(chosen)){const o=document.createElement('option');o.value=chosen;o.textContent=(chatLabels[chosen]||chosen)+' · 当前设置';select.append(o);}select.value=chosen;select.disabled=v.catalogLoading&&!levels.length||!!v.settingOp&&!['error','uncertain'].includes(v.settingOp.state);select.title=levels.length?'当前模型支持的思考强度':'原生 CLI 尚未返回可选强度；可刷新模型目录';
  const state=$('#chat-setting-state');state.textContent=v.settingOp?(v.settingOp.state==='error'?'设置失败':v.settingOp.state==='uncertain'?'设置待核实':'正在切换…'):v.pendingSettings?'下轮生效':current?(v.configForResume?'恢复时生效':ChatUI.selected?'原生配置':'首条消息生效'):'';$('#chat-settings-retry').hidden=!v.settingOp||!['error','uncertain'].includes(v.settingOp.state);state.parentElement.classList.toggle('has-update',!!v.settingOp||!!v.pendingSettings||!!v.configForResume);
  const notice=$('#chat-catalog-notice');notice.hidden=!v.catalogError;if(v.catalogError)notice.querySelector('span').textContent='模型目录加载失败：'+v.catalogError;
  const steer=$('#chat-send-mode option[value="steer"]');if(steer){const caps=v.settings?.capabilities||v.catalog?.capabilities;steer.disabled=caps?.steer===false;}
  if(ChatUI.inspector==='overview')chatInspectorRender();
  chatSyncChrome();
}
function chatClosePopover(restore=false){const box=$('#chat-popover'),open=box&&!box.hidden;box?.setAttribute('hidden','');$('#chat-model-picker')?.setAttribute('aria-expanded','false');if(open&&restore)$('#'+(ChatUI.popoverAnchor||'chat-model-picker'))?.focus({preventScroll:true});}
function chatModelPopover(){if(innerWidth<=1100&&ChatUI.inspector)chatInspector('');ChatUI.popoverAnchor='chat-model-picker';const box=$('#chat-popover');box.setAttribute('aria-label','模型选择');if(!box.hidden&&$('#chat-model-search')){chatClosePopover();return;}box.hidden=false;box.innerHTML=`<div class="chat-popover-head"><strong>选择模型</strong><button type="button" id="chat-model-refresh" title="重新读取本机模型" aria-label="刷新模型目录">${chatIcon('refresh')}</button><button type="button" id="chat-popover-close" aria-label="关闭模型选择">${chatIcon('close')}</button></div><input id="chat-model-search" type="search" placeholder="搜索名称、模型 ID 或服务商" aria-label="搜索模型" role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls="chat-model-results"><div id="chat-model-results" role="listbox" aria-label="模型目录"></div><div class="chat-popover-foot">↑ ↓ 选择 · Enter 确认 · Esc 关闭</div>`;$('#chat-model-picker').setAttribute('aria-expanded','true');$('#chat-popover-close').onclick=()=>chatClosePopover(true);$('#chat-model-refresh').onclick=()=>chatLoadCatalog(true);$('#chat-model-search').oninput=e=>chatRenderModels(e.target.value);chatRenderModels('');chatPositionLayers();$('#chat-model-search').focus();if(!chatView().catalog&&!chatView().catalogLoading)chatLoadCatalog();}
function chatRenderModels(query){const holder=$('#chat-model-results');if(!holder)return;
  const focused=holder.contains(document.activeElement)?document.activeElement.closest('.chat-model-option'):null,focusModel=focused?focused.dataset.model||'':null;
  const keyboard=holder.dataset.query===query?holder.querySelector('[data-keyboard="true"]'):null,keyboardModel=keyboard?keyboard.dataset.model||'':null;holder.dataset.query=query;holder.replaceChildren();$('#chat-model-search')?.removeAttribute('aria-activedescendant');const v=chatView(),q=query.trim().toLowerCase(),models=chatModels(v).filter(m=>[m.name,m.displayName,m.provider,m.id,m.model].join(' ').toLowerCase().includes(q));if(!models.length){const empty=document.createElement('p');empty.className='chat-picker-empty';empty.textContent=v.catalogLoading?'正在读取本机模型…':v.catalogError||(q?'没有匹配的模型':'本机没有返回可用模型。可刷新目录，或检查节点上的 CLI 登录和配置。');holder.append(empty);if(focusModel!==null)$('#chat-model-search').focus({preventScroll:true});return;}if(!q){const defaults=document.createElement('button');defaults.className='chat-model-option';defaults.type='button';defaults.setAttribute('role','option');defaults.setAttribute('aria-selected','false');defaults.textContent='恢复本机默认模型';defaults.onclick=()=>chatChooseModel('');holder.append(defaults);}const groups=new Map();for(const m of models){const key=m.provider||'Codex';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(m);}for(const [provider,rows] of groups){const group=document.createElement('div');group.className='chat-model-group';group.textContent=provider;holder.append(group);for(const m of rows){const value=chatModelKey(m),button=document.createElement('button');button.type='button';button.className='chat-model-option'+(value===chatModelCurrent(v)?' selected':'');button.setAttribute('role','option');button.setAttribute('aria-selected',String(value===chatModelCurrent(v)));button.dataset.model=value;const main=document.createElement('span'),title=document.createElement('strong'),id=document.createElement('small'),badges=document.createElement('span');title.textContent=m.name||m.displayName||m.id||m.model;id.textContent=m.id||m.model;main.append(title,id);badges.className='chat-model-badges';badges.textContent=[m.input?.includes('image')?'识图':'',m.reasoning||m.supportedReasoningEfforts?.length?'思考':'',m.contextWindow?Math.round(m.contextWindow/1000)+'k':''].filter(Boolean).join(' · ');button.append(main,badges);button.onclick=()=>chatChooseModel(value);holder.append(button);}}
  const options=[...holder.querySelectorAll('.chat-model-option')];
  if(focusModel!==null)(options.find(b=>(b.dataset.model||'')===focusModel)||$('#chat-model-search')).focus({preventScroll:true});
  if(keyboardModel!==null){const active=options.find(b=>(b.dataset.model||'')===keyboardModel);if(active){options.forEach((b,i)=>{b.id='chat-model-option-'+i;b.dataset.keyboard=String(b===active);});$('#chat-model-search').setAttribute('aria-activedescendant',active.id);}}
}
async function chatChooseModel(value){chatClosePopover(true);const v=chatView();if(ChatUI.selected&&['running','starting'].includes(ChatUI.selected.status)){await chatSubmitSettings({model:value});}else{v.configForResume=!!ChatUI.selected;v.selectedModel=value;v.selectedEffort=null;chatSettings(v.settings);await chatLoadCatalog(false,value,!value);}}
async function chatChooseEffort(value){if(!value&&ChatUI.selected&&['running','starting'].includes(ChatUI.selected.status)){await chatSubmitSettings({effort:''});return;}if(!value){const v=chatView();if(!ChatUI.selected||!['running','starting'].includes(ChatUI.selected.status)){v.configForResume=!!ChatUI.selected;v.selectedEffort='';chatStatus('已恢复本机默认思考设置');}chatSettings(v.settings);return;}if(ChatUI.selected&&['running','starting'].includes(ChatUI.selected.status))await chatSubmitSettings({effort:value});else{chatView().configForResume=!!ChatUI.selected;chatView().selectedEffort=value;chatSettings(chatView().settings);}}
async function chatSubmitSettings(patch,retry=false){
  const c=ChatUI,v=chatView(),id=c.selected?.id,project=c.project,identity=S.session;
  const visible=()=>S.session===identity&&S.page==='native'&&!!$('#chat-root')&&chatView()===v;
  if(!id)return;
  if(v.settingOp&&!retry&&v.settingOp.state!=='error'){chatStatus('上一项设置正在确认，请稍候。');return;}
  const op=retry?(v.settingOp?.state==='error'?{receipt:uid(),patch:v.settingOp.patch,state:'sending'}:v.settingOp):{receipt:uid(),patch,state:'sending'};
  if(!op)return;v.settingOp=op;op.state='sending';chatSettings(v.settings);
  try{
    const result=await chatAPI('chat_settings',{id,receipt:op.receipt,...op.patch},project);
    if(S.session!==identity||v.settingOp!==op)return;
    if(op.state==='sending')op.state=result.state||'queued';
    if(visible()){chatSettings(v.settings);chatStatus(op.state==='error'?'原生设置失败，可重试':'设置已提交，等待原生确认',op.state==='error');if(!['error','uncertain'].includes(op.state))chatWatchReceipt(id,op.receipt,c.generation,v,op);}
  }catch(e){
    if(S.session!==identity||v.settingOp!==op)return;
    op.state=['NETWORK_UNCERTAIN','CLI_UNCERTAIN','CLI_OFFLINE'].includes(e.code)?'uncertain':'error';op.error=e.message;
    if(visible()){chatStatus(e.message,true);chatSettings(v.settings);}
  }
}
async function chatWatchReceipt(id,receipt,gen,v,op){const ticket=op.watchTicket=(op.watchTicket||0)+1;for(let n=0;n<24;n++){await new Promise(r=>setTimeout(r,n<3?250:750));if(!chatCurrent(gen,v)||v.settingOp!==op||op.watchTicket!==ticket||op.state==='error')return;try{const result=await chatAPI('receipt',{id,receipt});if(!chatCurrent(gen,v)||v.settingOp!==op||op.watchTicket!==ticket||op.state==='error')return;if(['completed','applied'].includes(result.state)){v.settingOp=null;chatSettings(v.settings);chatStatus(v.pendingSettings?'设置将在下一条消息生效':'设置已由原生 CLI 接受');return;}if(['error','cancelled','uncertain','interrupted'].includes(result.state)){op.state=result.state==='uncertain'?'uncertain':'error';chatStatus('设置未生效，请查看错误或重试原请求',true);chatSettings(v.settings);return;}}catch{}}
  if(chatCurrent(gen,v)&&v.settingOp===op){op.state='uncertain';chatStatus('原生设置仍在等待处理，重试会使用同一回执',true);chatSettings(v.settings);}}
function chatCwdPopover(){
  ChatUI.popoverAnchor='chat-cwd-button';if(ChatUI.selected){chatInspector('overview');return;}
  const box=$('#chat-popover');box.hidden=false;box.setAttribute('aria-label','工作目录');
  box.innerHTML=`<div class="chat-popover-head"><strong>工作目录</strong><button type="button" id="chat-cwd-close" aria-label="关闭工作目录">×</button></div><p class="chat-popover-help">使用项目根目录或项目内子目录，不改变项目授权。不同目录的草稿分别保留。</p><input id="chat-cwd-input" aria-label="项目子目录" aria-describedby="chat-cwd-error" placeholder="例如 src"><p id="chat-cwd-error" role="alert" class="chat-popover-help"></p><button type="button" id="chat-cwd-apply" class="chat-primary-button">应用目录</button>`;
  const input=$('#chat-cwd-input');input.value=ChatUI.cwd;
  $('#chat-cwd-close').onclick=()=>chatClosePopover(true);
  const apply=()=>{const cwd=input.value.trim().replaceAll('\\','/')||'.';if(cwd.startsWith('/')||/^[a-z]:/i.test(cwd)||cwd.split('/').includes('..')||/[\x00-\x1f]/.test(cwd)){$('#chat-cwd-error').textContent='请使用项目内的相对目录，例如 src；不能使用绝对路径或 ..。';input.setAttribute('aria-invalid','true');return;}chatSwitch(null,ChatUI.project,{cwd}).catch(e=>chatStatus(e.message,true));};
  $('#chat-cwd-apply').onclick=apply;input.onkeydown=e=>{if(e.key==='Enter'&&!e.isComposing&&e.keyCode!==229){e.preventDefault();apply();}};
  chatPositionLayers();input.focus();
}
function chatLocalCommand(text){return /^\/(model|thinking|stats|new|queue|commands)\s*$/.test(text.trim());}
async function chatSend(retry=false){
  const c=ChatUI,v=chatView(),project=c.project,identity=S.session;if(v.busy||v.resumeBusy)return;
  const visible=()=>chatViewVisible(v,identity);
  if(!v.pending&&!retry&&chatLocalCommand(v.draft)&&chatBuiltin(v.draft.trim()))return;
  if(['stopping','orphaned'].includes(c.selected?.status)){chatStatus(c.selected.status==='stopping'?'正在等待会话进程退出，草稿已保留。':'会话进程状态待核实，请先检查节点。草稿已保留。',true);return;}
  const target=chatProjectInfo(project,c.selected||{});if(!project||target.reason){chatStatus(target.reason||'请先选择项目',true);if(!project)chatNewSession();return;}
  if(v.settingOp&&!['error'].includes(v.settingOp.state)){chatStatus('请等待模型设置确认后再发送');return;}
  if(!v.pending&&v.files.some(f=>!f.ready)){chatStatus('附件仍在上传，或需要重试失败的附件',true);return;}
  if(v.pending&&!retry){chatStatus('上一条请求尚未确认，请重试原请求',true);return;}
  if(!v.pending){
    const text=v.draft.trim();if(new TextEncoder().encode(text).length>65536){chatStatus('消息不能超过 64 KiB',true);return;}if(!text&&!v.files.length)return;if(chatBuiltin(text))return;
    const resume=c.selected&&!['running','starting'].includes(c.selected.status);
    v.pending={id:!resume&&c.selected?.id||uid(),receipt:uid(),text,attachments:v.files.map(f=>f.file),cli:c.selected?.provider||c.provider,cwd:c.cwd,model:v.selectedModel,effort:v.selectedEffort,started:!!c.selected&&!resume,continue_session:resume?c.selected.id:null,action:v.active&&v.mode==='steer'?'chat_steer':'chat_prompt'};
    // A lost explicit-resume response may already have started the process.
    // Continue that exact launch rather than creating a second continuation.
    if(resume&&v.resumeRequest){v.pending.id=v.resumeRequest.id;v.pending.startArgs=v.resumeRequest;}
  }
  const pending=v.pending;v.outbox.set(pending.receipt,{...pending,state:'sending'});v.busy=true;chatOutbox();chatSyncChrome();chatStatus(pending.continue_session?'正在恢复原生会话…':'正在发送…');
  try{
    if(!pending.started){const row=await chatAPI('start',pending.startArgs||{id:pending.id,cli:pending.cli,mode:'chat',cwd:pending.cwd,...(pending.continue_session?{continue_session:pending.continue_session}:{}),...(pending.model!=null?{model:pending.model}:{}),...(pending.effort!=null?{effort:pending.effort}:{})},project);pending.started=true;pending.row=row;if(v.resumeId===pending.id){v.resumeId=null;v.resumeRequest=null;}}
    // A two-stage first send must never continue under a different login.
    if(S.session!==identity)return;
    const result=await chatAPI(pending.action,{id:pending.id,receipt:pending.receipt,text:pending.text,attachments:pending.attachments},project);
    if(S.session!==identity)return;
    if(['error','cancelled','uncertain','interrupted'].includes(result.state))throw Object.assign(new Error('这条请求状态为「'+chatStateName(result.state)+'」，没有重新执行。请核对历史后再决定。'),{code:result.state==='uncertain'?'CLI_UNCERTAIN':'CLI_RECEIPT_TERMINAL'});
    v.pending=null;if(v.outbox.has(pending.receipt))v.outbox.get(pending.receipt).state=result.state||'queued';
    if(v.draft.trim()===pending.text)v.draft='';v.files=v.files.filter(f=>{if(!pending.attachments.includes(f.file))return true;if(f.preview)URL.revokeObjectURL(f.preview);return false;});
    const current=visible();if(current)chatRememberView();
    if(pending.row&&S.session===identity){const oldKey=[...c.views].find(([,view])=>view===v)?.[0];c.views.set(chatKey(project,pending.id),v);if(oldKey&&oldKey!==chatKey(project,pending.id))c.views.delete(oldKey);}
    if(!current)return;if(pending.row){c.selected=pending.row;v.cursor=0;c.cursor=0;v.configForResume=false;v.selectedModel=null;v.selectedEffort=null;chatConnect();chatList();}chatRestore(false);chatStatus(v.active?'':pending.action==='chat_steer'?'补充指令已提交':'已接收，等待本机处理');
  }catch(e){
    if(S.session!==identity)return;
    const rejected=['CLI_INVALID','CLI_RECEIPT_TERMINAL','ATTACHMENT_NOT_FOUND','CLI_BACKPRESSURE','CLI_QUOTA','CLI_LIMIT','INVALID_CWD','CLI_MISSING','CLI_CHAT_AGENT_UPDATE_REQUIRED','CLI_AGENT_UPDATE_REQUIRED','CLI_NOT_RUNNING'].includes(e.code);
    if(rejected&&!pending.started&&v.resumeRequest?.id===pending.id){v.resumeId=null;v.resumeRequest=null;}
    if(rejected){const current=visible();v.pending=null;v.outbox.delete(pending.receipt);if(pending.row){const key=[...c.views].find(([,view])=>view===v)?.[0];if(key)c.views.delete(key);c.views.set(chatKey(project,pending.id),v);v.sessionRow=pending.row;v.sessionStatus=pending.row.status;if(current){c.selected=pending.row;v.cursor=0;chatConnect();chatRestore(false);}}}
    else if(v.outbox.has(pending.receipt))v.outbox.get(pending.receipt).state='error';
    if(visible()){chatOutbox();chatStatus(e.message,true);$('#chat-retry').hidden=!v.pending;}
  }
  finally{v.busy=false;if(visible())chatSyncChrome();}
}
function chatConnection(state,text){ChatUI.connection=state;const el=$('#chat-connection');if(el){el.dataset.state=state;el.title=text;el.setAttribute('aria-label','连接状态：'+text);el.querySelector('span').textContent=text;}}
function chatConnect(){const c=ChatUI,gen=c.generation,id=c.selected?.id,v=chatView();if(!id)return;c.source?.close();c.cursor=v.cursor||0;
  const source=new EventSource('/api/native/sessions/'+encodeURIComponent(id)+'/events?'+new URLSearchParams({project:c.project,cursor:c.cursor}));c.source=source;
  const receive=e=>{if(!chatCurrent(gen,v)||source!==c.source)return;const cursor=Number(e.lastEventId);if(cursor&&cursor<=c.cursor)return;try{const event=JSON.parse(e.data);chatEvent(event);if(cursor){c.cursor=cursor;v.cursor=cursor;}}catch(error){chatStatus('无法读取结构化事件：'+error.message,true);}};
  source.onmessage=receive;source.addEventListener('chat',receive);
  source.addEventListener('session',e=>{if(!chatCurrent(gen,v)||source!==c.source)return;try{Object.assign(c.selected,JSON.parse(e.data));v.sessionStatus=c.selected.status;$('#chat-title').textContent=c.selected.title||'CLI 对话';if(!['running','starting'].includes(c.selected.status)){v.active=null;v.phase='';for(const item of v.items.values())if(item.kind==='approval'&&!item.data.resolved){item.data.resolved=true;item.data.resolution='closed';chatApproval(item);}$('#chat-interrupt').hidden=true;chatConnection('stopped',chatStateName(c.selected.status));chatStatus(c.selected.error||({stopping:'正在等待会话进程退出，草稿已保留。',orphaned:'会话进程状态待核实，请先检查节点。草稿已保留。'})[c.selected.status]||'会话已停止，发送消息可恢复原生历史');}chatSyncChrome();if(c.inspector==='overview')chatInspectorRender();}catch{}});
  source.addEventListener('error',e=>{if(!chatCurrent(gen,v)||source!==c.source||!e.data)return;try{const error=JSON.parse(e.data);source.close();chatConnection('error','连接受限');chatStatus(error.message||'访问权限已变化',true);}catch{}});
  source.onopen=()=>{if(chatCurrent(gen,v)&&source===c.source){chatConnection('online','实时连接');chatStatus('已连接 · Enter 发送');const op=v.settingOp;if(op&&['sending','queued','claimed','uncertain'].includes(op.state))chatWatchReceipt(id,op.receipt,gen,v,op);}};
  source.onerror=e=>{if(!e.data&&chatCurrent(gen,v)&&source===c.source){chatConnection('reconnecting','重连中');chatStatus('连接暂时中断，正在从已接收的位置恢复…');}};
}
function chatEvent(e){
  const c=ChatUI,v=chatView();if(e.type==='tool')v.phase='tool';if(e.type==='reasoning')v.phase='reasoning';if(e.type==='delta'||e.type==='message')v.phase='reply';if(e.type==='user')v.phase='working';chatScheduleLayout();
  if(e.type==='settings'){v.settings={...(v.settings||{}),...e};if(e.commands)v.commands=e.commands;chatSettings(v.settings);return;}
  if(e.type==='settings_pending'){v.pendingSettings=e.cleared?null:e;chatSettings(v.settings);return;}
  if(e.type==='commands'){v.commands=e.commands||[];if(c.inspector==='commands')chatInspectorRender();return;}
  if(e.type==='stats'){v.stats=e.stats||e;chatStats();if(c.inspector==='overview')chatInspectorRender();return;}
  if(e.type==='command_result'){if(e.state&&!['queued','claimed','running','accepted','uncertain'].includes(e.state)){for(const [key,receipt] of v.controlOps||[])if(receipt===e.receipt)v.controlOps.delete(key);}chatStatus(e.text||e.name+' · '+chatStateName(e.state));if(e.state==='error')chatStatus(e.text||'原生指令失败',true);if(c.inspector)chatInspectorRender();return;}
  if(e.type==='done'){v.phase='';
    if(!e.receipt||v.active===e.receipt){v.active=null;v.interruptReceipt=null;}$('#chat-interrupt').hidden=!v.active;v.outbox.delete(e.receipt);chatOutbox();chatRenderProcessGroups();chatSyncChrome();chatStatus(e.text||chatStateName(e.status));chatList();if(c.inspector==='queue')chatLoadQueue();
    for(const item of v.items.values())if(item.kind==='approval'&&item.data.receipt===e.receipt&&!item.data.resolved){item.data.resolved=true;item.data.resolution='closed';if(item.node){item.node.querySelector('.chat-approval-actions')?.remove();chatApproval(item);}}
    return;
  }
  if(e.type==='queue'||e.type==='cancelled'){if(e.target)v.outbox.delete(e.target);chatOutbox();if(c.inspector==='queue')chatLoadQueue();return;}
  if(!['user','delta','message','reasoning','tool','approval','error','status','review'].includes(e.type))return;
  if(e.type==='status'){chatStatus(e.text||e.status||'');return;}
  if(e.type==='error'&&v.pendingSettings?.receipt===e.receipt){v.pendingSettings=null;chatSettings(v.settings);}
  if(e.type==='error'&&v.settingOp?.receipt===e.receipt){v.settingOp.state='error';chatSettings(v.settings);chatStatus(e.text||'设置失败',true);}
  if(['user','delta','message','reasoning','tool'].includes(e.type)&&['已接收，等待本机处理','补充指令已提交','正在发送…','正在恢复原生会话…'].includes($('#chat-status').textContent))chatStatus('');
  if(e.type==='user'){if(['running','starting'].includes(c.selected?.status)&&!e.parent_receipt&&!e.steering)v.active=e.receipt;v.outbox.delete(e.receipt);chatOutbox();$('#chat-interrupt').hidden=!v.active;}
  const kind=['delta','message'].includes(e.type)?'assistant':e.type,key=(e.receipt||'session')+':'+kind+':'+(e.item_id||e.request_id||e.tool_id||'');let item=v.items.get(key);
  if(!item){item={key,kind,text:'',data:e,node:null,body:null};v.items.set(key,item);v.order.push(key);}
  item.data={...item.data,...e};if(e.type==='approval'&&!['running','starting'].includes(c.selected?.status)){item.data.resolved=true;item.data.resolution='closed';}if(e.type==='approval'&&item.data.resolved&&item.node){item.node.querySelector('.chat-approval-actions')?.remove();chatApproval(item);}
  if(e.type==='delta'||e.type==='reasoning'||e.type==='tool'&&e.status==='delta')item.text+=e.text||e.delta||'';else if(e.text!==undefined)item.text=e.text;else item.text=e.message||e.title||e.name||item.text;
  if(e.type==='review'&&c.inspector==='changes')chatInspectorRender();
  c.dirty.add(key);if(!c.frame)c.frame=requestAnimationFrame(chatFlush);
}
function chatAtBottom(){const el=$('#chat-scroll');return !el||el.scrollHeight-el.scrollTop-el.clientHeight<110;}
function chatBottom(){const el=$('#chat-scroll');if(el)el.scrollTop=el.scrollHeight;if($('#chat-latest'))$('#chat-latest').hidden=true;}
function chatProcessFailure(item){return item.kind==='tool'&&(item.data.isError||item.data.is_error||['error','failed','denied','interrupted','cancelled'].includes(item.data.status));}
function chatReconcileNodes(holder,nodes){
  let cursor=holder.firstChild;
  for(const node of nodes){if(node===cursor)cursor=cursor.nextSibling;else holder.insertBefore(node,cursor);}
  const keep=new Set(nodes);for(const node of [...holder.childNodes])if(!keep.has(node))node.remove();
}
// Group consecutive tool/thinking entries without replacing their disclosure nodes.
function chatRenderProcessGroups(){
  const v=chatView(),holder=$('#chat-messages');if(!holder)return;
  const keys=v.order.slice(-v.limit),visible=new Set(keys),nodes=[],groups=[];
  v.processGroups??=new Map();let group=null;
  for(const key of keys){
    const item=v.items.get(key);if(!item)continue;
    if(!item.node)chatCreateItem(item);chatUpdateItem(item);
    const compact=['tool','reasoning'].includes(item.kind)&&!chatProcessFailure(item);
    if(!compact){group=null;nodes.push(item.node);continue;}
    if(!group||group.receipt!==item.data.receipt){
      let node=v.processGroups.get(key);
      if(!node){
        node=document.createElement('details');node.className='chat-process';node.dataset.groupKey=key;
        const summary=document.createElement('summary'),label=document.createElement('span'),meta=document.createElement('span'),body=document.createElement('div');
        label.className='chat-process-label';label.textContent='执行过程';meta.className='chat-process-meta';body.className='chat-process-body';
        summary.append(label,meta);node.append(summary,body);v.processGroups.set(key,node);
      }
      group={node,receipt:item.data.receipt,items:[]};groups.push(group);nodes.push(node);
    }
    group.items.push(item);
  }
  for(const entry of groups){
    const {node,items,receipt}=entry,tools=items.filter(i=>i.kind==='tool').length,thoughts=items.length-tools;
    const live=!!v.active&&v.active===receipt&&items.some(i=>i.kind==='reasoning'||!['completed','end','succeeded','ok'].includes(i.data.status));
    const counts=[tools?tools+' 项工具':'',thoughts?thoughts+' 段思考':''].filter(Boolean).join(' · ');
    const last=items.at(-1),status=live?' · '+(last.kind==='tool'?(last.data.name||'工具')+' 执行中':'思考中'):'';
    const meta=node.querySelector('.chat-process-meta');if(meta.textContent!==counts+status)meta.textContent=counts+status;
    node.dataset.active=String(live);chatReconcileNodes(node.lastElementChild,items.map(i=>i.node));
  }
  chatReconcileNodes(holder,nodes);
  const retained=new Set(groups.map(g=>g.node.dataset.groupKey));
  for(const key of v.processGroups.keys())if(!retained.has(key))v.processGroups.delete(key);
  for(const [key,item] of v.items)if(item.node&&!visible.has(key)){item.node.remove();item.node=null;item.body=null;}
}
function chatFlush(){
  const c=ChatUI;c.frame=0;if(!$('#chat-messages'))return;
  const v=chatView(),bottom=chatAtBottom();chatRenderProcessGroups();c.dirty.clear();
  $('#chat-empty').hidden=!!v.order.length;$('#chat-older').hidden=v.order.length<=v.limit;
  if(bottom)chatBottom();else $('#chat-latest').hidden=false;chatSyncChrome();
}
function chatRenderHistory(scroll=true){
 const v=chatView();chatRenderProcessGroups();ChatUI.dirty.clear();
 $('#chat-empty').hidden=!!v.order.length;$('#chat-older').hidden=v.order.length<=v.limit;
 if(scroll){if(!v.order.length)$('#chat-scroll').scrollTop=0;else if(v.follow!==false)chatBottom();else $('#chat-scroll').scrollTop=v.scrollTop||0;}
 $('#chat-latest').hidden=!v.order.length||chatAtBottom();
}
function chatCreateItem(item){if(item.kind==='review'){chatCreateReview(item);return;}const article=document.createElement('article');article.className='chat-message chat-message-'+item.kind;article.dataset.key=item.key;const header=document.createElement('div'),label=document.createElement('span'),actions=document.createElement('div');header.className='chat-message-head';label.className='chat-message-label';label.textContent=({user:item.data.parent_receipt?'你 · 补充指令':'你',assistant:ChatUI.selected?.provider==='codex'?'Codex':'Pi',reasoning:'思考过程',tool:'工具执行',approval:'需要确认',error:'未完成'})[item.kind]||item.kind;actions.className='chat-message-actions';
  const copyButton=document.createElement('button');copyButton.type='button';copyButton.title='复制消息';copyButton.setAttribute('aria-label','复制消息');copyButton.innerHTML=chatIcon('copy');copyButton.onclick=()=>chatCopyText(item.text);actions.append(copyButton);
  if(item.kind==='user'){const edit=document.createElement('button');edit.textContent='编辑为新消息';edit.onclick=async()=>{if(await chatReplaceDraft(item.text))chatStatus('已复制到输入框；发送后是新消息，不会撤销先前的工具操作。');};actions.append(edit);}
  header.append(label,actions);article.append(header);let body;
  if(['reasoning','tool'].includes(item.kind)){const details=document.createElement('details'),summary=document.createElement('summary');body=document.createElement('pre');details.append(summary,body);article.append(details);}else{body=document.createElement('div');body.className='chat-message-body';article.append(body);}
  item.node=article;item.body=body;item.renderedSignature=null;
  if(item.kind==='user'&&item.data.attachments?.length){const files=document.createElement('div');files.className='chat-message-files';for(const f of item.data.attachments){const badge=document.createElement('span');badge.textContent=f.name||'附件';badge.title=f.mime||'';files.append(badge);}article.append(files);}
  if(item.kind==='approval'){const fields=item.data.details||{},context=fields.message||fields.command||fields.commandActions||fields.changes||fields.fileChanges;if(context){const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='操作内容';pre.textContent=(typeof context==='string'?context:JSON.stringify(context,null,2))+(fields.cwd?'\n工作目录：'+fields.cwd:'');details.open=true;details.append(summary,pre);article.append(details);}chatApproval(item);}
}
function chatUpdateItem(item){if(item.kind==='review'){chatUpdateReview(item);return;}const signature=item.text+'\0'+(item.data.status||'')+'\0'+(item.data.name||'')+'\0'+!!(item.data.isError||item.data.is_error);if(item.renderedSignature===signature&&item.body?.hasChildNodes())return;item.renderedSignature=signature;if(item.kind==='assistant'){if(typeof chatRichMarkdown==='function')chatRichMarkdown(item.body,item.text);else item.body.textContent=item.text;}else if(item.kind==='tool'){const summary=item.node.querySelector('summary');let parsed;try{parsed=JSON.parse(item.text);}catch{}const failed=chatProcessFailure(item);if(failed&&!item.node.classList.contains('chat-tool-failed'))item.node.querySelector('details').open=true;item.node.classList.toggle('chat-tool-failed',!!failed);summary.textContent=(item.data.name||'工具')+' · '+(failed?'执行异常':chatStateName(item.data.status));item.body.textContent=parsed?JSON.stringify(parsed,null,2):item.text;}else{item.body.textContent=item.text;if(item.kind==='reasoning')item.node.querySelector('summary').textContent='查看思考过程 · '+item.text.length+' 字符';}}
async function chatCopyText(text){if(typeof copy==='function'){await copy(text);return;}try{await navigator.clipboard.writeText(text);chatStatus('已复制');}catch{chatStatus('浏览器未允许访问剪贴板，可选中文字复制');}}
function chatApproval(item){
  if(!item.node)return;
  item.node.querySelectorAll('.chat-approval-actions').forEach(node=>node.remove());
  const data=item.data,box=document.createElement('div');box.className='chat-approval-actions';
  if(data.resolved||item.answered){box.textContent=data.resolution==='closed'?'此请求已结束':'回答已提交，等待原生处理';box.setAttribute('role','status');item.node.append(box);return;}
  async function answer(value){
    const op=item.answerRequest||(item.answerRequest={receipt:uid(),value,id:ChatUI.selected?.id,project:ChatUI.project,identity:S.session,view:chatView()});
    if(op.busy||op.identity!==S.session||item.data.resolved||item.answered||!['running','starting'].includes(op.view.sessionStatus))return;
    op.busy=true;op.error='';chatApproval(item);
    try{
      const result=await chatAPI('chat_answer',{id:op.id,receipt:op.receipt,request_id:data.request_id,answer:op.value},op.project);
      if(op.identity!==S.session)return;
      if(['error','uncertain','cancelled','interrupted'].includes(result.state))throw new Error('回答未被原生确认，请重试原回答核实。');
      item.answered=true;
    }catch(error){if(op.identity===S.session){op.error=error.message;if(chatViewVisible(op.view,op.identity))chatStatus(error.message,true);}}
    finally{
      op.busy=false;
      if(op.identity===S.session&&[...ChatUI.views.values()].includes(op.view)){chatApproval(item);if(chatViewVisible(op.view,op.identity))chatSyncChrome();}
    }
  }
  // The response owns this card, even while its conversation is detached.
  // A retry always retains the user's original answer and receipt.
  if(item.answerRequest){
    const op=item.answerRequest,note=document.createElement('span');note.setAttribute('role','status');note.textContent=op.busy?'正在提交回答…':op.error||'回答仍待确认';box.append(note);
    if(!op.busy){const retry=document.createElement('button');retry.type='button';retry.textContent='重试原回答';retry.onclick=()=>answer(op.value);box.append(retry);}
    item.node.append(box);return;
  }
  function button(label,value){const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=()=>{try{answer(typeof value==='function'?value():value);}catch(e){chatStatus(e.message,true);}};box.append(b);}
  if(data.method==='confirm'){button('允许本次',true);button('拒绝',false);}
  else if(['input','editor'].includes(data.method)){const input=document.createElement('textarea');input.setAttribute('aria-label',data.text||'回答');input.value=data.details?.prefill||'';box.append(input);button('提交回答',()=>input.value);button('取消',null);}
  else if(typeof chatExtraApproval==='function'&&chatExtraApproval(data,box,button)){}
  else if(data.details?.questions?.length){const inputs=[];for(const q of data.details.questions){const label=document.createElement('label');label.textContent=q.question||q.header||q.id;let input;if(q.options?.length){input=document.createElement('select');for(const o of q.options){const option=document.createElement('option');option.value=o.label||o.value;option.textContent=(o.label||o.value)+(o.description?' — '+o.description:'');input.append(option);}}else{input=document.createElement('input');if(q.isSecret)input.type='password';}input.setAttribute('aria-label',label.textContent);label.append(input);box.append(label);inputs.push([q.id,input]);}button('提交回答',()=>Object.fromEntries(inputs.map(([id,input])=>[id,[input.value]])));}
  else{const choices=data.choices||data.options||[];for(const choice of choices){const value=typeof choice==='string'?choice:choice.value??choice.id??choice;const label=typeof choice==='string'?({accept:'允许本次',acceptForSession:'本会话允许',decline:'拒绝',cancel:'取消'}[choice]||choice):choice.label||JSON.stringify(value);button(label,value);}if(!choices.length){const note=document.createElement('p');note.textContent='此原生请求没有可用选项。可中断当前任务后，在节点本机 CLI 处理此操作。';box.append(note);}}
  item.node.append(box);
}
function chatOutbox(){const holder=$('#chat-outbox');if(!holder)return;holder.replaceChildren();if(chatView().recoveredQueueDrafts?.length||chatView().recoveredQueueDraft){const recover=document.createElement('button');recover.type='button';recover.className='chat-text-button';recover.id='chat-recover-queue-draft';recover.textContent='取回已撤回消息';recover.onclick=()=>chatRestoreQueueDraft();holder.append(recover);}for(const item of chatView().outbox.values()){const row=document.createElement('div');row.className='chat-queued';const status=document.createElement('span'),text=document.createElement('span');status.textContent=chatStateName(item.state);text.textContent=item.text||'图片与文件';row.append(status,text);if(item.state==='queued'){const cancel=document.createElement('button');cancel.type='button';cancel.textContent='撤回';cancel.onclick=async()=>{cancel.disabled=true;try{await chatCancelQueued(item.receipt,item.text);}finally{cancel.disabled=false;}};row.append(cancel);}holder.append(row);}}
async function chatCancelQueued(receipt,text){const gen=ChatUI.generation,id=ChatUI.selected?.id,v=chatView();if(!id)return;const key='cancel:'+receipt;v.cancelOps=v.cancelOps||new Map();if(!v.cancelOps.has(key))v.cancelOps.set(key,uid());try{const result=await chatAPI('chat_cancel',{id,receipt:v.cancelOps.get(key),target:receipt});if(result.state!=='completed'){if(chatCurrent(gen,v))chatStatus('撤回仍待确认，消息暂时保留在队列中。');return;}v.outbox.delete(receipt);v.queueTicket=(v.queueTicket||0)+1;v.queue=v.queue.filter(item=>item.receipt!==receipt);if(chatCurrent(gen,v)){chatOutbox();chatStatus('已撤回尚未执行的消息');if(ChatUI.inspector==='queue')chatLoadQueue();}}catch(e){if(chatCurrent(gen,v))chatStatus(e.message,true);}}
async function chatInterrupt(){const c=ChatUI,v=chatView(),gen=c.generation,id=c.selected?.id;if(!id||!v.active)return;v.interruptReceipt=v.interruptReceipt||uid();try{await chatAPI('chat_interrupt',{id,receipt:v.interruptReceipt});if(chatCurrent(gen,v))chatStatus('正在中断当前回复；排队消息仍会继续');}catch(e){if(chatCurrent(gen,v))chatStatus(e.message,true);}}
async function chatRename(){
 const row=ChatUI.selected,v=chatView(),gen=ChatUI.generation,project=ChatUI.project;if(!row)return;
 const title=await chatDialog({title:'重命名对话',body:'给这段工作起个容易找到的名字。',value:row.title||'',confirmLabel:'保存'});
 if(!title||!chatCurrent(gen,v))return;
 await chatAPI('rename',{id:row.id,title},project);
 if(chatCurrent(gen,v)){row.title=title;$('#chat-title').textContent=title;chatList();}
}
async function chatResume(){
  const c=ChatUI,row=c.selected,v=chatView(),project=c.project,identity=S.session;if(!row||v.resumeBusy||v.busy)return;
  if(v.pending){chatStatus('上一条消息尚未确认，请重试原请求。草稿已保留。',true);return;}
  if(['running','starting','stopping','orphaned'].includes(row.status)){chatStatus('会话进程尚未退出，请先核实节点状态。');return;}
  v.resumeId=v.resumeId||uid();v.resumeRequest=v.resumeRequest||{id:v.resumeId,cli:row.provider,mode:'chat',cwd:c.cwd,continue_session:row.id,...(v.selectedModel!=null?{model:v.selectedModel}:{}),...(v.selectedEffort!=null?{effort:v.selectedEffort}:{})};v.resumeBusy=true;chatSyncChrome();
  try{const next=await chatAPI('start',v.resumeRequest,project);
    if(S.session!==identity||![...c.views.values()].includes(v))return;
    const current=chatViewVisible(v,identity);v.resumeId=null;v.resumeRequest=null;v.cursor=0;v.configForResume=false;v.selectedModel=null;v.selectedEffort=null;
    c.views.set(chatKey(project,next.id),v);if(current)await chatSwitch(next,project);c.views.delete(chatKey(project,row.id));
  }catch(e){
    // Definitive launch rejection permits correcting the next attempt. Lost
    // responses keep the immutable request so retrying confirms one process.
    if(S.session===identity&&e.status>=400&&e.status<500&&e.status!==408&&!['CLI_UNCERTAIN','CLI_OFFLINE'].includes(e.code)){v.resumeId=null;v.resumeRequest=null;}
    throw e;
  }finally{v.resumeBusy=false;if(chatViewVisible(v,identity))chatSyncChrome();}
}
async function chatStop(){
 const row=ChatUI.selected,v=chatView(),gen=ChatUI.generation,project=ChatUI.project;if(!row)return;
 const confirmed=await chatDialog({title:'停止会话？',body:'当前任务和排队消息会中断。对话历史与项目文件会保留。',confirmLabel:'停止会话',danger:true});
 if(!confirmed||!chatCurrent(gen,v))return;
 v.stopReceipt=v.stopReceipt||uid();await chatAPI('stop',{id:row.id,receipt:v.stopReceipt},project);
 if(chatCurrent(gen,v))chatStatus('已请求停止，等待原生进程退出');
}
async function chatDelete(){
 const c=ChatUI,row=c.selected,v=chatView(),gen=c.generation,project=c.project;if(!row)return;
 if(['starting','running','stopping','orphaned'].includes(row.status)){chatStatus('请先停止会话，确认进程退出后再删除记录',true);return;}
 const confirmed=await chatDialog({title:'删除网页记录？',body:'仅删除这条网页会话记录，不删除本机原生历史或项目文件。',confirmLabel:'删除记录',danger:true});
 if(!confirmed||!chatCurrent(gen,v))return;
 await chatAPI('delete',{id:row.id,confirm:row.id},project);
 if(chatCurrent(gen))await chatSwitch(null,project);
 c.views.delete(chatKey(project,row.id));for(const file of v.files){file.removed=true;if(file.preview)URL.revokeObjectURL(file.preview);}
}
function chatExport(format){const sid=ChatUI.selected?.id;if(!sid){chatStatus('请先打开一条会话');return;}const a=document.createElement('a');a.href='/api/native/sessions/'+encodeURIComponent(sid)+'/export?format='+format;a.download='conversation.'+format;a.click();chatStatus('正在导出此会话的完整已同步记录');}
function chatFindToggle(open){if(open){chatClosePopover();chatOptions(false);if($('#chat-root').classList.contains('drawer-open'))chatDrawer(false,true);if(ChatUI.inspector&&innerWidth<=1100)chatInspector('');}$('#chat-find-bar').hidden=!open;if(open){$('#chat-find-input').value=chatView().search||'';chatFind();$('#chat-find-input').focus();}else{for(const el of document.querySelectorAll('.chat-message.search-match'))el.classList.remove('search-match');$('#chat-compose').focus();}}
function chatFind(){const v=chatView(),q=v.search.toLocaleLowerCase();for(const el of document.querySelectorAll('.chat-message.search-match'))el.classList.remove('search-match');const keys=q?v.order.filter(k=>v.items.get(k).text.toLocaleLowerCase().includes(q)):[];for(const id of ['chat-find-prev','chat-find-next'])$('#'+id).disabled=!keys.length;if(!keys.length){$('#chat-find-count').textContent=q?'无匹配':'';return;}v.match=(v.match%keys.length+keys.length)%keys.length;const item=v.items.get(keys[v.match]);if(!item.node){v.limit=Math.max(v.limit,v.order.length-v.order.indexOf(item.key));chatRenderHistory(false);}item.node.classList.add('search-match');const group=item.node.closest('.chat-process');if(group)group.open=true;const detail=item.node.querySelector('details');if(detail)detail.open=true;item.node.scrollIntoView({block:'center',behavior:'auto'});$('#chat-find-count').textContent=(v.match+1)+' / '+keys.length;}
function chatFileLocked(view,file){return !!view.busy||!!view.pending?.attachments?.includes(file?.file);}
function chatFiles(){const holder=$('#chat-files');if(!holder)return;holder.replaceChildren();for(const f of chatView().files){const chip=document.createElement('span');chip.className='chat-file';if(f.preview){const image=document.createElement('img');image.src=f.preview;image.alt=f.name;chip.append(image);}const text=document.createElement('span');text.textContent=f.name;const state=document.createElement('small');state.textContent=f.error?'上传失败':f.ready?'已校验':(f.progress||0)+'%';state.title=f.errorMessage||'';const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','移除附件 '+f.name);remove.disabled=chatFileLocked(chatView(),f);remove.title=remove.disabled?'正在确认消息中的附件，暂不能移除':'移除附件';remove.onclick=()=>{if(chatFileLocked(chatView(),f))return;f.removed=true;const v=chatView();v.files=v.files.filter(x=>x!==f);if(f.preview)URL.revokeObjectURL(f.preview);chatFiles();};chip.append(text,state);if(f.error&&f.source){const retry=document.createElement('button');retry.type='button';retry.textContent='重试';retry.onclick=()=>chatUploadEntry(f);chip.append(retry);}chip.append(remove);holder.append(chip);}chatSyncChrome();}
async function chatUpload(files){
  const v=chatView(),project=ChatUI.project,identity=S.session;
  if(!project){chatStatus('请先选择项目');return;}
  if(v.files.length+files.length>10){chatStatus('每条消息最多 10 个附件',true);return;}
  const entries=files.map(file=>{const entry={name:file.name,ready:false,source:file,progress:0};if(file.type.startsWith('image/')&&file.type!=='image/svg+xml')entry.preview=URL.createObjectURL(file);return entry;});
  v.files.push(...entries);chatFiles();
  for(const entry of entries){if(S.session!==identity)break;if(!entry.removed)await chatUploadEntry(entry,v,project,identity);}
}
async function chatUploadEntry(entry,v=chatView(),project=ChatUI.project,identity=S.session){
  const file=entry.source;if(entry.uploading||entry.removed)return;
  const visible=()=>S.session===identity&&S.page==='native'&&$('#chat-root')&&chatView()===v;
  const mapping=()=>{const p=S.projects.find(p=>p.id===project);return JSON.stringify([project,p?.root,p?.device_id]);};
  entry.mapping??=mapping();
  const valid=()=>{if(S.session!==identity||entry.removed)return false;if(entry.mapping!==mapping())throw new Error('项目映射已改变，上传已停止。请核对工作项目后移除并重新添加附件。');return true;};
  entry.uploading=true;entry.error=false;entry.errorMessage='';if(visible())chatFiles();
  try{
    if(!file.size||file.size>20*1024*1024)throw new Error('每个附件须为 1 字节–20 MiB');
    const raw=new Uint8Array(await file.arrayBuffer()),sha256=await nativeSha256(raw);if(!valid())return;
    const catalog=await chatAPI('upload_list',{},project);if(!valid())return;
    const existing=(catalog.files||[]).find(x=>x.name===file.name&&x.size===raw.length&&x.sha256===sha256),id=existing?.file||entry.file||uid();entry.file=id;
    let reply=await chatAPI('upload_begin',{file:id,name:file.name,size:raw.length,sha256},project);
    if(!Number.isInteger(reply.received)||reply.received<0||reply.received>raw.length)throw new Error('附件进度无效，请重试。');
    while(reply.received<raw.length){
      if(!valid())return;
      const offset=reply.received,part=raw.slice(offset,offset+65536);let binary='';for(const byte of part)binary+=String.fromCharCode(byte);
      const payload={file:id,offset,data:btoa(binary),sha256:await nativeSha256(part)};let error;
      for(let attempt=0;attempt<3;attempt++){
        if(!valid())return;
        try{reply=await chatAPI('upload_chunk',payload,project);error=null;break;}
        catch(e){error=e;if(attempt<2)await new Promise(r=>setTimeout(r,300*(attempt+1)));}
      }
      if(error)throw error;
      if(!Number.isInteger(reply.received)||reply.received<=offset||reply.received>raw.length)throw new Error('附件进度未得到有效确认，请重试原附件。');
      entry.progress=Math.round(reply.received/raw.length*100);if(visible())chatFiles();
    }
    if(!valid())return;
    const finished=await chatAPI('upload_finish',{file:id},project);if(!finished.ready)throw new Error('附件尚未完成校验');
    if(valid())Object.assign(entry,{ready:true,progress:100});
  }catch(e){entry.error=true;entry.errorMessage=e.message;if(visible())chatStatus(e.message,true);}
  finally{entry.uploading=false;if(visible())chatFiles();}
}
