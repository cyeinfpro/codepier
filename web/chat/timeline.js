function chatAtBottom() {
  const el = $('#chat-scroll');
  return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 110;
}
function chatBottom() {
  const el = $('#chat-scroll');
  if (el) el.scrollTop = el.scrollHeight;
  if ($('#chat-latest')) $('#chat-latest').hidden = true;
}
function chatProcessFailure(item) {
  return (
    item.kind === 'tool' &&
    (item.data.isError ||
      item.data.is_error ||
      ['error', 'failed', 'denied', 'interrupted', 'cancelled'].includes(item.data.status))
  );
}
function chatHasPendingApproval(v = chatView()) {
  // Hydrated views are indexed once. Subsequent stream frames only inspect the
  // small set of approval entries, not the entire retained conversation.
  if (!v.approvalKeys)
    v.approvalKeys = new Set(
      [...v.items.values()].filter((item) => item.kind === 'approval').map((item) => item.key),
    );
  let pending = false;
  for (const key of v.approvalKeys) {
    const item = v.items.get(key);
    if (!item || item.data.resolved || item.answered) v.approvalKeys.delete(key);
    else pending = true;
  }
  return pending;
}
function chatReconcileNodes(holder, nodes) {
  let cursor = holder.firstChild;
  for (const node of nodes) {
    if (node === cursor) cursor = cursor.nextSibling;
    else holder.insertBefore(node, cursor);
  }
  const keep = new Set(nodes);
  for (const node of [...holder.childNodes]) if (!keep.has(node)) node.remove();
}
// Group consecutive tool/thinking entries without replacing their disclosure nodes.
function chatRenderProcessGroups() {
  const v = chatView(),
    holder = $('#chat-messages');
  if (!holder) return;
  const keys = v.order.slice(-v.limit),
    renderedItems = new Map(),
    nodes = [],
    groups = [];
  v.processGroups ??= new Map();
  let group = null;
  for (const key of keys) {
    const item = v.items.get(key);
    if (!item) continue;
    if (!item.node) chatCreateItem(item);
    chatUpdateItem(item);
    renderedItems.set(key, item);
    const compact = ['tool', 'reasoning'].includes(item.kind) && !chatProcessFailure(item);
    if (!compact) {
      group = null;
      nodes.push(item.node);
      continue;
    }
    if (!group || group.receipt !== item.data.receipt) {
      let node = v.processGroups.get(key);
      if (!node) {
        node = document.createElement('details');
        node.className = 'chat-process';
        node.dataset.groupKey = key;
        const summary = document.createElement('summary'),
          label = document.createElement('span'),
          meta = document.createElement('span'),
          body = document.createElement('div');
        label.className = 'chat-process-label';
        label.textContent = '执行过程';
        meta.className = 'chat-process-meta';
        body.className = 'chat-process-body';
        summary.append(label, meta);
        node.append(summary, body);
        v.processGroups.set(key, node);
      }
      group = { node, receipt: item.data.receipt, items: [] };
      groups.push(group);
      nodes.push(node);
    }
    group.items.push(item);
  }
  for (const entry of groups) {
    const { node, items, receipt } = entry,
      tools = items.filter((i) => i.kind === 'tool').length,
      thoughts = items.length - tools;
    const live =
      !!v.active &&
      v.active === receipt &&
      items.some((i) => !['completed', 'end', 'succeeded', 'ok'].includes(i.data.status));
    const counts = [tools ? tools + ' 项工具' : '', thoughts ? thoughts + ' 段思考' : '']
      .filter(Boolean)
      .join(' · ');
    const last = items.at(-1),
      status = live
        ? ' · ' + (last.kind === 'tool' ? (last.data.name || '工具') + ' 执行中' : '思考中')
        : '';
    const meta = node.querySelector('.chat-process-meta');
    if (meta.textContent !== counts + status) meta.textContent = counts + status;
    node.dataset.active = String(live);
    chatReconcileNodes(
      node.lastElementChild,
      items.map((i) => i.node),
    );
  }
  chatReconcileNodes(holder, nodes);
  const retained = new Set(groups.map((g) => g.node.dataset.groupKey));
  for (const key of v.processGroups.keys()) if (!retained.has(key)) v.processGroups.delete(key);
  for (const [key, item] of v.renderedItems || [])
    if (renderedItems.get(key) !== item) {
      item.node?.remove();
      item.node = null;
      item.body = null;
    }
  v.renderedItems = renderedItems;
}
function chatFlush() {
  const c = ChatUI;
  c.frame = 0;
  if (!$('#chat-messages')) return;
  const v = chatView(),
    bottom = chatAtBottom();
  chatRenderProcessGroups();
  c.dirty.clear();
  $('#chat-empty').hidden = !!v.order.length;
  $('#chat-older').hidden = v.order.length <= v.limit;
  if (bottom) chatBottom();
  else $('#chat-latest').hidden = false;
  chatSyncChrome();
}
function chatRenderHistory(scroll = true) {
  const v = chatView();
  chatRenderProcessGroups();
  ChatUI.dirty.clear();
  $('#chat-empty').hidden = !!v.order.length;
  $('#chat-older').hidden = v.order.length <= v.limit;
  if (scroll) {
    if (!v.order.length) $('#chat-scroll').scrollTop = 0;
    else if (v.follow !== false) chatBottom();
    else $('#chat-scroll').scrollTop = v.scrollTop || 0;
  }
  $('#chat-latest').hidden = !v.order.length || chatAtBottom();
}
function chatCreateItem(item) {
  if (item.kind === 'review') {
    chatCreateReview(item);
    return;
  }
  const article = document.createElement('article');
  article.className = 'chat-message chat-message-' + item.kind;
  article.dataset.key = item.key;
  const header = document.createElement('div'),
    label = document.createElement('span'),
    actions = document.createElement('div');
  header.className = 'chat-message-head';
  label.className = 'chat-message-label';
  label.textContent =
    {
      user: item.data.parent_receipt ? '你 · 补充指令' : '你',
      assistant: chatProviderName(),
      reasoning: '思考过程',
      tool: '工具执行',
      approval: '需要确认',
      error: '未完成',
    }[item.kind] || item.kind;
  actions.className = 'chat-message-actions';
  const copyButton = document.createElement('button');
  copyButton.type = 'button';
  copyButton.title = '复制消息';
  copyButton.setAttribute('aria-label', '复制消息');
  copyButton.innerHTML = chatIcon('copy');
  copyButton.onclick = () => chatCopyText(item.text);
  actions.append(copyButton);
  if (item.kind === 'user') {
    const edit = document.createElement('button');
    edit.textContent = '编辑为新消息';
    edit.onclick = async () => {
      if (await chatReplaceDraft(item.text))
        chatStatus('已复制到输入框；发送后是新消息，不会撤销先前的工具操作。');
    };
    actions.append(edit);
  }
  header.append(label, actions);
  article.append(header);
  let body;
  if (['reasoning', 'tool'].includes(item.kind)) {
    const details = document.createElement('details'),
      summary = document.createElement('summary');
    body = document.createElement('pre');
    details.append(summary, body);
    article.append(details);
  } else {
    body = document.createElement('div');
    body.className = 'chat-message-body';
    article.append(body);
  }
  item.node = article;
  item.body = body;
  item.renderedSignature = null;
  if (item.kind === 'user' && item.data.attachments?.length) {
    const files = document.createElement('div');
    files.className = 'chat-message-files';
    for (const f of item.data.attachments) {
      const badge = document.createElement('span');
      badge.textContent = f.name || '附件';
      badge.title = f.mime || '';
      files.append(badge);
    }
    article.append(files);
  }
  if (item.kind === 'approval') {
    const fields = item.data.details || {},
      context =
        fields.message ||
        fields.command ||
        fields.commandActions ||
        fields.changes ||
        fields.fileChanges;
    if (context) {
      const details = document.createElement('details'),
        summary = document.createElement('summary'),
        pre = document.createElement('pre');
      summary.textContent = '操作内容';
      pre.textContent =
        (typeof context === 'string' ? context : JSON.stringify(context, null, 2)) +
        (fields.cwd ? '\n工作目录：' + fields.cwd : '');
      details.open = true;
      details.append(summary, pre);
      article.append(details);
    }
    chatApproval(item);
  }
}
function chatUpdateItem(item) {
  if (item.kind === 'review') {
    chatUpdateReview(item);
    return;
  }
  const signature =
    item.text +
    '\0' +
    (item.data.status || '') +
    '\0' +
    (item.data.name || '') +
    '\0' +
    !!(item.data.isError || item.data.is_error);
  if (item.renderedSignature === signature && item.body?.hasChildNodes()) return;
  item.renderedSignature = signature;
  if (item.kind === 'assistant') {
    if (typeof chatRichMarkdown === 'function') chatRichMarkdown(item.body, item.text);
    else item.body.textContent = item.text;
  } else if (item.kind === 'tool') {
    const summary = item.node.querySelector('summary');
    let parsed;
    try {
      parsed = JSON.parse(item.text);
    } catch {}
    const failed = chatProcessFailure(item);
    if (failed && !item.node.classList.contains('chat-tool-failed'))
      item.node.querySelector('details').open = true;
    item.node.classList.toggle('chat-tool-failed', !!failed);
    summary.textContent =
      (item.data.name || '工具') + ' · ' + (failed ? '执行异常' : chatStateName(item.data.status));
    item.body.textContent = parsed ? JSON.stringify(parsed, null, 2) : item.text;
  } else {
    item.body.textContent = item.text;
    if (item.kind === 'reasoning')
      item.node.querySelector('summary').textContent =
        '查看思考过程 · ' + item.text.length + ' 字符';
  }
}
async function chatCopyText(text) {
  if (typeof copy === 'function') {
    await copy(text);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    chatStatus('已复制');
  } catch {
    chatStatus('浏览器未允许访问剪贴板，可选中文字复制');
  }
}
