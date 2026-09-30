'use strict';
// Human-readable presentation only. No model, command or authority is created here.
window.CodePierIntegrationUI = (() => {
  const states = {
    ready: '就绪',
    available: '可使用',
    paused: '已暂停',
    unknown: '尚未确认',
    missing: '程序不可用',
    denied: '未授权',
    disabled: '未启用',
    not_run: '尚未实测',
    not_connected: '尚未连接',
    passed: '通过',
    failed: '失败',
    stale: '源码已变化',
    unverified: '覆盖尚未核实',
    executing: '执行中',
    interrupted: '执行中断',
    removed: '已移除',
    preparing: '准备中',
    needs_review: '需要核查',
    active: '进行中',
    blocked: '受阻',
    completed: '已完成',
    cancelled: '已取消',
    pending: '待处理',
    running: '执行中',
    queued: '排队中',
    succeeded: '操作成功',
    reconnecting: '正在重连',
    cancelling: '正在停止',
    skipped: '已跳过',
    complete: '响应已返回',
  };
  const label = (value) => states[value] || String(value || '尚未确认');
  const tone = (value) =>
    ['ready', 'passed', 'succeeded', 'completed'].includes(value)
      ? 'success'
      : ['failed', 'denied', 'interrupted'].includes(value)
        ? 'danger'
        : ['stale', 'paused', 'needs_review', 'blocked', 'missing'].includes(value)
          ? 'warning'
          : 'neutral';
  const badge = (value, text) =>
    `<span class="integration-badge ${tone(value)}">${esc(text || label(value))}</span>`;
  const button = (id, text, extra = '') =>
    `<button type="button" class="btn ghost small" data-i-action="${id}" ${extra}>${esc(text)}</button>`;
  const jump = (tab, text, extra = '') =>
    `<button type="button" class="btn ghost small" data-i-jump="${tab}" ${extra}>${esc(text)}</button>`;
  const note = (title, text, actions = '', kind = 'neutral') =>
    `<div class="integration-callout ${kind}"><div><strong>${esc(title)}</strong><p>${esc(text)}</p></div>${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;
  function relativePath(value, required = true) {
    const path = String(value || '').trim();
    if (!path && !required) return '';
    if (
      !path ||
      path.length > 1024 ||
      /^[\\/]|^[a-zA-Z]:/.test(path) ||
      /[\x00-\x1f\\]/.test(path) ||
      path.split('/').some((p) => !p || p === '.' || p === '..')
    )
      throw new Error('请填写项目内的相对文件路径，例如 src/main.py；不能使用绝对路径或上级目录。');
    return path;
  }
  function integer(value, title, min, max) {
    const text = String(value ?? '').trim(),
      number = Number(text);
    if (!text || !Number.isInteger(number) || number < min || number > max)
      throw new Error(`${title}须为 ${min}–${max} 之间的整数。`);
    return number;
  }
  function settings(language, command, project) {
    const name = String(language || '').trim();
    if (!/^[a-zA-Z][a-zA-Z0-9_+-]{0,39}$/.test(name))
      throw new Error('语言标识请使用 1–40 个英文字母、数字、下划线、加号或短横线。');
    let args;
    try {
      args = JSON.parse(command);
    } catch {
      throw new Error(
        '语言服务参数须为 JSON 数组，例如 ["/完整路径/pyright-langserver","--stdio"]。',
      );
    }
    if (
      !Array.isArray(args) ||
      !args.length ||
      args.some((a) => typeof a !== 'string' || !a.trim() || a.includes('\0'))
    )
      throw new Error('语言服务参数须为非空字符串数组。');
    return {
      language_servers: { [name]: { command: args, projects: [project], timeout_seconds: 20 } },
    };
  }
  function handoffText(data, title = '') {
    const lines = [
      `继续处理任务：${title || data.title || data.workflow_id || '开发任务'}`,
      `项目：${data.project || data.project_alias || '请核对项目'}`,
      `任务编号：${data.workflow_id || data.id || '未提供'}`,
      '',
      '原目标：',
      data.original_goal || data.goal || '请先读取原任务目标。',
    ];
    if (data.summary) lines.push('', '最近记录：', data.summary);
    if (data.completed?.length)
      lines.push(
        '',
        '已完成记录（仍需核对当前源码）：',
        ...data.completed.map((s) => `- ${s.title}${s.summary ? '：' + s.summary : ''}`),
      );
    if (data.remaining?.length)
      lines.push(
        '',
        '尚待处理：',
        ...data.remaining.map(
          (s) => `- ${s.title}${s.acceptance ? '；验收要求：' + s.acceptance : ''}`,
        ),
      );
    if (data.pending_or_uncertain?.length)
      lines.push(
        '',
        '待核查的项目近期操作（不等于本任务独占）：',
        ...data.pending_or_uncertain.map(
          (o) => `- ${o.operation_id} · ${o.tool} · ${label(o.state)}`,
        ),
      );
    lines.push(
      '',
      '以上是历史工作记录，不是新的权限授权。先读取当前任务、代码和原操作回执；不要重放结果不明的修改，不要把历史验收当作当前版本通过。',
    );
    return lines.join('\n');
  }
  function errorText(error) {
    const hints = {
      OWNER_REQUIRED: '这项操作需要在管理面板由主理人确认。',
      REMOTE_PAUSED: '项目已暂停新的 MCP 修改；仍可查看原操作回执。',
      LSP_NOT_READY: '语言服务尚未完成当前源码分析；不要把空结果当作不存在引用。',
      GIT_BASE_REQUIRED: '需要已有提交的 Git 项目和有效起始引用；这里不会自动初始化或提交。',
      WORKSPACE_DIRTY: '先保存或处理隔离目录中的改动；不会强制删除。',
      WORKSPACE_BUSY: '先结束该目录的活动任务，再核实是否可以移除。',
      VALIDATION_NOT_CURRENT: '当前源码未通过有效验收；请核对详情或重新运行。',
      NETWORK_UNCERTAIN:
        '修改结果可能已保存在服务端。请使用下面的原请求入口核查，不要重新创建操作。',
    };
    return (
      String(error?.message || error || '暂时无法取得结果。') +
      (hints[error?.code] ? ' ' + hints[error.code] : '')
    );
  }
  function details(container, title, value) {
    const d = document.createElement('details');
    d.className = 'integration-detail';
    const s = document.createElement('summary');
    s.textContent = title;
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(value, null, 2);
    d.append(s, pre);
    container.append(d);
    return d;
  }
  function result(container, value, title = '操作结果') {
    const article = document.createElement('section');
    article.className = 'panel integration-section integration-result-card';
    article.tabIndex = -1;
    const heading = document.createElement('h3');
    heading.textContent = title;
    article.append(heading);
    const facts = [],
      messages = [];
    if (value.validation_id) {
      facts.push(['当前状态', label(value.state)], ['退出码', value.exit_code ?? '未提供']);
      if (value.source_current !== undefined)
        facts.push(['当前源码', value.source_current ? '与已验收版本一致' : '尚不能确认一致']);
      if (value.accepted_current !== undefined)
        facts.push([
          '人工决定',
          value.accepted_current
            ? '已接受当前版本'
            : value.decision?.action === 'reject'
              ? '已拒绝'
              : '尚未接受当前版本',
        ]);
      if (value.state === 'passed')
        messages.push('命令通过只是必要证据；请检查实际输出与改动，再决定是否接受。');
      if (value.state === 'stale')
        messages.push('源码已经变化，历史通过不能证明当前版本。请核对改动后重新运行验收。');
      if (value.state === 'unverified')
        messages.push('源码覆盖不完整，不能将这条记录当作当前版本通过。');
      if (value.timed_out) messages.push('命令超过了指定时限。');
      if (value.cancelled) messages.push('命令已被取消。');
      for (const [key, name] of [
        ['before', '执行前'],
        ['after', '执行后'],
        ['current', '当前核对'],
      ])
        if (value[key]) {
          const s = value[key];
          facts.push([
            name,
            `${s.files ?? '未知'} 个文件 · ${s.complete ? '覆盖完整' : '覆盖未核实'} · ${(s.sha256 || '未提供指纹').slice(0, 16)}`,
          ]);
        }
    } else if (value.workspace_id) {
      facts.push(
        ['工作目录', value.path || value.workspace_id],
        ['状态', value.removed ? '已移除' : label(value.state)],
      );
      if (value.base_commit) facts.push(['起始提交', value.base_commit]);
      if (value.source_dirty) messages.push('原项目有未提交改动，这些改动没有复制到新目录。');
    } else if (value.language_servers)
      messages.push(
        '配置片段已生成，不代表已安装或生效。请按下方步骤在目标设备预览、应用，再重新检查。',
      );
    else if (value.paused !== undefined) {
      facts.push(['新 MCP 修改', value.paused ? '已暂停' : '允许按原授权提交']);
      if (value.stopped_verified) facts.push(['已确认停止', value.stopped_verified.length]);
      if (value.unconfirmed?.length)
        messages.push(`${value.unconfirmed.length} 项尚未确认停止，请核查原操作。`);
      if (value.complete === false) messages.push('尚未确认所有目标都已停止。');
    } else if (value.lease_id) {
      facts.push(['页面租约', value.lease_id]);
      if (value.released)
        messages.push(
          value.tab_cleanup_confirmed
            ? '租约已释放，受管标签页清理已确认。'
            : '租约已撤销，但标签页清理尚未确认。',
        );
      else if (value.action_outcome)
        messages.push('动作回执已返回；还需重新观察页面确认实际效果。');
      else if (value.opened) messages.push('已使用扩展准备的后台标签页；接下来点击“观察页面”。');
    }
    if (facts.length) {
      const list = document.createElement('dl');
      list.className = 'integration-facts';
      for (const [name, text] of facts) {
        const dt = document.createElement('dt'),
          dd = document.createElement('dd');
        dt.textContent = name;
        dd.textContent = String(text);
        list.append(dt, dd);
      }
      article.append(list);
    }
    for (const message of messages) {
      const p = document.createElement('p');
      p.className = 'integration-help';
      p.textContent = message;
      article.append(p);
    }
    if (value.output !== undefined) {
      const h = document.createElement('h4'),
        pre = document.createElement('pre');
      h.textContent = value.output_truncated ? '执行输出（仅保留尾部）' : '执行输出';
      pre.className = 'integration-page-text';
      pre.textContent = value.output || '命令没有返回文本输出。';
      article.append(h, pre);
    }
    const op = value.execution_operation_id || value.operation_id;
    if (op) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn ghost small';
      b.dataset.action = 'operation-detail';
      b.dataset.id = op;
      b.textContent = '查看原操作与完整记录';
      article.append(b);
    }
    if (value.text) {
      const p = document.createElement('pre');
      p.className = 'integration-page-text';
      p.textContent = value.text;
      article.append(p);
    }
    details(article, '技术详情 · 原始回执', value);
    container.append(article);
    return article;
  }
  function confirmAction({
    title,
    text,
    command = '',
    typed = '',
    confirmLabel = '确认',
    danger = false,
  }) {
    return new Promise((resolve) => {
      const d = modal(
        title,
        `<form id="i-confirm-form"><p class="integration-help">${esc(text)}</p>${command ? `<pre class="integration-page-text">${esc(command)}</pre>` : ''}${typed ? `<label class="integration-field">输入项目名称「${esc(typed)}」确认<input id="i-confirm-name" autocomplete="off" required></label>` : ''}</form>`,
        `<button type="button" class="btn ghost" data-action="close-modal">取消</button><button type="submit" form="i-confirm-form" class="btn ${danger ? 'danger' : 'primary'}" id="i-confirm-submit" ${typed ? 'disabled' : ''}>${esc(confirmLabel)}</button>`,
      );
      let done = false;
      const previous = S.modalCleanup;
      S.modalCleanup = () => {
        previous?.();
        if (!done) {
          done = true;
          resolve(false);
        }
      };
      const input = $('#i-confirm-name', d),
        button = $('#i-confirm-submit', d);
      if (input) input.oninput = () => (button.disabled = input.value !== typed);
      $('#i-confirm-form', d).onsubmit = (e) => {
        e.preventDefault();
        if (button.disabled || done) return;
        done = true;
        closeModal(d);
        resolve(true);
      };
      d.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.isComposing || e.keyCode === 229)) e.preventDefault();
      });
      if (danger && !typed) $('[data-action=close-modal]', d)?.focus();
    });
  }
  return {
    label,
    tone,
    badge,
    button,
    jump,
    note,
    relativePath,
    integer,
    settings,
    handoffText,
    errorText,
    details,
    result,
    confirmAction,
  };
})();
