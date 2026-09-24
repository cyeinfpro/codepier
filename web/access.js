'use strict';
window.CodePierAccess=(()=>{
  function projectFields(projects,all=false,selected=[]){
    return `<div class="field"><label>允许访问的项目</label><label class="check"><input type="checkbox" name="all_projects" ${all?'checked':''}>全部现有及未来新增项目</label><small>开启后，新建项目会自动进入此连接的授权范围，无需重新连接；仍受项目、本机目录及工具权限限制。</small><div class="check-list" data-project-choices>${projects.map(p=>`<label class="check"><input type="checkbox" name="project" value="${esc(p.id)}" ${selected.includes(p.id)?'checked':''} ${all?'disabled':''}><span>${esc(p.alias)} <small> · ${esc(p.device_name||'')}</small></span></label>`).join('')||'<small>尚无项目。可明确选择全部现有及未来项目，稍后建立映射。</small>'}</div></div>`;
  }
  function bindProjects(dialog){
    const all=$('[name="all_projects"]',dialog);
    if(!all)return;
    const sync=()=>$$('[name="project"]',dialog).forEach(field=>{field.disabled=all.checked;});
    all.addEventListener('change',sync);sync();
  }
  function selection(dialog=document.querySelector('.modal')){
    const all=$('[name="all_projects"]',dialog)?.checked===true;
    return {all_projects:all,projects:all?[]:$$('[name="project"]:checked',dialog).map(field=>field.value)};
  }
  function html(){
    const defaults=S.settings?.access_defaults||{};
    return `<section class="panel" id="access-settings" aria-labelledby="access-settings-title"><div class="panel-head"><h2 id="access-settings-title">${icon('shield')}MCP 默认授权</h2></div><div class="panel-body"><form id="access-settings-form"><div class="check-list"><label class="check"><input name="all_projects" type="checkbox" ${defaults.all_projects?'checked':''}>默认选择全部现有及未来新增项目</label><label class="check"><input name="developer_scopes" type="checkbox" ${defaults.developer_scopes?'checked':''}>默认勾选读取、写入和执行权限</label><label class="check"><input name="apply_to_existing" type="checkbox" ${defaults.all_projects?'':'disabled'}>同时将全部项目范围应用到已有有效 OAuth 连接</label></div><p class="form-note">默认值只预选新授权页面，应用接入仍需你确认，且不会超出客户端申请的权限；桌面控制不默认勾选。批量应用仅扩展当前账号已有传统 OAuth 连接（不含 Profile 连接）的项目范围，不增加工具权限，不更换凭据，不延长有效期，也不影响 PAT。</p><p class="form-note">关闭默认选项不会自动撤销已有授权。可在“MCP 接入 → 调整项目范围”逐项收回未来项目或只保留指定项目。</p><div class="actions"><button class="btn primary" type="submit">保存授权设置</button><button class="btn ghost" type="button" data-nav="connect">管理已有连接</button></div><p id="access-settings-status" class="form-note" role="status"></p></form></div></section>`;
  }
  function bind(){
    const form=$('#access-settings-form');if(!form)return;
    const login=S.session,all=form.elements.all_projects,apply=form.elements.apply_to_existing,status=$('#access-settings-status',form);
    const sync=()=>{apply.disabled=!all.checked;if(!all.checked)apply.checked=false;};
    all.addEventListener('change',sync);
    form.onsubmit=event=>{
      event.preventDefault();
      busy($('button[type="submit"]',form),async()=>{
        const body={all_projects:all.checked,developer_scopes:form.elements.developer_scopes.checked,apply_to_existing:apply.checked};
        if(body.apply_to_existing&&!confirm('将当前账号所有有效传统 OAuth 连接（不含 Profile）改为可访问全部现有及未来新增项目？\n只扩展项目范围，现有工具权限和凭据保持不变。'))return;
        const controls=$$('input',form);controls.forEach(field=>{field.disabled=true;});status.textContent='正在保存授权设置…';
        try{
          const result=await api('/api/settings/access',{method:'PUT',body:JSON.stringify(body)});
          if(S.session!==login||!form.isConnected)return;
          S.settings.access_defaults=result.access_defaults;
          for(const name of ['all_projects','developer_scopes'])form.elements[name].defaultChecked=body[name];
          apply.checked=false;apply.defaultChecked=false;
          status.textContent=result.note+(body.apply_to_existing?` 已更新 ${result.updated_grants} 个现有 OAuth 连接；无需重新连接。`:'');
          toast('授权设置已保存');
        }catch(error){
          if(S.session===login&&form.isConnected)status.textContent=error.code==='NETWORK_UNCERTAIN'?'保存回执暂未收到。请重新读取设置和已有连接核实，不要假定保存失败。':error.message;
        }finally{if(S.session===login&&form.isConnected){controls.forEach(field=>{field.disabled=false;});sync();}}
      });
    };
  }
  async function editGrant(id){
    const login=S.session,page=S.page,intent=S.modalIntent||0;
    const [grant]=await Promise.all([api('/api/grants/'+encodeURIComponent(id)+'/projects'),loadBasics()]);
    if(S.session!==login||S.page!==page||(S.modalIntent||0)!==intent)return;
    if(!grant||grant.revoked)throw new Error('授权不存在或已撤销，请刷新列表');
    const dialog=modal('调整项目范围 · '+grant.label,`<form id="grant-projects-form">${projectFields(S.projects,grant.projects.includes('*'),grant.projects)}<p class="form-note">工具权限保持 ${esc(grant.scopes.join(' / '))}，不会更换凭据或延长有效期。缩小范围后，后续调用和尚未投递的操作会重新核对权限；已开始的任务不会自动停止。</p><p id="grant-projects-status" class="form-note" role="status"></p></form>`,`<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="grant-projects-form">保存项目范围</button>`);
    bindProjects(dialog);
    const form=$('#grant-projects-form',dialog),status=$('#grant-projects-status',dialog);
    form.onsubmit=event=>{
      event.preventDefault();
      busy($('button[type="submit"]',dialog),async()=>{
        const body={...selection(dialog),expected_projects:grant.projects,expected_revision:grant.project_revision};
        const controls=$$('input',form);controls.forEach(field=>{field.disabled=true;});status.textContent='正在保存…';
        try{
          const response=await api('/api/grants/'+encodeURIComponent(id)+'/projects',{method:'PUT',body:JSON.stringify(body)});
          if(S.session!==login||!dialog.isConnected)return;
          closeModal(dialog);toast(response.note);if(S.page===page)await renderPage(false);
        }catch(error){if(S.session===login&&dialog.isConnected)status.textContent=error.message;}
        finally{if(S.session===login&&dialog.isConnected){controls.forEach(field=>{field.disabled=false;});$$('[name="project"]',dialog).forEach(field=>{field.disabled=$('[name="all_projects"]',dialog).checked;});}}
      });
    };
  }
  return {html,bind,projectFields,bindProjects,selection,editGrant};
})();
