function chatModelKey(model) {
  if (typeof model === "string") return model;
  return model?.provider
    ? model.provider + "/" + model.id
    : model?.model || model?.id || "";
}
function chatModelCurrent(v = chatView()) {
  return (
    (!["error", "uncertain"].includes(v.settingOp?.state)
      ? v.settingOp?.patch?.model
      : "") ||
    v.pendingSettings?.model ||
    v.selectedModel ||
    chatModelKey(
      v.configForResume
        ? v.catalog?.model
        : v.settings?.model || v.catalog?.model,
    )
  );
}
function chatModels(v = chatView()) {
  const rows = v.catalog?.models || v.settings?.models || [];
  const seen = new Set();
  return rows
    .filter((m) => m && typeof m === "object")
    .filter((m) => {
      const key = chatModelKey(m);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}
function chatLevels(v = chatView()) {
  const current = chatModelCurrent(v),
    model = chatModels(v).find(
      (m) =>
        chatModelKey(m) === current || m.id === current || m.model === current,
    );
  const source =
    ChatUI.selected && !v.configForResume
      ? v.settings || v.catalog
      : v.catalog || v.settings;
  const confirmed =
    ChatUI.selected &&
    !v.configForResume &&
    chatModelKey(v.settings?.model) === current
      ? v.settings?.thinking_levels
      : null;
  return (
    confirmed ||
    model?.thinkingLevels ||
    model?.supportedReasoningEfforts ||
    (chatModelKey(source?.model) === current ? source?.thinking_levels : []) ||
    []
  )
    .map((x) => (typeof x === "string" ? x : x.reasoningEffort || x.level))
    .filter(Boolean);
}
function chatSettings(settings) {
  const v = chatView();
  if (settings) v.settings = settings;
  if (v.v2State && v.settingOp) {
    const overlay = v.v2State.pendingCommands?.get(v.settingOp.receipt);
    if (overlay) {
      if (overlay.actual && typeof overlay.actual === "object") {
        v.settings = { ...(v.settings || {}), ...overlay.actual };
      }
      if (
        overlay.status === "applied" &&
        !overlay.partial &&
        overlay.outcome !== "partial"
      ) {
        v.settingOp = null;
      } else if (overlay.outcome === "partial" || overlay.partial) {
        v.settingOp.outcome = "partial";
        v.settingOp.state = "error";
      } else if (overlay.status === "failed") {
        v.settingOp.state = "error";
        v.settingOp.outcome = overlay.outcome || "rejected";
      } else if (overlay.status === "uncertain") {
        v.settingOp.state = "uncertain";
        v.settingOp.outcome = "unknown";
      }
    }
  }
  const current = chatModelCurrent(v),
    model = chatModels(v).find(
      (m) =>
        chatModelKey(m) === current || m.model === current || m.id === current,
    );
  const name = $("#chat-model-name");
  if (!name) return;
  const label =
    model?.name ||
    model?.displayName ||
    current ||
    (v.catalog ? "本机默认" : "选择模型");
  // WebKit can lose the pending click when discovery replaces the text node
  // underneath a pressed picker. Keep that node while updating its label.
  if (name.firstChild?.nodeType === Node.TEXT_NODE)
    name.firstChild.nodeValue = label;
  else name.textContent = label;
  $("#chat-model-picker").title =
    (model?.provider ? model.provider + " / " : "") +
    (model?.id || current || "发送前可选择模型");
  const source = v.configForResume ? v.catalog : v.settings || v.catalog;
  const select = $("#chat-effort-select"),
    levels = chatLevels(v),
    chosen =
      (!["error", "uncertain"].includes(v.settingOp?.state)
        ? v.settingOp?.patch?.effort
        : null) ??
      v.pendingSettings?.effort ??
      v.selectedEffort ??
      source?.effort ??
      source?.thinkingLevel ??
      source?.reasoningEffort ??
      "";
  select.replaceChildren();
  const opt = document.createElement("option");
  opt.value = "";
  opt.textContent =
    v.catalogLoading && !levels.length
      ? "正在加载…"
      : !levels.length
        ? "本机默认"
        : "默认";
  select.append(opt);
  for (const level of [...new Set(levels)]) {
    const o = document.createElement("option");
    o.value = level;
    o.textContent = (chatLabels[level] || level) + " · " + level;
    select.append(o);
  }
  if (chosen && !levels.includes(chosen)) {
    const o = document.createElement("option");
    o.value = chosen;
    o.textContent = (chatLabels[chosen] || chosen) + " · 当前设置";
    select.append(o);
  }
  select.value = chosen;
  select.disabled =
    (v.catalogLoading && !levels.length) ||
    (!!v.settingOp && !["error", "uncertain"].includes(v.settingOp.state));
  select.title = levels.length
    ? "当前模型支持的思考强度"
    : "原生 CLI 尚未返回可选强度；可刷新模型目录";
  const state = $("#chat-setting-state");
  state.textContent = v.settingOp
    ? v.settingOp.state === "error"
      ? v.settingOp.outcome === "applied"
        ? "实际已生效 · 原操作失败"
        : v.settingOp.outcome === "partial"
          ? "部分生效"
          : v.settingOp.outcome === "rejected"
            ? "未变更"
            : "结果待核实"
      : v.settingOp.state === "uncertain"
        ? "设置待核实"
        : "正在切换…"
    : v.pendingSettings
      ? "下轮生效"
      : current
        ? v.configForResume
          ? "恢复时生效"
          : ChatUI.selected
            ? "原生配置"
            : "首条消息生效"
        : "";
  $("#chat-settings-retry").hidden =
    !v.settingOp ||
    v.settingOp.outcome === "applied" ||
    !["error", "uncertain"].includes(v.settingOp.state);
  state.parentElement.classList.toggle(
    "has-update",
    !!v.settingOp || !!v.pendingSettings || !!v.configForResume,
  );
  const caps = v.settings?.capabilities || v.catalog?.capabilities;
  if (
    caps?.effort_runtime === false &&
    ["starting", "running"].includes(ChatUI.selected?.status)
  ) {
    select.disabled = true;
    select.title = "Claude 思考强度在启动时生效；停止后可选择强度并恢复会话";
  }
  const steer = $('#chat-send-mode option[value="steer"]');
  if (steer) {
    steer.disabled = caps?.steer === false;
    if (steer.disabled && v.mode === "steer") {
      v.mode = "followup";
      $("#chat-send-mode").value = "followup";
    }
  }
  if (typeof chatSyncAvailabilityHints === "function")
    chatSyncAvailabilityHints();
  if (ChatUI.inspector === "overview") chatInspectorRender();
  chatSyncChrome();
}
function chatClosePopover(restore = false) {
  const box = $("#chat-popover"),
    open = box && !box.hidden;
  box?.setAttribute("hidden", "");
  $("#chat-model-picker")?.setAttribute("aria-expanded", "false");
  if (open && restore)
    $("#" + (ChatUI.popoverAnchor || "chat-model-picker"))?.focus({
      preventScroll: true,
    });
}
function chatModelPopover() {
  if (innerWidth <= 1100 && ChatUI.inspector) chatInspector("");
  // /model can open the picker without clicking the settings capsule. Its
  // anchor must be visible and focusable for positioning and Escape restore.
  if (!$("#chat-options").classList.contains("is-open")) chatOptions(true);
  ChatUI.popoverAnchor = "chat-model-picker";
  const box = $("#chat-popover");
  box.setAttribute("aria-label", "模型选择");
  if (!box.hidden && $("#chat-model-search")) {
    chatClosePopover();
    return;
  }
  box.hidden = false;
  box.innerHTML = `<div class="chat-popover-head"><strong>选择模型</strong><button type="button" id="chat-model-refresh" title="重新读取本机模型" aria-label="刷新模型目录">${chatIcon("refresh")}</button><button type="button" id="chat-popover-close" aria-label="关闭模型选择">${chatIcon("close")}</button></div><input id="chat-model-search" type="search" enterkeyhint="search" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="搜索名称、模型 ID 或服务商" aria-label="搜索模型" role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls="chat-model-results"><div id="chat-model-results" role="listbox" aria-label="模型目录"></div><div class="chat-popover-foot">↑ ↓ 选择 · Enter 确认 · Esc 关闭</div>`;
  $("#chat-model-picker").setAttribute("aria-expanded", "true");
  $("#chat-popover-close").onclick = () => chatClosePopover(true);
  $("#chat-model-refresh").onclick = () => chatLoadCatalog(true);
  $("#chat-model-search").oninput = (e) => chatRenderModels(e.target.value);
  chatRenderModels("");
  chatPositionLayers();
  $("#chat-model-search").focus();
  if (!chatView().catalog && !chatView().catalogLoading) chatLoadCatalog();
}
function chatRenderModels(query) {
  const holder = $("#chat-model-results");
  if (!holder) return;
  const focused = holder.contains(document.activeElement)
      ? document.activeElement.closest(".chat-model-option")
      : null,
    focusModel = focused ? focused.dataset.model || "" : null;
  const keyboard =
      holder.dataset.query === query
        ? holder.querySelector('[data-keyboard="true"]')
        : null,
    keyboardModel = keyboard ? keyboard.dataset.model || "" : null;
  holder.dataset.query = query;
  holder.replaceChildren();
  $("#chat-model-search")?.removeAttribute("aria-activedescendant");
  const v = chatView(),
    q = query.trim().toLowerCase(),
    models = chatModels(v).filter((m) =>
      [m.name, m.displayName, m.provider, m.id, m.model]
        .join(" ")
        .toLowerCase()
        .includes(q),
    );
  if (!models.length) {
    const empty = document.createElement("p");
    empty.className = "chat-picker-empty";
    empty.textContent = v.catalogLoading
      ? "正在读取本机模型…"
      : v.catalogError ||
        (q
          ? "没有匹配的模型"
          : "本机没有返回可用模型。可刷新目录，或检查电脑上的 CLI 登录和配置。");
    holder.append(empty);
    if (focusModel !== null)
      $("#chat-model-search").focus({ preventScroll: true });
    return;
  }
  if (!q) {
    const defaults = document.createElement("button");
    defaults.className = "chat-model-option";
    defaults.type = "button";
    defaults.setAttribute("role", "option");
    defaults.setAttribute("aria-selected", "false");
    defaults.textContent = "恢复本机默认模型";
    defaults.onclick = () => chatChooseModel("");
    holder.append(defaults);
  }
  const groups = new Map();
  for (const m of models) {
    const key = m.provider || chatProviderName();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(m);
  }
  for (const [provider, rows] of groups) {
    const group = document.createElement("div");
    group.className = "chat-model-group";
    group.textContent = provider;
    holder.append(group);
    for (const m of rows) {
      const value = chatModelKey(m),
        button = document.createElement("button");
      button.type = "button";
      button.className =
        "chat-model-option" +
        (value === chatModelCurrent(v) ? " selected" : "");
      button.setAttribute("role", "option");
      button.setAttribute(
        "aria-selected",
        String(value === chatModelCurrent(v)),
      );
      button.dataset.model = value;
      const main = document.createElement("span"),
        title = document.createElement("strong"),
        id = document.createElement("small"),
        badges = document.createElement("span");
      title.textContent = m.name || m.displayName || m.id || m.model;
      id.textContent = m.id || m.model;
      main.append(title, id);
      badges.className = "chat-model-badges";
      badges.textContent = [
        m.input?.includes("image") ? "识图" : "",
        m.reasoning || m.supportedReasoningEfforts?.length ? "思考" : "",
        m.contextWindow ? Math.round(m.contextWindow / 1000) + "k" : "",
      ]
        .filter(Boolean)
        .join(" · ");
      button.append(main, badges);
      button.onclick = () => chatChooseModel(value);
      holder.append(button);
    }
  }
  const options = [...holder.querySelectorAll(".chat-model-option")];
  if (focusModel !== null)
    (
      options.find((b) => (b.dataset.model || "") === focusModel) ||
      $("#chat-model-search")
    ).focus({
      preventScroll: true,
    });
  if (keyboardModel !== null) {
    const active = options.find(
      (b) => (b.dataset.model || "") === keyboardModel,
    );
    if (active) {
      options.forEach((b, i) => {
        b.id = "chat-model-option-" + i;
        b.dataset.keyboard = String(b === active);
      });
      $("#chat-model-search").setAttribute("aria-activedescendant", active.id);
    }
  }
}
async function chatChooseModel(value) {
  chatClosePopover(true);
  const v = chatView();
  if (
    ChatUI.selected &&
    ["running", "starting"].includes(ChatUI.selected.status)
  ) {
    await chatSubmitSettings({ model: value });
  } else {
    v.configForResume = !!ChatUI.selected;
    v.selectedModel = value;
    v.selectedEffort = null;
    chatSettings(v.settings);
    await chatLoadCatalog(false, value, !value);
  }
}
async function chatChooseEffort(value) {
  if (
    !value &&
    ChatUI.selected &&
    ["running", "starting"].includes(ChatUI.selected.status)
  ) {
    await chatSubmitSettings({ effort: "" });
    return;
  }
  if (!value) {
    const v = chatView();
    if (
      !ChatUI.selected ||
      !["running", "starting"].includes(ChatUI.selected.status)
    ) {
      v.configForResume = !!ChatUI.selected;
      v.selectedEffort = "";
      chatStatus("已恢复本机默认思考设置");
    }
    chatSettings(v.settings);
    return;
  }
  if (
    ChatUI.selected &&
    ["running", "starting"].includes(ChatUI.selected.status)
  )
    await chatSubmitSettings({ effort: value });
  else {
    chatView().configForResume = !!ChatUI.selected;
    chatView().selectedEffort = value;
    chatSettings(chatView().settings);
  }
}
async function chatSubmitSettings(patch, retry = false) {
  const c = ChatUI,
    v = chatView(),
    id = c.selected?.id,
    project = c.project,
    identity = S.session;
  const visible = () =>
    S.session === identity &&
    S.page === "native" &&
    !!$("#chat-root") &&
    chatView() === v;
  if (!id) return;
  if (
    v.settingOp &&
    !retry &&
    !["error", "uncertain"].includes(v.settingOp.state)
  ) {
    chatStatus("上一项设置正在确认，请稍候。");
    return;
  }
  // Uncertain timeouts/disconnects always preserve the original receipt; only
  // definitive rejections mint a new receipt when retried.
  const op = retry
    ? v.settingOp?.state === "error"
      ? {
          receipt: uid(),
          patch: v.settingOp.patch,
          state: "sending",
          generation: (v.v2SettingsGeneration =
            (v.v2SettingsGeneration || 0) + 1),
        }
      : v.settingOp
    : {
        receipt: uid(),
        patch,
        state: "sending",
        generation: (v.v2SettingsGeneration =
          (v.v2SettingsGeneration || 0) + 1),
      };
  if (!op) return;
  op.generation =
    op.generation ||
    (v.v2SettingsGeneration = (v.v2SettingsGeneration || 0) + 1);
  v.settingOp = op;
  op.state = "sending";
  if (
    v.v2State &&
    typeof CP !== "undefined" &&
    CP.nativeState?.upsertCommandOverlay
  ) {
    v.v2State = CP.nativeState.upsertCommandOverlay(v.v2State, {
      receipt: op.receipt,
      generation: op.generation,
      action: "settings",
      status: "pending",
      requested: op.patch,
    });
    if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
  }
  chatSettings(v.settings);
  try {
    const result = await chatAPI(
      "chat_settings",
      {
        id,
        receipt: op.receipt,
        settings_generation: op.generation,
        expected_settings_revision:
          v.settings?.settings_revision ?? v.settings?.revision ?? 0,
        ...op.patch,
      },
      project,
    );
    if (S.session !== identity || v.settingOp !== op) return;
    if (result.actual && typeof result.actual === "object") {
      v.settings = { ...(v.settings || {}), ...result.actual };
    }
    if (
      v.v2State &&
      typeof CP !== "undefined" &&
      CP.nativeState?.reconcileCommandOverlay
    ) {
      v.v2State = CP.nativeState.reconcileCommandOverlay(v.v2State, {
        receipt: op.receipt,
        generation: op.generation,
        status:
          result.state === "completed" || result.state === "applied"
            ? "applied"
            : ["queued", "claimed"].includes(result.state) || !result.state
              ? "accepted"
              : result.state,
        outcome: result.outcome,
        actual: result.actual,
        appliedRevision:
          result.applied_revision ?? result.projection_revision ?? null,
      });
      if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
    }
    if (op.state === "sending") op.state = result.state || "queued";
    if (result.outcome) op.outcome = result.outcome;
    if (visible()) {
      chatSettings(v.settings);
      chatStatus(
        op.state === "error"
          ? op.outcome === "partial"
            ? "部分设置已生效；当前模型与强度已回读，请核对后重试剩余项"
            : "原生设置失败，可重试"
          : "设置已提交，等待原生确认",
        op.state === "error",
      );
      if (!["error", "uncertain"].includes(op.state))
        chatWatchReceipt(id, op.receipt, c.generation, v, op);
    }
  } catch (e) {
    if (S.session !== identity || v.settingOp !== op) return;
    const isUncertain =
      ["NETWORK_UNCERTAIN", "CLI_UNCERTAIN", "CLI_OFFLINE"].includes(e.code) ||
      e.status === 408 ||
      !e.status;
    op.state = isUncertain ? "uncertain" : "error";
    op.error = e.message;
    if (
      v.v2State &&
      typeof CP !== "undefined" &&
      CP.nativeState?.reconcileCommandOverlay
    ) {
      v.v2State = CP.nativeState.reconcileCommandOverlay(v.v2State, {
        receipt: op.receipt,
        generation: op.generation,
        status: isUncertain ? "uncertain" : "failed",
        error: e.message,
      });
      if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
    }
    if (visible()) {
      chatStatus(e.message, true);
      chatSettings(v.settings);
    }
  }
}
