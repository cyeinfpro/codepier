'use strict';
// Human identity/Space management. No ID/access/refresh Token is persisted here.
window.CodePierIdentity=(()=>{
  let me=null,providers=[],members=[],roles=[],assignments=[],invites=[],users=[];
  const levels={owner:'主理人',admin:'管理员',member:'成员',guest:'访客'};
  const check=(name,label,value=false)=>`<label class="check"><input type="checkbox" name="${name}" ${value?'checked':''}>${esc(label)}</label>`;
  const field=(name,label,value='',extra='')=>`<div class="field"><label for="iam-${name}">${esc(label)}</label><input id="iam-${name}" name="${name}" value="${esc(value)}" ${extra}></div>`;
  const select=(name,label,options,value)=>`<div class="field"><label for="iam-${name}">${esc(label)}</label><select id="iam-${name}" name="${name}">${options.map(([id,text])=>`<option value="${esc(id)}" ${id===value?'selected':''}>${esc(text)}</option>`).join('')}</select></div>`;
  function current(){return me?.spaces.find(x=>x.id===S.space_id);}
  function admin(){return ['owner','admin'].includes(current()?.level);}
  function reset(){me=null;providers=[];members=[];roles=[];assignments=[];invites=[];users=[];}
  async function loginButtons(){
    const host=$('#oidc-login-buttons');if(!host)return;
    try{const out=await api('/api/auth/providers');if(!host.isConnected)return;
      host.replaceChildren();
      for(const provider of out.providers){
        const button=document.createElement('a');button.className='btn primary';button.textContent='使用 '+provider.label+' 登录';
        const returnTo='/'+location.search+location.hash;
        button.href='/auth/oidc/'+encodeURIComponent(provider.id)+'/start?'+new URLSearchParams({return_to:returnTo});host.append(button);
      }
      if(out.providers.length){const note=document.createElement('p');note.className='form-note';note.textContent='或使用本地账号登录；外部登录不会自动获得管理员权限。';host.append(note);}
    }catch(error){if(host.isConnected)host.textContent='外部登录暂时不可用；可使用本地恢复账号。';}
  }
  async function bootstrap(){
    me=await api('/api/iam/me');S.identity=me;
    const saved=sessionValue('codepier-space:'+me.id);
    const selected=me.spaces.find(x=>x.id===S.space_id)||me.spaces.find(x=>x.id===saved)||me.spaces[0];
    S.space_id=selected?.id||null;
    if(S.space_id)sessionValue('codepier-space:'+me.id,S.space_id);
    S.session={...S.session,instance_admin:me.instance_admin,spaces:me.spaces};
    if(!selected)S.page='identity';
  }
  function selector(){return `<div class="field iam-space-switch"><label for="iam-active-space">当前空间</label><select id="iam-active-space" aria-label="当前空间">${(me?.spaces||[]).map(s=>`<option value="${esc(s.id)}" ${s.id===S.space_id?'selected':''}>${esc(s.label)} · ${esc(levels[s.level]||s.level)}</option>`).join('')}</select></div>`;}
  function bindShell(){
    const choice=$('#iam-active-space');if(choice)choice.onchange=()=>switchSpace(choice.value);
    if(!admin())$$('[data-nav="members"]').forEach(n=>n.hidden=true);
    if(!me?.instance_admin)$$('[data-nav="identity-admin"]').forEach(n=>n.hidden=true);
  }
  async function switchSpace(id){
    const prior=S.space_id;
    if(id===prior)return;
    if(hasUnsavedChanges()&&!confirm('切换空间将清除当前标签页的草稿、附件和待确认请求。其他标签页不受影响。继续？')){const choice=$('#iam-active-space');if(choice)choice.value=prior;return;}
    const newest=await api('/api/iam/me');if(!newest.spaces.some(x=>x.id===id))throw new Error('你已不属于该空间');
    discardLocalWork();closeModal();stopEvents();clearTimeout(S.poll);
    if(typeof stopComputerApprovals==='function')stopComputerApprovals();
    window.CodePierPanelUpdate?.detach();window.CodePierIntegrations?.detach();
    me=newest;S.identity=me;S.space_id=id;S.session={...S.session,spaces:me.spaces};S.renderSeq++;
    Object.assign(S,{overview:null,projects:[],devices:[],settings:null,grants:[],integrations:null,workflow:null,delivery:null,vps:[],vpsQuery:'',vpsProject:'',auditOffset:0,auditQuery:'',auditStatus:'',auditSource:'',uiProjectQuery:''});
    S.suspendedUser=null;sessionValue('codepier-integration-receipts',null);sessionValue('codepier-space:'+me.id,id);
    S.page='overview';location.hash='overview';renderShell();connectEvents();if(typeof startComputerApprovals==='function')startComputerApprovals();await renderPage();
  }
  async function refresh(){
    const identity=await api('/api/iam/me');me=identity;S.identity=identity;
    if(S.space_id&&!me.spaces.some(x=>x.id===S.space_id)){
      discardLocalWork();closeModal();stopEvents();S.space_id=null;S.session={...S.session,spaces:me.spaces};S.renderSeq++;S.page='identity';renderShell();await renderPage();
      toast('当前空间权限已撤销。私有内容已清除，请选择仍可用的空间。',true);
    }else bindShell();
  }
  function form(title,body,save,options={}){
    const login=S.session,space=S.space_id;
    const dialog=modal(title,`<form id="iam-form">${body}<p class="form-note" id="iam-save-status" role="status"></p></form>`,`<button class="btn ghost" data-action="close-modal">取消</button><button type="submit" form="iam-form" class="btn primary">保存</button>`,!!options.large);
    $('#iam-form',dialog).onsubmit=e=>{e.preventDefault();const form=e.target;
      busy($('button[type="submit"]',dialog),async()=>{
        if(!form.reportValidity())return;
        const status=$('#iam-save-status',dialog);status.textContent='正在保存…';
        try{const result=await save(form);if(S.session!==login||S.space_id!==space)return;closeModal(dialog);
          if(options.done)await options.done(result);else{await refresh();await renderPage(false);toast('已保存');}
        }catch(error){if(dialog.isConnected)status.textContent=error.message;}
      });
    };
    return dialog;
  }
  function freshLink(provider){return busy(null,async()=>{const out=await post('/api/iam/oidc/'+encodeURIComponent(provider)+'/link',{return_to:'/#identity'});location.assign(out.redirect);});}
  async function html(page){
    me=await api('/api/iam/me');S.identity=me;
    if(page==='identity'){
      const [sessionData,providerData]=await Promise.all([api('/api/iam/sessions'),api('/api/auth/providers')]);providers=providerData.providers;
      return heading('我的账号','IDENTITY / SPACES',me.display_name,`<button class="btn primary" data-iam="new-space">新建团队空间</button><button class="btn" data-iam="accept-invite">接受邀请</button>`)+
        `<section class="panel"><div class="panel-head"><h2>空间</h2></div><div class="panel-body"><p class="form-note">空间选择仅作用于当前标签页。MCP 连接固定绑定授权空间，不会跟随这里的切换。</p>${me.spaces.map(s=>`<div class="grant-row"><div><h3>${esc(s.label)}</h3><small>${esc(levels[s.level])} · ${esc(s.kind)}</small></div><button class="btn small" data-iam-space="${esc(s.id)}">${s.id===S.space_id?'当前空间':'进入空间'}</button></div>`).join('')||empty('目前没有可用空间。')}${(me.disabled_spaces||[]).map(s=>`<div class="grant-row"><span>${esc(s.label)} · 已停用</span><button class="btn" data-iam-restore="${esc(s.id)}">恢复空间</button></div>`).join('')}</div></section>`+
        `<section class="panel"><div class="panel-head"><h2>登录身份</h2></div><div class="panel-body"><p>${me.local_login?'本地密码登录已启用。':'本账号仅使用外部身份登录。'}</p>${me.identities.map(i=>`<div class="grant-row"><div><strong>${esc(i.label)}</strong><p>${i.enabled?'已关联':'已停用 / 已解除关联'} · 权限有效至 ${esc(timeText(i.fresh_until))}</p></div><button class="btn danger small" data-iam-unlink="${esc(i.id)}">解除关联</button></div>`).join('')}<div class="actions">${providers.map(p=>`<button class="btn" data-iam-link="${esc(p.id)}">关联 ${esc(p.label)}</button><a class="btn ghost" href="/auth/oidc/${encodeURIComponent(p.id)}/start?return_to=%2F%23identity">重新认证</a>`).join('')}</div><p class="form-note">关联 / 解除关联要求近期登录。不会按邮箱自动合并账号；解除关联会撤销该身份的会话和 MCP 凭据。</p></div></section>`+
        `<section class="panel"><div class="panel-head"><h2>登录会话</h2></div><div class="panel-body">${sessionData.sessions.map(s=>`<div class="grant-row"><div><strong>${s.id===sessionData.current?'当前浏览器':'其他浏览器'}</strong><p>到期 ${esc(timeText(s.expires))}</p></div><button class="btn danger small" data-iam-session="${esc(s.id)}">退出此会话</button></div>`).join('')}</div></section>`;
    }
    if(page==='members'){
      if(!admin())return notice('只有空间管理员可以管理成员与角色分配。');
      const sid=encodeURIComponent(S.space_id),out=await Promise.all([api(`/api/iam/spaces/${sid}/members`),api('/api/access-roles'),api(`/api/iam/spaces/${sid}/assignments`),api(`/api/iam/spaces/${sid}/invites`)]);
      members=out[0].members;roles=out[1].roles;assignments=out[2].assignments;invites=out[3].invitations;
      return heading('空间成员','SPACE / MEMBERS',current()?.label,`<button class="btn" data-iam="edit-space">空间设置</button><button class="btn" data-iam="assign-role">分配角色</button><button class="btn primary" data-iam="invite">创建邀请</button>`)+
        `<section class="panel"><div class="panel-head"><h2>成员资格</h2></div><div class="panel-body"><p class="form-note">角色的使用、委派与编辑是独立权限。群组来源不可在这里改写；暂停成员可以覆盖所有来源。</p>${members.map(m=>`<div class="grant-row"><div><h3>${esc(m.display_name||m.username)}</h3><p>${esc(levels[m.level])} · ${esc(m.source)} · ${m.active&&!m.blocked?'有效':'已暂停'}</p><small class="mono">${esc(m.user_id)}</small></div><div class="actions"><button class="btn small" data-iam-member="${esc(m.user_id)}">人工资格</button><button class="btn danger small" data-iam-block="${esc(m.user_id)}" data-blocked="${m.blocked?'1':'0'}">${m.blocked?'恢复':'暂停'}</button></div></div>`).join('')}</div></section>`+
        `<section class="panel"><div class="panel-head"><h2>角色分配</h2></div><div class="panel-body">${assignments.map(a=>`<div class="grant-row"><div><strong>${esc(members.find(m=>m.user_id===a.user_id)?.username||a.user_id)} → ${esc(roles.find(r=>r.id===a.role_id)?.label||a.role_id)}</strong><p>${a.active?'有效':'已停用'} · ${a.may_delegate?'可通过 Profile 委派给 MCP':'只允许面板使用'} · ${esc(a.source)}</p></div>${a.source==='manual'?`<button class="btn small" data-iam-assignment="${esc(a.role_id)}" data-user="${esc(a.user_id)}">编辑</button>`:''}</div>`).join('')||empty('尚无角色分配。')}</div></section>`+
        `<section class="panel"><div class="panel-head"><h2>邀请</h2></div><div class="panel-body">${invites.map(i=>`<div class="grant-row"><span>${esc(levels[i.level])} · ${i.used_by?'已使用':'未使用'} · ${esc(timeText(i.expires))}</span><button class="btn danger small" data-iam-invite="${esc(i.id)}">撤销</button></div>`).join('')||empty('没有邀请。')}</div></section>`;
    }
    if(!me.instance_admin)return notice('需要实例管理员权限。');
    const out=await Promise.all([api('/api/iam/users'),api('/api/iam/oidc/providers')]);users=out[0].users;providers=out[1].providers;
    return heading('身份管理','INSTANCE / IDENTITY', 'OIDC 只负责认证；CodePier 管理角色和资源授权。',`<button class="btn" data-iam="sync">校验群组权限</button><button class="btn primary" data-iam="new-provider">添加 OIDC 提供者</button>`)+
      `<section class="panel"><div class="panel-head"><h2>身份提供者</h2></div><div class="panel-body">${providers.map(p=>`<div class="grant-row"><div><h3>${esc(p.label)} · ${p.enabled?'已启用':'已停用'}</h3><p class="iam-wrap">${esc(p.issuer)}</p><small>加入策略：${esc(p.admission)} · 权限校验窗口 ${p.freshness_seconds} 秒</small></div><div class="actions"><button class="btn small" data-iam-provider="${esc(p.id)}">编辑</button><button class="btn small" data-iam-check="${esc(p.id)}">测试发现</button><button class="btn small" data-iam-mappings="${esc(p.id)}">群组映射</button></div></div>`).join('')||empty('没有 OIDC 提供者。本地恢复登录继续有效。')}</div></section>`+
      `<section class="panel"><div class="panel-head"><h2>用户</h2></div><div class="panel-body">${users.map(u=>`<div class="grant-row"><div><strong>${esc(u.display_name||u.username)}</strong><p>${u.active?'有效':'已停用'} · ${u.instance_admin?'实例管理员':'普通用户'} · ${u.local_login?'本地登录':'外部登录'}</p><small class="mono">${esc(u.id)}</small></div><button class="btn small" data-iam-user="${esc(u.id)}">管理</button></div>`).join('')}</div></section>`;
  }
  function memberEdit(userId){
    const old=members.find(m=>m.user_id===userId&&m.source==='manual');
    form('人工成员资格',select('level','级别',Object.entries(levels),old?.level||'member')+check('active','启用人工成员资格',old?!!old.active:true)+notice('降级或移除最后一位主理人会被拒绝。群组来源的资格独立保留。'),f=>api(`/api/iam/spaces/${S.space_id}/members/${encodeURIComponent(userId)}`,{method:'PUT',body:JSON.stringify({level:f.elements.level.value,active:f.elements.active.checked,expected_version:old?.version||0})}));
  }
  function assignEdit(roleId='',userId=''){
    const old=assignments.find(a=>a.role_id===roleId&&a.user_id===userId&&a.source==='manual');
    const people=[...new Map(members.map(m=>[m.user_id,m.username])).entries()];
    form('分配动态角色',select('user_id','成员',people,userId)+select('role_id','角色',roles.map(r=>[r.id,r.label]),roleId)+check('active','允许使用角色',old?!!old.active:true)+check('may_delegate','允许通过 Profile 委派给 ChatGPT / MCP',!!old?.may_delegate)+notice('角色后续增加项目和能力会应用到已授权的角色连接。'),f=>{
      const uid=f.elements.user_id.value,rid=f.elements.role_id.value,currentAssignment=assignments.find(a=>a.role_id===rid&&a.user_id===uid&&a.source==='manual');
      return api(`/api/iam/spaces/${S.space_id}/assignments/${rid}/${uid}`,{method:'PUT',body:JSON.stringify({active:f.elements.active.checked,may_delegate:f.elements.may_delegate.checked,expected_version:currentAssignment?.version||0})});
    });
  }
  function providerEdit(id){
    const p=providers.find(p=>p.id===id);
    const body=field('label','显示名称',p?.label||'', 'required maxlength="80"')+field('issuer','精确 Issuer',p?.issuer||'','required type="url"')+field('client_id','Client ID',p?.client_id||'','required')+field('client_secret',p?'Client secret（留空保留）':'Client secret','','type="password" autocomplete="new-password" '+(p?'':'required'))+field('discovery_url','Discovery URL（留空使用默认）',p?.discovery_url||'','type="url"')+field('scopes','OIDC scopes',p?.scopes||'openid profile','required')+field('group_claim','群组 claim',p?.group_claim||'groups','required')+field('required_group','允许登录的群组（可空）',p?.required_group||'')+field('freshness_seconds','权限校验最大有效期（秒）',p?.freshness_seconds||900,'type="number" min="60" max="86400" required')+field('endpoint_origins','额外批准的端点来源（逗号分隔）',p?.endpoint_origins?.join(', ')||'')+select('admission','未知外部用户加入策略',[['closed','关闭；只允许已关联身份'],['jit','验证后创建个人账号和个人空间']],p?.admission||'closed')+check('enabled','启用外部登录',!!p?.enabled)+notice('群组限制/映射要求 UserInfo 返回字段；Microsoft Entra 的 UserInfo 不支持 groups，不能靠添加 ID Token claim 修复。请先验证兼容性；不要直接移除准入限制。',true)+notice('使用 TLS、精确回调和 issuer。不会从邮箱自动关联账号；请保留本地恢复管理员。',true);
    const dialog=form(p?'编辑 OIDC 提供者':'添加 OIDC 提供者',body,f=>{
      const data=Object.fromEntries(new FormData(f));data.enabled=f.elements.enabled.checked;data.freshness_seconds=Number(data.freshness_seconds);data.endpoint_origins=data.endpoint_origins.split(',').map(s=>s.trim()).filter(Boolean);
      if(!data.client_secret)delete data.client_secret;if(p)data.expected_version=p.version;
      return api('/api/iam/oidc/providers'+(p?'/'+p.id:''),{method:p?'PUT':'POST',body:JSON.stringify(data)});
    },{large:true});
    if(p)$('#iam-issuer',dialog).readOnly=true;
  }
  async function mappingEditor(id){
    const login=S.session,space=S.space_id;
    const [mapping,targets]=await Promise.all([api('/api/iam/oidc/providers/'+id+'/mappings'),api('/api/iam/oidc/targets')]);
    if(login!==S.session||space!==S.space_id)return;
    const existing=mapping.mappings.map(m=>`<div class="grant-row"><span>${esc(m.group_name)} → ${esc(targets.spaces.find(s=>s.id===m.space_id)?.label||m.space_id)} / ${esc(m.level)}</span><button type="button" class="btn danger small" data-delete-map="${esc(m.id)}">移除</button></div>`).join('');
    const dialog=form('OIDC 群组映射',existing+notice('群组映射仅支持 UserInfo claim；仅在 ID Token 返回群组的提供者（包括 Microsoft Entra）不兼容。',true)+field('group_name','精确群组名称','','required')+select('space_id','团队空间',targets.spaces.map(s=>[s.id,s.label]),space)+select('level','成员级别',[['member','成员'],['guest','访客'],['admin','空间管理员']],'member')+select('role_id','角色（可空）',[['','仅成员资格'],...targets.roles.filter(r=>r.space_id===space).map(r=>[r.id,r.label])],'')+check('may_delegate','允许委派选定角色')+notice('只移除本映射产生的资格；人工权限独立保留。群组不能授予实例管理员权限。'),f=>post('/api/iam/oidc/providers/'+id+'/mappings',{group_name:f.elements.group_name.value,space_id:f.elements.space_id.value,level:f.elements.level.value,role_id:f.elements.role_id.value||null,may_delegate:f.elements.may_delegate.checked}),{large:true});
    $('#iam-space_id',dialog).onchange=e=>{$('#iam-role_id',dialog).innerHTML='<option value="">仅成员资格</option>'+targets.roles.filter(r=>r.space_id===e.target.value).map(r=>`<option value="${esc(r.id)}">${esc(r.label)}</option>`).join('');};
    $$('[data-delete-map]',dialog).forEach(button=>button.onclick=()=>busy(button,async()=>{await api('/api/iam/oidc/mappings/'+button.dataset.deleteMap,{method:'DELETE'});closeModal(dialog);await mappingEditor(id);}));
  }
  function bind(){
    $$('[data-iam-space]').forEach(x=>x.onclick=()=>busy(x,()=>switchSpace(x.dataset.iamSpace)));
    $$('[data-iam-link]').forEach(x=>x.onclick=()=>freshLink(x.dataset.iamLink));
    $$('[data-iam-unlink]').forEach(x=>x.onclick=()=>busy(x,async()=>{if(!confirm('解除关联并撤销此身份签发的会话和 MCP 连接？'))return;const out=await api('/api/iam/identities/'+x.dataset.iamUnlink,{method:'DELETE'});if(out.relogin_required)endSession(true);else await renderPage(false);}));
    $$('[data-iam-session]').forEach(x=>x.onclick=()=>busy(x,async()=>{await api('/api/iam/sessions/'+x.dataset.iamSession,{method:'DELETE'});const checkSession=await api('/api/session');if(!checkSession.authenticated)endSession(true);else await renderPage(false);}));
    $$('[data-iam-member]').forEach(x=>x.onclick=()=>memberEdit(x.dataset.iamMember));
    $$('[data-iam-block]').forEach(x=>x.onclick=()=>busy(x,async()=>{if(!confirm('更新此成员所有来源的暂停状态？'))return;await api(`/api/iam/spaces/${S.space_id}/members/${x.dataset.iamBlock}/suspension`,{method:'PUT',body:JSON.stringify({blocked:x.dataset.blocked!=='1'})});await renderPage(false);}));
    $$('[data-iam-assignment]').forEach(x=>x.onclick=()=>assignEdit(x.dataset.iamAssignment,x.dataset.user));
    $$('[data-iam-invite]').forEach(x=>x.onclick=()=>busy(x,async()=>{await api(`/api/iam/spaces/${S.space_id}/invites/${x.dataset.iamInvite}`,{method:'DELETE'});await renderPage(false);}));
    $$('[data-iam-provider]').forEach(x=>x.onclick=()=>providerEdit(x.dataset.iamProvider));
    $$('[data-iam-mappings]').forEach(x=>x.onclick=()=>busy(x,()=>mappingEditor(x.dataset.iamMappings)));
    $$('[data-iam-check]').forEach(x=>x.onclick=()=>busy(x,async()=>{const out=await post('/api/iam/oidc/providers/'+x.dataset.iamCheck+'/check');modal('OIDC 发现已验证',`${(out.warnings||[]).map(text=>notice(esc(text),true)).join('')}<p>签名密钥：${out.signing_keys}</p><p>精确回调：</p><div class="code-box iam-wrap">${esc(out.callback)}</div><p>Back-channel logout：</p><div class="code-box iam-wrap">${esc(out.backchannel_logout)}</div>`);}));
    $$('[data-iam-restore]').forEach(x=>x.onclick=()=>busy(x,async()=>{const s=me.disabled_spaces.find(s=>s.id===x.dataset.iamRestore);await post('/api/iam/spaces/'+s.id+'/restore',{label:s.label,active:true,expected_version:s.version});await refresh();await renderPage(false);}));
    $$('[data-iam-user]').forEach(x=>x.onclick=()=>{const u=users.find(u=>u.id===x.dataset.iamUser);form('管理账号',check('active','启用账号',!!u.active)+check('instance_admin','实例管理员（全局管理权限）',!!u.instance_admin)+notice('修改会撤销该账号现有会话和 MCP 凭据；最后一位恢复管理员不能移除。'),f=>api('/api/iam/users/'+u.id,{method:'PUT',body:JSON.stringify({active:f.elements.active.checked,instance_admin:f.elements.instance_admin.checked,expected_version:u.version})}));});
    $$('[data-iam]').forEach(x=>x.onclick=()=>busy(x,async()=>{
      switch(x.dataset.iam){
        case 'new-space':{const key=uid();form('新建团队空间',field('label','名称','','required maxlength="100"'),f=>post('/api/iam/spaces',{label:f.elements.label.value,idempotency_key:key}));break;}
        case 'accept-invite':form('接受团队邀请',field('invitation','邀请凭据','','required autocomplete="off"'),f=>post('/api/iam/invites/accept',{invitation:f.elements.invitation.value}));break;
        case 'invite':form('创建一次性邀请',select('level','成员级别',[['member','成员'],['guest','访客'],['admin','空间管理员']],'member')+field('days','有效天数',7,'type="number" min="1" max="30" required'),f=>post(`/api/iam/spaces/${S.space_id}/invites`,{level:f.elements.level.value,days:Number(f.elements.days.value)}),{done:result=>{modal('保存一次性邀请',notice('仅显示一次。请通过可信渠道交给对方；对方必须登录自己的账号后接受。')+`<div class="code-box secret iam-wrap">${esc(result.invitation)}</div>`);}});break;
        case 'assign-role':assignEdit();break;
        case 'edit-space':{const s=current();form('空间设置',field('label','名称',s.label,'required maxlength="100"')+check('active','启用空间',!!s.active),f=>api(`/api/iam/spaces/${s.id}`,{method:'PUT',body:JSON.stringify({label:f.elements.label.value,active:f.elements.active.checked,expected_version:s.version})}));break;}
        case 'new-provider':providerEdit();break;
        case 'sync':await post('/api/iam/oidc/reconcile');toast('权限同步已完成；网络故障不会延长旧权限有效期。');await renderPage(false);break;
      }
    }));
  }
  function applyCapabilities(){
    if(!admin()){
      $('#role-create')?.setAttribute('hidden','');$$('[data-role-edit],[data-action="edit-project"]').forEach(x=>x.disabled=true);
    }
    $$('[data-action="agent-update"],[data-action="agent-restart"],[data-action="agent-uninstall"],[data-action="agent-commands"],[data-action="agent-repair"]').forEach(x=>{const d=S.devices.find(d=>d.id===x.dataset.id);if(d&&d.can_manage===false)x.disabled=true;});
  }
  return {loginButtons,bootstrap,selector,bindShell,html,bind,refresh,switchSpace,admin,current,reset,applyCapabilities};
})();
