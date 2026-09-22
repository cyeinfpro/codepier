'use strict';
// Only opaque request metadata is retained. No credentials, logs or source code.
window.CodePierPanelUpdate=(()=>{
  let generation=0,timer=null,reader=null,data=null,pending=null,submitting=false,note='';
  const terminal=new Set(['succeeded','failed','rolled_back','recovery_required']);
  const storageKey=()=>`codepier-panel-update:${sessionOwner(S.session)||'anonymous'}`;
  function savePending(value){pending=value;sessionValue(storageKey(),value?JSON.stringify(value):null);}
  function restorePending(){
    try{const value=JSON.parse(sessionValue(storageKey())||'null');return value&&['check','apply'].includes(value.action)&&/^[A-Za-z0-9._:-]{8,128}$/.test(value.body?.idempotency_key)?value:null;}
    catch{return null;}
  }
  function html(){return `<section class="panel panel-update" id="panel-update" aria-labelledby="panel-update-title">
    <div class="panel-head"><h2 id="panel-update-title">${icon('refresh')}面板更新</h2><span class="badge" id="panel-update-badge">读取中</span></div>
    <div class="panel-body"><p>从 GitHub 正式 Release 更新网页、Hub、Agent 安装包及安装脚本。账号、配对、项目和配置保留；不会强制重启已连接电脑上的 Agent。</p>
    <dl class="kv"><dt>运行版本</dt><dd id="panel-update-current">${esc(S.settings?.version||'—')}</dd><dt>更新来源</dt><dd id="panel-update-repo">—</dd><dt>可用版本</dt><dd id="panel-update-target">尚未检查</dd></dl>
    <div class="actions"><button class="btn" id="panel-update-check" type="button" disabled>检查 GitHub 更新</button><button class="btn primary" id="panel-update-apply" type="button" disabled>一键更新面板及 Agent 文件</button><button class="btn ghost" id="panel-update-refresh" type="button">刷新状态</button><button class="btn" id="panel-update-retry" type="button" hidden>重新提交原请求</button><button class="btn" id="panel-update-reload" type="button" hidden>刷新到新版本</button></div>
    <p id="panel-update-state" role="status" aria-live="polite">正在读取宿主机更新服务状态…</p><p class="form-note" id="panel-update-note" role="status" hidden></p>
    <details id="panel-update-release" hidden><summary>发布说明与校验值</summary><p id="panel-update-digest" class="mono"></p><pre id="panel-update-notes"></pre></details>
    <details id="panel-update-history" hidden><summary>更新阶段记录</summary><pre id="panel-update-events"></pre></details>
    <details id="panel-update-setup" hidden><summary>首次启用更新服务</summary><p>需要 Linux、systemd 与 Docker Compose。先在服务器部署包含本功能的新版源码，再在该部署目录运行一次：</p><div class="code-box"><pre>sudo python3 scripts/panel_updater.py install --root "$PWD"</pre></div><p>宿主机更新服务独立运行，网页进程没有 Docker 管理权限。安装或更新面板时也可使用 <code>sudo bash install.sh --enable-panel-update</code>。未完成的更新须先在宿主机核查，不能靠重新安装服务覆盖。</p></details>
    <p class="form-note">切换前保存完整数据副本。更新期间短暂进入维护状态；关闭页面不取消更新。原数据卷、镜像和更新记录不会自动删除，请定期在宿主机归档。</p></div></section>`;}
  function text(id,value){const node=document.getElementById(id);if(node&&node.textContent!==String(value??''))node.textContent=String(value??'');}
  function paint(){
    if(!document.getElementById('panel-update'))return;
    const d=data||{},job=d.operation,busy=submitting||!!pending||!!d.busy,enabled=d.enabled===true;
    text('panel-update-current',d.running_version||S.settings?.version||'—');
    text('panel-update-repo',d.repository||'宿主机固定配置，网页不能更换来源');
    text('panel-update-target',d.candidate?`${d.candidate.version}${d.update_available?' · 可更新':' · 无更新版本或检查已过期'}`:'尚未检查');
    const labels={queued:'已排队',running:'进行中',succeeded:'已完成',failed:'失败',rolled_back:'已恢复旧版',recovery_required:'需宿主机处理'};
    text('panel-update-badge',submitting?'提交中':job?labels[job.state]||'结果待核实':enabled?'已启用':'未连接');
    text('panel-update-state',job?`${job.message||'更新处理中'}${job.id?' · 操作 '+job.id:''}`:d.reason||(enabled?'点击检查，读取 GitHub 最新正式发布。':'正在读取宿主机更新服务状态…'));
    text('panel-update-note',note);$('#panel-update-note').hidden=!note;
    $('#panel-update-check').disabled=!enabled||busy||d.recovery_required;
    $('#panel-update-apply').disabled=!enabled||busy||!d.update_available||d.recovery_required;
    $('#panel-update-refresh').disabled=submitting;
    $('#panel-update-retry').hidden=!(pending&&enabled&&d.request_found===false&&!d.busy);
    $('#panel-update-retry').disabled=submitting;
    $('#panel-update-reload').hidden=!(job?.kind==='apply'&&job.state==='succeeded'&&d.running_version===job.target_version);
    $('#panel-update-setup').hidden=enabled;
    $('#panel-update-release').hidden=!d.candidate;
    text('panel-update-digest',d.candidate?`SHA-256: ${d.candidate.sha256}`:'');
    text('panel-update-notes',d.candidate?.notes||'此 Release 未提供说明。');
    $('#panel-update-history').hidden=!job?.events?.length;
    text('panel-update-events',(job?.events||[]).map(e=>`${timeText(e.at)}  ${e.message}`).join('\n'));
  }
  function schedule(ms=2000){clearTimeout(timer);if(S.session&&S.page==='settings')timer=setTimeout(()=>load(),ms);}
  async function load(){
    const gen=generation;if(!S.session||S.page!=='settings'||!$('#panel-update'))return;
    reader?.abort();const control=new AbortController();reader=control;
    try{
      const key=pending?.body?.idempotency_key;
      const result=await api('/api/panel-update/status'+(key?'?request_key='+encodeURIComponent(key):''),{signal:control.signal,retryDelays:[],requestTimeout:10000});
      if(gen!==generation)return;
      data=result;
      if(result.enabled){
        if(pending&&result.request_found&&terminal.has(result.operation?.state))savePending(null);
        note=pending&&result.request_found===false?'宿主机未找到原请求记录。核实后可使用“重新提交原请求”；不会生成第二个更新编号。':'';
      }else if(pending){note='更新结果尚未核实。正在恢复连接；服务未连接不代表更新失败，请不要重新创建更新。';}
      paint();
      if(result.busy||pending||result.recovery_required)schedule(result.enabled?2000:4000);
    }catch(error){
      if(gen!==generation||error.name==='AbortError'||error.code==='SESSION_CHANGED')return;
      note='连接暂时中断，正在恢复更新进度。不会自动重复提交更新。';paint();schedule(4000);
    }finally{if(reader===control)reader=null;}
  }
  async function submit(action,reuse=false){
    if(submitting||(!reuse&&pending)||!data?.enabled)return;
    const gen=generation;
    if(!reuse){
      if(action==='apply'){
        if(!data.update_available||data.busy||data.recovery_required)return;
        const c=data.candidate;
        if(!confirm(`从 ${data.repository} 更新到 ${c.version}？\n包含面板、Hub 和 Agent 分发文件；不会重启已连接电脑上的 Agent。\n切换期间面板短暂维护，原数据卷保留为备份。`))return;
        savePending({action,body:{idempotency_key:'panel-update-'+uid(),version:c.version,release_id:c.release_id,sha256:c.sha256,confirmation:c.version}});
      }else savePending({action,body:{idempotency_key:'panel-check-'+uid()}});
    }
    const request=pending;if(!request)return;
    submitting=true;note='正在提交并保存更新请求…';paint();
    try{
      await post('/api/panel-update/'+request.action,request.body);
      if(gen===generation)note='请求已保存，正在读取更新进度。';
    }catch(error){
      if(error.code==='SESSION_CHANGED'||gen!==generation)return;
      // Only a definitive pre-execution rejection releases the request identity.
      if(error.status>=400&&error.status<500){savePending(null);note=error.message;toast(error.message,true);}
      else if(gen===generation)note='提交回执暂未收到，正在按原请求编号核实。请不要重复点击更新。';
    }finally{
      submitting=false;
      if(gen===generation){paint();await load();}
    }
  }
  function detach(){generation++;clearTimeout(timer);timer=null;reader?.abort();reader=null;data=null;note='';}
  function bind(){
    detach();pending=restorePending();
    $('#panel-update-check').onclick=()=>submit('check');
    $('#panel-update-apply').onclick=()=>submit('apply');
    $('#panel-update-refresh').onclick=()=>load();
    $('#panel-update-retry').onclick=()=>submit(pending?.action,true);
    $('#panel-update-reload').onclick=()=>{
      const address=$('#settings-form [name="public_url"]');
      const password=$('#password-form');
      const dirty=hasUnsavedChanges()||(address&&address.value!==S.settings.public_url)||[...(password?.querySelectorAll('input')||[])].some(i=>i.value);
      if(!dirty||confirm('刷新将丢弃当前页面的未保存输入，继续？'))location.reload();
    };
    paint();load();
  }
  return {html,bind,detach};
})();
