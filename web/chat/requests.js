async function chatWatchReceipt(id, receipt, gen, v, op) {
  const ticket = (op.watchTicket = (op.watchTicket || 0) + 1);
  for (let n = 0; n < 24; n++) {
    await new Promise((r) => setTimeout(r, n < 3 ? 250 : 750));
    if (
      !chatCurrent(gen, v) ||
      v.settingOp !== op ||
      op.watchTicket !== ticket ||
      op.state === "error"
    )
      return;
    try {
      const result = await chatAPI("receipt", { id, receipt });
      if (
        !chatCurrent(gen, v) ||
        v.settingOp !== op ||
        op.watchTicket !== ticket ||
        op.state === "error"
      )
        return;
      if (
        v.v2State &&
        typeof CP !== "undefined" &&
        CP.nativeState?.reconcileCommandOverlay
      ) {
        const mappedStatus =
          result.state === "completed" || result.state === "applied"
            ? "applied"
            : ["queued", "claimed"].includes(result.state)
              ? "accepted"
              : result.state === "uncertain"
                ? "uncertain"
                : ["error", "cancelled", "interrupted"].includes(result.state)
                  ? "failed"
                  : undefined;
        v.v2State = CP.nativeState.reconcileCommandOverlay(v.v2State, {
          receipt,
          generation: op.generation,
          status: mappedStatus,
          outcome: result.outcome,
          actual: result.actual,
          appliedRevision:
            result.applied_revision ?? result.projection_revision ?? null,
        });
        if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
      }
      if (result.actual && typeof result.actual === "object") {
        v.settings = { ...(v.settings || {}), ...result.actual };
      }
      if (["completed", "applied"].includes(result.state)) {
        if (result.outcome === "partial") {
          op.outcome = "partial";
          op.state = "error";
          chatSettings(v.settings);
          chatStatus(
            "部分设置已生效；当前模型与强度已回读，请核对后重试剩余项",
            true,
          );
          return;
        }
        v.settingOp = null;
        chatSettings(v.settings);
        chatStatus(
          v.pendingSettings
            ? "设置将在下一条消息生效"
            : "设置已由原生 CLI 接受",
        );
        return;
      }
      if (
        ["error", "cancelled", "uncertain", "interrupted"].includes(
          result.state,
        )
      ) {
        op.state = result.state === "uncertain" ? "uncertain" : "error";
        op.outcome =
          result.outcome ||
          (result.state === "uncertain" ? "unknown" : op.outcome);
        chatStatus(
          op.state === "uncertain"
            ? "原生设置仍在核实中，重试会使用同一确认编号"
            : "设置返回错误，实际配置待核实；可能部分生效",
          true,
        );
        chatSettings(v.settings);
        return;
      }
    } catch {}
  }
  if (chatCurrent(gen, v) && v.settingOp === op) {
    op.state = "uncertain";
    if (
      v.v2State &&
      typeof CP !== "undefined" &&
      CP.nativeState?.reconcileCommandOverlay
    ) {
      v.v2State = CP.nativeState.reconcileCommandOverlay(v.v2State, {
        receipt,
        generation: op.generation,
        status: "uncertain",
        outcome: "unknown",
      });
      if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
    }
    chatStatus("原生设置仍在等待处理，重试会使用同一确认编号", true);
    chatSettings(v.settings);
  }
}
function chatCwdPopover() {
  ChatUI.popoverAnchor = "chat-cwd-button";
  if (ChatUI.selected) {
    chatInspector("overview");
    return;
  }
  const box = $("#chat-popover");
  box.hidden = false;
  box.setAttribute("aria-label", "工作目录");
  box.innerHTML = `<div class="chat-popover-head"><strong>工作目录</strong><button type="button" id="chat-cwd-close" aria-label="关闭工作目录">×</button></div><p class="chat-popover-help">使用项目根目录或项目内子目录，不改变项目授权。不同目录的草稿分别保留。</p><input id="chat-cwd-input" autocapitalize="none" autocorrect="off" spellcheck="false" enterkeyhint="done" aria-label="项目子目录" aria-describedby="chat-cwd-error" placeholder="例如 src"><p id="chat-cwd-error" role="alert" class="chat-popover-help"></p><button type="button" id="chat-cwd-apply" class="chat-primary-button">应用目录</button>`;
  const input = $("#chat-cwd-input");
  input.value = ChatUI.cwd;
  $("#chat-cwd-close").onclick = () => chatClosePopover(true);
  const apply = () => {
    const cwd = input.value.trim().replaceAll("\\", "/") || ".";
    if (
      cwd.startsWith("/") ||
      /^[a-z]:/i.test(cwd) ||
      cwd.split("/").includes("..") ||
      /[\x00-\x1f]/.test(cwd)
    ) {
      $("#chat-cwd-error").textContent =
        "请使用项目内的相对目录，例如 src；不能使用绝对路径或 ..。";
      input.setAttribute("aria-invalid", "true");
      return;
    }
    chatSwitch(null, ChatUI.project, { cwd }).catch((e) =>
      chatStatus(e.message, true),
    );
  };
  $("#chat-cwd-apply").onclick = apply;
  input.onkeydown = (e) => {
    if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) {
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
    input = $("#chat-compose"),
    hint = $("#chat-text-limit");
  if (input) input.setAttribute("aria-invalid", String(exceeded));
  if (hint) {
    hint.hidden = bytes < CHAT_MESSAGE_MAX_BYTES * 0.9;
    hint.textContent = hint.hidden
      ? ""
      : `${bytes.toLocaleString()} / ${CHAT_MESSAGE_MAX_BYTES.toLocaleString()} 字节${exceeded ? " · 消息过长" : ""}`;
  }
  return exceeded ? "消息不能超过 64 KiB，请缩短内容或改用附件" : "";
}
function chatBuildExplicitSelection(v) {
  const model =
    typeof chatModelCurrent === "function"
      ? chatModelCurrent(v)
      : v.selectedModel;
  if (!model) return null;
  const source = v.configForResume ? v.catalog : v.settings || v.catalog;
  const effort =
    v.selectedEffort ??
    v.pendingSettings?.effort ??
    source?.effort ??
    source?.thinkingLevel ??
    source?.reasoningEffort ??
    null;
  return {
    model,
    options: effort ? { effort } : {},
  };
}
async function chatSend(retry = false) {
  const c = ChatUI,
    v = chatView(),
    project = c.project,
    identity = S.session;
  if (v.busy || v.resumeBusy) return;
  const visible = () => chatViewVisible(v, identity);
  if (
    !v.pending &&
    !retry &&
    chatLocalCommand(v.draft) &&
    chatBuiltin(v.draft.trim())
  )
    return;
  if (["stopping", "orphaned"].includes(c.selected?.status)) {
    chatStatus(
      c.selected.status === "stopping"
        ? "正在等待会话进程退出，草稿已保留。"
        : "会话进程状态待核实，请先检查电脑。草稿已保留。",
      true,
    );
    return;
  }
  const target = chatProjectInfo(project, c.selected || {});
  if (!project || target.reason) {
    chatStatus(target.reason || "请先选择项目", true);
    if (!project) chatNewSession();
    return;
  }
  if (v.settingOp && !["error"].includes(v.settingOp.state)) {
    chatStatus("请等待模型设置确认后再发送");
    return;
  }
  if (!v.pending && v.files.some((f) => !f.ready)) {
    chatStatus("附件仍在上传，或需要重试失败的附件", true);
    return;
  }
  if (v.pending && !retry) {
    chatStatus("上一条请求尚未确认，请重试", true);
    return;
  }
  if (!v.pending) {
    if (chatImageBytes(v.files) > CHAT_ATTACHMENT_MAX_BYTES) {
      chatStatus("每条消息的图片合计不能超过 20 MiB", true);
      return;
    }
    const text = v.draft.trim();
    if (chatDraftBytes(v) > CHAT_MESSAGE_MAX_BYTES) {
      chatStatus("消息不能超过 64 KiB", true);
      return;
    }
    if (!text && !v.files.length) return;
    if (chatBuiltin(text)) return;
    const resume =
      c.selected && !["running", "starting"].includes(c.selected.status);
    const requestedMode = v.selectionMode || "inherit_at_dispatch";
    const explicitSelection =
      requestedMode === "explicit" ? chatBuildExplicitSelection(v) : null;
    const selectionMode =
      requestedMode === "explicit" && explicitSelection
        ? "explicit"
        : "inherit_at_dispatch";
    v.pending = {
      id: (!resume && c.selected?.id) || uid(),
      receipt: uid(),
      text,
      attachments: v.files.map((f) => f.file),
      cli: c.selected?.provider || c.provider,
      cwd: c.cwd,
      model: v.selectedModel,
      effort: v.selectedEffort,
      selection_mode: selectionMode,
      selection: selectionMode === "explicit" ? explicitSelection : null,
      started: !!c.selected && !resume,
      continue_session: resume ? c.selected.id : null,
      action: v.active && v.mode === "steer" ? "chat_steer" : "chat_prompt",
      generation: (v.v2SettingsGeneration = (v.v2SettingsGeneration || 0) + 1),
    };
    // A lost explicit-resume response may already have started the process.
    // Continue that exact launch rather than creating a second continuation.
    if (resume && v.resumeRequest) {
      v.pending.id = v.resumeRequest.id;
      v.pending.startArgs = v.resumeRequest;
    }
  }
  const pending = v.pending;
  if (
    v.v2State &&
    typeof CP !== "undefined" &&
    CP.nativeState?.upsertCommandOverlay
  ) {
    v.v2State = CP.nativeState.upsertCommandOverlay(v.v2State, {
      receipt: pending.receipt,
      generation: pending.generation || 1,
      action: pending.action,
      status: "pending",
      requested: { text: pending.text, selection_mode: pending.selection_mode },
    });
    if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
  }
  v.outbox.set(pending.receipt, { ...pending, state: "sending" });
  v.busy = true;
  chatOutbox();
  chatSyncChrome();
  chatStatus(pending.continue_session ? "正在恢复原生会话…" : "正在发送…");
  try {
    if (!pending.started) {
      const row = await chatAPI(
        "start",
        pending.startArgs || {
          id: pending.id,
          cli: pending.cli,
          mode: "chat",
          cwd: pending.cwd,
          ...(pending.continue_session
            ? { continue_session: pending.continue_session }
            : {}),
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
    const promptArgs = {
      id: pending.id,
      receipt: pending.receipt,
      text: pending.text,
      attachments: pending.attachments,
      ...(pending.selection_mode
        ? { selection_mode: pending.selection_mode }
        : {}),
      ...(pending.selection_mode === "explicit" && pending.selection
        ? { selection: pending.selection }
        : {}),
    };
    const result = await chatAPI(pending.action, promptArgs, project);
    if (S.session !== identity) return;
    if (
      ["error", "cancelled", "uncertain", "interrupted"].includes(result.state)
    )
      throw Object.assign(
        new Error(
          "这条请求状态为「" +
            chatStateName(result.state) +
            "」，没有重新执行。请核对历史后再决定。",
        ),
        {
          code:
            result.state === "uncertain"
              ? "CLI_UNCERTAIN"
              : "CLI_RECEIPT_TERMINAL",
        },
      );
    if (
      v.v2State &&
      typeof CP !== "undefined" &&
      CP.nativeState?.reconcileCommandOverlay
    ) {
      v.v2State = CP.nativeState.reconcileCommandOverlay(v.v2State, {
        receipt: pending.receipt,
        generation: pending.generation || 1,
        status: result.state === "completed" ? "applied" : "accepted",
        appliedRevision:
          result.applied_revision ?? result.projection_revision ?? null,
      });
      if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
    }
    v.pending = null;
    if (v.outbox.has(pending.receipt))
      v.outbox.get(pending.receipt).state = result.state || "queued";
    if (v.draft.trim() === pending.text) v.draft = "";
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
      if (oldKey && oldKey !== chatKey(project, pending.id))
        c.views.delete(oldKey);
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
      v.active
        ? ""
        : pending.action === "chat_steer"
          ? "补充指令已提交"
          : "已接收，等待本机处理",
    );
  } catch (e) {
    if (S.session !== identity) return;
    const rejected = [
      "CLI_INVALID",
      "CLI_RECEIPT_TERMINAL",
      "ATTACHMENT_NOT_FOUND",
      "CLI_BACKPRESSURE",
      "CLI_QUOTA",
      "CLI_LIMIT",
      "INVALID_CWD",
      "CLI_MISSING",
      "CLI_CHAT_AGENT_UPDATE_REQUIRED",
      "CLI_AGENT_UPDATE_REQUIRED",
      "CLI_NOT_RUNNING",
    ].includes(e.code);
    if (
      v.v2State &&
      typeof CP !== "undefined" &&
      CP.nativeState?.reconcileCommandOverlay
    ) {
      v.v2State = CP.nativeState.reconcileCommandOverlay(v.v2State, {
        receipt: pending.receipt,
        generation: pending.generation || 1,
        status: rejected ? "failed" : "uncertain",
        error: e.message,
      });
      if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
    }
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
    } else if (v.outbox.has(pending.receipt))
      v.outbox.get(pending.receipt).state = "error";
    if (visible()) {
      chatOutbox();
      chatStatus(e.message, true);
      $("#chat-retry").hidden = !v.pending;
    }
  } finally {
    v.busy = false;
    if (visible()) chatSyncChrome();
  }
}
