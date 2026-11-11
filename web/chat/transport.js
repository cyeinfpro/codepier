function chatConnection(state, text) {
  ChatUI.connection = state;
  const el = $('#chat-connection');
  if (el) {
    el.dataset.state = state;
    el.title = text;
    el.setAttribute('aria-label', '连接状态：' + text);
    el.querySelector('span').textContent = text;
  }
}
function chatDisconnect() {
  const source = ChatUI.source;
  ChatUI.source = null;
  source?.close();
}
function chatConnect() {
  const c = ChatUI,
    gen = c.generation,
    id = c.selected?.id,
    v = chatView();
  chatDisconnect();
  if (!id) return;
  c.cursor = v.cursor || 0;
  const source = new EventSource(
    '/api/native/sessions/' +
      encodeURIComponent(id) +
      '/events?' +
      new URLSearchParams({ space_id: S.space_id || '', project: c.project, cursor: c.cursor }),
  );
  c.source = source;
  const receive = (e) => {
    if (!chatCurrent(gen, v) || source !== c.source) return;
    const cursor = Number(e.lastEventId);
    if (cursor && cursor <= c.cursor) return;
    try {
      const event = JSON.parse(e.data);
      chatEvent(event);
      if (cursor) {
        c.cursor = cursor;
        v.cursor = cursor;
      }
    } catch (error) {
      chatStatus('无法读取结构化事件：' + error.message, true);
    }
  };
  source.onmessage = receive;
  source.addEventListener('chat', receive);
  source.addEventListener('session', (e) => {
    if (!chatCurrent(gen, v) || source !== c.source) return;
    try {
      Object.assign(c.selected, JSON.parse(e.data));
      v.sessionStatus = c.selected.status;
      $('#chat-title').textContent = c.selected.title || 'CLI 对话';
      if (!['running', 'starting'].includes(c.selected.status)) {
        v.active = null;
        v.phase = '';
        for (const item of v.items.values())
          if (item.kind === 'approval' && !item.data.resolved) {
            item.data.resolved = true;
            item.data.resolution = 'closed';
            chatApproval(item);
          }
        $('#chat-interrupt').hidden = true;
        chatConnection('stopped', chatStateName(c.selected.status));
        chatStatus(
          c.selected.error ||
            {
              stopping: '正在等待会话进程退出，草稿已保留。',
              orphaned: '会话进程状态待核实，请先检查节点。草稿已保留。',
            }[c.selected.status] ||
            '会话已停止，发送消息可恢复原生历史',
        );
      }
      chatSyncChrome();
      if (c.inspector === 'overview') chatInspectorRender();
    } catch {}
  });
  source.addEventListener('error', (e) => {
    if (!chatCurrent(gen, v) || source !== c.source || !e.data) return;
    try {
      const error = JSON.parse(e.data);
      chatDisconnect();
      chatConnection('error', '连接受限');
      chatStatus(error.message || '访问权限已变化', true);
    } catch {}
  });
  source.onopen = () => {
    if (chatCurrent(gen, v) && source === c.source) {
      chatConnection('online', '实时连接');
      chatStatus('已连接 · Enter 发送');
      const op = v.settingOp;
      if (op && ['sending', 'queued', 'claimed', 'uncertain'].includes(op.state))
        chatWatchReceipt(id, op.receipt, gen, v, op);
    }
  };
  source.onerror = (e) => {
    if (!e.data && chatCurrent(gen, v) && source === c.source) {
      chatConnection('reconnecting', '重连中');
      chatStatus('连接暂时中断，正在从已接收的位置恢复…');
    }
  };
}
function chatEvent(e) {
  const c = ChatUI,
    v = chatView();
  if (e.type === 'tool') v.phase = 'tool';
  if (e.type === 'reasoning') v.phase = 'reasoning';
  if (e.type === 'delta' || e.type === 'message') v.phase = 'reply';
  if (e.type === 'user') v.phase = 'working';
  chatScheduleLayout();
  if (e.type === 'settings') {
    v.settings = { ...(v.settings || {}), ...e };
    if (e.commands) v.commands = e.commands;
    chatSettings(v.settings);
    return;
  }
  if (e.type === 'settings_pending') {
    v.pendingSettings = e.cleared ? null : e;
    chatSettings(v.settings);
    return;
  }
  if (e.type === 'commands') {
    v.commands = e.commands || [];
    if (c.inspector === 'commands') chatInspectorRender();
    return;
  }
  if (e.type === 'stats') {
    v.stats = e.stats || e;
    if (c.inspector === 'overview') chatInspectorRender();
    return;
  }
  if (e.type === 'command_result') {
    if (e.state && !['queued', 'claimed', 'running', 'accepted', 'uncertain'].includes(e.state)) {
      for (const [key, receipt] of v.controlOps || [])
        if (receipt === e.receipt) v.controlOps.delete(key);
    }
    chatStatus(e.text || e.name + ' · ' + chatStateName(e.state));
    if (e.state === 'error') chatStatus(e.text || '原生指令失败', true);
    if (c.inspector) chatInspectorRender();
    return;
  }
  if (e.type === 'done') {
    v.phase = '';
    if (!e.receipt || v.active === e.receipt) {
      v.active = null;
      v.interruptReceipt = null;
    }
    $('#chat-interrupt').hidden = !v.active;
    v.outbox.delete(e.receipt);
    chatOutbox();
    chatRenderProcessGroups();
    chatSyncChrome();
    chatStatus(e.text || chatStateName(e.status));
    chatList();
    if (c.inspector === 'queue') chatLoadQueue();
    for (const item of v.items.values())
      if (item.kind === 'approval' && item.data.receipt === e.receipt && !item.data.resolved) {
        item.data.resolved = true;
        item.data.resolution = 'closed';
        if (item.node) {
          item.node.querySelector('.chat-approval-actions')?.remove();
          chatApproval(item);
        }
      }
    return;
  }
  if (e.type === 'queue' || e.type === 'cancelled') {
    if (e.target) v.outbox.delete(e.target);
    chatOutbox();
    if (c.inspector === 'queue') chatLoadQueue();
    return;
  }
  if (
    ![
      'user',
      'delta',
      'message',
      'reasoning',
      'tool',
      'approval',
      'error',
      'status',
      'review',
    ].includes(e.type)
  )
    return;
  if (e.type === 'status') {
    chatStatus(e.text || e.status || '');
    return;
  }
  if (e.type === 'error' && v.pendingSettings?.receipt === e.receipt) {
    v.pendingSettings = null;
    chatSettings(v.settings);
  }
  if (e.type === 'error' && v.settingOp?.receipt === e.receipt) {
    v.settingOp.state = 'error';
    chatSettings(v.settings);
    chatStatus(e.text || '设置失败', true);
  }
  if (
    ['user', 'delta', 'message', 'reasoning', 'tool'].includes(e.type) &&
    ['已接收，等待本机处理', '补充指令已提交', '正在发送…', '正在恢复原生会话…'].includes(
      $('#chat-status').textContent,
    )
  )
    chatStatus('');
  if (e.type === 'user') {
    if (['running', 'starting'].includes(c.selected?.status) && !e.parent_receipt && !e.steering)
      v.active = e.receipt;
    v.outbox.delete(e.receipt);
    chatOutbox();
    $('#chat-interrupt').hidden = !v.active;
  }
  // Older Claude adapters wrote every final block as message-id:0. Repair
  // history replay only when one unfinished block in that same message matches.
  if (
    e.type === 'message' &&
    !('block_index' in e) &&
    (c.selected?.provider || c.provider) === 'claude' &&
    typeof e.item_id === 'string' &&
    e.item_id.endsWith(':0')
  ) {
    const prefix = e.item_id.slice(0, -1),
      matches = [...v.items.values()].filter(
        (item) =>
          item.kind === 'assistant' &&
          item.data.type === 'delta' &&
          item.data.receipt === e.receipt &&
          item.data.item_id?.startsWith(prefix) &&
          item.text === e.text,
      );
    if (matches.length === 1) e = { ...e, item_id: matches[0].data.item_id };
  }
  const kind = ['delta', 'message'].includes(e.type) ? 'assistant' : e.type,
    key =
      (e.receipt || 'session') + ':' + kind + ':' + (e.item_id || e.request_id || e.tool_id || '');
  let item = v.items.get(key);
  if (!item) {
    item = { key, kind, text: '', data: e, node: null, body: null };
    v.items.set(key, item);
    v.order.push(key);
  }
  item.data = { ...item.data, ...e };
  if (kind === 'approval') {
    if (!v.approvalKeys) chatHasPendingApproval(v);
    v.approvalKeys.add(key);
  }
  if (e.type === 'approval' && !['running', 'starting'].includes(c.selected?.status)) {
    item.data.resolved = true;
    item.data.resolution = 'closed';
  }
  if (e.type === 'approval' && item.data.resolved && item.node) {
    item.node.querySelector('.chat-approval-actions')?.remove();
    chatApproval(item);
  }
  if (e.type === 'delta' || e.type === 'reasoning' || (e.type === 'tool' && e.status === 'delta'))
    item.text += e.text || e.delta || '';
  else if (e.text !== undefined) item.text = e.text;
  else item.text = e.message || e.title || e.name || item.text;
  if (e.type === 'review' && c.inspector === 'changes') chatInspectorRender();
  c.dirty.add(key);
  if (!c.frame) c.frame = requestAnimationFrame(chatFlush);
}
