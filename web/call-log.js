'use strict';
// Details exist only in this authenticated tab's memory; never in Web Storage.
window.CodePierCallLog = (() => {
  let timer = null,
    root = null,
    inflight = null,
    epoch = 0,
    owner = null,
    detailTurn = 0;
  const cache = new Map(),
    opened = new Set();
  const st = () =>
    S.callLog ||
    (S.callLog = {
      cursor: '',
      history: [],
      next: null,
      project: '',
      tool: '',
      live: true,
      rows: [],
      incoming: false,
    });
  const active = (r) => ACTIVE_STATES.includes(r.state);
  const objects = (value) =>
    Array.isArray(value) ? value.filter((x) => x && typeof x === 'object') : [];
  const duration = (n) =>
    Number.isFinite(n)
      ? n < 1000
        ? Math.round(n) + ' 毫秒'
        : (n / 1000).toFixed(n < 10000 ? 2 : 1) + ' 秒'
      : '未记录';
  const stamp = (n) =>
    n
      ? new Date(n * 1000).toLocaleString('zh-CN', { hour12: false }) +
        '.' +
        String(Math.floor(n * 1000) % 1000).padStart(3, '0')
      : '未记录';
  const source = (r) =>
    r.actor?.startsWith('mcp:') ? 'MCP' : r.actor?.startsWith('panel:') ? '面板' : '其他';
  const group = (r) =>
    /^(fs_read|read|fs_tree|fs_search|search)/.test(r.tool)
      ? ['read', '读取']
      : /^(fs_write|write)/.test(r.tool)
        ? ['write', '写入']
        : /^(fs_edit|edit|fs_apply|apply)/.test(r.tool)
          ? ['edit', '编辑']
          : /^(shell_|ssh_|exec$|vps_exec|tasks_run|bash)/.test(r.tool)
            ? ['shell', '命令']
            : ['other', '工具'];
  const params = () =>
    new URLSearchParams({
      limit: '40',
      cursor: st().cursor,
      q: S.auditQuery || '',
      source: S.auditSource || '',
      status: S.auditStatus || '',
      project: st().project,
      tool: st().tool,
    });
  const key = () => params().toString();
  function ensureOwner() {
    if (owner !== S.session) {
      detach();
      cache.clear();
      opened.clear();
      S.callLog = null;
      owner = S.session;
    }
  }
  function detach() {
    clearTimeout(timer);
    timer = null;
    epoch++;
    inflight?.abort();
    inflight = null;
    root = null;
  }
  function clear() {
    detach();
    cache.clear();
    opened.clear();
    owner = null;
    S.callLog = null;
  }
  const summary = (r) =>
    `<code class="call-preview">${esc(r.summary || '没有可显示的参数摘要')}</code>`;
  const status = (r) =>
    `${badge(r.state)}<span class="call-duration">${active(r) ? '已历时' : '总历时'} ${duration(r.elapsed_ms)}</span>${r.exit_code !== null && r.exit_code !== undefined ? `<span>退出码 ${esc(r.exit_code)}</span>` : ''}`;
  function rowHTML(r) {
    const [tone, label] = group(r),
      saved = cache.get(r.id);
    return `<details class="call-entry call-${tone}" data-call-id="${esc(r.id)}" ${opened.has(r.id) ? 'open' : ''}><summary aria-label="展开调用 项目 ${esc(r.alias || '未记录')} ${esc(r.tool)} ${esc(r.id)}"><span class="call-main"><span class="call-top"><span class="call-project"><span class="call-project-label">项目</span><strong>${esc(r.alias || '未记录')}</strong></span><span class="call-tool"><strong class="mono">${esc(r.tool)}</strong><span class="call-kind">${label}</span></span><span class="call-state">${status(r)}</span></span><span class="call-args">${summary(r)}</span><span class="call-meta"><time>${esc(stamp(r.created))}</time><span>${esc(r.device_name || '设备未记录')}</span><span>${source(r)}</span><code>#${esc(r.id.slice(0, 12))}</code><span class="call-filter-note" hidden>状态已变化，不再符合当前筛选</span></span></span><span class="call-chevron" aria-hidden="true">⌄</span></summary><div class="call-detail">${saved?.data ? detailHTML(saved.data) : '<p class="muted" role="status">展开后加载参数、输出与执行链路。</p>'}</div></details>`;
  }
  function section(title, value, name, { truncated = false, open = false } = {}) {
    const text = typeof value === 'string' ? value : json(value);
    if (text === undefined || text === null || text === '') return '';
    return `<details class="call-section" data-call-section="${name}" ${open ? 'open' : ''}><summary>${esc(title)}<small>${text.length.toLocaleString()} 字符${truncated ? ' · 已截断' : ''}</small></summary><div class="call-section-tools"><button type="button" class="btn ghost small" data-call-copy="${name}">${icon('copy')}复制已脱敏内容</button></div><pre tabindex="0">${esc(text)}</pre></details>`;
  }
  function detailHTML(d) {
    const t = d.timing || {},
      trace = d.trace,
      events = objects(trace?.events);
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
                      `<button type="button" class="btn ghost small" data-insight="trace" data-id="${esc(b.operation_id)}">等待 ${esc(b.tool)} · #${esc(String(b.operation_id).slice(0, 12))}</button>`,
                  )
                  .join('')}</li>`,
            )
            .join('') || '<li>没有可用的阶段记录，可能由旧版 Agent 执行。</li>'
        }</ol><p class="form-note">只展示已保存的阶段，最多最近 200 项；Hub 时间是收到事件的时刻，不是同步的 Agent 时钟。</p></details>`
      : '';
    return `<div class="call-detail-toolbar"><span class="muted tiny">${esc(d.actor)}</span><button type="button" class="btn ghost small" data-call-copy="all">${icon('copy')}复制调用详情</button><button type="button" class="btn ghost small" data-call-action="detail-refresh">${icon('refresh')}刷新详情</button></div><dl class="call-facts"><div><dt>操作编号</dt><dd><code>${esc(d.id)}</code></dd></div><div><dt>总历时</dt><dd>${duration(d.elapsed_ms)}</dd></div><div><dt>执行前等待（Hub 观测）</dt><dd>${duration(t.wait_ms)}</dd></div><div><dt>实际执行（本次 Agent 计时）</dt><dd>${duration(t.execution_ms)}</dd></div><div><dt>投递次数</dt><dd>${esc(d.attempts ?? '未记录')}</dd></div><div><dt>当前阶段</dt><dd>${esc(trace?.current?.reason || '未记录')}</dd></div></dl>${d.error ? `<div class="notice call-error" role="note">${esc(typeof d.error === 'string' ? d.error : json(d.error))}</div>` : ''}${d.transport_error ? `<p class="form-note">连接状态：${esc(d.transport_error)}</p>` : ''}${d.display?.truncated ? '<p class="form-note">此详情是有长度限制的已脱敏副本；部分输出或结果已截断，不代表完整记录。</p>' : ''}${section('参数摘要', d.args_summary, 'arguments', { open: true, truncated: d.arguments_truncated })}${section('任务输出（保存的尾部）', d.output, 'output', { truncated: d.output_truncated, open: !!d.error })}${section('保存的返回结果', d.result, 'result', { truncated: d.display?.sections?.result })}${timeline}${d.trace_error ? `<p class="form-note">执行链路暂不可用：${esc(d.trace_error)}</p>` : ''}<p class="form-note">工具状态以执行回执为准；HTTP 200 仅表示请求被接收。未采集的耗时不补造，自动脱敏不能识别所有无标签的秘密值。</p>`;
  }
  async function html(seq) {
    ensureOwner();
    const session = S.session,
      filter = key(),
      r = await api('/api/call-log?' + filter);
    if (seq !== S.renderSeq || session !== S.session || filter !== key()) return '';
    st().rows = r.operations;
    st().next = r.next_cursor;
    st().incoming = false;
    const keep = new Set(r.operations.map((x) => x.id));
    for (const id of cache.keys()) if (!keep.has(id)) cache.delete(id);
    for (const id of opened) if (!keep.has(id)) opened.delete(id);
    const options = (rows, value, label) =>
      rows
        .map(
          (x) =>
            `<option value="${esc(x.id ?? x)}" ${(x.id ?? x) === value ? 'selected' : ''}>${esc(x[label] ?? x)}</option>`,
        )
        .join('');
    return (
      heading(
        '操作审计',
        'AUDIT TRAIL / OBSERVABILITY',
        '',
        `<button type="button" class="btn ghost" data-call-action="export">${icon('download')}导出本页调用</button>`,
      ) +
      `<div class="filters call-filters"><div class="tabs"><button class="active" data-action="audit-mode" data-mode="operations">工具执行</button><button data-action="audit-mode" data-mode="events">全部事件</button></div><select id="audit-source" aria-label="来源"><option value="">全部来源</option>${['mcp', 'panel'].map((x) => `<option value="${x}" ${S.auditSource === x ? 'selected' : ''}>${x.toUpperCase()}</option>`).join('')}</select><select id="audit-status" aria-label="操作状态"><option value="">全部状态</option>${[...ACTIVE_STATES, ...TERMINAL_STATES].map((x) => `<option value="${x}" ${S.auditStatus === x ? 'selected' : ''}>${esc(stateNames[x] || x)}</option>`).join('')}</select><select id="call-project" aria-label="筛选调用项目"><option value="">全部项目</option>${options(r.projects || [], st().project, 'alias')}</select><select id="call-tool" aria-label="筛选调用工具"><option value="">全部工具</option>${options(r.tools || [], st().tool, 'name')}</select><form id="call-search"><input id="audit-query" type="search" aria-label="搜索调用" maxlength="200" placeholder="搜索路径、命令、调用者或编号" value="${esc(S.auditQuery)}"><button type="submit" class="btn small">搜索</button></form></div><div class="call-controls"><button type="button" class="btn ghost small" data-call-action="live" aria-pressed="${st().live}">${st().live ? '暂停实时更新' : '恢复实时更新'}</button><button type="button" class="btn ghost small" data-call-action="refresh">${icon('refresh')}刷新</button><span id="call-sync" class="muted tiny" role="status">${st().cursor ? '正在查看历史页，不自动插入新调用' : st().live ? '实时更新已开启' : '实时更新已暂停'}</span><button type="button" id="call-new" class="btn small" data-call-action="latest" hidden>列表有更新，显示最新</button></div><section class="call-log" aria-label="工具调用流水">${r.operations.map(rowHTML).join('') || empty('当前筛选范围没有调用记录。')}</section><div class="pagination"><span id="call-count">本页 ${r.operations.length} 项 · 每页最多 40 项</span><div class="actions"><button type="button" class="btn ghost small" data-call-action="prev" ${st().history.length ? '' : 'disabled'}>上一页</button><button type="button" class="btn ghost small" data-call-action="next" ${r.next_cursor ? '' : 'disabled'}>下一页</button></div></div>${uiHelp('记录范围与隐私', '展示已保存的工具执行证据，不含完整对话；搜索仅覆盖已保存的摘要，导出仅包含当前页。参数正文仍按原有策略省略；密码、令牌、授权请求头和环境变量值默认脱敏。列表按创建时间及操作编号稳定分页；新调用不会挤动正在阅读的历史或已展开内容。旧记录缺少字段时显示未记录。')}`
    );
  }
  function schedule() {
    clearTimeout(timer);
    if (root?.isConnected && st().live) timer = setTimeout(() => refresh(true), 5000);
  }
  function setSync(text) {
    const node = root?.querySelector('#call-sync');
    if (node) node.textContent = text;
  }
  async function detail(node, force = false, manual = false) {
    if (!node?.isConnected || !node.open) return;
    const id = node.dataset.callId,
      old = cache.get(id);
    if (old?.loading || (!force && old?.data)) return;
    const session = S.session,
      generation = epoch,
      entry = { ...old, loading: true };
    cache.set(id, entry);
    if (!entry.data)
      node.querySelector('.call-detail').innerHTML =
        '<p class="muted" role="status">正在加载调用详情…</p>';
    try {
      const d = await api('/api/call-log/' + encodeURIComponent(id));
      if (
        session !== S.session ||
        generation !== epoch ||
        !node.isConnected ||
        cache.get(id) !== entry
      )
        return;
      entry.data = d;
      node.querySelector('.call-state').innerHTML = status(d);
      const body = node.querySelector('.call-detail'),
        selection = window.getSelection();
      // Do not replace selected text, a focused copy button or a nested fold.
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
          return [x.dataset.callSection, p ? [p.scrollTop, p.scrollLeft] : [0, 0]];
        }),
      );
      const rendered = detailHTML(d);
      if (rendered !== entry.rendered) {
        body.innerHTML = rendered;
        if (old?.data)
          body.querySelectorAll('details').forEach((x) => {
            x.open = expanded.has(x.dataset.callSection);
          });
        for (const section of body.querySelectorAll('[data-call-section]')) {
          const p = section.querySelector('pre'),
            offset = scrolls.get(section.dataset.callSection);
          if (p && offset) {
            p.scrollTop = offset[0];
            p.scrollLeft = offset[1];
          }
        }
        entry.rendered = rendered;
      }
      entry.needsRender = false;
      if (focusAction === 'detail-refresh')
        body.querySelector('[data-call-action="detail-refresh"]')?.focus({ preventScroll: true });
    } catch (e) {
      if (session === S.session && generation === epoch && node.isConnected) {
        if (entry.data) setSync('详情更新失败，可手动重试：' + e.message);
        else
          node.querySelector('.call-detail').innerHTML =
            `<p class="form-note" role="alert">${esc(e.message)}</p><button type="button" class="btn small" data-call-action="detail-refresh">重试详情</button>`;
      }
    } finally {
      entry.loading = false;
    }
  }
  async function refresh(automatic = false) {
    if (!root?.isConnected || S.page !== 'audit' || S.auditMode !== 'operations') return detach();
    if (inflight) return;
    if (automatic && (!st().live || document.hidden)) {
      schedule();
      return;
    }
    const controller = new AbortController();
    inflight = controller;
    const generation = epoch,
      session = S.session,
      filter = key();
    try {
      const watch = st()
        .rows.map((x) => x.id)
        .join(',');
      const r = await api(
        '/api/call-log?' + filter + '&include_filters=false&watch=' + encodeURIComponent(watch),
        { signal: controller.signal, retryDelays: [] },
      );
      if (
        generation !== epoch ||
        session !== S.session ||
        !root?.isConnected ||
        filter !== key() ||
        (automatic && !st().live)
      )
        return;
      const nodes = new Map(
        [...root.querySelectorAll('[data-call-id]')].map((n) => [n.dataset.callId, n]),
      );
      if (
        (r.operations.some((x) => !nodes.has(x.id)) ||
          [...nodes.keys()].some((id) => !r.operations.some((x) => x.id === id))) &&
        !st().cursor
      ) {
        st().incoming = true;
        root.querySelector('#call-new').hidden = false;
      }
      for (const item of [...r.operations, ...(r.updates || [])]) {
        const node = nodes.get(item.id);
        if (!node) continue;
        const previous = st().rows.find((x) => x.id === item.id),
          cached = cache.get(item.id)?.data;
        if (cached?.updated === item.updated) item.exit_code = cached.exit_code;
        Object.assign(previous || {}, item);
        const part = node.querySelector('.call-state'),
          markup = status(item);
        if (part.innerHTML !== markup) part.innerHTML = markup;
        const note = node.querySelector('.call-filter-note');
        if (note) note.hidden = !S.auditStatus || item.state === S.auditStatus;
      }
      // Rows may move out of the latest page. Open details are refreshed by ID.
      const live = [...nodes.values()].filter(
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
      for (let i = 0; i < Math.min(4, live.length); i++)
        await detail(live[(detailTurn + i) % live.length], true);
      if (
        generation !== epoch ||
        session !== S.session ||
        !root?.isConnected ||
        filter !== key() ||
        (automatic && !st().live)
      )
        return;
      detailTurn = live.length ? (detailTurn + 4) % live.length : 0;
      const selection = window.getSelection(),
        focused = document.activeElement;
      if (
        !st().cursor &&
        st().incoming &&
        !opened.size &&
        window.scrollY < 60 &&
        (!selection || selection.isCollapsed) &&
        (!root.contains(focused) || !focused?.matches('input,select,textarea,button,summary,pre'))
      ) {
        const keep = new Set(r.operations.map((x) => x.id));
        for (const id of cache.keys()) if (!keep.has(id)) cache.delete(id);
        root.querySelector('.call-log').innerHTML =
          r.operations.map(rowHTML).join('') || empty('当前筛选范围没有调用记录。');
        st().rows = r.operations;
        st().next = r.next_cursor;
        st().incoming = false;
        root.querySelector('#call-new').hidden = true;
        root.querySelector('#call-count').textContent =
          `本页 ${r.operations.length} 项 · 每页最多 40 项`;
        const next = root.querySelector('[data-call-action="next"]');
        if (next) next.disabled = !r.next_cursor;
        bindEntries();
      }
      setSync(
        (st().live ? '已同步 ' : '已手动同步 ') +
          new Date().toLocaleTimeString('zh-CN', { hour12: false }) +
          (st().cursor ? ' · 历史页' : ''),
      );
    } catch (e) {
      if (generation === epoch && session === S.session && e.name !== 'AbortError')
        setSync('连接暂不可用，保留现有记录；稍后重试。');
    } finally {
      if (inflight === controller) inflight = null;
      if (generation === epoch) schedule();
    }
  }
  function reset() {
    detach();
    st().cursor = '';
    st().history = [];
    st().next = null;
    cache.clear();
    opened.clear();
    S.auditOffset = 0;
    return renderPage(false);
  }
  function bindEntries() {
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
  function bind() {
    detach();
    root = document.querySelector('#page');
    if (!root) return;
    const update = (selector, fn) => {
      const input = root.querySelector(selector);
      if (input)
        input.onchange = (e) => {
          fn(e.target.value);
          reset().catch((e) => toast(e.message, true));
        };
    };
    update('#audit-source', (v) => {
      S.auditSource = v;
    });
    update('#audit-status', (v) => {
      S.auditStatus = v;
    });
    update('#call-project', (v) => {
      st().project = v;
    });
    update('#call-tool', (v) => {
      st().tool = v;
    });
    root.querySelector('#call-search').onsubmit = (e) => {
      e.preventDefault();
      S.auditQuery = root.querySelector('#audit-query').value.trim();
      reset().catch((err) => toast(err.message, true));
    };
    bindEntries();
    root.querySelectorAll('[data-call-action]').forEach((b) => {
      if (!b.closest('[data-call-id]'))
        b.onclick = () => action(b).catch((e) => toast(e.message, true));
    });
    root.querySelector('.call-log').onclick = (e) => {
      const b = e.target.closest('[data-call-action],[data-call-copy]');
      if (b) {
        e.preventDefault();
        action(b).catch((err) => toast(err.message, true));
      }
    };
    schedule();
  }
  async function action(b) {
    const name = b.dataset.callAction,
      node = b.closest('[data-call-id]');
    if (b.dataset.callCopy) {
      const d = cache.get(node?.dataset.callId)?.data;
      if (!d) return;
      const v =
        b.dataset.callCopy === 'all'
          ? d
          : b.dataset.callCopy === 'arguments'
            ? d.args_summary
            : d[b.dataset.callCopy];
      return copy(typeof v === 'string' ? v : json(v));
    }
    if (name === 'detail-refresh') return detail(node, true, true);
    if (name === 'export')
      return download(
        'codepier-calls-page.json',
        json({ scope: 'visible_page', exported_at: new Date().toISOString(), calls: st().rows }),
        'application/json',
      );
    if (name === 'live') {
      st().live = !st().live;
      b.setAttribute('aria-pressed', String(st().live));
      b.textContent = st().live ? '暂停实时更新' : '恢复实时更新';
      setSync(st().live ? '实时更新已开启' : '实时更新已暂停');
      if (st().live) return refresh(true);
      clearTimeout(timer);
      return;
    }
    if (name === 'refresh') return refresh(false);
    if (name === 'latest') return reset();
    if (name === 'next' && st().next) {
      st().history.push(st().cursor);
      st().cursor = st().next;
    } else if (name === 'prev' && st().history.length) st().cursor = st().history.pop();
    else return;
    detach();
    cache.clear();
    opened.clear();
    await renderPage(false);
  }
  return { html, bind, detach, clear, refresh, schedule, reset };
})();
