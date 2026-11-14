function chatApproval(item) {
  if (!item.node) return;
  item.node
    .querySelectorAll('.chat-approval-actions, .chat-approval-kv')
    .forEach((node) => node.remove());
  const data = item.data;
  const cmd = data.details?.command || data.command;
  const cwd = data.details?.cwd || data.cwd;
  const risk = data.details?.risk || data.details?.reason;
  if (cmd || cwd || risk) {
    const kv = document.createElement('dl');
    kv.className = 'chat-approval-kv';
    for (const [label, val] of [
      ['执行命令', cmd],
      ['作用目录', cwd],
      ['风险提示', risk],
    ]) {
      if (!val) continue;
      const dt = document.createElement('dt'),
        dd = document.createElement('dd');
      dt.textContent = label;
      dd.textContent = typeof val === 'string' ? val : JSON.stringify(val);
      kv.append(dt, dd);
    }
    if (kv.childElementCount) item.node.append(kv);
  }
  const box = document.createElement('div');
  box.className = 'chat-approval-actions';
  if (data.resolved || item.answered) {
    box.textContent = data.resolution === 'closed' ? '此请求已结束' : '回答已提交，等待原生处理';
    box.setAttribute('role', 'status');
    item.node.append(box);
    return;
  }
  async function answer(value) {
    const op =
      item.answerRequest ||
      (item.answerRequest = {
        receipt: uid(),
        value,
        id: ChatUI.selected?.id,
        project: ChatUI.project,
        identity: S.session,
        view: chatView(),
      });
    if (
      op.busy ||
      op.identity !== S.session ||
      item.data.resolved ||
      item.answered ||
      !['running', 'starting'].includes(op.view.sessionStatus)
    )
      return;
    op.busy = true;
    op.error = '';
    chatApproval(item);
    try {
      const result = await chatAPI(
        'chat_answer',
        { id: op.id, receipt: op.receipt, request_id: data.request_id, answer: op.value },
        op.project,
      );
      if (op.identity !== S.session) return;
      if (['error', 'uncertain', 'cancelled', 'interrupted'].includes(result.state))
        throw new Error('回答还未确认送达，点“重试”。');
      item.answered = true;
    } catch (error) {
      if (op.identity === S.session) {
        op.error = error.message;
        if (chatViewVisible(op.view, op.identity)) chatStatus(error.message, true);
      }
    } finally {
      op.busy = false;
      if (op.identity === S.session && [...ChatUI.views.values()].includes(op.view)) {
        chatApproval(item);
        if (chatViewVisible(op.view, op.identity)) chatSyncChrome();
      }
    }
  }
  // The response owns this card, even while its conversation is detached.
  // A retry always retains the user's original answer and receipt.
  if (item.answerRequest) {
    const op = item.answerRequest,
      note = document.createElement('span');
    note.setAttribute('role', 'status');
    note.textContent = op.busy ? '正在提交回答…' : op.error || '回答仍待确认';
    box.append(note);
    if (!op.busy) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.textContent = '重试';
      retry.onclick = () => answer(op.value);
      box.append(retry);
    }
    item.node.append(box);
    return;
  }
  function button(label, value) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.onclick = () => {
      try {
        answer(typeof value === 'function' ? value() : value);
      } catch (e) {
        chatStatus(e.message, true);
      }
    };
    box.append(b);
  }
  if (data.method === 'confirm') {
    button('允许本次', true);
    button('拒绝', false);
  } else if (['input', 'editor'].includes(data.method)) {
    const input = document.createElement('textarea');
    input.setAttribute('aria-label', data.text || '回答');
    input.value = data.details?.prefill || '';
    box.append(input);
    button('提交回答', () => input.value);
    button('取消', null);
  } else if (data.method === 'claude/question') {
    const inputs = [];
    for (const q of data.details?.questions || []) {
      const field = document.createElement('fieldset'),
        legend = document.createElement('legend');
      legend.textContent = q.question;
      field.append(legend);
      const select = document.createElement('select');
      select.multiple = !!q.multiSelect;
      select.setAttribute('aria-label', q.question);
      if (!q.multiSelect) {
        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = '请选择，或填写其他答案';
        select.append(empty);
      }
      for (const o of q.options || []) {
        const option = document.createElement('option');
        option.value = o.label;
        option.textContent = o.label + (o.description ? ' — ' + o.description : '');
        select.append(option);
      }
      const other = document.createElement('input');
      other.placeholder = '其他答案';
      other.maxLength = 10000;
      other.setAttribute('aria-label', q.question + ' · 其他答案');
      field.append(select, other);
      box.append(field);
      inputs.push({ q, select, other });
    }
    button('提交回答', () =>
      Object.fromEntries(
        inputs.map(({ q, select, other }) => {
          let values = [...select.selectedOptions].map((o) => o.value).filter(Boolean),
            custom = other.value.trim();
          if (custom) values = q.multiSelect ? [...values, custom] : [custom];
          if (!values.length) throw new Error('请回答：' + q.question);
          return [q.id, values];
        }),
      ),
    );
    button('取消', null);
  } else if (typeof chatExtraApproval === 'function' && chatExtraApproval(data, box, button)) {
  } else if (data.details?.questions?.length) {
    const inputs = [];
    for (const q of data.details.questions) {
      const label = document.createElement('label');
      label.textContent = q.question || q.header || q.id;
      let input;
      if (q.options?.length) {
        input = document.createElement('select');
        for (const o of q.options) {
          const option = document.createElement('option');
          option.value = o.label || o.value;
          option.textContent = (o.label || o.value) + (o.description ? ' — ' + o.description : '');
          input.append(option);
        }
      } else {
        input = document.createElement('input');
        if (q.isSecret) input.type = 'password';
      }
      input.setAttribute('aria-label', label.textContent);
      label.append(input);
      box.append(label);
      inputs.push([q.id, input]);
    }
    button('提交回答', () => Object.fromEntries(inputs.map(([id, input]) => [id, [input.value]])));
  } else {
    const choices = data.choices || data.options || [];
    for (const choice of choices) {
      const value = typeof choice === 'string' ? choice : (choice.value ?? choice.id ?? choice);
      const label =
        typeof choice === 'string'
          ? { accept: '允许本次', acceptForSession: '本会话允许', decline: '拒绝', cancel: '取消' }[
              choice
            ] || choice
          : choice.label || JSON.stringify(value);
      button(label, value);
    }
    if (!choices.length) {
      const note = document.createElement('p');
      note.textContent = '此原生请求没有可用选项。可中断当前任务后，在电脑本机 CLI 处理此操作。';
      box.append(note);
    }
  }
  item.node.append(box);
}
function chatOutbox() {
  const holder = $('#chat-outbox');
  if (!holder) return;
  holder.replaceChildren();
  if (chatView().recoveredQueueDrafts?.length || chatView().recoveredQueueDraft) {
    const recover = document.createElement('button');
    recover.type = 'button';
    recover.className = 'chat-text-button';
    recover.id = 'chat-recover-queue-draft';
    recover.textContent = '取回已撤回消息';
    recover.onclick = () => chatRestoreQueueDraft();
    holder.append(recover);
  }
  for (const item of chatView().outbox.values()) {
    const row = document.createElement('div');
    row.className = 'chat-queued';
    const status = document.createElement('span'),
      text = document.createElement('span');
    status.textContent = chatStateName(item.state);
    text.textContent = item.text || '图片与文件';
    row.append(status, text);
    if (item.state === 'queued') {
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.textContent = '撤回';
      cancel.onclick = async () => {
        cancel.disabled = true;
        try {
          await chatCancelQueued(item.receipt, item.text);
        } finally {
          cancel.disabled = false;
        }
      };
      row.append(cancel);
    }
    holder.append(row);
  }
}
async function chatCancelQueued(receipt, text) {
  const gen = ChatUI.generation,
    id = ChatUI.selected?.id,
    v = chatView();
  if (!id) return;
  const key = 'cancel:' + receipt;
  v.cancelOps = v.cancelOps || new Map();
  if (!v.cancelOps.has(key)) v.cancelOps.set(key, uid());
  try {
    const result = await chatAPI('chat_cancel', {
      id,
      receipt: v.cancelOps.get(key),
      target: receipt,
    });
    if (result.state !== 'completed') {
      if (chatCurrent(gen, v)) chatStatus('撤回仍待确认，消息暂时保留在队列中。');
      return;
    }
    v.outbox.delete(receipt);
    v.queueTicket = (v.queueTicket || 0) + 1;
    v.queue = v.queue.filter((item) => item.receipt !== receipt);
    if (chatCurrent(gen, v)) {
      chatOutbox();
      chatStatus('已撤回尚未执行的消息');
      if (ChatUI.inspector === 'queue') chatLoadQueue();
    }
  } catch (e) {
    if (chatCurrent(gen, v)) chatStatus(e.message, true);
  }
}
