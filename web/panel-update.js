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
    deferredKey = '',
    nextCheckAt = 0,
    checkFailures = 0,
    discovery = null,
    discovering = false,
    intentNote = '',
    accessDenied = false,
    statusFailed = false,
    agentConsent = true;
  const checkTTL = 5 * 60 * 1000;
  const failureDelay = () => Math.min(15 * 60 * 1000, 60000 * 2 ** Math.min(checkFailures - 1, 4));
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
        /^[A-Za-z0-9._:-]{8,128}$/.test(value.body?.idempotency_key) &&
        (!value.intent ||
          (/^\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(value.intent.version || '') &&
            Number.isInteger(value.intent.release_id) &&
            /^[a-f0-9]{64}$/i.test(value.intent.sha256 || '') &&
            typeof value.intent.update_agents === 'boolean'))
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
  function closeDetails(focus = true) {
    document.getElementById('panel-update-popover')?.remove();
    const trigger = document.querySelector('[data-panel-update-open]');
    trigger?.setAttribute('aria-expanded', 'false');
    if (focus) trigger?.focus({ preventScroll: true });
  }
  function showDetails() {
    if (!document.getElementById('panel-update-popover')) {
      const section = document.createElement('aside');
      section.id = 'panel-update-popover';
      section.className = 'panel panel-update panel-update-popover';
      section.setAttribute('aria-labelledby', 'panel-update-float-title');
      section.innerHTML = `<div class="panel-head"><h2 id="panel-update-float-title" tabindex="-1">面板更新</h2><button type="button" class="btn ghost" data-panel-update-close aria-label="关闭更新详情">关闭</button></div>
        <div class="panel-body"><p class="form-note">顶部快捷更新仅更新面板、Hub 和 Agent 分发文件，不自动更新已安装的 Agent。下方勾选仅用于详情中的再次确认更新。</p><p id="panel-update-float-state" role="status" aria-live="polite"></p><p id="panel-update-float-note" class="form-note"></p>
        <label class="panel-agent-consent"><input type="checkbox" id="panel-update-float-agents"> 面板更新成功后自动更新当前空间已授权的 Agent</label>
        <div class="actions"><button type="button" class="btn primary" id="panel-update-float-apply">更新面板及已授权 Agent</button><button type="button" class="btn" id="panel-update-float-check">检查更新</button><button type="button" class="btn ghost" id="panel-update-float-refresh">刷新状态</button><button type="button" class="btn" id="panel-update-float-retry" hidden>重新提交原请求</button><button type="button" class="btn" id="panel-update-float-reload" hidden>刷新到新版本</button></div>
        <details><summary>版本、发布说明与更新进度</summary><pre id="panel-update-float-details"></pre></details></div>`;
      document.body.append(section);
      $('#panel-update-float-apply').onclick = () => submit('apply');
      $('#panel-update-float-check').onclick = () => submit('check');
      $('#panel-update-float-refresh').onclick = () => load(true);
      $('#panel-update-float-retry').onclick = () => submit(pending?.action, true);
      $('#panel-update-float-reload').onclick = manualReload;
      $('#panel-update-float-agents').onchange = (event) => setConsent(event.target.checked);
    }
    document.querySelector('[data-panel-update-open]')?.setAttribute('aria-expanded', 'true');
    paint();
    $('#panel-update-float-title')?.focus({ preventScroll: true });
  }
  async function open() {
    if (!S.session?.instance_admin) return;
    showDetails();
    if (canApply()) {
      const c = availableCandidate();
      const intent = {
        version: c.version,
        release_id: c.release_id,
        sha256: c.sha256,
        update_agents: false,
      };
      if (hostMatches(intent)) await submit('apply', false, false, true, intent);
      else await submit('check', false, false, false, intent);
    } else if (!submitting && !pending) await load();
  }
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-panel-update-close]')) closeDetails();
    else if (event.target.closest('[data-panel-update-open]')) open();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.getElementById('panel-update-popover')) closeDetails();
  });
  function setConsent(value) {
    agentConsent = value;
    for (const id of ['panel-update-agents', 'panel-update-float-agents']) {
      const node = document.getElementById(id);
      if (node) node.checked = value;
    }
  }
  function currentAgentScope() {
    if (pending?.action === 'apply') return pending.body.update_agents === true;
    if (pending?.action === 'check' && pending.intent) return pending.intent.update_agents === true;
    return agentConsent;
  }
  function sameCandidate(a, b) {
    return (
      !!a &&
      !!b &&
      a.version === b.version &&
      a.release_id === b.release_id &&
      a.sha256 === b.sha256
    );
  }
  function availableCandidate() {
    if (discovery) return discovery.available && !discovery.error ? discovery.candidate : null;
    return data?.update_available ? data.candidate : null;
  }
  function hostMatches(intent) {
    const age = Date.now() - Number(data?.candidate?.checked_at) * 1000;
    return (
      data?.update_available && age >= 0 && age < 900000 && sameCandidate(intent, data.candidate)
    );
  }
  function awaitingVersion() {
    const job = data?.operation;
    return (
      job?.kind === 'apply' &&
      job.state === 'succeeded' &&
      /^\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(job.target_version || '') &&
      data.running_version !== job.target_version
    );
  }
  function canApply() {
    return (
      data?.enabled &&
      availableCandidate() &&
      !awaitingVersion() &&
      !data.busy &&
      !data.recovery_required &&
      !submitting &&
      !pending &&
      !discovering &&
      !accessDenied &&
      !statusFailed
    );
  }
  function entryStatus() {
    if (data?.recovery_required) return ['recovery_required', '需恢复'];
    if (awaitingVersion()) return ['verifying', '等待服务恢复'];
    if (discovering) return ['checking', '检查中'];
    if (pending || submitting || data?.busy)
      return (pending?.action || data?.operation?.kind) === 'check'
        ? ['checking', '检查中']
        : ['updating', '更新中'];
    if (accessDenied || statusFailed) return ['unknown', '状态待核实'];
    if (data?.enabled === false)
      return ['unknown', data.code === 'UPDATER_NOT_CONFIGURED' ? '未启用' : '连接待恢复'];
    const candidate = availableCandidate();
    if (candidate) return ['available', '更新面板至 v' + candidate.version];
    if (discovery?.error) return ['failed', '检查失败'];
    if (data?.operation?.kind === 'check' && data.operation.state === 'failed')
      return ['failed', '检查失败'];
    if (data?.agent_rollout?.state === 'running')
      return ['updating', 'Agent ' + data.agent_rollout.completed + '/' + data.agent_rollout.total];
    if (
      ['failed', 'rolled_back'].includes(data?.operation?.state) ||
      data?.agent_rollout?.state === 'partial_failure'
    )
      return ['failed', '查看更新结果'];
    const checked = Number(discovery?.checked_at || data?.candidate?.checked_at) * 1000;
    if (
      data?.enabled &&
      !discovery?.error &&
      checked > 0 &&
      Date.now() - checked >= 0 &&
      Date.now() - checked < checkTTL
    )
      return ['latest', '已是最新'];
    return ['unknown', '尚未检查'];
  }
  function manualReload() {
    if (!dirty() || confirm('刷新将丢弃当前页面的未保存输入，继续？')) reloadPage(data?.operation);
  }
  function paintDetails() {
    if (!document.getElementById('panel-update-popover')) return;
    const d = data || {},
      job = d.operation,
      busy = submitting || !!pending || !!d.busy;
    text(
      'panel-update-float-state',
      d.host_engine?.activating
        ? 'Hub 已提交，正在切换已验证的宿主更新器；请等待就绪回执。'
        : job?.message || d.reason || entryStatus()[1],
    );
    text('panel-update-float-note', note || d.host_update_notice || '');
    $('#panel-update-float-note').hidden = !(note || d.host_update_notice);
    $('#panel-update-float-apply').disabled = !canApply();
    $('#panel-update-float-check').disabled =
      !d.enabled || busy || d.recovery_required || accessDenied || awaitingVersion();
    $('#panel-update-float-refresh').disabled = submitting;
    $('#panel-update-float-agents').disabled = busy;
    $('#panel-update-float-agents').checked = currentAgentScope();
    $('#panel-update-float-retry').hidden = !(
      pending &&
      d.enabled &&
      d.request_found === false &&
      !d.busy &&
      !d.recovery_required &&
      !accessDenied
    );
    $('#panel-update-float-retry').disabled = submitting;
    $('#panel-update-float-reload').hidden = !(
      job?.kind === 'apply' &&
      job.state === 'succeeded' &&
      d.running_version === job.target_version
    );
    const rollout = d.agent_rollout,
      candidate = availableCandidate() || d.candidate;
    text(
      'panel-update-float-details',
      [
        '运行版本：' + (d.running_version || loadedVersion || '未知'),
        '更新来源：' + (d.repository || '未连接'),
        '候选版本：' + (candidate?.version || '尚未检查'),
        candidate ? 'SHA-256: ' + candidate.sha256 : '',
        candidate?.notes || '',
        job?.id ? '操作：' + job.id : '',
        ...(job?.events || []).map((e) => timeText(e.at) + '  ' + e.message),
        rollout
          ? 'Agent 更新：' +
            rollout.completed +
            '/' +
            rollout.total +
            ' · ' +
            (rollout.message || rollout.state)
          : '',
        ...(rollout?.nodes || []).map((n) => n.name + ' · ' + n.state + ' · ' + n.message),
      ]
        .filter(Boolean)
        .join('\n'),
    );
  }
  function html() {
    return `<section class="panel panel-update" id="panel-update" aria-labelledby="panel-update-title">
    <div class="panel-head"><h2 id="panel-update-title" tabindex="-1">${icon('refresh')}面板更新</h2><span class="badge" id="panel-update-badge">读取中</span></div>
    <div class="panel-body"><p>从 GitHub 正式 Release 更新网页、Hub、Agent 安装包及安装脚本。账号、配对、项目和配置保留。面板健康检查成功后，自动更新本次确认时当前空间中已有管理权限的受管 Agent；忙节点等待任务结束，离线节点重连后继续。</p>
    <dl class="kv"><dt>运行版本</dt><dd id="panel-update-current">${esc(S.settings?.version || '—')}</dd><dt>更新来源</dt><dd id="panel-update-repo">—</dd><dt>可用版本</dt><dd id="panel-update-target">尚未检查</dd></dl>
    <label class="panel-agent-consent"><input type="checkbox" id="panel-update-agents" checked> 面板更新成功后自动更新当前空间已授权的 Agent</label>
    <div class="actions"><button class="btn" id="panel-update-check" type="button" disabled>检查 GitHub 更新</button><button class="btn primary" id="panel-update-apply" type="button" disabled>更新面板及已授权 Agent</button><button class="btn ghost" id="panel-update-refresh" type="button">刷新状态</button><button class="btn" id="panel-update-retry" type="button" hidden>重新提交原请求</button><button class="btn" id="panel-update-reload" type="button" hidden>刷新到新版本</button></div>
    <p id="panel-update-host-notice" class="form-note" role="status" hidden></p><p id="panel-update-state" role="status" aria-live="polite">正在读取宿主机更新服务状态…</p><p class="form-note" id="panel-update-note" role="status" hidden></p>
    <section id="panel-agent-rollout" hidden aria-labelledby="panel-agent-rollout-title"><h3 id="panel-agent-rollout-title">Agent 更新进度</h3><p id="panel-agent-rollout-summary" role="status"></p><ul id="panel-agent-rollout-nodes"></ul></section>
    <details id="panel-update-release" hidden><summary>发布说明与校验值</summary><p id="panel-update-digest" class="mono"></p><pre id="panel-update-notes"></pre></details>
    <details id="panel-update-history" hidden><summary>更新阶段记录</summary><pre id="panel-update-events"></pre></details>
    <details id="panel-update-setup" hidden><summary>首次启用更新服务</summary><p>需要 Linux、systemd 与 Docker Compose。先在服务器部署包含本功能的新版源码，再在该部署目录运行一次：</p><div class="code-box"><pre>sudo python3 scripts/panel_updater.py install --root "$PWD"</pre></div><p>宿主机更新服务独立运行，网页进程没有 Docker 管理权限。安装或更新面板时也可使用 <code>sudo bash install.sh --enable-panel-update</code>。未完成的更新须先在宿主机核查，不能靠重新安装服务覆盖。</p></details>
    <p class="form-note">从旧版面板首次升级没有自动更新 Agent 的授权记录，请在设备节点页按原流程确认更新。具备新版宿主能力时默认分离数据库快照与附件原件；旧宿主仍按完整卷复制。更新期间短暂进入维护状态；关闭页面不取消更新。原数据卷、镜像和更新记录不会自动删除，请定期在宿主机归档。</p></div></section>`;
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
      const [state, label] = entryStatus();
      entry.querySelector('[data-panel-update-status]').textContent = label;
      entry.dataset.state = state;
      entry.title =
        state === 'available'
          ? '立即更新面板、Hub 和 Agent 分发文件；不自动更新已安装的 Agent。'
          : '查看更新状态与进度';
      entry.setAttribute(
        'aria-label',
        state === 'available'
          ? label + '：更新面板、Hub 和 Agent 分发文件，不自动更新已安装的 Agent'
          : '面板版本与更新：' + label,
      );
      entry.setAttribute(
        'aria-expanded',
        String(!!document.getElementById('panel-update-popover')),
      );
    }
    paintDetails();
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
      d.host_engine?.activating
        ? 'Hub 已提交，正在切换已验证的宿主更新器；请等待就绪回执。'
        : job
          ? `${job.message || '更新处理中'}${job.id ? ' · 操作 ' + job.id : ''}`
          : d.reason ||
            (enabled ? '点击检查，读取 GitHub 最新正式发布。' : '正在读取宿主机更新服务状态…'),
    );
    text('panel-update-host-notice', d.host_update_notice || '');
    $('#panel-update-host-notice').hidden = !d.host_setup_required;
    text('panel-update-note', note);
    $('#panel-update-note').hidden = !note;
    $('#panel-update-check').disabled =
      !enabled || busy || d.recovery_required || accessDenied || awaitingVersion();
    $('#panel-update-apply').disabled = !canApply();
    $('#panel-update-refresh').disabled = submitting;
    $('#panel-update-agents').disabled = busy;
    $('#panel-update-agents').checked = currentAgentScope();
    $('#panel-update-retry').hidden = !(
      pending &&
      enabled &&
      d.request_found === false &&
      !d.busy &&
      !d.recovery_required &&
      !accessDenied
    );
    $('#panel-update-retry').disabled = submitting;
    $('#panel-update-reload').hidden = !(
      job?.kind === 'apply' &&
      job.state === 'succeeded' &&
      d.running_version === job.target_version
    );
    $('#panel-update-setup').hidden = enabled && !d.host_setup_required;
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
    if (!accessDenied && S.session && sessionOwner(S.session) === owner && S.session.instance_admin)
      timer = setTimeout(() => load(), ms);
  }
  async function load(manual = false) {
    if (manual) {
      accessDenied = false;
      nextCheckAt = 0;
    }
    const gen = generation;
    if (!S.session || sessionOwner(S.session) !== owner || !S.session.instance_admin) return;
    if (reader || submitting || accessDenied) return;
    clearTimeout(timer);
    timer = null;
    let continueIntent = null;
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
      statusFailed = false;
      if (result.enabled) {
        if (
          key &&
          pending?.body?.idempotency_key === key &&
          result.request_found &&
          terminal.has(result.operation?.state)
        ) {
          if (pending.action === 'check' && result.operation.state === 'succeeded')
            discovery = null;
          if (pending.action === 'check' && pending.intent) {
            if (
              result.operation.kind === 'check' &&
              result.operation.state === 'succeeded' &&
              hostMatches(pending.intent)
            )
              continueIntent = pending.intent;
            else
              intentNote =
                result.operation.state === 'succeeded'
                  ? '发布版本或校验值已变化，未自动更新。请核对新版本后重新点击。'
                  : '版本核查未成功，已停止本次更新。';
          }
          savePending(null);
        }
        note =
          pending && result.request_found === false
            ? '宿主机未找到原请求记录。核实后可使用“重新提交原请求”；不会生成第二个更新编号。'
            : intentNote;
      } else if (pending) {
        note = '更新结果尚未核实。正在恢复连接；服务未连接不代表更新失败，请不要重新创建更新。';
      }
      if (!continueIntent && shouldDiscover()) {
        nextCheckAt = Date.now() + checkTTL;
        discovering = true;
        paint();
        try {
          const available = await api('/api/panel-update/available', {
            signal: control.signal,
            retryDelays: [],
            requestTimeout: 15000,
            cache: 'no-store',
          });
          if (gen !== generation || reader !== control) return;
          // A user may start an update while discovery is in flight. Never replace that receipt.
          if (!pending && !submitting) {
            discovery = {
              candidate: available.discovery_candidate || null,
              available: available.discovery_available === true,
              checked_at: available.discovery_checked_at,
              error: available.discovery_error || null,
            };
            if (discovery.error) {
              checkFailures++;
              nextCheckAt = Date.now() + failureDelay();
              note = discovery.error.message || '检查正式发布失败，请稍后重试。';
            } else checkFailures = 0;
          }
        } catch (error) {
          if (
            gen !== generation ||
            reader !== control ||
            error.name === 'AbortError' ||
            error.code === 'SESSION_CHANGED'
          )
            return;
          if (!pending && !submitting) {
            discovery = { candidate: null, available: false, error: { message: error.message } };
            checkFailures++;
            nextCheckAt = Date.now() + failureDelay();
            accessDenied = [401, 403, 404].includes(error.status);
            note = accessDenied
              ? '更新检查不可用，已停止自动检查，可手动刷新状态。'
              : '检查正式发布失败，请稍后重试。';
          }
        } finally {
          if (gen === generation) discovering = false;
        }
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
      statusFailed = true;
      accessDenied = [401, 403, 404].includes(error.status);
      note = accessDenied
        ? '当前会话无更新权限或服务不支持更新，已停止自动检查，可手动刷新状态。'
        : '连接暂时中断，正在恢复更新进度。不会自动重复提交更新。';
      paint();
      schedule(4000);
    } finally {
      if (reader === control) reader = null;
    }
    if (
      continueIntent &&
      gen === generation &&
      !pending &&
      !submitting &&
      hostMatches(continueIntent)
    )
      await submit('apply', false, false, true, continueIntent);
  }
  function shouldDiscover() {
    if (
      awaitingVersion() ||
      document.hidden ||
      !data?.enabled ||
      data.busy ||
      data.recovery_required ||
      pending ||
      submitting ||
      accessDenied
    )
      return false;
    const checked = Number(discovery?.checked_at || data.candidate?.checked_at) * 1000;
    const fresh = checked > 0 && Date.now() >= checked && Date.now() - checked < checkTTL;
    return !fresh && Date.now() >= nextCheckAt;
  }
  async function submit(action, reuse = false, automatic = false, direct = false, intent = null) {
    if (
      awaitingVersion() ||
      !S.session?.instance_admin ||
      accessDenied ||
      submitting ||
      (!reuse && pending) ||
      !data?.enabled ||
      data.busy ||
      data.recovery_required
    )
      return;
    const gen = generation;
    if (!reuse) {
      intentNote = '';
      if (action === 'apply') {
        if (intent ? !hostMatches(intent) : !canApply()) return;
        const c = intent || availableCandidate();
        if (!c) return;
        const updateAgents = direct ? intent?.update_agents === true : agentConsent;
        if (
          !direct &&
          !confirm(
            `从 ${data.repository} 更新到 ${c.version}？\n包含面板、Hub 和 Agent 分发文件。${updateAgents ? '健康检查成功后自动更新当前空间已授权受管 Agent，忙节点等待任务结束，离线节点重连后继续。' : '本次不自动更新 Agent。'}权限和配置保持原样。\n切换期间面板短暂维护，原数据卷保留为备份。`,
          )
        )
          return;
        if (!hostMatches(c)) {
          await submit('check', false, false, false, {
            version: c.version,
            release_id: c.release_id,
            sha256: c.sha256,
            update_agents: updateAgents,
          });
          return;
        }
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
      } else
        savePending({
          action,
          body: { idempotency_key: 'panel-check-' + uid() },
          ...(intent ? { intent } : {}),
        });
    }
    if (action === 'check') nextCheckAt = Date.now() + checkTTL;
    clearTimeout(timer);
    timer = null;
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
        if (action === 'check') {
          checkFailures++;
          nextCheckAt = Date.now() + failureDelay();
        }
        if ([401, 403, 404].includes(error.status)) accessDenied = true;
        if (!automatic) toast(error.message, true);
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
  function detach(force = false) {
    // Session teardown runs before S.session is cleared; it must bypass the repaint fast path.
    const currentOwner = force ? null : sessionOwner(S.session);
    // Page repaints must not starve a live update monitor or interrupt its POST.
    if (!force && owner === currentOwner && S.session?.instance_admin) {
      if (!timer && !reader && !submitting) schedule(watching ? 2000 : 30000);
      return;
    }
    generation++;
    closeDetails(false);
    nextCheckAt = 0;
    checkFailures = 0;
    discovery = null;
    discovering = false;
    intentNote = '';
    accessDenied = false;
    statusFailed = false;
    agentConsent = true;
    clearTimeout(timer);
    timer = null;
    reader?.abort();
    reader = null;
    submitting = false;
    if (force || owner !== currentOwner) {
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
    $('#panel-update-agents').onchange = (event) => setConsent(event.target.checked);
    $('#panel-update-check').onclick = () => submit('check');
    $('#panel-update-apply').onclick = () => submit('apply');
    $('#panel-update-refresh').onclick = () => load(true);
    $('#panel-update-retry').onclick = () => submit(pending?.action, true);
    $('#panel-update-reload').onclick = manualReload;
    paint();
    load();
  }
  // Navigation cancels browser reads, not a durable update request. Invalidate
  // callbacks before WebKit tears down an in-flight status fetch during reload.
  window.addEventListener('pagehide', () => {
    generation++;
    clearTimeout(timer);
    timer = null;
    reader?.abort();
    reader = null;
    submitting = false;
  });
  // A back-forward cached document survives pagehide and must resume its
  // original idempotency-key watch instead of submitting a second update.
  window.addEventListener('pageshow', (event) => {
    if (event.persisted && S.session?.instance_admin) resume();
  });
  return { html, bind, detach, resume, entry, open };
})();
