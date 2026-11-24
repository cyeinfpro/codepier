function chatAtBottom() {
  const el = $("#chat-scroll");
  return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 110;
}
function chatSetScrollTop(v, el, top) {
  if (!el) return;
  if (v?.v2Window) {
    v.v2Window.programmaticScroll = true;
    v.v2Window.expectedScrollTop = top;
  }
  el.scrollTop = top;
  if (v?.v2Window) {
    Promise.resolve().then(() => {
      if (v.v2Window) v.v2Window.programmaticScroll = false;
    });
  }
}
function chatBottom() {
  const el = $("#chat-scroll");
  const v = chatView();
  if (el) chatSetScrollTop(v, el, el.scrollHeight);
  if ($("#chat-latest")) $("#chat-latest").hidden = true;
}
function chatProcessFailure(item) {
  return (
    item.kind === "tool" &&
    (item.data.isError ||
      item.data.is_error ||
      ["error", "failed", "denied", "interrupted", "cancelled"].includes(
        item.data.status,
      ))
  );
}
function chatHasPendingApproval(v = chatView()) {
  // Hydrated views are indexed once. Subsequent stream frames only inspect the
  // small set of approval entries, not the entire retained conversation.
  if (!v.approvalKeys)
    v.approvalKeys = new Set(
      [...v.items.values()]
        .filter((item) => item.kind === "approval")
        .map((item) => item.key),
    );
  let pending = false;
  for (const key of v.approvalKeys) {
    const item = v.items.get(key);
    if (!item || item.data.resolved || item.answered)
      v.approvalKeys.delete(key);
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
function chatViewportActive(v = chatView()) {
  return (
    typeof CP !== "undefined" &&
    !!CP.nativeWindow &&
    typeof chatV2Enabled === "function" &&
    chatV2Enabled("native_viewport_v2") &&
    !!v?.v2State
  );
}
function chatUpdateProcessGroupMeta(v, node, items, receipt) {
  const tools = items.filter((i) => i.kind === "tool").length,
    thoughts = items.length - tools;
  const live =
    !!v.active &&
    v.active === receipt &&
    items.some(
      (i) => !["completed", "end", "succeeded", "ok"].includes(i.data.status),
    );
  const counts = [
    tools ? tools + " 项工具" : "",
    thoughts ? thoughts + " 段思考" : "",
  ]
    .filter(Boolean)
    .join(" · ");
  const last = items.at(-1),
    status = live
      ? " · " +
        (last.kind === "tool"
          ? (last.data.name || "工具") + " 执行中"
          : "思考中")
      : "";
  const meta = node.querySelector(".chat-process-meta");
  if (meta && meta.textContent !== counts + status)
    meta.textContent = counts + status;
  node.dataset.active = String(live);
}
function chatEnsureProcessGroupNode(v, key) {
  v.processGroups ??= new Map();
  let node = v.processGroups.get(key);
  if (!node) {
    node = document.createElement("details");
    node.className = "chat-process";
    node.dataset.groupKey = key;
    node.dataset.rowKey = key;
    const summary = document.createElement("summary"),
      label = document.createElement("span"),
      meta = document.createElement("span"),
      body = document.createElement("div");
    label.className = "chat-process-label";
    label.textContent = "执行过程";
    meta.className = "chat-process-meta";
    body.className = "chat-process-body";
    summary.append(label, meta);
    node.append(summary, body);
    if (typeof chatSyncGroupToolOutputs === "function") {
      node.addEventListener("toggle", () => chatSyncGroupToolOutputs(v, node));
    }
    v.processGroups.set(key, node);
  }
  return node;
}
// Group consecutive tool/thinking entries without replacing their disclosure nodes.
function chatRenderProcessGroups() {
  const v = chatView(),
    holder = $("#chat-messages");
  if (!holder) return;
  const keys = v.order.slice(-v.limit),
    renderedItems = new Map(),
    nodes = [],
    groups = [];
  v.processGroups ??= new Map();
  v.processGroupEntries = new Map();
  let group = null;
  for (const key of keys) {
    const item = v.items.get(key);
    if (!item) continue;
    if (!item.node) chatCreateItem(item);
    chatUpdateItem(item);
    renderedItems.set(key, item);
    const compact =
      ["tool", "reasoning"].includes(item.kind) && !chatProcessFailure(item);
    if (!compact) {
      group = null;
      item.groupKey = null;
      nodes.push(item.node);
      continue;
    }
    if (!group || group.receipt !== item.data.receipt) {
      const node = chatEnsureProcessGroupNode(v, key);
      group = { groupKey: key, node, receipt: item.data.receipt, items: [] };
      groups.push(group);
      v.processGroupEntries.set(key, group);
      nodes.push(node);
    }
    item.groupKey = group.groupKey;
    group.items.push(item);
  }
  for (const entry of groups) {
    chatUpdateProcessGroupMeta(v, entry.node, entry.items, entry.receipt);
    chatReconcileNodes(
      entry.node.lastElementChild,
      entry.items.map((i) => i.node),
    );
  }
  chatReconcileNodes(holder, nodes);
  const retained = new Set(groups.map((g) => g.node.dataset.groupKey));
  for (const key of v.processGroups.keys())
    if (!retained.has(key)) v.processGroups.delete(key);
  for (const [key, item] of v.renderedItems || [])
    if (renderedItems.get(key) !== item) {
      if (item.kind === "tool" && typeof chatCloseToolOutput === "function") {
        chatCloseToolOutput(item.key, v);
      }
      item.node?.remove();
      item.node = null;
      item.body = null;
    }
  v.renderedItems = renderedItems;
}
function chatBuildViewportRows(v, keys) {
  const rows = [];
  const itemToRowKey = new Map();
  v.processGroups ??= new Map();
  v.processGroupEntries = new Map();
  let group = null;
  for (const key of keys) {
    const item = v.items.get(key);
    if (!item) continue;
    const compact =
      ["tool", "reasoning"].includes(item.kind) && !chatProcessFailure(item);
    if (!compact) {
      group = null;
      item.groupKey = null;
      itemToRowKey.set(key, key);
      rows.push({
        rowKey: key,
        isGroup: false,
        itemKeys: [key],
        receipt: item.data?.receipt,
      });
      continue;
    }
    if (!group || group.receipt !== item.data.receipt) {
      group = {
        rowKey: key,
        isGroup: true,
        itemKeys: [],
        receipt: item.data.receipt,
      };
      rows.push(group);
    }
    item.groupKey = group.rowKey;
    itemToRowKey.set(key, group.rowKey);
    group.itemKeys.push(key);
  }
  return { rows, itemToRowKey };
}
function chatEnsureViewportWindow(v) {
  const el = $("#chat-scroll");
  const holder = $("#chat-messages");
  if (!el || !holder || typeof CP === "undefined" || !CP.nativeWindow)
    return null;
  el.style.overflowAnchor = "none";
  if (v.v2Window && v.v2Window.el === el && v.v2Window.holder === holder)
    return v.v2Window;
  v.v2Window?.observer?.disconnect?.();
  const topSpacer = document.createElement("div");
  topSpacer.className = "chat-spacer-top";
  topSpacer.setAttribute("aria-hidden", "true");
  const bottomSpacer = document.createElement("div");
  bottomSpacer.className = "chat-spacer-bottom";
  bottomSpacer.setAttribute("aria-hidden", "true");
  const win = {
    owner: Symbol("v2-window"),
    userScrollGeneration: 0,
    followBottom: v.follow !== false,
    programmaticScroll: false,
    expectedScrollTop: 0,
    heights: new CP.nativeWindow.HeightIndex([], { defaultHeight: 72 }),
    knownHeights: new Map(),
    pinSet: new CP.nativeWindow.PinSet(16),
    mountedKeys: new Set(),
    rows: [],
    rowByKey: new Map(),
    itemToRowKey: new Map(),
    groupKeyFor: (key) => win.itemToRowKey.get(key) || key,
    el,
    holder,
    topSpacer,
    bottomSpacer,
    overscanPx: 240,
    pendingMeasurements: new Map(),
    measureFrame: 0,
    flushRuntime: {
      frame: 0,
      generation: ChatUI.generation,
      structureDirty: false,
      dirtyKeys: new Set(),
      budget: { renderSliceMs: 8 },
      viewport: {
        isMounted: (itemKey) => win.mountedKeys.has(win.groupKeyFor(itemKey)),
      },
      reconcileVisibleStructure: () => chatRenderViewportWindow(v),
      updateItem: (item) => {
        if (!item.node) return;
        chatUpdateItem(item);
        if (item.groupKey && v.processGroups?.has(item.groupKey)) {
          const row = win.rowByKey.get(item.groupKey);
          if (row) {
            const gItems = row.itemKeys
              .map((k) => v.items.get(k))
              .filter(Boolean);
            chatUpdateProcessGroupMeta(
              v,
              v.processGroups.get(item.groupKey),
              gItems,
              row.receipt,
            );
          }
        }
      },
    },
    renderWindow: (range) => chatMountViewportRange(v, win, range),
  };
  if (typeof ResizeObserver === "function") {
    win.observer = new ResizeObserver((entries) => {
      if (win !== v.v2Window || !win.holder.isConnected) return;
      for (const entry of entries) {
        const target = entry.target;
        const rowKey =
          target.dataset.rowKey ||
          target.dataset.groupKey ||
          target.dataset.key;
        if (!rowKey || !win.mountedKeys.has(rowKey)) continue;
        const h =
          entry.borderBoxSize?.[0]?.blockSize ||
          entry.contentRect?.height ||
          target.offsetHeight ||
          0;
        if (h > 0 && Math.abs((win.knownHeights.get(rowKey) || 0) - h) > 0.5) {
          win.pendingMeasurements.set(rowKey, h);
          win.knownHeights.set(rowKey, h);
        }
      }
      if (!win.pendingMeasurements.size || win.measureFrame) return;
      win.measureFrame = requestAnimationFrame(() => {
        win.measureFrame = 0;
        if (win !== v.v2Window || !win.holder.isConnected) return;
        const anchor = CP.nativeWindow.captureAnchor(win);
        const batch = [...win.pendingMeasurements.entries()].map(
          ([key, height]) => ({
            key,
            height,
          }),
        );
        win.pendingMeasurements.clear();
        win.programmaticScroll = true;
        try {
          CP.nativeWindow.commitHeightBatch(win, batch, anchor);
        } finally {
          Promise.resolve().then(() => {
            win.programmaticScroll = false;
          });
        }
      });
    });
  }
  v.v2Window = win;
  return win;
}
function chatMountViewportRange(v, win, range) {
  const holder = win.holder;
  if (!holder) return;
  const totalRows = win.rows.length;
  const indices = new Set();
  for (let i = range.start; i < range.end && i < totalRows; i += 1)
    indices.add(i);
  const pinnedRanges = win.pinSet.discreteRanges(
    win.heights,
    range.start,
    range.end,
  );
  for (const pr of pinnedRanges) {
    for (let i = pr.start; i < pr.end && i < totalRows; i += 1) indices.add(i);
  }
  const sortedIndices = [...indices].sort((a, b) => a - b);
  const nextMounted = new Set();
  const nextRenderedItems = new Map();
  const nodes = [win.topSpacer];
  win.topSpacer.style.height = Math.max(0, range.top) + "px";
  win.bottomSpacer.style.height = Math.max(0, range.bottom) + "px";
  for (const idx of sortedIndices) {
    const row = win.rows[idx];
    if (!row) continue;
    nextMounted.add(row.rowKey);
    if (row.isGroup) {
      const gNode = chatEnsureProcessGroupNode(v, row.rowKey);
      const gItems = [];
      for (const itemKey of row.itemKeys) {
        const item = v.items.get(itemKey);
        if (!item) continue;
        if (!item.node) chatCreateItem(item);
        chatUpdateItem(item);
        nextRenderedItems.set(itemKey, item);
        gItems.push(item);
      }
      chatUpdateProcessGroupMeta(v, gNode, gItems, row.receipt);
      chatReconcileNodes(
        gNode.lastElementChild,
        gItems.map((i) => i.node),
      );
      nodes.push(gNode);
      win.observer?.observe(gNode);
    } else {
      const item = v.items.get(row.itemKeys[0]);
      if (!item) continue;
      if (!item.node) chatCreateItem(item);
      item.node.dataset.rowKey = row.rowKey;
      chatUpdateItem(item);
      nextRenderedItems.set(item.key, item);
      nodes.push(item.node);
      win.observer?.observe(item.node);
    }
  }
  nodes.push(win.bottomSpacer);
  chatReconcileNodes(holder, nodes);
  for (const prevKey of win.mountedKeys) {
    if (!nextMounted.has(prevKey)) {
      const oldRow = win.rowByKey.get(prevKey);
      if (oldRow?.isGroup) {
        const gNode = v.processGroups?.get(prevKey);
        if (gNode) win.observer?.unobserve(gNode);
      }
    }
  }
  for (const [key, item] of v.renderedItems || []) {
    if (nextRenderedItems.get(key) !== item) {
      if (item.kind === "tool" && typeof chatCloseToolOutput === "function") {
        chatCloseToolOutput(item.key, v);
      }
      win.observer?.unobserve(item.node);
      item.node?.remove();
      item.node = null;
      item.body = null;
    }
  }
  win.mountedKeys = nextMounted;
  v.renderedItems = nextRenderedItems;
}
function chatRenderViewportWindow(v = chatView()) {
  const win = chatEnsureViewportWindow(v);
  if (!win) {
    chatRenderProcessGroups();
    return;
  }
  const { rows, itemToRowKey } = chatBuildViewportRows(v, v.order);
  win.rows = rows;
  win.itemToRowKey = itemToRowKey;
  win.rowByKey = new Map(rows.map((r) => [r.rowKey, r]));
  for (const key of v.approvalKeys || []) {
    const item = v.items.get(key);
    const rk = itemToRowKey.get(key);
    if (item && !item.data.resolved && !item.answered && rk)
      win.pinSet.pin(rk, "approval");
    else if (rk) win.pinSet.unpin(rk, "approval");
  }
  win.heights.rebuild(
    rows.map((r) => r.rowKey),
    win.knownHeights,
    72,
  );
  const total = win.heights.offset(win.heights.count);
  const clientH = win.el.clientHeight || 600;
  const targetTop = win.followBottom
    ? Math.max(0, total - clientH)
    : win.el.scrollTop;
  const range = CP.nativeWindow.visibleRange(
    win.heights,
    targetTop,
    clientH,
    win.overscanPx,
  );
  chatMountViewportRange(v, win, range);
}
function chatFlush() {
  const c = ChatUI;
  c.frame = 0;
  if (!$("#chat-messages")) return;
  const v = chatView(),
    bottom = chatAtBottom();
  const dirtyKeys = new Set([...c.dirty, ...(v.v2DirtyKeys || [])]);
  let structureDirty = Boolean(
    v.structureDirty || v.v2StructureDirty || !v.renderedItems?.size,
  );
  if (chatViewportActive(v)) {
    const win = chatEnsureViewportWindow(v);
    if (win) {
      win.followBottom = bottom;
      win.flushRuntime.generation = c.generation;
      win.flushRuntime.structureDirty = structureDirty;
      win.flushRuntime.dirtyKeys = new Set(dirtyKeys);
      CP.nativeWindow.flushVisibleDirty(v, win.flushRuntime);
      if (win.flushRuntime.dirtyKeys.size) {
        for (const k of win.flushRuntime.dirtyKeys) c.dirty.add(k);
      } else {
        c.dirty.clear();
      }
      v.v2DirtyKeys?.clear();
      v.structureDirty = false;
      v.v2StructureDirty = false;
      v.v2State?.dirtyKeys?.clear();
      v.v2State?.changeReasons?.clear();
      if (v.v2State) v.v2State.structureDirty = false;
    }
  } else {
    if (!structureDirty && dirtyKeys.size > 0 && v.renderedItems?.size) {
      const updatedGroups = new Map();
      for (const key of dirtyKeys) {
        const item = v.items.get(key);
        if (!item) {
          structureDirty = true;
          break;
        }
        if (!item.node || v.renderedItems.get(key) !== item) {
          const windowKeys = v.order.slice(-v.limit);
          if (windowKeys.includes(key)) {
            structureDirty = true;
            break;
          }
          continue;
        }
        const compact =
          ["tool", "reasoning"].includes(item.kind) &&
          !chatProcessFailure(item);
        const insideGroup = Boolean(item.groupKey);
        if (compact !== insideGroup) {
          structureDirty = true;
          break;
        }
        chatUpdateItem(item);
        if (item.groupKey && v.processGroupEntries?.has(item.groupKey)) {
          updatedGroups.set(
            item.groupKey,
            v.processGroupEntries.get(item.groupKey),
          );
        }
      }
      for (const entry of updatedGroups.values()) {
        chatUpdateProcessGroupMeta(v, entry.node, entry.items, entry.receipt);
      }
    } else {
      structureDirty = true;
    }
    if (structureDirty) chatRenderProcessGroups();
    c.dirty.clear();
    v.v2DirtyKeys?.clear();
    v.structureDirty = false;
    v.v2StructureDirty = false;
    v.v2State?.dirtyKeys?.clear();
    v.v2State?.changeReasons?.clear();
    if (v.v2State) v.v2State.structureDirty = false;
  }
  $("#chat-empty").hidden = !!v.order.length;
  $("#chat-older").hidden = v.v2State
    ? !v.v2State.projection?.history?.hasMore
    : v.order.length <= v.limit;
  if (bottom) chatBottom();
  else $("#chat-latest").hidden = false;
  chatSyncChrome();
}
function chatRenderHistory(scroll = true) {
  const v = chatView();
  if (chatViewportActive(v)) {
    chatRenderViewportWindow(v);
  } else {
    chatRenderProcessGroups();
  }
  ChatUI.dirty.clear();
  v.v2DirtyKeys?.clear();
  v.structureDirty = false;
  v.v2StructureDirty = false;
  v.v2State?.dirtyKeys?.clear();
  v.v2State?.changeReasons?.clear();
  if (v.v2State) v.v2State.structureDirty = false;
  $("#chat-empty").hidden = !!v.order.length;
  $("#chat-older").hidden = v.v2State
    ? !v.v2State.projection?.history?.hasMore
    : v.order.length <= v.limit;
  if (scroll) {
    if (!v.order.length) chatSetScrollTop(v, $("#chat-scroll"), 0);
    else if (v.follow !== false) chatBottom();
    else chatSetScrollTop(v, $("#chat-scroll"), v.scrollTop || 0);
  }
  $("#chat-latest").hidden = !v.order.length || chatAtBottom();
}
function chatCreateItem(item) {
  if (item.kind === "review") {
    chatCreateReview(item);
    return;
  }
  const article = document.createElement("article");
  article.className = "chat-message chat-message-" + item.kind;
  article.dataset.key = item.key;
  const header = document.createElement("div"),
    label = document.createElement("span"),
    actions = document.createElement("div");
  header.className = "chat-message-head";
  label.className = "chat-message-label";
  label.textContent =
    {
      user: item.data.parent_receipt ? "你 · 补充指令" : "你",
      assistant: chatProviderName(),
      reasoning: "思考过程",
      tool: "工具执行",
      approval: "需要确认",
      error: "未完成",
    }[item.kind] || item.kind;
  actions.className = "chat-message-actions";
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.title = "复制消息";
  copyButton.setAttribute("aria-label", "复制消息");
  copyButton.innerHTML = chatIcon("copy");
  copyButton.onclick = () => chatCopyText(item.text);
  actions.append(copyButton);
  if (item.kind === "user") {
    const edit = document.createElement("button");
    edit.textContent = "编辑为新消息";
    edit.onclick = async () => {
      if (await chatReplaceDraft(item.text))
        chatStatus("已复制到输入框；发送后是新消息，不会撤销先前的工具操作。");
    };
    actions.append(edit);
  }
  header.append(label, actions);
  article.append(header);
  let body;
  if (["reasoning", "tool"].includes(item.kind)) {
    const details = document.createElement("details"),
      summary = document.createElement("summary");
    body = document.createElement("pre");
    details.append(summary, body);
    if (item.kind === "tool" && typeof chatOnToolDetailsToggle === "function") {
      details.addEventListener("toggle", () =>
        chatOnToolDetailsToggle(item, details),
      );
    }
    article.append(details);
  } else {
    body = document.createElement("div");
    body.className = "chat-message-body";
    article.append(body);
  }
  item.node = article;
  item.body = body;
  item.renderedSignature = null;
  if (item.kind === "user" && item.data.attachments?.length) {
    const files = document.createElement("div");
    files.className = "chat-message-files";
    for (const f of item.data.attachments) {
      const badge = document.createElement("span");
      badge.textContent = f.name || "附件";
      badge.title = f.mime || "";
      files.append(badge);
    }
    article.append(files);
  }
  if (item.kind === "approval") {
    const fields = item.data.details || {},
      context =
        fields.message ||
        fields.command ||
        fields.commandActions ||
        fields.changes ||
        fields.fileChanges;
    if (context) {
      const details = document.createElement("details"),
        summary = document.createElement("summary"),
        pre = document.createElement("pre");
      summary.textContent = "操作内容";
      pre.textContent =
        (typeof context === "string"
          ? context
          : JSON.stringify(context, null, 2)) +
        (fields.cwd ? "\n工作目录：" + fields.cwd : "");
      details.open = true;
      details.append(summary, pre);
      article.append(details);
    }
    chatApproval(item);
  }
}
function chatUpdateItem(item) {
  if (item.kind === "review") {
    chatUpdateReview(item);
    if (typeof chatApplyReviewEvidenceState === "function")
      chatApplyReviewEvidenceState(item);
    return;
  }
  const signature =
    item.text +
    "\0" +
    (item.data.status || "") +
    "\0" +
    (item.data.name || "") +
    "\0" +
    !!(item.data.isError || item.data.is_error) +
    "\0" +
    !!item.data.partial +
    "\0" +
    (item.data.body_loaded_bytes || 0);
  if (item.renderedSignature === signature && item.body?.hasChildNodes())
    return;
  item.renderedSignature = signature;
  if (item.kind === "assistant") {
    if (typeof chatRichMarkdown === "function")
      chatRichMarkdown(item.body, item.text);
    else item.body.textContent = item.text;
  } else if (item.kind === "tool") {
    const summary = item.node.querySelector("summary");
    let parsed;
    try {
      parsed = JSON.parse(item.text);
    } catch {}
    const failed = chatProcessFailure(item);
    if (failed && !item.node.classList.contains("chat-tool-failed"))
      item.node.querySelector("details").open = true;
    item.node.classList.toggle("chat-tool-failed", !!failed);
    summary.textContent =
      (item.data.name || "工具") +
      " · " +
      (failed ? "执行异常" : chatStateName(item.data.status));
    item.body.textContent = parsed
      ? JSON.stringify(parsed, null, 2)
      : item.text;
  } else {
    item.body.textContent = item.text;
    if (item.kind === "reasoning")
      item.node.querySelector("summary").textContent =
        "查看思考过程 · " + item.text.length + " 字符";
  }
  if (typeof chatSyncPartialBodyNotice === "function")
    chatSyncPartialBodyNotice(item);
}
async function chatCopyText(text) {
  if (typeof copy === "function") {
    await copy(text);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    chatStatus("已复制");
  } catch {
    chatStatus("浏览器未允许访问剪贴板，可选中文字复制");
  }
}
