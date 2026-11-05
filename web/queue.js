'use strict';
// Ephemeral, authorized queue views. Never persist operation data in Web Storage.
window.CodePierQueue = (() => {
  let root = null,
    epoch = 0,
    timer = null,
    controller = null,
    live = true,
    stageFilter = '',
    truncated = false;
  const pending = new Set();
  const rows = new Map();
  const stages = { hub_queue: 'Hub 待投递', agent_waiting: 'Agent 等资源', running: '运行中' };
  const reasons = {
    resource_conflict: '资源冲突',
    hub_delivery: '等待 Hub 投递',
    agent_pending: '节点已接收，等待执行',
    lane_capacity: '通道容量',
    project_limit: '项目配额',
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
    controller?.abort();
    controller = null;
    root = null;
    rows.clear();
    pending.clear();
    stageFilter = '';
    truncated = false;
  }
  function placeholder() {
    return '<section class="panel queue-panel" id="operation-queue" aria-label="当前操作队列"><div class="panel-head queue-heading"><div><h2>当前队列</h2><p class="queue-sync" role="status" data-queue-status>正在读取活动操作…</p></div><button class="btn ghost small" type="button" data-queue-refresh>刷新队列</button></div><div class="panel-body"><div data-queue-content></div><div data-queue-diagnostics></div></div></section>';
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
  function card(item) {
    const summary = item.summary || item.id;
    return (
      '<article class="queue-item" data-queue-id="' +
      esc(item.id) +
      '">' +
      '<div class="queue-item-head"><div class="queue-identity"><strong class="queue-project">' +
      esc(item.alias || '未记录项目') +
      '</strong><code class="queue-tool">' +
      esc(item.tool) +
      '</code><span class="queue-device">' +
      esc(item.device_name || '未记录节点') +
      '</span></div>' +
      badge(item.state) +
      '</div>' +
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
          (stageFilter ? '此阶段暂无操作' : '当前没有活动操作') +
          '</strong><span>' +
          (stageFilter ? '可切换到全部活动查看其他操作。' : '新的活动操作将在同步后显示。') +
          '</span></div>') +
      '</div>' +
      (truncated
        ? '<p class="form-note queue-limit">仅展示最早的 100 个可见活动操作，不代表全部队列。</p>'
        : '')
    );
  }
  function paint() {
    const target = root.querySelector('[data-queue-content]');
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
    target.querySelector('.queue-list').scrollTop = scroll;
    if (focusedStage !== undefined)
      target.querySelectorAll('[data-queue-stage]').forEach((node) => {
        if (node.dataset.queueStage === focusedStage) node.focus({ preventScroll: true });
      });
  }
  function schedule() {
    clearTimeout(timer);
    if (live && root?.isConnected) timer = setTimeout(refresh, 5000);
  }
  async function refresh() {
    if (!root?.isConnected || controller) return;
    const c = context(),
      request = new AbortController();
    controller = request;
    try {
      const data = await api('/api/operations/queue', { signal: request.signal });
      if (!current(c)) return;
      const target = root.querySelector('[data-queue-content]');
      // A refresh must not replace a pressed/focused button or selected evidence.
      const selection = window.getSelection();
      if (
        (!target.contains(document.activeElement) ||
          document.activeElement?.closest('[data-queue-stage]')) &&
        (!selection || selection.isCollapsed || !target.contains(selection.anchorNode))
      ) {
        rows.clear();
        list(data.operations).forEach((item) => rows.set(item.id, item));
        truncated = !!data.truncated;
        paint();
      }
      root.querySelector('[data-queue-status]').textContent =
        '当前空间可见操作 · 已同步 ' +
        new Date().toLocaleTimeString('zh-CN', { hour12: false }) +
        (live ? '' : ' · 自动更新已暂停');
      const diagnostics = root.querySelector('[data-queue-diagnostics]');
      if (!data.diagnostics_available) diagnostics.replaceChildren();
      if (data.diagnostics_available && !diagnostics.firstChild)
        diagnostics.innerHTML =
          '<details class="queue-diagnostics"><summary>实例队列诊断（24 小时）</summary><button type="button" class="btn ghost small" data-queue-report>读取诊断</button><div data-queue-report-content></div></details>';
    } catch (error) {
      if (current(c) && error.name !== 'AbortError') {
        if ([401, 403, 404].includes(error.status)) {
          rows.clear();
          root.querySelector('[data-queue-content]').replaceChildren();
          root.querySelector('[data-queue-diagnostics]').replaceChildren();
        }
        root.querySelector('[data-queue-status]').textContent = '队列暂不可用：' + error.message;
      }
    } finally {
      if (controller === request) controller = null;
      if (current(c)) schedule();
    }
  }
  function reportHTML(data) {
    const report = data.contention || {},
      metrics = data.metrics || {};
    const percentile = (value) =>
      value && typeof value === 'object'
        ? 'p50 ' + (value.p50 ?? '—') + ' / p99 ' + (value.p99 ?? '—') + ' ms'
        : '未记录';
    const alerts = report.alerts || {};
    return (
      '<p>过去 24 小时：' +
      esc(report.operations ?? '未记录') +
      ' 个操作；等待超过 10 秒 ' +
      esc(report.waits_over_10s ?? '未记录') +
      ' 个；车队占比 ' +
      (Number.isFinite(report.convoy_ratio)
        ? (report.convoy_ratio * 100).toFixed(1) + '%'
        : '未记录') +
      '</p>' +
      '<dl class="kv"><dt>数据库 worker 排队</dt><dd>' +
      esc(percentile(metrics.worker?.queue_ms)) +
      '</dd><dt>数据库 phase</dt><dd>' +
      esc(percentile(metrics.worker?.phase_ms)) +
      '</dd><dt>数据库锁等待</dt><dd>' +
      esc(percentile(metrics.lock?.wait_ms)) +
      '</dd></dl>' +
      '<p role="status">告警：等待超过 60 秒 ' +
      (Array.isArray(alerts.operations_over_60s) ? alerts.operations_over_60s.length : 0) +
      '；项目等待超过 5 个 ' +
      list(alerts.projects_over_5).length +
      '；阻塞超过 5 个操作的持有者 ' +
      list(alerts.holders_over_5).length +
      '。</p>' +
      '<details><summary>按原因的等待小时数与根阻塞者</summary><pre>' +
      esc(
        json({
          wait_hours_by_reason: report.wait_hours_by_reason,
          root_blockers: report.root_blockers,
        }),
      ) +
      '</pre></details>' +
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
        '？已发生的副作用不会回滚；运行中的命令需等待节点确认。';
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
        // The request has settled. A refreshed card must not inherit the
        // disabled state of the old button when live polling is paused.
        pending.delete(id);
        await refresh();
      }
    } catch (error) {
      if (current(c)) toast(error.message, true);
    } finally {
      if (current(c)) pending.delete(id);
      if (button.isConnected) {
        button.disabled = false;
        // The sheet cannot restore a disabled origin. Restore it only when
        // dismissal left focus unclaimed and the original queue is still live.
        if (
          restoreFocus &&
          current(c) &&
          (document.activeElement === document.body || document.activeElement === originFocus)
        )
          button.focus({ preventScroll: true });
      }
    }
  }
  function bind() {
    detach();
    root = document.querySelector('#operation-queue');
    if (!root) return;
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
    refresh();
  }
  function setLive(value) {
    live = !!value;
    if (live) refresh();
    else clearTimeout(timer);
  }
  return { placeholder, bind, detach, setLive, refresh, waitingHTML, liveHTML, runHTML };
})();
