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
    visListener = null;
  const cache = new Map(),
    opened = new Set(),
    expandedBursts = new Set(),
    detailOutputTimers = new Map(),
    prefetchControllers = new Map(),
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
      density: 'cards',
      burst: true,
      live: true,
      rows: [],
      projects: [],
      tools: [],
      incoming: false,
      incomingCount: 0,
      incomingActive: 0,
      pinnedOp: null,
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
  function timeOnly(n) {
    if (!n) return '未记录';
    const d = new Date(n * 1000);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}.${pad3(Math.floor(n * 1000) % 1000)}`;
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
      return '';
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
    return `<span class="call-state-badge">${badge(r.state)}</span><span class="call-duration${durationTone(liveMs)}"${liveAttr}>${active(r) ? '已历时' : '总历时'} ${duration(liveMs)}</span>${r.exit_code !== null && r.exit_code !== undefined ? `<span class="call-exit">退出码 ${esc(r.exit_code)}</span>` : ''}`;
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
    const rawHash = typeof location !== 'undefined' ? location.hash.slice(1) : '';
    const qIdx = rawHash.indexOf('?');
    if (qIdx < 0) return;
    const hp = new URLSearchParams(rawHash.slice(qIdx + 1));
    if (hp.has('project')) st().project = hp.get('project').slice(0, 100);
    if (hp.has('tool')) st().tool = hp.get('tool').slice(0, 100);
    if (hp.has('status') && [...ACTIVE_STATES, ...TERMINAL_STATES, ''].includes(hp.get('status')))
      S.auditStatus = hp.get('status');
    if (hp.has('preset') && PRESET_DEFS.some(([k]) => k === hp.get('preset')))
      st().preset = hp.get('preset');
    if (hp.has('source') && ['', 'mcp', 'panel'].includes(hp.get('source')))
      S.auditSource = hp.get('source');
    if (
      hp.has('trigger') &&
      ['', 'system_maintenance', 'request', 'unknown'].includes(hp.get('trigger'))
    )
      st().triggerSource = hp.get('trigger');
    if (hp.has('density') && ['cards', 'table'].includes(hp.get('density')))
      st().density = hp.get('density');
    if (hp.has('burst')) st().burst = hp.get('burst') !== '0';
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
    if (st().density === 'table') hp.set('density', 'table');
    if (!st().burst) hp.set('burst', '0');
    const suffix = hp.toString();
    const targetHash = '#audit' + (suffix ? '?' + suffix : '');
    if (location.hash !== targetHash) {
      try {
        history.replaceState(null, '', location.pathname + location.search + targetHash);
      } catch {
        // Sandboxed about:blank harnesses may disallow replaceState.
      }
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
      S.callLog = null;
      ownerSession = S.session;
      ownerSpace = nextSpace;
    }
  }

  function detach() {
    window.CodePierQueue?.detach();
    clearTimeout(timer);
    clearInterval(tickTimer);
    clearTimeout(searchTimer);
    clearTimeout(hoverTimer);
    clearTimeout(progressTimer);
    clearTimeout(sseThrottleTimer);
    for (const t of detailOutputTimers.values()) clearTimeout(t);
    detailOutputTimers.clear();
    for (const c of prefetchControllers.values()) c.abort();
    prefetchControllers.clear();
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
    const [tone, label] = group(r),
      saved = cache.get(r.id),
      argsBlock = summaryHTML(r),
      errBlock = inlineErrorHTML(r),
      waitPill = contentionPillHTML(r),
      isFailed = ['failed', 'interrupted'].includes(r.state),
      isStaleActive =
        active(r) && window.CodePierOperationStore?.status().health === 'reconnecting';
    const showKindBadge = !OBVIOUS_TOOLS.has(r.tool);
    return `<details class="call-entry call-${tone}${argsBlock ? '' : ' call-entry-compact'}${isFailed ? ' call-entry-error' : ''}${isStaleActive ? ' call-entry-stale' : ''}" data-call-id="${esc(r.id)}" data-cp-key="call:${esc(r.id)}" ${opened.has(r.id) ? 'open' : ''}><summary aria-label="展开调用 项目 ${esc(r.alias || '未记录')} ${esc(r.tool)} ${esc(r.id)}"><span class="call-main"><span class="call-top"><span class="call-project${r.alias ? '' : ' call-project-unknown'}" style="--call-project-hue:${projectHues.get(projectKey(r)) ?? 270}"><strong>${esc(r.alias || '未记录')}</strong></span><span class="call-tool"><i class="call-tool-dot" aria-hidden="true"></i><strong class="mono">${esc(title(r))}</strong>${showKindBadge ? `<span class="call-kind">${label}</span>` : ''}${triggerChipHTML(r)}${modelLabel(r)}${waitPill}</span><span class="call-state">${status(r)}</span></span>${errBlock}${argsBlock}<span class="call-meta"><time title="${esc(stamp(r.created))}">${esc(timeOnly(r.created))}</time><span>${esc(r.device_name || '设备未记录')}</span><span>${source(r)}</span><span class="call-authorization">${authorizationHTML(r)}</span><button type="button" class="call-id-chip mono" data-call-copy-id="${esc(r.id)}" title="点击复制完整编号 ${esc(r.id)}">#${esc(r.id.slice(0, 12))}</button><span class="call-filter-note" ${!S.auditStatus || r.state === S.auditStatus ? 'hidden' : ''}>状态已变化，不再符合当前筛选</span></span></span><span class="call-chevron" aria-hidden="true">⌄</span></summary><div class="call-detail">${saved?.data ? detailHTML(saved.data) : '<p class="muted" role="status">展开后加载参数、输出与执行链路。</p>'}</div></details>`;
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
    const chunks = [];
    let currentDay = '';
    let i = 0;
    while (i < rows.length) {
      const r = rows[i];
      const dKey = dayKey(r.created);
      if (dKey && dKey !== currentDay) {
        currentDay = dKey;
        chunks.push(
          `<div class="call-day-divider" data-cp-key="day:${esc(dKey)}" role="separator"><span>${esc(dayHeading(dKey))}</span></div>`,
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
        if (groupRows.length >= 3 && !groupRows.some((item) => opened.has(item.id))) {
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
            `<details class="call-burst" data-burst-id="${esc(burstKey)}" data-cp-key="${esc(burstKey)}" ${expandedBursts.has(burstKey) ? 'open' : ''}><summary class="call-burst-summary"><span class="call-project${r.alias ? '' : ' call-project-unknown'}" style="--call-project-hue:${projectHues.get(projectKey(r)) ?? 270}"><strong>${esc(r.alias || '未记录')}</strong></span><span class="call-burst-title"><strong>${esc(authInfo.label)}</strong> 连续探查 ${groupRows.length} 次（${esc(breakdown)}）</span><span class="call-burst-meta mono">${duration(totalMs)} · 展开明细 ⌄</span></summary><div class="call-burst-items">${groupRows.map(rowHTML).join('')}</div></details>`,
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
    const tailMs = Math.max(0, total - (waitMs || 0) - (execMs || 0));
    return `<div class="call-waterfall" aria-label="耗时分布"><div class="call-waterfall-bar"><i class="cw-wait" style="width:${waitPct}%" title="排队与投递 ${duration(waitMs)}"></i><i class="cw-exec" style="width:${execPct}%" title="实际执行 ${duration(execMs)}"></i><i class="cw-tail" style="width:${tailPct}%" title="回传与落盘 ${duration(tailMs)}"></i></div><div class="call-waterfall-legend"><span><b class="cw-dot cw-wait"></b>排队等待 ${duration(waitMs)}</span><span><b class="cw-dot cw-exec"></b>实际执行 ${duration(execMs)}</span><span><b class="cw-dot cw-tail"></b>回传与落盘 ${duration(tailMs)}</span></div></div>`;
  }

  function section(
    secTitle,
    value,
    name,
    { truncated = false, open = false, liveFollow = false } = {},
  ) {
    const text = typeof value === 'string' ? value : json(value);
    if (text === undefined || text === null || text === '') return '';
    return `<details class="call-section" data-call-section="${name}" ${open ? 'open' : ''}><summary>${esc(secTitle)}<small>${text.length.toLocaleString()} 字符${truncated ? ' · 已截断' : ''}</small></summary><div class="call-section-tools">${liveFollow ? '<button type="button" class="btn ghost small call-jump-bottom" data-call-action="scroll-output-bottom" hidden>↓ 跳至最新输出</button>' : ''}<button type="button" class="btn ghost small" data-call-copy="${name}">${icon('copy')}复制已脱敏内容</button></div><pre tabindex="0">${esc(text)}</pre></details>`;
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
    return `<div class="call-detail-toolbar"><span class="muted tiny">${esc(d.actor)}</span>${d.project_id ? `<button type="button" class="btn ghost small" data-call-action="filter-project" data-project="${esc(d.project_id)}">筛选此项目</button>` : ''}${d.tool ? `<button type="button" class="btn ghost small" data-call-action="filter-tool" data-tool="${esc(d.tool)}">筛选此工具</button>` : ''}<button type="button" class="btn ghost small" data-call-copy="all">${icon('copy')}复制调用详情</button><button type="button" class="btn ghost small" data-call-action="detail-refresh">${icon('refresh')}刷新详情</button></div>${d.error ? `<div class="notice call-error" role="note">${esc(typeof d.error === 'string' ? d.error : json(d.error))}</div>` : ''}${d.transport_error ? `<p class="form-note">连接状态：${esc(d.transport_error)}</p>` : ''}${window.CodePierQueue?.waitingHTML(d) || ''}${window.CodePierQueue?.liveHTML(d) || ''}${window.CodePierQueue?.runHTML(d) || ''}${waterfallHTML(d)}${d.display?.truncated ? '<p class="form-note">此详情是有长度限制的已脱敏副本；部分输出或结果已截断，不代表完整记录。</p>' : ''}${diffSection}${section(isKnowledge(d) ? '程序输出（保存的尾部）' : '任务输出（保存的尾部）', d.output, 'output', { truncated: d.output_truncated, open: !!d.error, liveFollow: true })}${section('参数摘要', d.args_summary, 'arguments', { open: true, truncated: d.arguments_truncated })}${section('保存的返回结果', d.result, 'result', { truncated: d.display?.sections?.result })}<dl class="call-facts">${originFacts(d)}<div><dt>授权依据（调用时记录）</dt><dd class="call-authorization-detail">${authorizationHTML(d, true)}</dd></div><div><dt>操作编号</dt><dd><code>${esc(d.id)}</code></dd></div><div><dt>总历时</dt><dd>${duration(d.elapsed_ms)}</dd></div><div><dt>执行前等待（Hub 观测）</dt><dd>${duration(t.wait_ms)}</dd></div><div><dt>实际执行（本次 Agent 计时）</dt><dd>${duration(t.execution_ms)}</dd></div><div><dt>投递次数</dt><dd>${esc(d.attempts ?? '未记录')}</dd></div><div><dt>当前阶段</dt><dd>${esc(trace?.current?.reason || '未记录')}</dd></div></dl>${timeline}${d.trace_error ? `<p class="form-note">执行链路暂不可用：${esc(d.trace_error)}</p>` : ''}<p class="form-note">工具状态以执行回执为准；HTTP 200 仅表示请求被接收。未采集的耗时不补造，自动脱敏不能识别所有无标签的秘密值。</p>`;
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
      ? `<div class="panel call-pinned-inspector" data-cp-key="pinned:${esc(st().pinnedOp.id)}"><div class="call-pinned-head"><strong>定位操作详情 · #${esc(st().pinnedOp.id.slice(0, 12))}</strong><button type="button" class="btn ghost small" data-call-action="close-pinned">关闭定位</button></div><div class="call-detail">${detailHTML(st().pinnedOp)}</div></div>`
      : '';
    return (
      `<div class="call-audit-shell">` +
      heading('操作审计', '调用记录', '', syncPodHTML()) +
      (window.CodePierQueue?.placeholder() || '') +
      `<div class="filters call-filters"><div class="tabs"><button class="active" data-action="audit-mode" data-mode="operations">工具执行</button><button data-action="audit-mode" data-mode="events">全部事件</button></div><select id="call-project" aria-label="筛选调用项目"><option value="">全部项目</option>${options(projects, st().project, 'alias')}</select><select id="call-tool" aria-label="筛选调用工具"><option value="">全部工具</option>${options(tools, st().tool, 'name')}</select><select id="audit-status" aria-label="操作状态"><option value="">全部状态</option>${[...ACTIVE_STATES, ...TERMINAL_STATES].map((x) => `<option value="${x}" ${S.auditStatus === x ? 'selected' : ''}>${esc(stateNames[x] || x)}</option>`).join('')}</select><form id="call-search"><div class="call-search-box"><input id="audit-query" type="search" aria-label="搜索调用" maxlength="200" placeholder="搜索路径、命令、调用者或编号（/ 聚焦）" value="${esc(S.auditQuery || '')}"><span role="button" tabindex="0" class="call-search-clear" data-call-action="clear-query" aria-label="清空搜索词" ${S.auditQuery ? '' : 'hidden'}>×</span></div><button type="submit" class="btn small">搜索</button></form><details id="call-more-filters" class="call-more-filters"${(st().moreFilters ?? !!(S.auditSource || st().triggerSource)) ? ' open' : ''}><summary>更多筛选${[S.auditSource, st().triggerSource].filter(Boolean).length ? `<span class="call-more-count">${[S.auditSource, st().triggerSource].filter(Boolean).length}</span>` : ''}</summary><div class="call-more-body"><select id="audit-source" aria-label="来源"><option value="">全部来源</option>${['mcp', 'panel'].map((x) => `<option value="${x}" ${S.auditSource === x ? 'selected' : ''}>${x.toUpperCase()}</option>`).join('')}</select><select id="call-trigger-source" aria-label="触发来源"><option value="">全部触发来源</option>${[
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
      `<div class="call-subtoolbar"><div class="call-presets" role="group" aria-label="快捷筛选视图">${PRESET_DEFS.map(([presetKey, label]) => `<button type="button" class="call-preset-pill${st().preset === presetKey ? ' is-active' : ''}" data-call-action="preset" data-preset="${presetKey}" aria-pressed="${st().preset === presetKey}">${esc(label)}</button>`).join('')}</div><div class="call-view-toggles"><button type="button" class="call-chip-toggle" data-call-action="toggle-burst" aria-pressed="${st().burst}">聚合连续探查</button><div class="call-density-switch" role="group" aria-label="列表视图密度"><button type="button" class="call-chip-toggle${st().density === 'cards' ? ' is-active' : ''}" data-call-action="density" data-density="cards" aria-pressed="${st().density === 'cards'}">卡片</button><button type="button" class="call-chip-toggle${st().density === 'table' ? ' is-active' : ''}" data-call-action="density" data-density="table" aria-pressed="${st().density === 'table'}">紧凑表格</button></div></div></div>` +
      `<div class="call-stream-banner"><button type="button" id="call-new" class="btn small call-new-pill" data-call-action="latest" hidden>↑ 列表有更新，显示最新</button><div class="call-loading-bar" data-call-progress hidden aria-hidden="true"></div></div>` +
      pinnedMarkup +
      `<section class="call-log${st().density === 'table' ? ' call-log-table' : ''}" aria-label="工具调用流水">${rowsMarkup}</section>` +
      `<div class="pagination"><span id="call-count">${esc(countTextHTML(rowsLen))}</span><div class="actions"><button type="button" class="btn ghost small" data-call-action="prev" ${st().history.length ? '' : 'disabled'}>上一页</button><button type="button" class="btn ghost small" data-call-action="next" ${hasNext ? '' : 'disabled'}>下一页</button></div></div>` +
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

  // Keyed DOM patcher for .call-log so existing <details data-call-id> preserve DOM identity & open state.
  function reconcileListDOM(container, htmlString, { syncOpenState = false } = {}) {
    if (!container) return;
    if (typeof window.CP?.dom?.replacePage === 'function') {
      window.CP.dom.replacePage(container, htmlString);
      if (syncOpenState) {
        container.querySelectorAll('details[data-call-id]').forEach((node) => {
          const shouldOpen = opened.has(node.dataset.callId);
          if (node.open !== shouldOpen) node.open = shouldOpen;
        });
      }
      return;
    }
    const template = document.createElement('template');
    template.innerHTML = htmlString;
    const existingByKey = new Map();
    for (const child of [...container.children]) {
      const k = child.dataset?.cpKey || child.dataset?.callId;
      if (k) existingByKey.set(k, child);
    }
    let cursor = container.firstChild;
    const used = new Set();
    for (const incoming of [...template.content.children]) {
      const k = incoming.dataset?.cpKey || incoming.dataset?.callId;
      let target = k ? existingByKey.get(k) : null;
      if (target && target.tagName === incoming.tagName && !used.has(target)) {
        const nextOpen = syncOpenState ? opened.has(target.dataset.callId) : target.open;
        target.className = incoming.className;
        const newSum = incoming.querySelector(':scope > summary');
        const oldSum = target.querySelector(':scope > summary');
        if (newSum && oldSum && oldSum.innerHTML !== newSum.innerHTML) {
          oldSum.innerHTML = newSum.innerHTML;
          const aria = newSum.getAttribute('aria-label');
          if (aria) oldSum.setAttribute('aria-label', aria);
        }
        if (!nextOpen) {
          const detailBox = target.querySelector(':scope > .call-detail');
          if (detailBox)
            detailBox.innerHTML =
              '<p class="muted" role="status">展开后加载参数、输出与执行链路。</p>';
        }
        target.open = nextOpen;
      } else {
        target = incoming;
      }
      used.add(target);
      if (target !== cursor) container.insertBefore(target, cursor);
      cursor = target.nextSibling;
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
  async function reloadList({ resetCursor = false, clearDetails = false } = {}) {
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
      for (const c of prefetchControllers.values()) c.abort();
      prefetchControllers.clear();
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
      const r = await api('/api/call-log?' + filter + '&include_filters=false&include_total=true', {
        signal: controller.signal,
      });
      if (
        listController !== controller ||
        session !== S.session ||
        generation !== epoch ||
        !root?.isConnected
      )
        return;
      window.CodePierOperationStore?.ingestCallLog(r);
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
      for (const id of cache.keys()) if (!keep.has(id)) cache.delete(id);
      for (const id of opened) if (!keep.has(id)) opened.delete(id);
      reconcileListDOM(listEl, buildRowsMarkup(st().rows), { syncOpenState: clearDetails });
      const banner = root.querySelector('#call-new');
      if (banner) banner.hidden = true;
      updatePaginationControls();
      bindEntries();
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
      timer = setTimeout(() => refresh(true), pollDelayMs());
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
          el.textContent = '已历时 ' + duration(ms);
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

  // AUD-06: 120ms hover/focus intent prefetch (capped at 2 concurrent prefetches).
  async function prefetchDetail(id) {
    if (!id || cache.has(id) || prefetchControllers.has(id) || prefetchControllers.size >= 2)
      return;
    const session = S.session,
      generation = epoch,
      ctrl = new AbortController();
    prefetchControllers.set(id, ctrl);
    try {
      const d = await api('/api/call-log/' + encodeURIComponent(id), {
        signal: ctrl.signal,
        retryDelays: [],
      });
      if (session !== S.session || generation !== epoch || cache.has(id)) return;
      cache.set(id, { loading: false, data: d, rendered: '' });
    } catch {
      // Silent prefetch ignore.
    } finally {
      prefetchControllers.delete(id);
    }
  }

  async function detail(node, force = false, manual = false) {
    if (!node?.isConnected || !node.open) return;
    const id = node.dataset.callId;
    if (prefetchControllers.has(id)) {
      prefetchControllers.get(id).abort();
      prefetchControllers.delete(id);
    }
    const old = cache.get(id);
    if (old?.loading || (!force && old?.data)) {
      if (old?.data && !node.querySelector('.call-facts')) {
        const body = node.querySelector('.call-detail');
        const rendered = detailHTML(old.data);
        body.innerHTML = rendered;
        old.rendered = rendered;
      }
      return;
    }
    const session = S.session,
      generation = epoch,
      entry = { ...old, loading: true };
    cache.set(id, entry);
    if (!entry.data)
      node.querySelector('.call-detail').innerHTML =
        '<p class="muted" role="status">正在加载调用详情…</p>';
    try {
      let canIncremental =
        force && !manual && old?.data && active(old.data) && Number.isInteger(old.data.output_seq);
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
        session !== S.session ||
        generation !== epoch ||
        !node.isConnected ||
        cache.get(id) !== entry;
      let fetched = await api(detailUrl);
      if (stale()) return;
      if (canIncremental && !active(fetched)) {
        // Incremental reads omit the full trace, so final wait/execution timing
        // cannot be derived from them. Settle a finished call with one full read.
        fetched = await api('/api/call-log/' + encodeURIComponent(id));
        if (stale()) return;
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
      entry.data = d;
      node.querySelector('.call-state').innerHTML = status(d);
      const body = node.querySelector('.call-detail'),
        selection = window.getSelection();
      if (
        force &&
        !manual &&
        (body.contains(document.activeElement) ||
          (selection && !selection.isCollapsed && body.contains(selection.anchorNode)))
      ) {
        entry.needsRender = true;
        return;
      }
      const focusAction = manual ? document.activeElement?.dataset.callAction : null;
      const expanded = new Set(
        [...body.querySelectorAll('details[open]')].map((x) => x.dataset.callSection),
      );
      const scrolls = new Map(
        [...body.querySelectorAll('[data-call-section]')].map((x) => {
          const p = x.querySelector('pre');
          const nearBottom = p ? p.scrollHeight - p.scrollTop - p.clientHeight < 28 : false;
          return [
            x.dataset.callSection,
            p ? [p.scrollTop, p.scrollLeft, nearBottom, p.scrollHeight] : [0, 0, false, 0],
          ];
        }),
      );
      const rendered = detailHTML(d);
      if (rendered !== entry.rendered) {
        body.innerHTML = rendered;
        if (old?.data)
          body.querySelectorAll('details[data-call-section]').forEach((x) => {
            x.open = expanded.has(x.dataset.callSection);
          });
        for (const secNode of body.querySelectorAll('[data-call-section]')) {
          const p = secNode.querySelector('pre'),
            savedScroll = scrolls.get(secNode.dataset.callSection);
          if (p && savedScroll) {
            const [top, left, nearBottom, oldHeight] = savedScroll;
            if (
              secNode.dataset.callSection === 'output' &&
              nearBottom &&
              top > 0 &&
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
        entry.rendered = rendered;
      }
      entry.needsRender = false;
      if (focusAction === 'detail-refresh')
        body.querySelector('[data-call-action="detail-refresh"]')?.focus({ preventScroll: true });
    } catch (e) {
      if (session === S.session && generation === epoch && node.isConnected) {
        if (entry.data) setSync('详情更新失败，可手动重试：' + e.message, 'reconnecting');
        else
          node.querySelector('.call-detail').innerHTML =
            `<p class="form-note" role="alert">${esc(e.message)}</p><button type="button" class="btn small" data-call-action="detail-refresh">重试详情</button>`;
      }
    } finally {
      entry.loading = false;
    }
  }

  function patchVisibleRowInPlace(node, item) {
    if (!node) return;
    const previous = st().rows.find((x) => x.id === item.id),
      cached = cache.get(item.id)?.data;
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
    node.classList.remove('call-entry-stale');
    const part = node.querySelector('.call-state'),
      markup = status(merged);
    if (part && part.innerHTML !== markup) part.innerHTML = markup;
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
        if (node) patchVisibleRowInPlace(node, item);
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
        listEl = root.querySelector('.call-log');
      const interactiveFocusInsideList =
        listEl?.contains(focused) && focused?.matches('input,select,textarea,button,summary,pre');
      if (
        !st().cursor &&
        st().incoming &&
        !opened.size &&
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

  function onServerEvent(m) {
    if (!m || typeof m !== 'object') return;
    window.CodePierOperationStore?.ingestEvent(m);
    if (!root?.isConnected || S.page !== 'audit' || S.auditMode !== 'operations' || !st().live)
      return;
    if (m.type === 'operation' && m.data?.id) {
      const id = m.data.id;
      const node = root.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
      if (node) {
        patchVisibleRowInPlace(node, m.data);
        syncLiveTicker();
        if (opened.has(id) || ['failed', 'interrupted'].includes(m.data.state)) {
          triggerThrottledHeadRefresh();
        }
      } else if (!st().cursor) {
        triggerThrottledHeadRefresh();
      }
      return;
    }
    if ((m.type === 'output' || m.type === 'trace') && m.data?.id) {
      const id = m.data.id;
      // AUD-03: Ignore output/trace completely when the entry is collapsed!
      if (!opened.has(id)) return;
      if (detailOutputTimers.has(id)) return;
      detailOutputTimers.set(
        id,
        setTimeout(() => {
          detailOutputTimers.delete(id);
          const node = root?.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
          if (node?.open && st().live) detail(node, true).catch(() => {});
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

  async function focusOperation(id) {
    if (!id || !root?.isConnected) return false;
    const burstWrap = [...root.querySelectorAll('.call-burst')].find((b) =>
      b.querySelector(`[data-call-id="${CSS.escape(id)}"]`),
    );
    if (burstWrap && !burstWrap.open) {
      burstWrap.open = true;
      expandedBursts.add(burstWrap.dataset.burstId);
    }
    const existing = root.querySelector(`[data-call-id="${CSS.escape(id)}"]`);
    if (existing) {
      existing.open = true;
      opened.add(id);
      existing.classList.add('call-entry-highlight');
      setTimeout(() => existing.classList.remove('call-entry-highlight'), 1800);
      existing.scrollIntoView({ behavior: 'smooth', block: 'center' });
      await detail(existing);
      return true;
    }
    const d = await api('/api/call-log/' + encodeURIComponent(id));
    if (!root?.isConnected) return true;
    st().pinnedOp = d;
    const existingPin = root.querySelector('.call-pinned-inspector');
    const markup = `<div class="panel call-pinned-inspector" data-cp-key="pinned:${esc(d.id)}"><div class="call-pinned-head"><strong>定位操作详情 · #${esc(d.id.slice(0, 12))}</strong><button type="button" class="btn ghost small" data-call-action="close-pinned">关闭定位</button></div><div class="call-detail">${detailHTML(d)}</div></div>`;
    if (existingPin) existingPin.outerHTML = markup;
    else root.querySelector('.call-stream-banner')?.insertAdjacentHTML('afterend', markup);
    root
      .querySelector('.call-pinned-inspector')
      ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return true;
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
      };
    });
    root.querySelectorAll('[data-call-id]').forEach((node) => {
      node.ontoggle = () => {
        if (node.open) {
          opened.add(node.dataset.callId);
          detail(node);
        } else opened.delete(node.dataset.callId);
      };
      if (node.open) detail(node);
    });
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
      if (e.key === '/') {
        const searchInput = root.querySelector('#audit-query');
        if (searchInput) {
          e.preventDefault();
          searchInput.focus();
          searchInput.select?.();
        }
      } else if (e.key === 'j' || e.key === 'k') {
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
        summaries[nextIdx]?.focus();
      } else if (e.key === 'o') {
        const entry = document.activeElement?.closest?.('.call-entry[data-call-id]');
        if (entry && document.activeElement.tagName === 'SUMMARY') {
          e.preventDefault();
          entry.open = !entry.open;
        }
      } else if (e.key === 'r') {
        e.preventDefault();
        refresh(false).catch((err) => toast(err.message, true));
      }
    };
    document.addEventListener('keydown', keyListener);
  }

  function bind() {
    const cachedInitialQueue = initialQueueData;
    initialQueueData = null;
    detach();
    root = document.querySelector('#page');
    if (!root) return;
    window.CodePierQueue?.setLive(st().live, { silent: true });
    window.CodePierQueue?.bind({
      initialData: cachedInitialQueue,
      managedPolling: !!cachedInitialQueue,
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

    // Hover & keyboard focus 120ms intent prefetch on <summary>.
    const listContainer = root.querySelector('.call-log');
    if (listContainer) {
      const startIntent = (e) => {
        const sum = e.target.closest?.('.call-entry > summary');
        const entryNode = sum?.parentElement;
        if (!entryNode || entryNode.open) return;
        const id = entryNode.dataset.callId;
        if (!id || cache.has(id)) return;
        clearTimeout(hoverTimer);
        hoverTimer = setTimeout(() => prefetchDetail(id), 120);
      };
      const cancelIntent = () => clearTimeout(hoverTimer);
      listContainer.addEventListener('pointerover', startIntent);
      listContainer.addEventListener('pointerout', cancelIntent);
    }

    // Unified event delegation on root shell so dynamic list updates keep all handlers.
    const shell = root.querySelector('.call-audit-shell') || root;
    shell.onclick = (e) => {
      const copyIdBtn = e.target.closest('[data-call-copy-id]');
      if (copyIdBtn) {
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
        e.preventDefault();
        e.stopPropagation();
        focusOperation(jumpBtn.dataset.callJump).catch((err) => toast(err.message, true));
        return;
      }
      const b = e.target.closest('[data-call-action],[data-call-copy]');
      if (b) {
        e.preventDefault();
        const menu = b.closest('details.tool-menu');
        if (menu) menu.open = false;
        action(b).catch((err) => toast(err.message, true));
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
    installKeyboardShortcuts();
    syncLiveTicker();
    schedule();
    if (pendingFocusId) {
      const idToOpen = pendingFocusId;
      pendingFocusId = '';
      // Consume the one-shot deep link so later renders do not re-open it.
      syncUrlHash();
      focusOperation(idToOpen).catch(() => {});
    }
  }

  async function action(b) {
    const name = b.dataset.callAction,
      node = b.closest('[data-call-id]');
    if (b.dataset.callCopy) {
      const pinned = b.closest('.call-pinned-inspector') ? st().pinnedOp : null;
      const d = pinned || cache.get(node?.dataset.callId)?.data;
      if (!d) return;
      const v =
        b.dataset.callCopy === 'all'
          ? d
          : b.dataset.callCopy === 'arguments'
            ? d.args_summary
            : d[b.dataset.callCopy];
      return copy(typeof v === 'string' ? v : json(v));
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
      st().pinnedOp = null;
      root?.querySelector('.call-pinned-inspector')?.remove();
      return;
    }
    if (name === 'filter-project') {
      st().project = b.dataset.project || '';
      const sel = root?.querySelector('#call-project');
      if (sel) sel.value = st().project;
      return reloadList({ resetCursor: true, clearDetails: true });
    }
    if (name === 'filter-tool') {
      st().tool = b.dataset.tool || '';
      const sel = root?.querySelector('#call-tool');
      if (sel) sel.value = st().tool;
      return reloadList({ resetCursor: true, clearDetails: true });
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
      return;
    }
    if (name === 'detail-refresh') return detail(node, true, true);
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
      clearInterval(tickTimer);
      tickTimer = null;
      setSync('实时更新已暂停', 'paused');
      return;
    }
    if (name === 'refresh') return refresh(false);
    if (name === 'latest') {
      window.scrollTo?.({ top: 0, behavior: 'smooth' });
      return reloadList({ resetCursor: true, clearDetails: true });
    }
    if (name === 'next' && st().next) {
      st().history.push(st().cursor);
      st().cursor = st().next;
    } else if (name === 'prev' && st().history.length) {
      st().cursor = st().history.pop();
    } else return;
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
    onServerEvent,
    onConnectionState,
    focusOperation,
  };
})();
