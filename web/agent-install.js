'use strict';
// Installation commands and pairing material live only in the current dialog.
function agentInstallFields(platform='posix',repair=false){
  return `<div class="field"><label>面板地址</label><input name="hub_url" value="${esc(location.origin)}" required placeholder="http://服务器IP:8765"><small>目标电脑通过这个地址连接面板。</small></div><div class="field"><label>目标电脑系统</label><select name="platform"><option value="posix" ${platform==='posix'?'selected':''}>Linux / macOS</option><option value="windows" ${platform==='windows'?'selected':''}>Windows</option></select></div><div class="field"><label>允许访问的目录</label><input name="allow_root" required maxlength="2048" autocomplete="off" spellcheck="false"><small>填写目标电脑上的完整目录路径。文件工具限制在授权目录内，完整 Shell 不受目录沙箱限制。</small></div>${repair?'<p class="form-note">已有节点保留原执行权限，不会因为修复安装而打开已关闭的权限。旧版开启执行的方法见“系统设置 → 已有 Agent 开启执行能力”。</p>':'<label class="check"><input name="enable_execution" type="checkbox" checked>安装时开启 Shell 与目录任务（默认）</label><p class="form-note">Shell 使用 Agent 当前系统用户的完整权限，不是目录沙箱；还需要面板项目与客户端的执行授权。桌面控制仍独立关闭。取消勾选可安装为无执行能力模式。</p>'}`;
}
function agentExecutionHelp(){
  const posix='cd "$HOME/.codepier-agent/runtime" &&\n.venv/bin/python -m agent --config "$HOME/.codepier-agent/config.json" configure --shell full';
  const windows="$Base = Join-Path $env:USERPROFILE '.codepier-agent'\nPush-Location (Join-Path $Base 'runtime')\ntry {\n  & (Join-Path $Base 'runtime/.venv/Scripts/python.exe') -m agent --config (Join-Path $Base 'config.json') configure --shell full\n  if ($LASTEXITCODE -ne 0) { throw 'Agent configuration failed' }\n} finally { Pop-Location }";
  return uiHelp('已有 Agent 开启执行能力',`<p>旧配置和升级会保留原权限，不会自动放开已关闭的能力。在对应电脑的本机终端执行以下命令，可以开启完整 Shell 及可写授权目录的任务能力。仍需给面板项目勾选执行权限，并给客户端授权 execute；只读目录不会变为可写。</p><h3>受管安装 · Linux / macOS</h3><div class="code-box"><pre>${esc(posix)}</pre></div><h3>受管安装 · Windows PowerShell</h3><div class="code-box"><pre>${esc(windows)}</pre></div><p class="form-note">上述命令使用默认安装目录。自定义安装请替换安装路径；源码运行请在 Agent 源码目录使用其 Python 环境，并通过 <code>--config</code> 指定原配置文件。不要重新初始化或创建第二份配置。运行中的 Agent 会自动重载有效配置，无需重新配对；随后在项目弹窗再次验证保存。</p><p class="form-note">完整 Shell 使用 Agent 系统用户权限，不是目录沙箱。只需预定义任务时，可仅把原配置对应目录的 <code>allow_tasks</code> 改为 <code>true</code>，保持 <code>shell.enabled=false</code>。桌面控制仍需独立开启。</p>`);
}
async function agentInstallSetup(device=null){
  const login=S.session;
  const dialog=modal(device?'安装 / 修复 Agent · '+device.name:'接入电脑',`<form id="device-form">${device?'':'<div class="field"><label>设备名称</label><input name="name" placeholder="例如：Home-PC" required maxlength="80"></div>'}${agentInstallFields(device?.info?.platform==='Windows'?'windows':'posix',!!device)}<p class="form-note">${device?'在这台节点执行新命令，会校验设备身份并安全修复或升级受管 Agent；授权目录、配置、状态和日志会保留。':'复制生成的命令，在目标电脑的终端执行，即可下载、安装并自动启动连接程序。'}</p><p id="device-install-error" class="form-note" role="status"></p></form>`,`<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" id="create-device" type="submit" form="device-form">生成安装命令</button>`);
  const form=$('#device-form',dialog),button=$('#create-device',dialog),error=$('#device-install-error',dialog);
  const current=()=>dialog.isConnected&&S.session===login;
  let submitting=false,uncertain=false,pairing=null;
  const hint=()=>{form.elements.allow_root.placeholder=form.elements.platform.value==='windows'?'例如：D:\\Projects':'例如：/home/me/Projects 或 /Users/me/Projects';form.elements.allow_root.setCustomValidity('');};
  form.elements.platform.onchange=hint;form.elements.allow_root.oninput=()=>form.elements.allow_root.setCustomValidity('');hint();
  if(device?.info?.roots?.[0]?.path)form.elements.allow_root.value=device.info.roots[0].path;
  form.onsubmit=async event=>{
    event.preventDefault();
    if(submitting||uncertain||!current())return;
    const platform=form.elements.platform.value,allow_root=form.elements.allow_root.value.trim();
    const absolute=platform==='windows'?/^(?:[a-zA-Z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)/.test(allow_root):allow_root.startsWith('/');
    form.elements.allow_root.setCustomValidity(absolute&&!/[\u0000-\u001f\u007f]/.test(allow_root)?'':'请输入目标电脑上的完整目录路径。');
    if(!form.reportValidity())return;
    const options={hub_url:form.elements.hub_url.value.trim(),platform,allow_root,enable_execution:form.elements.enable_execution?.checked??true};
    submitting=true;button.disabled=true;error.textContent='';
    try{
      pairing=device?null:(await post('/api/devices',{name:form.elements.name.value.trim(),hub_url:options.hub_url})).pairing;
      if(!current())return;
      showAgentInstallCommand(device||{id:pairing.device_id,name:pairing.name},options,pairing);
      loadBasics().catch(()=>{});
    }catch(cause){
      if(!current())return;
      uncertain=cause.code==='NETWORK_UNCERTAIN';
      if(pairing&&!device){
        showAgentInstallCommand({id:pairing.device_id,name:pairing.name},options,pairing,false);
        return;
      }
      error.textContent=uncertain?'设备创建结果暂时无法确认。请先回到设备列表查看，避免重复添加。':cause.message;
    }finally{if(current()){submitting=false;button.disabled=uncertain;}}
  };
}
function showAgentInstallCommand(device,options,pairing=null,autoGenerate=true){
  const login=S.session;
  const terminal=options.platform==='windows'?'PowerShell':'终端';
  const dialog=modal('在目标电脑安装 · '+device.name,`<p>在要接入的电脑上打开${terminal}，粘贴并执行下方命令。新节点会完成安装；已有受管节点会先校验身份，再原子修复或升级并自动恢复连接。</p><dl class="kv"><dt>允许访问</dt><dd>${esc(options.allow_root)}</dd><dt>系统</dt><dd>${options.platform==='windows'?'Windows':'Linux / macOS'}</dd><dt>新装执行能力</dt><dd>${options.enable_execution===false?'关闭 Shell / 目录任务':'开启 Shell / 目录任务'}；已有配置保持不变</dd></dl><div class="field"><label for="agent-install-command">安装命令</label><textarea id="agent-install-command" class="agent-install-command" rows="6" readonly spellcheck="false" autocapitalize="off" autocomplete="off" placeholder="正在生成安装命令…"></textarea></div><p id="agent-install-status" class="form-note" role="status">正在生成安装命令…</p><p class="form-note">命令会下载并校验当前面板的 Agent 包，使用一次性连接凭据。已有节点的配置、授权目录、状态和日志会保留；请只在对应目标电脑执行。</p>`,`<button class="btn ghost" data-action="close-modal">完成</button>${pairing?'<button class="btn ghost" id="download-pairing">下载配对文件</button>':''}<button class="btn" id="agent-install-refresh">重新生成</button><button class="btn primary" id="agent-install-copy" disabled>复制安装命令</button>`,true);
  const field=$('#agent-install-command',dialog),status=$('#agent-install-status',dialog),copyButton=$('#agent-install-copy',dialog),refresh=$('#agent-install-refresh',dialog);
  let command='',expires=0,creating=false,disposed=false;
  const current=()=>!disposed&&dialog.isConnected&&S.session===login;
  function expiry(){
    if(!current())return;
    if(command&&Date.now()/1000>=expires){command='';field.value='';copyButton.disabled=true;status.textContent='安装命令已过期，请重新生成。';}
  }
  async function generate(){
    if(creating||!current())return;
    creating=true;refresh.disabled=true;copyButton.disabled=true;status.textContent='正在生成安装命令…';
    try{
      const result=await post('/api/devices/'+encodeURIComponent(device.id)+'/install-ticket',options);
      if(!current())return;
      if(typeof result.command!=='string'||!result.command||!Number.isFinite(result.expires_at))throw new Error('没有收到有效安装命令，请重新生成。');
      command=result.command;expires=result.expires_at;field.value=command;copyButton.disabled=false;
      status.textContent='请在 '+timeText(expires)+' 前执行。命令使用一次后失效。';
      expiry();field.focus({preventScroll:true});
    }catch(cause){
      if(!current())return;
      // A generation failure must not create another device or discard its pairing fallback.
      command='';field.value='';status.textContent='设备已保留，安装命令暂时无法生成。'+cause.message+' 可重试'+(pairing?'，或下载配对文件手动接入。':'。');
    }finally{if(current()){creating=false;refresh.disabled=false;}}
  }
  copyButton.onclick=async()=>{
    expiry();if(!current()||!command||copyButton.disabled)return;
    try{
      if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(command);
      else{field.focus();field.select();if(!document.execCommand('copy'))throw new Error('copy');}
      if(current())toast('已复制安装命令');
    }catch{if(current()){field.focus();field.select();status.textContent='已选中安装命令，请按 Ctrl+C 或 ⌘C 复制。';}}
  };
  refresh.onclick=generate;
  if(pairing)$('#download-pairing',dialog).onclick=()=>{if(current())download('pairing.json',json(pairing));};
  const ticker=setInterval(expiry,1000),cleanup=S.modalCleanup;
  S.modalCleanup=()=>{disposed=true;clearInterval(ticker);command='';field.value='';pairing=null;cleanup?.();};
  if(autoGenerate)generate();
  else status.textContent='设备已创建，但安装命令暂时无法生成。可重试，或下载配对文件手动接入。';
}

const AGENT_LIFECYCLE={
  agent_update:{label:'更新 Agent',route:'agent-update'},
  agent_restart:{label:'重启 Agent',route:'agent-restart'},
  agent_uninstall:{label:'卸载 Agent',route:'agent-uninstall'},
};
function agentActionLabel(action){return AGENT_LIFECYCLE[action]?.label||action||'Agent 操作';}
function agentLifecycleBusy(device){return ['queued','running','reconnecting','cancelling','unknown'].includes(device?.agent?.latest_action?.state);}
function agentLifecycleSummary(device){
  const agent=device.agent||{},latest=agent.latest_action,current=agent.version||device.info?.version||'—',target=agent.target_version||'—';
  const managed=agent.managed&&agent.service;
  const versionState=agent.update_available?'<span class="badge running">可更新</span>':current!=='—'?'<span class="badge completed">已是当前版本</span>':'<span class="badge neutral">等待版本信息</span>';
  const service=managed?`<span class="badge purple">受管服务${agent.service_kind?' · '+esc(agent.service_kind):''}</span>`:'<span class="badge neutral">未纳入一键管理</span>';
  const latestText=latest?`<div class="lifecycle-latest"><span>${esc(agentActionLabel(latest.tool))}</span>${badge(latest.state)}<small>${esc(timeText(latest.updated||latest.created))}</small></div>`:'';
  const brandState={pending:'CodePier 命名迁移将在节点空闲后自动完成',migrating:'正在迁移 CodePier 路径与服务',completed:'CodePier 命名迁移已完成',rollback:'命名迁移未完成，已恢复原安装；检查后重新升级',error:'命名迁移未完成，原安装已保留；请检查本机管理记录',recovery_required:'命名迁移需要恢复，请先检查本机备份与迁移记录'}[agent.brand_migration];
  const issue=agent.last_error||agent.reason;
  return `<div class="device-lifecycle"><div class="device-version"><div><small>Agent 版本</small><strong>${esc(current)}</strong>${target&&target!==current?`<span>→ ${esc(target)}</span>`:''}</div>${versionState}</div><div class="lifecycle-badges">${service}${agent.status&&agent.status!=='ready'?`<span class="badge running">${esc(agent.status)}</span>`:''}</div>${brandState?`<p class="lifecycle-note">${esc(brandState)}</p>`:''}${latestText}${issue?`<p class="lifecycle-note">${esc(issue)}</p>`:''}</div>`;
}
function agentLifecycleButtons(device,compact=false){
  const agent=device.agent||{},declared=Array.isArray(agent.actions)?agent.actions:[],actions=device.online?declared:[],locked=agentLifecycleBusy(device);
  const title=locked?'该节点已有生命周期操作进行中':(agent.reason||'');
  const updateLabel=agent.update_available?'更新 Agent':'重新部署';
  const updateDisabled=locked||!agent.can_update?' disabled':'';
  const restartDisabled=locked||!agent.can_restart?' disabled':'';
  const update=actions.includes('agent_update')?`<button class="btn ${compact?'small':'primary small'}" data-action="agent-update" data-id="${esc(device.id)}"${updateDisabled} title="${esc(title)}">${icon('download')}${updateLabel}</button>`:'';
  const restart=actions.includes('agent_restart')?`<button class="btn ghost small" data-action="agent-restart" data-id="${esc(device.id)}"${restartDisabled} title="${esc(title)}">${icon('refresh')}重启</button>`:'';
  const repair=device.enabled&&(!device.online||!declared.length)?`<button class="btn ${compact?'small':'primary small'}" data-action="agent-repair" data-id="${esc(device.id)}">${icon('download')}修复 / 升级</button>`:'';
  // Keep overview cards compact; command management belongs in the full node dialog.
  const commands=compact?'':`<button class="btn ghost small" data-action="agent-commands" data-id="${esc(device.id)}">命令升级 / 卸载</button>`;
  return update+restart+repair+commands;
}
async function refreshAgentNodePages(){
  await loadBasics();
  if(['overview','devices'].includes(S.page)&&!$('.modal'))await renderPage(false);
}
async function trackAgentLifecycle(operationId,device,action){
  const deadline=Date.now()+16*60*1000;
  try{
    while(Date.now()<deadline&&S.session){
      const operation=await tool('operations_wait',{operation_id:operationId,wait_seconds:8});
      if(!operation.pending){
        if(operation.state==='succeeded'&&operation.result?.ok){
          const text=action==='agent_update'?'更新已交接，节点会短暂离线并以校验后的版本重新上线。':action==='agent_restart'?'重启已交接，节点会短暂离线后自动恢复。':'卸载已交接。面板节点记录与项目映射已保留，本机项目文件不会被删除。';
          toast(text);
        }else{
          toast((operation.result?.error?.message||operation.error||agentActionLabel(action)+'失败')+' · 操作 '+operationId,true);
        }
        await refreshAgentNodePages().catch(()=>{});
        for(const delay of [3000,8000]){await pause(delay);await refreshAgentNodePages().catch(()=>{});}
        return;
      }
      await pause(400);
    }
  }catch(error){
    toast(`${agentActionLabel(action)}已保存为操作 ${operationId.slice(0,12)}，状态刷新暂时中断：${error.message}`,true);
  }
}
function agentLifecycleModal(device,action){
  const spec=AGENT_LIFECYCLE[action];if(!spec)return;
  const agent=device.agent||{},current=agent.version||device.info?.version||'未知',target=agent.target_version||current;
  let body='',confirmLabel=spec.label;
  if(action==='agent_update'){
    confirmLabel=agent.update_available?'校验并更新':'重新部署当前版本';
    body=notice(`<strong>${esc(current)}</strong> → <strong>${esc(target)}</strong><br>新运行时会先完整下载、校验并安装依赖，面板确认结果后才原子切换。配置、授权目录、状态和日志保持不变；启动检查失败会恢复上一版本。`,true);
  }else if(action==='agent_restart'){
    body=notice('将通过系统服务重启 Agent。配置和项目文件不会改变，节点会短暂离线；设备仍有其他操作时，面板会拒绝本次重启。',true);
  }else{
    body=`${notice('<strong>这会从目标电脑移除 Agent 服务、运行时、配置、状态和日志。</strong><br>不会删除授权目录中的项目文件；面板中的节点记录、项目映射和审计记录也会保留，便于重新安装。')}<div class="field confirm-name"><label for="agent-uninstall-confirm">输入完整节点名称以确认</label><input id="agent-uninstall-confirm" name="confirmation" autocomplete="off" spellcheck="false" placeholder="${esc(device.name)}"><small>必须准确输入：<code>${esc(device.name)}</code></small></div>`;
    confirmLabel='确认卸载 Agent';
  }
  const dialog=modal(`${spec.label} · ${device.name}`,`<form id="agent-lifecycle-form">${body}<p id="agent-lifecycle-status" class="form-note" role="status"></p></form>`,`<button class="btn ghost" data-action="close-modal">取消</button><button class="btn ${action==='agent_uninstall'?'danger':'primary'}" id="agent-lifecycle-submit" type="submit" form="agent-lifecycle-form">${esc(confirmLabel)}</button>`);
  const form=$('#agent-lifecycle-form',dialog),button=$('#agent-lifecycle-submit',dialog),status=$('#agent-lifecycle-status',dialog);
  form.onsubmit=async event=>{
    event.preventDefault();if(button.disabled)return;
    const confirmation=action==='agent_uninstall'?$('#agent-uninstall-confirm',dialog).value:'';
    if(action==='agent_uninstall'&&confirmation!==device.name){
      const field=$('#agent-uninstall-confirm',dialog);field.setCustomValidity('请输入完整且完全一致的节点名称。');field.reportValidity();return;
    }
    const storage=`codepier-agent-lifecycle:${device.id}:${action}`;
    const idempotencyKey=sessionValue(storage)||'panel-'+uid();sessionValue(storage,idempotencyKey);
    const original=button.innerHTML;button.disabled=true;button.innerHTML='<i class="spinner"></i> 正在提交';status.textContent='正在把操作保存到面板，并核对节点状态…';
    try{
      const receipt=await api(`/api/devices/${encodeURIComponent(device.id)}/${spec.route}`,{method:'POST',body:JSON.stringify({idempotency_key:idempotencyKey,confirmation}),retrySafe:true,requestTimeout:30000});
      if(!receipt?.operation_id)throw new Error('面板没有返回可跟踪的操作编号。');
      sessionValue(storage,null);
      closeModal(dialog);toast(`${spec.label}已保存 · ${receipt.operation_id.slice(0,12)}`);
      trackAgentLifecycle(receipt.operation_id,device,action);
      await refreshAgentNodePages().catch(()=>{});
    }catch(error){
      if(error.code!=='NETWORK_UNCERTAIN')sessionValue(storage,null);
      if(dialog.isConnected){status.textContent=error.code==='NETWORK_UNCERTAIN'?'提交结果暂时无法确认。已保留同一幂等键；网络恢复后点击同一按钮会核实原操作，不会重复创建。':error.message;button.disabled=false;button.innerHTML=original;}
    }
  };
  if(action==='agent_uninstall')$('#agent-uninstall-confirm',dialog).oninput=e=>e.target.setCustomValidity('');
}


function showAgentMaintenanceCommands(device){
  const login=S.session,platform=device.info?.platform==='Windows'?'windows':'posix';
  const body=`<form id="agent-command-form"><p>在这台节点自己的终端执行。升级保留原身份、配置、授权目录和历史，不需要重新配对；卸载会在终端要求确认，不删除安装目录之外的项目文件。</p><div class="field"><label>面板地址</label><input name="hub_url" required value="${esc(location.origin)}"></div><div class="field"><label>目标电脑系统</label><select name="platform"><option value="posix" ${platform==='posix'?'selected':''}>Linux / macOS</option><option value="windows" ${platform==='windows'?'selected':''}>Windows PowerShell</option></select></div><div class="field"><label>Agent 安装目录（可选）</label><input name="install_dir" autocomplete="off" spellcheck="false" maxlength="2048" placeholder="留空使用当前用户的 .codepier-agent"><small>自定义安装时填写目标电脑上的绝对路径；不要填写项目目录。</small></div><p class="form-note">命令固定本次安装包校验值并检查节点身份，只适用于面板一键安装的受管 Agent。面板更新后旧命令可能失效，届时重新生成。复制不会执行任何操作。</p><div class="field"><label for="agent-command-upgrade">一键升级命令</label><textarea id="agent-command-upgrade" class="agent-install-command" rows="4" readonly spellcheck="false"></textarea><button class="btn small" type="button" data-copy-command="upgrade" disabled>复制升级命令</button></div><div class="field"><label for="agent-command-uninstall">一键卸载命令</label><textarea id="agent-command-uninstall" class="agent-install-command" rows="4" readonly spellcheck="false"></textarea><button class="btn small" type="button" data-copy-command="uninstall" disabled>复制卸载命令</button></div><p class="form-note">新版安装或升级后，还可以离线使用安装目录内的 agentctl uninstall（Windows：agentctl.ps1 uninstall）。卸载会删除 Agent 配置、密钥、日志和状态，请先备份需要的记录。</p><p id="agent-command-status" class="form-note" role="status"></p></form>`;
  const dialog=modal('本机命令管理 · '+device.name,body,`<button class="btn ghost" data-action="close-modal">关闭</button><button class="btn primary" type="submit" form="agent-command-form" id="agent-command-generate">生成管理命令</button>`,true);
  const form=$('#agent-command-form',dialog),status=$('#agent-command-status',dialog),button=$('#agent-command-generate',dialog);
  const current=()=>dialog.isConnected&&S.session===login;
  let revision=0,submitting=false;
  function clear(){revision++;for(const action of ['upgrade','uninstall']){$('#agent-command-'+action,dialog).value='';$('[data-copy-command="'+action+'"]',dialog).disabled=true;}}
  for(const field of [form.elements.hub_url,form.elements.platform,form.elements.install_dir])field.addEventListener('input',()=>{clear();status.textContent='条件已修改，请重新生成管理命令。';});
  form.onsubmit=async event=>{
    event.preventDefault();if(submitting||!current()||!form.reportValidity())return;
    clear();const version=revision;
    const options={hub_url:form.elements.hub_url.value.trim(),platform:form.elements.platform.value,install_dir:form.elements.install_dir.value.trim()};
    submitting=true;button.disabled=true;status.textContent='正在生成带校验和节点身份检查的命令…';
    try{
      const result=await post('/api/devices/'+encodeURIComponent(device.id)+'/agent-commands',options);
      if(!current()||version!==revision)return;
      if(!['upgrade','uninstall'].every(action=>typeof result.commands?.[action]==='string'&&result.commands[action]))throw new Error('没有收到完整管理命令，请重试。');
      for(const action of ['upgrade','uninstall']){$('#agent-command-'+action,dialog).value=result.commands[action];$('[data-copy-command="'+action+'"]',dialog).disabled=false;}
      status.textContent='命令已生成。请仅在 '+device.name+' 的本机终端执行，不要通过这个 Agent 自身的远程 Shell 执行。';
    }catch(error){if(current()&&version===revision)status.textContent=error.message;}
    finally{if(current()){submitting=false;button.disabled=false;}}
  };
  for(const action of ['upgrade','uninstall'])$('[data-copy-command="'+action+'"]',dialog).onclick=async()=>{
    const field=$('#agent-command-'+action,dialog);if(!current()||!field.value)return;
    try{if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(field.value);else{field.focus();field.select();if(!document.execCommand('copy'))throw new Error('copy');}if(current())toast('已复制'+(action==='upgrade'?'升级':'卸载')+'命令');}
    catch{if(current()){field.focus();field.select();status.textContent='已选中命令，请按 Ctrl+C 或 ⌘C 复制。';}}
  };
  const cleanup=S.modalCleanup;S.modalCleanup=()=>{clear();cleanup?.();};form.requestSubmit();
}
