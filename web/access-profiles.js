'use strict';
window.CodePierProfiles=(()=>{
  const scopeNames={read:'读取',write:'写入',execute:'执行',computer:'浏览器 / 桌面'};
  const allScopes=['read','write','execute','computer'];
  const boundary='Profile 限制连接凭据，不会将 ChatGPT 聊天或 Project 绑定为安全边界。Shell 仍受 Agent 系统账号权限控制，不是项目沙箱。';
  async function html(){
    const result=await api('/api/access-profiles');
    const cards=result.profiles.map(p=>`<div class="grant-row"><div><h3>${esc(p.label)} <span class="badge ${p.enabled?'purple':'neutral'}">${p.enabled?'已启用':'已停用'}</span></h3><p>${esc(p.scopes.map(s=>scopeNames[s]).join(' / '))} · ${esc(p.all_projects?'全部现有及未来项目':p.projects.map(id=>S.projects.find(x=>x.id===id)?.alias||'已移除项目').join(', '))}</p><small class="mono">${esc(p.id)}</small></div><button class="btn small" data-profile-edit="${esc(p.id)}">编辑 / 停用</button></div>`).join('');
    return `<div id="profiles-page">${heading('访问 Profiles','ACCESS PROFILES / CONNECTION IDENTITY','',`<button class="btn primary" id="profile-create">${icon('key')}新建 Profile</button>`)}${notice(boundary)}<section class="panel"><div class="panel-head"><h2>可重复连接的权限身份</h2><span class="badge neutral">${result.profiles.length} / ${result.limit}</span></div><div class="panel-body"><p class="form-note">先建立例如「NewAPI Dev」「Codex Review」，再在 OAuth 或创建 PAT 时选择。名称可改，身份 ID 不变。修改权限上限不会扩大旧 grant 的同意范围；扩权后需重新授权。停用会拒绝后续调用和尚未派送的操作，已开始的任务不会自动停止。</p>${cards||empty('还没有 Profile；现有连接继续按原 grant 授权。')}</div></section><div class="actions"><button class="btn ghost" data-nav="connect">查看 MCP 连接</button></div></div>`;
  }
  function bind(){
    $('#profile-create').onclick=()=>edit();
    $$('[data-profile-edit]').forEach(button=>{button.onclick=()=>edit(button.dataset.profileEdit);});
  }
  async function edit(id=null){
    const login=S.session,page=S.page,intent=S.modalIntent||0;
    try{
      const saved=id?await api('/api/access-profiles/'+encodeURIComponent(id)):null;
      if(S.session!==login||S.page!==page||(S.modalIntent||0)!==intent)return;
      const key=crypto.randomUUID();
      const dialog=modal(saved?'编辑访问 Profile':'新建访问 Profile',`<form id="profile-form"><div class="field"><label for="profile-label">名称</label><input id="profile-label" name="label" required maxlength="80" value="${esc(saved?.label||'')}" placeholder="例如 NewAPI Dev / Codex Review"></div><div class="field"><label class="check"><input name="enabled" type="checkbox" ${!saved||saved.enabled?'checked':''}>启用此身份</label></div>${permissionsHTML(S.projects,allScopes,{})}<p class="form-note">${esc(boundary)}</p><p class="form-note">只调整这个 Profile 的上限，不替旧连接追加同意。停用不等于取消正在运行的命令；已撤销 grant 不会因重新启用 Profile 而恢复。</p><p id="profile-save-status" class="form-note" role="status"></p></form>`,`<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="profile-form">保存 Profile</button>`);
      const form=$('#profile-form',dialog),status=$('#profile-save-status',dialog);
      if(saved){
        $$('[name="scope"]',dialog).forEach(x=>{x.checked=saved.scopes.includes(x.value);});
        $('[name="all_projects"]',dialog).checked=saved.all_projects;
        $$('[name="project"]',dialog).forEach(x=>{x.checked=saved.projects.includes(x.value);});
      }
      CodePierAccess.bindProjects(dialog);
      form.onsubmit=event=>{
        event.preventDefault();
        busy($('button[type="submit"]',dialog),async()=>{
          if(!form.reportValidity())return;
          const body={label:form.elements.label.value,enabled:form.elements.enabled.checked,
            scopes:$$('[name="scope"]:checked',dialog).map(x=>x.value),...CodePierAccess.selection(dialog),
            ...(saved?{expected_version:saved.version}:{idempotency_key:key})};
          const controls=$$('input',form),disabled=controls.map(x=>x.disabled);
          controls.forEach(x=>{x.disabled=true;});status.textContent='正在保存…';
          try{
            await api('/api/access-profiles'+(id?'/'+encodeURIComponent(id):''),{method:id?'PUT':'POST',body:JSON.stringify(body)});
            if(S.session!==login||!dialog.isConnected)return;
            closeModal(dialog);toast('Profile 已保存；旧 grant 不会自动扩权');
            if(S.page===page)await renderPage(false);
          }catch(error){
            if(S.session===login&&dialog.isConnected)status.textContent=error.code==='NETWORK_UNCERTAIN'?'保存结果未确认。请先检查 Profile 列表；仅可使用原参数重试，不要重新创建。':error.message;
          }finally{
            if(S.session===login&&dialog.isConnected)controls.forEach((x,i)=>{x.disabled=disabled[i];});
          }
        });
      };
    }catch(error){if(S.session===login)toast(error.message,true);}
  }
  function selectorHTML(profiles){
    return `<div class="field"><label for="access-profile-selector">以哪个 Profile 连接</label><select id="access-profile-selector" name="access_profile"><option value="">传统逐次授权（不使用 Profile）</option>${profiles.map(p=>`<option value="${esc(p.id)}" ${p.enabled?'':'disabled'}>${esc(p.label)}${p.enabled?'':' · 已停用'}</option>`).join('')}</select><small>不同用途请连接不同 Profile。重新连接或增加权限时选择原 Profile，身份 ID 才会保持一致。</small><a href="/#profiles" target="_blank" rel="noopener noreferrer">在新标签页管理 Profiles</a></div>`;
  }
  function bindSelector(dialog,profiles,requested=allScopes,defaults={}){
    const select=$('#access-profile-selector',dialog),fields=$('[data-profile-permissions]',dialog);
    const update=()=>{
      const profile=profiles.find(p=>p.id===select.value);
      if(!profile){fields.innerHTML=permissionsHTML(S.projects,requested,defaults);}
      else{
        const projects=S.projects.filter(p=>profile.all_projects||profile.projects.includes(p.id));
        const scopes=requested.filter(s=>profile.scopes.includes(s));
        fields.innerHTML=permissionsHTML(projects,scopes,{developer_scopes:true,all_projects:profile.all_projects});
        if(!profile.all_projects){
          $('[name="all_projects"]',fields).closest('label').hidden=true;
          $('[name="all_projects"]',fields).disabled=true;
          $$('[name="project"]',fields).forEach(x=>{x.checked=true;});
        }
        // Computer is deliberately NOT prechecked, even for a desktop profile.
        fields.insertAdjacentHTML('beforeend',`<p class="form-note">Profile 上限：${esc(profile.label)} · 版本 ${profile.version}。下面可进一步缩小本次连接权限；桌面控制需要另行勾选。</p>`);
      }
      CodePierAccess.bindProjects(dialog);
    };
    select.addEventListener('change',update);update();
    return ()=>{
      if(!select.value)return {};
      const profile=profiles.find(p=>p.id===select.value);
      if(!profile)throw new Error('Profile 已变化，请重新打开授权页面');
      return {profile_id:profile.id,profile_version:profile.version};
    };
  }
  return {html,bind,edit,selectorHTML,bindSelector};
})();
