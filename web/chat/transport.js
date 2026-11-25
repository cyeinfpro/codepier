function chatConnection(state, text) {
  ChatUI.connection = state;
  const el = $("#chat-connection");
  if (el) {
    el.dataset.state = state;
    el.title = text;
    el.setAttribute("aria-label", "连接状态：" + text);
    el.querySelector("span").textContent = text;
  }
}
function chatDisconnect() {
  const source = ChatUI.source;
  ChatUI.source = null;
  source?.close();
  const activeScope = ChatUI.v2ActiveScope;
  ChatUI.v2ActiveScope = null;
  activeScope?.stopNative?.();
  if (ChatUI.views) {
    for (const view of ChatUI.views.values()) {
      view.v2Scope?.stopNative?.();
      view.v2OutputSubscriptions?.closeAll?.();
    }
  }
}
async function chatLoadNativeCapabilities(force = false) {
  const c = ChatUI;
  if (typeof CP === "undefined" || !CP.nativeState || !CP.nativeRuntime) {
    c.v2Capabilities = { native_view_v2: false, flags: {} };
    return c.v2Capabilities;
  }
  if (!force && c.v2Capabilities) return c.v2Capabilities;
  if (!force && c.v2CapabilitiesPromise) return c.v2CapabilitiesPromise;
  const identity = typeof S !== "undefined" ? S.session : null;
  const promise = (async () => {
    try {
      const data = await api("/api/native/capabilities");
      if (typeof S !== "undefined" && S.session !== identity) {
        return { native_view_v2: false, flags: {} };
      }
      const caps =
        data && data.native_view_v2 === true
          ? {
              native_view_v2: true,
              schema_version: data.schema_version ?? 2,
              flags: data.flags || {},
              budgets: data.budgets || {},
            }
          : { native_view_v2: false, flags: {} };
      c.v2Capabilities = caps;
      return caps;
    } catch {
      return { native_view_v2: false, flags: {} };
    } finally {
      if (c.v2CapabilitiesPromise === promise) c.v2CapabilitiesPromise = null;
    }
  })();
  c.v2CapabilitiesPromise = promise;
  return promise;
}
function chatNormalizeV2Item(rawItem) {
  if (!rawItem || typeof rawItem !== "object") return rawItem;
  const item = { ...rawItem };
  const body = item.body;
  if (body && typeof body === "object" && !Array.isArray(body)) {
    if (body.kind === "inline") {
      if (item.text === undefined) item.text = body.text ?? "";
      item._seenBodyText = body.text ?? "";
    } else if (body.kind === "ref") {
      item.body_ref = item.body_ref ?? body.body_ref ?? null;
      item.content_generation =
        item.content_generation ?? body.content_generation ?? 1;
      item.total_bytes = item.total_bytes ?? body.byte_length ?? null;
      item.partial = Boolean(body.partial ?? true);
      if (item.tail_text === undefined && typeof body.preview === "string") {
        item.tail_text = body.preview;
      }
    }
  }
  return item;
}
function chatNormalizeV2Snapshot(raw) {
  if (!raw || typeof raw !== "object") return raw;
  const pending = Array.isArray(raw.pending)
    ? raw.pending
    : Array.isArray(raw.pending?.items)
      ? raw.pending.items
      : raw.pending &&
          typeof raw.pending === "object" &&
          !("count" in raw.pending)
        ? raw.pending
        : [];
  return {
    ...raw,
    items: Array.isArray(raw.items) ? raw.items.map(chatNormalizeV2Item) : [],
    pending,
    history: raw.history
      ? {
          has_more: Boolean(raw.history.has_more ?? raw.history.hasMore),
          next_token:
            raw.history.next_token ??
            raw.history.nextToken ??
            raw.history.before ??
            null,
          page_revision:
            raw.history.page_revision ?? raw.projection_revision ?? 0,
          page_applied_cursor:
            raw.history.page_applied_cursor ?? raw.applied_cursor ?? 0,
        }
      : undefined,
  };
}
function chatNormalizeV2HistoryPage(page) {
  if (!page || typeof page !== "object") return page;
  return {
    ...page,
    next_token: page.next_token ?? page.nextToken ?? page.before ?? null,
    items: Array.isArray(page.items) ? page.items.map(chatNormalizeV2Item) : [],
  };
}
function chatV2ItemText(pItem) {
  if (!pItem) return "";
  if (pItem.partial) {
    if (pItem.body_loaded_bytes > 0 && typeof pItem.body === "string")
      return pItem.body;
    if (
      pItem.body &&
      typeof pItem.body === "object" &&
      typeof pItem.body.preview === "string"
    ) {
      return pItem.body.preview + (pItem.tail_text || "");
    }
    return pItem.tail_text ?? pItem.text ?? "";
  }
  if (typeof pItem.text === "string") return pItem.text;
  if (typeof pItem.body === "string") return pItem.body;
  if (
    pItem.body &&
    typeof pItem.body === "object" &&
    typeof pItem.body.text === "string"
  ) {
    return pItem.body.text;
  }
  return pItem.tail_text || "";
}
function chatApplyV2SessionMeta(c, v, sessionMeta) {
  if (!sessionMeta || typeof sessionMeta !== "object") return;
  if (c.selected && c.selected.id === (sessionMeta.id || c.selected.id)) {
    Object.assign(c.selected, sessionMeta);
  }
  v.sessionStatus = sessionMeta.status || v.sessionStatus;
  if ($("#chat-title") && sessionMeta.title)
    $("#chat-title").textContent = sessionMeta.title;
  if (
    sessionMeta.status &&
    !["running", "starting"].includes(sessionMeta.status)
  ) {
    v.active = null;
    v.phase = "";
    for (const key of v.approvalKeys || []) {
      const item = v.items.get(key);
      if (item && !item.data.resolved) {
        item.data.resolved = true;
        item.data.resolution = "closed";
        chatApproval(item);
      }
    }
    if ($("#chat-interrupt")) $("#chat-interrupt").hidden = true;
    chatConnection("stopped", chatStateName(sessionMeta.status));
    chatStatus(
      sessionMeta.error ||
        {
          stopping: "正在等待会话进程退出，草稿已保留。",
          orphaned: "会话进程状态待核实，请先检查电脑。草稿已保留。",
        }[sessionMeta.status] ||
        "会话已停止，发送消息可恢复原生历史",
    );
  }
}
function chatApplyV2ExtraMeta(c, v, meta) {
  if (!meta || typeof meta !== "object") return;
  if (meta.sync) v.v2Sync = meta.sync;
  if (meta.commands !== undefined) v.commands = meta.commands;
  if (meta.stats && typeof meta.stats === "object") v.stats = meta.stats;
  if (meta.queue !== undefined) {
    v.queue = Array.isArray(meta.queue)
      ? meta.queue
      : meta.queue?.commands || v.queue;
    if (c.inspector === "queue" && typeof chatQueueRender === "function")
      chatQueueRender();
  }
  if (meta.control && typeof meta.control === "object") {
    v.v2Control = { ...(v.v2Control || {}), ...meta.control };
    if (meta.control.action_availability) {
      v.v2ActionAvailability = meta.control.action_availability;
    }
  }
  if (meta.action_availability)
    v.v2ActionAvailability = meta.action_availability;
  if (meta.status_text) chatStatus(meta.status_text);
  if (meta.settings_result) {
    const res = meta.settings_result;
    const op = v.settingOp;
    if (v.v2State && res.receipt && CP?.nativeState?.reconcileCommandOverlay) {
      v.v2State = CP.nativeState.reconcileCommandOverlay(v.v2State, {
        receipt: res.receipt,
        outcome: res.outcome,
        actual: res.actual,
        appliedRevision: res.settings_revision ?? null,
      });
    }
    if (op && op.receipt === res.receipt) {
      if (res.actual) v.settings = { ...(v.settings || {}), ...res.actual };
      op.outcome = res.outcome;
      op.state = res.outcome === "unknown" ? "uncertain" : "error";
      chatSettings(v.settings);
      chatStatus(
        {
          partial: "部分设置已生效；当前模型与强度已回读，请核对后重试剩余项",
          applied: "实际配置已生效，但原操作回执仍为失败；无需重试",
          rejected: "原生回读确认设置未变更",
          unknown: "设置结果待核实；不能确认是否部分生效",
        }[res.outcome] || "设置结果待核实",
        res.outcome !== "applied",
      );
    }
  }
  if (meta.command_result) {
    const cr = meta.command_result;
    if (
      cr.state &&
      !["queued", "claimed", "running", "accepted", "uncertain"].includes(
        cr.state,
      )
    ) {
      for (const [key, receipt] of v.controlOps || []) {
        if (receipt === cr.receipt) v.controlOps.delete(key);
      }
    }
    if (cr.text || cr.name) {
      chatStatus(
        cr.text || cr.name + " · " + chatStateName(cr.state),
        cr.state === "error",
      );
    }
  }
}
function chatSyncV2ToView(
  v,
  extraMeta = null,
  dirtyOverride = null,
  structureOverride = false,
) {
  const c = ChatUI;
  const state = v.v2State;
  if (!state) return;
  const proj = state.projection;
  const fullReconcile = dirtyOverride === null || structureOverride;
  if (fullReconcile) {
    v.order = [...proj.order];
    for (const oldKey of v.items.keys()) {
      if (proj.items.has(oldKey)) continue;
      v.items.get(oldKey)?.node?.remove();
      v.items.delete(oldKey);
      v.approvalKeys?.delete(oldKey);
    }
    v.v2StructureDirty = true;
    v.structureDirty = true;
  }
  const keysToSync = fullReconcile ? proj.order : dirtyOverride;
  for (const key of keysToSync) {
    const source = proj.items.get(key);
    if (!source) continue;
    const pItem = { ...source };
    if (pItem.body?.kind === "inline") {
      pItem.text = pItem.body.text ?? "";
    } else if (pItem.body?.kind === "ref" && pItem.partial !== false) {
      pItem.body_ref ??= pItem.body.body_ref ?? null;
      pItem.content_generation ??= pItem.body.content_generation ?? 1;
      pItem.total_bytes ??= pItem.body.byte_length ?? null;
      pItem.partial = true;
    }
    const nextText = chatV2ItemText(pItem);
    const nextData = {
      ...pItem,
      type: pItem.kind,
      isError: Boolean(pItem.is_error || pItem.isError),
    };
    let uiItem = v.items.get(key);
    if (!uiItem) {
      uiItem = {
        key,
        kind: pItem.kind,
        text: nextText,
        data: nextData,
        node: null,
        body: null,
      };
      v.items.set(key, uiItem);
      v.v2StructureDirty = true;
      v.structureDirty = true;
    } else {
      const prevCompact =
        ["tool", "reasoning"].includes(uiItem.kind) &&
        !chatProcessFailure(uiItem);
      const prevKind = uiItem.kind;
      uiItem.kind = pItem.kind;
      uiItem.text = nextText;
      uiItem.data = nextData;
      const nextCompact =
        ["tool", "reasoning"].includes(uiItem.kind) &&
        !chatProcessFailure(uiItem);
      if (prevKind !== uiItem.kind || prevCompact !== nextCompact) {
        v.v2StructureDirty = true;
        v.structureDirty = true;
      }
    }
    if (uiItem.kind === "approval") {
      v.approvalKeys ??= new Set();
      if (uiItem.data.resolved || uiItem.answered) v.approvalKeys.delete(key);
      else v.approvalKeys.add(key);
      if (uiItem.data.resolved && uiItem.node) {
        uiItem.node.querySelector(".chat-approval-actions")?.remove();
        chatApproval(uiItem);
      }
    }
    if (uiItem.kind === "review" && c.inspector === "changes")
      chatInspectorRender();
    c.dirty.add(key);
    v.v2DirtyKeys.add(key);
  }
  if (fullReconcile) {
    v.v2StructureDirty = true;
    v.structureDirty = true;
  }
  if (proj.session) chatApplyV2SessionMeta(c, v, proj.session);
  if (proj.run) {
    v.active = proj.run.receipt || null;
    v.phase = proj.run.phase || "";
    if ($("#chat-interrupt")) $("#chat-interrupt").hidden = !v.active;
  }
  if (proj.stats) v.stats = proj.stats;
  if (proj.pendingSettings !== undefined)
    v.pendingSettings = proj.pendingSettings;
  if (proj.settings) v.settings = { ...(v.settings || {}), ...proj.settings };
  if (proj.capabilities?.action_availability) {
    v.v2ActionAvailability = proj.capabilities.action_availability;
  }
  if (extraMeta) chatApplyV2ExtraMeta(c, v, extraMeta);
  if (proj.settings || proj.pendingSettings !== undefined)
    chatSettings(v.settings);
  if (c.inspector === "overview") chatInspectorRender();
}
function chatConnectV1(c, gen, id, v) {
  c.cursor = v.cursor || 0;
  const source = new EventSource(
    "/api/native/sessions/" +
      encodeURIComponent(id) +
      "/events?" +
      new URLSearchParams({
        space_id: S.space_id || "",
        project: c.project,
        cursor: c.cursor,
      }),
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
      chatStatus("无法读取结构化事件：" + error.message, true);
    }
  };
  source.onmessage = receive;
  source.addEventListener("chat", receive);
  source.addEventListener("session", (e) => {
    if (!chatCurrent(gen, v) || source !== c.source) return;
    try {
      Object.assign(c.selected, JSON.parse(e.data));
      v.sessionStatus = c.selected.status;
      $("#chat-title").textContent = c.selected.title || "CLI 对话";
      if (!["running", "starting"].includes(c.selected.status)) {
        v.active = null;
        v.phase = "";
        for (const item of v.items.values())
          if (item.kind === "approval" && !item.data.resolved) {
            item.data.resolved = true;
            item.data.resolution = "closed";
            chatApproval(item);
          }
        $("#chat-interrupt").hidden = true;
        chatConnection("stopped", chatStateName(c.selected.status));
        chatStatus(
          c.selected.error ||
            {
              stopping: "正在等待会话进程退出，草稿已保留。",
              orphaned: "会话进程状态待核实，请先检查电脑。草稿已保留。",
            }[c.selected.status] ||
            "会话已停止，发送消息可恢复原生历史",
        );
      }
      chatSyncChrome();
      if (c.inspector === "overview") chatInspectorRender();
    } catch {}
  });
  source.addEventListener("error", (e) => {
    if (!chatCurrent(gen, v) || source !== c.source || !e.data) return;
    try {
      const error = JSON.parse(e.data);
      chatDisconnect();
      chatConnection("error", "连接受限");
      chatStatus(error.message || "访问权限已变化", true);
    } catch {}
  });
  source.onopen = () => {
    if (chatCurrent(gen, v) && source === c.source) {
      chatConnection("online", "实时连接");
      chatStatus("已连接 · Enter 发送");
      const op = v.settingOp;
      if (
        op &&
        ["sending", "queued", "claimed", "uncertain"].includes(op.state)
      )
        chatWatchReceipt(id, op.receipt, gen, v, op);
    }
  };
  source.onerror = (e) => {
    if (!e.data && chatCurrent(gen, v) && source === c.source) {
      chatConnection("reconnecting", "重连中");
      chatStatus("连接暂时中断，正在从已接收的位置恢复…");
    }
  };
}
async function chatConnectV2(c, gen, id, v) {
  const identitySession = S.session;
  const mapping = chatV2MappingSignature(c.project);
  const cache = chatV2Cache();
  let scopeKey = c.v2ScopeKeys.get(mapping.key) || v.v2State?.scopeKey || "";
  let initialSnap = null;
  if (scopeKey && cache) {
    const hit = cache.get({ scopeKey, sessionId: id, schemaVersion: 2 });
    if (hit?.snapshot) {
      const pendingCommands = v.v2State?.pendingCommands;
      v.v2State = CP.nativeState.installNativeSnapshot(hit.snapshot, {
        scopeKey,
        sessionId: id,
      });
      if (pendingCommands?.size)
        v.v2State.pendingCommands = new Map(pendingCommands);
      chatSyncV2ToView(v, hit.snapshot);
      if (chatCurrent(gen, v)) chatRenderHistory();
    }
  }
  if (!v.v2State) {
    try {
      initialSnap = await api(
        "/api/native/sessions/" + encodeURIComponent(id) + "/view",
      );
    } catch (err) {
      if (!chatCurrent(gen, v)) return;
      if (err?.status === 404 || err?.status === 409 || err?.status === 403) {
        chatConnection("error", "连接受限");
        chatStatus(err.message || "访问权限已变化", true);
        return;
      }
      chatConnectV1(c, gen, id, v);
      return;
    }
    if (!chatCurrent(gen, v) || S.session !== identitySession) return;
    const projState = initialSnap?.sync?.projection_state;
    if (projState === "projection_building" || projState === "building") {
      v.v2ProjectionState = projState;
      chatConnectV1(c, gen, id, v);
      return;
    }
    scopeKey = initialSnap.scope_key;
    c.v2ScopeKeys.set(mapping.key, scopeKey);
    const normalized = chatNormalizeV2Snapshot(initialSnap);
    const pendingCommands = v.v2State?.pendingCommands;
    v.v2State = CP.nativeState.installNativeSnapshot(normalized, {
      scopeKey,
      sessionId: id,
    });
    if (pendingCommands?.size)
      v.v2State.pendingCommands = new Map(pendingCommands);
    v.v2ProjectionState = projState || "ready";
    v.v2Halted = null;
    cache?.enqueue(
      v.v2State,
      () => chatCurrent(gen, v) && S.session === identitySession,
    );
    chatSyncV2ToView(v, initialSnap);
    if (chatCurrent(gen, v)) {
      chatRenderHistory();
      chatSyncChrome();
    }
  }
  if (!chatCurrent(gen, v)) return;
  const scope = {
    identity: { scopeKey, sessionId: id },
    authoritative: v.v2State,
    cache,
    initiallySynchronized: false,
    scheduleRender: () => {
      if (!chatCurrent(gen, v)) return;
      if (!c.frame) c.frame = requestAnimationFrame(chatFlush);
    },
  };
  v.v2Scope = scope;
  c.v2ActiveScope = scope;
  const io = {
    commitApplied: (sc, next, owner, summaryPatches, delta) => {
      if (sc.owner !== owner || !chatCurrent(gen, v)) return false;
      sc.authoritative = next;
      v.v2State = next;
      chatSyncV2ToView(v, null, delta?.dirtyKeys ?? null, delta?.structureDirty ?? false);
      for (const patch of summaryPatches) chatApplyV2ExtraMeta(c, v, patch);
      sc.authoritative = v.v2State;
      sc.cache?.enqueue(
        sc.authoritative,
        () => sc.owner === owner && chatCurrent(gen, v),
        { immutableRevision: true },
      );
      sc.scheduleRender?.();
      return true;
    },
    consumeAttempt: async ({ identity: idObj, after, signal, onFrame }) => {
      const qs = new URLSearchParams({
        after: after || "",
        ...(S.space_id ? { space_id: S.space_id } : {}),
      });
      const res = await fetch(
        "/api/native/sessions/" +
          encodeURIComponent(idObj.sessionId) +
          "/view-events?" +
          qs,
        {
          credentials: "same-origin",
          headers: {
            ...(S.space_id ? { "X-CodePier-Space": S.space_id } : {}),
            ...(S.session?.csrf ? { "X-RD-CSRF": S.session.csrf } : {}),
          },
          signal,
        },
      ).catch((error) => {
        throw new CP.nativeRuntime.NativeTransportReadError(error);
      });
      if (signal.aborted || !chatCurrent(gen, v))
        return { kind: "done", stablySynchronized: false };
      if (!res.ok) {
        let errBody = null;
        try {
          errBody = await res.json();
        } catch {}
        if (res.status === 401) {
          cache?.invalidateScope(idObj.scopeKey);
          endSession(false);
          return { kind: "blocked", reason: "unauthorized" };
        }
        if ([403, 404, 409].includes(res.status)) {
          return {
            kind: "blocked",
            reason: errBody?.error?.code || "HTTP_" + res.status,
            message: errBody?.error?.message,
          };
        }
        return { kind: "resume", stablySynchronized: false };
      }
      if (!res.body) return { kind: "resume", stablySynchronized: false };
      let stablySynchronized = false;
      await CP.nativeRuntime.parseSseFrames(
        res.body,
        async (frame) => {
          if (signal.aborted || !chatCurrent(gen, v)) return;
          if (!frame.data) return;
          let payload;
          try {
            payload = JSON.parse(frame.data);
          } catch (error) {
            throw new CP.nativeRuntime.NativeTransportReadError(error);
          }
          if (frame.event === "synchronized") {
            await onFrame({ ...payload, kind: "synchronized" });
            stablySynchronized = true;
            chatConnection("online", "实时连接");
            chatStatus("已连接 · Enter 发送");
            const op = v.settingOp;
            if (
              op &&
              ["sending", "queued", "claimed", "uncertain"].includes(op.state)
            ) {
              chatWatchReceipt(id, op.receipt, gen, v, op);
            }
            return;
          }
          if (frame.event === "reset_required") {
            await onFrame({ kind: "reset_required", ...payload });
            return;
          }
          if (frame.event === "blocked") {
            await onFrame({ kind: "blocked", ...payload });
            return;
          }
          if (frame.event === "error") {
            await onFrame({
              kind: "blocked",
              reason: payload.code || "stream_error",
              message: payload.message,
            });
            return;
          }
          if (frame.event === "batch" || payload.kind === "batch") {
            await onFrame({
              ...payload,
              _rawBytes: new TextEncoder().encode(frame.data).length,
            });
          }
        },
        signal,
      );
      return { kind: "resume", stablySynchronized };
    },
    replaceSnapshotBounded: async (sc, owner, signal) => {
      try {
        const snap = await api(
          "/api/native/sessions/" + encodeURIComponent(id) + "/view",
          { signal },
        );
        if (signal.aborted || sc.owner !== owner || !chatCurrent(gen, v))
          return "superseded";
        const projState = snap?.sync?.projection_state;
        if (projState === "projection_building" || projState === "building") {
          v.v2ProjectionState = projState;
          sc.stopNative?.();
          chatConnectV1(c, gen, id, v);
          return "fallback_v1";
        }
        const normalized = chatNormalizeV2Snapshot(snap);
        const applied = CP.nativeRuntime.applyBoundedSnapshotReplacement(
          sc,
          normalized,
          owner,
        );
        if (applied) {
          v.v2State = sc.authoritative;
          v.v2Halted = null;
          chatSyncV2ToView(v, snap);
          chatRenderHistory(false);
        }
        return applied ? "replaced" : "superseded";
      } catch (error) {
        if (signal.aborted || sc.owner !== owner || !chatCurrent(gen, v))
          return "superseded";
        const status = Number(error?.status ?? error?.response?.status ?? 0);
        if ([401, 403, 404, 410].includes(status)) {
          return {
            kind: "blocked",
            status,
            message:
              status === 401 || status === 403
                ? "会话访问权限已变化，请重新登录或检查权限"
                : "此会话已不存在或不再可访问",
          };
        }
        return status >= 400 && status < 500 && ![408, 429].includes(status)
          ? "terminal_failure"
          : "retryable_failure";
      }
    },
    publishResetHalted: (sc, outcome) => {
      if (!chatCurrent(gen, v)) return;
      if (outcome.reason === "PROJECTION_BUILDING") {
        v.v2ProjectionState = "projection_building";
        sc.stopNative?.();
        chatConnectV1(c, gen, id, v);
        return;
      }
      v.v2Halted = outcome;
      chatConnection("error", "同步受阻");
      chatStatus(
        outcome.reason === "SOURCE_GAP"
          ? "本机记录存在缺口，草稿与待确认操作已保留；请检查电脑连接"
          : outcome.reason === "SOURCE_CORRUPT"
            ? "本机记录校验异常，草稿已保留；请检查电脑记录"
            : outcome.reason === "SNAPSHOT_RECOVERY_FAILED"
              ? "暂时无法重新加载会话，草稿已保留；请刷新后重试"
              : "视图重置次数过多，草稿已保留；请刷新后重试",
        true,
      );
      chatSyncChrome();
    },
    publishBlocked: (sc, outcome) => {
      if (!chatCurrent(gen, v)) return;
      cache?.invalidateScope(sc.identity.scopeKey);
      chatConnection("error", "连接受限");
      chatStatus(outcome.message || "访问权限或项目目录配置已变化", true);
      chatSyncChrome();
    },
    publishFatal: (_sc, error) => {
      if (!chatCurrent(gen, v)) return;
      chatConnection("reconnecting", "重连中");
      chatStatus(error?.message || "连接暂时中断，正在恢复…", true);
    },
  };
  CP.nativeRuntime.startNativeTransport(scope, io);
}
function chatConnect() {
  const c = ChatUI,
    gen = c.generation,
    id = c.selected?.id,
    v = chatView();
  chatDisconnect();
  if (!id) return;
  if (typeof CP === "undefined" || !CP.nativeState || !CP.nativeRuntime) {
    chatConnectV1(c, gen, id, v);
    return;
  }
  if (c.v2Capabilities) {
    if (
      chatV2Enabled("native_snapshot_v2_read") &&
      v.v2ProjectionState !== "projection_building"
    ) {
      void chatConnectV2(c, gen, id, v);
    } else {
      chatConnectV1(c, gen, id, v);
    }
    return;
  }
  void (async () => {
    await chatLoadNativeCapabilities();
    if (!chatCurrent(gen, v) || c.selected?.id !== id) return;
    if (
      chatV2Enabled("native_snapshot_v2_read") &&
      v.v2ProjectionState !== "projection_building"
    ) {
      await chatConnectV2(c, gen, id, v);
    } else {
      chatConnectV1(c, gen, id, v);
    }
  })();
}
function chatEvent(e) {
  const c = ChatUI,
    v = chatView();
  if (e.type === "tool") v.phase = "tool";
  if (e.type === "reasoning") v.phase = "reasoning";
  if (e.type === "delta" || e.type === "message") v.phase = "reply";
  if (e.type === "user") v.phase = "working";
  chatScheduleLayout();
  if (e.type === "settings_result") {
    const op = v.settingOp;
    if (!op || op.receipt !== e.receipt) return;
    if (e.actual) v.settings = { ...(v.settings || {}), ...e.actual };
    op.outcome = e.outcome;
    op.state = "error";
    chatSettings(v.settings);
    chatStatus(
      {
        partial: "部分设置已生效；当前模型与强度已回读，请核对后重试剩余项",
        applied: "实际配置已生效，但原操作回执仍为失败；无需重试",
        rejected: "原生回读确认设置未变更",
        unknown: "设置结果待核实；不能确认是否部分生效",
      }[e.outcome] || "设置结果待核实",
      e.outcome !== "applied",
    );
    chatSyncChrome();
    return;
  }
  if (e.type === "settings") {
    v.settings = { ...(v.settings || {}), ...e };
    if (e.commands) v.commands = e.commands;
    chatSettings(v.settings);
    return;
  }
  if (e.type === "settings_pending") {
    v.pendingSettings = e.cleared ? null : e;
    chatSettings(v.settings);
    return;
  }
  if (e.type === "commands") {
    v.commands = e.commands || [];
    if (c.inspector === "commands") chatInspectorRender();
    return;
  }
  if (e.type === "stats") {
    v.stats = e.stats || e;
    if (c.inspector === "overview") chatInspectorRender();
    return;
  }
  if (e.type === "command_result") {
    if (
      e.state &&
      !["queued", "claimed", "running", "accepted", "uncertain"].includes(
        e.state,
      )
    ) {
      for (const [key, receipt] of v.controlOps || [])
        if (receipt === e.receipt) v.controlOps.delete(key);
    }
    chatStatus(e.text || e.name + " · " + chatStateName(e.state));
    if (e.state === "error") chatStatus(e.text || "原生指令失败", true);
    if (c.inspector) chatInspectorRender();
    return;
  }
  if (e.type === "done") {
    v.phase = "";
    if (!e.receipt || v.active === e.receipt) {
      v.active = null;
      v.interruptReceipt = null;
    }
    $("#chat-interrupt").hidden = !v.active;
    v.outbox.delete(e.receipt);
    chatOutbox();
    chatRenderProcessGroups();
    chatSyncChrome();
    chatStatus(e.text || chatStateName(e.status));
    chatList();
    if (c.inspector === "queue") chatLoadQueue();
    for (const item of v.items.values())
      if (
        item.kind === "approval" &&
        item.data.receipt === e.receipt &&
        !item.data.resolved
      ) {
        item.data.resolved = true;
        item.data.resolution = "closed";
        if (item.node) {
          item.node.querySelector(".chat-approval-actions")?.remove();
          chatApproval(item);
        }
      }
    return;
  }
  if (e.type === "queue" || e.type === "cancelled") {
    if (e.target) v.outbox.delete(e.target);
    chatOutbox();
    if (c.inspector === "queue") chatLoadQueue();
    return;
  }
  if (
    ![
      "user",
      "delta",
      "message",
      "reasoning",
      "tool",
      "approval",
      "error",
      "status",
      "review",
    ].includes(e.type)
  )
    return;
  if (e.type === "status") {
    chatStatus(e.text || e.status || "");
    return;
  }
  if (e.type === "error" && v.pendingSettings?.receipt === e.receipt) {
    v.pendingSettings = null;
    chatSettings(v.settings);
  }
  if (e.type === "error" && v.settingOp?.receipt === e.receipt) {
    v.settingOp.state = "error";
    chatSettings(v.settings);
    chatStatus(
      (e.text || "原生设置返回错误") + "；正在核实实际配置，可能部分生效",
      true,
    );
  }
  if (
    ["user", "delta", "message", "reasoning", "tool"].includes(e.type) &&
    [
      "已接收，等待本机处理",
      "补充指令已提交",
      "正在发送…",
      "正在恢复原生会话…",
    ].includes($("#chat-status").textContent)
  )
    chatStatus("");
  if (e.type === "user") {
    if (
      ["running", "starting"].includes(c.selected?.status) &&
      !e.parent_receipt &&
      !e.steering
    )
      v.active = e.receipt;
    v.outbox.delete(e.receipt);
    chatOutbox();
    $("#chat-interrupt").hidden = !v.active;
  }
  // Older Claude adapters wrote every final block as message-id:0. Repair
  // history replay only when one unfinished block in that same message matches.
  if (
    e.type === "message" &&
    !("block_index" in e) &&
    (c.selected?.provider || c.provider) === "claude" &&
    typeof e.item_id === "string" &&
    e.item_id.endsWith(":0")
  ) {
    const prefix = e.item_id.slice(0, -1),
      matches = [...v.items.values()].filter(
        (item) =>
          item.kind === "assistant" &&
          item.data.type === "delta" &&
          item.data.receipt === e.receipt &&
          item.data.item_id?.startsWith(prefix) &&
          item.text === e.text,
      );
    if (matches.length === 1) e = { ...e, item_id: matches[0].data.item_id };
  }
  const kind = ["delta", "message"].includes(e.type) ? "assistant" : e.type,
    key =
      (e.receipt || "session") +
      ":" +
      kind +
      ":" +
      (e.item_id || e.request_id || e.tool_id || "");
  let item = v.items.get(key);
  if (!item) {
    item = { key, kind, text: "", data: e, node: null, body: null };
    v.items.set(key, item);
    v.order.push(key);
    v.structureDirty = true;
  }
  const prevCompact =
    ["tool", "reasoning"].includes(item.kind) && !chatProcessFailure(item);
  item.data = { ...item.data, ...e };
  const nextCompact =
    ["tool", "reasoning"].includes(item.kind) && !chatProcessFailure(item);
  if (prevCompact !== nextCompact) v.structureDirty = true;
  if (kind === "approval") {
    if (!v.approvalKeys) chatHasPendingApproval(v);
    v.approvalKeys.add(key);
  }
  if (
    e.type === "approval" &&
    !["running", "starting"].includes(c.selected?.status)
  ) {
    item.data.resolved = true;
    item.data.resolution = "closed";
  }
  if (e.type === "approval" && item.data.resolved && item.node) {
    item.node.querySelector(".chat-approval-actions")?.remove();
    chatApproval(item);
  }
  if (
    e.type === "delta" ||
    e.type === "reasoning" ||
    (e.type === "tool" && e.status === "delta")
  )
    item.text += e.text || e.delta || "";
  else if (e.text !== undefined) item.text = e.text;
  else item.text = e.message || e.title || e.name || item.text;
  if (e.type === "review" && c.inspector === "changes") chatInspectorRender();
  c.dirty.add(key);
  if (!c.frame) c.frame = requestAnimationFrame(chatFlush);
}
