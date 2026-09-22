'use strict';
// Window presentation only. Native operations remain in chat.js / chat-panels.js.
// No conversations, credentials, prompts or attachments are persisted here.
function chatWorkspaceBar() {
  const back='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m10 5-7 7 7 7M3 12h18"/></svg>';
  return `<button type="button" id="chat-back" class="chat-back-button" data-nav="overview" title="返回面板，当前会话和草稿会保留">${back}<span>返回面板</span></button>`;
}
function chatRememberView() {
  if (!$('#chat-root') || !ChatUI.listeners) return;
  if (ChatUI.frame) { cancelAnimationFrame(ChatUI.frame); chatFlush(); }
  const v=chatView(), scroll=$('#chat-scroll'), input=$('#chat-compose');
  v.scrollTop=scroll?.scrollTop||0; v.follow=chatAtBottom();
  v.selection=input ? [input.selectionStart,input.selectionEnd] : null;
}
function chatApplyAppearance(value) {
  const c=ChatUI;
  const appearance=window.CodePierAppearance;
  if(appearance){
    if(value)appearance.setPreference(value);
    c.appearance=appearance.getPreference();
    const scheme=appearance.getScheme(),root=$('#chat-root');
    if(root&&root.dataset.appearance!==scheme){
      root.classList.add('appearance-changing');root.dataset.appearance=scheme;
      void root.offsetHeight;root.classList.remove('appearance-changing');
    }
    document.body.dataset.chatAppearance=scheme;
    const control=$('#chat-appearance');
    if(control){control.value=c.appearance;control.setAttribute('aria-label','全站外观');}
    return;
  }
  if (value) { c.appearance=value; try { localStorage.setItem('codepier-chat-appearance',value); } catch {} }
  if (!c.appearance) { try { c.appearance=localStorage.getItem('codepier-chat-appearance'); } catch {} }
  if (!['auto','light','dark'].includes(c.appearance)) c.appearance='auto';
  const scheme=c.appearance==='auto' ? (matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light') : c.appearance;
  const root=$('#chat-root');
  if(root&&root.dataset.appearance!==scheme){
    // Apply the whole color scheme atomically; retain hover animation afterward.
    root.classList.add('appearance-changing');root.dataset.appearance=scheme;
    void root.offsetHeight;root.classList.remove('appearance-changing');
  }
  document.body.dataset.chatAppearance=scheme;
  const control=$('#chat-appearance');if(control)control.value=c.appearance;
}
function chatMountChrome() {
  const c=ChatUI, root=$('#chat-root'), signal=c.listeners.signal;
  document.body.classList.add('chat-mode');
  document.title='CLI 会话 · CodePier';
  if(typeof uiPageReady==='function')uiPageReady(false);
  chatApplyAppearance();
  const mac=/Mac|iPhone|iPad/.test(navigator.platform), modifier=mac?'⌘':'Ctrl';
  root.querySelector('[data-chat-shortcut="new"]').textContent=modifier+' ⇧ O';
  root.querySelector('[data-chat-shortcut="command"]').textContent=modifier+' K';
  $('#chat-history-toggle').title='会话历史 · '+modifier+' B';
  $('#chat-appearance').addEventListener('change',e=>chatApplyAppearance(e.target.value),{signal});
  if(window.CodePierAppearance){
    c.appearanceUnsubscribe?.();
    c.appearanceUnsubscribe=window.CodePierAppearance.subscribe(()=>chatApplyAppearance());
    signal.addEventListener('abort',()=>{c.appearanceUnsubscribe?.();c.appearanceUnsubscribe=null;},{once:true});
  }else{
    const media=matchMedia('(prefers-color-scheme: dark)'),listener=()=>chatApplyAppearance();
    if(typeof media.addEventListener==='function')media.addEventListener('change',listener,{signal});
    else if(typeof media.addListener==='function'){media.addListener(listener);signal.addEventListener('abort',()=>media.removeListener(listener),{once:true});}
  }
  $('#chat-command-menu').addEventListener('click',chatPalette,{signal});
  c.resizeObserver=new ResizeObserver(()=>chatScheduleLayout());
  for(const el of [root,$('.chat-composer-wrap'),$('.chat-header')])if(el)c.resizeObserver.observe(el);
  window.visualViewport?.addEventListener('scroll',chatScheduleLayout,{signal});
  root.addEventListener('keydown',chatInteractionKey,{signal});
  root.addEventListener('click',e=>{
    if(e.target.closest('.chat-overflow summary')&&root.classList.contains('drawer-open'))chatDrawer(false,true);
    if($('#chat-options').classList.contains('is-open')&&!e.target.closest('#chat-options,#chat-popover,#chat-options-toggle'))chatOptions(false,!e.target.closest('button,summary,input,select'));
    const action=e.target.closest('[data-chat-action]');if(action){const target=$('#'+action.dataset.chatAction);if(action.dataset.chatAction==='chat-inspector-toggle')c.inspectorOrigin=$('.chat-overflow summary');target?.click();}
    const menu=$('.chat-overflow');
    if(menu?.open&&(!menu.contains(e.target)||e.target.closest('button')))menu.open=false;
  },{signal});
  root.addEventListener('focusin',e=>{
    const menu=$('.chat-overflow');if(menu?.open&&!menu.contains(e.target))menu.open=false;
  },{signal});
  c.mobileMedia=matchMedia('(max-width: 760px)');
  c.mobileMedia.addEventListener('change',()=>{const panel=$('#chat-options'),directoryOpen=!$('#chat-popover').hidden&&c.popoverAnchor==='chat-cwd-button',restore=panel.classList.contains('is-open')||panel.contains(document.activeElement)||directoryOpen;if(directoryOpen)chatClosePopover();chatOptions(false,restore);chatDrawer(false,true);chatInspector(c.inspector);chatResizeComposer();chatScheduleLayout();},{signal});
  chatOptions(false);chatSyncChrome();
}
// A compact, non-modal settings sheet; native controls keep their state and handlers.
function chatOptions(open,restoreFocus=false) {
  const panel=$('#chat-options');if(!panel)return;
  const mobile=innerWidth<=760;open=!!open&&mobile;
  if(open){chatClosePopover();chatDrawer(false,true);if(ChatUI.inspector)chatInspector('');$('#chat-slash').hidden=true;$('.chat-overflow').open=false;}
  panel.classList.toggle('is-open',open);panel.setAttribute('role',mobile?'dialog':'region');
  panel.setAttribute('aria-hidden',String(mobile&&!open));
  $('#chat-options-toggle').setAttribute('aria-expanded',String(open));$('#chat-options-shade').hidden=!open;
  if(open)$('#chat-options-close').focus({preventScroll:true});
  else if(restoreFocus){const target=mobile?$('#chat-options-toggle'):$('#chat-model-picker');target.focus({preventScroll:true});}
  chatScheduleLayout();
}
function chatUnmountChrome() {
  ChatUI.dialogCancel?.(); ChatUI.dialogCancel=null;
  ChatUI.appearanceUnsubscribe?.(); ChatUI.appearanceUnsubscribe=null;
  ChatUI.resizeObserver?.disconnect(); ChatUI.resizeObserver=null;
  cancelAnimationFrame(ChatUI.layoutFrame); ChatUI.layoutFrame=0;
  document.body.classList.remove('chat-mode');
}
function chatScheduleLayout() {
  if(ChatUI.layoutFrame)return;
  ChatUI.layoutFrame=requestAnimationFrame(()=>{ChatUI.layoutFrame=0;chatPositionLayers();});
}
function chatPositionLayers() {
  const root=$('#chat-root');if(!root)return;
  const bounds=root.getBoundingClientRect(),composer=$('.chat-composer-wrap');
  if(composer)root.style.setProperty('--chat-composer-height',composer.getBoundingClientRect().height+'px');
  for(const [id,anchorId] of [['chat-popover',ChatUI.popoverAnchor||'chat-model-picker'],['chat-slash','chat-compose']]) {
    const box=$('#'+id),anchor=$('#'+anchorId);if(!box||box.hidden||!anchor)continue;
    const a=anchor.getBoundingClientRect(), margin=10;
    const width=Math.min(id==='chat-slash'?440:420,bounds.width-2*margin);
    const left=Math.max(margin,Math.min(a.left-bounds.left,bounds.width-width-margin));
    // Anchor to the actual control, not a percentage of the entire workspace.
    const above=a.top-bounds.top-margin,below=bounds.bottom-a.bottom-margin;
    box.style.width=width+'px';box.style.left=left+'px';box.style.right='auto';
    if(above>=Math.min(260,below)||above>below){
      box.style.top='auto';box.style.bottom=Math.max(margin,bounds.bottom-a.top+7)+'px';
      box.style.maxHeight=Math.max(90,above-7)+'px';
    }else{
      box.style.bottom='auto';box.style.top=Math.max(margin,a.bottom-bounds.top+7)+'px';
      box.style.maxHeight=Math.max(90,below-7)+'px';
    }
  }
}
function chatSyncChrome() {
  const root=$('#chat-root');if(!root)return;
  const c=ChatUI,v=chatView();
  chatTargetSync();
  $('#chat-history-search').value=c.query;$('#chat-history-filter').value=c.filter;
  root.classList.toggle('history-collapsed',!!c.historyCollapsed);
  $('#chat-history-toggle').setAttribute('aria-expanded',String(innerWidth>760?!c.historyCollapsed:root.classList.contains('drawer-open')));
  $('#chat-focus').setAttribute('aria-pressed',String(!!c.focus));
  $('#chat-inspector-toggle').setAttribute('aria-expanded',String(!!c.inspector));
  const send=$('#chat-send');
  const target=chatProjectInfo(c.project,c.selected||{}),localCommand=chatLocalCommand(v.draft)&&!v.pending;
  const processBlocked=({stopping:'正在等待会话进程退出',orphaned:'会话进程状态待核实，请先检查节点'})[c.selected?.status];
  const blocked=v.resumeBusy&&'正在恢复会话'||v.pending&&'上一条消息待确认，请重试原请求'||!localCommand&&(processBlocked||target.reason||v.settingOp&&!['error'].includes(v.settingOp.state)&&'请等待模型设置确认'||v.files.some(f=>!f.ready)&&'请等待附件上传完成，或重试失败的附件');
  send.disabled=!!v.busy||!!blocked||(!v.draft.trim()&&!v.files.length);
  send.title=blocked||(localCommand?'执行界面指令':v.busy?'正在确认发送':!v.draft.trim()&&!v.files.length?'输入消息或添加附件后发送':v.active&&v.mode==='steer'?'立即补充':'发送消息');
  send.setAttribute('aria-label',v.busy?'正在确认发送':v.active&&v.mode==='steer'?'立即补充':'发送消息');
  $('#chat-form').setAttribute('aria-busy',String(!!v.busy));
  $('#chat-send-mode').hidden=!v.active;
  $('#chat-title').title=c.selected?.title||'新对话';
  const activity=$('#chat-activity');
  if(activity){
    const approval=[...v.items.values()].some(item=>item.kind==='approval'&&!item.data.resolved&&!item.answered);
    activity.hidden=!v.active&&!approval;activity.dataset.state=approval?'approval':'working';
    const label=approval?'等待你的确认':v.phase==='tool'?'正在执行工具':v.phase==='reasoning'?'正在思考':v.phase==='reply'?'正在回复':'正在处理';if($('#chat-activity-text').textContent!==label)$('#chat-activity-text').textContent=label;
    $('#chat-activity-queue').hidden=!v.outbox.size;
    const queued=v.outbox.size?'队列 '+v.outbox.size:'';if($('#chat-activity-queue').textContent!==queued)$('#chat-activity-queue').textContent=queued;
  }
  const row=c.selected,live=row&&['starting','running','stopping','orphaned'].includes(row.status);
  const availability={
    'chat-rename':!row?'先打开一条会话':'',
    'chat-export':!row?'先打开一条会话':'',
    'chat-export-json':!row?'先打开一条会话':'',
    'chat-resume':v.resumeBusy?'正在恢复会话':!row?'先打开一条会话':live?'会话仍在运行，直接继续发送即可':v.busy?'正在确认发送':v.pending?'上一条消息待确认，请重试原请求':target.reason,
    'chat-stop':!row?'先打开一条会话':!live?'会话已经停止':row.status==='stopping'?'正在等待进程退出':target.reason,
    'chat-delete':!row?'先打开一条会话':live?'请先停止会话并确认进程退出':''
  };
  for(const [id,reason] of Object.entries(availability)){const button=$('#'+id);button.disabled=!!reason;button.title=reason||button.textContent;}
  [...root.querySelectorAll('.chat-file')].forEach((chip,index)=>{for(const button of chip.querySelectorAll('button'))button.disabled=chatFileLocked(v,v.files[index]);});
  chatScheduleLayout();
}
function chatTrapFocus(e,root) {
  if(e.key!=='Tab')return;
  const controls=[...root.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,[tabindex="0"]')].filter(x=>x.getClientRects().length);
  const first=controls[0],last=controls.at(-1);if(!first)return;
  if(e.shiftKey&&(document.activeElement===first||!root.contains(document.activeElement))){e.preventDefault();last.focus();}
  else if(!e.shiftKey&&(document.activeElement===last||!root.contains(document.activeElement))){e.preventDefault();first.focus();}
}
function chatInteractionKey(e) {
  if(e.defaultPrevented||e.isComposing||e.keyCode===229||e.target.closest('dialog'))return;
  const c=ChatUI,root=$('#chat-root'),box=$('#chat-popover'),key=e.key.toLowerCase();
  if((e.metaKey||e.ctrlKey)&&key==='k'){e.preventDefault();e.stopPropagation();chatPalette();return;}
  if((e.metaKey||e.ctrlKey)&&e.shiftKey&&key==='o'){e.preventDefault();e.stopPropagation();chatNewSession();return;}
  if((e.metaKey||e.ctrlKey)&&key==='b'){e.preventDefault();e.stopPropagation();chatDrawer(innerWidth>760?!!c.historyCollapsed:!root.classList.contains('drawer-open'));return;}
  if(e.key==='Escape') {
    if(!box.hidden){e.preventDefault();chatClosePopover(true);return;}
    if(!$('#chat-slash').hidden){e.preventDefault();$('#chat-slash').hidden=true;return;}
    if($('.chat-overflow')?.open){e.preventDefault();$('.chat-overflow').open=false;$('.chat-overflow summary').focus();return;}
    if($('#chat-options').classList.contains('is-open')){e.preventDefault();chatOptions(false,true);return;}
    if(root.classList.contains('drawer-open')){e.preventDefault();chatDrawer(false);return;}
    if(c.inspector){e.preventDefault();chatInspector('');return;}
    if(!$('#chat-find-bar').hidden){e.preventDefault();chatFindToggle(false);return;}
  }
  if(!box.hidden){
    if(['ArrowDown','ArrowUp','Enter','Home','End'].includes(e.key)&&$('#chat-model-search')&&(e.target.id==='chat-model-search'||e.target.closest('.chat-model-option'))){
      const rows=[...box.querySelectorAll('.chat-model-option')];if(!rows.length)return;
      const focused=rows.indexOf(document.activeElement);
      const active=focused>=0?focused:rows.findIndex(x=>x.dataset.keyboard==='true');
      if(e.key==='Enter'){e.preventDefault();(rows[active]||rows.find(x=>x.classList.contains('selected'))||rows[0]).click();return;}
      const next=e.key==='Home'?0:e.key==='End'?rows.length-1:active<0?(e.key==='ArrowDown'?0:rows.length-1):(active+(e.key==='ArrowDown'?1:-1)+rows.length)%rows.length;
      e.preventDefault();rows.forEach((x,i)=>{x.dataset.keyboard=String(i===next);x.id='chat-model-option-'+i;});
      $('#chat-model-search').setAttribute('aria-activedescendant',rows[next].id);
      rows[next].scrollIntoView({block:'nearest'});if(focused>=0)rows[next].focus({preventScroll:true});
    }
    chatTrapFocus(e,box);
  }else if($('#chat-options').classList.contains('is-open'))chatTrapFocus(e,$('#chat-options'));
  else if(root.classList.contains('drawer-open'))chatTrapFocus(e,$('.chat-sidebar'));
  else if(c.inspector&&innerWidth<=1100)chatTrapFocus(e,$('#chat-inspector'));
}
function chatDialog({title,body='',value,confirmLabel='确定',danger=false}) {
  ChatUI.dialogCancel?.();chatClosePopover();
  const root=$('#chat-root'),returnFocus=$('.chat-overflow')?.open?$('.chat-overflow summary'):document.activeElement;
  if(!root)return Promise.resolve(null);
  const dialog=document.createElement('dialog');dialog.className='chat-sheet';
  const form=document.createElement('form'),heading=document.createElement('h2'),description=document.createElement('p'),actions=document.createElement('div');
  heading.id='chat-dialog-title';heading.textContent=title;dialog.setAttribute('aria-labelledby',heading.id);
  description.id='chat-dialog-body';description.textContent=body;dialog.setAttribute('aria-describedby',description.id);
  actions.className='chat-sheet-actions';form.append(heading,description);
  let input;
  if(value!==undefined){input=document.createElement('input');input.id='chat-dialog-input';input.value=value;input.maxLength=120;input.required=true;input.setAttribute('aria-label',title);form.append(input);}
  const cancel=document.createElement('button'),confirm=document.createElement('button');
  cancel.type='button';cancel.textContent='取消';cancel.className='chat-secondary-button';
  confirm.type='submit';confirm.textContent=confirmLabel;confirm.className=danger?'chat-danger-button':'chat-primary-button';confirm.id='chat-dialog-confirm';
  actions.append(cancel,confirm);form.append(actions);dialog.append(form);root.append(dialog);
  return new Promise(resolve=>{
    let finished=false;
    const finish=result=>{if(finished)return;finished=true;if(ChatUI.dialogCancel===abort)ChatUI.dialogCancel=null;dialog.close();dialog.remove();if(returnFocus?.isConnected)returnFocus.focus({preventScroll:true});resolve(result);};
    const abort=()=>finish(null);ChatUI.dialogCancel=abort;
    cancel.onclick=abort;dialog.addEventListener('cancel',e=>{e.preventDefault();abort();});
    form.addEventListener('submit',e=>{e.preventDefault();const answer=input?input.value.trim():true;if(answer)finish(answer);});
    form.addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.isComposing||e.keyCode===229))e.preventDefault();});
    dialog.showModal();(input||(danger?cancel:confirm)).focus();input?.select();
  });
}
function chatPalette() {
  const root=$('#chat-root');if(!root)return;
  ChatUI.dialogCancel?.();chatClosePopover();
  const dialog=document.createElement('dialog');dialog.className='chat-sheet chat-palette';dialog.setAttribute('aria-label','快捷操作');
  dialog.innerHTML='<div class="chat-palette-search"><input type="search" aria-label="搜索操作与会话" placeholder="搜索操作、当前列表会话或页面…"><kbd>esc</kbd></div><div class="chat-palette-results"></div><footer>↑ ↓ 选择 <span>↵ 打开</span></footer>';
  root.append(dialog);const input=dialog.querySelector('input'),results=dialog.querySelector('.chat-palette-results'),origin=document.activeElement;
  const close=()=>{if(ChatUI.dialogCancel===close)ChatUI.dialogCancel=null;dialog.close();dialog.remove();if(origin?.isConnected)origin.focus({preventScroll:true});};ChatUI.dialogCancel=close;
  const modifier=/Mac|iPhone|iPad/.test(navigator.platform)?'⌘':'Ctrl';
  const actions=[
    {label:'新对话',hint:modifier+' ⇧ O',action:()=>chatNewSession()},
    {label:'搜索全部会话',hint:'跨项目',action:()=>{chatHistoryClear();chatDrawer(true);$('#chat-history-search').focus({preventScroll:true});}},
    {label:'选择模型',hint:'设置',action:chatModelPopover},{label:'搜索当前对话',hint:modifier+' F',action:()=>chatFindToggle(true)},
    {label:'指令与技能',hint:'会话',action:()=>chatInspector('commands')},{label:'消息队列',hint:'会话',action:()=>chatInspector('queue')},
    {label:'文件与图片',hint:'会话',action:()=>chatInspector('files')},
    ...ChatUI.rows.map(row=>({label:row.title||'新对话',hint:chatProjectInfo(row.project_id||ChatUI.project,row).alias+' · '+row.provider,action:()=>chatSwitch(row,row.project_id||ChatUI.project)})),
    ...(typeof nav!=='undefined'?nav.filter(n=>n[0]!=='native').map(([id,ico,label])=>({label,hint:'控制台',action:()=>navigate(id)})):[])
  ];let filtered=[],active=0;
  const choose=i=>{const item=filtered[i];if(!item)return;close();chatDrawer(false,true);Promise.resolve().then(item.action).catch(e=>chatStatus(e.message,true));};
  const render=()=>{results.replaceChildren();filtered=actions.filter(a=>(a.label+' '+a.hint).toLowerCase().includes(input.value.toLowerCase()));active=0;filtered.forEach((a,i)=>{const b=document.createElement('button'),label=document.createElement('span'),hint=document.createElement('small');b.type='button';label.textContent=a.label;hint.textContent=a.hint;b.append(label,hint);b.onclick=()=>choose(i);results.append(b);});if(!filtered.length)results.textContent='没有匹配结果';highlight();};
  const highlight=()=>{[...results.children].forEach((b,i)=>b.classList.toggle('active',i===active));results.children[active]?.scrollIntoView({block:'nearest'});};
  input.oninput=render;dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
  dialog.addEventListener('keydown',e=>{if(e.isComposing||e.keyCode===229)return;if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close();return;}if(['ArrowDown','ArrowUp'].includes(e.key)&&filtered.length){e.preventDefault();const focused=[...results.children].indexOf(document.activeElement);active=((focused>=0?focused:active)+(e.key==='ArrowDown'?1:-1)+filtered.length)%filtered.length;highlight();if(focused>=0)results.children[active]?.focus({preventScroll:true});}else if(e.key==='Enter'){e.preventDefault();const focused=[...results.children].indexOf(document.activeElement);choose(focused>=0?focused:active);}});
  dialog.showModal();render();input.focus();
}
function chatProjectButtons(project){return `<button class="btn small" data-native-launch="pi" data-project="${esc(project)}">Pi</button><button class="btn small" data-native-launch="codex" data-project="${esc(project)}">Codex</button>`;}
document.addEventListener('click',e=>{const b=e.target.closest('[data-native-launch]');if(b)chatOpenProject(b.dataset.project,b.dataset.nativeLaunch).catch(err=>typeof toast==='function'?toast(err.message,true):chatStatus(err.message,true));});
