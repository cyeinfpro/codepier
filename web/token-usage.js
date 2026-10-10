'use strict';
// Presentation of server-recorded, sanitized text estimates only. No tokenizer,
// request bodies, credentials, or host billing values are stored in this module.
window.CodePierTokenUsage = (() => {
  const VERSION = 'codepier-text-v1';
  const fields = ['estimated_tokens', 'low', 'high', 'characters', 'utf8_bytes'];
  const integer = (value) => (Number.isSafeInteger(value) && value >= 0 ? value : null);
  const number = (value) => integer(value)?.toLocaleString('zh-CN') ?? '未记录';
  // Decimal K/M/B, two decimals at most, promoting again after rounding.
  function formatTokens(value) {
    if (integer(value) === null) return '未记录';
    const n = BigInt(value),
      scales = [1n, 1000n, 1000000n, 1000000000n];
    let unit = 0;
    while (unit < 3 && n >= scales[unit + 1]) unit += 1;
    if (!unit) return String(value);
    let rounded = (n * 100n + scales[unit] / 2n) / scales[unit];
    if (rounded >= 100000n && unit < 3) {
      unit += 1;
      rounded = (n * 100n + scales[unit] / 2n) / scales[unit];
    }
    const fraction = String(rounded % 100n)
      .padStart(2, '0')
      .replace(/0+$/, '');
    return String(rounded / 100n) + (fraction ? '.' + fraction : '') + ['', 'K', 'M', 'B'][unit];
  }
  function formatUSD(nano) {
    if (integer(nano) === null) return '未记录';
    if (nano > 0 && nano < 100000) return '< $0.0001';
    const usd = nano / 1e9;
    return (
      '$' +
      usd.toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: usd < 1 ? 4 : 2,
      })
    );
  }
  function combined(value) {
    const sides = ['input', 'output'].map((direction) => value[direction] || {});
    const sum = (field) => {
      const counts = sides.map((item) => integer(item[field])).filter((n) => n !== null);
      const total = counts.reduce((a, b) => a + b, 0);
      return counts.length && Number.isSafeInteger(total) ? total : null;
    };
    return {
      estimated_tokens: sum('estimated_tokens'),
      low: sum('low'),
      high: sum('high'),
      partial: sides.some(
        (item) =>
          integer(item.estimated_tokens) === null ||
          item.unavailable_attempts ||
          item.partial_attempts ||
          item.source_truncated_attempts,
      ),
    };
  }
  function costForRows(rows) {
    let cost = null,
      totalPico = 0n,
      missing = 0,
      incompatible = false;
    for (const row of rows) {
      const item = row.token_usage?.reference_cost;
      if (
        item?.kind !== 'reference_estimate' ||
        item.scope !== 'visible_tool_text_equivalent' ||
        item.currency !== 'USD' ||
        !item.pricing?.version ||
        integer(item.amount_nano_usd) === null ||
        (!side(row.token_usage, 'input') && !side(row.token_usage, 'output'))
      ) {
        missing += 1;
        continue;
      }
      if (
        cost &&
        (cost.version !== item.version ||
          cost.pricing.model !== item.pricing.model ||
          cost.pricing.cache_read_percent !== item.pricing.cache_read_percent)
      )
        incompatible = true;
      cost = item;
      const exact = item.amount_pico_usd_exact;
      const pico =
        typeof exact === 'string' && /^\d{1,40}$/.test(exact)
          ? BigInt(exact)
          : integer(item.amount_pico_usd) !== null
            ? BigInt(item.amount_pico_usd)
            : BigInt(item.amount_nano_usd) * 1000n;
      totalPico += pico;
    }
    const total = Number(totalPico / 1000n);
    return cost && !incompatible && Number.isSafeInteger(total)
      ? {
          kind: cost.kind,
          scope: cost.scope,
          currency: cost.currency,
          version: cost.version,
          pricing: cost.pricing,
          amount_nano_usd: total,
          amount_pico_usd_exact: String(totalPico),
          partial: missing > 0 || rows.some((row) => row.token_usage?.reference_cost?.partial),
        }
      : null;
  }
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
    result.total = combined(result);
    result.reference_cost = costForRows(rows);
    return result;
  }

  function cell(usage, direction) {
    const value = side(usage, direction);
    if (!value) return '<span class="muted">未记录</span>';
    const partial = value.state === 'partial' || value.truncated;
    return (
      '<span>约 ' +
      formatTokens(value.estimated_tokens) +
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

  function headline(value) {
    const total = combined(value),
      n = integer(total.estimated_tokens);
    const cost = value.reference_cost;
    const amount =
      cost?.kind === 'reference_estimate' &&
      cost.scope === 'visible_tool_text_equivalent' &&
      cost.currency === 'USD'
        ? integer(cost.amount_nano_usd)
        : null;
    return (
      '<div class="token-dashboard-metrics">' +
      '<div class="token-dashboard-metric is-total"><span>总 Token</span><strong title="' +
      number(n) +
      ' Token">' +
      (n === null ? '未记录' : '约 ' + formatTokens(n)) +
      '</strong></div>' +
      '<div class="token-dashboard-metric is-cost"><span>参考估价 · USD</span><strong>' +
      escape(formatUSD(amount)) +
      '</strong></div></div>' +
      (cost?.pricing
        ? '<p class="token-dashboard-note">' +
          escape(cost.pricing.model_label || cost.pricing.model || '参考模型未知') +
          ' · Standard API · 输入缓存 ' +
          escape(cost.pricing.cache_read_percent) +
          '% 假设</p>'
        : '') +
      (n !== null && total.partial ? '<p class="token-dashboard-note">仅已记录部分</p>' : '')
    );
  }

  function pricingDetails(cost, adjustable = false) {
    const p = cost?.pricing;
    if (!p || cost.kind !== 'reference_estimate')
      return '<p>参考估价未记录；实际模型用量：未提供。</p>';
    const rates = p.usd_per_million || {};
    const money = (value) => (Number.isFinite(value) && value >= 0 ? '$' + value : '未记录');
    return (
      '<div class="token-pricing-details"><p><strong>' +
      escape(p.model_label || p.model || '参考模型未知') +
      ' 参考估价</strong> · Standard API 短上下文，USD / 1M Token。</p>' +
      (adjustable
        ? '<label class="token-cache-assumption">参考计价模型 <select data-token-filter="reference_model" aria-label="参考计价模型">' +
          (
            p.available_models || [
              { id: p.model || 'gpt-6-astra', label: p.model_label || 'GPT-6 Astra' },
            ]
          )
            .map(
              (model) =>
                '<option value="' +
                escape(model.id) +
                '"' +
                (model.id === (p.model || 'gpt-6-astra') ? ' selected' : '') +
                '>' +
                escape(model.label) +
                '</option>',
            )
            .join('') +
          '</select></label>'
        : '') +
      (adjustable
        ? '<label class="token-cache-assumption">模型输入缓存读取假设 <span><input type="number" min="0" max="100" step="1" inputmode="numeric" data-token-filter="cache_read_percent" aria-label="模型输入缓存读取假设百分比" value="' +
          escape(p.cache_read_percent) +
          '"> %</span></label>'
        : '<p>模型输入缓存读取假设：' + escape(p.cache_read_percent) + '%。</p>') +
      '<p>输入 ' +
      money(rates.input) +
      '，缓存读取 ' +
      money(rates.cached_input) +
      '，输出 ' +
      money(rates.output) +
      '；按此假设，输入综合单价 ' +
      money(p.effective_input_usd_per_million) +
      ' / 1M。</p>' +
      (cost.breakdown
        ? '<p>估价拆分：普通输入 ' +
          escape(formatUSD(cost.breakdown.uncached_input?.amount_nano_usd)) +
          '，缓存读取 ' +
          escape(formatUSD(cost.breakdown.cached_input?.amount_nano_usd)) +
          '，输出 ' +
          escape(formatUSD(cost.breakdown.output?.amount_nano_usd)) +
          '。</p>'
        : '') +
      '<p>MCP 请求参数按模型输出计价；MCP 响应文本按后续模型输入计价。缓存是输入的一部分，不另加到总 Token；' +
      '此比例是假设，不是实测缓存命中率，也不由重复文本推断。没有缓存写入数据，不估算缓存写入费用。</p>' +
      '<p>仅估算可见工具文本的等价成本；实际模型用量未知，不是完整会话或实际账单。完整请求超过 272K 输入时适用整请求长上下文价；' +
      '不能从单次 MCP 文本大小判断。本参考未使用长上下文、加速档位、地区附加费或折扣。ChatGPT/Codex订阅及额度规则不同，不能用此值代替账单。</p>' +
      '<p>真实缓存命中、缓存写入、思考 Token：未知。切换参考模型只重算此情景，不改变实际执行模型。</p>' +
      '<p>官方来源：<a href="https://developers.openai.com/api/docs/models/' +
      escape(
        ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna'].includes(p.model) ? p.model : 'gpt-6-astra',
      ) +
      '" target="_blank" rel="noopener noreferrer">模型价格</a> · ' +
      '<a href="https://developers.openai.com/api/docs/pricing" target="_blank" rel="noopener noreferrer">计价说明</a>；核对 ' +
      escape(p.source_date || '未记录') +
      ' · ' +
      escape(p.version) +
      '。</p></div>'
    );
  }

  function metricDetails(value, direction, label) {
    const item = value[direction] || {};
    return (
      '<p><strong>' +
      label +
      '</strong>：' +
      (integer(item.low) === null
        ? '范围未记录；'
        : '粗略范围 ' + number(item.low) + '–' + number(item.high) + '；') +
      number(item.measured_attempts) +
      ' 次有记录，' +
      number(item.unavailable_attempts) +
      ' 次未记录' +
      (item.partial_attempts ? '，' + number(item.partial_attempts) + ' 次仅部分文本' : '') +
      (item.source_truncated_attempts
        ? '，' + number(item.source_truncated_attempts) + ' 次源工具已截断'
        : '') +
      '。已计入 ' +
      number(item.characters) +
      ' 个 Unicode 字符 / ' +
      number(item.utf8_bytes) +
      ' 字节。</p>'
    );
  }

  function summary(rows) {
    const value = summarize(rows);
    const times = mergeActivities([], rows)
      .map((row) => row.started)
      .filter(Number.isFinite);
    const range = times.length
      ? dateText(Math.min(...times), 'UTC') +
        ' 至 ' +
        dateText(Math.max(...times), 'UTC') +
        ' · UTC'
      : '时间范围未记录';
    return (
      '<div class="token-dashboard token-loaded-summary"><div class="integration-section-head"><h3>已加载工具 Token</h3>' +
      '<span class="badge neutral">估算 · 非账单</span></div>' +
      headline(value) +
      '<details class="token-usage-help"><summary>详情 · 当前已载入 ' +
      number(value.wire_attempts) +
      ' 次调用</summary>' +
      '<p>' +
      escape(range) +
      '；只汇总已加载明细，不代表整个时间段。实际模型用量：未提供。</p>' +
      metricDetails(value, 'input', 'MCP 请求参数') +
      metricDetails(value, 'output', 'MCP 响应文本') +
      pricingDetails(value.reference_cost) +
      '<p>' +
      number(value.distinct_server_operation_ids) +
      ' 个不同服务端操作编号。重试返回文本仍计入；操作编号不用于扣减 Token。</p>' +
      '<p>只统计经筛选的项目工具文本。不是整段宿主对话或模型计费；不含工具定义、图片与文件字节，' +
      '已识别的敏感字段和链接不计入。历史缺失不会补算，范围不是统计置信区间。算法 ' +
      escape(value.version) +
      '。</p></details></div>'
    );
  }

  function dateText(timestamp, timezone, bucket = '') {
    if (!Number.isFinite(timestamp)) return '未记录';
    try {
      const options = { timeZone: timezone || 'UTC', hour12: false };
      if (bucket !== 'hour') Object.assign(options, { month: '2-digit', day: '2-digit' });
      if (bucket !== 'day') Object.assign(options, { hour: '2-digit', minute: '2-digit' });
      return new Intl.DateTimeFormat('zh-CN', options).format(new Date(timestamp * 1000));
    } catch {
      return '未记录';
    }
  }

  function trendHTML(data) {
    const trend = Array.isArray(data?.trend) ? data.trend : [];
    if (!trend.length)
      return '<p class="token-dashboard-note">此范围没有保留的调用记录；缺失时段不会补成 0。</p>';
    const maximum = Math.max(
      1,
      ...trend.flatMap((point) =>
        ['input', 'output'].map((direction) => integer(point[direction]?.estimated_tokens) || 0),
      ),
    );
    return (
      '<div class="token-dashboard-legend"><span>MCP 请求参数</span><span class="is-output">MCP 响应文本</span><span class="is-missing">未记录</span></div>' +
      '<ol class="token-trend" aria-label="有调用记录的时间桶；缺失不补零">' +
      trend
        .map((point) => {
          const bar = (direction) => {
            const n = integer(point[direction]?.estimated_tokens);
            return n === null
              ? '<div class="token-trend-missing" aria-hidden="true"></div>'
              : `<div class="token-trend-bar ${direction === 'output' ? 'is-output' : ''}" style="--token-bar:${((n / maximum) * 100).toFixed(2)}%" aria-hidden="true"></div>`;
          };
          return (
            '<li><time>' +
            escape(dateText(point.started, data.period?.timezone, data.period?.bucket)) +
            '</time><div class="token-trend-bars">' +
            bar('input') +
            bar('output') +
            '</div><small>请求 ' +
            formatTokens(point.input?.estimated_tokens) +
            ' / 响应 ' +
            formatTokens(point.output?.estimated_tokens) +
            '<br>' +
            number(point.wire_attempts) +
            ' 次调用</small></li>'
          );
        })
        .join('') +
      '</ol>'
    );
  }

  const filterValues = (data) => ({
    period: data.period?.key || 'today',
    ...data.filters,
    cache_read_percent: String(data.summary?.reference_cost?.pricing?.cache_read_percent ?? 90),
    reference_model: data.summary?.reference_cost?.pricing?.model || 'gpt-6-astra',
  });
  let dashboardState = null;
  function resetDashboard() {
    dashboardState = null;
  }
  function viewState(data, continuing = true) {
    if (!continuing || !dashboardState || dashboardState.authority !== data.authority_key) {
      dashboardState = {
        authority: data.authority_key,
        filters: filterValues(data),
        expanded: false,
        revision: 0,
      };
    }
    return dashboardState;
  }
  async function prepareDashboard(initial, fetcher, continuing, valid) {
    if (!initial?.summary || !valid()) return initial;
    const state = viewState(initial, continuing),
      revision = state.revision;
    const filters = { ...state.filters };
    let result = initial;
    if (
      filters.period !== 'today' ||
      filters.project ||
      filters.connection ||
      filters.session ||
      filters.cache_read_percent !== '90' ||
      filters.reference_model !== 'gpt-6-astra'
    ) {
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
    if (expanded === null)
      expanded =
        dashboardState?.authority === data?.authority_key ? dashboardState?.expanded : false;
    if (!data?.summary)
      return '<div class="token-dashboard" data-token-dashboard><div class="token-dashboard-head"><h3>今日工具 Token</h3><span class="token-estimate-badge">估算</span></div><p role="status">统计暂不可用</p><details class="token-usage-help"><summary>统计说明</summary><p>实际模型用量未提供。</p></details></div>';
    const value = data.summary,
      period = data.period || {},
      filters = data.filters || {};
    const title = { today: '今日', '7d': '近 7 天', '30d': '近 30 天' }[period.key] || '所选时间';
    const select = (name, label, choices, selected, all = '') =>
      '<label>' +
      label +
      `<select data-token-filter="${name}" aria-label="${label}">` +
      (all ? `<option value="">${all}</option>` : '') +
      choices
        .map(
          (item) =>
            `<option value="${escape(item.id)}"${item.id === selected ? ' selected' : ''}>${escape(item.label)}</option>`,
        )
        .join('') +
      '</select></label>';
    return (
      '<div class="token-dashboard" data-token-dashboard>' +
      `<div class="token-dashboard-head"><h3>${title}工具 Token</h3><span class="token-estimate-badge">估算</span></div>` +
      headline(value) +
      (data.coverage?.collection_unavailable ? '<p role="status">估算读取暂不可用</p>' : '') +
      `<details data-token-filters${expanded ? ' open' : ''}><summary><span>${number(value.wire_attempts)} 次记录</span> · 详情与筛选</summary><div class="token-dashboard-filters">` +
      select(
        'period',
        '时间范围',
        [
          { id: 'today', label: '今日' },
          { id: '7d', label: '近 7 天' },
          { id: '30d', label: '近 30 天' },
        ],
        period.key,
      ) +
      select('project', '授权项目', data.options?.projects || [], filters.project, '全部授权项目') +
      select(
        'connection',
        '授权连接标签',
        data.options?.connections || [],
        filters.connection,
        '全部授权连接',
      ) +
      select('session', '匿名窗口', data.options?.sessions || [], filters.session, '全部匿名窗口') +
      '</div>' +
      metricDetails(value, 'input', 'MCP 请求参数') +
      metricDetails(value, 'output', 'MCP 响应文本') +
      pricingDetails(value.reference_cost, true) +
      trendHTML(data) +
      '<button type="button" class="secondary" data-token-export>导出当前统计 JSON</button>' +
      '<details class="token-usage-help"><summary>统计说明</summary>' +
      '<p>' +
      escape(dateText(period.start, period.timezone)) +
      ' 至 ' +
      escape(dateText(period.end, period.timezone)) +
      ' · ' +
      escape(period.timezone || 'UTC') +
      ' · 当前保留的授权记录 ' +
      number(value.wire_attempts) +
      ' 次调用。</p>' +
      '<p>实际模型用量未提供：没有可核验的模型实际用量接口。仅工具文本，二进制文件/图片字节不计入，' +
      '不含工具定义，已识别的敏感字段和链接不计入。重试仍有传输文本成本；不是完整对话账单。粗略范围不是统计置信区间。</p>' +
      '<p>连接名称是授权标签，不代表已识别宿主产品；匿名窗口只表示相关性，可能因重启变化。</p>' +
      '<p>仅显示有保留调用的时间桶，缺失不补零；历史未记录不会补算。活动最多保留 ' +
      number(data.coverage?.activity_row_limit) +
      ' 条，估算最长保留 ' +
      number(data.coverage?.estimate_retention_days) +
      ' 天，所选时间段可能不完整。算法 ' +
      escape(value.version || VERSION) +
      '。</p></details></details>' +
      '<p class="token-dashboard-status" role="status" aria-live="polite"></p></div>'
    );
  }

  function exportJSON(data) {
    // Export only the already-authorized aggregate response. No row bodies,
    // credentials, connection labels, project paths, or raw request metadata.
    const value = {
      kind: 'mcp_tool_text_reference_estimate',
      exported_at: new Date().toISOString(),
      schema_version: data.schema_version || 2,
      period: data.period,
      filters: data.filters,
      summary: data.summary,
      trend: data.trend,
      coverage: data.coverage,
    };
    return JSON.stringify(value, null, 2);
  }

  let bindingGeneration = 0;
  function bindDashboard(container, fetcher, initial) {
    let root = container?.querySelector('[data-token-dashboard]');
    if (!root || !initial?.summary) return;
    let data = initial,
      sequence = 0;
    let state = viewState(initial);
    const generation = ++bindingGeneration;
    const bind = () => {
      root._codepierTokenGeneration = generation;
      const details = root.querySelector('[data-token-filters]');
      if (details)
        details.ontoggle = () => {
          if (
            details.isConnected &&
            details.closest('[data-token-dashboard]') === root &&
            root._codepierTokenGeneration === generation &&
            dashboardState === state &&
            state.expanded !== details.open
          ) {
            state.expanded = details.open;
            state.revision += 1;
          }
        };
      root.onclick = (event) => {
        if (
          !event.target.closest('[data-token-export]') ||
          !root.isConnected ||
          root._codepierTokenGeneration !== generation ||
          dashboardState !== state
        )
          return;
        try {
          const url = URL.createObjectURL(
            new Blob([exportJSON(data)], { type: 'application/json' }),
          );
          const link = document.createElement('a');
          link.href = url;
          link.download = 'codepier-mcp-tool-estimate.json';
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch {
          root.querySelector('.token-dashboard-status').textContent = '导出失败，请重试。';
        }
      };
      root.onchange = async (event) => {
        const target = event.target.closest('[data-token-filter]');
        if (!target) return;
        if (
          target.dataset.tokenFilter === 'cache_read_percent' &&
          (!/^\d{1,3}$/.test(target.value) || Number(target.value) > 100)
        ) {
          target.value = filterValues(data).cache_read_percent;
          root.querySelector('.token-dashboard-status').textContent =
            '请输入 0–100 的整数缓存比例。';
          return;
        }
        const captured = root,
          current = ++sequence;
        const filters = Object.fromEntries(
          [...root.querySelectorAll('[data-token-filter]')].map((select) => [
            select.dataset.tokenFilter,
            select.value,
          ]),
        );
        if (['period', 'project'].includes(target.dataset.tokenFilter)) {
          filters.connection = '';
          filters.session = '';
        }
        if (target.dataset.tokenFilter === 'connection') filters.session = '';
        state.filters = filters;
        state.expanded = !!root.querySelector('[data-token-filters]')?.open;
        state.revision += 1;
        root.querySelector('.token-dashboard-status').textContent = '正在读取当前授权范围…';
        try {
          let result = await fetcher('/api/token-usage?' + new URLSearchParams(filters));
          if (
            current !== sequence ||
            !captured.isConnected ||
            captured._codepierTokenGeneration !== generation ||
            dashboardState !== state
          )
            return;
          if (result.authority_key !== state.authority) {
            result = await fetcher('/api/token-usage');
            if (
              current !== sequence ||
              !captured.isConnected ||
              captured._codepierTokenGeneration !== generation ||
              dashboardState !== state
            )
              return;
            state = viewState(result, false);
          }
          if (
            current !== sequence ||
            !captured.isConnected ||
            captured._codepierTokenGeneration !== generation ||
            dashboardState !== state
          )
            return;
          state.expanded = !!captured.querySelector('[data-token-filters]')?.open;
          data = result;
          state.filters = filterValues(result);
          const holder = document.createElement('div');
          holder.innerHTML = dashboard(data, state.expanded);
          root = holder.firstElementChild;
          captured.replaceWith(root);
          bind();
        } catch {
          if (
            current !== sequence ||
            !captured.isConnected ||
            captured._codepierTokenGeneration !== generation ||
            dashboardState !== state
          )
            return;
          state.filters = filterValues(data);
          state.revision += 1;
          root.querySelectorAll('[data-token-filter]').forEach((select) => {
            select.value = filterValues(data)[select.dataset.tokenFilter] || '';
          });
          root.querySelector('.token-dashboard-status').textContent =
            '统计读取失败，仍显示上一次结果。请重新选择或刷新。';
        }
      };
    };
    bind();
  }

  return {
    formatTokens,
    formatUSD,
    exportJSON,
    mergeActivities,
    summarize,
    cell,
    summary,
    dashboard,
    bindDashboard,
    trendHTML,
    prepareDashboard,
    resetDashboard,
  };
})();
