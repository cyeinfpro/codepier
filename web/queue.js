'use strict';
// Ephemeral, authorized queue views and tab-memory OperationStore.
// Never persist operation data in Web Storage or IndexedDB.
if (!window.CodePierOperationStore) {
  window.CodePierOperationStore = (() => {
    let scopeKey = '',
      clockAnchor = null,
      lastSyncedAt = 0,
      health = 'live',
      lastError = '';
    const byId = new Map(),
      activeIds = new Set(),
      listeners = new Set();
    const isTerminal = (state) =>
      ['succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted'].includes(state);
    const isActive = (state) =>
      ['queued', 'running', 'reconnecting', 'cancelling', 'unknown'].includes(state);
    const currentKey = () =>
      S?.session ? `${S.session.user_id || 'u'}:${S.space_id || 'default'}` : '';
    function reset() {
      byId.clear();
      activeIds.clear();
      clockAnchor = null;
      lastSyncedAt = 0;
      health = 'live';
      lastError = '';
      scopeKey = currentKey();
    }
    function ensureScope() {
      const next = currentKey();
      if (next !== scopeKey) reset();
      return !!next;
    }
    function emit(event) {
      for (const fn of [...listeners]) {
        try {
          fn(event);
        } catch {
          // Listener isolation preserves store consistency.
        }
      }
    }
    function anchorClock(observedAt) {
      if (typeof observedAt === 'number' && Number.isFinite(observedAt) && observedAt > 0)
        clockAnchor = { serverNow: observedAt, monoNow: performance.now() };
    }
    function upsertRow(item) {
      if (!item || typeof item.id !== 'string') return null;
      const prev = byId.get(item.id) || {};
      const merged = { ...prev, ...item };
      byId.set(item.id, merged);
      if (isTerminal(merged.state)) activeIds.delete(item.id);
      else if (isActive(merged.state)) activeIds.add(item.id);
      return merged;
    }
    function ingestQueue(data) {
      if (!ensureScope() || !data || typeof data !== 'object') return;
      anchorClock(data.observed_at);
      lastSyncedAt = Date.now();
      health = 'live';
      lastError = '';
      const seenActive = new Set();
      const ops = Array.isArray(data.operations) ? data.operations : [];
      for (const item of ops) {
        if (item && typeof item.id === 'string') {
          seenActive.add(item.id);
          upsertRow(item);
        }
      }
      const vanished = [];
      if (data.complete !== false && !data.truncated) {
        for (const id of [...activeIds]) {
          if (!seenActive.has(id)) {
            activeIds.delete(id);
            vanished.push(id);
          }
        }
      }
      emit({ type: 'queue', operations: ops, vanished });
    }
    function ingestCallLog(data) {
      if (!ensureScope() || !data || typeof data !== 'object') return;
      anchorClock(data.observed_at);
      lastSyncedAt = Date.now();
      health = 'live';
      lastError = '';
      for (const list of [data.operations, data.updates]) {
        if (Array.isArray(list)) for (const item of list) upsertRow(item);
      }
      if (data.queue && typeof data.queue === 'object') ingestQueue(data.queue);
      emit({ type: 'call-log' });
    }
    function ingestEvent(message) {
      if (!ensureScope() || !message || typeof message !== 'object') return;
      if (message.type === 'operation' && message.data?.id) {
        if (typeof message.at === 'number' && Number.isFinite(message.at)) anchorClock(message.at);
        const merged = upsertRow(message.data);
        emit({ type: 'operation', id: message.data.id, row: merged, raw: message.data });
      } else if ((message.type === 'output' || message.type === 'trace') && message.data?.id) {
        emit({ type: message.type, id: message.data.id });
      }
    }
    function setConnectionHealth(nextHealth, errorText = '') {
      ensureScope();
      health = nextHealth;
      if (errorText) lastError = errorText;
      emit({ type: 'health', health, lastError, lastSyncedAt });
    }
    function liveElapsedMs(created, fallbackMs) {
      if (
        health === 'live' &&
        clockAnchor &&
        typeof created === 'number' &&
        Number.isFinite(created)
      ) {
        const nowSec = clockAnchor.serverNow + (performance.now() - clockAnchor.monoNow) / 1000;
        return Math.max(0, Math.round((nowSec - created) * 1000));
      }
      return fallbackMs;
    }
    function subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    }
    return {
      reset,
      ensureScope,
      ingestQueue,
      ingestCallLog,
      ingestEvent,
      setConnectionHealth,
      liveElapsedMs,
      subscribe,
      get: (id) => (ensureScope() ? byId.get(id) : undefined),
      activeSet: () => (ensureScope() ? new Set(activeIds) : new Set()),
      status: () => ({ health, lastSyncedAt, lastError, clockAnchor }),
    };
  })();
}

window.CodePierQueue = (() => {
  let root = null,
    epoch = 0,
    timer = null,
    controller = null,
    live = true,
    managed = false,
    stageFilter = '',
    truncated = false,
    completenessReason = '',
    lastSyncedClock = '',
    unsubscribeStore = null;
  const pending = new Set();
  const rows = new Map();
  const stages = { hub_queue: 'Hub 待投递', agent_waiting: 'Agent 等资源', running: '运行中' };
  const reasons = {
    resource_conflict: '资源冲突',
    hub_delivery: '等待 Hub 投递',
    agent_pending: '电脑已接收，等待执行',
    lane_capacity: '通道容量',
    project_limit: '项目配额',
    remote_target_limit: '远程目标并发额度',
    resource_pressure: '电脑资源压力',
    project_fairness: '项目公平轮转',
    waiting_resource: '等待资源',
    waiting_worker: '等待执行额度',
    waiting_project: '等待项目额度',
  };
  const list = (value) =>
    Array.isArray(value) ? value.filter((x) => x && typeof x === 'object') : [];
  const seconds = (value) =>
    Number.isFinite(value) ? Math.max(0, Math.round(value)) + ' 秒' : '未记录';
  const age = (value) => seconds(Number.isFinite(value) ? value / 1000 : NaN);
  const context = () => ({ session: S.session, space: S.space_id, page: S.page, epoch, root });
  const current = (c) =>
    c.session === S.session &&
    c.space === S.space_id &&
    c.page === S.page &&
    c.epoch === epoch &&
    c.root === root &&
    !!root?.isConnected;
  function detach() {
    epoch++;
    clearTimeout(timer);
    timer = null;
    controller?.abort();
    controller = null;
    unsubscribeStore?.();
    unsubscribeStore = null;
    root = null;
    managed = false;
    rows.clear();
    pending.clear();
    stageFilter = '';
    truncated = false;
    lastSyncedClock = '';
  }
  function placeholder() {
    return '<section class="panel queue-panel" id="operation-queue" data-cp-key="operation-queue" data-queue-mode="idle" aria-label="当前操作队列"><div class="panel-head queue-heading"><div class="queue-title-group"><h2>当前队列</h2><p class="queue-sync" role="status" data-queue-status>正在读取活动操作…</p></div><button class="btn ghost small" type="button" data-queue-refresh>刷新队列</button></div><div class="panel-body"><div data-queue-content></div><div data-queue-diagnostics></div></div></section>';
  }

  function waitingHTML(item) {
    const waiting = item.waiting;
    if (!waiting) return '';
    const blockers = list(waiting.blocked_by);
    return (
      '<div class="queue-wait"><p>' +
      esc(reasons[waiting.reason] || waiting.reason || '等待调度') +
      ' · ' +
      esc(waiting.lane || '通道未记录') +
      ' · 已等待 ' +
      esc(age(waiting.queue_age_ms)) +
      '</p>' +
      blockers
        .map((b) =>
          b.operation_id
            ? '<div class="queue-blocker"><span>阻塞者 ' +
              esc(b.tool || '操作') +
              ' · ' +
              esc(b.state || '状态未知') +
              ' · 已历时 ' +
              esc(seconds(b.elapsed_s)) +
              '</span><code>' +
              esc(b.summary || b.operation_id) +
              '</code><button type="button" class="btn ghost small" data-action="operation-detail" data-id="' +
              esc(b.operation_id) +
              '">查看阻塞者</button></div>'
            : '<p class="form-note">部分阻塞操作受限或不可用</p>',
        )
        .join('') +
      (waiting.blocker_visibility === 'restricted_or_unavailable' &&
      !blockers.some((b) => !b.operation_id)
        ? '<p class="form-note">部分阻塞操作受限或不可用</p>'
        : '') +
      '</div>'
    );
  }
  function liveHTML(value) {
    const concurrency = value?.concurrency || value?.result?.data?.concurrency;
    if (concurrency?.mode !== 'live') return '';
    return (
      '<div class="notice queue-live" role="note"><p>实时读取：读取时有进程可能写入重叠路径，内容不是一致快照；SHA 仍对应本次读取字节。</p>' +
      list(concurrency.overlapping_writers)
        .map(
          (writer) =>
            '<p>可见并行写入：' +
            esc(writer.tool || '操作') +
            ' ' +
            esc(writer.operation_id || writer.id || '') +
            '</p>',
        )
        .join('') +
      '</div>'
    );
  }
  function runHTML(value) {
    const data = value?.result?.data || value;
    const path = data?.run_directory;
    if (typeof path !== 'string' || !/^\.codepier\/runs\/[a-zA-Z0-9_-]{1,100}$/.test(path))
      return '';
    const artifacts = Array.isArray(data.snapshot_artifacts)
      ? data.snapshot_artifacts.filter((p) => typeof p === 'string').slice(0, 100)
      : [];
    return (
      '<section class="queue-run"><h3>本次操作产物目录</h3><code>' +
      esc(path) +
      '</code><button type="button" class="btn ghost small" data-queue-copy="' +
      esc(path) +
      '">复制相对路径</button>' +
      (artifacts.length
        ? '<p>已导出的快照产物：</p><ul>' +
          artifacts.map((p) => '<li><code>' + esc(p) + '</code></li>').join('') +
          '</ul>'
        : '') +
      '<p class="form-note">目录位于所选工作树内，按保留期清理。这里只复制位置，不开放受保护目录、不自动下载或分享文件；执行输出仍在本操作回执中。</p></section>'
    );
  }
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-queue-copy]');
    if (button && S.session && !button.disabled)
      copy(button.dataset.queueCopy).catch((error) => toast(error.message, true));
  });
  function formatLiveClock(item) {
    const ms = window.CodePierOperationStore
      ? window.CodePierOperationStore.liveElapsedMs(item.created, item.elapsed_ms)
      : item.elapsed_ms;
    if (!Number.isFinite(ms)) return '';
    return ms < 1000 ? Math.round(ms) + 'ms' : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's';
  }
  function card(item) {
    const summary = item.summary || item.id;
    const elapsedText = formatLiveClock(item);
    return (
      '<article class="queue-item" data-queue-id="' +
      esc(item.id) +
      '">' +
      '<div class="queue-item-head"><div class="queue-identity"><strong class="queue-project">' +
      esc(item.alias || '未记录项目') +
      '</strong><code class="queue-tool">' +
      esc(item.tool) +
      '</code><span class="queue-device">' +
      esc(item.device_name || '未记录电脑') +
      '</span></div>' +
      '<div class="queue-head-right">' +
      (elapsedText
        ? '<span class="queue-elapsed mono" data-live-created="' +
          esc(Number.isFinite(item.created) ? item.created : '') +
          '">' +
          esc(elapsedText) +
          '</span>'
        : '') +
      (item.queue_stage && stages[item.queue_stage] && ['queued', 'running'].includes(item.state)
        ? '<span class="badge" data-queue-state="' +
          esc(item.queue_stage) +
          '">' +
          esc(stages[item.queue_stage]) +
          '</span>'
        : badge(item.state)) +
      '</div></div>' +
      '<details class="queue-command"><summary aria-label="展开或收起参数摘要"><code class="queue-summary">' +
      esc(summary) +
      '</code><span class="queue-command-toggle" aria-hidden="true">参数 <span>⌄</span></span></summary>' +
      '<pre tabindex="0" aria-label="完整参数摘要">' +
      esc(summary) +
      '</pre></details>' +
      waitingHTML(item) +
      liveHTML(item) +
      '<div class="queue-item-footer"><code class="queue-operation-id" title="' +
      esc(item.id) +
      '">#' +
      esc(String(item.id).slice(0, 12)) +
      '</code><div class="actions">' +
      '<button type="button" class="btn ghost small" data-action="operation-detail" data-id="' +
      esc(item.id) +
      '">查看操作</button>' +
      (item.can_cancel
        ? '<button type="button" class="btn ghost small queue-cancel" data-queue-cancel="' +
          esc(item.id) +
          '"' +
          (pending.has(item.id) ? ' disabled' : '') +
          '>请求取消</button>'
        : '') +
      '</div></div></article>'
    );
  }
  function updateMode() {
    if (!root) return;
    const all = [...rows.values()];
    if (!all.length && !stageFilter && !truncated) root.dataset.queueMode = 'idle';
    else if (all.length > 3 || all.some((x) => x.queue_stage === 'agent_waiting'))
      root.dataset.queueMode = 'busy';
    else root.dataset.queueMode = 'compact';
  }
  function render() {
    const all = [...rows.values()];
    const items = stageFilter ? all.filter((item) => item.queue_stage === stageFilter) : all;
    return (
      '<div class="queue-overview" role="group" aria-label="按队列阶段筛选">' +
      [['', '全部活动'], ...Object.entries(stages)]
        .map(([key, title]) => {
          const count = key ? all.filter((item) => item.queue_stage === key).length : all.length;
          return (
            '<button type="button" class="queue-stage" data-queue-stage="' +
            key +
            '" data-zero="' +
            (count === 0) +
            '" aria-pressed="' +
            (stageFilter === key) +
            '"><span>' +
            title +
            '</span><strong>' +
            count +
            '</strong></button>'
          );
        })
        .join('') +
      '</div>' +
      '<div class="queue-list" tabindex="0" role="region" aria-label="活动操作列表">' +
      (items.map(card).join('') ||
        '<div class="queue-empty"><strong>' +
          (truncated
            ? '已读取范围内暂无匹配操作'
            : stageFilter
              ? '此阶段暂无操作'
              : '当前没有活动操作') +
          '</strong><span>' +
          (stageFilter ? '可切换到全部活动查看其他操作。' : '新的活动操作将在同步后显示。') +
          '</span></div>') +
      '</div>' +
      (truncated
        ? '<p class="form-note queue-limit">' +
          (completenessReason === 'bounded_authorization_scan'
            ? '可见范围扫描尚未完整；已读取结果不代表全部队列。'
            : '仅展示最早的 100 个可见活动操作，不代表全部队列。') +
          '</p>'
        : '')
    );
  }
  function canMutateDOM() {
    if (!root?.isConnected) return false;
    const target = root.querySelector('[data-queue-content]');
    if (!target) return false;
    const selection = window.getSelection();
    return (
      (!target.contains(document.activeElement) ||
        document.activeElement?.closest('[data-queue-stage]')) &&
      (!selection || selection.isCollapsed || !target.contains(selection.anchorNode))
    );
  }
  function paint() {
    if (!root?.isConnected) return;
    updateMode();
    const target = root.querySelector('[data-queue-content]');
    if (!target) return;
    const focusedStage = document.activeElement?.closest('[data-queue-stage]')?.dataset.queueStage;
    const expanded = new Set(
      [...target.querySelectorAll('.queue-command[open]')].map(
        (node) => node.closest('[data-queue-id]').dataset.queueId,
      ),
    );
    const scroll = target.querySelector('.queue-list')?.scrollTop || 0;
    target.innerHTML = render();
    target.querySelectorAll('.queue-command').forEach((node) => {
      node.open = expanded.has(node.closest('[data-queue-id]').dataset.queueId);
    });
    const listEl = target.querySelector('.queue-list');
    if (listEl) listEl.scrollTop = scroll;
    if (focusedStage !== undefined)
      target.querySelectorAll('[data-queue-stage]').forEach((node) => {
        if (node.dataset.queueStage === focusedStage) node.focus({ preventScroll: true });
      });
  }
  function applySnapshot(data) {
    if (!root?.isConnected || !data) return false;
    window.CodePierOperationStore?.ingestQueue(data);
    let applied = false;
    if (canMutateDOM()) {
      rows.clear();
      list(data.operations).forEach((item) => rows.set(item.id, item));
      truncated = !!data.truncated || data.complete === false;
      completenessReason = data.completeness_reason || '';
      paint();
      applied = true;
    }
    delete root.dataset.stale;
    lastSyncedClock = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    const statusEl = root.querySelector('[data-queue-status]');
    if (statusEl) {
      statusEl.textContent =
        (applied ? '当前空间可见操作 · 已同步 ' : '已取得新状态，当前交互结束后刷新 · ') +
        lastSyncedClock +
        (live ? '' : ' · 自动更新已暂停');
    }
    const diagnostics = root.querySelector('[data-queue-diagnostics]');
    if (diagnostics) {
      if (!data.diagnostics_available) diagnostics.replaceChildren();
      if (data.diagnostics_available && !diagnostics.firstChild)
        diagnostics.innerHTML =
          '<details class="queue-diagnostics"><summary>实例队列诊断（24 小时）</summary><button type="button" class="btn ghost small" data-queue-report>读取诊断</button><div data-queue-report-content></div></details>';
    }
    return applied;
  }
  function schedule() {
    clearTimeout(timer);
    if (live && !managed && root?.isConnected) timer = setTimeout(refresh, 5000);
  }
  async function refresh() {
    if (!root?.isConnected || controller) return;
    const c = context(),
      request = new AbortController();
    controller = request;
    try {
      const data = await api('/api/operations/queue', { signal: request.signal });
      if (!current(c)) return;
      applySnapshot(data);
    } catch (error) {
      if (current(c) && error.name !== 'AbortError') {
        if ([401, 403, 404].includes(error.status)) {
          rows.clear();
          delete root.dataset.stale;
          root.dataset.queueMode = 'idle';
          root.querySelector('[data-queue-content]')?.replaceChildren();
          root.querySelector('[data-queue-diagnostics]')?.replaceChildren();
        } else {
          root.dataset.stale = 'true';
        }
        const statusEl = root.querySelector('[data-queue-status]');
        if (statusEl) {
          statusEl.textContent =
            '队列暂不可用：' +
            error.message +
            (lastSyncedClock && ![401, 403, 404].includes(error.status)
              ? ' · 以下为 ' + lastSyncedClock + ' 快照'
              : '');
        }
      }
    } finally {
      if (controller === request) controller = null;
      if (current(c)) schedule();
    }
  }
  function metricPillHTML(label, value) {
    if (!value || typeof value !== 'object')
      return `<div class="queue-diag-metric"><span>${esc(label)}</span><strong>未记录</strong></div>`;
    const p50 = value.p50 ?? '—',
      p99 = value.p99 ?? '—',
      warn = typeof value.p99 === 'number' && value.p99 >= 50;
    return `<div class="queue-diag-metric${warn ? ' is-warn' : ''}"><span>${esc(label)}</span><strong>p50 ${esc(p50)} / p99 ${esc(p99)} ms</strong></div>`;
  }
  function reasonBarsHTML(waitHours) {
    if (!waitHours || typeof waitHours !== 'object')
      return '<p class="muted tiny">暂无分段等待记录</p>';
    const entries = Object.entries(waitHours)
      .filter(([, v]) => typeof v === 'number' && v > 0)
      .sort((a, b) => b[1] - a[1]);
    if (!entries.length) return '<p class="muted tiny">过去 24 小时无显著排队等待</p>';
    const maxVal = Math.max(...entries.map(([, v]) => v), 0.001);
    return (
      '<div class="queue-diag-bars">' +
      entries
        .slice(0, 5)
        .map(([reasonKey, hours]) => {
          const pct = Math.max(4, Math.round((hours / maxVal) * 100));
          const secs =
            hours < 0.02 ? (hours * 3600).toFixed(1) + ' 秒' : hours.toFixed(2) + ' 小时';
          return `<div class="queue-diag-bar-row"><span>${esc(reasons[reasonKey] || reasonKey)}</span><div class="queue-diag-bar-track"><i style="width:${pct}%"></i></div><strong class="mono">${esc(secs)}</strong></div>`;
        })
        .join('') +
      '</div>'
    );
  }
  function rootBlockersHTML(blockers) {
    const items = list(blockers);
    if (!items.length) return '<p class="muted tiny">过去 24 小时无显著根阻塞操作</p>';
    return (
      '<div class="queue-diag-blockers">' +
      items
        .slice(0, 5)
        .map((b) => {
          const opId = String(b.operation_id || b.id || '');
          const label = opId ? '#' + opId.slice(0, 10) : '受限操作';
          const detail = [
            b.tool,
            Number.isFinite(b.blocked_operations) ? `阻塞 ${b.blocked_operations} 项` : '',
          ]
            .filter(Boolean)
            .join(' · ');
          return opId
            ? `<button type="button" class="btn ghost small queue-diag-blocker-chip" data-action="operation-detail" data-id="${esc(opId)}"><code>${esc(label)}</code><span>${esc(detail || '定位详情')}</span></button>`
            : `<span class="queue-diag-blocker-chip"><code>${esc(label)}</code></span>`;
        })
        .join('') +
      '</div>'
    );
  }
  function reportHTML(data) {
    const report = data.contention || {},
      metrics = data.metrics || {},
      alerts = report.alerts || {};
    const convoyPct = Number.isFinite(report.convoy_ratio)
      ? Math.min(100, Math.max(0, report.convoy_ratio * 100))
      : null;
    const convoyText = convoyPct !== null ? convoyPct.toFixed(1) + '%' : '未记录';
    const ringDash = convoyPct !== null ? `${(convoyPct * 0.88).toFixed(1)} 88` : '0 88';
    const over60 = Array.isArray(alerts.operations_over_60s)
      ? alerts.operations_over_60s.length
      : 0;
    const projOver5 = list(alerts.projects_over_5).length;
    const holdOver5 = list(alerts.holders_over_5).length;
    return (
      '<div class="queue-diag-grid">' +
      '<div class="queue-diag-card">' +
      '<h4>吞吐与车队阻塞</h4>' +
      '<div class="queue-diag-convoy">' +
      `<svg class="queue-diag-ring" viewBox="0 0 36 36" aria-hidden="true"><circle cx="18" cy="18" r="14" fill="none" stroke="var(--ui-line)" stroke-width="4"></circle><circle cx="18" cy="18" r="14" fill="none" stroke="${convoyPct && convoyPct >= 20 ? 'var(--ui-warning)' : 'var(--ui-accent)'}" stroke-width="4" stroke-dasharray="${ringDash}" stroke-linecap="round" transform="rotate(-90 18 18)"></circle></svg>` +
      '<div><p class="queue-diag-kpi">过去 24 小时：<strong>' +
      esc(report.operations ?? '未记录') +
      '</strong> 个操作 · 慢等待（&gt;10s）<strong>' +
      esc(report.waits_over_10s ?? '未记录') +
      '</strong> 个</p><p class="queue-diag-sub">车队占比 <strong>' +
      esc(convoyText) +
      '</strong></p></div></div>' +
      '<p class="queue-diag-alerts" role="status">告警：等待超过 60 秒 ' +
      over60 +
      '；项目等待超过 5 个 ' +
      projOver5 +
      '；阻塞超过 5 个操作的持有者 ' +
      holdOver5 +
      '。</p></div>' +
      '<div class="queue-diag-card"><h4>存储延迟与等待原因</h4><div class="queue-diag-metrics">' +
      metricPillHTML('数据库 worker 排队', metrics.worker?.queue_ms) +
      metricPillHTML('数据库 phase', metrics.worker?.phase_ms) +
      metricPillHTML('数据库锁等待', metrics.lock?.wait_ms) +
      '</div>' +
      reasonBarsHTML(report.wait_hours_by_reason) +
      '</div>' +
      '<div class="queue-diag-card"><h4>根阻塞者排行</h4>' +
      rootBlockersHTML(report.root_blockers) +
      '</div></div>' +
      '<p class="form-note">实例级聚合，仅实例管理员可读。受采样窗口、事件保留和并发状态变化影响，不代表完整执行时间。</p>'
    );
  }
  async function cancel(button) {
    const id = button.dataset.queueCancel,
      item = rows.get(id),
      c = context(),
      originFocus = document.activeElement;
    if (!item?.can_cancel || pending.has(id)) return;
    pending.add(id);
    button.disabled = true;
    let restoreFocus = false;
    try {
      const text =
        '请求取消 ' +
        (item.tool || '操作') +
        ' #' +
        id.slice(0, 12) +
        '？已发生的副作用不会回滚；运行中的命令需等待电脑确认。';
      if (typeof CP.ui?.confirmSheet !== 'function')
        throw new Error('确认组件未就绪，请刷新页面后重试。');
      const yes = await CP.ui.confirmSheet({
        title: '请求取消操作？',
        body: text,
        confirmLabel: '请求取消',
        danger: true,
      });
      restoreFocus = !yes;
      if (!yes || !current(c)) return;
      await tool('operations_cancel', { operation_id: id });
      if (current(c)) {
        root.querySelector('[data-queue-status]').textContent =
          '取消请求已提交，等待执行回执确认结果。';
        toast('取消请求已提交，等待执行回执确认结果。');
        button.blur();
        pending.delete(id);
        await refresh();
      }
    } catch (error) {
      if (current(c)) toast(error.message, true);
    } finally {
      if (current(c)) pending.delete(id);
      if (button.isConnected) {
        button.disabled = false;
        if (
          restoreFocus &&
          current(c) &&
          (document.activeElement === document.body || document.activeElement === originFocus)
        )
          button.focus({ preventScroll: true });
      }
    }
  }
  function reconcileEvent(event) {
    if (!root?.isConnected || event?.type !== 'operation' || !event.id) return;
    const state = event.raw?.state || event.row?.state;
    if (!state) return;
    if (['succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted'].includes(state)) {
      if (rows.has(event.id) && canMutateDOM()) {
        rows.delete(event.id);
        paint();
      }
    } else if (rows.has(event.id) && canMutateDOM()) {
      const existing = rows.get(event.id);
      rows.set(event.id, {
        ...existing,
        state,
        ...(event.raw?.queue_stage ? { queue_stage: event.raw.queue_stage } : {}),
      });
      paint();
    }
  }
  function bind(options = {}) {
    detach();
    root = document.querySelector('#operation-queue');
    if (!root) return;
    managed = !!options.managedPolling;
    if (window.CodePierOperationStore) {
      unsubscribeStore = window.CodePierOperationStore.subscribe(reconcileEvent);
    }
    root.addEventListener('click', async (event) => {
      const button = event.target.closest('button');
      if (!button || button.disabled) return;
      if (button.hasAttribute('data-queue-stage')) {
        stageFilter = button.dataset.queueStage;
        paint();
        root.querySelectorAll('[data-queue-stage]').forEach((node) => {
          if (node.dataset.queueStage === stageFilter) node.focus({ preventScroll: true });
        });
        return;
      }
      if (button.hasAttribute('data-queue-cancel')) return cancel(button);
      if (button.hasAttribute('data-queue-refresh')) return refresh();
      if (button.hasAttribute('data-queue-report')) {
        const c = context();
        button.disabled = true;
        try {
          const report = await api('/api/queue-diagnostics');
          const target = current(c) && root.querySelector('[data-queue-report-content]');
          if (target) target.innerHTML = reportHTML(report);
        } catch (error) {
          const target = current(c) && root.querySelector('[data-queue-report-content]');
          if (target) target.textContent = error.message;
        } finally {
          if (button.isConnected) button.disabled = false;
        }
      }
    });
    if (options.initialData && typeof options.initialData === 'object') {
      applySnapshot(options.initialData);
      schedule();
    } else {
      refresh();
    }
  }
  function hydrate(data) {
    if (root?.isConnected && data && typeof data === 'object') applySnapshot(data);
  }
  function setLive(value, options = {}) {
    live = !!value;
    if (live) {
      if (!options.silent && !managed) refresh();
    } else {
      clearTimeout(timer);
      timer = null;
    }
  }
  function setStale(errorMessage) {
    if (!root?.isConnected) return;
    root.dataset.stale = 'true';
    const statusEl = root.querySelector('[data-queue-status]');
    if (statusEl) {
      statusEl.textContent =
        '队列暂不可用：' +
        errorMessage +
        (lastSyncedClock ? ' · 以下为 ' + lastSyncedClock + ' 快照' : '');
    }
  }
  return {
    placeholder,
    bind,
    detach,
    setLive,
    refresh,
    hydrate,
    setStale,
    waitingHTML,
    liveHTML,
    runHTML,
  };
})();
