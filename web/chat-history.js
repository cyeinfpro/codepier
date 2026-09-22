'use strict';
// History scope is independent of the current conversation's execution target.
// Project names, paths and conversation titles are rendered as text, never HTML.
function chatProjectInfo(id, row={}) {
  const project=S.projects.find(p=>p.id===id);
  const device=(S.devices||[]).find(d=>d.id===(project?.device_id||row.device_id));
  const online=typeof device?.online==='boolean'?device.online:
    typeof project?.online==='boolean'?project.online:typeof row.online==='boolean'?row.online:null;
  const alias=project?.alias||row.project_alias||'未知项目';
  const node=device?.name||project?.device_name||row.device_name||'本机节点';
  const forbidden=project&&(project.mode!==undefined&&project.mode!=='write'||
    [false,0].includes(project.allow_tasks)||[false,0].includes(project.device_enabled)||[false,0].includes(device?.enabled));
  const reason=!project?'项目映射已不可用，请重新选择项目':forbidden?'项目未允许写入或执行，请在项目映射中检查权限':online===false?'节点离线；可以查看历史和准备草稿，联网后再发送':'';
  return {project,device,online,alias,node,forbidden,reason,root:project?.root||row.root||''};
}
async function chatRefreshTargets(){
  const c=ChatUI,identity=S.session;if(!identity||c.targetsLoading)return;
  c.targetsLoading=true;
  try{const [projects,devices]=await Promise.all([api('/api/projects'),api('/api/devices')]);if(S.session!==identity||S.page!=='native'||!$('#chat-root'))return;if(!Array.isArray(projects.projects)||!Array.isArray(devices.devices))return;S.projects=projects.projects;S.devices=devices.devices;
    for(const [id,value,all] of [['chat-project',c.project,false],['chat-history-project',c.historyProject,true]]){const select=$('#'+id),signature=JSON.stringify(S.projects.map(p=>[p.id,p.alias]));if(select.dataset.options===signature)continue;select.dataset.options=signature;select.replaceChildren();if(all){const option=document.createElement('option');option.value='';option.textContent='所有项目';select.append(option);}for(const p of S.projects){const option=document.createElement('option');option.value=p.id;option.textContent=p.alias;select.append(option);}if(value&&!S.projects.some(p=>p.id===value)){const option=document.createElement('option');option.value=value;option.textContent='项目已不可用';option.disabled=true;select.append(option);}select.value=value||'';}
    const pickerSignature=JSON.stringify(S.projects.map(p=>[p.id,p.alias,p.root,p.device_id,p.mode,p.allow_tasks,p.online]))+JSON.stringify(S.devices.map(d=>[d.id,d.name,d.online,d.enabled]));if(c.pickerSignature!==pickerSignature){c.pickerSignature=pickerSignature;c.projectPickerRefresh?.();}chatSyncChrome();
  }catch{/* Read failures keep the draft and last known target; catalog/list show their own retry state. */}
  finally{if(S.session===identity)c.targetsLoading=false;}
}
function chatMountHistory(){
  const signal=ChatUI.listeners.signal;clearInterval(ChatUI.historyTimer);
  const refresh=()=>{if(S.page==='native'&&!document.hidden&&!ChatUI.historyLoading){chatList();chatRefreshTargets();}};
  ChatUI.historyTimer=setInterval(refresh,15000);
  signal.addEventListener('abort',()=>{clearInterval(ChatUI.historyTimer);ChatUI.historyTimer=null;},{once:true});
  window.addEventListener('online',refresh,{signal});document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();},{signal});
}
function chatHistoryClear() {
  const c=ChatUI;c.query='';c.filter='';c.historyProject='';c.historyOffset=0;c.listTicket++;
  $('#chat-history-search').value='';$('#chat-history-filter').value='';$('#chat-history-project').value='';
  chatList();$('#chat-history-search').focus({preventScroll:true});
}
function chatHistoryControls() {
  const c=ChatUI;
  $('#chat-history-clear').hidden=!(c.query||c.filter||c.historyProject);
  $('#chat-history-project').value=c.historyProject||'';
  for(const id of ['chat-history-prev','chat-history-next'])$('#'+id).disabled=!!c.historyLoading;
}
async function chatList() {
  const c=ChatUI,gen=c.generation,ticket=++c.listTicket,holder=$('#chat-history');
  if(!holder)return;
  const requestedOffset=c.historyOffset,query=c.query.trim();
  c.historyLoading=true;holder.setAttribute('aria-busy','true');chatHistoryControls();
  if(!holder.childElementCount){const note=document.createElement('p');note.className='chat-history-empty';note.textContent='正在加载全部会话…';holder.append(note);}
  try {
    const data=await api('/api/native/sessions?'+new URLSearchParams({project:c.historyProject||'',limit:50,mode:'chat',q:query,status:c.filter,offset:requestedOffset}));
    if(!chatCurrent(gen)||ticket!==c.listTicket)return;
    if(!Array.isArray(data.sessions))throw new Error('没有收到有效的会话列表，请重试。');
    c.rows=data.sessions.filter(row=>row.mode==='chat');c.historyLoaded=true;
    if(Number.isInteger(data.offset))c.historyOffset=data.offset;
    const total=Number.isFinite(data.total)?Math.max(0,data.total):c.rows.length,scroll=holder.scrollTop;
    $('#chat-history-notice').hidden=true;
    $('#chat-history-prev').hidden=!c.historyOffset;
    $('#chat-history-next').hidden=c.historyOffset+c.rows.length>=total||!c.rows.length;
    $('#chat-history-count').textContent=total>50?`${c.historyOffset+1}–${c.historyOffset+c.rows.length} / ${total} 条`:`${total} 条对话`;
    const old=new Map([...holder.querySelectorAll('[data-session-id]')].map(n=>[n.dataset.sessionId,n]));
    const groups=new Map([...holder.querySelectorAll('.chat-history-group')].map(n=>[n.textContent,n]));
    const wanted=[];let lastGroup='';const now=new Date(),yesterday=new Date(now);yesterday.setDate(now.getDate()-1);
    for(const row of c.rows) {
      const time=Number(row.updated||row.created||0)*1000,day=time?new Date(time):null;
      const group=!day?'对话':day.toDateString()===now.toDateString()?'今天':day.toDateString()===yesterday.toDateString()?'昨天':'更早';
      if(group!==lastGroup){const label=groups.get(group)||document.createElement('div');label.className='chat-history-group';label.textContent=group;wanted.push(label);lastGroup=group;}
      let button=old.get(row.id);
      if(!button){
        button=document.createElement('button');button.type='button';button.dataset.sessionId=row.id;
        const title=document.createElement('span'),meta=document.createElement('small'),project=document.createElement('span'),state=document.createElement('span');
        title.className='chat-session-title';meta.className='chat-session-meta';project.className='chat-project-badge';state.className='chat-session-state';
        meta.append(project,state);button.append(title,meta);
      }
      const info=chatProjectInfo(row.project_id||c.project,row),active=row.id===c.selected?.id;
      const title=row.title||'未命名对话',cli=row.provider==='codex'?'Codex':'Pi';
      const state=info.online===false?'节点离线':chatStateName(row.status);
      button.className='chat-session'+(active?' active':'');
      if(active)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
      button.dataset.state=info.online===false?'offline':row.status;
      button.firstElementChild.textContent=title;
      button.querySelector('.chat-project-badge').textContent=info.alias;
      button.querySelector('.chat-session-state').textContent=cli+' · '+state;
      button.title=[title,info.alias+' · '+info.node,row.cwd||info.root,time?new Date(time).toLocaleString('zh-CN',{hour12:false}):''].filter(Boolean).join('\n');
      button.setAttribute('aria-label',title+'，项目 '+info.alias+'，'+info.node+'，'+cli+'，'+state);
      button.onclick=()=>chatSwitch(row,row.project_id||c.project).catch(e=>chatStatus(e.message,true));
      wanted.push(button);
      if(active&&c.selected)c.selected.online=row.online;
    }
    if(!wanted.length) {
      const empty=document.createElement('div'),note=document.createElement('p'),button=document.createElement('button');
      empty.className='chat-history-empty';
      const filtered=!!(c.query||c.filter||c.historyProject);
      note.textContent=filtered?'没有匹配的会话。可以调整关键词或清除筛选。':'还没有对话。选择一个项目，开始第一段工作。';
      button.type='button';button.className='chat-text-button';button.textContent=filtered?'清除筛选':'新对话';
      button.onclick=filtered?chatHistoryClear:()=>chatNewSession();empty.append(note,button);wanted.push(empty);
    }
    chatReconcileNodes(holder,wanted);holder.scrollTop=requestedOffset===c.historyOffset?scroll:0;chatSyncChrome();
  } catch(error) {
    if(chatCurrent(gen)&&ticket===c.listTicket){
      const notice=$('#chat-history-notice');notice.hidden=false;
      notice.querySelector('span').textContent=(c.historyLoaded?'列表未更新，仍显示上次结果。':'会话列表加载失败。')+' '+error.message;
      if(!c.historyLoaded)holder.replaceChildren();
    }
  } finally {
    if(chatCurrent(gen)&&ticket===c.listTicket){c.historyLoading=false;holder.setAttribute('aria-busy','false');chatHistoryControls();}
  }
}
function chatTargetSync() {
  const c=ChatUI,box=$('#chat-new-context');if(!box)return;
  box.hidden=!!c.selected;
  const info=chatProjectInfo(c.project,c.selected||{});
  $('#chat-project').value=c.project;
  $('#chat-target-info').textContent=info.reason||info.node+' · 首条消息发送后才创建会话';
  $('#chat-target-info').title=info.root;
  box.dataset.unavailable=String(!!info.reason);
  const notice=$('#chat-target-notice');if(notice){notice.hidden=!info.reason;notice.querySelector('span').textContent=info.reason;}
  $('#chat-subtitle').textContent=info.project?info.alias+' · '+info.node:'选择工作项目';
  $('#chat-subtitle').title=[info.root,c.selected?.cwd||c.cwd].filter(Boolean).join('\n');
}
function chatNewSession(projectHint=ChatUI.project,providerHint=ChatUI.provider) {
  const root=$('#chat-root');if(!root)return;
  const focused=document.activeElement,origin=root.classList.contains('drawer-open')?$('#chat-new'):$('#chat-options').classList.contains('is-open')?$('#chat-options-toggle'):$('.chat-overflow').open?$('.chat-overflow summary'):focused;
  chatRememberView();ChatUI.dialogCancel?.();chatClosePopover();chatOptions(false);
  const dialog=document.createElement('dialog');
  dialog.id='chat-project-dialog';dialog.className='chat-sheet chat-project-sheet';
  dialog.setAttribute('aria-labelledby','chat-project-dialog-title');dialog.setAttribute('aria-describedby','chat-project-dialog-description');
  dialog.innerHTML='<header class="chat-project-sheet-head"><div><span class="chat-eyebrow">新对话 · 选择项目</span><h2 id="chat-project-dialog-title">这次在哪个项目工作？</h2></div><button type="button" id="chat-project-dialog-close" class="chat-square" aria-label="取消新对话">×</button></header><p id="chat-project-dialog-description">选定项目后再写消息。现在不会启动 CLI，也不会离开或停止原会话。</p><input id="chat-project-search" type="search" role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls="chat-project-results" aria-label="搜索工作项目" placeholder="搜索项目、节点或目录…"><div id="chat-project-results" role="listbox" aria-label="工作项目"></div><footer class="chat-project-sheet-foot"><span>↑ ↓ 选择 · Enter 进入</span><button type="button" id="chat-project-manage" class="chat-text-button">管理项目</button></footer>';
  root.append(dialog);
  const input=dialog.querySelector('input'),results=dialog.querySelector('#chat-project-results');
  let active=-1,finished=false,buttons=[];
  function close() {
    if(finished)return;finished=true;if(ChatUI.dialogCancel===close)ChatUI.dialogCancel=null;ChatUI.projectPickerRefresh=null;
    dialog.close();dialog.remove();
    if(origin?.isConnected&&origin.getClientRects().length&&!origin.closest('[inert]'))origin.focus({preventScroll:true});
  }
  ChatUI.dialogCancel=close;
  async function choose(project) {
    if(finished)return;close();
    ChatUI.recentProjects=[project,...(ChatUI.recentProjects||[]).filter(id=>id!==project)].slice(0,12);
    try {await chatSwitch(null,project,{provider:providerHint,cwd:'.'});if(chatView().draft.trim())chatStatus('已恢复这个项目的未发送草稿；发送后才创建会话。');}catch(e){chatStatus(e.message,true);}
  }
  function highlight(index) {
    active=index;buttons.forEach((b,i)=>{b.dataset.keyboard=String(i===index);b.setAttribute('aria-selected',String(i===index));});
    if(buttons[index]){input.setAttribute('aria-activedescendant',buttons[index].id);buttons[index].scrollIntoView({block:'nearest'});}
    else input.removeAttribute('aria-activedescendant');
  }
  function render() {
    const previousId=buttons[active]?.dataset.projectId,focusedId=document.activeElement?.closest('.chat-project-option')?.dataset.projectId;
    results.replaceChildren();input.removeAttribute('aria-activedescendant');active=-1;buttons=[];
    const q=input.value.trim().toLocaleLowerCase(),recent=ChatUI.recentProjects||[];
    const rank=p=>p.id===projectHint?-2:recent.includes(p.id)?recent.indexOf(p.id):100;
    const projects=[...S.projects].sort((a,b)=>rank(a)-rank(b)||a.alias.localeCompare(b.alias,'zh-CN'));
    for(const project of projects) {
      const info=chatProjectInfo(project.id);
      if(q&&![info.alias,info.node,info.root].join(' ').toLocaleLowerCase().includes(q))continue;
      const button=document.createElement('button'),badge=document.createElement('span'),content=document.createElement('span'),title=document.createElement('strong'),meta=document.createElement('small'),path=document.createElement('span');
      button.type='button';button.className='chat-project-option';button.dataset.projectId=project.id;
      button.id='chat-project-option-'+results.childElementCount;button.setAttribute('role','option');button.setAttribute('aria-selected','false');
      button.disabled=!!info.forbidden;button.title=info.root+(info.reason?'\n'+info.reason:'');
      badge.className='chat-project-initial';badge.textContent=Array.from(info.alias)[0]||'P';
      title.textContent=info.alias;meta.textContent=info.node+' · '+(info.forbidden?'无执行权限':info.online===false?'离线，可准备草稿':info.online===true?'在线':'状态待确认')+(project.id===projectHint?' · 最近使用':'');
      path.className='chat-project-path';path.textContent=info.root;content.append(title,meta,path);button.append(badge,content);
      button.onclick=()=>choose(project.id);results.append(button);if(!button.disabled)buttons.push(button);
    }
    if(!results.childElementCount){const note=document.createElement('p');note.className='chat-picker-empty';note.textContent=S.projects.length?'没有匹配的项目，试试项目名、节点或目录。':'还没有项目。请先在「管理项目」中添加项目映射。';results.append(note);}
    const index=buttons.findIndex(b=>b.dataset.projectId===(focusedId||previousId));if(index>=0){highlight(index);if(focusedId)buttons[index].focus({preventScroll:true});}else if(focusedId)input.focus({preventScroll:true});
  }
  input.oninput=()=>{active=-1;render();};
  dialog.querySelector('#chat-project-dialog-close').onclick=close;
  dialog.querySelector('#chat-project-manage').onclick=()=>{close();Promise.resolve(navigate('projects')).catch(e=>chatStatus(e.message,true));};
  dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
  dialog.addEventListener('keydown',e=>{
    if(e.isComposing||e.keyCode===229)return;
    if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close();return;}
    const focused=buttons.indexOf(document.activeElement);
    if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)&&(e.target===input||focused>=0)&&buttons.length){
      e.preventDefault();const from=focused>=0?focused:active;
      highlight(e.key==='Home'?0:e.key==='End'?buttons.length-1:from<0?(e.key==='ArrowDown'?0:buttons.length-1):(from+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length);
      if(focused>=0)buttons[active]?.focus({preventScroll:true});
    }else if(e.key==='Enter'&&(e.target===input||focused>=0)){
      e.preventDefault();buttons[focused>=0?focused:active>=0?active:0]?.click();
    }
  });
  ChatUI.projectPickerRefresh=render;dialog.showModal();render();input.focus();
}
