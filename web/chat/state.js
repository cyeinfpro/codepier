"use strict";
// One view per project/native conversation. Model selection never sends a prompt.
const CHAT_MESSAGE_MAX_BYTES = 64 * 1024;
const CHAT_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024;
const CHAT_ATTACHMENT_MAX_COUNT = 10;
const CHAT_UPLOAD_CHUNK_BYTES = 64 * 1024;
function chatProviderName(
  provider = ChatUI.selected?.provider || ChatUI.provider,
) {
  return { pi: "Pi", codex: "Codex", claude: "Claude" }[provider] || "CLI";
}
const ChatUI = {
  generation: 0,
  project: "",
  provider: "pi",
  cwd: ".",
  selected: null,
  views: new Map(),
  source: null,
  listeners: null,
  frame: 0,
  dirty: new Set(),
  cursor: 0,
  listTicket: 0,
  query: "",
  historyOffset: 0,
  historyProject: "",
  historyLoading: false,
  historyLoaded: false,
  recentProjects: [],
  filter: "",
  inspector: "",
  catalogTicket: 0,
  catalogCache: new Map(),
  rows: [],
  connection: "idle",
  focus: false,
  v2Cache:
    typeof CP !== "undefined" && CP.nativeCache?.NativeSnapshotCache
      ? new CP.nativeCache.NativeSnapshotCache()
      : null,
  v2Capabilities: null,
  v2CapabilitiesPromise: null,
  v2ScopeKeys: new Map(),
  v2ProjectMappings: new Map(),
  v2ActiveScope: null,
};
function chatV2Cache() {
  if (
    !ChatUI.v2Cache &&
    typeof CP !== "undefined" &&
    CP.nativeCache?.NativeSnapshotCache
  ) {
    ChatUI.v2Cache = new CP.nativeCache.NativeSnapshotCache();
  }
  return ChatUI.v2Cache;
}
function chatV2Enabled(flag = "native_snapshot_v2_read") {
  return (
    typeof CP !== "undefined" &&
    !!CP.nativeState &&
    !!CP.nativeRuntime &&
    ChatUI.v2Capabilities?.native_view_v2 === true &&
    ChatUI.v2Capabilities?.flags?.[flag] === true
  );
}
function chatV2MappingSignature(projectId = ChatUI.project) {
  const project = (
    typeof S !== "undefined" && S.projects ? S.projects : []
  ).find((p) => p.id === projectId);
  const space = typeof S !== "undefined" ? S.space_id || "" : "";
  const user =
    typeof S !== "undefined"
      ? S.session?.user_id || S.session?.username || ""
      : "";
  return {
    projectId: projectId || "",
    root: project?.root || "",
    deviceId: project?.device_id || "",
    key: `${space}::${user}::${projectId || ""}::${project?.device_id || ""}::${project?.root || ""}`,
  };
}
function chatInvalidateProjectV2(projectId) {
  if (!projectId) return;
  const cache = chatV2Cache();
  const prefix = `::${projectId}::`;
  for (const [mKey, scopeKey] of [...ChatUI.v2ScopeKeys.entries()]) {
    if (mKey.includes(prefix)) {
      cache?.invalidateScope(scopeKey);
      ChatUI.v2ScopeKeys.delete(mKey);
    }
  }
  for (const [viewKey, view] of ChatUI.views.entries()) {
    if (!viewKey.startsWith(projectId + "\n")) continue;
    view.v2Scope?.stopNative?.();
    view.v2OutputSubscriptions?.closeAll?.();
    view.v2Window?.observer?.disconnect?.();
    view.v2State = null;
    view.v2Scope = null;
    view.v2Window = null;
    view.v2Halted = null;
    view.v2ProjectionState = "";
    view.v2DirtyKeys?.clear();
    view.v2StructureDirty = false;
  }
}
function chatCheckProjectMappings() {
  if (typeof S === "undefined" || !Array.isArray(S.projects)) return;
  for (const p of S.projects) {
    const prev = ChatUI.v2ProjectMappings.get(p.id);
    const next = { root: p.root || "", device_id: p.device_id || "" };
    if (
      prev &&
      (prev.root !== next.root || prev.device_id !== next.device_id)
    ) {
      chatInvalidateProjectV2(p.id);
    }
    ChatUI.v2ProjectMappings.set(p.id, next);
  }
}
function chatClearV2Scopes(invalidateCache = false) {
  const activeScope = ChatUI.v2ActiveScope;
  ChatUI.v2ActiveScope = null;
  activeScope?.stopNative?.();
  for (const v of ChatUI.views.values()) {
    v.v2Scope?.stopNative?.();
    v.v2OutputSubscriptions?.closeAll?.();
    v.v2Window?.observer?.disconnect?.();
    if (invalidateCache) {
      v.v2State = null;
      v.v2Scope = null;
      v.v2Window = null;
      v.v2Halted = null;
    }
  }
  if (invalidateCache) {
    chatV2Cache()?.invalidateAll();
    ChatUI.v2ScopeKeys.clear();
    ChatUI.v2ProjectMappings.clear();
    ChatUI.v2Capabilities = null;
    ChatUI.v2CapabilitiesPromise = null;
  }
}
const chatLabels = {
  off: "关闭",
  none: "不启用",
  minimal: "最少",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "极高",
  max: "最高",
};
function chatIcon(name) {
  const paths = {
    model: "M12 3 3 8l9 5 9-5-9-5ZM3 12l9 5 9-5M3 16l9 5 9-5",
    brain:
      "M9 18H7a4 4 0 0 1-3-6 4 4 0 0 1 3-6 3 3 0 0 1 5-2 3 3 0 0 1 5 2 4 4 0 0 1 3 6 4 4 0 0 1-3 6h-2M12 4v16M8 8l2 2M16 8l-2 2M7 14l3-1M17 14l-3-1",
    plus: "M12 5v14M5 12h14",
    send: "M12 19V5m-6 6 6-6 6 6",
    menu: "M4 6h16M4 12h16M4 18h16",
    search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 6 6",
    folder: "M3 6h6l2 2h10v12H3z",
    close: "m6 6 12 12M6 18 18 6",
    chevron: "m7 10 5 5 5-5",
    settings: "M4 7h16M4 17h16M9 4v6M15 14v6",
    file: "M5 3h9l5 5v13H5zM14 3v6h5",
    command:
      "M8 5a3 3 0 1 0-3 3h14a3 3 0 1 0-3-3v14a3 3 0 1 0 3-3H5a3 3 0 1 0 3 3V5",
    terminal: "m4 6 5 5-5 5m8 1h8",
    refresh:
      "M20 7v5h-5M4 17v-5h5M19 9a7 7 0 0 0-12-4L4 8m1 7a7 7 0 0 0 12 4l3-3",
    expand: "M8 3H3v5M16 3h5v5M21 16v5h-5M3 16v5h5",
    queue: "M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1",
    stop: "M6 6h12v12H6z",
    check: "m5 12 4 4L19 6",
    copy: "M8 8h12v13H8zM4 16H2V2h13v3",
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${(
    paths[name] || paths.command
  )
    .split("|||")
    .map((d) => `<path d="${d}"></path>`)
    .join("")}</svg>`;
}
function chatKey(project = ChatUI.project, id = ChatUI.selected?.id) {
  return project + "\n" + (id || "new:" + ChatUI.provider + ":" + ChatUI.cwd);
}
function chatView() {
  const key = chatKey();
  if (!ChatUI.views.has(key))
    ChatUI.views.set(key, {
      draft: "",
      files: [],
      pending: null,
      busy: false,
      items: new Map(),
      order: [],
      limit: 100,
      settings: null,
      catalog: null,
      catalogError: "",
      catalogLoading: false,
      selectedModel: null,
      selectedEffort: null,
      active: null,
      cursor: 0,
      outbox: new Map(),
      pendingSettings: null,
      settingOp: null,
      stats: null,
      commands: null,
      mode: "followup",
      selectionMode: "inherit_at_dispatch",
      search: "",
      match: 0,
      queue: [],
      sessionStatus: ChatUI.selected?.status || "new",
      v2State: null,
      v2Scope: null,
      v2DirtyKeys: new Set(),
      v2StructureDirty: false,
      v2Window: null,
      v2OutputSubscriptions: null,
      v2ProjectionState: "",
      v2Halted: null,
      v2HistoryBusy: false,
      v2BodyRequests: new Map(),
      v2SettingsGeneration: 0,
      v2Control: null,
      v2Sync: null,
      v2ActionAvailability: null,
      structureDirty: false,
    });
  return ChatUI.views.get(key);
}
function chatAPI(action, args = {}, project = ChatUI.project) {
  return api("/api/native/" + action, {
    method: "POST",
    body: JSON.stringify({ project, args }),
    retrySafe: true,
  });
}
function chatCurrent(gen, view) {
  return (
    !!S.session &&
    S.page === "native" &&
    gen === ChatUI.generation &&
    (!view || view === chatView())
  );
}
function chatViewVisible(view, identity = S.session) {
  return (
    !!identity &&
    S.session === identity &&
    S.page === "native" &&
    !!$("#chat-root") &&
    ChatUI.views.get(chatKey()) === view
  );
}
function chatStateName(s) {
  return (
    {
      sending: "发送中",
      queued: "等待执行",
      claimed: "进行中",
      starting: "连接中",
      running: "已连接",
      stopping: "正在停止",
      exited: "已停止",
      interrupted: "已中断",
      completed: "已完成",
      start: "开始",
      started: "执行中",
      delta: "执行中",
      end: "完成",
      error: "失败",
      cancelled: "已取消",
      cleared: "已清理",
      orphaned: "需检查电脑",
      uncertain: "待核实",
    }[s] ||
    s ||
    ""
  );
}
function chatStatus(text, error = false) {
  const el = $("#chat-status");
  if (el) {
    el.textContent = text;
    el.classList.toggle("is-error", error);
    el.dataset.quiet = String(
      !error &&
        (!text ||
          text === "Enter 发送 · Shift+Enter 换行" ||
          text === "已连接 · Enter 发送"),
    );
  }
}
function chatDetach(clear = false) {
  const c = ChatUI;
  chatDisconnect();
  try {
    chatRememberView();
  } finally {
    // A pending render can fail while leaving the page. Release transports and
    // listeners even then, before allowing that error to reach the caller.
    c.generation++;
    c.catalogTicket++;
    c.listeners?.abort();
    c.listeners = null;
    cancelAnimationFrame(c.frame);
    c.frame = 0;
    c.dirty.clear();
    chatClearV2Scopes(clear);
    document.body.classList.remove("chat-focus");
    if (clear) {
      chatCatalogAbortAll();
      Object.assign(c, {
        historyProject: "",
        query: "",
        filter: "",
        historyOffset: 0,
        historyLoaded: false,
        targetsLoading: false,
        rows: [],
        recentProjects: [],
      });
      for (const v of c.views.values())
        for (const f of v.files) if (f.preview) URL.revokeObjectURL(f.preview);
      c.views.clear();
      c.catalogCache.clear();
      c.selected = null;
      c.project = "";
    }
    chatUnmountChrome();
  }
}
async function chatOpenProject(project, provider = "pi") {
  if (S.page === "native" && $("#chat-root"))
    return chatSwitch(null, project, { provider, cwd: "." });
  chatDetach();
  Object.assign(ChatUI, {
    project,
    provider,
    cwd: ".",
    selected: null,
    inspector: "",
  });
  await navigate("native");
}
async function chatSwitch(
  row,
  project = row?.project_id || ChatUI.project,
  options = {},
) {
  const c = ChatUI;
  chatDisconnect();
  chatRememberView();
  chatCheckProjectMappings();
  c.dialogCancel?.();
  chatClosePopover();
  chatOptions(false);
  c.generation++;
  c.catalogTicket++;
  c.listTicket++;
  cancelAnimationFrame(c.frame);
  c.frame = 0;
  c.dirty.clear();
  c.cursor = 0;
  c.selected = row;
  c.project = project;
  if (row) {
    c.provider = row.provider;
    const root =
        row.root || S.projects.find((p) => p.id === project)?.root || "",
      cwd = row.cwd || root;
    const normalize = (p) => p.replaceAll("\\", "/").replace(/\/$/, "");
    const r = normalize(root),
      d = normalize(cwd);
    c.cwd =
      d === r ? "." : r && d.startsWith(r + "/") ? d.slice(r.length + 1) : ".";
  } else {
    if (options.provider) c.provider = options.provider;
    if (options.cwd !== undefined) c.cwd = options.cwd;
  }
  if (!$("#chat-root") || !c.listeners) {
    $("#page").replaceChildren();
    await chatPage();
    return;
  }
  $("#chat-slash").hidden = true;
  $("#chat-find-bar").hidden = true;
  $(".chat-overflow").open = false;
  chatDrawer(false, true);
  chatConnection("idle", "就绪");
  chatStatus("Enter 发送 · Shift+Enter 换行");
  chatRestore();
  chatSyncChrome();
  chatList();
  chatLoadCatalog();
  if (row) {
    chatConnect();
    const view = chatView();
    if (
      view.settingOp &&
      ["sending", "queued", "claimed", "uncertain"].includes(
        view.settingOp.state,
      )
    )
      chatWatchReceipt(
        row.id,
        view.settingOp.receipt,
        c.generation,
        view,
        view.settingOp,
      );
  }
  if (c.inspector) chatInspector(c.inspector);
  const input = $("#chat-compose"),
    v = chatView();
  input.focus({ preventScroll: true });
  if (v.selection) input.setSelectionRange(...v.selection);
}
