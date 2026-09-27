'use strict';
function chatNumber(n) {
  return typeof n === 'number'
    ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(n)
    : '—';
}
function chatStats() {
  const s = chatView().stats || {},
    u = s.contextUsage || {};
  $('#chat-usage').textContent =
    u.percent != null
      ? '上下文 ' + Math.round(u.percent) + '%'
      : s.tokens?.total != null
        ? '用量 ' + chatNumber(s.tokens.total)
        : '上下文';
}
function chatInspector(tab) {
  const previous = ChatUI.inspector;
  if (tab && !previous)
    ChatUI.inspectorOrigin = $('#chat-options').classList.contains('is-open')
      ? $('#chat-options-toggle')
      : $('.chat-overflow').open
        ? $('.chat-overflow summary')
        : document.activeElement;
  if (tab) chatOptions(false);
  ChatUI.inspector = tab;
  $('#chat-inspector').hidden = !tab;
  $('#chat-root').classList.toggle('inspector-open', !!tab);
  $('#chat-inspector-toggle')?.setAttribute('aria-expanded', String(!!tab));
  if (!tab) {
    if (previous) {
      const origin = ChatUI.inspectorOrigin;
      const target =
        origin?.isConnected && origin.getClientRects().length && !origin.closest('[inert]')
          ? origin
          : innerWidth <= 760
            ? $('.chat-overflow summary')
            : $('#chat-inspector-toggle');
      target?.focus({ preventScroll: true });
    }
    return;
  }
  chatClosePopover();
  if (tab === 'commands') chatLoadCommands();
  chatInspectorRender();
  if (tab === 'files') chatLoadLibrary();
  if (tab === 'queue') chatLoadQueue();
  if (!previous && innerWidth <= 1100) $('#chat-inspector-close').focus({ preventScroll: true });
  chatScheduleLayout();
}
function chatInfoRow(holder, label, value) {
  const row = document.createElement('div');
  row.className = 'chat-info-row';
  const key = document.createElement('span'),
    val = document.createElement('strong');
  key.textContent = label;
  val.textContent = String(value ?? '—');
  row.append(key, val);
  holder.append(row);
}
function chatSection(holder, title) {
  const section = document.createElement('section'),
    heading = document.createElement('h3');
  heading.textContent = title;
  section.append(heading);
  holder.append(section);
  return section;
}
function chatPanelButton(holder, label, fn) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'chat-secondary-button';
  b.textContent = label;
  b.onclick = () => {
    const gen = ChatUI.generation;
    b.disabled = true;
    Promise.resolve()
      .then(fn)
      .catch((e) => {
        if (chatCurrent(gen)) chatStatus(e.message, true);
      })
      .finally(() => {
        b.disabled = false;
      });
  };
  holder.append(b);
  return b;
}
function chatInspectorRender() {
  const c = ChatUI,
    v = chatView(),
    holder = $('#chat-inspector-body');
  if (!holder || !c.inspector) return;
  const focused = document.activeElement?.id === 'chat-command-filter',
    selection = focused
      ? [document.activeElement.selectionStart, document.activeElement.selectionEnd]
      : null;
  document
    .querySelectorAll('[data-chat-tab]')
    .forEach((b) => b.classList.toggle('active', b.dataset.chatTab === c.inspector));
  holder.replaceChildren();
  if (c.inspector === 'overview') {
    const session = chatSection(holder, '当前会话');
    chatInfoRow(session, '项目', S.projects.find((p) => p.id === c.project)?.alias);
    chatInfoRow(session, '工作目录', c.selected?.cwd || c.cwd);
    chatInfoRow(session, 'CLI', c.selected?.provider || c.provider);
    chatInfoRow(session, '状态', c.selected ? chatStateName(c.selected.status) : '尚未创建');
    if (c.selected) chatInfoRow(session, '会话 ID', c.selected.id);
    const config = chatSection(holder, '模型设置');
    chatInfoRow(
      config,
      '已确认模型',
      chatModelKey(v.settings?.model || v.catalog?.model) || '原生默认',
    );
    chatInfoRow(
      config,
      '已确认思考',
      v.settings?.effort ||
        v.settings?.thinkingLevel ||
        v.settings?.reasoningEffort ||
        v.catalog?.thinkingLevel ||
        v.catalog?.reasoningEffort ||
        '原生默认',
    );
    if (v.pendingSettings) {
      chatInfoRow(config, '下一轮模型', v.pendingSettings.model || '不变');
      chatInfoRow(config, '下一轮思考', v.pendingSettings.effort || '不变');
    }
    chatPanelButton(config, '更换模型', chatModelPopover);
    const stats = v.stats || {},
      tokens = stats.tokens || {},
      usage = stats.contextUsage || {},
      section = chatSection(holder, '上下文与用量');
    if (usage.percent != null) {
      const meter = document.createElement('meter');
      meter.min = 0;
      meter.max = 100;
      meter.value = Math.min(100, Math.max(0, usage.percent));
      meter.setAttribute('aria-label', '上下文使用比例');
      section.append(meter);
    }
    chatInfoRow(
      section,
      '上下文',
      usage.tokens != null
        ? chatNumber(usage.tokens) + ' / ' + chatNumber(usage.contextWindow)
        : '等待原生统计',
    );
    chatInfoRow(
      section,
      '输入 / 输出',
      chatNumber(tokens.input) + ' / ' + chatNumber(tokens.output),
    );
    chatInfoRow(section, '缓存读取', chatNumber(tokens.cacheRead));
    chatInfoRow(section, '总用量', chatNumber(tokens.total));
    if (stats.cost != null) chatInfoRow(section, '费用（原生统计）', stats.cost);
    if (stats.toolCalls != null) chatInfoRow(section, '工具调用', stats.toolCalls);
    chatPanelButton(section, '刷新状态', () => chatRunCommand('refresh'));
    const compact = chatPanelButton(section, '压缩上下文', () => chatRunCommand('compact'));
    compact.disabled =
      !c.selected ||
      !['running', 'starting'].includes(c.selected.status) ||
      !!chatProjectInfo(c.project, c.selected || {}).reason;
    compact.title = compact.disabled
      ? '需要在线且运行中的会话'
      : '调用原生 CLI 压缩上下文，执行前会再次确认';
    const note = document.createElement('p');
    note.className = 'chat-inspector-note';
    note.textContent = '关闭页面不会停止会话。模型参数只影响当前会话，不覆盖本机全局配置。';
    holder.append(note);
  } else if (c.inspector === 'changes') {
    chatReviewPanel(holder);
  } else if (c.inspector === 'commands') {
    const section = chatSection(holder, '指令与技能'),
      input = document.createElement('input'),
      list = document.createElement('div');
    input.type = 'search';
    input.placeholder = '搜索指令、技能';
    input.setAttribute('aria-label', '搜索指令');
    input.id = 'chat-command-filter';
    input.value = v.commandQuery || '';
    list.className = 'chat-command-list';
    section.append(input, list);
    const render = () => {
      list.replaceChildren();
      for (const cmd of chatCommandList().filter((x) =>
        [x.name, x.description, x.source]
          .join(' ')
          .toLowerCase()
          .includes(input.value.toLowerCase()),
      )) {
        const b = document.createElement('button'),
          name = document.createElement('strong'),
          desc = document.createElement('small');
        name.textContent = cmd.invocation;
        desc.textContent = cmd.description || cmd.source || '原生指令';
        b.append(name, desc);
        b.onclick = async () => {
          if (await chatReplaceDraft(cmd.invocation + ' ')) {
            if (innerWidth <= 1100) chatInspector('');
            $('#chat-compose').focus({ preventScroll: true });
          }
        };
        list.append(b);
      }
      if (!list.childElementCount) list.textContent = '没有匹配的指令';
    };
    input.oninput = () => {
      v.commandQuery = input.value;
      render();
    };
    render();
    if (focused) {
      input.focus({ preventScroll: true });
      try {
        input.setSelectionRange(...selection);
      } catch {}
    }
    if (v.commandsLoading || v.commandsError) {
      const status = document.createElement('p');
      status.className = 'chat-inspector-note';
      status.textContent = v.commandsError
        ? '指令加载失败：' + v.commandsError
        : '正在加载本机指令…';
      section.append(status);
    }
    chatPanelButton(section, '刷新本机指令', () =>
      c.selected && ['running', 'starting'].includes(c.selected.status)
        ? chatRunCommand('refresh')
        : chatLoadCommands(true),
    );
  } else if (c.inspector === 'files') {
    const section = chatSection(holder, '项目附件库');
    chatPanelButton(section, '上传图片或文件', () => $('#chat-file-input').click());
    chatPanelButton(section, '刷新附件库', chatLoadLibrary);
    const list = document.createElement('div');
    list.id = 'chat-library-list';
    section.append(list);
    chatLibraryRender();
  } else if (c.inspector === 'queue') {
    const section = chatSection(holder, '当前会话队列');
    chatPanelButton(section, '刷新队列', chatLoadQueue);
    const list = document.createElement('div');
    list.id = 'chat-queue-list';
    section.append(list);
    chatQueueRender();
  }
}
async function chatLoadLibrary() {
  const gen = ChatUI.generation,
    v = chatView(),
    ticket = (v.libraryTicket = (v.libraryTicket || 0) + 1);
  const current = () => chatCurrent(gen, v) && v.libraryTicket === ticket;
  try {
    const data = await chatAPI('upload_list');
    if (current()) {
      v.library = data.files || [];
      chatLibraryRender();
    }
  } catch (e) {
    if (current() && $('#chat-library-list')) $('#chat-library-list').textContent = e.message;
  }
}
function chatAttachmentBusy(file) {
  return [...ChatUI.views.values()].some(
    (view) =>
      view.pending?.attachments?.includes(file) ||
      view.files.some((entry) => entry.file === file && entry.uploading),
  );
}
function chatLibraryRender() {
  const list = $('#chat-library-list');
  if (!list) return;
  list.replaceChildren();
  const v = chatView();
  if (!v.library?.length) {
    list.textContent = v.library ? '暂无附件' : '正在加载…';
    return;
  }
  for (const file of v.library) {
    const card = document.createElement('div'),
      name = document.createElement('strong'),
      meta = document.createElement('small');
    card.className = 'chat-library-file';
    name.textContent = file.name;
    meta.textContent =
      (file.size / 1024 / 1024).toFixed(2) +
      ' MiB · ' +
      (file.ready ? '已校验' : '待续传 ' + Math.round((file.received / file.size) * 100) + '%');
    card.append(name, meta);
    chatPanelButton(card, '附到消息', () => {
      if (v.files.some((f) => f.file === file.file)) return;
      if (v.files.length >= 10) throw new Error('每条消息最多 10 个附件');
      v.files.push({ ...file, ready: true });
      chatFiles();
      chatStatus('已附到输入框，发送后才交给模型');
    }).disabled = !file.ready;
    const remove = chatPanelButton(card, '删除', async () => {
      const gen = ChatUI.generation,
        project = ChatUI.project;
      if (chatAttachmentBusy(file.file)) {
        chatStatus('附件正在上传或等待发送确认，暂不能删除。', true);
        return;
      }
      if (
        !(await chatDialog({
          title: '删除附件？',
          body: '原文件不受影响，正在使用的附件不能删除。',
          confirmLabel: '删除附件',
          danger: true,
        })) ||
        !chatCurrent(gen, v) ||
        chatAttachmentBusy(file.file)
      )
        return;
      await chatAPI('upload_delete', { file: file.file, confirm: file.file }, project);
      if (!chatCurrent(gen, v)) return;
      v.libraryTicket = (v.libraryTicket || 0) + 1;
      v.library = v.library.filter((f) => f.file !== file.file);
      v.files = v.files.filter((f) => f.file !== file.file);
      chatLibraryRender();
      chatFiles();
    });
    remove.disabled = chatAttachmentBusy(file.file);
    remove.title = remove.disabled
      ? '附件正在上传或等待发送确认，暂不能删除'
      : '删除项目附件副本，原文件不受影响';
    list.append(card);
  }
}
async function chatLoadQueue() {
  const c = ChatUI,
    v = chatView(),
    gen = c.generation,
    ticket = (v.queueTicket = (v.queueTicket || 0) + 1);
  const current = () => chatCurrent(gen, v) && v.queueTicket === ticket;
  if (!c.selected) {
    v.queue = [];
    chatQueueRender();
    return;
  }
  try {
    const data = await chatAPI('chat_queue', { id: c.selected.id });
    if (current()) {
      v.queue = data.commands || [];
      chatQueueRender();
    }
  } catch (e) {
    if (current() && $('#chat-queue-list')) $('#chat-queue-list').textContent = e.message;
  }
}
function chatQueueRender() {
  const list = $('#chat-queue-list');
  if (!list) return;
  list.replaceChildren();
  for (const item of chatView().queue || []) {
    const row = document.createElement('div'),
      state = document.createElement('small'),
      text = document.createElement('p');
    row.className = 'chat-queue-card';
    state.textContent = chatStateName(item.state) + ' · ' + item.kind;
    text.textContent = item.text || '设置/控制指令';
    row.append(state, text);
    if (item.state === 'queued') {
      chatPanelButton(row, '撤回', () => chatCancelQueued(item.receipt, item.text));
      if (['chat_prompt', 'prompt'].includes(item.kind))
        chatPanelButton(row, '撤回并编辑', () => chatEditQueued(item));
    }
    list.append(row);
  }
  if (!list.childElementCount) list.textContent = '没有等待执行的消息。';
}
function chatQueueSnapshot(item, view) {
  const original = view.outbox.get(item.receipt) || {},
    values = item.attachments || item.attachment_ids || original.attachments || [];
  return {
    receipt: item.receipt,
    text: item.text ?? original.text ?? '',
    attachments: values.map((value) => {
      if (typeof value === 'object') return { ...value };
      const known =
        view.files.find((file) => file.file === value) ||
        view.library?.find((file) => file.file === value);
      return known
        ? {
            file: value,
            name: known.name,
            size: known.size,
            sha256: known.sha256,
            ready: known.ready,
          }
        : {
            file: value,
            name: '附件',
            ready: false,
            error: true,
            errorMessage: '请刷新附件库核实此附件',
          };
    }),
  };
}
function chatApplyQueueDraft(view, snapshot) {
  for (const file of view.files) if (file.preview) URL.revokeObjectURL(file.preview);
  view.files = snapshot.attachments.map((file) => ({ ...file }));
  chatSetDraft(snapshot.text);
  chatFiles();
}
async function chatEditQueued(item) {
  const v = chatView(),
    gen = ChatUI.generation,
    id = ChatUI.selected?.id,
    project = ChatUI.project,
    draft = v.draft,
    identity = S.session;
  if (!id) return;
  v.cancelOps ??= new Map();
  const key = 'cancel:' + item.receipt;
  if (!v.cancelOps.has(key)) v.cancelOps.set(key, uid());
  const result = await chatAPI(
    'chat_cancel',
    { id, receipt: v.cancelOps.get(key), target: item.receipt },
    project,
  );
  if (S.session !== identity) return;
  if (result.state !== 'completed') {
    if (chatCurrent(gen, v)) chatStatus('撤回仍待确认，原消息尚未复制到输入框。');
    return;
  }
  const snapshot = chatQueueSnapshot(result.message || item, v);
  v.outbox.delete(item.receipt);
  v.queueTicket = (v.queueTicket || 0) + 1;
  v.queue = v.queue.filter((entry) => entry.receipt !== item.receipt);
  if (chatCurrent(gen, v) && v.draft === draft && !draft.trim() && !v.files.length)
    chatApplyQueueDraft(v, snapshot);
  else {
    v.recoveredQueueDrafts ??= [];
    if (!v.recoveredQueueDrafts.some((x) => x.receipt === snapshot.receipt))
      v.recoveredQueueDrafts.push(snapshot);
    v.recoveredQueueDraft = v.recoveredQueueDrafts[0]?.text || '';
    if (chatCurrent(gen, v))
      chatStatus('消息与附件已撤回，现有草稿已保留。点击「取回已撤回消息」继续编辑。');
  }
  if (!chatCurrent(gen, v)) return;
  chatOutbox();
  chatLoadQueue();
}
async function chatRestoreQueueDraft() {
  const v = chatView(),
    gen = ChatUI.generation,
    project = ChatUI.project,
    draft = v.draft;
  const snapshot =
    v.recoveredQueueDrafts?.[0] ||
    (v.recoveredQueueDraft ? { text: v.recoveredQueueDraft, attachments: [] } : null);
  if (!snapshot || v.recoveringQueueDraft) return;
  const files = [...v.files],
    unchanged = () =>
      chatCurrent(gen, v) &&
      v.draft === draft &&
      files.length === v.files.length &&
      files.every((file, i) => file === v.files[i]);
  v.recoveringQueueDraft = true;
  try {
    if (
      (draft.trim() || files.length) &&
      !(await chatDialog({
        title: '替换当前草稿？',
        body: '将恢复已撤回消息的文字与附件。取消会保留两份内容，原始文件不会删除。',
        confirmLabel: '替换草稿',
      }))
    )
      return;
    if (!unchanged()) return;
    const restored = {
      ...snapshot,
      attachments: (snapshot.attachments || []).map((file) => ({ ...file })),
    };
    if (restored.attachments.length) {
      const library = await chatAPI('upload_list', {}, project);
      if (!unchanged()) return;
      restored.attachments = restored.attachments.map((file) => {
        const saved = library.files?.find((entry) => entry.file === file.file);
        return saved?.ready
          ? { ...file, ...saved, ready: true, error: false }
          : { ...file, ready: false, error: true, errorMessage: '附件已不可用，请移除后重新添加' };
      });
    }
    chatApplyQueueDraft(v, restored);
    v.recoveredQueueDrafts?.shift();
    v.recoveredQueueDraft = v.recoveredQueueDrafts?.[0]?.text || '';
    chatOutbox();
  } catch (error) {
    if (chatCurrent(gen, v)) chatStatus('取回未完成，消息与附件仍保留：' + error.message, true);
  } finally {
    v.recoveringQueueDraft = false;
  }
}
function chatCommandList() {
  const builtins = [
    ['model', '选择模型'],
    ['thinking', '选择思考强度'],
    ['stats', '查看上下文与用量'],
    ['compact', '压缩当前会话上下文'],
    ['queue', '查看和撤回排队消息'],
    ['new', '新建会话'],
    ['commands', '查看本机指令与技能'],
  ].map(([name, description]) => ({
    name,
    description,
    source: '界面操作',
    invocation: '/' + name,
    builtin: true,
  }));
  const native = (chatView().commands || []).map((x) => ({
    ...x,
    invocation:
      x.invocation ||
      (x.name?.startsWith('/') || x.name?.startsWith('$')
        ? x.name
        : (ChatUI.provider === 'codex' && x.source === 'skill' ? '$' : '/') + x.name),
  }));
  const seen = new Set(builtins.map((c) => c.invocation));
  return [...builtins, ...native.filter((c) => c.name && !seen.has(c.invocation))];
}
function chatBuiltin(text) {
  const match = text.match(/^\/(model|thinking|stats|compact|new|queue|commands)\s*$/);
  if (!match) return false;
  chatSetDraft('');
  $('#chat-slash').hidden = true;
  switch (match[1]) {
    case 'model':
      chatModelPopover();
      break;
    case 'thinking':
      chatOptions(true);
      $('#chat-effort-select').focus();
      try {
        $('#chat-effort-select').showPicker?.();
      } catch {}
      break;
    case 'stats':
      chatInspector('overview');
      break;
    case 'compact':
      chatRunCommand('compact');
      break;
    case 'queue':
      chatInspector('queue');
      break;
    case 'new':
      chatNewSession();
      break;
    case 'commands':
      chatInspector('commands');
      break;
  }
  return true;
}
function chatSlash() {
  const box = $('#chat-slash'),
    text = chatView().draft;
  if (!/^[/\$][^\s]*$/.test(text)) {
    box.hidden = true;
    return;
  }
  if (chatView().commands === null && !chatView().commandsError) chatLoadCommands();
  const rows = chatCommandList()
    .filter((c) => c.invocation.toLowerCase().startsWith(text.toLowerCase()))
    .slice(0, 12);
  if (!rows.length) {
    box.hidden = true;
    return;
  }
  box.replaceChildren();
  box.hidden = false;
  ChatUI.slashIndex = 0;
  rows.forEach((cmd) => {
    const b = document.createElement('button'),
      label = document.createElement('strong'),
      desc = document.createElement('span');
    b.type = 'button';
    b.setAttribute('role', 'option');
    b.id = 'chat-command-option-' + box.children.length;
    b.setAttribute('aria-selected', String(!box.children.length));
    label.textContent = cmd.invocation;
    desc.textContent = cmd.description || cmd.source;
    b.append(label, desc);
    b.onclick = () => {
      chatSetDraft(cmd.invocation + ' ');
      box.hidden = true;
    };
    box.append(b);
  });
  box.firstElementChild.classList.add('active');
  chatPositionLayers();
}
function chatSlashKey(key) {
  const box = $('#chat-slash'),
    buttons = [...box.querySelectorAll('button')];
  if (key === 'Escape') {
    box.hidden = true;
    return;
  }
  if (key === 'Tab' || key === 'Enter') {
    buttons[ChatUI.slashIndex || 0]?.click();
    return;
  }
  ChatUI.slashIndex =
    ((ChatUI.slashIndex || 0) + (key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
  buttons.forEach((b, i) => {
    b.classList.toggle('active', i === ChatUI.slashIndex);
    b.setAttribute('aria-selected', String(i === ChatUI.slashIndex));
  });
  $('#chat-compose').setAttribute('aria-activedescendant', buttons[ChatUI.slashIndex]?.id || '');
  buttons[ChatUI.slashIndex]?.scrollIntoView({ block: 'nearest' });
}
async function chatRunCommand(name, extra = {}) {
  const c = ChatUI,
    v = chatView(),
    gen = c.generation;
  if (!c.selected) {
    if (name === 'refresh') {
      chatLoadCatalog(true);
      return;
    }
    chatStatus('请先打开一个会话');
    return;
  }
  if (
    name === 'compact' &&
    (!(await chatDialog({
      title: '压缩上下文？',
      body: '将调用原生 CLI，可能产生模型费用。完整网页记录不会清除。',
      confirmLabel: '压缩上下文',
    })) ||
      !chatCurrent(gen, v))
  )
    return;
  v.controlOps = v.controlOps || new Map();
  const key = name + JSON.stringify(extra);
  let receipt = v.controlOps.get(key);
  if (!receipt) {
    receipt = uid();
    v.controlOps.set(key, receipt);
  }
  try {
    const result = await chatAPI('chat_command', { id: c.selected.id, receipt, name, ...extra });
    if (chatCurrent(gen, v)) chatStatus('原生指令已提交：' + name);
    if (!['queued', 'claimed', 'uncertain'].includes(result.state)) v.controlOps.delete(key);
  } catch (e) {
    if (chatCurrent(gen, v)) chatStatus(e.message, true);
  }
}

// Immutable source review. Only summaries enter the transcript; source diffs
// are fetched explicitly and never replaced by a fresh worktree comparison.
function chatReviewCounts(data) {
  const s = data.summary || {},
    approx = s.line_counts_approximate ? '≈' : '';
  return (
    (s.files ?? 0) +
    ' 个文件 · ' +
    approx +
    '+' +
    (s.added_lines ?? 0) +
    ' −' +
    (s.removed_lines ?? 0)
  );
}
function chatCreateReview(item) {
  const article = document.createElement('article'),
    details = document.createElement('details');
  const summary = document.createElement('summary'),
    label = document.createElement('strong'),
    meta = document.createElement('span');
  const body = document.createElement('div'),
    note = document.createElement('p'),
    list = document.createElement('div');
  article.className = 'chat-message chat-message-review';
  article.dataset.key = item.key;
  details.className = 'chat-review';
  label.textContent = '本轮改动';
  meta.className = 'chat-review-counts';
  summary.append(label, meta);
  body.className = 'chat-review-body';
  note.className = 'chat-review-note';
  list.className = 'chat-review-files';
  body.append(note, list);
  details.append(summary, body);
  article.append(details);
  Object.assign(item, {
    node: article,
    body,
    reviewNode: details,
    reviewMeta: meta,
    reviewNote: note,
    reviewList: list,
    reviewOffset: 0,
    reviewLoaded: false,
    reviewLoading: false,
    reviewRendered: null,
  });
  details.addEventListener('toggle', () => {
    if (details.open && !item.reviewLoaded && !item.reviewLoading && item.data.available !== false)
      chatLoadReview(item);
  });
}
function chatUpdateReview(item) {
  const data = item.data,
    signature = JSON.stringify([
      data.review_ref,
      data.available,
      data.summary,
      data.coverage,
      data.text,
    ]);
  if (item.reviewRendered === signature) return;
  item.reviewRendered = signature;
  item.reviewMeta.textContent = data.available === false ? '未能核实' : chatReviewCounts(data);
  item.reviewNote.textContent =
    data.available === false
      ? data.text || '改动审阅不可用'
      : '固定快照 · ' +
        (data.coverage?.complete === false ? '已标注未捕获范围 · ' : '') +
        '共享目录期间变化，不代表全部由本会话产生。';
  item.reviewNode.title = data.scope || '展开查看已保存的文件差异';
}
async function chatReviewRequest(item, args) {
  const v = chatView(),
    id = ChatUI.selected?.id,
    project = ChatUI.project,
    identity = S.session;
  const mapping = S.projects.find((p) => p.id === project),
    target = JSON.stringify([mapping?.root, mapping?.device_id]);
  if (!id) throw new Error('请先打开原会话');
  const data = await chatAPI(
    'chat_review',
    { id, review_ref: item.data.review_ref, ...args },
    project,
  );
  const current = S.projects.find((p) => p.id === project);
  // Complete into this item's original, possibly detached, nodes. Navigation
  // must not remove the only pagination/retry control from a retained review.
  if (
    S.session !== identity ||
    ![...ChatUI.views.values()].includes(v) ||
    target !== JSON.stringify([current?.root, current?.device_id])
  )
    return null;
  if (data.review_ref !== item.data.review_ref || data.immutable !== true)
    throw new Error('未收到有效的固定快照，请检查节点版本');
  return data;
}
async function chatLoadReview(item) {
  if (item.reviewLoading) return;
  item.reviewLoading = true;
  const identity = S.session,
    v = chatView(),
    list = item.reviewList;
  list.querySelector('.chat-review-more')?.remove();
  list.querySelectorAll('.chat-review-loading').forEach((node) => node.remove());
  const status = document.createElement('p');
  status.className = 'chat-review-loading';
  status.textContent = '读取已保存的改动…';
  status.setAttribute('role', 'status');
  list.append(status);
  try {
    const data = await chatReviewRequest(item, { offset: item.reviewOffset, limit: 40 });
    if (!data) return;
    for (const file of data.files || []) list.append(chatReviewFile(item, file));
    item.reviewLoaded = true;
    if (data.next_offset != null) {
      item.reviewOffset = data.next_offset;
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'chat-review-more chat-text-button';
      more.textContent = '显示更多文件';
      more.onclick = () => chatLoadReview(item);
      list.append(more);
    } else if (!data.summary.files) {
      const empty = document.createElement('p');
      empty.className = 'chat-review-note';
      empty.textContent = data.coverage.complete
        ? '已捕获范围内没有文件变化。'
        : '已捕获范围内未检测到变化；未捕获部分不能据此判断。';
      list.append(empty);
    }
  } catch (e) {
    if (S.session === identity && [...ChatUI.views.values()].includes(v)) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'chat-review-more chat-text-button';
      retry.textContent = '重新读取快照';
      retry.onclick = () => chatLoadReview(item);
      status.textContent = e.message;
      status.classList.add('is-error');
      list.append(retry);
      return;
    }
  } finally {
    item.reviewLoading = false;
    if (!status.classList.contains('is-error')) status.remove();
  }
}
function chatReviewFile(item, file) {
  const details = document.createElement('details'),
    summary = document.createElement('summary'),
    name = document.createElement('strong'),
    meta = document.createElement('span'),
    body = document.createElement('div');
  details.className = 'chat-review-file';
  summary.title = file.path;
  name.textContent = file.path;
  const label =
    { added: '新增', deleted: '删除', modified: '修改', unverified: '未核实' }[file.status] ||
    '变化';
  meta.textContent =
    label + (file.text_diff_available ? ' · +' + file.added_lines + ' −' + file.removed_lines : '');
  body.className = 'chat-review-diff';
  summary.append(name, meta);
  details.append(summary, body);
  let loading = false,
    loaded = false,
    offset = 0;
  async function load() {
    if (loading) return;
    loading = true;
    body.querySelector('button')?.remove();
    const status = document.createElement('p');
    status.className = 'chat-review-loading';
    status.textContent = '读取差异…';
    body.append(status);
    try {
      const data = await chatReviewRequest(item, { path: file.path, offset, max_chars: 16000 });
      if (!data) return;
      let pre = body.querySelector('pre');
      if (!pre) {
        pre = document.createElement('pre');
        pre.tabIndex = 0;
        pre.setAttribute('aria-label', file.path + ' 的差异');
        body.append(pre);
      }
      // Text nodes only: filenames and patch lines never become HTML or links.
      pre.append(
        document.createTextNode(
          data.diff || (offset === 0 ? '文本内容没有变化（可能仅修改文件属性）。' : ''),
        ),
      );
      loaded = true;
      if (data.next_offset != null) {
        offset = data.next_offset;
        const more = document.createElement('button');
        more.type = 'button';
        more.className = 'chat-text-button';
        more.textContent = '继续读取差异';
        more.onclick = load;
        body.append(more);
      } else if (data.diff_truncated) {
        const note = document.createElement('p');
        note.className = 'chat-review-note';
        note.textContent = '差异达到比较上限或无法作为文本展示；请在本机核查。';
        body.append(note);
      }
    } catch (e) {
      status.textContent = e.message;
      status.classList.add('is-error');
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'chat-text-button';
      retry.textContent = '重新读取';
      retry.onclick = () => {
        status.remove();
        load();
      };
      body.append(retry);
    } finally {
      loading = false;
      if (!status.classList.contains('is-error')) status.remove();
    }
  }
  details.addEventListener('toggle', () => {
    if (details.open && !loaded) load();
  });
  return details;
}
function chatReviewPanel(holder) {
  const section = chatSection(holder, '改动审阅'),
    v = chatView();
  const reviews = v.order.map((key) => v.items.get(key)).filter((item) => item?.kind === 'review');
  if (!reviews.length) {
    const note = document.createElement('p');
    note.className = 'chat-inspector-note';
    note.textContent =
      '新的执行轮次结束后，改动摘要会保存在这里。旧记录没有修改前快照，不补造历史差异。';
    section.append(note);
    return;
  }
  for (const item of reviews.slice().reverse()) {
    const button = chatPanelButton(
      section,
      item.data.available === false ? '本轮改动 · 未能核实' : chatReviewCounts(item.data),
      () => {
        v.limit = Math.max(v.limit, v.order.length - v.order.indexOf(item.key));
        chatRenderHistory(false);
        if (innerWidth <= 760) chatInspector('');
        item.reviewNode.open = true;
        item.node.scrollIntoView({ block: 'center', behavior: 'instant' });
      },
    );
    button.classList.add('chat-review-jump');
  }
}
