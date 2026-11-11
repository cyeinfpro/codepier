async function chatInterrupt() {
  const c = ChatUI,
    v = chatView(),
    gen = c.generation,
    id = c.selected?.id;
  if (!id || !v.active) return;
  v.interruptReceipt = v.interruptReceipt || uid();
  try {
    await chatAPI('chat_interrupt', { id, receipt: v.interruptReceipt });
    if (chatCurrent(gen, v)) chatStatus('正在中断当前回复；排队消息仍会继续');
  } catch (e) {
    if (chatCurrent(gen, v)) chatStatus(e.message, true);
  }
}
async function chatRename() {
  const row = ChatUI.selected,
    v = chatView(),
    gen = ChatUI.generation,
    project = ChatUI.project;
  if (!row) return;
  const title = await chatDialog({
    title: '重命名对话',
    body: '给这段工作起个容易找到的名字。',
    value: row.title || '',
    confirmLabel: '保存',
  });
  if (!title || !chatCurrent(gen, v)) return;
  await chatAPI('rename', { id: row.id, title }, project);
  if (chatCurrent(gen, v)) {
    row.title = title;
    $('#chat-title').textContent = title;
    chatList();
  }
}
async function chatResume() {
  const c = ChatUI,
    row = c.selected,
    v = chatView(),
    project = c.project,
    identity = S.session;
  if (!row || v.resumeBusy || v.busy) return;
  if (v.pending) {
    chatStatus('上一条消息还没确认发出，点“重试”。', true);
    return;
  }
  if (['running', 'starting', 'stopping', 'orphaned'].includes(row.status)) {
    chatStatus('会话还在运行，先停止再恢复。');
    return;
  }
  v.resumeId = v.resumeId || uid();
  v.resumeRequest = v.resumeRequest || {
    id: v.resumeId,
    cli: row.provider,
    mode: 'chat',
    cwd: c.cwd,
    continue_session: row.id,
    ...(v.selectedModel != null ? { model: v.selectedModel } : {}),
    ...(v.selectedEffort != null ? { effort: v.selectedEffort } : {}),
  };
  v.resumeBusy = true;
  chatSyncChrome();
  try {
    const next = await chatAPI('start', v.resumeRequest, project);
    if (S.session !== identity || ![...c.views.values()].includes(v)) return;
    const current = chatViewVisible(v, identity);
    v.resumeId = null;
    v.resumeRequest = null;
    v.cursor = 0;
    v.configForResume = false;
    v.selectedModel = null;
    v.selectedEffort = null;
    c.views.set(chatKey(project, next.id), v);
    if (current) await chatSwitch(next, project);
    c.views.delete(chatKey(project, row.id));
  } catch (e) {
    // Definitive launch rejection permits correcting the next attempt. Lost
    // responses keep the immutable request so retrying confirms one process.
    if (
      S.session === identity &&
      e.status >= 400 &&
      e.status < 500 &&
      e.status !== 408 &&
      !['CLI_UNCERTAIN', 'CLI_OFFLINE'].includes(e.code)
    ) {
      v.resumeId = null;
      v.resumeRequest = null;
    }
    throw e;
  } finally {
    v.resumeBusy = false;
    if (chatViewVisible(v, identity)) chatSyncChrome();
  }
}
async function chatStop() {
  const row = ChatUI.selected,
    v = chatView(),
    gen = ChatUI.generation,
    project = ChatUI.project;
  if (!row) return;
  const confirmed = await chatDialog({
    title: '停止会话？',
    body: '正在进行的回复和排队消息会取消。',
    confirmLabel: '停止会话',
    danger: true,
  });
  if (!confirmed || !chatCurrent(gen, v)) return;
  if (v.stopSessionId !== row.id) {
    v.stopSessionId = row.id;
    v.stopReceipt = uid();
  }
  v.stopReceipt = v.stopReceipt || uid();
  await chatAPI('stop', { id: row.id, receipt: v.stopReceipt }, project);
  if (chatCurrent(gen, v)) chatStatus('正在停止…');
}
async function chatDelete() {
  const c = ChatUI,
    row = c.selected,
    v = chatView(),
    gen = c.generation,
    project = c.project;
  if (!row || v.deleteBusy) return;
  const live = () => ['starting', 'running', 'stopping', 'orphaned'].includes(row.status);
  const running = live();
  v.deleteBusy = true;
  try {
    const confirmed = await chatDialog({
      title: running ? '停止并删除这个会话？' : '删除这个会话？',
      body:
        (running ? '正在进行的回复和排队消息会取消。' : '') + '电脑上的 CLI 历史和项目文件会保留。',
      confirmLabel: running ? '停止并删除' : '删除',
      danger: true,
    });
    if (!confirmed || !chatCurrent(gen, v)) return;
    if (live()) {
      if (v.stopSessionId !== row.id) {
        v.stopSessionId = row.id;
        v.stopReceipt = uid();
      }
      v.stopReceipt ||= uid();
      if (row.status !== 'stopping')
        await chatAPI('stop', { id: row.id, receipt: v.stopReceipt }, project);
      if (!chatCurrent(gen, v)) return;
      chatStatus('正在停止…');
      const deadline = Date.now() + 30000;
      while (live() && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 300));
        if (!chatCurrent(gen, v)) return;
      }
      if (live()) {
        chatStatus('会话还在停止，稍后再试。', true);
        return;
      }
    }
    await chatAPI('delete', { id: row.id, confirm: row.id }, project);
    if (chatCurrent(gen, v)) await chatSwitch(null, project);
    c.views.delete(chatKey(project, row.id));
    for (const file of v.files) {
      file.removed = true;
      if (file.preview) URL.revokeObjectURL(file.preview);
    }
  } finally {
    v.deleteBusy = false;
    if (chatCurrent(gen, v)) chatSyncChrome();
  }
}
function chatExport(format) {
  const sid = ChatUI.selected?.id;
  if (!sid) {
    chatStatus('请先打开一条会话');
    return;
  }
  const a = document.createElement('a');
  a.href =
    '/api/native/sessions/' +
    encodeURIComponent(sid) +
    '/export?' +
    new URLSearchParams({ format, space_id: S.space_id || '' });
  a.download = 'conversation.' + format;
  a.click();
  chatStatus('正在导出此会话的完整已同步记录');
}
function chatFindToggle(open) {
  if (open) {
    chatClosePopover();
    chatOptions(false);
    if ($('#chat-root').classList.contains('drawer-open')) chatDrawer(false, true);
    if (ChatUI.inspector && innerWidth <= 1100) chatInspector('');
  }
  $('#chat-find-bar').hidden = !open;
  if (open) {
    $('#chat-find-input').value = chatView().search || '';
    chatFind();
    $('#chat-find-input').focus();
  } else {
    for (const el of document.querySelectorAll('.chat-message.search-match'))
      el.classList.remove('search-match');
    $('#chat-compose').focus();
  }
}
function chatFind() {
  const v = chatView(),
    q = v.search.toLocaleLowerCase();
  for (const el of document.querySelectorAll('.chat-message.search-match'))
    el.classList.remove('search-match');
  const keys = q ? v.order.filter((k) => v.items.get(k).text.toLocaleLowerCase().includes(q)) : [];
  for (const id of ['chat-find-prev', 'chat-find-next']) $('#' + id).disabled = !keys.length;
  if (!keys.length) {
    $('#chat-find-count').textContent = q ? '无匹配' : '';
    return;
  }
  v.match = ((v.match % keys.length) + keys.length) % keys.length;
  const item = v.items.get(keys[v.match]);
  if (!item.node) {
    v.limit = Math.max(v.limit, v.order.length - v.order.indexOf(item.key));
    chatRenderHistory(false);
  }
  item.node.classList.add('search-match');
  const group = item.node.closest('.chat-process');
  if (group) group.open = true;
  const detail = item.node.querySelector('details');
  if (detail) detail.open = true;
  item.node.scrollIntoView({ block: 'center', behavior: 'auto' });
  $('#chat-find-count').textContent = v.match + 1 + ' / ' + keys.length;
}
