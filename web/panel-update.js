'use strict';
// Only opaque request metadata is retained. No credentials, logs or source code.
window.CodePierPanelUpdate = (() => {
  let generation = 0,
    timer = null,
    reader = null,
    data = null,
    pending = null,
    submitting = false,
    note = '',
    owner = null,
    watching = false,
    reloadAttempted = false,
    deferredKey = '';
  // Captured from the script loaded with this document, never from a later API response.
  const loadedVersion = new URL(
    document.currentScript?.src || location.href,
    location.href,
  ).searchParams
    .get('v')
    ?.replace(/^codepier-/, '');
  const reloadParameter = '_codepier_updated';
  const terminal = new Set(['succeeded', 'failed', 'rolled_back', 'recovery_required']);
  const storageKey = () => `codepier-panel-update:${sessionOwner(S.session) || 'anonymous'}`;
  function savePending(value) {
    pending = value;
    sessionValue(storageKey(), value ? JSON.stringify(value) : null);
  }
  function restorePending() {
    try {
      const value = JSON.parse(sessionValue(storageKey()) || 'null');
      return value &&
        ['check', 'apply'].includes(value.action) &&
        /^[A-Za-z0-9._:-]{8,128}$/.test(value.body?.idempotency_key)
        ? value
        : null;
    } catch {
      return null;
    }
  }
  function entry() {
    const version = data?.running_version || S.settings?.version || loadedVersion || '—';
    return S.session?.instance_admin
      ? '<button type="button" class="panel-update-entry" data-panel-update-open aria-label="面板版本与更新"><span data-panel-update-version>v' +
          esc(version) +
          '</span><strong data-panel-update-status>检查更新</strong></button>'
      : '<span class="panel-update-version">v' + esc(version) + '</span>';
  }
  async function open() {
    if (!S.session?.instance_admin) return;
    if (S.page !== 'settings') await navigate('settings');
    if (S.page !== 'settings') return;
    document.querySelector('.settings-groups [data-settings-group="advanced"]')?.click();
    const section = document.querySelector('.settings-maintenance');
    if (section) section.open = true;
    document.getElementById('panel-update')?.scrollIntoView({ block: 'start' });
    document.getElementById('panel-update-title')?.focus({ preventScroll: true });
  }
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-panel-update-open]')) open();
  });
  function html() {
    return `<section class="panel panel-update" id="panel-update" aria-labelledby="panel-update-title">
    <div class="panel-head"><h2 id="panel-update-title" tabindex="-1">${icon('refresh')}面板更新</h2><span class="badge" id="panel-update-badge">读取中</span></div>
    <div class="panel-body"><p>从 GitHub 正式 Release 更新网页、Hub、Agent 安装包及安装脚本。账号、配对、项目和配置保留。面板健康检查成功后，自动更新本次确认时当前空间中已有管理权限的受管 Agent；忙节点等待任务结束，离线节点重连后继续。</p>
    <dl class="kv"><dt>运行版本</dt><dd id="panel-update-current">${esc(S.settings?.version || '—')}</dd><dt>更新来源</dt><dd id="panel-update-repo">—</dd><dt>可用版本</dt><dd id="panel-update-target">尚未检查</dd></dl>
    <label class="panel-agent-consent"><input type="checkbox" id="panel-update-agents" checked> 面板更新成功后自动更新当前空间已授权的 Agent</label>
    <div class="actions"><button class="btn" id="panel-update-check" type="button" disabled>检查 GitHub 更新</button><button class="btn primary" id="panel-update-apply" type="button" disabled>更新面板及已授权 Agent</button><button class="btn ghost" id="panel-update-refresh" type="button">刷新状态</button><button class="btn" id="panel-update-retry" type="button" hidden>重新提交原请求</button><button class="btn" id="panel-update-reload" type="button" hidden>刷新到新版本</button></div>
    <p id="panel-update-state" role="status" aria-live="polite">正在读取宿主机更新服务状态…</p><p class="form-note" id="panel-update-note" role="status" hidden></p>
    <section id="panel-agent-rollout" hidden aria-labelledby="panel-agent-rollout-title"><h3 id="panel-agent-rollout-title">Agent 更新进度</h3><p id="panel-agent-rollout-summary" role="status"></p><ul id="panel-agent-rollout-nodes"></ul></section>
    <details id="panel-update-release" hidden><summary>发布说明与校验值</summary><p id="panel-update-digest" class="mono"></p><pre id="panel-update-notes"></pre></details>
    <details id="panel-update-history" hidden><summary>更新阶段记录</summary><pre id="panel-update-events"></pre></details>
    <details id="panel-update-setup" hidden><summary>首次启用更新服务</summary><p>需要 Linux、systemd 与 Docker Compose。先在服务器部署包含本功能的新版源码，再在该部署目录运行一次：</p><div class="code-box"><pre>sudo python3 scripts/panel_updater.py install --root "$PWD"</pre></div><p>宿主机更新服务独立运行，网页进程没有 Docker 管理权限。安装或更新面板时也可使用 <code>sudo bash install.sh --enable-panel-update</code>。未完成的更新须先在宿主机核查，不能靠重新安装服务覆盖。</p></details>
    <p class="form-note">从旧版面板首次升级没有自动更新 Agent 的授权记录，请在设备节点页按原流程确认更新。切换前保存完整数据副本。更新期间短暂进入维护状态；关闭页面不取消更新。原数据卷、镜像和更新记录不会自动删除，请定期在宿主机归档。</p></div></section>`;
  }
  function text(id, value) {
    const node = document.getElementById(id);
    if (node && node.textContent !== String(value ?? '')) node.textContent = String(value ?? '');
  }
  function paint() {
    const entry = document.querySelector('[data-panel-update-open]');
    if (entry) {
      const version = data?.running_version || S.settings?.version || loadedVersion || '—';
      entry.querySelector('[data-panel-update-version]').textContent = 'v' + version;
      const rollout = data?.agent_rollout;
      const label =
        submitting || pending || data?.busy
          ? '更新中'
          : rollout?.state === 'running'
            ? 'Agent ' + rollout.completed + '/' + rollout.total
            : rollout?.state === 'partial_failure' ||
                data?.recovery_required ||
                ['failed', 'rolled_back'].includes(data?.operation?.state)
              ? '查看更新结果'
              : data?.update_available
                ? '有新版本'
                : '检查更新';
      entry.querySelector('[data-panel-update-status]').textContent = label;
      entry.dataset.state = data?.update_available
        ? 'available'
        : rollout?.state || data?.operation?.state || 'idle';
    }
    if (!document.getElementById('panel-update')) return;
    const d = data || {},
      job = d.operation,
      busy = submitting || !!pending || !!d.busy,
      enabled = d.enabled === true;
    text('panel-update-current', d.running_version || S.settings?.version || '—');
    text('panel-update-repo', d.repository || '宿主机固定配置，网页不能更换来源');
    text(
      'panel-update-target',
      d.candidate
        ? `${d.candidate.version}${d.update_available ? ' · 可更新' : ' · 无更新版本或检查已过期'}`
        : '尚未检查',
    );
    const labels = {
      queued: '已排队',
      running: '进行中',
      succeeded: '已完成',
      failed: '失败',
      rolled_back: '已恢复旧版',
      recovery_required: '需宿主机处理',
    };
    text(
      'panel-update-badge',
      submitting
        ? '提交中'
        : job
          ? labels[job.state] || '结果待核实'
          : enabled
            ? '已启用'
            : '未连接',
    );
    text(
      'panel-update-state',
      job
        ? `${job.message || '更新处理中'}${job.id ? ' · 操作 ' + job.id : ''}`
        : d.reason ||
            (enabled ? '点击检查，读取 GitHub 最新正式发布。' : '正在读取宿主机更新服务状态…'),
    );
    text('panel-update-note', note);
    $('#panel-update-note').hidden = !note;
    $('#panel-update-check').disabled = !enabled || busy || d.recovery_required;
    $('#panel-update-apply').disabled =
      !enabled || busy || !d.update_available || d.recovery_required;
    $('#panel-update-refresh').disabled = submitting;
    $('#panel-update-agents').disabled = busy;
    if (pending?.action === 'apply')
      $('#panel-update-agents').checked = pending.body.update_agents === true;
    $('#panel-update-retry').hidden = !(pending && enabled && d.request_found === false && !d.busy);
    $('#panel-update-retry').disabled = submitting;
    $('#panel-update-reload').hidden = !(
      job?.kind === 'apply' &&
      job.state === 'succeeded' &&
      d.running_version === job.target_version
    );
    $('#panel-update-setup').hidden = enabled;
    $('#panel-update-release').hidden = !d.candidate;
    text('panel-update-digest', d.candidate ? `SHA-256: ${d.candidate.sha256}` : '');
    text('panel-update-notes', d.candidate?.notes || '此 Release 未提供说明。');
    const rollout = d.agent_rollout;
    $('#panel-agent-rollout').hidden = !rollout;
    if (rollout) {
      text(
        'panel-agent-rollout-summary',
        '目标 ' +
          rollout.target_version +
          ' · 已核对 ' +
          rollout.completed +
          '/' +
          rollout.total +
          (rollout.message ? ' · ' + rollout.message : ''),
      );
      const labels = {
        waiting_panel: '等待面板',
        offline: '离线待更新',
        waiting_idle: '等待任务结束',
        updating: '更新中',
        awaiting_restart: '等待重连',
        succeeded: '版本已核验',
        skipped: '无需更新',
        blocked: '已停止',
        failed: '更新失败',
      };
      $('#panel-agent-rollout-nodes').innerHTML = (rollout.nodes || [])
        .map(
          (node) =>
            '<li data-agent-update-state="' +
            esc(node.state) +
            '"><div><strong>' +
            esc(node.name) +
            '</strong><span class="badge">' +
            esc(labels[node.state] || node.state) +
            '</span></div><p>' +
            esc(node.message) +
            '</p><small>当前 ' +
            esc(node.version || '未知') +
            (node.operation_id ? ' · 操作 ' + esc(node.operation_id) : '') +
            '</small></li>',
        )
        .join('');
    }
    $('#panel-update-history').hidden = !job?.events?.length;
    text(
      'panel-update-events',
      (job?.events || []).map((e) => `${timeText(e.at)}  ${e.message}`).join('\n'),
    );
  }
  function dirty() {
    if (hasUnsavedChanges() || $('.modal') || $('button[aria-busy="true"]')) return true;
    return $$('form input,form textarea,form select').some((field) => {
      if (field.type === 'hidden' || field.readOnly) return false;
      if (['checkbox', 'radio'].includes(field.type)) return field.checked !== field.defaultChecked;
      if (field.tagName === 'SELECT') {
        const options = [...field.options],
          explicit = options.some((option) => option.defaultSelected);
        return options.some(
          (option, index) =>
            option.selected !== (option.defaultSelected || (!explicit && index === 0)),
        );
      }
      return field.value !== field.defaultValue;
    });
  }
  function successKey(job) {
    return (
      String(job?.id || job?.request_key || job?.target_version || '').slice(0, 128) +
      ':' +
      job?.target_version
    );
  }
  function reloadPage(job) {
    if (reloadAttempted) return;
    const url = new URL(location.href);
    url.searchParams.set(reloadParameter, successKey(job));
    reloadAttempted = true;
    location.replace(url.href);
  }
  function refreshAfterUpdate() {
    const job = data?.operation;
    if (
      !data?.enabled ||
      data.busy ||
      submitting ||
      pending ||
      data.recovery_required ||
      job?.kind !== 'apply' ||
      job.state !== 'succeeded' ||
      !/^\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(job.target_version || '')
    )
      return false;
    if (data.running_version !== job.target_version) {
      note = '更新记录已完成，正在等待目标版本恢复服务；不会提前刷新或再次提交。';
      return true;
    }
    const key = successKey(job);
    // The URL marker also prevents loops when browser storage is unavailable.
    if (
      reloadAttempted ||
      loadedVersion === job.target_version ||
      new URL(location.href).searchParams.get(reloadParameter) === key
    )
      return false;
    if (dirty()) {
      note =
        '新版已就绪。检测到未保存输入、草稿或进行中的界面操作，暂缓自动刷新；处理完后自动继续，也可点击“刷新到新版本”。';
      if (S.page !== 'settings' && deferredKey !== key) {
        deferredKey = key;
        toast(note);
      }
      return true;
    }
    note = '新版服务已恢复，正在自动刷新页面…';
    paint();
    reloadPage(job);
    return false;
  }
  function schedule(ms = 2000) {
    clearTimeout(timer);
    if (S.session && sessionOwner(S.session) === owner && S.session.instance_admin)
      timer = setTimeout(() => load(), ms);
  }
  async function load() {
    const gen = generation;
    if (!S.session || sessionOwner(S.session) !== owner || !S.session.instance_admin) return;
    clearTimeout(timer);
    timer = null;
    reader?.abort();
    const control = new AbortController();
    reader = control;
    try {
      const key = pending?.body?.idempotency_key;
      const result = await api(
        '/api/panel-update/status' + (key ? '?request_key=' + encodeURIComponent(key) : ''),
        { signal: control.signal, retryDelays: [], requestTimeout: 10000, cache: 'no-store' },
      );
      if (gen !== generation || reader !== control) return;
      const wasWatching = watching;
      data = result;
      if (result.enabled) {
        if (pending && result.request_found && terminal.has(result.operation?.state))
          savePending(null);
        note =
          pending && result.request_found === false
            ? '宿主机未找到原请求记录。核实后可使用“重新提交原请求”；不会生成第二个更新编号。'
            : '';
      } else if (pending) {
        note = '更新结果尚未核实。正在恢复连接；服务未连接不代表更新失败，请不要重新创建更新。';
      }
      const awaiting = refreshAfterUpdate();
      watching = !!(
        result.busy ||
        ['waiting_panel', 'running'].includes(result.agent_rollout?.state) ||
        pending ||
        result.recovery_required ||
        awaiting ||
        (!result.enabled && wasWatching)
      );
      paint();
      if (watching || (!result.enabled && result.code === 'UPDATER_UNAVAILABLE'))
        schedule(result.enabled ? 2000 : 4000);
      else schedule(30000);
    } catch (error) {
      if (
        gen !== generation ||
        reader !== control ||
        error.name === 'AbortError' ||
        error.code === 'SESSION_CHANGED'
      )
        return;
      note = '连接暂时中断，正在恢复更新进度。不会自动重复提交更新。';
      paint();
      schedule(4000);
    } finally {
      if (reader === control) reader = null;
    }
  }
  async function submit(action, reuse = false) {
    if (submitting || (!reuse && pending) || !data?.enabled) return;
    const gen = generation;
    if (!reuse) {
      if (action === 'apply') {
        if (!data.update_available || data.busy || data.recovery_required) return;
        const c = data.candidate;
        const updateAgents = $('#panel-update-agents')?.checked === true;
        if (
          !confirm(
            `从 ${data.repository} 更新到 ${c.version}？\n包含面板、Hub 和 Agent 分发文件。${updateAgents ? '健康检查成功后自动更新当前空间已授权受管 Agent，忙节点等待任务结束，离线节点重连后继续。' : '本次不自动更新 Agent。'}权限和配置保持原样。\n切换期间面板短暂维护，原数据卷保留为备份。`,
          )
        )
          return;
        savePending({
          action,
          body: {
            idempotency_key: 'panel-update-' + uid(),
            version: c.version,
            release_id: c.release_id,
            sha256: c.sha256,
            confirmation: c.version,
            update_agents: updateAgents,
          },
        });
      } else savePending({ action, body: { idempotency_key: 'panel-check-' + uid() } });
    }
    const request = pending;
    if (!request) return;
    submitting = true;
    watching = true;
    note = '正在提交并保存更新请求…';
    paint();
    try {
      await post('/api/panel-update/' + request.action, request.body);
      if (gen === generation) note = '请求已保存，正在读取更新进度。';
    } catch (error) {
      if (error.code === 'SESSION_CHANGED' || gen !== generation) return;
      // Only a definitive pre-execution rejection releases the request identity.
      if (error.status >= 400 && error.status < 500) {
        savePending(null);
        note = error.message;
        toast(error.message, true);
      } else if (gen === generation)
        note = '提交回执暂未收到，正在按原请求编号核实。请不要重复点击更新。';
    } finally {
      if (gen === generation) {
        submitting = false;
        paint();
        await load();
      }
    }
  }
  function detach() {
    const currentOwner = sessionOwner(S.session);
    // Page repaints must not starve a live update monitor or interrupt its POST.
    if (owner === currentOwner && S.session?.instance_admin) {
      if (!timer && !reader && !submitting) schedule(watching ? 2000 : 30000);
      return;
    }
    generation++;
    clearTimeout(timer);
    timer = null;
    reader?.abort();
    reader = null;
    submitting = false;
    if (owner !== currentOwner) {
      pending = null;
      watching = false;
      owner = currentOwner;
    }
    data = null;
    note = '';
  }
  function resume() {
    if (!S.session?.instance_admin) return;
    if (owner !== sessionOwner(S.session)) detach();
    pending = pending || restorePending();
    watching = watching || !!pending;
    paint();
    schedule(0);
  }
  function bind() {
    detach();
    pending = pending || restorePending();
    watching = watching || !!pending;
    $('#panel-update-check').onclick = () => submit('check');
    $('#panel-update-apply').onclick = () => submit('apply');
    $('#panel-update-refresh').onclick = () => load();
    $('#panel-update-retry').onclick = () => submit(pending?.action, true);
    $('#panel-update-reload').onclick = () => {
      if (!dirty() || confirm('刷新将丢弃当前页面的未保存输入，继续？'))
        reloadPage(data?.operation);
    };
    paint();
    load();
  }
  return { html, bind, detach, resume, entry, open };
})();
