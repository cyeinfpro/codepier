'use strict';
// Details exist only in this authenticated tab's memory; never persist
// operation data in Web Storage or IndexedDB. The shared tab-memory
// window.CodePierOperationStore is defined once, in queue.js (loaded first);
// every use below is optional so this component also works standalone.

window.CodePierCallLog = (() => {
  let timer = null,
    tickTimer = null,
    searchTimer = null,
    hoverTimer = null,
    progressTimer = null,
    sseThrottleTimer = null,
    sseLastRun = 0,
    root = null,
    inflight = null,
    listController = null,
    epoch = 0,
    ownerSession = null,
    ownerSpace = null,
    detailTurn = 0,
    consecutiveFailures = 0,
    lastSyncedTime = '',
    initialQueueData = null,
    pendingFocusId = '',
    refreshQueued = false,
    unsubscribeStore = null,
    keyListener = null,
    visListener = null,
    wideMql = null,
    wideMqlHandler = null,
    resizeListener = null,
    lastAppliedHash = null,
    focusController = null,
    focusTurn = 0,
    retryAt = 0,
    retryTicker = null;
  const cache = new Map(),
    detailInflight = new Map(),
    opened = new Set(),
    expandedBursts = new Set(),
    detailOutputTimers = new Map(),
    projectHues = new Map(),
    projectIds = new Map();

  const WAIT_REASONS = {
    resource_conflict: '资源冲突',
    hub_delivery: '等待投递',
    agent_pending: '等待执行',
    lane_capacity: '通道排队',
    project_limit: '项目配额',
    remote_target_limit: '远端并发上限',
    resource_pressure: '资源压力',
    project_fairness: '公平轮转',
    waiting_resource: '等待资源锁',
    waiting_worker: '等待执行槽',
    waiting_project: '等待项目锁',
  };
  const PRESET_DEFS = [
    ['', '全部'],
    ['errors', '异常与失败'],
    ['active', '进行中'],
    ['slow', '慢调用 ≥5s'],
    ['mutations', '写入与命令'],
  ];
  const OBVIOUS_TOOLS = new Set([
    'exec',
    'shell_exec',
    'ssh_exec',
    'vps_exec',
    'read',
    'fs_read',
    'fs_read_many',
    'write',
    'fs_write',
    'edit',
    'fs_edit',
    'apply_patch',
    'fs_apply',
    'search',
    'fs_search',
  ]);
  const BURST_READ_TOOLS = new Set([
    'read',
    'fs_read',
    'fs_read_many',
    'fs_tree',
    'tree',
    'file',
    'search',
    'fs_search',
    'symbols',
    'project',
    'project_context',
    'open_workspace',
    'changes',
    'history',
    'whoami',
    'targets',
  ]);

  const st = () =>
    S.callLog ||
    (S.callLog = {
      cursor: '',
      history: [],
      next: null,
      total: null,
      totalCapped: false,
      project: '',
      tool: '',
      triggerSource: '',
      preset: '',
      density: 'table',
      burst: true,
      live: true,
      rows: [],
      projects: [],
      tools: [],
      incoming: false,
      incomingCount: 0,
      incomingActive: 0,
      pinnedOp: null,
      inspectedId: '',
    });

  const active = (r) => Array.isArray(ACTIVE_STATES) && ACTIVE_STATES.includes(r.state);
  const objects = (value) =>
    Array.isArray(value) ? value.filter((x) => x && typeof x === 'object') : [];
  const duration = (n) =>
    Number.isFinite(n)
      ? n < 1000
        ? Math.round(n) + ' 毫秒'
        : (n / 1000).toFixed(n < 10000 ? 2 : 1) + ' 秒'
      : '未记录';
  const durationTone = (n) =>
    !Number.isFinite(n) ? '' : n >= 10000 ? ' call-dur-slow' : n >= 1000 ? ' call-dur-mid' : '';
  const pad2 = (n) => String(n).padStart(2, '0');
  const pad3 = (n) => String(n).padStart(3, '0');
  const stamp = (n) =>
    n
      ? new Date(n * 1000).toLocaleString('zh-CN', { hour12: false }) +
        '.' +
        pad3(Math.floor(n * 1000) % 1000)
      : '未记录';
  function timeShort(n) {
    if (!n) return '未记录';
    const d = new Date(n * 1000);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }
  function dayKey(n) {
    if (!n) return '';
    const d = new Date(n * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  }
  function dayHeading(keyStr) {
    if (!keyStr) return '日期未记录';
    const now = new Date();
    const today = `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
    const yestDate = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    const yesterday = `${yestDate.getFullYear()}-${pad2(yestDate.getMonth() + 1)}-${pad2(yestDate.getDate())}`;
    if (keyStr === today) return `今天 · ${keyStr}`;
    if (keyStr === yesterday) return `昨天 · ${keyStr}`;
    return keyStr;
  }
  const source = (r) =>
    r.actor?.startsWith('mcp:') ? 'MCP' : r.actor?.startsWith('panel:') ? '面板' : '其他';
  const trigger = (r) =>
    r.trigger_source === 'system_maintenance'
      ? '自动维护'
      : r.trigger_source === 'request'
        ? '请求发起'
        : '触发来源未记录';
  const isKnowledge = (r) => r.tool === 'project_knowledge_build';
  const title = (r) => (isKnowledge(r) ? '项目地图更新' : r.tool);
  const modelLabel = (r) =>
    isKnowledge(r) && r.model_invocation === false ? '<span class="call-kind">无需模型</span>' : '';

  function triggerChipHTML(r) {
    // AUD-11: Suppress zero-entropy 'request' badge on collapsed rows; keep non-default origins clear.
    if (r.trigger_source === 'request') return '';
    if (r.trigger_source === 'system_maintenance')
      return '<span class="call-kind call-kind-maint">⚙ 自动维护</span>';
    if (isKnowledge(r) || r.trigger_source === 'unknown')
      return '<span class="call-kind">触发来源未记录</span>';
    return '';
  }

  function originFacts(d) {
    const reason =
      d.trigger_reason === 'automatic_refresh'
        ? '自动刷新（未区分定时或写入后触发）'
        : d.trigger_reason || '未记录';
    const mode = { incremental: '增量', full: '全量' }[d.knowledge_mode] || '未记录';
    const stats = Object.entries({
      entries_count: '条目数',
      changed_entries: '变化条目',
      reused_entries: '复用条目',
      skipped_files: '跳过文件',
      metadata_reused: '元数据复用',
    })
      .filter(([key]) => Number.isInteger(d.knowledge_stats?.[key]) && d.knowledge_stats[key] >= 0)
      .map(([key, label]) => `<div><dt>${label}</dt><dd>${d.knowledge_stats[key]}</dd></div>`)
      .join('');
    return `<div><dt>触发来源</dt><dd>${esc(trigger(d))}</dd></div><div><dt>触发原因</dt><dd>${esc(reason)}</dd></div><div><dt>原始工具</dt><dd><code>${esc(d.tool)}</code></dd></div>${isKnowledge(d) ? `<div><dt>执行方式</dt><dd>${d.model_invocation === false ? '本地程序 · 无模型调用' : '未记录'}</dd></div><div><dt>地图更新模式（执行回执）</dt><dd>${esc(mode)}</dd></div>${stats}` : ''}`;
  }

  function authorization(r) {
    const actor = typeof r.actor === 'string' ? r.actor : '';
    if (!actor.startsWith('mcp:'))
      return {
        label: actor.startsWith('panel:')
          ? '面板操作（不使用 MCP 授权）'
          : '授权未知（未记录 MCP 身份）',
        id: '',
      };
    const [id, ...label] = actor.slice(4).split(':');
    return { label: label.join(':') || '名称未记录', id };
  }
  function shortActorLabel(r) {
    const grant = authorization(r);
    if (r.actor?.startsWith('mcp:')) return grant.label || 'MCP';
    if (r.actor?.startsWith('panel:')) return '面板';
    return source(r);
  }
  function authorizationHTML(r, full = false) {
    const grant = authorization(r);
    if (!r.actor?.startsWith('mcp:')) return esc(grant.label);
    return `授权：${esc(grant.label)} · ${grant.id ? '<code title="' + esc(grant.id) + '">' + esc(full ? grant.id : grant.id.slice(0, 12)) + '</code>' : '编号未知'}`;
  }
  const group = (r) =>
    isKnowledge(r)
      ? ['other', '项目地图']
      : /^(fs_read|read|fs_tree|tree|file|fs_search|search|symbols)/.test(r.tool)
        ? ['read', '读取']
        : /^(fs_write|write)/.test(r.tool)
          ? ['write', '写入']
          : /^(fs_edit|edit|fs_apply|apply)/.test(r.tool)
            ? ['edit', '编辑']
            : /^(shell_|ssh_|exec$|vps_exec|tasks_run|validation_run|bash)/.test(r.tool)
              ? ['shell', '命令']
              : ['other', '工具'];

  function parseSummaryFallback(r) {
    if (r.summary_parts && typeof r.summary_parts === 'object') return r.summary_parts;
    const raw = typeof r.summary === 'string' ? r.summary.trim() : '';
    if (!raw) {
      if (/^(open_workspace|project|whoami|targets|get_profile)$/.test(r.tool || ''))
        return { kind: 'empty', primary: '', dir: '', name: '', badges: [] };
      return { kind: 'empty', primary: '', dir: '', name: '', badges: [] };
    }
    const badges = [];
    let rest = raw;
    rest = rest.replace(/\s*\bcwd=\.(?=\s|$)/g, '');
    rest = rest.replace(/\s*\btimeout_seconds=(?:3600|30)(?=\s|$)/g, '');
    const inChars = rest.match(/\binputChars=(\d+)/);
    if (inChars) {
      const n = Number(inChars[1]);
      badges.push(n < 1024 ? `${n} 字符` : `${(n / 1024).toFixed(1)} KB`);
      rest = rest.replace(/\s*\binputChars=\d+/, '');
    }
    const cmdChars = rest.match(/\bcommandChars=(\d+)/);
    if (cmdChars) {
      const n = Number(cmdChars[1]);
      badges.push(n < 1024 ? `${n} 字符` : `${(n / 1024).toFixed(1)} KB`);
      rest = rest.replace(/\s*\bcommandChars=\d+/, '');
    }
    const editCount = rest.match(/\bedits=(\d+\+?)/);
    if (editCount) {
      badges.push(`${editCount[1]} 处修改`);
      rest = rest.replace(/\s*\bedits=\d+\+?/, '');
    }
    rest = rest.trim();
    if (rest.startsWith('command=')) {
      return { kind: 'command', primary: rest.slice(8).trim(), dir: '', name: '', badges };
    }
    if (rest.startsWith('path=')) {
      const p = rest.slice(5).trim();
      const slash = p.lastIndexOf('/');
      return {
        kind: 'file',
        primary: p,
        dir: slash >= 0 ? p.slice(0, slash + 1) : '',
        name: slash >= 0 ? p.slice(slash + 1) : p,
        badges,
      };
    }
    return { kind: 'generic', primary: rest, dir: '', name: '', badges };
  }

  function summaryHTML(r) {
    const parts = parseSummaryFallback(r);
    if (parts.kind === 'empty' && !parts.primary && (!parts.badges || !parts.badges.length))
      return '<span class="call-args is-empty"><span class="call-args-empty-dash" aria-hidden="true">—</span></span>';
    const badgesMarkup = (parts.badges || [])
      .map((b) => `<span class="call-arg-badge">${esc(b)}</span>`)
      .join('');
    let bodyMarkup = '';
    if (parts.kind === 'file' && (parts.dir || parts.name)) {
      bodyMarkup = `<code class="call-preview call-preview-file">${parts.dir ? `<span class="call-path-dir">${esc(parts.dir)}</span>` : ''}<strong class="call-path-name">${esc(parts.name)}</strong></code>`;
    } else if (parts.kind === 'search') {
      bodyMarkup = `<code class="call-preview">🔍 ${esc(parts.primary)}</code>`;
    } else {
      bodyMarkup = `<code class="call-preview">${esc(parts.primary || r.summary || '')}</code>`;
    }
    return `<span class="call-args">${bodyMarkup}${badgesMarkup ? `<span class="call-arg-badges">${badgesMarkup}</span>` : ''}</span>`;
  }

  function contentionPillHTML(r) {
    if (r.state !== 'queued' || !r.waiting) return '';
    const w = r.waiting;
    const reasonText = WAIT_REASONS[w.reason] || w.reason || '排队中';
    const blocker = objects(w.blocked_by).find((b) => typeof b.operation_id === 'string');
    if (blocker) {
      return `<button type="button" class="call-blocker-pill" data-call-jump="${esc(blocker.operation_id)}" title="点击定位根阻塞操作 #${esc(blocker.operation_id)}">⏳ ${esc(reasonText)} · 等待 #${esc(blocker.operation_id.slice(0, 8))} ${esc(blocker.tool || '')}</button>`;
    }
    return `<span class="call-blocker-pill is-passive">⏳ ${esc(reasonText)}</span>`;
  }

  function inlineErrorHTML(r) {
    if (
      !['failed', 'interrupted'].includes(r.state) &&
      (r.exit_code === null || r.exit_code === undefined || r.exit_code === 0)
    )
      return '';
    const errText =
      typeof r.error === 'string'
        ? r.error.split('\n')[0].trim().slice(0, 140)
        : r.error
          ? json(r.error).split('\n')[0].slice(0, 140)
          : '';
    const pieces = [];
    if (r.exit_code !== null && r.exit_code !== undefined)
      pieces.push(`退出码 ${esc(r.exit_code)}`);
    if (errText) pieces.push(esc(errText));
    if (!pieces.length) return '';
    return `<span class="call-inline-error">${pieces.join(' · ')}</span>`;
  }

  function status(r) {
    const liveMs = active(r)
      ? window.CodePierOperationStore
        ? window.CodePierOperationStore.liveElapsedMs(r.created, r.elapsed_ms)
        : r.elapsed_ms
      : r.elapsed_ms;
    const liveAttr =
      active(r) && Number.isFinite(r.created) ? ` data-live-created="${esc(r.created)}"` : '';
    const prefix = active(r) ? '已历时' : '总历时';
    return `<span class="call-state-badge">${badge(r.state)}</span><span class="call-duration${durationTone(liveMs)}"${liveAttr}><span class="call-dur-prefix">${prefix} </span>${duration(liveMs)}</span>${r.exit_code !== null && r.exit_code !== undefined ? `<span class="call-exit">退出码 ${esc(r.exit_code)}</span>` : ''}`;
  }

  const params = () => {
    const p = new URLSearchParams({
      category: 'tools',
      limit: '40',
      cursor: st().cursor,
      q: S.auditQuery || '',
      source: S.auditSource || '',
      trigger_source: st().triggerSource || '',
      status: S.auditStatus || '',
      project: st().project,
      tool: st().tool,
    });
    if (st().preset) p.set('preset', st().preset);
    return p;
  };
  const key = () => params().toString();

  function applyHashParams() {
    if (typeof location === 'undefined') return;
    const currentHash = location.hash || '';
    if (currentHash === lastAppliedHash) return;
    lastAppliedHash = currentHash;
    const rawHash = currentHash.slice(1);
    if (!rawHash.startsWith('audit')) return;
    const qIdx = rawHash.indexOf('?');
    const hp = new URLSearchParams(qIdx >= 0 ? rawHash.slice(qIdx + 1) : '');
    st().project = hp.has('project') ? hp.get('project').slice(0, 100) : '';
    st().tool = hp.has('tool') ? hp.get('tool').slice(0, 100) : '';
    S.auditStatus =
      hp.has('status') && [...ACTIVE_STATES, ...TERMINAL_STATES, ''].includes(hp.get('status'))
        ? hp.get('status')
        : '';
    st().preset =
      hp.has('preset') && PRESET_DEFS.some(([k]) => k === hp.get('preset')) ? hp.get('preset') : '';
    S.auditSource =
      hp.has('source') && ['', 'mcp', 'panel'].includes(hp.get('source')) ? hp.get('source') : '';
    st().triggerSource =
      hp.has('trigger') &&
      ['', 'system_maintenance', 'request', 'unknown'].includes(hp.get('trigger'))
        ? hp.get('trigger')
        : '';
    st().density =
      hp.has('density') && ['cards', 'table'].includes(hp.get('density'))
        ? hp.get('density')
        : 'table';
    st().burst = hp.has('burst') ? hp.get('burst') !== '0' : true;
    if (hp.has('op') && /^[a-zA-Z0-9_-]{1,64}$/.test(hp.get('op'))) pendingFocusId = hp.get('op');
  }

  function syncUrlHash() {
    if (typeof history === 'undefined' || !history.replaceState || S.page !== 'audit') return;
    if (typeof location !== 'undefined' && location.protocol === 'about:') return;
    // Security Red Line: never write free-text search queries or arguments into URL hash.
    const hp = new URLSearchParams();
    if (st().project) hp.set('project', st().project);
    if (st().tool) hp.set('tool', st().tool);
    if (S.auditStatus) hp.set('status', S.auditStatus);
    if (st().preset) hp.set('preset', st().preset);
    if (S.auditSource) hp.set('source', S.auditSource);
    if (st().triggerSource) hp.set('trigger', st().triggerSource);
    if (st().density === 'cards') hp.set('density', 'cards');
    if (!st().burst) hp.set('burst', '0');
    const suffix = hp.toString();
    const targetHash = '#audit' + (suffix ? '?' + suffix : '');
    if (location.hash !== targetHash) {
      try {
        history.replaceState(null, '', location.pathname + location.search + targetHash);
        lastAppliedHash = location.hash || targetHash;
      } catch {
        // Sandboxed about:blank harnesses may disallow replaceState.
      }
    } else {
      lastAppliedHash = targetHash;
    }
  }

  function ensureOwner() {
    const nextSpace = S.space_id ?? '';
    if (ownerSession !== S.session || ownerSpace !== nextSpace) {
      detach();
      cache.clear();
      opened.clear();
      expandedBursts.clear();
      projectHues.clear();
      projectIds.clear();
      window.CodePierOperationStore?.reset();
      lastAppliedHash = null;
      S.callLog = null;
      ownerSession = S.session;
      ownerSpace = nextSpace;
    }
  }

  function abortAllDetailInflight({ prefetchOnly = false } = {}) {
    for (const [id, req] of [...detailInflight.entries()]) {
      if (prefetchOnly && !req.prefetch) continue;
      detailInflight.delete(id);
      req.controller.abort();
    }
  }

  function abortDetailIfUnused(id) {
    if (!id || opened.has(id) || st().inspectedId === id || st().pinnedOp?.id === id) return;
    const req = detailInflight.get(id);
    if (req) {
      detailInflight.delete(id);
      req.controller.abort();
    }
  }

  function detach() {
    window.CodePierQueue?.detach();
    cancelFocus();
    clearInterval(retryTicker);
    retryTicker = null;
    clearTimeout(timer);
    clearInterval(tickTimer);
    clearTimeout(searchTimer);
    clearTimeout(hoverTimer);
    clearTimeout(progressTimer);
    clearTimeout(sseThrottleTimer);
    for (const t of detailOutputTimers.values()) clearTimeout(t);
    detailOutputTimers.clear();
    abortAllDetailInflight();
    timer = tickTimer = searchTimer = hoverTimer = progressTimer = sseThrottleTimer = null;
    epoch++;
    inflight?.abort();
    inflight = null;
    listController?.abort();
    listController = null;
    unsubscribeStore?.();
    unsubscribeStore = null;
    if (keyListener) {
      document.removeEventListener('keydown', keyListener);
      keyListener = null;
    }
    if (visListener) {
      document.removeEventListener('visibilitychange', visListener);
      visListener = null;
    }
    if (wideMql && wideMqlHandler) {
      if (wideMql.removeEventListener) wideMql.removeEventListener('change', wideMqlHandler);
      else wideMql.removeListener?.(wideMqlHandler);
      wideMql = null;
      wideMqlHandler = null;
    }
    if (resizeListener) {
      window.removeEventListener('resize', resizeListener);
      resizeListener = null;
    }
    root = null;
  }

  function clear() {
    detach();
    cache.clear();
    opened.clear();
    expandedBursts.clear();
    projectHues.clear();
    projectIds.clear();
    window.CodePierOperationStore?.reset();
    ownerSession = null;
    ownerSpace = null;
    lastSyncedTime = '';
    initialQueueData = null;
    pendingFocusId = '';
    lastAppliedHash = null;
    S.callLog = null;
  }

  const projectKey = (r) =>
    r.project_id || projectIds.get(r.alias) || (r.alias ? 'alias:' + r.alias : '');
  function prepareProjectColors(projects, rows) {
    for (const project of projects) projectIds.set(project.alias, project.id);
    const keys = new Set([...projects.map((project) => project.id), ...rows.map(projectKey)]);
    const palette = [270, 205, 145, 28, 340, 175, 55, 310, 110, 225, 0, 80];
    const used = new Set(projectHues.values());
    for (const k of [...keys].filter(Boolean).sort()) {
      if (projectHues.has(k)) continue;
      let hash = 0;
      for (const char of k) hash = (Math.imul(hash, 31) + char.codePointAt(0)) >>> 0;
      const start = hash % palette.length;
      let hue = palette[start];
      for (let index = 0; index < palette.length; index++) {
        hue = palette[(start + index) % palette.length];
        if (!used.has(hue)) break;
      }
      if (used.has(hue)) {
        hue = hash % 360;
        for (let index = 0; index < 360 && used.has(hue); index++) hue = (hue + 137) % 360;
      }
      projectHues.set(k, hue);
      used.add(hue);
    }
  }

  function rowHTML(r) {
    r = { ...r, ...(window.CodePierOperationStore?.get(r.id) || {}) };
    const [tone, label] = group(r),
      saved = cache.get(r.id),
      argsBlock = summaryHTML(r),
      hasArgs = !argsBlock.includes('is-empty'),
      errBlock = inlineErrorHTML(r),
      waitPill = contentionPillHTML(r),
      isFailed = ['failed', 'interrupted'].includes(r.state),
      isStaleActive =
        active(r) && window.CodePierOperationStore?.status().health === 'reconnecting';
    const showKindBadge = !OBVIOUS_TOOLS.has(r.tool);
    const kindClass = label === '工具' ? 'call-kind call-kind-generic' : 'call-kind';
    const isSelected = st().inspectedId && st().inspectedId === r.id;
    const inlineOpen = opened.has(r.id) && !useInspectorDrawer();
    const actorShort = shortActorLabel(r);
    const sourceTitle = `${r.device_name || '设备未记录'} · ${source(r)} · ${authorization(r).label}`;
    return `<details class="call-entry call-${tone}${hasArgs ? '' : ' call-entry-compact'}${isFailed ? ' call-entry-error' : ''}${isStaleActive ? ' call-entry-stale' : ''}${isSelected ? ' is-selected' : ''}" data-call-id="${esc(r.id)}" data-call-state="${esc(r.state || '')}" data-cp-key="call:${esc(r.id)}" ${inlineOpen ? 'open' : ''}><summary aria-label="展开调用 项目 ${esc(r.alias || '未记录')} ${esc(r.tool)} ${esc(r.id)}"><span class="call-main"><span class="call-top"><span class="call-identity"><span class="call-project${r.alias ? '' : ' call-project-unknown'}" style="--call-project-hue:${projectHues.get(projectKey(r)) ?? 270}" title="${esc(r.alias || '未记录')}"><strong>${esc(r.alias || '未记录')}</strong></span><span class="call-tool"><i class="call-tool-dot" aria-hidden="true"></i><strong class="mono" title="${esc(title(r))}">${esc(title(r))}</strong>${showKindBadge ? `<span class="${kindClass}">${label}</span>` : ''}${triggerChipHTML(r)}${modelLabel(r)}${waitPill}</span></span><span class="call-state">${status(r)}</span></span>${errBlock}${argsBlock}<span class="call-meta"><time title="${esc(stamp(r.created))}">${esc(timeShort(r.created))}<span class="call-time-ms">${r.created ? '.' + pad3(Math.floor(r.created * 1000) % 1000) : ''}</span></time><span class="call-meta-device">${esc(r.device_name || '设备未记录')}</span><span class="call-meta-source">${source(r)}</span><span class="call-authorization">${authorizationHTML(r)}</span><span class="call-source-cell" title="${esc(sourceTitle)}"><span class="call-actor-short">${esc(actorShort)}</span><button type="button" class="call-id-chip mono" data-call-copy-id="${esc(r.id)}" title="点击复制完整编号 ${esc(r.id)}">#${esc(r.id.slice(0, 8))}</button></span><span class="call-filter-note" ${!S.auditStatus || r.state === S.auditStatus ? 'hidden' : ''}>状态已变化，不再符合当前筛选</span></span></span><span class="call-chevron" aria-hidden="true">⌄</span></summary><div class="call-detail"${inlineOpen && saved?.data ? ` data-detail-id="${esc(r.id)}"` : ''}>${inlineOpen && saved?.data ? detailHTML(saved.data) : '<p class="muted" role="status">展开后加载参数、输出与执行链路。</p>'}</div></details>`;
  }

  function isCollapsibleProbe(r) {
    return (
      r &&
      r.state === 'succeeded' &&
      BURST_READ_TOOLS.has(r.tool) &&
      !r.error &&
      r.trigger_source !== 'system_maintenance'
    );
  }

  function buildRowsMarkup(rows) {
    if (!rows.length) return empty('当前筛选范围没有调用记录。');
    const chunks = [
      `<div class="call-table-header" data-cp-key="table-header" aria-hidden="true"><span>时间</span><span>项目 / 工具</span><span>目标与摘要</span><span>状态</span><span class="call-th-right">耗时</span><span class="call-th-right">来源</span><span></span></div>`,
    ];
    let currentDay = '';
    let dayOccurrence = new Map();
    let i = 0;
    while (i < rows.length) {
      const r = rows[i];
      const dKey = dayKey(r.created);
      if (dKey && dKey !== currentDay) {
        currentDay = dKey;
        const occ = (dayOccurrence.get(dKey) || 0) + 1;
        dayOccurrence.set(dKey, occ);
        chunks.push(
          `<div class="call-day-divider" data-cp-key="day:${esc(dKey)}:${occ}" role="separator"><span>${esc(dayHeading(dKey))}</span></div>`,
        );
      }
      if (st().burst && isCollapsibleProbe(r)) {
        const groupRows = [r];
        let j = i + 1;
        while (j < rows.length) {
          const nextRow = rows[j];
          const prevRow = groupRows[groupRows.length - 1];
          const sameContext =
            nextRow.actor === r.actor &&
            (nextRow.project_id || nextRow.alias) === (r.project_id || r.alias) &&
            nextRow.device_id === r.device_id &&
            dayKey(nextRow.created) === dKey;
          const timeGap =
            Number.isFinite(prevRow.created) && Number.isFinite(nextRow.created)
              ? Math.abs(prevRow.created - nextRow.created)
              : 999;
          if (sameContext && isCollapsibleProbe(nextRow) && timeGap <= 15) {
            groupRows.push(nextRow);
            j++;
          } else break;
        }
        if (
          groupRows.length >= 3 &&
          !groupRows.some((item) => opened.has(item.id) || st().inspectedId === item.id)
        ) {
          // Key by the OLDEST member: new probes prepend to a live burst, so the
          // newest ID changes and would collapse a burst the reader expanded.
          const burstKey = `burst:${groupRows[groupRows.length - 1].id}`;
          const searchCnt = groupRows.filter((x) => /search|symbols/.test(x.tool)).length;
          const readCnt = groupRows.length - searchCnt;
          const totalMs = groupRows.reduce(
            (acc, x) => acc + (Number.isFinite(x.elapsed_ms) ? x.elapsed_ms : 0),
            0,
          );
          const authInfo = authorization(r);
          const breakdown = [
            searchCnt ? `${searchCnt} 次搜索` : '',
            readCnt ? `${readCnt} 次读取` : '',
          ]
            .filter(Boolean)
            .join(' · ');
          chunks.push(
            `<details class="call-burst" data-burst-id="${esc(burstKey)}" data-cp-key="${esc(burstKey)}" ${expandedBursts.has(burstKey) ? 'open' : ''}><summary class="call-burst-summary"><time class="call-burst-time" title="${esc(stamp(r.created))}">${esc(timeShort(r.created))}</time><span class="call-burst-identity"><span class="call-project${r.alias ? '' : ' call-project-unknown'}" style="--call-project-hue:${projectHues.get(projectKey(r)) ?? 270}" title="${esc(r.alias || '未记录')}"><strong>${esc(r.alias || '未记录')}</strong></span><span class="call-burst-count">连续探查 ${groupRows.length} 次</span></span><span class="call-burst-title">${esc(breakdown)}</span><span class="call-burst-state">已聚合</span><span class="call-burst-meta mono">${duration(totalMs)}</span><span class="call-burst-actor">${esc(authInfo.label)}</span><span class="call-burst-chevron" aria-hidden="true">⌄</span></summary><div class="call-burst-items">${groupRows.map(rowHTML).join('')}</div></details>`,
          );
          i = j;
          continue;
        }
      }
      chunks.push(rowHTML(r));
      i++;
    }
    return chunks.join('');
  }

  function renderFallbackDiff(data) {
    if (typeof window.diffHTML === 'function') return window.diffHTML(data);
    const added = data.added_lines || 0,
      removed = data.removed_lines || 0;
    return `<div class="call-diff-fallback"><div class="diff-stats"><span>+ ${added} 行</span><span>− ${removed} 行</span></div><pre tabindex="0">${esc(data.diff || '没有内容差异')}</pre></div>`;
  }

  function waterfallHTML(d) {
    const total = d.elapsed_ms,
      t = d.timing || {};
    if (!Number.isFinite(total) || total <= 0) return '';
    const waitMs = Number.isFinite(t.wait_ms) ? Math.max(0, t.wait_ms) : null;
    const execMs = Number.isFinite(t.execution_ms) ? Math.max(0, t.execution_ms) : null;
    if (waitMs === null && execMs === null) return '';
    const safeTotal = Math.max(total, (waitMs || 0) + (execMs || 0), 1);
    const waitPct = waitMs !== null ? Math.min(100, Math.round((waitMs / safeTotal) * 100)) : 0;
    const execPct =
      execMs !== null ? Math.min(100 - waitPct, Math.round((execMs / safeTotal) * 100)) : 0;
    const tailPct = Math.max(0, 100 - waitPct - execPct);
    // Unknown stages must not be mislabeled as measured return/persistence time.
    const tailMs = waitMs !== null && execMs !== null ? Math.max(0, total - waitMs - execMs) : null;
    return `<div class="call-waterfall" data-detail-part="waterfall" aria-label="耗时分布"><div class="call-waterfall-bar"><i class="cw-wait" style="width:${waitPct}%" title="排队与投递 ${duration(waitMs)}"></i><i class="cw-exec" style="width:${execPct}%" title="实际执行 ${duration(execMs)}"></i><i class="cw-tail" style="width:${tailPct}%" title="${tailMs === null ? '未记录阶段' : '回传与落盘'} ${duration(tailMs)}"></i></div><div class="call-waterfall-legend"><span><b class="cw-dot cw-wait"></b>排队等待 ${duration(waitMs)}</span><span><b class="cw-dot cw-exec"></b>实际执行 ${duration(execMs)}</span><span><b class="cw-dot cw-tail"></b>${tailMs === null ? '未记录阶段' : '回传与落盘'} ${duration(tailMs)}</span></div></div>`;
  }

  function section(
    secTitle,
    value,
    name,
    { truncated = false, open = false, liveFollow = false } = {},
  ) {
    const text = typeof value === 'string' ? value : json(value);
    if (text === undefined || text === null || text === '') return '';
    return `<details class="call-section" data-call-section="${name}" ${open ? 'open' : ''}><summary>${esc(secTitle)}<small>${text.length.toLocaleString()} 字符${truncated ? ' · 已截断' : ''}</small></summary><div class="call-section-tools">${liveFollow ? '<label class="call-output-search-label">查找输出 <input type="search" class="call-output-search" aria-label="查找当前输出" maxlength="200"><span class="call-output-matches" role="status"></span></label><button type="button" class="btn ghost small" data-call-action="find-output-next">下一处</button><button type="button" class="btn ghost small call-jump-bottom" data-call-action="scroll-output-bottom" hidden>↓ 跳至最新输出</button>' : ''}<button type="button" class="btn ghost small" data-call-copy="${name}">${icon('copy')}复制已脱敏内容</button></div><div class="${liveFollow ? 'call-output-terminal' : 'call-section-content'}">${liveFollow ? '<div class="call-output-lines" aria-hidden="true">' + Array.from({ length: text.split('\n').length }, (_, i) => i + 1).join('\n') + '</div>' : ''}<pre tabindex="0">${esc(text)}</pre></div></details>`;
  }

  function detailHTML(d) {
    const t = d.timing || {},
      trace = d.trace,
      events = objects(trace?.events),
      resData = d.result && typeof d.result === 'object' ? d.result.data : null,
      hasDiff = resData && typeof resData === 'object' && resData.diff !== undefined;
    const timeline = trace
      ? `<details class="call-section" data-call-section="trace"><summary>执行链路<small>${events.length} 项阶段事件</small></summary><ol class="call-timeline">${
          events
            .map(
              (e) =>
                `<li><strong>${esc(e.label || e.stage)}</strong><small>${esc(stamp(e.at))} · ${esc(e.source)}${Number.isFinite(e.elapsed_ms) ? ' · 本次处理 ' + duration(e.elapsed_ms) : ''}</small>${objects(
                  e.blocked_by,
                )
                  .filter((b) => typeof b.operation_id === 'string')
                  .map(
                    (b) =>
                      `<button type="button" class="btn ghost small" data-call-jump="${esc(b.operation_id)}">等待 ${esc(b.tool)} · #${esc(String(b.operation_id).slice(0, 12))}</button>`,
                  )
                  .join('')}</li>`,
            )
            .join('') || '<li>没有可用的阶段记录，可能由旧版 Agent 执行。</li>'
        }</ol><p class="form-note">只展示已保存的阶段，最多最近 200 项；Hub 时间是收到事件的时刻，不是同步的 Agent 时钟。</p></details>`
      : '';
    const diffSection = hasDiff
      ? `<details class="call-section" data-call-section="diff" open><summary>文件差异<small>+${resData.added_lines || 0} / −${resData.removed_lines || 0} 行</small></summary><div class="call-diff-box">${renderFallbackDiff(resData)}</div></details>`
      : '';
    // AUD-14: Inverted detail hierarchy — Errors & Waterfall & Diff/Output/Arguments first, compact semantic <dl class="call-facts"> & Timeline below.
    return `<div class="call-detail-toolbar" data-detail-part="toolbar"><span class="muted tiny">${esc(d.actor)}</span>${d.project_id ? `<button type="button" class="btn ghost small" data-call-action="filter-project" data-project="${esc(d.project_id)}">筛选此项目</button>` : ''}${d.tool ? `<button type="button" class="btn ghost small" data-call-action="filter-tool" data-tool="${esc(d.tool)}">筛选此工具</button>` : ''}<button type="button" class="btn ghost small" data-call-copy="all">${icon('copy')}复制调用详情</button><button type="button" class="btn ghost small" data-call-action="detail-refresh">${icon('refresh')}刷新详情</button></div>${d.error ? `<div class="notice call-error" data-detail-part="error-banner" role="note">${esc(typeof d.error === 'string' ? d.error : json(d.error))}</div>` : ''}${d.transport_error ? `<p class="form-note" data-detail-part="transport-error">连接状态：${esc(d.transport_error)}</p>` : ''}${window.CodePierQueue?.waitingHTML(d) || ''}${window.CodePierQueue?.liveHTML(d) || ''}${window.CodePierQueue?.runHTML(d) || ''}${waterfallHTML(d)}${d.display?.truncated ? '<p class="form-note" data-detail-part="truncated-note">此详情是有长度限制的已脱敏副本；部分输出或结果已截断，不代表完整记录。</p>' : ''}${diffSection}${section(isKnowledge(d) ? '程序输出（保存的尾部）' : '任务输出（保存的尾部）', d.output, 'output', { truncated: d.output_truncated, open: !!d.error, liveFollow: true })}${section('参数摘要', d.args_summary, 'arguments', { open: true, truncated: d.arguments_truncated })}${section('保存的返回结果', d.result, 'result', { truncated: d.display?.sections?.result })}<dl class="call-facts" data-detail-part="facts">${originFacts(d)}<div><dt>授权依据（调用时记录）</dt><dd class="call-authorization-detail">${authorizationHTML(d, true)}</dd></div><div><dt>操作编号</dt><dd><code>${esc(d.id)}</code></dd></div><div><dt>总历时</dt><dd>${duration(d.elapsed_ms)}</dd></div><div><dt>执行前等待（Hub 观测）</dt><dd>${duration(t.wait_ms)}</dd></div><div><dt>实际执行（本次 Agent 计时）</dt><dd>${duration(t.execution_ms)}</dd></div><div><dt>投递次数</dt><dd>${esc(d.attempts ?? '未记录')}</dd></div><div><dt>当前阶段</dt><dd>${esc(trace?.current?.reason || '未记录')}</dd></div></dl>${timeline}${d.trace_error ? `<p class="form-note" data-detail-part="trace-error">执行链路暂不可用：${esc(d.trace_error)}</p>` : ''}<p class="form-note" data-detail-part="footer-note">工具状态以执行回执为准；HTTP 200 仅表示请求被接收。未采集的耗时不补造，自动脱敏不能识别所有无标签的秘密值。</p>`;
  }

  function detailChildKey(el, counts) {
    const base = el.dataset.callSection || el.dataset.detailPart || el.className + ':' + el.tagName;
    const n = (counts.get(base) || 0) + 1;
    counts.set(base, n);
    return n === 1 ? base : `${base}#${n}`;
  }

  // Incremental output must keep the terminal element, selection and scroll identity.
  // Reconcile the other evidence sections independently, not the entire detail body.
  function reconcileDetailDOM(body, rendered) {
    const template = document.createElement('template');
    template.innerHTML = rendered;
    const oldCounts = new Map();
    const oldSections = new Map(
      [...body.children].map((el) => [detailChildKey(el, oldCounts), el]),
    );
    const incomingCounts = new Map();
    const keep = new Set();
    let cursor = body.firstElementChild;
    for (const incoming of [...template.content.children]) {
      const k = detailChildKey(incoming, incomingCounts);
      let target = oldSections.get(k);
      if (target && !keep.has(target) && target.tagName === incoming.tagName) {
        if (incoming.dataset.callSection === 'output') {
          const pre = target.querySelector('pre'),
            nextPre = incoming.querySelector('pre');
          if (pre && nextPre) {
            const oldText = pre.textContent,
              nextText = nextPre.textContent;
            if (nextText.startsWith(oldText)) {
              if (nextText.length > oldText.length)
                pre.append(document.createTextNode(nextText.slice(oldText.length)));
            } else if (nextText !== oldText) pre.textContent = nextText;
            const summary = target.querySelector('summary');
            if (summary) summary.innerHTML = incoming.querySelector('summary').innerHTML;
            const lines = target.querySelector('.call-output-lines');
            if (lines) lines.textContent = incoming.querySelector('.call-output-lines').textContent;
            findOutput(target, false, false);
          }
        } else if (incoming.dataset.callSection === 'trace') {
          const oldList = target.querySelector('ol'),
            nextList = incoming.querySelector('ol');
          if (oldList && nextList) {
            const oldRows = [...oldList.children];
            const nextRows = [...nextList.children];
            let samePrefix = oldRows.length <= nextRows.length;
            for (let i = 0; samePrefix && i < oldRows.length; i++)
              samePrefix = oldRows[i].outerHTML === nextRows[i].outerHTML;
            if (samePrefix) {
              for (const row of nextRows.slice(oldRows.length)) oldList.append(row);
            } else if (oldList.innerHTML !== nextList.innerHTML)
              oldList.replaceChildren(...nextRows);
            target.querySelector('summary').innerHTML = incoming.querySelector('summary').innerHTML;
          }
        } else if (target.innerHTML !== incoming.innerHTML) {
          // A section whose actual evidence changed may update independently.
          target.innerHTML = incoming.innerHTML;
        }
      } else target = incoming;
      keep.add(target);
      if (target !== cursor) body.insertBefore(target, cursor);
      cursor = target.nextElementSibling;
    }
    for (const child of [...body.children]) if (!keep.has(child)) child.remove();
    bindOutputScroll(body);
  }

  function bindOutputScroll(body) {
    for (const section of body.querySelectorAll('[data-call-section="output"]')) {
      const pre = section.querySelector('pre'),
        gutter = section.querySelector('.call-output-lines');
      if (!pre) continue;
      pre.onscroll = () => {
        if (gutter) gutter.scrollTop = pre.scrollTop;
        const button = section.querySelector('.call-jump-bottom');
        if (button) button.hidden = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 28;
      };
    }
  }

  function findOutput(section, advance = false, navigate = true) {
    const input = section?.querySelector('.call-output-search'),
      pre = section?.querySelector('pre'),
      label = section?.querySelector('.call-output-matches');
    if (!input || !pre || !label) return;
    const query = input.value.toLocaleLowerCase();
    const text = pre.textContent.toLocaleLowerCase();
    const positions = [];
    if (query) {
      let at = text.indexOf(query);
      while (at >= 0 && positions.length < 10000) {
        positions.push(at);
        at = text.indexOf(query, at + Math.max(1, query.length));
      }
    }
    const oldIndex = Number(input.dataset.matchIndex || 0);
    const index = positions.length ? (advance ? oldIndex + 1 : oldIndex) % positions.length : 0;
    input.dataset.matchIndex = String(index);
    label.textContent = query
      ? positions.length
        ? `${index + 1} / ${positions.length} 处`
        : '无匹配'
      : '';
    if (positions.length && navigate) {
      const line = pre.textContent.slice(0, positions[index]).split('\n').length - 1;
      const lineHeight = parseFloat(getComputedStyle(pre).lineHeight) || 20;
      pre.scrollTop = Math.max(0, (line - 2) * lineHeight);
      section.querySelector('.call-output-lines')?.scrollTo(0, pre.scrollTop);
    }
  }

  function syncPodHTML() {
    const health = !st().live ? 'paused' : window.CodePierOperationStore?.status().health || 'live';
    const syncText = st().cursor
      ? '正在查看历史页，不自动插入新调用'
      : !st().live
        ? '实时更新已暂停'
        : health === 'reconnecting'
          ? `连接暂不可用，保留${lastSyncedTime ? ' ' + lastSyncedTime : ''}现有记录；稍后重试。`
          : lastSyncedTime
            ? `已同步 ${lastSyncedTime}`
            : '实时更新已开启';
    return `<div class="call-sync-pod"><span id="call-sync" class="call-sync-pill" data-health="${health}" role="status"><i class="call-sync-dot" aria-hidden="true"></i><span>${esc(syncText)}</span></span><button type="button" class="btn ghost small" data-call-action="live" aria-pressed="${st().live}">${st().live ? '暂停实时更新' : '恢复实时更新'}</button><button type="button" class="btn ghost small" data-call-action="refresh">${icon('refresh')}刷新</button><div class="call-export-split"><button type="button" class="btn ghost small" data-call-action="export">${icon('download')}导出本页调用</button><details class="tool-menu call-export-menu"><summary class="btn ghost small" aria-label="导出当前筛选全集">全集 ▾</summary><div class="tool-menu-items"><button type="button" class="btn ghost small" data-call-action="export-all-csv">导出筛选全集 CSV</button><button type="button" class="btn ghost small" data-call-action="export-all-ndjson">导出筛选全集 NDJSON</button></div></details></div></div>`;
  }

  function countTextHTML(rowsLen = st().rows.length) {
    const totalPart =
      typeof st().total === 'number'
        ? ` · 共 ${st().total.toLocaleString()}${st().totalCapped ? '+' : ''} 项`
        : '';
    return `本页 ${rowsLen} 项 · 每页最多 40 项${totalPart}`;
  }

  function shellInnerHTML({
    rowsMarkup,
    projects = st().projects,
    tools = st().tools,
    hasNext = !!st().next,
    rowsLen = st().rows.length,
  }) {
    const options = (rowsList, value, label) =>
      rowsList
        .map(
          (x) =>
            `<option value="${esc(x.id ?? x)}" ${(x.id ?? x) === value ? 'selected' : ''}>${esc(x[label] ?? x)}</option>`,
        )
        .join('');
    const pinnedMarkup = st().pinnedOp
      ? `<div class="panel call-pinned-inspector" data-cp-key="pinned:${esc(st().pinnedOp.id)}"><div class="call-pinned-head"><strong>定位操作详情 · #${esc(st().pinnedOp.id.slice(0, 12))}</strong><button type="button" class="btn ghost small" data-call-action="close-pinned">关闭定位</button></div><div class="call-detail" data-detail-id="${esc(st().pinnedOp.id)}">${detailHTML(st().pinnedOp)}</div></div>`
      : '';
    return (
      `<div class="call-audit-shell">` +
      heading('操作审计', '调用记录', '', syncPodHTML()) +
      `<div class="filters call-filters"><div class="tabs"><button class="active" data-action="audit-mode" data-mode="operations">工具执行</button><button data-action="audit-mode" data-mode="events">全部事件</button></div><select id="call-project" aria-label="筛选调用项目"><option value="">全部项目</option>${options(projects, st().project, 'alias')}</select><select id="call-tool" aria-label="筛选调用工具"><option value="">全部工具</option>${options(tools, st().tool, 'name')}</select><select id="audit-status" aria-label="操作状态"><option value="">全部状态</option>${[...ACTIVE_STATES, ...TERMINAL_STATES].map((x) => `<option value="${x}" ${S.auditStatus === x ? 'selected' : ''}>${esc(stateNames[x] || x)}</option>`).join('')}</select><form id="call-search"><div class="call-search-box"><input id="audit-query" type="search" aria-label="搜索调用" maxlength="200" placeholder="搜索路径、命令、调用者或编号（/ 聚焦）" value="${esc(S.auditQuery || '')}"><span role="button" tabindex="0" class="call-search-clear" data-call-action="clear-query" aria-label="清空搜索词" ${S.auditQuery ? '' : 'hidden'}>×</span></div><button type="submit" class="btn small">搜索</button></form><details id="call-more-filters" class="call-more-filters"${st().moreFilters ? ' open' : ''}><summary>更多筛选${[S.auditSource, st().triggerSource].filter(Boolean).length ? `<span class="call-more-count">${[S.auditSource, st().triggerSource].filter(Boolean).length}</span>` : ''}</summary><div class="call-more-body"><select id="audit-source" aria-label="来源"><option value="">全部来源</option>${['mcp', 'panel'].map((x) => `<option value="${x}" ${S.auditSource === x ? 'selected' : ''}>${x.toUpperCase()}</option>`).join('')}</select><select id="call-trigger-source" aria-label="触发来源"><option value="">全部触发来源</option>${[
        ['system_maintenance', '自动维护'],
        ['request', '请求发起'],
        ['unknown', '未记录'],
      ]
        .map(
          ([value, label]) =>
            `<option value="${value}" ${st().triggerSource === value ? 'selected' : ''}>${label}</option>`,
        )
        .join('')}
      </select></div></details></div>` +
      `<div class="call-subtoolbar"><div class="call-presets" role="group" aria-label="快捷筛选视图">${PRESET_DEFS.map(([presetKey, label]) => `<button type="button" class="call-preset-pill${st().preset === presetKey ? ' is-active' : ''}" data-call-action="preset" data-preset="${presetKey}" aria-pressed="${st().preset === presetKey}">${esc(label)}</button>`).join('')}</div><div class="call-view-toggles"><button type="button" class="call-chip-toggle" data-call-action="toggle-burst" aria-pressed="${st().burst}"><i class="call-toggle-dot" aria-hidden="true"></i>聚合连续探查</button><span class="call-toolbar-sep" aria-hidden="true"></span><div class="call-density-switch" role="group" aria-label="列表视图密度"><button type="button" class="call-chip-toggle${st().density === 'cards' ? ' is-active' : ''}" data-call-action="density" data-density="cards" aria-pressed="${st().density === 'cards'}">卡片</button><button type="button" class="call-chip-toggle${st().density === 'table' ? ' is-active' : ''}" data-call-action="density" data-density="table" aria-pressed="${st().density === 'table'}">紧凑表格</button></div></div></div>` +
      `<div class="call-stream-banner"><button type="button" id="call-new" class="btn small call-new-pill" data-call-action="latest" hidden>↑ 列表有更新，显示最新</button><div class="call-loading-bar" data-call-progress hidden aria-hidden="true"></div></div>` +
      pinnedMarkup +
      `<div class="call-workspace"><section class="call-log${st().density === 'table' ? ' call-log-table' : ''}" aria-label="工具调用流水">${rowsMarkup}</section><aside class="call-inspector" aria-label="调用详情抽屉" hidden></aside></div>` +
      `<div class="pagination"><span id="call-count">${esc(countTextHTML(rowsLen))}</span><div class="actions"><button type="button" class="btn ghost small" data-call-action="prev" ${st().history.length ? '' : 'disabled'}>上一页</button><button type="button" class="btn ghost small" data-call-action="next" ${hasNext ? '' : 'disabled'}>下一页</button></div></div>` +
      (window.CodePierQueue?.placeholder() || '') +
      uiHelp(
        '记录范围与隐私',
        '展示已保存的工具执行证据，不含完整对话；搜索覆盖已保存的摘要，支持导出当前页或当前筛选全集。参数正文仍按原有策略省略；密码、令牌、授权请求头和环境变量值默认脱敏。列表按创建时间及操作编号稳定分页；新调用通过滚动锚定平滑合入，不挤动正在展开阅读的内容。触发来源与授权身份分开记录，MCP 是调用渠道，不代表模型仍在运行。旧记录缺少字段时显示未记录。',
      ) +
      `</div>`
    );
  }

  // AUD-01: 0ms App-Shell synchronous render (cached snapshot if warm, fixed-height pulse rows if cold).
  function renderShellHTML() {
    ensureOwner();
    applyHashParams();
    if (st().rows.length) {
      return shellInnerHTML({
        rowsMarkup: buildRowsMarkup(st().rows),
        projects: st().projects,
        tools: st().tools,
        hasNext: !!st().next,
        rowsLen: st().rows.length,
      });
    }
    const skeletons = Array.from(
      { length: 6 },
      (_, idx) =>
        `<div class="call-skeleton-row" data-cp-key="skel:${idx}" aria-hidden="true"></div>`,
    ).join('');
    return shellInnerHTML({ rowsMarkup: skeletons, rowsLen: 0, hasNext: false });
  }

  async function html(seq) {
    ensureOwner();
    applyHashParams();
    const session = S.session,
      filter = key(),
      r = await api('/api/call-log?' + filter + '&include_queue=true');
    if (seq !== S.renderSeq || session !== S.session || filter !== key()) return '';
    window.CodePierOperationStore?.ingestCallLog(r);
    initialQueueData = r.queue && typeof r.queue === 'object' ? r.queue : null;
    lastSyncedTime = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    consecutiveFailures = 0;
    prepareProjectColors(r.projects || [], r.operations || []);
    st().rows = r.operations || [];
    st().projects = r.projects || st().projects;
    st().tools = r.tools || st().tools;
    st().next = r.next_cursor;
    if (typeof r.total === 'number') {
      st().total = r.total;
      st().totalCapped = !!r.total_capped;
    }
    st().incoming = false;
    st().incomingCount = 0;
    const keep = new Set(st().rows.map((x) => x.id));
    for (const id of cache.keys()) if (!keep.has(id)) cache.delete(id);
    for (const id of opened) if (!keep.has(id)) opened.delete(id);
    return shellInnerHTML({
      rowsMarkup: buildRowsMarkup(st().rows),
      projects: st().projects,
      tools: st().tools,
      hasNext: !!r.next_cursor,
      rowsLen: st().rows.length,
    });
  }

  function collectKeyedNodes(container, map = new Map()) {
    for (const child of container.children) {
      const k = child.dataset?.cpKey || child.dataset?.callId;
      if (k && !map.has(k)) map.set(k, child);
      if (child.classList?.contains('call-burst')) {
        const itemsBox = child.querySelector(':scope > .call-burst-items');
        if (itemsBox) collectKeyedNodes(itemsBox, map);
      }
    }
    return map;
  }

  // Unified keyed DOM patcher for .call-log (identical behavior with or without core/bundle.js):
  // preserves open <details data-call-id> subtrees, scroll positions, and listeners across list reloads & bursts.
  function reconcileListDOM(
    container,
    htmlString,
    { syncOpenState = false, keyedPool = null } = {},
  ) {
    if (!container) return;
    const template = document.createElement('template');
    template.innerHTML = htmlString;
    const existingByKey = keyedPool || collectKeyedNodes(container);
    let prevPlaced = null;
    const used = new Set();
    for (const incoming of [...template.content.children]) {
      const k = incoming.dataset?.cpKey || incoming.dataset?.callId;
      let target = k ? existingByKey.get(k) : null;
      const reusing = !!(target && target.tagName === incoming.tagName && !used.has(target));
      if (!reusing) target = incoming;
      used.add(target);
      const refNode = prevPlaced ? prevPlaced.nextSibling : container.firstChild;
      if (target !== refNode) {
        const savedScrolls = reusing
          ? [...target.querySelectorAll('pre')].map((p) => [p, p.scrollTop, p.scrollLeft])
          : null;
        container.insertBefore(target, refNode);
        if (savedScrolls?.length) {
          for (const [p, top, left] of savedScrolls) {
            p.scrollTop = top;
            p.scrollLeft = left;
            const gutter = p
              .closest('[data-call-section="output"]')
              ?.querySelector('.call-output-lines');
            if (gutter) gutter.scrollTop = top;
          }
        }
      }
      prevPlaced = target;
      if (reusing) {
        const isBurst = target.classList.contains('call-burst');
        const nextOpen = isBurst
          ? expandedBursts.has(target.dataset.burstId)
          : useInspectorDrawer()
            ? false
            : syncOpenState
              ? opened.has(target.dataset.callId)
              : target.open || opened.has(target.dataset.callId);
        target.className = incoming.className;
        if (incoming.dataset.callState !== undefined)
          target.dataset.callState = incoming.dataset.callState;
        const newSum = incoming.querySelector(':scope > summary');
        const oldSum = target.querySelector(':scope > summary');
        if (newSum && oldSum && oldSum.innerHTML !== newSum.innerHTML) {
          oldSum.innerHTML = newSum.innerHTML;
          const aria = newSum.getAttribute('aria-label');
          if (aria) oldSum.setAttribute('aria-label', aria);
        }
        if (isBurst) {
          const newItems = incoming.querySelector(':scope > .call-burst-items');
          const oldItems = target.querySelector(':scope > .call-burst-items');
          if (newItems && oldItems) {
            reconcileListDOM(oldItems, newItems.innerHTML, {
              syncOpenState,
              keyedPool: existingByKey,
            });
          }
        } else if (!nextOpen) {
          const detailBox = target.querySelector(':scope > .call-detail');
          if (detailBox && (detailBox.dataset.detailId || detailBox.querySelector('.call-facts'))) {
            delete detailBox.dataset.detailId;
            detailBox._renderedHTML = '';
            detailBox.innerHTML =
              '<p class="muted" role="status">展开后加载参数、输出与执行链路。</p>';
          }
        }
        if (target.open !== nextOpen) target.open = nextOpen;
      }
    }
    for (const child of [...container.childNodes]) {
      if (!used.has(child)) child.remove();
    }
  }

  function updatePaginationControls() {
    if (!root?.isConnected) return;
    const countNode = root.querySelector('#call-count');
    if (countNode) countNode.textContent = countTextHTML(st().rows.length);
    const prevBtn = root.querySelector('[data-call-action="prev"]');
    if (prevBtn) prevBtn.disabled = !st().history.length;
    const nextBtn = root.querySelector('[data-call-action="next"]');
    if (nextBtn) nextBtn.disabled = !st().next;
    const clearBtn = root.querySelector('[data-call-action="clear-query"]');
    if (clearBtn) clearBtn.hidden = !(S.auditQuery || root.querySelector('#audit-query')?.value);
    const moreCountValues = [S.auditSource, st().triggerSource].filter(Boolean).length;
    const moreSum = root.querySelector('#call-more-filters > summary');
    if (moreSum) {
      moreSum.innerHTML =
        '更多筛选' +
        (moreCountValues ? `<span class="call-more-count">${moreCountValues}</span>` : '');
    }
    root.querySelectorAll('[data-call-action="preset"]').forEach((btn) => {
      const on = btn.dataset.preset === st().preset;
      btn.classList.toggle('is-active', on);
      btn.setAttribute('aria-pressed', String(on));
    });
  }

  // AUD-02: Zero-wipe local list reload — preserves #operation-queue and search input focus/IME!
  async function reloadList({ resetCursor = false, clearDetails = false, initial = false } = {}) {
    ensureOwner();
    if (!root?.isConnected || !root.querySelector('.call-log')) {
      return renderPage(false);
    }
    if (resetCursor) {
      st().cursor = '';
      st().history = [];
      st().next = null;
      S.auditOffset = 0;
    }
    if (clearDetails) {
      clearTimeout(hoverTimer);
      abortAllDetailInflight();
      cache.clear();
      opened.clear();
    }
    syncUrlHash();
    inflight?.abort();
    inflight = null;
    listController?.abort();
    const controller = new AbortController();
    listController = controller;
    const session = S.session,
      generation = epoch,
      filter = key();
    const listEl = root.querySelector('.call-log'),
      progressEl = root.querySelector('[data-call-progress]');
    clearTimeout(progressTimer);
    progressTimer = setTimeout(() => {
      if (listController === controller && root?.isConnected) {
        if (progressEl) progressEl.hidden = false;
        if (listEl) listEl.classList.add('is-reloading');
      }
    }, 120);
    try {
      const r = await api(
        '/api/call-log?' +
          filter +
          (initial
            ? '&include_queue=true&include_filters=true&include_total=true'
            : '&include_filters=false&include_total=true'),
        {
          signal: controller.signal,
        },
      );
      if (
        listController !== controller ||
        session !== S.session ||
        generation !== epoch ||
        !root?.isConnected
      )
        return;
      window.CodePierOperationStore?.ingestCallLog(r);
      if (r.queue) window.CodePierQueue?.hydrate(r.queue);
      if (initial) {
        st().projects = r.projects || st().projects;
        st().tools = r.tools || st().tools;
        const updateOptions = (selector, items, selected, label, title) => {
          const input = root.querySelector(selector);
          if (!input) return;
          input.replaceChildren(
            new Option(title, ''),
            ...items.map((item) => {
              const value = item.id ?? item;
              return new Option(item[label] ?? item, value, false, value === selected);
            }),
          );
        };
        updateOptions('#call-project', st().projects, st().project, 'alias', '全部项目');
        updateOptions('#call-tool', st().tools, st().tool, 'name', '全部工具');
      }
      lastSyncedTime = new Date().toLocaleTimeString('zh-CN', { hour12: false });
      consecutiveFailures = 0;
      prepareProjectColors(st().projects, r.operations || []);
      st().rows = r.operations || [];
      st().next = r.next_cursor;
      if (typeof r.total === 'number') {
        st().total = r.total;
        st().totalCapped = !!r.total_capped;
      }
      st().incoming = false;
      st().incomingCount = 0;
      const keep = new Set(st().rows.map((x) => x.id));
      if (st().pinnedOp?.id) keep.add(st().pinnedOp.id);
      for (const id of cache.keys()) if (!keep.has(id)) cache.delete(id);
      for (const id of [...opened]) {
        if (!keep.has(id)) {
          opened.delete(id);
          abortDetailIfUnused(id);
        }
      }
      if (st().inspectedId && (clearDetails || !st().rows.some((x) => x.id === st().inspectedId)))
        closeInspector();
      reconcileListDOM(listEl, buildRowsMarkup(st().rows), { syncOpenState: clearDetails });
      const banner = root.querySelector('#call-new');
      if (banner) banner.hidden = true;
      updatePaginationControls();
      bindEntries();
      if (st().inspectedId && useInspectorDrawer()) {
        if (!root.querySelector('.call-workspace.has-inspector')) {
          openInspector(st().inspectedId).catch(() => {});
        } else {
          updateInspectorNavButtons();
          const selNode = root.querySelector(`[data-call-id="${CSS.escape(st().inspectedId)}"]`);
          if (selNode) scrollRowIntoList(selNode);
        }
      }
      syncLiveTicker();
      setSync(
        (st().live ? '已同步 ' : '已手动同步 ') + lastSyncedTime + (st().cursor ? ' · 历史页' : ''),
        'live',
      );
    } catch (e) {
      // A newer filter/search superseded this request: not a user-facing error.
      if (e?.name === 'AbortError' || listController !== controller) return;
      throw e;
    } finally {
      if (listController === controller) {
        listController = null;
        clearTimeout(progressTimer);
        if (progressEl) progressEl.hidden = true;
        if (listEl) listEl.classList.remove('is-reloading');
      }
      if (generation === epoch) {
        schedule();
        flushQueuedRefresh();
      }
    }
  }

  // An SSE hint that arrives while a list request is in flight may postdate
  // that request's snapshot. Replay it once instead of waiting a full poll.
  function flushQueuedRefresh() {
    if (!refreshQueued || inflight || listController) return;
    refreshQueued = false;
    triggerThrottledHeadRefresh();
  }

  function pollDelayMs() {
    if (consecutiveFailures > 0) {
      const base = Math.min(10000, 1000 * 2 ** Math.min(3, consecutiveFailures - 1));
      return base + Math.floor(Math.random() * 250);
    }
    const sseLive = typeof S !== 'undefined' && S.events?.readyState === 1;
    return sseLive ? 15000 : 5000;
  }

  function schedule() {
    clearTimeout(timer);
    if (root?.isConnected && st().live) {
      const delay = pollDelayMs();
      retryAt = Date.now() + delay;
      timer = setTimeout(() => refresh(true), delay);
      clearInterval(retryTicker);
      retryTicker = consecutiveFailures
        ? setInterval(() => {
            if (root?.isConnected && st().live && consecutiveFailures)
              setSync(
                `连接暂不可用，保留 ${lastSyncedTime || '上次'} 快照 · ${Math.max(0, Math.ceil((retryAt - Date.now()) / 1000))} 秒后重试`,
                'reconnecting',
              );
          }, 1000)
        : null;
    }
  }

  function syncLiveTicker() {
    clearInterval(tickTimer);
    tickTimer = null;
    if (!root?.isConnected || !st().live || document.hidden) return;
    if (!root.querySelector('[data-live-created]')) return;
    tickTimer = setInterval(() => {
      if (!root?.isConnected || !st().live || document.hidden) return;
      if (window.CodePierOperationStore?.status().health !== 'live') return;
      const liveNodes = root.querySelectorAll('[data-live-created]');
      if (!liveNodes.length) {
        clearInterval(tickTimer);
        tickTimer = null;
        return;
      }
      for (const el of liveNodes) {
        const created = Number(el.dataset.liveCreated);
        if (!Number.isFinite(created) || created <= 0) continue;
        const ms = window.CodePierOperationStore.liveElapsedMs(created, null);
        if (!Number.isFinite(ms)) continue;
        if (el.classList.contains('queue-elapsed')) {
          el.textContent =
            ms < 1000 ? Math.round(ms) + 'ms' : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's';
        } else {
          el.innerHTML = `<span class="call-dur-prefix">已历时 </span>${duration(ms)}`;
        }
      }
    }, 1000);
  }

  function setSync(text, healthState = null) {
    const node = root?.querySelector('#call-sync');
    if (!node) return;
    if (healthState) node.dataset.health = healthState;
    else if (!st().live) node.dataset.health = 'paused';
    const label = node.querySelector('span') || node;
    label.textContent = text;
  }

  // Unified detail fetcher shared by inline <details>, wide-screen .call-inspector,
  // .call-pinned-inspector, and 120ms hover/focus prefetch.
  async function fetchDetailData(
    id,
    { force = false, manual = false, prefetch = false, externalSignal = null } = {},
  ) {
    if (!id) return null;
    const old = cache.get(id);
    if (!force && old?.data) return old.data;
    const existing = detailInflight.get(id);
    if (existing) {
      if (prefetch) return existing.promise;
      if (!manual && (!force || existing.force)) {
        existing.prefetch = false;
        return existing.promise;
      }
      detailInflight.delete(id);
      existing.controller.abort();
    } else if (prefetch) {
      let prefetchCount = 0;
      for (const req of detailInflight.values()) if (req.prefetch) prefetchCount++;
      if (prefetchCount >= 2) return null;
    }
    const session = S.session,
      generation = epoch,
      ctrl = new AbortController();
    let onExtAbort = null;
    if (externalSignal) {
      if (externalSignal.aborted) return null;
      onExtAbort = () => ctrl.abort();
      externalSignal.addEventListener('abort', onExtAbort, { once: true });
    }
    const reqEntry = { controller: ctrl, force, manual, prefetch, promise: null };
    const run = (async () => {
      try {
        let canIncremental =
          !prefetch &&
          force &&
          !manual &&
          old?.data &&
          active(old.data) &&
          Number.isInteger(old.data.output_seq);
        let detailUrl = '/api/call-log/' + encodeURIComponent(id);
        if (canIncremental) {
          const dp = new URLSearchParams({
            after_output_seq: String(old.data.output_seq),
            after_event_id: String(old.data.trace?.latest_event_id || 0),
          });
          if (
            Number.isInteger(old.data.output_clean_chars) &&
            old.data.output_clean_chars > 0 &&
            typeof old.data.output_clean_sha === 'string'
          ) {
            dp.set('after_output_chars', String(old.data.output_clean_chars));
            dp.set('after_output_sha', old.data.output_clean_sha);
          }
          detailUrl += '?' + dp.toString();
        }
        const stale = () =>
          ctrl.signal.aborted ||
          session !== S.session ||
          generation !== epoch ||
          detailInflight.get(id) !== reqEntry;
        const reqOpts = prefetch
          ? { signal: ctrl.signal, retryDelays: [] }
          : { signal: ctrl.signal };
        let fetched = await api(detailUrl, reqOpts);
        if (stale()) return null;
        if (canIncremental && !active(fetched)) {
          // Incremental reads omit the full trace, so final wait/execution timing
          // cannot be derived from them. Settle a finished call with one full read.
          fetched = await api('/api/call-log/' + encodeURIComponent(id), reqOpts);
          if (stale()) return null;
          canIncremental = false;
        }
        let d = fetched;
        if (canIncremental && old?.data) {
          const mergedOutput = fetched.output_unchanged
            ? old.data.output
            : typeof fetched.output_delta === 'string' &&
                typeof old.data.output === 'string' &&
                fetched.output_base_chars === old.data.output.length
              ? old.data.output + fetched.output_delta
              : fetched.output;
          const prevEvents = objects(old.data.trace?.events);
          const newEvents = objects(fetched.trace?.events);
          const mergedTrace =
            fetched.trace_incremental && old.data.trace
              ? {
                  ...old.data.trace,
                  ...fetched.trace,
                  events: [...prevEvents, ...newEvents].slice(-200),
                  latest_event_id:
                    fetched.trace?.latest_event_id ?? old.data.trace?.latest_event_id ?? 0,
                }
              : fetched.trace;
          d = {
            ...old.data,
            ...fetched,
            output: mergedOutput,
            timing: fetched.trace_incremental ? old.data.timing : fetched.timing,
            trace: mergedTrace,
          };
        }
        if (!prefetch) {
          window.CodePierOperationStore?.ingestCallLog({ operations: [d] });
          const canonical = window.CodePierOperationStore?.get(id);
          const localRow = st().rows.find((x) => x.id === id);
          const newerRow =
            canonical ||
            (localRow && Number(localRow.updated || 0) > Number(d.updated || 0) ? localRow : null);
          if (newerRow) {
            d = { ...d, state: newerRow.state, updated: newerRow.updated };
            for (const field of ['elapsed_ms', 'exit_code', 'error']) {
              if (newerRow[field] !== undefined) d[field] = newerRow[field];
            }
          }
        }
        const prevEntry = cache.get(id);
        cache.set(id, { data: d, needsRender: !!prevEntry?.needsRender });
        if (!prefetch && root?.isConnected) {
          const rowNode = root.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
          if (rowNode) patchVisibleRowInPlace(rowNode, d);
          const drawer = root.querySelector('.call-inspector');
          if (drawer && !drawer.hidden && drawer.dataset.inspectorId === id) {
            renderInspectorHeaderTitle(drawer, id);
          }
        }
        return d;
      } catch (err) {
        if (prefetch || ctrl.signal.aborted || err?.name === 'AbortError') return null;
        throw err;
      } finally {
        if (onExtAbort && externalSignal) externalSignal.removeEventListener('abort', onExtAbort);
        if (detailInflight.get(id) === reqEntry) detailInflight.delete(id);
      }
    })();
    reqEntry.promise = run;
    detailInflight.set(id, reqEntry);
    return run;
  }

  // Unified detail DOM renderer for inline <details>, .call-inspector, and .call-pinned-inspector.
  // Tracks _renderedHTML and data-detail-id directly on the container DOM node so containers never
  // pollute one another, and preserves section fold states, <pre> scroll, and button focus.
  function renderDetailInto(body, d, { force = false, manual = false, focusAction = null } = {}) {
    if (!body?.isConnected || !d) return false;
    const sameOp = body.dataset.detailId === d.id && !!body.querySelector('.call-facts');
    const selection = window.getSelection();
    if (
      sameOp &&
      force &&
      !manual &&
      (body.contains(document.activeElement) ||
        (selection && !selection.isCollapsed && body.contains(selection.anchorNode)))
    ) {
      const entry = cache.get(d.id);
      if (entry) entry.needsRender = true;
      return false;
    }
    const prevSections = sameOp
      ? new Map(
          [...body.querySelectorAll('details[data-call-section]')].map((x) => [
            x.dataset.callSection,
            x.open,
          ]),
        )
      : null;
    const scrolls = sameOp
      ? new Map(
          [...body.querySelectorAll('[data-call-section]')].map((x) => {
            const p = x.querySelector('pre');
            const nearBottom = p ? p.scrollHeight - p.scrollTop - p.clientHeight < 28 : false;
            return [
              x.dataset.callSection,
              p ? [p.scrollTop, p.scrollLeft, nearBottom, p.scrollHeight] : [0, 0, false, 0],
            ];
          }),
        )
      : null;
    const rendered = detailHTML(d);
    if (!sameOp || rendered !== body._renderedHTML) {
      if (sameOp) {
        reconcileDetailDOM(body, rendered);
        body.querySelectorAll('details[data-call-section]').forEach((x) => {
          const secName = x.dataset.callSection;
          if (prevSections.has(secName)) x.open = prevSections.get(secName);
        });
        for (const secNode of body.querySelectorAll('[data-call-section]')) {
          const p = secNode.querySelector('pre'),
            savedScroll = scrolls.get(secNode.dataset.callSection);
          if (p && savedScroll) {
            const [top, left, nearBottom, oldHeight] = savedScroll;
            if (
              secNode.dataset.callSection === 'output' &&
              nearBottom &&
              p.scrollHeight > oldHeight
            ) {
              p.scrollTop = p.scrollHeight;
              p.scrollLeft = left;
            } else {
              p.scrollTop = top;
              p.scrollLeft = left;
              if (
                secNode.dataset.callSection === 'output' &&
                p.scrollHeight - p.scrollTop - p.clientHeight > 40 &&
                p.scrollHeight > oldHeight
              ) {
                const jumpBtn = secNode.querySelector('.call-jump-bottom');
                if (jumpBtn) jumpBtn.hidden = false;
              }
            }
          }
        }
      } else {
        body.innerHTML = rendered;
        bindOutputScroll(body);
      }
      body.dataset.detailId = d.id;
      body._renderedHTML = rendered;
    } else {
      bindOutputScroll(body);
    }
    const entry = cache.get(d.id);
    if (entry) entry.needsRender = false;
    if (focusAction === 'detail-refresh') {
      body.querySelector('[data-call-action="detail-refresh"]')?.focus({ preventScroll: true });
    }
    return true;
  }

  // AUD-06: 120ms hover/focus intent prefetch (capped at 2 concurrent prefetches).
  function prefetchDetail(id) {
    if (!id || cache.has(id) || detailInflight.has(id)) return;
    fetchDetailData(id, { prefetch: true }).catch(() => {});
  }

  async function detail(node, force = false, manual = false) {
    if (!node?.isConnected || !node.open) return;
    const id = node.dataset.callId,
      body = node.querySelector(':scope > .call-detail');
    if (!id || !body) return;
    const focusAction = manual ? document.activeElement?.dataset.callAction : null;
    const cached = cache.get(id)?.data;
    if (!force && cached) {
      patchVisibleRowInPlace(node, cached);
      renderDetailInto(body, cached, { force: false, manual, focusAction });
      return;
    }
    if (!cached && !body.querySelector('.call-facts')) {
      delete body.dataset.detailId;
      body._renderedHTML = '';
      body.innerHTML = '<p class="muted" role="status">正在加载调用详情…</p>';
    }
    const session = S.session,
      generation = epoch;
    try {
      const d = await fetchDetailData(id, { force, manual });
      if (!d || session !== S.session || generation !== epoch || !node.isConnected || !node.open)
        return;
      renderDetailInto(body, d, { force, manual, focusAction });
    } catch (e) {
      if (
        e?.name !== 'AbortError' &&
        session === S.session &&
        generation === epoch &&
        node.isConnected &&
        node.open
      ) {
        if (cache.get(id)?.data) setSync('详情更新失败，可手动重试：' + e.message, 'reconnecting');
        else {
          delete body.dataset.detailId;
          body._renderedHTML = '';
          body.innerHTML = `<p class="form-note" role="alert">${esc(e.message)}</p><button type="button" class="btn small" data-call-action="detail-refresh">重试详情</button>`;
        }
      }
    }
  }

  function patchVisibleRowInPlace(node, item) {
    if (!node) return;
    const previous = st().rows.find((x) => x.id === item.id);
    // Monotonic guard: never let an older snapshot overwrite a newer terminal/SSE row state!
    if (
      previous &&
      Number.isFinite(previous.updated) &&
      Number.isFinite(item.updated) &&
      item.updated < previous.updated
    ) {
      return;
    }
    const cached = cache.get(item.id)?.data;
    if (cached?.updated === item.updated && item.exit_code === undefined)
      item.exit_code = cached.exit_code;
    const merged = Object.assign(previous || {}, item);
    // SSE state frames carry created/updated but no elapsed_ms. Without this the
    // row would freeze at the last polled running duration once it finishes.
    // Same derivation as the server's public_row for terminal records.
    if (
      item.elapsed_ms === undefined &&
      !active(merged) &&
      Number.isFinite(merged.created) &&
      Number.isFinite(merged.updated)
    )
      merged.elapsed_ms = Math.max(0, Math.round((merged.updated - merged.created) * 1000));
    node.classList.toggle('call-entry-error', ['failed', 'interrupted'].includes(merged.state));
    node.dataset.callState = merged.state || '';
    node.classList.remove('call-entry-stale');
    const part = node.querySelector('.call-state'),
      markup = status(merged);
    if (part && part.innerHTML !== markup) part.innerHTML = markup;
    const oldWait = node.querySelector('.call-tool .call-blocker-pill');
    const nextWaitHTML = contentionPillHTML(merged);
    if (oldWait && !nextWaitHTML) oldWait.remove();
    else if (!oldWait && nextWaitHTML)
      node.querySelector('.call-tool')?.insertAdjacentHTML('beforeend', nextWaitHTML);
    else if (oldWait && nextWaitHTML && oldWait.outerHTML !== nextWaitHTML)
      oldWait.outerHTML = nextWaitHTML;
    const oldErr = node.querySelector(':scope > summary .call-inline-error');
    const nextErrHTML = inlineErrorHTML(merged);
    if (oldErr && !nextErrHTML) oldErr.remove();
    else if (!oldErr && nextErrHTML)
      node.querySelector(':scope > summary .call-top')?.insertAdjacentHTML('afterend', nextErrHTML);
    else if (oldErr && nextErrHTML && oldErr.outerHTML !== nextErrHTML)
      oldErr.outerHTML = nextErrHTML;
    const note = node.querySelector('.call-filter-note');
    if (note) note.hidden = !S.auditStatus || merged.state === S.auditStatus;
  }

  async function refresh(automatic = false, { fromEvent = false } = {}) {
    if (!root?.isConnected || S.page !== 'audit' || S.auditMode !== 'operations') return detach();
    if (inflight || listController) {
      if (fromEvent) refreshQueued = true;
      return;
    }
    if (automatic && (!st().live || document.hidden)) {
      schedule();
      return;
    }
    const controller = new AbortController();
    inflight = controller;
    const generation = epoch,
      session = S.session,
      filter = key();
    // The queue panel stops its own polling once the call log supplies its
    // snapshot (managed mode), so every list refresh must carry the queue —
    // above all while SSE is down and this poll is the only update path.
    const shouldIncludeQueue = !!window.CodePierQueue;
    try {
      const watch = st()
        .rows.map((x) => x.id)
        .slice(0, 40)
        .join(',');
      const url =
        '/api/call-log?' +
        filter +
        '&include_filters=false&watch=' +
        encodeURIComponent(watch) +
        (shouldIncludeQueue ? '&include_queue=true' : '');
      const r = await api(url, {
        signal: controller.signal,
        retryDelays: automatic ? [] : [400, 1000],
      });
      if (
        generation !== epoch ||
        session !== S.session ||
        !root?.isConnected ||
        filter !== key() ||
        (automatic && !st().live)
      )
        return;
      consecutiveFailures = 0;
      lastSyncedTime = new Date().toLocaleTimeString('zh-CN', { hour12: false });
      window.CodePierOperationStore?.ingestCallLog(r);
      if (r.queue && typeof r.queue === 'object') {
        window.CodePierQueue?.hydrate(r.queue);
      } else if (!automatic) {
        window.CodePierQueue?.refresh();
      }
      prepareProjectColors(st().projects, r.operations || []);
      const nodes = new Map(
        [...root.querySelectorAll('[data-call-id]')].map((n) => [n.dataset.callId, n]),
      );
      const newOps = (r.operations || []).filter((x) => !nodes.has(x.id));
      const changedMembership =
        newOps.length > 0 ||
        [...nodes.keys()].some((id) => !(r.operations || []).some((x) => x.id === id));
      if (changedMembership && !st().cursor) {
        st().incoming = true;
        st().incomingCount = newOps.length || 1;
        st().incomingActive = newOps.filter(active).length;
        const newBanner = root.querySelector('#call-new');
        if (newBanner) {
          const suffix = newOps.length
            ? `（新增 ${newOps.length} 条${st().incomingActive ? ` · ${st().incomingActive} 条运行中` : ''}）`
            : '';
          newBanner.textContent = '↑ 列表有更新，显示最新' + suffix;
          newBanner.hidden = false;
        }
      }
      for (const item of [...(r.operations || []), ...(r.updates || [])]) {
        const node = nodes.get(item.id);
        if (node) patchVisibleRowInPlace(node, window.CodePierOperationStore?.get(item.id) || item);
      }
      const liveNodes = [...nodes.values()].filter(
        (n) =>
          n.open &&
          (cache.get(n.dataset.callId)?.needsRender ||
            cache.get(n.dataset.callId)?.data?.updated !==
              st().rows.find((x) => x.id === n.dataset.callId)?.updated ||
            active(
              cache.get(n.dataset.callId)?.data ||
                st().rows.find((x) => x.id === n.dataset.callId) ||
                {},
            )),
      );
      // Independent nodes: refresh concurrently (detail() handles its own errors)
      // instead of paying up to four sequential round trips per poll.
      await Promise.all(
        Array.from({ length: Math.min(4, liveNodes.length) }, (_, i) =>
          detail(liveNodes[(detailTurn + i) % liveNodes.length], true),
        ),
      );
      if (st().inspectedId && useInspectorDrawer()) {
        const inspId = st().inspectedId;
        const inspCached = cache.get(inspId);
        const inspRow = st().rows.find((x) => x.id === inspId);
        if (
          inspCached?.needsRender ||
          inspCached?.data?.updated !== inspRow?.updated ||
          active(inspCached?.data || inspRow || {})
        ) {
          await openInspector(inspId, { force: true });
        }
      }
      if (st().pinnedOp?.id) {
        const pinId = st().pinnedOp.id;
        const pinCached = cache.get(pinId);
        const pinStore = window.CodePierOperationStore?.get(pinId);
        if (
          pinCached?.needsRender ||
          (pinStore && pinStore.updated !== st().pinnedOp.updated) ||
          active(st().pinnedOp)
        ) {
          await refreshPinnedOp({ force: true });
        }
      }
      if (
        generation !== epoch ||
        session !== S.session ||
        !root?.isConnected ||
        filter !== key() ||
        (automatic && !st().live)
      )
        return;
      detailTurn = liveNodes.length ? (detailTurn + 4) % liveNodes.length : 0;

      // AUD-05: Scroll-anchored / viewport-aware insertion instead of brittle window.scrollY < 60!
      const selection = window.getSelection(),
        focused = document.activeElement,
        listEl = root.querySelector('.call-log'),
        drawerEl = root.querySelector('.call-inspector');
      const interactiveFocusInsideList =
        (listEl?.contains(focused) || drawerEl?.contains(focused)) &&
        focused?.matches('input,select,textarea,button,summary,pre');
      if (
        !st().cursor &&
        st().incoming &&
        !opened.size &&
        !st().inspectedId &&
        (!selection || selection.isCollapsed) &&
        !interactiveFocusInsideList &&
        listEl
      ) {
        const listTop = listEl.getBoundingClientRect().top;
        let anchorNode = null,
          anchorTop = 0;
        if (listTop < -32) {
          anchorNode = [...listEl.querySelectorAll('[data-call-id]')].find(
            (el) => el.getBoundingClientRect().bottom > 0,
          );
          if (anchorNode) anchorTop = anchorNode.getBoundingClientRect().top;
        }
        const keep = new Set((r.operations || []).map((x) => x.id));
        if (st().pinnedOp?.id) keep.add(st().pinnedOp.id);
        for (const id of cache.keys()) if (!keep.has(id)) cache.delete(id);
        st().rows = r.operations || [];
        st().next = r.next_cursor;
        st().incoming = false;
        st().incomingCount = 0;
        reconcileListDOM(listEl, buildRowsMarkup(st().rows));
        if (anchorNode?.isConnected) {
          const drift = anchorNode.getBoundingClientRect().top - anchorTop;
          if (Math.abs(drift) > 1) window.scrollBy(0, drift);
        }
        const newBanner = root.querySelector('#call-new');
        if (newBanner) newBanner.hidden = true;
        updatePaginationControls();
        bindEntries();
      }
      syncLiveTicker();
      setSync(
        (st().live ? '已同步 ' : '已手动同步 ') + lastSyncedTime + (st().cursor ? ' · 历史页' : ''),
        'live',
      );
    } catch (e) {
      if (generation === epoch && session === S.session && e.name !== 'AbortError') {
        consecutiveFailures++;
        window.CodePierOperationStore?.setConnectionHealth('reconnecting', e.message);
        window.CodePierQueue?.setStale?.(e.message);
        root?.querySelectorAll('.call-entry').forEach((node) => {
          const row = st().rows.find((x) => x.id === node.dataset.callId);
          if (row && active(row)) node.classList.add('call-entry-stale');
        });
        setSync(
          `连接暂不可用，保留${lastSyncedTime ? ' ' + lastSyncedTime : ''}现有记录；稍后重试。`,
          'reconnecting',
        );
      }
    } finally {
      if (inflight === controller) inflight = null;
      if (generation === epoch) {
        schedule();
        flushQueuedRefresh();
      }
    }
  }

  // AUD-03: Leading + trailing throttled head sync for unknown SSE operations.
  function triggerThrottledHeadRefresh() {
    if (!root?.isConnected || !st().live) return;
    const now = Date.now();
    const elapsed = now - sseLastRun;
    if (elapsed >= 1200) {
      clearTimeout(sseThrottleTimer);
      sseThrottleTimer = null;
      sseLastRun = now;
      refresh(true, { fromEvent: true }).catch(() => {});
      return;
    }
    if (!sseThrottleTimer) {
      const waitMs = Math.max(300, 1200 - elapsed);
      sseThrottleTimer = setTimeout(() => {
        sseThrottleTimer = null;
        sseLastRun = Date.now();
        if (root?.isConnected && st().live) refresh(true, { fromEvent: true }).catch(() => {});
      }, waitMs);
    }
  }

  function useInspectorDrawer() {
    return (
      st().density === 'table' &&
      typeof matchMedia === 'function' &&
      matchMedia('(min-width: 1280px)').matches
    );
  }

  function syncWorkspaceSplitHeight() {
    if (!root?.isConnected) return;
    const workspace = root.querySelector('.call-workspace');
    if (!workspace) return;
    if (!workspace.classList.contains('has-inspector')) {
      workspace.style.removeProperty('--call-split-height');
      return;
    }
    const rect = workspace.getBoundingClientRect();
    const minH = Math.min(360, Math.max(240, window.innerHeight - 48));
    // Compute scroll-invariant document offset so outer window scrolling never resizes the split pane.
    const naturalTop = Math.max(12, Math.round(rect.top + window.scrollY));
    const headerBudget = Math.min(naturalTop, Math.round(window.innerHeight * 0.36));
    const avail = Math.max(minH, Math.floor(window.innerHeight - headerBudget - 44));
    workspace.style.setProperty('--call-split-height', `${avail}px`);
  }

  function ensureWorkspaceVisible() {
    if (!root?.isConnected) return;
    const workspace = root.querySelector('.call-workspace');
    if (!workspace) return;
    const wsRect = workspace.getBoundingClientRect();
    if (wsRect.top < 8 || wsRect.top > window.innerHeight * 0.65) {
      window.scrollTo?.({
        top: Math.max(0, Math.round(window.scrollY + wsRect.top - 12)),
        behavior: 'instant',
      });
    }
  }

  function scrollRowIntoList(entryNode, { center = false } = {}) {
    if (!entryNode?.isConnected || !root?.isConnected) return;
    const listEl = root.querySelector('.call-log');
    if (!listEl || listEl.scrollHeight <= listEl.clientHeight + 1) return;
    const listRect = listEl.getBoundingClientRect();
    const rowRect = entryNode.getBoundingClientRect();
    const headerEl = root.querySelector('.call-table-header');
    const headerH =
      headerEl && getComputedStyle(headerEl).position === 'sticky' ? headerEl.offsetHeight : 0;
    const dayH = root.querySelector('.call-day-divider')?.offsetHeight || 0;
    const topSafeOffset = headerH + dayH + 6;
    const safeTop = listRect.top + topSafeOffset;
    const safeBottom = listRect.bottom - 8;
    if (center) {
      const usableH = Math.max(rowRect.height, listEl.clientHeight - topSafeOffset);
      const targetTop =
        listEl.scrollTop +
        (rowRect.top - listRect.top) -
        topSafeOffset -
        (usableH - rowRect.height) / 2;
      listEl.scrollTop = Math.max(0, Math.round(targetTop));
      return;
    }
    if (rowRect.top < safeTop) {
      listEl.scrollTop = Math.max(0, Math.round(listEl.scrollTop - (safeTop - rowRect.top)));
    } else if (rowRect.bottom > safeBottom) {
      listEl.scrollTop = Math.round(listEl.scrollTop + (rowRect.bottom - safeBottom));
    }
  }

  function closeInspector({ restoreFocus = false, keepInflight = false } = {}) {
    const prevId = st().inspectedId;
    st().inspectedId = '';
    if (!keepInflight && prevId) abortDetailIfUnused(prevId);
    if (!root?.isConnected) return;
    const workspace = root.querySelector('.call-workspace');
    workspace?.classList.remove('has-inspector');
    workspace?.style.removeProperty('--call-split-height');
    const drawer = root.querySelector('.call-inspector');
    if (drawer) {
      drawer.hidden = true;
      drawer.innerHTML = '';
      delete drawer.dataset.inspectorId;
    }
    root
      .querySelectorAll('.call-entry.is-selected')
      .forEach((n) => n.classList.remove('is-selected'));
    if (restoreFocus && prevId) {
      const sum = root.querySelector(`[data-call-id="${CSS.escape(prevId)}"] > summary`);
      if (sum && sum.offsetParent !== null) sum.focus({ preventScroll: true });
    }
  }

  function updateInspectorNavButtons() {
    const drawer = root?.querySelector('.call-inspector');
    if (!drawer || drawer.hidden || !st().inspectedId) return;
    const ids = visibleEntryIds();
    const idx = ids.indexOf(st().inspectedId);
    const hasPrev = idx > 0;
    const hasNext = idx >= 0 && idx < ids.length - 1;
    const prevBtn = drawer.querySelector('[data-call-action="inspector-prev"]');
    const nextBtn = drawer.querySelector('[data-call-action="inspector-next"]');
    const closeBtn = drawer.querySelector('[data-call-action="inspector-close"]');
    const prevFocused = document.activeElement === prevBtn;
    const nextFocused = document.activeElement === nextBtn;
    if (prevBtn) prevBtn.disabled = !hasPrev;
    if (nextBtn) nextBtn.disabled = !hasNext;
    if (prevFocused && !hasPrev) {
      (hasNext ? nextBtn : closeBtn)?.focus({ preventScroll: true });
    } else if (nextFocused && !hasNext) {
      (hasPrev ? prevBtn : closeBtn)?.focus({ preventScroll: true });
    }
  }

  function syncDetailLayoutMode() {
    if (!root?.isConnected) return;
    if (useInspectorDrawer()) {
      let pickId = st().inspectedId;
      for (const id of [...opened]) {
        const node = root.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
        if (node) node.open = false;
        if (!pickId) pickId = id;
      }
      opened.clear();
      if (pickId) openInspector(pickId).catch(() => {});
    } else if (st().inspectedId) {
      const id = st().inspectedId;
      closeInspector({ keepInflight: true });
      const node = root.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
      if (node) {
        node.open = true;
        opened.add(id);
        detail(node).catch(() => {});
      } else {
        abortDetailIfUnused(id);
      }
    }
  }

  function visibleEntryIds() {
    if (!root?.isConnected) return [];
    return [...root.querySelectorAll('.call-log .call-entry[data-call-id]')]
      .filter((n) => n.offsetParent !== null)
      .map((n) => n.dataset.callId);
  }

  function renderInspectorHeaderTitle(drawer, id) {
    const titleBox = drawer?.querySelector('.call-inspector-title');
    if (!titleBox) return;
    const row = st().rows.find((x) => x.id === id) || cache.get(id)?.data || { id, tool: '' };
    titleBox.innerHTML =
      `<span class="call-project${row.alias ? '' : ' call-project-unknown'}" style="--call-project-hue:${projectHues.get(projectKey(row)) ?? 270}"><strong>${esc(row.alias || '未记录')}</strong></span>` +
      `<strong class="mono">${esc(title(row))}</strong>` +
      `<span class="muted mono tiny">#${esc(id.slice(0, 8))}</span>`;
  }

  function ensureInspectorFrame(id) {
    const drawer = root?.querySelector('.call-inspector');
    if (!drawer) return null;
    if (!drawer.querySelector('.call-inspector-head')) {
      drawer.innerHTML =
        `<div class="call-inspector-head">` +
        `<div class="call-inspector-title"></div>` +
        `<div class="call-inspector-nav">` +
        `<button type="button" class="btn ghost small" data-call-action="inspector-prev" title="上一条 (↑ / k)">↑</button>` +
        `<button type="button" class="btn ghost small" data-call-action="inspector-next" title="下一条 (↓ / j)">↓</button>` +
        `<button type="button" class="btn ghost small" data-call-action="inspector-close" title="关闭抽屉 (Esc)">关闭</button>` +
        `</div></div>` +
        `<div class="call-inspector-body"><div class="call-detail"><p class="muted" role="status">正在加载调用详情…</p></div></div>`;
    }
    drawer.dataset.inspectorId = id;
    drawer.hidden = false;
    root.querySelector('.call-workspace')?.classList.add('has-inspector');
    renderInspectorHeaderTitle(drawer, id);
    updateInspectorNavButtons();
    syncWorkspaceSplitHeight();
    return drawer.querySelector('.call-detail');
  }

  async function openInspector(id, { force = false, manual = false } = {}) {
    if (!id || !root?.isConnected) return;
    if (!useInspectorDrawer()) {
      const node = root.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
      if (node) {
        node.open = true;
        opened.add(id);
        await detail(node, force, manual);
      }
      return;
    }
    const prevId = st().inspectedId;
    st().inspectedId = id;
    if (prevId && prevId !== id) abortDetailIfUnused(prevId);
    // Collapse any stale inline-expanded rows so table stays compact alongside the drawer.
    if (opened.size) {
      for (const openId of [...opened]) {
        const openNode = root.querySelector(`[data-call-id="${CSS.escape(openId)}"]`);
        if (openNode) openNode.open = false;
        opened.delete(openId);
        abortDetailIfUnused(openId);
      }
    }
    const burstAncestor = root
      .querySelector(`[data-call-id="${CSS.escape(id)}"]`)
      ?.closest?.('details.call-burst');
    if (burstAncestor && !burstAncestor.open) {
      burstAncestor.open = true;
      expandedBursts.add(burstAncestor.dataset.burstId);
    }
    root
      .querySelectorAll('.call-entry.is-selected')
      .forEach((n) => n.classList.remove('is-selected'));
    const workspace = root.querySelector('.call-workspace');
    const wasSplit = !!workspace?.classList.contains('has-inspector');
    const entryNode = root.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
    if (entryNode) entryNode.classList.add('is-selected');
    const drawer = root.querySelector('.call-inspector');
    const sameDrawer = !!(drawer && !drawer.hidden && drawer.dataset.inspectorId === id);
    const body = ensureInspectorFrame(id);
    if (!wasSplit && workspace) {
      ensureWorkspaceVisible();
      syncWorkspaceSplitHeight();
    }
    if (!sameDrawer && drawer) {
      const scroller = drawer.querySelector('.call-inspector-body');
      if (scroller) scroller.scrollTop = 0;
    }
    if (entryNode && (prevId !== id || !wasSplit)) {
      scrollRowIntoList(entryNode, { center: !wasSplit });
    }
    if (!body) return;
    const focusAction = manual ? document.activeElement?.dataset.callAction : null;
    const cached = cache.get(id)?.data;
    if (!force && cached) {
      if (entryNode) patchVisibleRowInPlace(entryNode, cached);
      renderDetailInto(body, cached, { force: false, manual, focusAction });
      return;
    }
    if (!sameDrawer || (!cached && !body.querySelector('.call-facts'))) {
      delete body.dataset.detailId;
      body._renderedHTML = '';
      body.innerHTML = '<p class="muted" role="status">正在加载调用详情…</p>';
    }
    const session = S.session,
      generation = epoch;
    try {
      const d = await fetchDetailData(id, { force, manual });
      if (
        !d ||
        session !== S.session ||
        generation !== epoch ||
        !root?.isConnected ||
        st().inspectedId !== id ||
        drawer?.dataset.inspectorId !== id
      )
        return;
      renderDetailInto(body, d, { force, manual, focusAction });
    } catch (e) {
      if (
        e?.name !== 'AbortError' &&
        session === S.session &&
        generation === epoch &&
        root?.isConnected &&
        st().inspectedId === id &&
        drawer?.dataset.inspectorId === id
      ) {
        if (cache.get(id)?.data) setSync('详情更新失败，可手动重试：' + e.message, 'reconnecting');
        else {
          delete body.dataset.detailId;
          body._renderedHTML = '';
          body.innerHTML = `<p class="form-note" role="alert">${esc(e.message)}</p><button type="button" class="btn small" data-call-action="detail-refresh">重试详情</button>`;
        }
      }
    }
  }

  function stepInspector(delta, { syncSummaryFocus = false } = {}) {
    const ids = visibleEntryIds();
    if (!ids.length) return;
    const curIdx = ids.indexOf(st().inspectedId);
    const nextIdx =
      curIdx < 0
        ? delta > 0
          ? 0
          : ids.length - 1
        : Math.max(0, Math.min(ids.length - 1, curIdx + delta));
    const nextId = ids[nextIdx];
    if (nextId && nextId !== st().inspectedId) {
      openInspector(nextId).catch(() => {});
      if (syncSummaryFocus && root?.isConnected) {
        const sum = root.querySelector(`[data-call-id="${CSS.escape(nextId)}"] > summary`);
        sum?.focus({ preventScroll: true });
      }
    }
  }

  async function refreshPinnedOp({ force = true, manual = false } = {}) {
    const id = st().pinnedOp?.id;
    if (!id || !root?.isConnected) return;
    const pinEl = root.querySelector('.call-pinned-inspector');
    const pinBody = pinEl?.querySelector('.call-detail');
    if (!pinBody) return;
    const focusAction = manual ? document.activeElement?.dataset.callAction : null;
    const session = S.session,
      space = S.space_id,
      generation = epoch;
    try {
      const d = await fetchDetailData(id, { force, manual });
      if (
        !d ||
        session !== S.session ||
        space !== S.space_id ||
        generation !== epoch ||
        !root?.isConnected ||
        st().pinnedOp?.id !== id
      )
        return;
      st().pinnedOp = d;
      renderDetailInto(pinBody, d, { force, manual, focusAction });
      syncWorkspaceSplitHeight();
    } catch (err) {
      if (manual) throw err;
    }
  }

  function onServerEvent(m, { ingested = false } = {}) {
    if (!m || typeof m !== 'object') return;
    if (!ingested) window.CodePierOperationStore?.ingestEvent(m);
    if (!root?.isConnected || S.page !== 'audit' || S.auditMode !== 'operations' || !st().live)
      return;
    if (m.type === 'operation' && m.data?.id) {
      const id = m.data.id;
      const node = root.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
      if (node) {
        patchVisibleRowInPlace(node, window.CodePierOperationStore?.get(id) || m.data);
        syncLiveTicker();
        if (st().inspectedId === id) {
          openInspector(id, { force: true }).catch(() => {});
        }
        if (opened.has(id) || ['failed', 'interrupted'].includes(m.data.state)) {
          triggerThrottledHeadRefresh();
        }
      } else if (!st().cursor) {
        triggerThrottledHeadRefresh();
      }
      if (st().pinnedOp?.id === id) {
        refreshPinnedOp({ force: true }).catch(() => {});
      }
      return;
    }
    if ((m.type === 'output' || m.type === 'trace') && m.data?.id) {
      const id = m.data.id;
      // AUD-03: Ignore output/trace completely when the entry is collapsed!
      if (!opened.has(id) && st().inspectedId !== id && st().pinnedOp?.id !== id) return;
      if (detailOutputTimers.has(id)) return;
      detailOutputTimers.set(
        id,
        setTimeout(() => {
          detailOutputTimers.delete(id);
          if (!st().live) return;
          if (st().pinnedOp?.id === id) {
            refreshPinnedOp({ force: true }).catch(() => {});
          }
          if (st().inspectedId === id) {
            openInspector(id, { force: true }).catch(() => {});
            return;
          }
          const node = root?.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
          if (node?.open) detail(node, true).catch(() => {});
        }, 400),
      );
      return;
    }
    if (m.type === 'device' || m.type === 'project') {
      triggerThrottledHeadRefresh();
    }
  }

  function onConnectionState(connected) {
    if (!connected) {
      window.CodePierOperationStore?.setConnectionHealth('reconnecting');
      if (root?.isConnected && st().live) {
        setSync(
          `连接暂不可用，保留${lastSyncedTime ? ' ' + lastSyncedTime : ''}现有记录；正在重连…`,
          'reconnecting',
        );
      }
    } else {
      window.CodePierOperationStore?.setConnectionHealth('live');
      consecutiveFailures = 0;
      if (root?.isConnected && st().live) refresh(true).catch(() => {});
    }
  }

  function cancelFocus() {
    focusTurn++;
    focusController?.abort();
    focusController = null;
  }

  async function focusOperation(id, { refreshPinned = false, manual = false } = {}) {
    if (!id || !root?.isConnected) return false;
    const prevPinnedId = st().pinnedOp?.id;
    cancelFocus();
    const request = new AbortController(),
      turn = focusTurn,
      target = root,
      session = S.session,
      space = S.space_id,
      generation = epoch;
    focusController = request;
    const current = () =>
      turn === focusTurn &&
      generation === epoch &&
      root === target &&
      target.isConnected &&
      session === S.session &&
      space === S.space_id &&
      S.page === 'audit' &&
      S.auditMode === 'operations';
    const burstWrap = [...root.querySelectorAll('.call-burst')].find((b) =>
      b.querySelector(`[data-call-id="${CSS.escape(id)}"]`),
    );
    if (burstWrap && !burstWrap.open) {
      burstWrap.open = true;
      expandedBursts.add(burstWrap.dataset.burstId);
    }
    const existing = root.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
    if (existing && !refreshPinned) {
      if (st().pinnedOp?.id === id) {
        st().pinnedOp = null;
        target.querySelector('.call-pinned-inspector')?.remove();
      }
      existing.classList.add('call-entry-highlight');
      setTimeout(() => existing.classList.remove('call-entry-highlight'), 1800);
      if (useInspectorDrawer()) {
        ensureWorkspaceVisible();
        await openInspector(id);
        scrollRowIntoList(existing, { center: true });
        return true;
      }
      existing.open = true;
      opened.add(id);
      existing.scrollIntoView({
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
        block: 'center',
      });
      await detail(existing);
      return true;
    }
    if (
      refreshPinned &&
      st().pinnedOp?.id === id &&
      target.querySelector('.call-pinned-inspector')
    ) {
      try {
        await refreshPinnedOp({ force: true, manual });
        return true;
      } finally {
        if (focusController === request) focusController = null;
      }
    }
    if (prevPinnedId && prevPinnedId !== id) abortDetailIfUnused(prevPinnedId);
    try {
      const d = await fetchDetailData(id, {
        force: true,
        manual,
        externalSignal: request.signal,
      });
      if (!d || !current()) return false;
      st().pinnedOp = d;
      const existingPin = target.querySelector('.call-pinned-inspector');
      const rendered = detailHTML(d);
      const markup = `<div class="panel call-pinned-inspector" data-cp-key="pinned:${esc(d.id)}"><div class="call-pinned-head"><strong>定位操作详情 · #${esc(d.id.slice(0, 12))}</strong><button type="button" class="btn ghost small" data-call-action="close-pinned">关闭定位</button></div><div class="call-detail" data-detail-id="${esc(d.id)}">${rendered}</div></div>`;
      if (existingPin) existingPin.outerHTML = markup;
      else target.querySelector('.call-stream-banner')?.insertAdjacentHTML('afterend', markup);
      const pinEl = target.querySelector('.call-pinned-inspector');
      const pinBody = pinEl?.querySelector('.call-detail');
      if (pinBody) {
        pinBody._renderedHTML = rendered;
        bindOutputScroll(pinBody);
      }
      pinEl?.scrollIntoView({
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
        block: 'nearest',
      });
      syncWorkspaceSplitHeight();
      return true;
    } catch (error) {
      if (!current() || error?.name === 'AbortError') return false;
      throw error;
    } finally {
      if (focusController === request) focusController = null;
    }
  }

  async function exportFilteredAll(format) {
    const p = params();
    p.delete('limit');
    p.delete('cursor');
    p.set('format', format);
    const session = S.session,
      space = S.space_id;
    const resp = await fetch('/api/call-log-export?' + p.toString(), {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: space ? { 'X-CodePier-Space': space } : {},
    });
    // Never hand a previous account's/Space's export to whoever is signed in now.
    if (session !== S.session || space !== S.space_id) return;
    if (!resp.ok) {
      let msg = `导出失败 (${resp.status})`;
      try {
        const err = await resp.json();
        msg = err?.error?.message || msg;
      } catch {
        // Fallback status message.
      }
      throw new Error(msg);
    }
    const text = await resp.text();
    if (session !== S.session || space !== S.space_id) return;
    const truncated = resp.headers.get('X-Export-Truncated') === 'true';
    const rowsCount = resp.headers.get('X-Export-Rows') || '';
    download(
      format === 'ndjson' ? 'codepier-call-log.ndjson' : 'codepier-call-log.csv',
      text,
      format === 'ndjson' ? 'application/x-ndjson' : 'text/csv;charset=utf-8',
    );
    toast(
      truncated
        ? `已导出前 ${rowsCount} 条脱敏记录（超出单次 10,000 条上限）`
        : `已导出 ${rowsCount || '全部'} 条筛选记录`,
    );
  }

  function reset() {
    return reloadList({ resetCursor: true, clearDetails: true });
  }

  function bindEntries() {
    if (!root?.isConnected) return;
    root.querySelectorAll('.call-burst').forEach((burstNode) => {
      burstNode.ontoggle = () => {
        if (burstNode.open) expandedBursts.add(burstNode.dataset.burstId);
        else expandedBursts.delete(burstNode.dataset.burstId);
        updateInspectorNavButtons();
      };
    });
    root.querySelectorAll('[data-call-id]').forEach((node) => {
      node.ontoggle = () => {
        const id = node.dataset.callId;
        if (node.open && useInspectorDrawer()) {
          node.open = false;
          opened.delete(id);
          openInspector(id).catch(() => {});
          return;
        }
        if (node.open) {
          opened.add(id);
          detail(node);
        } else {
          opened.delete(id);
          abortDetailIfUnused(id);
        }
      };
      if (node.open) {
        if (useInspectorDrawer()) {
          node.open = false;
        } else {
          const body = node.querySelector(':scope > .call-detail');
          if (body?.querySelector('.call-facts')) bindOutputScroll(body);
          detail(node);
        }
      }
    });
  }

  function closeFloatingMenus(exceptTarget = null) {
    if (!root?.isConnected) return false;
    let closedAny = false;
    root.querySelectorAll('details.tool-menu[open]').forEach((menu) => {
      if (!exceptTarget || !menu.contains(exceptTarget)) {
        menu.open = false;
        closedAny = true;
      }
    });
    const more = root.querySelector('#call-more-filters[open]');
    if (more && (!exceptTarget || !more.contains(exceptTarget))) {
      const moreBody = more.querySelector('.call-more-body');
      if (moreBody && getComputedStyle(moreBody).position === 'absolute') {
        more.open = false;
        st().moreFilters = false;
        closedAny = true;
      }
    }
    return closedAny;
  }

  function installKeyboardShortcuts() {
    if (keyListener) document.removeEventListener('keydown', keyListener);
    keyListener = (e) => {
      if (!root?.isConnected || S.page !== 'audit' || S.auditMode !== 'operations') return;
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
      const target = e.target;
      if (target?.matches?.('input,textarea,select,[contenteditable="true"]')) {
        if (e.key === 'Escape' && target.id === 'audit-query') target.blur();
        return;
      }
      if (document.querySelector('dialog[open], .modal')) return;
      if (e.key === 'Escape') {
        if (closeFloatingMenus()) {
          e.preventDefault();
          return;
        }
        if (st().inspectedId) {
          e.preventDefault();
          closeInspector({ restoreFocus: true });
          return;
        }
      }
      if (e.key === '/') {
        const searchInput = root.querySelector('#audit-query');
        if (searchInput) {
          e.preventDefault();
          searchInput.focus();
          searchInput.select?.();
        }
      } else if (
        (e.key === 'ArrowDown' || e.key === 'ArrowUp') &&
        st().inspectedId &&
        useInspectorDrawer()
      ) {
        if (
          target?.closest?.(
            'pre, .call-inspector-body, .call-pinned-inspector, #operation-queue, details.tool-menu, #call-more-filters',
          )
        )
          return;
        e.preventDefault();
        const fromListOrBody =
          !target ||
          target === document.body ||
          target === document.documentElement ||
          !!target.closest?.('.call-log');
        stepInspector(e.key === 'ArrowDown' ? 1 : -1, { syncSummaryFocus: fromListOrBody });
      } else if (e.key === 'j' || e.key === 'k') {
        if (target?.closest?.('#operation-queue, details.tool-menu, #call-more-filters')) return;
        if (st().inspectedId && useInspectorDrawer()) {
          e.preventDefault();
          const fromListOrBody =
            !target ||
            target === document.body ||
            target === document.documentElement ||
            !!target.closest?.('.call-log');
          stepInspector(e.key === 'j' ? 1 : -1, { syncSummaryFocus: fromListOrBody });
          return;
        }
        const summaries = [...root.querySelectorAll('.call-log .call-entry > summary')].filter(
          (s) => s.offsetParent !== null,
        );
        if (!summaries.length) return;
        e.preventDefault();
        const idx = summaries.indexOf(document.activeElement);
        const nextIdx =
          e.key === 'j'
            ? idx < 0
              ? 0
              : Math.min(summaries.length - 1, idx + 1)
            : idx <= 0
              ? 0
              : idx - 1;
        summaries[nextIdx]?.focus({ preventScroll: true });
        const focusedEntry = summaries[nextIdx]?.closest?.('.call-entry');
        if (focusedEntry && root.querySelector('.call-workspace.has-inspector')) {
          scrollRowIntoList(focusedEntry);
        }
      } else if (e.key === 'o') {
        const entry = document.activeElement?.closest?.('.call-entry[data-call-id]');
        if (entry && document.activeElement.tagName === 'SUMMARY') {
          e.preventDefault();
          if (useInspectorDrawer()) {
            const id = entry.dataset.callId;
            if (st().inspectedId === id) closeInspector({ restoreFocus: true });
            else openInspector(id).catch(() => {});
          } else {
            entry.open = !entry.open;
          }
        }
      } else if (e.key === 'r') {
        e.preventDefault();
        refresh(false).catch((err) => toast(err.message, true));
      }
    };
    document.addEventListener('keydown', keyListener);
  }

  function bind({ shellOnly = false } = {}) {
    const cachedInitialQueue = initialQueueData;
    initialQueueData = null;
    detach();
    root = document.querySelector('#page');
    if (!root) return;
    window.CodePierQueue?.setLive(st().live, { silent: true });
    window.CodePierQueue?.bind({
      initialData: cachedInitialQueue,
      managedPolling: shellOnly || !!cachedInitialQueue,
      deferInitial: shellOnly,
    });
    if (window.CodePierOperationStore) {
      unsubscribeStore = window.CodePierOperationStore.subscribe((ev) => {
        if (!root?.isConnected || ev?.type !== 'queue' || !ev.vanished?.length) return;
        for (const id of ev.vanished) {
          const row = st().rows.find((x) => x.id === id);
          if (row && active(row)) {
            triggerThrottledHeadRefresh();
            break;
          }
        }
      });
    }
    const update = (selector, fn) => {
      const input = root.querySelector(selector);
      if (input)
        input.onchange = (e) => {
          fn(e.target.value);
          reloadList({ resetCursor: true, clearDetails: true }).catch((err) =>
            toast(err.message, true),
          );
        };
    };
    update('#audit-source', (v) => {
      S.auditSource = v;
    });
    update('#call-trigger-source', (v) => {
      st().triggerSource = v;
    });
    update('#audit-status', (v) => {
      S.auditStatus = v;
      if (v && st().preset && ['errors', 'active'].includes(st().preset)) st().preset = '';
    });
    update('#call-project', (v) => {
      st().project = v;
    });
    update('#call-tool', (v) => {
      st().tool = v;
    });
    const more = root.querySelector('#call-more-filters');
    if (more) more.ontoggle = () => (st().moreFilters = more.open);

    const searchForm = root.querySelector('#call-search'),
      queryInput = root.querySelector('#audit-query'),
      clearBtn = root.querySelector('[data-call-action="clear-query"]');
    if (queryInput) {
      const scheduleInstantSearch = () => {
        if (clearBtn) clearBtn.hidden = !queryInput.value;
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          const nextQ = queryInput.value.trim();
          if (nextQ !== (S.auditQuery || '')) {
            S.auditQuery = nextQ;
            reloadList({ resetCursor: true, clearDetails: false }).catch((err) =>
              toast(err.message, true),
            );
          }
        }, 220);
      };
      queryInput.addEventListener('input', (e) => {
        if (clearBtn) clearBtn.hidden = !queryInput.value;
        if (!e.isComposing) scheduleInstantSearch();
      });
      queryInput.addEventListener('compositionend', scheduleInstantSearch);
    }
    if (clearBtn) {
      clearBtn.onkeydown = (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          action(clearBtn).catch((err) => toast(err.message, true));
        }
      };
    }
    if (searchForm) {
      searchForm.onsubmit = (e) => {
        e.preventDefault();
        clearTimeout(searchTimer);
        S.auditQuery = (queryInput?.value || '').trim();
        reloadList({ resetCursor: true, clearDetails: true }).catch((err) =>
          toast(err.message, true),
        );
      };
    }

    bindEntries();
    const initialPinnedBody = root.querySelector('.call-pinned-inspector .call-detail');
    if (initialPinnedBody) bindOutputScroll(initialPinnedBody);

    // Hover & keyboard focus 120ms intent prefetch on <summary>.
    const listContainer = root.querySelector('.call-log');
    if (listContainer) {
      const startIntent = (e) => {
        const sum = e.target.closest?.('.call-entry > summary');
        const entryNode = sum?.parentElement;
        if (!entryNode || entryNode.open) return;
        const id = entryNode.dataset.callId;
        if (!id || cache.has(id) || detailInflight.has(id)) return;
        clearTimeout(hoverTimer);
        hoverTimer = setTimeout(() => prefetchDetail(id), 120);
      };
      const cancelIntent = () => clearTimeout(hoverTimer);
      listContainer.addEventListener('pointerover', startIntent);
      listContainer.addEventListener('pointerout', cancelIntent);
      listContainer.addEventListener('focusin', startIntent);
      listContainer.addEventListener('focusout', cancelIntent);
    }

    // Unified event delegation on root shell so dynamic list updates keep all handlers.
    const shell = root.querySelector('.call-audit-shell') || root;
    shell.oninput = (e) => {
      if (e.target.matches('.call-output-search')) {
        e.target.dataset.matchIndex = '0';
        findOutput(e.target.closest('[data-call-section="output"]'));
      }
    };
    shell.onclick = (e) => {
      closeFloatingMenus(e.target);
      const copyIdBtn = e.target.closest('[data-call-copy-id]');
      if (copyIdBtn) {
        if (copyIdBtn.disabled) return;
        e.preventDefault();
        e.stopPropagation();
        const fullId = copyIdBtn.dataset.callCopyId;
        Promise.resolve(copy(fullId))
          .then(() => {
            const orig = copyIdBtn.textContent;
            copyIdBtn.textContent = '✓ 已复制';
            setTimeout(() => {
              if (copyIdBtn.isConnected) copyIdBtn.textContent = orig;
            }, 1200);
          })
          .catch((err) => toast(err.message, true));
        return;
      }
      const jumpBtn = e.target.closest('[data-call-jump]');
      if (jumpBtn) {
        if (jumpBtn.disabled) return;
        e.preventDefault();
        e.stopPropagation();
        focusOperation(jumpBtn.dataset.callJump).catch((err) => toast(err.message, true));
        return;
      }
      const b = e.target.closest('[data-call-action],[data-call-copy]');
      if (b) {
        e.preventDefault();
        if (b.disabled || b.getAttribute('aria-disabled') === 'true') return;
        const menu = b.closest('details.tool-menu');
        if (menu) menu.open = false;
        action(b).catch((err) => toast(err.message, true));
        return;
      }
      const sum = e.target.closest('.call-entry[data-call-id] > summary');
      if (sum && useInspectorDrawer()) {
        e.preventDefault();
        const id = sum.parentElement.dataset.callId;
        if (st().inspectedId === id) closeInspector({ restoreFocus: true });
        else openInspector(id).catch((err) => toast(err.message, true));
      }
    };

    visListener = () => {
      if (!document.hidden && root?.isConnected && st().live) {
        syncLiveTicker();
        refresh(true).catch(() => {});
      } else {
        clearInterval(tickTimer);
        tickTimer = null;
      }
    };
    document.addEventListener('visibilitychange', visListener);
    if (typeof matchMedia === 'function') {
      wideMql = matchMedia('(min-width: 1280px)');
      wideMqlHandler = () => syncDetailLayoutMode();
      if (wideMql.addEventListener) wideMql.addEventListener('change', wideMqlHandler);
      else wideMql.addListener?.(wideMqlHandler);
    }
    resizeListener = () => syncWorkspaceSplitHeight();
    window.addEventListener('resize', resizeListener);
    installKeyboardShortcuts();
    syncLiveTicker();
    if (!shellOnly) schedule();
    if (pendingFocusId) {
      const idToOpen = pendingFocusId;
      pendingFocusId = '';
      // Consume the one-shot deep link so later renders do not re-open it.
      syncUrlHash();
      focusOperation(idToOpen).catch(() => {});
    } else if (st().inspectedId && st().rows.length) {
      syncDetailLayoutMode();
    }
  }

  async function action(b) {
    const name = b.dataset.callAction,
      node = b.closest('[data-call-id]');
    if (b.dataset.callCopy) {
      const pinned = b.closest('.call-pinned-inspector') ? st().pinnedOp : null;
      const inspected = b.closest('.call-inspector') ? cache.get(st().inspectedId)?.data : null;
      const d = pinned || inspected || cache.get(node?.dataset.callId)?.data;
      if (!d) return;
      const v =
        b.dataset.callCopy === 'all'
          ? d
          : b.dataset.callCopy === 'arguments'
            ? d.args_summary
            : d[b.dataset.callCopy];
      return copy(typeof v === 'string' ? v : json(v));
    }
    if (name === 'inspector-close') {
      closeInspector({ restoreFocus: true });
      return;
    }
    if (name === 'inspector-prev') {
      stepInspector(-1);
      return;
    }
    if (name === 'inspector-next') {
      stepInspector(1);
      return;
    }
    if (name === 'find-output-next') {
      findOutput(b.closest('[data-call-section="output"]'), true);
      return;
    }
    if (name === 'scroll-output-bottom') {
      const pre = b.closest('[data-call-section="output"]')?.querySelector('pre');
      if (pre) {
        pre.scrollTop = pre.scrollHeight;
        b.hidden = true;
      }
      return;
    }
    if (name === 'close-pinned') {
      const prevPinId = st().pinnedOp?.id;
      cancelFocus();
      st().pinnedOp = null;
      if (prevPinId) abortDetailIfUnused(prevPinId);
      root?.querySelector('.call-pinned-inspector')?.remove();
      syncWorkspaceSplitHeight();
      return;
    }
    if (name === 'filter-project') {
      st().project = b.dataset.project || '';
      const sel = root?.querySelector('#call-project');
      if (sel) {
        if (st().project && ![...sel.options].some((o) => o.value === st().project)) {
          sel.add(new Option(st().project, st().project));
        }
        sel.value = st().project;
      }
      const keepOpen = !!b.closest('.call-inspector');
      return reloadList({ resetCursor: true, clearDetails: !keepOpen });
    }
    if (name === 'filter-tool') {
      st().tool = b.dataset.tool || '';
      const sel = root?.querySelector('#call-tool');
      if (sel) {
        if (st().tool && ![...sel.options].some((o) => o.value === st().tool)) {
          sel.add(new Option(st().tool, st().tool));
        }
        sel.value = st().tool;
      }
      const keepOpen = !!b.closest('.call-inspector');
      return reloadList({ resetCursor: true, clearDetails: !keepOpen });
    }
    if (name === 'clear-query') {
      const qInput = root?.querySelector('#audit-query');
      if (qInput) {
        qInput.value = '';
        qInput.focus();
      }
      S.auditQuery = '';
      b.hidden = true;
      return reloadList({ resetCursor: true, clearDetails: true });
    }
    if (name === 'preset') {
      st().preset = b.dataset.preset || '';
      if (st().preset === 'errors' || st().preset === 'active') {
        S.auditStatus = '';
        const stSel = root?.querySelector('#audit-status');
        if (stSel) stSel.value = '';
      }
      return reloadList({ resetCursor: true, clearDetails: true });
    }
    if (name === 'toggle-burst') {
      st().burst = !st().burst;
      b.setAttribute('aria-pressed', String(st().burst));
      syncUrlHash();
      const listEl = root?.querySelector('.call-log');
      if (listEl) {
        reconcileListDOM(listEl, buildRowsMarkup(st().rows));
        bindEntries();
        updateInspectorNavButtons();
      }
      return;
    }
    if (name === 'density') {
      st().density = b.dataset.density === 'table' ? 'table' : 'cards';
      syncUrlHash();
      const listEl = root?.querySelector('.call-log');
      if (listEl) listEl.classList.toggle('call-log-table', st().density === 'table');
      root?.querySelectorAll('[data-call-action="density"]').forEach((btn) => {
        const on = btn.dataset.density === st().density;
        btn.classList.toggle('is-active', on);
        btn.setAttribute('aria-pressed', String(on));
      });
      syncDetailLayoutMode();
      return;
    }
    if (name === 'detail-refresh') {
      if (b.closest('.call-pinned-inspector') && st().pinnedOp)
        return focusOperation(st().pinnedOp.id, { refreshPinned: true, manual: true });
      if (b.closest('.call-inspector') && st().inspectedId)
        return openInspector(st().inspectedId, { force: true, manual: true });
      return detail(node, true, true);
    }
    if (name === 'export')
      return download(
        'codepier-calls-page.json',
        json({ scope: 'visible_page', exported_at: new Date().toISOString(), calls: st().rows }),
        'application/json',
      );
    if (name === 'export-all-csv') return exportFilteredAll('csv');
    if (name === 'export-all-ndjson') return exportFilteredAll('ndjson');
    if (name === 'live') {
      st().live = !st().live;
      window.CodePierQueue?.setLive(st().live);
      b.setAttribute('aria-pressed', String(st().live));
      b.textContent = st().live ? '暂停实时更新' : '恢复实时更新';
      if (st().live) {
        window.CodePierOperationStore?.setConnectionHealth('live');
        setSync('实时更新已开启', 'live');
        syncLiveTicker();
        return refresh(true);
      }
      clearTimeout(timer);
      clearInterval(retryTicker);
      retryTicker = null;
      clearInterval(tickTimer);
      tickTimer = null;
      setSync('实时更新已暂停', 'paused');
      return;
    }
    if (name === 'refresh') return refresh(false);
    if (name === 'latest') {
      const listEl = root?.querySelector('.call-log');
      if (listEl) listEl.scrollTop = 0;
      if (!st().inspectedId) window.scrollTo?.({ top: 0, behavior: 'smooth' });
      return reloadList({ resetCursor: true, clearDetails: !st().inspectedId });
    }
    if (name === 'next' && st().next) {
      st().history.push(st().cursor);
      st().cursor = st().next;
    } else if (name === 'prev' && st().history.length) {
      st().cursor = st().history.pop();
    } else return;
    const listEl = root?.querySelector('.call-log');
    if (listEl) listEl.scrollTop = 0;
    return reloadList({ resetCursor: false, clearDetails: true });
  }

  return {
    html,
    renderShellHTML,
    bind,
    detach,
    clear,
    refresh,
    schedule,
    reset,
    load: () => reloadList({ initial: true }),
    onServerEvent,
    onConnectionState,
    focusOperation,
  };
})();
