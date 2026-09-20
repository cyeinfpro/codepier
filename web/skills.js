'use strict';
// Local skills are source material, not an automatic script runner.
async function localSkills(project=S.work.project){
  if(!project){toast('请先选择项目',true);return;}
  const session=S.session,dialog=modal('本地技能',`<form id="local-skills-form" class="skill-filters"><input name="query" aria-label="搜索技能" maxlength="200" placeholder="搜索名称或用途"><select name="source" aria-label="技能来源"><option value="all">全部来源</option><option value="codex">Codex 用户技能</option><option value="project">当前项目</option><option value="configured">额外授权目录</option></select><button class="btn primary">搜索</button></form><div id="local-skills-results"><p class="muted">读取技能目录…</p></div>`,'',true);
  let generation=0,query='',source='all',catalog='',history=[];
  const form=$('#local-skills-form',dialog),area=$('#local-skills-results',dialog);
  async function load(offset=0){
    const seq=++generation;
    try{
      const r=await settled(tool('skills_list',{project,query,source,include_disabled:true,offset,limit:20,...(offset&&catalog?{catalog_sha256:catalog}:{})}));
      if(seq!==generation||session!==S.session||!dialog.isConnected)return;
      catalog=r.catalog_sha256;
      area.innerHTML=`<p class="muted tiny">${r.total} 项匹配 · ${r.codex_enabled_for_project?'Codex 用户技能已授权':'Codex 用户技能未对该项目开启'}${r.truncated?' · 扫描达到预算，请缩小来源':''}</p><section class="local-skill-list">${r.skills.map(s=>`<article class="local-skill-card"><div class="card-top"><strong>${esc(s.display_name)}</strong><span class="badge ${s.enabled?'completed':'blocked'}">${s.enabled?(s.allow_implicit_invocation?'可按需读取':'需明确选择'):'已禁用'}</span></div><p class="muted tiny">${esc(s.source)} · <code>${esc(s.name)}</code>${s.name_conflict?' · 存在同名技能':''}</p><p>${esc(s.description)}</p><details><summary>来源路径</summary><code>${esc(s.path)}</code></details><button class="btn small" data-skill-open="${esc(s.skill_id)}" ${s.enabled?'':'disabled'}>读取技能</button></article>`).join('')||empty('未找到技能。用户级目录需在本机授权。')}</section><div class="pagination"><span>每页 20 项</span><div class="actions"><button class="btn small" id="skills-prev" ${history.length?'':'disabled'}>上一页</button><button class="btn small" id="skills-next" ${r.has_more?'':'disabled'}>下一页</button></div></div>${r.warnings.length?`<details class="skill-warnings"><summary>${r.warnings.length} 项读取提示</summary><pre>${esc(json(r.warnings))}</pre></details>`:''}<p class="form-note">仅读取技能，不启动 Codex、不安装依赖、不执行脚本。</p>`;
      $$('[data-skill-open]',area).forEach(b=>b.onclick=()=>localSkillRead(project,b.dataset.skillOpen).catch(e=>toast(e.message,true)));
      $('#skills-next',area).onclick=()=>{history.push(offset);load(r.next_offset);};
      $('#skills-prev',area).onclick=()=>load(history.pop()||0);
    }catch(error){if(seq===generation&&session===S.session&&dialog.isConnected)area.innerHTML=notice(esc(error.message));}
  }
  form.onsubmit=e=>{e.preventDefault();query=form.elements.query.value;source=form.elements.source.value;history=[];catalog='';load();};
  await load();
}
async function localSkillRead(project,id,resource='SKILL.md',offset=0,sha=''){
  const session=S.session,loading=modal('读取技能','<p class="muted">读取已选择的本机技能…</p>');
  try{
    const r=await settled(tool('skills_read',{project,skill_id:id,resource_path:resource,explicit:true,offset,max_chars:16000,expected_sha256:sha}));
    if(session!==S.session||!loading.isConnected)return;
    const dialog=modal('技能 · '+r.name,`<p class="muted tiny">${esc(r.source)} · ${esc(r.resource_path)} · 第 ${r.start_line}–${r.end_line} 行${r.truncated?' · 本页未展示完整内容':''}</p><div class="skill-path"><span>本机目录</span><code>${esc(r.skill_dir)}</code></div><div class="skill-document"><pre>${esc(r.content)}</pre></div><details class="skill-resources"><summary>脚本、参考资料与资源（${r.resources.length}${r.resources_truncated?'，列表已截断':''}）</summary>${r.resources.map(f=>`<button class="btn ghost small" data-skill-resource="${esc(f.path)}">${esc(f.path)} <span class="muted tiny">${f.bytes} B</span></button>`).join('')}</details>${r.dependencies.length?`<details class="skill-warnings"><summary>声明的工具依赖（不会自动连接）</summary><pre>${esc(json(r.dependencies))}</pre></details>`:''}<p class="form-note">这里只展示源码。脚本执行仍需 Shell 权限与明确授权。</p>`,`<button class="btn ghost" id="back-to-skills">返回技能列表</button>${r.next_offset!==null?'<button class="btn primary" id="skill-next-page">继续读取</button>':''}`,true);
    $('#back-to-skills',dialog).onclick=()=>localSkills(project);
    if(r.next_offset!==null)$('#skill-next-page',dialog).onclick=()=>localSkillRead(project,id,resource,r.next_offset,r.sha256).catch(e=>toast(e.message,true));
    $$('[data-skill-resource]',dialog).forEach(b=>b.onclick=()=>localSkillRead(project,id,b.dataset.skillResource).catch(e=>toast(e.message,true)));
  }catch(error){if(session===S.session&&loading.isConnected)$('.modal-body',loading).innerHTML=notice(esc(error.message));throw error;}
}
document.addEventListener('click',e=>{
  if(e.target.closest('[data-local-skills]')&&S.session)localSkills().catch(error=>toast(error.message,true));
});
