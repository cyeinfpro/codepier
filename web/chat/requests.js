async function chatWatchReceipt(id, receipt, gen, v, op) {
  const ticket = (op.watchTicket = (op.watchTicket || 0) + 1);
  for (let n = 0; n < 24; n++) {
    await new Promise((r) => setTimeout(r, n < 3 ? 250 : 750));
    if (
      !chatCurrent(gen, v) ||
      v.settingOp !== op ||
      op.watchTicket !== ticket ||
      op.state === 'error'
    )
      return;
    try {
      const result = await chatAPI('receipt', { id, receipt });
      if (
        !chatCurrent(gen, v) ||
        v.settingOp !== op ||
        op.watchTicket !== ticket ||
        op.state === 'error'
      )
        return;
      if (['completed', 'applied'].includes(result.state)) {
        v.settingOp = null;
        chatSettings(v.settings);
        chatStatus(v.pendingSettings ? '设置将在下一条消息生效' : '设置已由原生 CLI 接受');
        return;
      }
      if (['error', 'cancelled', 'uncertain', 'interrupted'].includes(result.state)) {
        op.state = result.state === 'uncertain' ? 'uncertain' : 'error';
        chatStatus('设置未生效，请查看错误或重试', true);
        chatSettings(v.settings);
        return;
      }
    } catch {}
  }
  if (chatCurrent(gen, v) && v.settingOp === op) {
    op.state = 'uncertain';
    chatStatus('原生设置仍在等待处理，重试会使用同一回执', true);
    chatSettings(v.settings);
  }
}
function chatCwdPopover() {
  ChatUI.popoverAnchor = 'chat-cwd-button';
  if (ChatUI.selected) {
    chatInspector('overview');
    return;
  }
  const box = $('#chat-popover');
  box.hidden = false;
  box.setAttribute('aria-label', '工作目录');
  box.innerHTML = `<div class="chat-popover-head"><strong>工作目录</strong><button type="button" id="chat-cwd-close" aria-label="关闭工作目录">×</button></div><p class="chat-popover-help">使用项目根目录或项目内子目录，不改变项目授权。不同目录的草稿分别保留。</p><input id="chat-cwd-input" aria-label="项目子目录" aria-describedby="chat-cwd-error" placeholder="例如 src"><p id="chat-cwd-error" role="alert" class="chat-popover-help"></p><button type="button" id="chat-cwd-apply" class="chat-primary-button">应用目录</button>`;
  const input = $('#chat-cwd-input');
  input.value = ChatUI.cwd;
  $('#chat-cwd-close').onclick = () => chatClosePopover(true);
  const apply = () => {
    const cwd = input.value.trim().replaceAll('\\', '/') || '.';
    if (
      cwd.startsWith('/') ||
      /^[a-z]:/i.test(cwd) ||
      cwd.split('/').includes('..') ||
      /[\x00-\x1f]/.test(cwd)
    ) {
      $('#chat-cwd-error').textContent =
        '请使用项目内的相对目录，例如 src；不能使用绝对路径或 ..。';
      input.setAttribute('aria-invalid', 'true');
      return;
    }
    chatSwitch(null, ChatUI.project, { cwd }).catch((e) => chatStatus(e.message, true));
  };
  $('#chat-cwd-apply').onclick = apply;
  input.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      apply();
    }
  };
  chatPositionLayers();
  input.focus();
}
function chatLocalCommand(text) {
  return /^\/(model|thinking|stats|new|queue|commands)\s*$/.test(text.trim());
}
function chatDraftBytes(v = chatView()) {
  const text = v.draft.trim();
  if (v.measuredDraft !== text) {
    v.measuredDraft = text;
    v.draftBytes = new TextEncoder().encode(text).length;
  }
  return v.draftBytes;
}
function chatDraftLimit(v = chatView()) {
  const bytes = chatDraftBytes(v),
    exceeded = bytes > CHAT_MESSAGE_MAX_BYTES,
    input = $('#chat-compose'),
    hint = $('#chat-text-limit');
  if (input) input.setAttribute('aria-invalid', String(exceeded));
  if (hint) {
    hint.hidden = bytes < CHAT_MESSAGE_MAX_BYTES * 0.9;
    hint.textContent = hint.hidden
      ? ''
      : `${bytes.toLocaleString()} / ${CHAT_MESSAGE_MAX_BYTES.toLocaleString()} 字节${exceeded ? ' · 消息过长' : ''}`;
  }
  return exceeded ? '消息不能超过 64 KiB，请缩短内容或改用附件' : '';
}
async function chatSend(retry = false) {
  const c = ChatUI,
    v = chatView(),
    project = c.project,
    identity = S.session;
  if (v.busy || v.resumeBusy) return;
  const visible = () => chatViewVisible(v, identity);
  if (!v.pending && !retry && chatLocalCommand(v.draft) && chatBuiltin(v.draft.trim())) return;
  if (['stopping', 'orphaned'].includes(c.selected?.status)) {
    chatStatus(
      c.selected.status === 'stopping'
        ? '正在等待会话进程退出，草稿已保留。'
        : '会话进程状态待核实，请先检查电脑。草稿已保留。',
      true,
    );
    return;
  }
  const target = chatProjectInfo(project, c.selected || {});
  if (!project || target.reason) {
    chatStatus(target.reason || '请先选择项目', true);
    if (!project) chatNewSession();
    return;
  }
  if (v.settingOp && !['error'].includes(v.settingOp.state)) {
    chatStatus('请等待模型设置确认后再发送');
    return;
  }
  if (!v.pending && v.files.some((f) => !f.ready)) {
    chatStatus('附件仍在上传，或需要重试失败的附件', true);
    return;
  }
  if (v.pending && !retry) {
    chatStatus('上一条请求尚未确认，请重试', true);
    return;
  }
  if (!v.pending) {
    if (chatImageBytes(v.files) > CHAT_ATTACHMENT_MAX_BYTES) {
      chatStatus('每条消息的图片合计不能超过 20 MiB', true);
      return;
    }
    const text = v.draft.trim();
    if (chatDraftBytes(v) > CHAT_MESSAGE_MAX_BYTES) {
      chatStatus('消息不能超过 64 KiB', true);
      return;
    }
    if (!text && !v.files.length) return;
    if (chatBuiltin(text)) return;
    const resume = c.selected && !['running', 'starting'].includes(c.selected.status);
    v.pending = {
      id: (!resume && c.selected?.id) || uid(),
      receipt: uid(),
      text,
      attachments: v.files.map((f) => f.file),
      cli: c.selected?.provider || c.provider,
      cwd: c.cwd,
      model: v.selectedModel,
      effort: v.selectedEffort,
      started: !!c.selected && !resume,
      continue_session: resume ? c.selected.id : null,
      action: v.active && v.mode === 'steer' ? 'chat_steer' : 'chat_prompt',
    };
    // A lost explicit-resume response may already have started the process.
    // Continue that exact launch rather than creating a second continuation.
    if (resume && v.resumeRequest) {
      v.pending.id = v.resumeRequest.id;
      v.pending.startArgs = v.resumeRequest;
    }
  }
  const pending = v.pending;
  v.outbox.set(pending.receipt, { ...pending, state: 'sending' });
  v.busy = true;
  chatOutbox();
  chatSyncChrome();
  chatStatus(pending.continue_session ? '正在恢复原生会话…' : '正在发送…');
  try {
    if (!pending.started) {
      const row = await chatAPI(
        'start',
        pending.startArgs || {
          id: pending.id,
          cli: pending.cli,
          mode: 'chat',
          cwd: pending.cwd,
          ...(pending.continue_session ? { continue_session: pending.continue_session } : {}),
          ...(pending.model != null ? { model: pending.model } : {}),
          ...(pending.effort != null ? { effort: pending.effort } : {}),
        },
        project,
      );
      pending.started = true;
      pending.row = row;
      if (v.resumeId === pending.id) {
        v.resumeId = null;
        v.resumeRequest = null;
      }
    }
    // A two-stage first send must never continue under a different login.
    if (S.session !== identity) return;
    const result = await chatAPI(
      pending.action,
      {
        id: pending.id,
        receipt: pending.receipt,
        text: pending.text,
        attachments: pending.attachments,
      },
      project,
    );
    if (S.session !== identity) return;
    if (['error', 'cancelled', 'uncertain', 'interrupted'].includes(result.state))
      throw Object.assign(
        new Error(
          '这条请求状态为「' +
            chatStateName(result.state) +
            '」，没有重新执行。请核对历史后再决定。',
        ),
        { code: result.state === 'uncertain' ? 'CLI_UNCERTAIN' : 'CLI_RECEIPT_TERMINAL' },
      );
    v.pending = null;
    if (v.outbox.has(pending.receipt))
      v.outbox.get(pending.receipt).state = result.state || 'queued';
    if (v.draft.trim() === pending.text) v.draft = '';
    v.files = v.files.filter((f) => {
      if (!pending.attachments.includes(f.file)) return true;
      if (f.preview) URL.revokeObjectURL(f.preview);
      return false;
    });
    const current = visible();
    if (current) chatRememberView();
    if (pending.row && S.session === identity) {
      const oldKey = [...c.views].find(([, view]) => view === v)?.[0];
      c.views.set(chatKey(project, pending.id), v);
      if (oldKey && oldKey !== chatKey(project, pending.id)) c.views.delete(oldKey);
    }
    if (!current) return;
    if (pending.row) {
      c.selected = pending.row;
      v.cursor = 0;
      c.cursor = 0;
      v.configForResume = false;
      v.selectedModel = null;
      v.selectedEffort = null;
      chatConnect();
      chatList();
    }
    chatRestore(false);
    chatStatus(
      v.active ? '' : pending.action === 'chat_steer' ? '补充指令已提交' : '已接收，等待本机处理',
    );
  } catch (e) {
    if (S.session !== identity) return;
    const rejected = [
      'CLI_INVALID',
      'CLI_RECEIPT_TERMINAL',
      'ATTACHMENT_NOT_FOUND',
      'CLI_BACKPRESSURE',
      'CLI_QUOTA',
      'CLI_LIMIT',
      'INVALID_CWD',
      'CLI_MISSING',
      'CLI_CHAT_AGENT_UPDATE_REQUIRED',
      'CLI_AGENT_UPDATE_REQUIRED',
      'CLI_NOT_RUNNING',
    ].includes(e.code);
    if (rejected && !pending.started && v.resumeRequest?.id === pending.id) {
      v.resumeId = null;
      v.resumeRequest = null;
    }
    if (rejected) {
      const current = visible();
      v.pending = null;
      v.outbox.delete(pending.receipt);
      if (pending.row) {
        const key = [...c.views].find(([, view]) => view === v)?.[0];
        if (key) c.views.delete(key);
        c.views.set(chatKey(project, pending.id), v);
        v.sessionRow = pending.row;
        v.sessionStatus = pending.row.status;
        if (current) {
          c.selected = pending.row;
          v.cursor = 0;
          chatConnect();
          chatRestore(false);
        }
      }
    } else if (v.outbox.has(pending.receipt)) v.outbox.get(pending.receipt).state = 'error';
    if (visible()) {
      chatOutbox();
      chatStatus(e.message, true);
      $('#chat-retry').hidden = !v.pending;
    }
  } finally {
    v.busy = false;
    if (visible()) chatSyncChrome();
  }
}
