'use strict';
// Saved connection management. Passwords stay in the active form only; neither
// metadata state nor browser storage ever receives a saved credential.
async function vpsInventory(){
  const items=[];let offset=0;
  do{const r=await api('/api/vps?limit=200&offset='+offset);items.push(...r.vps);offset=r.next_offset;}while(offset!==null);
  return items;
}
function vpsIntent(){
  const session=S.session,page=S.page,id=S.vpsIntent=(S.vpsIntent||0)+1;
  return ()=>session===S.session&&page===S.page&&id===S.vpsIntent;
}
function vpsEndpoint(v){return `${v.host.includes(':')?'['+v.host+']':v.host}:${v.port}`;}
async function vpsHTML(seq){
  const [items]=await Promise.all([vpsInventory(),loadBasics()]);
  if(seq!==S.renderSeq)return '';
  S.vps=items;
  return heading('VPS 管理','服务器连接','保存连接，按项目分配。',`<button class="btn primary" data-vps-action="add">${icon('plus')}添加 VPS</button>`)+
    `<section class="vps-summary" aria-label="服务器概况"><div><strong>${items.length}</strong><span>全部连接</span></div><div><strong>${items.filter(v=>v.enabled).length}</strong><span>已启用</span></div><div><strong>${items.filter(v=>v.project_ids.length).length}</strong><span>已分配项目</span></div><p>${icon('lock')}密码保存在服务端，调用时无需再次提供。</p></section>
    <div class="vps-filters"><label class="vps-search">${icon('search')}<input id="vps-query" type="search" placeholder="搜索名称、IP、服务商、地区" aria-label="搜索 VPS" value="${esc(S.vpsQuery||'')}"></label><select id="vps-project-filter" aria-label="按项目筛选"><option value="">全部项目</option><option value="__unassigned__" ${S.vpsProject==='__unassigned__'?'selected':''}>未分配</option>${S.projects.map(p=>`<option value="${esc(p.id)}" ${S.vpsProject===p.id?'selected':''}>${esc(p.alias)}</option>`).join('')}</select><span class="muted tiny" id="vps-count" role="status"></span></div>
    <section class="vps-grid">${items.map(v=>`<article class="panel vps-card" data-vps-card="${esc(v.id)}"><header><span class="vps-symbol">${icon('cloud')}</span><div><h2>${esc(v.name)}</h2><p>${esc([v.provider,v.region].filter(Boolean).join(' · ')||'未填写服务商 / 地区')}</p></div><span class="badge ${v.enabled?'neutral':'offline'}">${v.enabled?'已启用':'已停用'}</span></header><div class="vps-address"><code>${esc(vpsEndpoint(v))}</code><button class="icon-btn" data-vps-action="copy" data-id="${esc(v.id)}" aria-label="复制 ${esc(v.name)} 的地址">${icon('copy')}</button></div><dl class="vps-facts"><div><dt>SSH 账号</dt><dd>${esc(v.username)}</dd></div><div><dt>系统</dt><dd>${esc(v.system||'未填写')}</dd></div><div><dt>认证</dt><dd>已保存密码</dd></div><div><dt>主机密钥</dt><dd>${v.host_key_policy==='strict'?'严格验证':'信任首次连接'}</dd></div></dl><div class="vps-projects" aria-label="已分配项目">${v.projects.map(p=>`<button class="badge neutral" data-vps-action="project" data-project="${esc(p.id)}" title="管理 ${esc(p.alias)} 的 VPS 分配">${icon('folder')}${esc(p.alias)}</button>`).join('')||'<span class="muted tiny">尚未分配项目</span>'}</div>${v.notes?`<p class="vps-notes">${esc(v.notes)}</p>`:''}<footer><button class="btn ghost small" data-vps-action="assign" data-id="${esc(v.id)}">分配项目</button><button class="btn ghost small" data-vps-action="edit" data-id="${esc(v.id)}" aria-label="编辑 ${esc(v.name)}">${icon('edit')}编辑</button><button class="btn small" data-vps-action="check" data-id="${esc(v.id)}" ${!v.enabled||!v.project_ids.length?'disabled':''}>${icon('terminal')}检查连接</button></footer></article>`).join('')}</section>
    <div id="vps-empty" class="empty panel" hidden><p>${items.length?'没有匹配的 VPS':'还没有保存服务器连接'}</p>${items.length?'':'<button class="btn primary" data-vps-action="add">添加第一台 VPS</button>'}</div>
    ${uiHelp('如何在 ChatGPT 中调用','<p>保存连接后，将它分配给一个或多个项目。之后可以说“SSH 到某个 VPS，检查磁盘”，通过 <code>vps_list</code> 查询、<code>vps_exec</code> 执行。一个项目可分配多台 VPS；同一 IP 有多个端口或账号时需要明确选择。</p><p>“已启用”是配置状态，不代表服务器在线。执行由所选项目的 Agent 发起，仍需项目执行权限及本机 Shell 授权；检查连接只读取主机与根目录磁盘信息。</p>')}`;
}
function vpsFilter(){
  const q=($('#vps-query')?.value||'').trim().toLocaleLowerCase(),project=$('#vps-project-filter')?.value||'';
  S.vpsQuery=$('#vps-query')?.value||'';S.vpsProject=project;
  let count=0;
  $$('[data-vps-card]').forEach(card=>{const v=S.vps.find(v=>v.id===card.dataset.vpsCard);const match=(!q||[v.name,v.host,v.provider,v.region].join(' ').toLocaleLowerCase().includes(q))&&(!project||(project==='__unassigned__'?!v.project_ids.length:v.project_ids.includes(project)));card.hidden=!match;if(match)count++;});
  if($('#vps-count'))$('#vps-count').textContent=`${count} / ${(S.vps||[]).length} 个连接`;
  if($('#vps-empty'))$('#vps-empty').hidden=count>0;
}
function bindVps(){ $('#vps-query').oninput=vpsFilter;$('#vps-project-filter').onchange=vpsFilter;vpsFilter(); }
function vpsProjectChoices(selected=[]){return `<div class="check-list vps-check-list">${S.projects.map(p=>`<label class="check"><input type="checkbox" name="project_ids" value="${esc(p.id)}" ${selected.includes(p.id)?'checked':''}><span>${esc(p.alias)}<small>${esc(p.device_name)} · ${p.online?'Agent 在线':'Agent 离线'}${p.mode!=='write'||!p.allow_tasks?' · 未授权执行':''}</small></span></label>`).join('')||'<p class="form-note">尚无项目映射，可以先保存 VPS，稍后分配。</p>'}</div>`;}
async function vpsEdit(id=null){
  const current=vpsIntent();
  const [v]=await Promise.all([id?api('/api/vps/'+id):Promise.resolve({}),loadBasics()]);
  if(!current())return;
  const dialog=modal(id?'编辑 VPS · '+v.name:'添加 VPS',`<form id="vps-form" autocomplete="off"><div class="field"><label>VPS 名称</label><input name="name" required maxlength="80" value="${esc(v.name||'')}" placeholder="例如：香港面板、广州 6M"></div><div class="form-row"><div class="field"><label>IP / 主机名</label><input name="host" required maxlength="253" value="${esc(v.host||'')}" placeholder="服务器 IP 或域名" autocapitalize="off" spellcheck="false"></div><div class="field vps-port"><label>SSH 端口</label><input name="port" type="number" required min="1" max="65535" value="${v.port||22}"></div></div><div class="form-row"><div class="field"><label>SSH 账号</label><input name="username" required maxlength="128" value="${esc(v.username||'root')}" autocapitalize="off" spellcheck="false"></div><div class="field"><label>SSH 密码</label><input name="password" type="password" ${id?'':'required'} maxlength="4096" autocomplete="new-password" placeholder="${id?'已保存，留空保持原密码':'仅用于连接服务器'}"><small>${id?'不会回显原密码；输入新密码才会替换。':'加密保存，不写入浏览器持久存储。'}</small></div></div><div class="field"><label>主机密钥验证</label><select name="host_key_policy"><option value="strict" ${v.host_key_policy!=='accept-new'?'selected':''}>严格验证已有主机密钥</option><option value="accept-new" ${v.host_key_policy==='accept-new'?'selected':''}>信任首次连接，拒绝后续密钥变化</option></select><small>首次连接且 Agent 尚无主机记录时，需要明确选择“信任首次连接”。</small></div><details class="vps-extra" ${v.provider||v.region||v.system||v.notes?'open':''}><summary>服务商、地区与备注</summary><div class="form-row"><div class="field"><label>服务商</label><input name="provider" maxlength="120" value="${esc(v.provider||'')}" placeholder="可选"></div><div class="field"><label>地区</label><input name="region" maxlength="120" value="${esc(v.region||'')}" placeholder="可选"></div></div><div class="field"><label>系统 / 配置</label><input name="system" maxlength="160" value="${esc(v.system||'')}" placeholder="例如 Debian 13 · 2 核 / 4 GB"></div><div class="field"><label>备注</label><textarea name="notes" rows="2" maxlength="2000" placeholder="用途或部署目录，不要填写密码">${esc(v.notes||'')}</textarea></div></details><div class="field"><label>分配给项目（可多选）</label>${vpsProjectChoices(v.project_ids)}</div><label class="check"><input type="checkbox" name="enabled" ${v.enabled!==false?'checked':''}>启用此连接</label><p id="vps-form-error" class="vps-error" role="alert" hidden></p></form>`,`${id?'<button class="btn danger" id="vps-delete">删除连接</button>':''}${buttons('vps-save','保存 VPS')}`);
  const form=$('#vps-form',dialog),save=$('#vps-save',dialog);
  const submit=()=>{if(!form.reportValidity())return;return busy(save,async()=>{const error=$('#vps-form-error',dialog);error.hidden=true;try{const data=Object.fromEntries(new FormData(form));data.port=Number(data.port);data.enabled=form.elements.enabled.checked;data.project_ids=$$('input[name="project_ids"]:checked',form).map(x=>x.value);if(id)data.expected_version=v.version;if(!data.password)delete data.password;await api('/api/vps'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify(data)});form.reset();closeModal(dialog);toast('VPS 已保存');if(S.page==='vps'||S.page==='projects')await renderPage(false);}catch(e){if(dialog.isConnected){error.textContent=e.code==='NETWORK_UNCERTAIN'?'保存结果待核实。请刷新 VPS 列表确认，不要反复新建；相同连接不会重复保存。':e.message;error.hidden=false;}}});};
  save.onclick=submit;form.onsubmit=e=>{e.preventDefault();submit();};
  if(id)$('#vps-delete',dialog).onclick=()=>{if(!confirm(`删除“${v.name}”的连接及全部项目分配？不会删除服务器；已下发的命令不会自动停止。`))return;busy($('#vps-delete',dialog),async()=>{await api('/api/vps/'+id+'?expected_version='+v.version,{method:'DELETE'});form.reset();closeModal(dialog);toast('已删除连接');await renderPage(false);});};
}
async function vpsAssign(id){
  const current=vpsIntent();
  const [v]=await Promise.all([api('/api/vps/'+id),loadBasics()]);if(!current())return;
  const dialog=modal('分配项目 · '+v.name,`<p class="form-note">${esc(vpsEndpoint(v))} · ${esc(v.username)}。同一台 VPS 可以供多个项目使用。</p><div class="field"><label>允许使用此 VPS 的项目</label>${vpsProjectChoices(v.project_ids)}</div><p class="form-note">取消分配会阻止尚未下发的命令；已下发或被 Agent 接收的任务不会自动终止，请查询原操作回执。</p>`,buttons('vps-assign-save','保存分配'));
  $('#vps-assign-save',dialog).onclick=()=>busy($('#vps-assign-save',dialog),async()=>{await api('/api/vps/'+id+'/projects',{method:'PUT',body:JSON.stringify({project_ids:$$('input[name="project_ids"]:checked',dialog).map(x=>x.value),expected_version:v.version})});closeModal(dialog);toast('项目分配已更新');await renderPage(false);});
}
async function vpsAssignProject(projectId){
  const current=vpsIntent();const [items]=await Promise.all([vpsInventory(),loadBasics()]);if(!current())return;
  const project=S.projects.find(p=>p.id===projectId);if(!project)throw new Error('项目已变化，请刷新');
  const selected=items.filter(v=>v.project_ids.includes(projectId)).map(v=>v.id);
  const dialog=modal('项目 VPS · '+project.alias,`<p class="form-note">选择这个项目可使用的服务器。连接由 ${esc(project.device_name)} 发起；不会改变其他项目的分配。</p><div class="field"><label>分配 VPS（可多选）</label><div class="check-list vps-check-list">${items.map(v=>`<label class="check"><input type="checkbox" name="vps_ids" value="${esc(v.id)}" ${selected.includes(v.id)?'checked':''}><span>${esc(v.name)}${v.enabled?'':' · 已停用'}<small>${esc(vpsEndpoint(v))} · ${esc(v.username)}</small></span></label>`).join('')||'<p>还没有 VPS，请先到 VPS 管理添加连接。</p>'}</div></div>`,buttons('project-vps-save','保存分配'));
  $('#project-vps-save',dialog).onclick=()=>busy($('#project-vps-save',dialog),async()=>{await api('/api/projects/'+projectId+'/vps',{method:'PUT',body:JSON.stringify({vps_ids:$$('input[name="vps_ids"]:checked',dialog).map(x=>x.value),expected_vps_ids:selected})});closeModal(dialog);toast('VPS 分配已更新');await renderPage(false);});
}
async function vpsCheck(id){
  const session=S.session,current=vpsIntent(),v=await api('/api/vps/'+id);if(!current())return;
  const projects=v.projects.filter(p=>p.mode==='write'&&p.allow_tasks);
  const dialog=modal('检查连接 · '+v.name,`<p class="form-note">${esc(vpsEndpoint(v))} · ${esc(v.username)}。只读取主机、当前账号、系统和根目录磁盘用量，不修改服务器。</p><div class="field"><label>通过哪个项目连接</label><select id="vps-check-project">${projects.map(p=>`<option value="${esc(p.id)}">${esc(p.alias)} · ${esc(p.device_name)} · ${p.online?'在线':'离线（命令将排队）'}</option>`).join('')}</select></div>${projects.length?'':notice('没有已分配且允许执行的项目，请先分配项目并开启执行权限。')}<p id="vps-check-state" class="form-note" role="status">尚未执行</p><div class="code-box"><pre id="vps-check-output">等待连接检查</pre></div>`,`<button class="btn ghost" data-action="close-modal">关闭</button><button class="btn primary" id="vps-check-run" ${!projects.length||!v.enabled?'disabled':''}>开始检查</button>`);
  const button=$('#vps-check-run',dialog),status=$('#vps-check-state',dialog),output=$('#vps-check-output',dialog),select=$('#vps-check-project',dialog);
  let args=null,operation=null,finished=false;
  button.onclick=async()=>{if(finished){args=null;operation=null;finished=false;}await busy(button,async()=>{
    if(!args)args={project:select.value,vps:id,command:"printf 'CODEPIER_VPS_OK\\n'; hostname; id -un; uname -sr; df -h /",timeout_seconds:30,idempotency_key:'vps-check-'+uid()};
    select.disabled=true;
    try{
      if(!operation){const result=await tool('vps_exec',args);operation=result.operation_id;}
      if(!dialog.isConnected)return;
      status.textContent='检查已提交 · '+operation+'。关闭窗口后仍可在操作审计中查看。';
      while(dialog.isConnected&&session===S.session){
        const op=await tool('operations_wait',{operation_id:operation,wait_seconds:8,output_limit:12000});
        if(!dialog.isConnected||session!==S.session)return;
        output.textContent=op.output||op.result?.data?.output||'等待 Agent 返回…';
        status.textContent=(stateNames[op.state]||op.state)+' · '+operation;
        if(!op.pending){finished=true;const error=op.result?.error;status.textContent=(op.state==='succeeded'?'连接检查成功':(error?.message||op.error||'检查未完成'))+' · '+operation;return;}
      }
    }catch(e){if(e.operation_id)operation=e.operation_id;if(dialog.isConnected&&session===S.session){status.textContent=e.message+(operation?' · 原操作 '+operation:'；点击恢复将沿用同一请求，不创建重复命令。');}}
  });if(dialog.isConnected){button.textContent=finished?'重新检查':'恢复本次检查';button.disabled=false;select.disabled=!finished;}};
}
document.addEventListener('click',async e=>{
  const button=e.target.closest('[data-vps-action]');if(!button||button.disabled)return;
  try{switch(button.dataset.vpsAction){case'add':await vpsEdit();break;case'edit':await vpsEdit(button.dataset.id);break;case'assign':await vpsAssign(button.dataset.id);break;case'project':await vpsAssignProject(button.dataset.project);break;case'check':await vpsCheck(button.dataset.id);break;case'copy':{const v=S.vps?.find(v=>v.id===button.dataset.id);if(v)await copy(vpsEndpoint(v));break;}}}catch(error){toast(error.message,true);}
});
