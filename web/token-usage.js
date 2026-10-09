'use strict';
// Presentation of server-recorded, sanitized text estimates only. No tokenizer,
// request bodies, credentials, or host billing values are stored in this module.
window.CodePierTokenUsage = (() => {
  const VERSION = 'codepier-text-v1';
  const fields = ['estimated_tokens', 'low', 'high', 'characters', 'utf8_bytes'];
  const integer = (value) => (Number.isSafeInteger(value) && value >= 0 ? value : null);
  const number = (value) => integer(value)?.toLocaleString('zh-CN') ?? '未记录';
  const escape = (value) =>
    String(value).replace(
      /[&<>"']/g,
      (character) =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character],
    );

  function side(usage, direction) {
    const value = usage?.[direction];
    if (
      usage?.kind !== 'estimate' ||
      usage.version !== VERSION ||
      usage.scope !== 'project_tool_payload' ||
      !value ||
      !['available', 'partial'].includes(value.state) ||
      fields.some((field) => integer(value[field]) === null) ||
      value.low > value.estimated_tokens ||
      value.estimated_tokens > value.high
    )
      return null;
    return value;
  }

  function mergeActivities(current, incoming) {
    const rows = new Map();
    for (const row of [...current, ...incoming]) {
      if (row && integer(row.id) !== null && row.id > 0) rows.set(row.id, row);
    }
    // The server retains at most 10,000 activity records. Keep this view bounded.
    return [...rows.values()].slice(0, 10000);
  }

  function summarize(rows) {
    rows = mergeActivities([], rows);
    const result = {
      scope: 'loaded_activity_rows',
      wire_attempts: rows.length,
      distinct_server_operation_ids: new Set(
        rows.map((row) => row.operation_id).filter((id) => typeof id === 'string' && id),
      ).size,
      version: VERSION,
      actual_usage: null,
    };
    for (const direction of ['input', 'output']) {
      const total = Object.fromEntries(fields.map((field) => [field, 0]));
      let measured = 0,
        partial = 0,
        sourceTruncated = 0,
        overflow = false;
      for (const row of rows) {
        const value = side(row.token_usage, direction);
        if (!value) continue;
        measured += 1;
        if (value.state === 'partial' || value.truncated) partial += 1;
        if (value.source_truncated === true) sourceTruncated += 1;
        for (const field of fields) {
          total[field] += value[field];
          if (!Number.isSafeInteger(total[field])) overflow = true;
        }
      }
      if (!measured || overflow) for (const field of fields) total[field] = null;
      result[direction] = {
        ...total,
        measured_attempts: measured,
        unavailable_attempts: rows.length - measured,
        partial_attempts: partial,
        source_truncated_attempts: sourceTruncated,
        overflow,
      };
    }
    return result;
  }

  function cell(usage, direction) {
    const value = side(usage, direction);
    if (!value) return '<span class="muted">未记录</span>';
    const partial = value.state === 'partial' || value.truncated;
    return (
      '<span>约 ' +
      number(value.estimated_tokens) +
      '</span>' +
      '<small>范围 ' +
      number(value.low) +
      '–' +
      number(value.high) +
      (partial ? ' · 部分文本' : '') +
      (value.source_truncated === true ? ' · 源工具已截断' : '') +
      '</small>'
    );
  }

  function metricHTML(value, direction, label) {
    const item = value[direction] || {}, n = integer(item.estimated_tokens);
    const notes = [];
    if (n !== null && item.unavailable_attempts) notes.push(number(item.unavailable_attempts) + ' 次未记录');
    if (item.partial_attempts) notes.push('部分文本');
    if (item.source_truncated_attempts) notes.push('源工具截断');
    return '<div class="token-dashboard-metric"><span>' + label + '</span><strong' +
      (n === null ? ' class="is-unavailable"' : '') + '>' + (n === null ? '未记录' : '约 ' + number(n)) +
      '</strong>' + (notes.length ? '<small>' + notes.join(' · ') + '</small>' : '') + '</div>';
  }

  function metricDetails(value, direction, label) {
    const item = value[direction] || {};
    return '<p><strong>' + label + '</strong>：' +
      (integer(item.low) === null ? '范围未记录；' : '粗略范围 ' + number(item.low) + '–' + number(item.high) + '；') +
      number(item.measured_attempts) + ' 次有记录，' + number(item.unavailable_attempts) + ' 次未记录' +
      (item.partial_attempts ? '，' + number(item.partial_attempts) + ' 次仅部分文本' : '') +
      (item.source_truncated_attempts ? '，' + number(item.source_truncated_attempts) + ' 次源工具已截断' : '') +
      '。已计入 ' + number(item.characters) + ' 个 Unicode 字符 / ' + number(item.utf8_bytes) + ' 字节。</p>';
  }

  function summary(rows) {
    const value = summarize(rows);
    const times = mergeActivities([], rows).map(row => row.started).filter(Number.isFinite);
    const range = times.length ? dateText(Math.min(...times), 'UTC') + ' 至 ' + dateText(Math.max(...times), 'UTC') + ' · UTC' : '时间范围未记录';
    const metric = (direction, title) => {
      const data = value[direction];
      return (
        '<div><strong>' +
        title +
        '</strong><p>' +
        (data.estimated_tokens === null
          ? '未记录'
          : '约 ' + number(data.estimated_tokens) + ' Token') +
        '</p><small>' +
        (data.low === null ? '' : '粗略范围 ' + number(data.low) + '–' + number(data.high) + '；') +
        number(data.measured_attempts) +
        ' 次有记录，' +
        number(data.unavailable_attempts) +
        ' 次未记录' +
        (data.partial_attempts ? '，' + number(data.partial_attempts) + ' 次仅部分文本' : '') +
        (data.source_truncated_attempts
          ? '，' + number(data.source_truncated_attempts) + ' 次源工具已截断'
          : '') +
        '</small><p class="integration-help">已计入 ' +
        number(data.characters) +
        ' 个 Unicode 字符 / ' +
        number(data.utf8_bytes) +
        ' 字节</p></div>'
      );
    };
    return (
      '<div class="integration-section-head"><h3>工具文本 Token 估算</h3>' +
      '<span class="badge neutral">估算 · 非账单</span></div>' +
      '<p class="integration-help">当前已载入 ' +
      number(value.wire_attempts) +
      ' 次调用、' +
      number(value.distinct_server_operation_ids) +
      ' 个不同服务端操作编号。重试返回的文本仍计入调用量；操作编号仅作观察，不用于扣减 Token。</p>' +
      '<p class="integration-help">' + escape(range) + '；只汇总已加载明细，不代表整个时间段。实际模型用量：未提供。</p>' +
      '<div class="integration-grid">' +
      metric('input', '输入估算') +
      metric('output', '输出估算') +
      '</div>' +
      '<p class="integration-help">只统计经筛选的项目工具文本。不是整段宿主对话或模型计费；' +
      '不含工具定义、图片与文件字节，已识别的敏感字段和链接不计入。历史缺失不会补算，范围不是统计置信区间。' +
      '算法：' +
      escape(value.version) +
      '。</p>'
    );
  }

  function dateText(timestamp, timezone, bucket = '') {
    if (!Number.isFinite(timestamp)) return '未记录';
    try {
      const options = { timeZone: timezone || 'UTC', hour12: false };
      if (bucket !== 'hour') Object.assign(options, { month: '2-digit', day: '2-digit' });
      if (bucket !== 'day') Object.assign(options, { hour: '2-digit', minute: '2-digit' });
      return new Intl.DateTimeFormat('zh-CN', options).format(new Date(timestamp * 1000));
    } catch { return '未记录'; }
  }

  function trendHTML(data) {
    const trend = Array.isArray(data?.trend) ? data.trend : [];
    if (!trend.length) return '<p class="token-dashboard-note">此范围没有保留的调用记录；缺失时段不会补成 0。</p>';
    const maximum = Math.max(1, ...trend.flatMap(point => ['input', 'output'].map(direction => integer(point[direction]?.estimated_tokens) || 0)));
    return '<div class="token-dashboard-legend"><span>输入估算</span><span class="is-output">输出估算</span><span class="is-missing">未记录</span></div>' +
      '<ol class="token-trend" aria-label="有调用记录的时间桶；缺失不补零">' + trend.map(point => {
        const bar = direction => {
          const n = integer(point[direction]?.estimated_tokens);
          return n === null ? '<div class="token-trend-missing" aria-hidden="true"></div>' :
            `<div class="token-trend-bar ${direction === 'output' ? 'is-output' : ''}" style="--token-bar:${(n / maximum * 100).toFixed(2)}%" aria-hidden="true"></div>`;
        };
        return '<li><time>' + escape(dateText(point.started, data.period?.timezone, data.period?.bucket)) +
          '</time><div class="token-trend-bars">' + bar('input') + bar('output') + '</div><small>入 ' + number(point.input?.estimated_tokens) +
          ' / 出 ' + number(point.output?.estimated_tokens) + '<br>' + number(point.wire_attempts) + ' 次调用</small></li>';
      }).join('') + '</ol>';
  }


  let dashboardState = null;
  function resetDashboard() { dashboardState = null; }
  function viewState(data, continuing = true) {
    if (!continuing || !dashboardState || dashboardState.authority !== data.authority_key) {
      dashboardState = { authority: data.authority_key, filters: { period: data.period?.key || 'today', ...data.filters }, expanded: false, revision: 0 };
    }
    return dashboardState;
  }
  async function prepareDashboard(initial, fetcher, continuing, valid) {
    if (!initial?.summary || !valid()) return initial;
    const state = viewState(initial, continuing), revision = state.revision;
    const filters = { ...state.filters };
    let result = initial;
    if (filters.period !== 'today' || filters.project || filters.connection || filters.session) {
      result = await fetcher('/api/token-usage?' + new URLSearchParams(filters));
      if (!valid() || dashboardState !== state || state.revision !== revision) return null;
      if (result.authority_key !== state.authority) {
        resetDashboard();
        result = await fetcher('/api/token-usage');
        if (!valid()) return null;
        viewState(result, false);
      }
    }
    return result;
  }

  function dashboard(data, expanded = null) {
    if (expanded === null) expanded = dashboardState?.authority === data?.authority_key ? dashboardState?.expanded : false;
    if (!data?.summary) return '<div class="token-dashboard" data-token-dashboard><div class="token-dashboard-head"><h3>今日工具 Token</h3><span class="token-estimate-badge">估算</span></div><p role="status">统计暂不可用</p><details class="token-usage-help"><summary>统计说明</summary><p>实际模型用量未提供。</p></details></div>';
    const value = data.summary, period = data.period || {}, filters = data.filters || {};
    const title = { today: '今日', '7d': '近 7 天', '30d': '近 30 天' }[period.key] || '所选时间';
    const select = (name, label, choices, selected, all = '') => '<label>' + label + `<select data-token-filter="${name}" aria-label="${label}">` +
      (all ? `<option value="">${all}</option>` : '') + choices.map(item => `<option value="${escape(item.id)}"${item.id === selected ? ' selected' : ''}>${escape(item.label)}</option>`).join('') + '</select></label>';
    return '<div class="token-dashboard" data-token-dashboard>' +
      `<div class="token-dashboard-head"><h3>${title}工具 Token</h3><span class="token-estimate-badge">估算</span></div>` +
      '<div class="token-dashboard-metrics">' + metricHTML(value, 'input', '输入') + metricHTML(value, 'output', '输出') + '</div>' +
      (data.coverage?.collection_unavailable ? '<p role="status">估算读取暂不可用</p>' : '') +
      `<details data-token-filters${expanded ? ' open' : ''}><summary><span>${number(value.wire_attempts)} 次记录</span> · 筛选与趋势</summary><div class="token-dashboard-filters">` +
      select('period', '时间范围', [{id:'today',label:'今日'},{id:'7d',label:'近 7 天'},{id:'30d',label:'近 30 天'}], period.key) +
      select('project', '授权项目', data.options?.projects || [], filters.project, '全部授权项目') +
      select('connection', '授权连接标签', data.options?.connections || [], filters.connection, '全部授权连接') +
      select('session', '匿名窗口', data.options?.sessions || [], filters.session, '全部匿名窗口') + '</div>' +
      trendHTML(data) +
      '<details class="token-usage-help"><summary>统计说明</summary>' +
      '<p>' + escape(dateText(period.start, period.timezone)) + ' 至 ' + escape(dateText(period.end, period.timezone)) +
      ' · ' + escape(period.timezone || 'UTC') + ' · 当前保留的授权记录 ' + number(value.wire_attempts) + ' 次调用。</p>' +
      metricDetails(value, 'input', '输入') + metricDetails(value, 'output', '输出') +
      '<p>实际模型用量未提供：没有可核验的模型实际用量接口。仅工具文本，二进制文件/图片字节不计入，' +
      '不含工具定义，已识别的敏感字段和链接不计入。重试仍有传输文本成本；不是完整对话账单。粗略范围不是统计置信区间。</p>' +
      '<p>连接名称是授权标签，不代表已识别宿主产品；匿名窗口只表示相关性，可能因重启变化。</p>' +
      '<p>仅显示有保留调用的时间桶，缺失不补零；历史未记录不会补算。活动最多保留 ' + number(data.coverage?.activity_row_limit) +
      ' 条，估算最长保留 ' + number(data.coverage?.estimate_retention_days) + ' 天，所选时间段可能不完整。算法 ' + escape(value.version || VERSION) + '。</p></details></details>' +
      '<p class="token-dashboard-status" role="status" aria-live="polite"></p></div>';
  }

  let bindingGeneration = 0;
  function bindDashboard(container, fetcher, initial) {
    let root = container?.querySelector('[data-token-dashboard]');
    if (!root || !initial?.summary) return;
    let data = initial, sequence = 0;
    let state = viewState(initial);
    const generation = ++bindingGeneration;
    const bind = () => {
      root._codepierTokenGeneration = generation;
      const details = root.querySelector('[data-token-filters]');
      if (details) details.ontoggle = () => {
        if (dashboardState === state && state.expanded !== details.open) { state.expanded = details.open; state.revision += 1; }
      };
      root.onchange = async event => {
        const target = event.target.closest('[data-token-filter]');
        if (!target) return;
        const captured = root, current = ++sequence;
        const filters = Object.fromEntries([...root.querySelectorAll('[data-token-filter]')].map(select => [select.dataset.tokenFilter, select.value]));
        if (['period', 'project'].includes(target.dataset.tokenFilter)) { filters.connection = ''; filters.session = ''; }
        if (target.dataset.tokenFilter === 'connection') filters.session = '';
        state.filters = filters;
        state.revision += 1;
        root.querySelector('.token-dashboard-status').textContent = '正在读取当前授权范围…';
        try {
          let result = await fetcher('/api/token-usage?' + new URLSearchParams(filters));
          if (current !== sequence || !captured.isConnected || captured._codepierTokenGeneration !== generation || dashboardState !== state) return;
          if (result.authority_key !== state.authority) {
            result = await fetcher('/api/token-usage');
            if (current !== sequence || !captured.isConnected || captured._codepierTokenGeneration !== generation || dashboardState !== state) return;
            state = viewState(result, false);
          }
          if (current !== sequence || !captured.isConnected || captured._codepierTokenGeneration !== generation || dashboardState !== state) return;
          data = result;
          state.filters = { period: result.period.key, ...result.filters };
          const holder = document.createElement('div');
          holder.innerHTML = dashboard(data, state.expanded);
          root = holder.firstElementChild;
          captured.replaceWith(root);
          bind();
        } catch {
          if (current !== sequence || !captured.isConnected || captured._codepierTokenGeneration !== generation || dashboardState !== state) return;
          state.filters = { period: data.period.key, ...data.filters };
          state.revision += 1;
          root.querySelectorAll('[data-token-filter]').forEach(select => { select.value = select.dataset.tokenFilter === 'period' ? data.period.key : data.filters[select.dataset.tokenFilter] || ''; });
          root.querySelector('.token-dashboard-status').textContent = '统计读取失败，仍显示上一次结果。请重新选择或刷新。';
        }
      };
    };
    bind();
  }

  return { mergeActivities, summarize, cell, summary, dashboard, bindDashboard, trendHTML, prepareDashboard, resetDashboard };

})();
