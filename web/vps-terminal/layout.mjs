import { computeTerminalViewport } from "./viewport.mjs";
import { DEFAULT_LIMITS } from "./protocol.mjs";

const MOBILE_LAYOUT =
  "(max-width: 700px), (pointer: coarse) and (max-height: 500px)";
const EDITABLE =
  ".xterm-helper-textarea, input:not([type=button]):not([type=checkbox]), textarea, [contenteditable=true]";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function action(text, fn, className = "") {
  const node = el("button", `vt-button ${className}`, text);
  node.type = "button";
  node.addEventListener("click", fn);
  return node;
}
function formatVpsEndpoint(item) {
  const user = item?.username ? `${item.username}@` : "";
  if (item?.host) return `${user}${item.host}:${item.port || 22}`;
  return item?.username || "";
}
function matchesVpsQuery(vps, query) {
  const needle = (query || "").trim().toLowerCase();
  if (!needle) return true;
  return [
    vps.name,
    vps.host,
    vps.username,
    vps.region,
    vps.provider,
    vps.system,
  ]
    .filter(Boolean)
    .some((field) => String(field).toLowerCase().includes(needle));
}

const QUICK_CONNECT_LIMIT = 40;
const isComposing = (event) => event.isComposing || event.keyCode === 229;

export class TerminalLayout {
  constructor({ store, onBack }) {
    this.store = store;
    this.onBack = onBack;
    this.tabs = new Map();
    this.disposed = false;
    this.quickIndex = 0;
    this.confirmSessionId = null;
    this.lastFocusedTabId = null;
    this.keepAwake = false;
    this.wakeLockSentinel = null;
    this.wakeLockPending = false;
    this.themePreference = "system";
    this.screenReaderPreference = false;
    this.appliedExpandedTheme = false;
    if (store && !store.measureInitialGrid) {
      this.ownsMeasureInitialGrid = true;
      store.measureInitialGrid = (limits) => this.measureInitialGrid(limits);
    }

    this.root = el("section", "vt-workspace");
    this.root.setAttribute("aria-label", "VPS 终端工作区");

    this.heading = el("div", "vt-heading");
    const back = action("← VPS", () => onBack?.(), "vt-back");
    this.title = el("h1", "", "终端");
    this.summary = el("span", "vt-summary");
    this.recentButton = action("最近会话", () => this.toggleRecent());
    this.heading.append(back, this.title, this.summary, this.recentButton);

    this.tabRow = el("div", "vt-tab-row");
    this.mobileBackButton = action("←", () => onBack?.(), "vt-mobile-back");
    this.mobileBackButton.setAttribute("aria-label", "返回 VPS");
    this.tabList = el("div", "vt-tabs");
    this.tabList.setAttribute("role", "tablist");
    this.tabList.setAttribute("aria-label", "SSH 会话");
    this.newButton = action(
      "＋ 新建",
      () => this.toggleQuickConnect(),
      "vt-new",
    );
    this.newButton.setAttribute("aria-label", "新建终端会话");
    this.newButton.setAttribute("aria-haspopup", "dialog");
    this.newButton.setAttribute("aria-expanded", "false");
    this.newButton.setAttribute("aria-controls", "vt-server-picker");
    this.moreButton = action(
      "⋯",
      () => this.toggleMoreMenu(),
      "vt-more-trigger",
    );
    this.moreButton.setAttribute("aria-label", "更多终端操作");
    this.moreButton.setAttribute("aria-haspopup", "menu");
    this.moreButton.setAttribute("aria-expanded", "false");
    this.quickConnect = this.makeQuickConnect();
    this.moreMenu = this.makeMoreMenu();
    this.tabRow.append(
      this.mobileBackButton,
      this.tabList,
      this.newButton,
      this.moreButton,
      this.quickConnect,
      this.moreMenu,
    );

    this.toolbar = el("div", "vt-toolbar");
    this.toolbar.setAttribute("aria-label", "终端操作");
    this.findButton = action("查找", () => this.focused()?.view.toggleSearch());
    this.copyButton = action("复制", () => this.focused()?.view.copy());
    this.pasteButton = action("粘贴", () =>
      this.focused()?.view.clipboardPaste(),
    );
    this.splitButton = action("分屏", () =>
      this.store.secondaryId
        ? store.unsplit()
        : this.run(() => store.duplicate(this.focused(), true)),
    );
    this.fullscreenButton = action("全屏", () => this.toggleFullscreen());
    this.diagnosticsButton = action("诊断", () =>
      this.focused()?.view.toggleDiagnostics(),
    );
    this.settingsDetails = this.makeSettings();
    this.endButton = action(
      "结束会话",
      () => this.confirmClose(this.focused(), this.endButton),
      "vt-end",
    );
    this.toolbar.append(
      this.findButton,
      this.copyButton,
      this.pasteButton,
      this.splitButton,
      this.fullscreenButton,
      this.diagnosticsButton,
      this.settingsDetails,
      this.endButton,
    );

    this.error = el("div", "vt-workspace-error");
    this.error.hidden = true;
    this.error.setAttribute("role", "alert");
    this.confirm = el("div", "vt-close-confirm");
    this.confirm.hidden = true;
    this.confirm.setAttribute("role", "alertdialog");
    this.confirm.setAttribute("aria-labelledby", "vt-close-confirm-title");
    this.recent = el("section", "vt-recent");
    this.recent.hidden = true;

    this.deck = el("div", "vt-deck");
    this.paneHosts = new Map();
    this.gutter = el("div", "vt-gutter");
    this.gutter.tabIndex = 0;
    this.gutter.setAttribute("role", "separator");
    this.gutter.setAttribute("aria-label", "调整终端分屏宽度");
    this.gutter.setAttribute("aria-orientation", "vertical");
    this.gutter.setAttribute("aria-valuemin", "25");
    this.gutter.setAttribute("aria-valuemax", "75");
    this.gutter.setAttribute("aria-valuenow", "50");
    this.deck.append(this.gutter);

    this.empty = this.makeEmpty();

    this.root.append(
      this.heading,
      this.tabRow,
      this.toolbar,
      this.error,
      this.confirm,
      this.recent,
      this.deck,
      this.empty,
    );
    this.parking = el("div", "vt-parking");
    this.parking.hidden = true;
    document.body.append(this.parking);
    this.parking.append(this.root);

    this.viewport = () => this.fitViewport();
    window.addEventListener("resize", this.viewport);
    window.addEventListener("orientationchange", this.viewport);
    window.screen?.orientation?.addEventListener?.("change", this.viewport);
    this.root.addEventListener("focusin", this.viewport);
    this.root.addEventListener("focusout", this.viewport);
    globalThis.visualViewport?.addEventListener?.("resize", this.viewport);
    globalThis.visualViewport?.addEventListener?.("scroll", this.viewport);

    this.fullscreen = () => {
      this.syncFullscreenLabel();
      this.fitViewport();
    };
    document.addEventListener("fullscreenchange", this.fullscreen);

    this.lifecycleResume = () => this.handleForegroundChange();
    document.addEventListener("visibilitychange", this.lifecycleResume);
    window.addEventListener("pageshow", this.lifecycleResume);
    window.addEventListener("online", this.lifecycleResume);

    this.outsidePointer = (event) => {
      if (!this.visible || this.disposed) return;
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (
        this.settingsDetails?.open &&
        !this.settingsDetails.contains(target)
      ) {
        this.settingsDetails.open = false;
      }
      if (
        !this.moreMenu.hidden &&
        !this.moreMenu.contains(target) &&
        !this.moreButton.contains(target)
      ) {
        this.closeMoreMenu();
      }
      if (
        !this.quickConnect.hidden &&
        !this.quickConnect.contains(target) &&
        !this.newButton.contains(target)
      ) {
        this.closeQuickConnect();
      }
      if (
        !this.recent.hidden &&
        !this.recent.contains(target) &&
        !this.recentButton.contains(target) &&
        !this.moreRecentButton?.contains(target)
      ) {
        this.recent.hidden = true;
      }
    };
    document.addEventListener("pointerdown", this.outsidePointer);

    this.escapeHandler = (event) => {
      if (
        !this.visible ||
        this.disposed ||
        event.key !== "Escape" ||
        event.defaultPrevented ||
        isComposing(event)
      )
        return;
      if (!this.confirm.hidden) {
        event.preventDefault();
        this.dismissConfirm(true);
        return;
      }
      if (!this.moreMenu.hidden) {
        event.preventDefault();
        this.closeMoreMenu({ restoreFocus: true });
        return;
      }
      if (!this.quickConnect.hidden) {
        event.preventDefault();
        this.closeQuickConnect({ restoreFocus: true });
        return;
      }
      if (this.settingsDetails?.open) {
        event.preventDefault();
        this.settingsDetails.open = false;
        this.settingsDetails.querySelector("summary")?.focus();
        return;
      }
      if (!this.recent.hidden) {
        event.preventDefault();
        this.recent.hidden = true;
        this.recentButton.focus();
        return;
      }
      if (this.root.classList.contains("vt-expanded")) {
        event.preventDefault();
        this.root.classList.remove("vt-expanded");
        this.syncFullscreenLabel();
        this.fitViewport();
      }
    };
    document.addEventListener("keydown", this.escapeHandler);

    this.setupGutter();
    this.render();
  }

  focused() {
    return this.store.records.get(this.store.focusedId) || this.store.active;
  }

  availableServers() {
    if (this.store.vpsCatalog.length) return this.store.vpsCatalog;
    const seen = new Set();
    const list = [];
    for (const record of this.store.records.values()) {
      if (
        record.vps?.id &&
        !seen.has(record.vps.id) &&
        record.vps.enabled !== false
      ) {
        seen.add(record.vps.id);
        list.push(record.vps);
      }
    }
    return list;
  }

  async run(fn) {
    this.error.hidden = true;
    try {
      return await fn();
    } catch (error) {
      if (!this.disposed) {
        this.error.textContent = error.message || "暂时无法完成，请重试";
        this.error.hidden = false;
      }
    }
  }

  mount(container) {
    if (this.disposed) return;
    if (this.root.parentNode !== container)
      container.replaceChildren(this.root);
    this.root.hidden = false;
    this.visible = true;
    this.syncFullscreenLabel();
    this.render();
    this.fitViewport();
  }

  hide() {
    if (this.disposed) return;
    this.visible = false;
    this.dismissConfirm();
    this.closeQuickConnect();
    this.closeMoreMenu();
    if (this.settingsDetails) this.settingsDetails.open = false;
    for (const record of this.store.records.values())
      record.view?.syncHardwareAcceleration?.(false);
    this.releaseWakeLock();
    this.syncFullscreenThemeColor(true);
    if (document.fullscreenElement === this.root)
      document.exitFullscreen().catch(() => {});
    this.root.hidden = true;
    this.parking.append(this.root);
  }

  measureInitialGrid(limits = DEFAULT_LIMITS) {
    if (!this.visible || !this.root?.isConnected) return null;
    const narrow = !!globalThis.matchMedia?.(MOBILE_LAYOUT).matches;
    const mobileFont = !!globalThis.matchMedia?.("(max-width: 700px)").matches;
    const fontSize = this.focused()?.view?.fontSize || (mobileFont ? 13.5 : 14);
    const lineHeight = mobileFont ? 1.18 : 1.2;
    const activeCanvas = this.store.active?.view?.canvas;
    let usableWidth = 0;
    let usableHeight = 0;
    if (
      activeCanvas?.isConnected &&
      activeCanvas.clientWidth >= 40 &&
      activeCanvas.clientHeight >= 40
    ) {
      const pad = narrow ? 12 : 16;
      usableWidth = activeCanvas.clientWidth - pad;
      usableHeight = activeCanvas.clientHeight - pad;
    } else {
      const rect = this.root.getBoundingClientRect?.();
      const totalWidth = rect?.width || 0;
      const totalHeight =
        this.viewportState?.availableHeight || rect?.height || 0;
      if (totalWidth < 80 || totalHeight < 120) return null;
      const chromeHeight = narrow ? 92 : 146;
      const pad = narrow ? 12 : 16;
      usableWidth = totalWidth - pad;
      usableHeight = totalHeight - chromeHeight;
    }
    if (usableWidth < 40 || usableHeight < 40) return null;
    const cellWidth = Math.max(5, fontSize * 0.6);
    const cellHeight = Math.max(10, fontSize * lineHeight);
    return {
      cols: Math.max(
        limits.min_cols,
        Math.min(limits.max_cols, Math.floor(usableWidth / cellWidth)),
      ),
      rows: Math.max(
        limits.min_rows,
        Math.min(limits.max_rows, Math.floor(usableHeight / cellHeight)),
      ),
    };
  }

  handleForegroundChange() {
    if (this.disposed) return;
    const visible = globalThis.document?.visibilityState !== "hidden";
    this.syncWakeLock();
    if (!this.visible) return;
    this.render();
    if (visible) {
      for (const record of this.store.records.values())
        record.transport?.resumeOnForeground?.();
      this.fitViewport();
    }
  }

  makeEmpty() {
    const container = el("div", "vt-empty");
    const hero = el("div", "vt-empty-hero");
    this.emptyHeading = el("h2", "", "在这里连接服务器");
    this.emptyNote = el("p", "", "选择一台 VPS，即可打开 SSH 终端。");
    hero.append(this.emptyHeading, this.emptyNote);

    this.emptyQuick = el("div", "vt-empty-quick");
    this.emptyQuick.hidden = true;
    this.emptySearchInput = el("input", "vt-empty-search");
    this.emptySearchInput.type = "search";
    this.emptySearchInput.placeholder = "搜索服务器名称、IP、账号或地区";
    this.emptySearchInput.setAttribute("aria-label", "搜索可用服务器");
    this.emptySearchInput.addEventListener("input", () =>
      this.renderEmptyServers(),
    );
    this.emptySearchInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        const first = this.filteredEmptyServers()[0];
        if (first && !this.store.creating.size) {
          event.preventDefault();
          this.run(() => this.store.open({ vps: first }));
        }
      }
    });
    this.emptyGrid = el("div", "vt-empty-grid");
    this.emptyQuick.append(this.emptySearchInput, this.emptyGrid);

    this.emptyResume = el("div", "vt-empty-resume");
    this.emptyResume.hidden = true;
    this.emptyResumeTitle = el(
      "strong",
      "vt-empty-section-title",
      "恢复远端持久会话",
    );
    this.emptyResumeList = el("div", "vt-empty-resume-list");
    this.emptyResume.append(this.emptyResumeTitle, this.emptyResumeList);

    const actions = el("div", "vt-empty-actions");
    this.emptyBackButton = action(
      "返回 VPS",
      () => this.onBack?.(),
      "vt-primary",
    );
    actions.append(this.emptyBackButton);

    container.append(hero, this.emptyQuick, this.emptyResume, actions);
    return container;
  }

  filteredEmptyServers() {
    return this.store.vpsCatalog.filter((vps) =>
      matchesVpsQuery(vps, this.emptySearchInput.value),
    );
  }

  renderEmptyServers() {
    const servers = this.store.vpsCatalog;
    this.emptyQuick.hidden = !servers.length;
    this.emptyBackButton.classList.toggle("vt-primary", !servers.length);
    if (!servers.length) {
      this.emptyGrid.replaceChildren();
      return;
    }
    const filtered = this.filteredEmptyServers();
    this.emptyGrid.replaceChildren();
    if (!filtered.length) {
      this.emptyGrid.append(el("p", "vt-muted", "没有匹配的服务器"));
      return;
    }
    const busy = this.store.creating.size > 0;
    for (const vps of filtered.slice(0, 40)) {
      const card = el("div", "vt-server-card");
      const info = el("div", "vt-server-info");
      const titleRow = el("div", "vt-server-title-row");
      titleRow.append(el("strong", "vt-server-name", vps.name || "VPS"));
      for (const tag of [vps.region, vps.system].filter(Boolean)) {
        titleRow.append(el("span", "vt-badge", tag));
      }
      const endpoint = el(
        "span",
        "vt-server-endpoint",
        formatVpsEndpoint(vps) || "SSH",
      );
      info.append(titleRow, endpoint);
      const connectBtn = action(
        "连接",
        () => this.run(() => this.store.open({ vps })),
        "vt-primary vt-server-connect",
      );
      connectBtn.disabled = busy;
      card.append(info, connectBtn);
      this.emptyGrid.append(card);
    }
  }

  renderEmptyResume() {
    const sessions = this.store.remoteSessions
      .filter(
        (session) =>
          !["exited", "closed", "failed", "revoked"].includes(session.state),
      )
      .slice(0, 8);
    this.emptyResume.hidden = !sessions.length;
    this.emptyResumeList.replaceChildren();
    for (const session of sessions) {
      const id = session.session_id || session.id;
      const item = action(
        "",
        () => this.run(() => this.store.restore(id)),
        "vt-empty-session",
      );
      item.append(
        el("strong", "", session.vps_name || session.name || "VPS"),
        el(
          "span",
          "vt-muted",
          `${formatVpsEndpoint(session) || session.device_name || "SSH"} · ${
            session.mode === "tmux" ? "持久会话" : "普通会话"
          }`,
        ),
      );
      this.emptyResumeList.append(item);
    }
  }

  makeQuickConnect() {
    const popover = el("div", "vt-quick-connect");
    popover.hidden = true;
    popover.id = "vt-server-picker";
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-label", "快速连接服务器");
    this.quickSearchInput = el("input", "vt-quick-search");
    this.quickSearchInput.type = "search";
    this.quickSearchInput.placeholder = "搜索名称、IP、账号或地区";
    this.quickSearchInput.setAttribute("aria-label", "搜索要连接的服务器");
    this.quickSearchInput.addEventListener("input", () => {
      this.quickIndex = 0;
      this.renderQuickConnect();
    });
    popover.addEventListener("keydown", (event) => {
      if (isComposing(event)) return;
      const items = this.filteredQuickServers();
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        this.closeQuickConnect({ restoreFocus: true });
        return;
      }
      if (!items.length) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        this.quickIndex = (this.quickIndex + 1) % items.length;
        this.syncQuickSelection();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        this.quickIndex = (this.quickIndex - 1 + items.length) % items.length;
        this.syncQuickSelection();
      } else if (
        event.key === "Enter" &&
        event.target === this.quickSearchInput
      ) {
        event.preventDefault();
        const target = items[this.quickIndex] || items[0];
        if (target)
          this.openFromQuickConnect(target, event.shiftKey && !this.narrow);
      }
    });
    this.quickList = el("div", "vt-quick-list");
    const cancel = action(
      "取消",
      () => this.closeQuickConnect({ restoreFocus: true }),
      "vt-quick-cancel",
    );
    popover.append(this.quickSearchInput, this.quickList, cancel);
    return popover;
  }

  filteredQuickServers() {
    return this.availableServers()
      .filter((vps) => matchesVpsQuery(vps, this.quickSearchInput.value))
      .slice(0, QUICK_CONNECT_LIMIT);
  }

  toggleQuickConnect(force = this.quickConnect.hidden) {
    if (!force) {
      this.closeQuickConnect();
      return;
    }
    this.closeMoreMenu();
    if (this.settingsDetails) this.settingsDetails.open = false;
    this.recent.hidden = true;
    this.quickConnect.hidden = false;
    this.newButton.setAttribute("aria-expanded", "true");
    this.quickSearchInput.value = "";
    this.quickIndex = 0;
    this.renderQuickConnect();
    this.quickSearchInput.focus();
    this.store.refreshVpsCatalog().then(() => {
      if (!this.disposed && !this.quickConnect.hidden)
        this.renderQuickConnect();
    });
  }

  closeQuickConnect({ restoreFocus = false } = {}) {
    if (this.quickConnect.hidden) return;
    this.quickConnect.hidden = true;
    this.newButton.setAttribute("aria-expanded", "false");
    if (restoreFocus) this.newButton.focus();
  }

  openFromQuickConnect(vps, split = false) {
    this.closeQuickConnect();
    return this.run(() => this.store.open({ vps, newSession: true, split }));
  }

  syncQuickSelection() {
    const rows = [...this.quickList.querySelectorAll(".vt-quick-row")];
    rows.forEach((row, index) => {
      const active = index === this.quickIndex;
      row.classList.toggle("vt-quick-row-active", active);
      if (active) row.scrollIntoView?.({ block: "nearest" });
    });
  }

  renderQuickConnect() {
    const items = this.filteredQuickServers();
    if (this.quickIndex >= items.length) this.quickIndex = 0;
    this.quickList.replaceChildren();
    if (!items.length) {
      const empty = el("div", "vt-quick-empty");
      empty.append(
        el("span", "vt-muted", "暂无匹配的可用服务器"),
        action("返回 VPS", () => {
          this.closeQuickConnect();
          this.onBack?.();
        }),
      );
      this.quickList.append(empty);
      return;
    }
    items.forEach((vps, index) => {
      const row = el("div", "vt-quick-row");
      if (index === this.quickIndex) row.classList.add("vt-quick-row-active");
      const main = action(
        "",
        () => this.openFromQuickConnect(vps, false),
        "vt-quick-main",
      );
      const titleLine = el("span", "vt-quick-title");
      titleLine.append(el("strong", "", vps.name || "VPS"));
      if (vps.region) titleLine.append(el("span", "vt-badge", vps.region));
      main.append(
        titleLine,
        el("span", "vt-quick-endpoint", formatVpsEndpoint(vps) || "SSH"),
      );
      const actions = el("span", "vt-quick-actions");
      actions.append(
        action(
          "新标签",
          () => this.openFromQuickConnect(vps, false),
          "vt-quick-btn",
        ),
      );
      if (!this.narrow) {
        actions.append(
          action(
            "右分屏",
            () => this.openFromQuickConnect(vps, true),
            "vt-quick-btn",
          ),
        );
      }
      row.append(main, actions);
      this.quickList.append(row);
    });
  }

  render() {
    if (this.disposed) return;
    const store = this.store,
      records = [...store.records.values()];
    const narrow = window.matchMedia(MOBILE_LAYOUT).matches;
    const coarse = window.matchMedia("(pointer: coarse)").matches;
    this.narrow = narrow;
    this.splitButton.hidden = narrow;
    this.newButton.textContent = narrow ? "＋" : "＋ 新建";
    // Rotation changes visible slots, not the store's split or its order.
    const primaryId =
      narrow && store.secondaryId && store.focusedId === store.secondaryId
        ? store.secondaryId
        : store.activeId;
    const secondaryId = narrow ? null : store.secondaryId;
    this.summary.textContent = records.length ? `${store.count()} 个会话` : "";
    this.empty.hidden = !!store.active;
    this.deck.hidden = !store.active;
    this.emptyHeading.textContent = store.creating.size
      ? "正在打开终端"
      : "在这里连接服务器";
    this.emptyNote.textContent = store.creating.size
      ? "正在确认连接电脑与 SSH 能力…"
      : "选择一台 VPS，即可直接打开 SSH 终端。";
    if (!store.active) {
      this.renderEmptyServers();
      this.renderEmptyResume();
    }
    this.newButton.disabled = store.creating.size > 0;
    this.splitButton.textContent = store.secondaryId ? "合并分屏" : "分屏";
    this.root.classList.toggle("vt-has-split", !!secondaryId);

    for (const [id, tab] of this.tabs)
      if (!store.records.has(id)) {
        tab.remove();
        this.tabs.delete(id);
        this.paneHosts.get(id)?.remove();
        this.paneHosts.delete(id);
      }
    for (const record of records) {
      this.renderSessionTabAndSlot(record, { primaryId, secondaryId, coarse });
    }

    const record = this.focused();
    if (!record) this.lastFocusedTabId = null;
    const confirmedTab = store.records.get(this.confirmSessionId);
    if (
      !this.confirm.hidden &&
      (!confirmedTab ||
        store.isEnded(confirmedTab) ||
        (this.confirmDismissesTab && confirmedTab.tabLocked))
    )
      this.dismissConfirm(true);

    for (const node of [
      this.findButton,
      this.copyButton,
      this.splitButton,
      this.endButton,
      this.diagnosticsButton,
    ])
      node.disabled = !record;
    this.pasteButton.disabled = !record?.transport.canInput;
    this.detachButton.disabled =
      !record ||
      !!record.tabLocked ||
      !!record.closing ||
      !!record.dismissOnEnd;
    this.endButton.disabled =
      !record ||
      record.closing ||
      ["exited", "closed", "revoked"].includes(record.state);
    this.endButton.textContent = record?.closing ? "正在结束" : "结束会话";
    this.syncMoreMenu(record);
    this.syncWakeLock();
    if (!this.recent.hidden) this.renderRecent();
    if (!this.quickConnect.hidden) this.renderQuickConnect();
  }

  createTabElement(record) {
    const store = this.store;
    const tab = el("div", "vt-tab");
    tab.dataset.sessionId = record.id;
    tab.addEventListener("mousedown", (event) => {
      if (event.button === 1) event.preventDefault();
    });
    tab.addEventListener("auxclick", (event) => {
      if (event.button === 1) {
        event.preventDefault();
        this.confirmClose(record, tab.querySelector(".vt-tab-close"), true);
      }
    });
    const choose = action("", () => store.activate(record.id));
    choose.className = "vt-tab-select";
    choose.setAttribute("role", "tab");
    choose.id = `vt-tab-${record.id}`;
    choose.setAttribute("aria-controls", `vt-pane-${record.id}`);
    const dot = el("span", "vt-tab-dot");
    dot.setAttribute("aria-hidden", "true");
    const label = el("span", "vt-tab-label");
    const lockBadge = el("span", "vt-tab-lock-badge", "锁");
    lockBadge.setAttribute("aria-hidden", "true");
    lockBadge.hidden = true;
    const badge = el("span", "vt-tab-badge", "tmux");
    badge.setAttribute("aria-hidden", "true");
    choose.append(dot, label, lockBadge, badge);
    choose.addEventListener("dblclick", () => this.rename(record, tab));
    choose.addEventListener("keydown", (event) =>
      this.handleTabKeydown(event, record, tab),
    );
    const close = action(
      "×",
      () => this.confirmClose(record, close, true),
      "vt-tab-close",
    );
    const lock = action(
      "锁",
      () => {
        record.tabLocked = !record.tabLocked;
        this.render();
      },
      "vt-tab-lock",
    );
    tab.append(choose, lock, close);
    this.tabs.set(record.id, tab);
    this.tabList.append(tab);
    return tab;
  }

  handleTabKeydown(event, record, tab) {
    if (event.key === "F2") {
      event.preventDefault();
      this.rename(record, tab);
      return;
    }
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const ids = [...this.store.records.keys()];
    const current = ids.indexOf(record.id);
    const index =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? ids.length - 1
          : (current + (event.key === "ArrowRight" ? 1 : -1) + ids.length) %
            ids.length;
    this.store.activate(ids[index]);
    this.tabs.get(ids[index])?.querySelector("[role=tab]")?.focus();
  }

  renderSessionTabAndSlot(record, { primaryId, secondaryId, coarse }) {
    const store = this.store;
    const tab = this.tabs.get(record.id) || this.createTabElement(record);
    const selected = record.id === primaryId || record.id === secondaryId;
    const focused = record.id === store.focusedId;
    const choose = tab.querySelector("[role=tab]");
    choose.setAttribute("aria-selected", String(selected));
    choose.tabIndex =
      focused || (!store.focusedId && record.id === store.activeId) ? 0 : -1;
    tab.classList.toggle("vt-tab-active", selected);
    tab.classList.toggle("vt-tab-focused", focused);
    tab.dataset.state = record.state;
    this.syncTabControls(tab, choose, record);
    if (focused && this.lastFocusedTabId !== record.id) {
      this.lastFocusedTabId = record.id;
      tab.scrollIntoView?.({ block: "nearest", inline: "nearest" });
    }
    this.syncPaneSlot(record, choose, {
      primaryId,
      secondaryId,
      focused,
      coarse,
    });
  }

  syncTabControls(tab, choose, record) {
    const close = tab.querySelector(".vt-tab-close");
    const lock = tab.querySelector(".vt-tab-lock");
    close.disabled = !!record.tabLocked || !!record.closing;
    close.title = record.tabLocked
      ? "标签已锁定，解锁后可关闭"
      : record.state === "close_unknown"
        ? "结束结果待确认，点击重试"
        : record.closing
          ? "正在结束会话"
          : "结束会话并关闭标签";
    close.setAttribute("aria-label", `${record.label}：${close.title}`);
    lock.textContent = record.tabLocked ? "解锁" : "锁";
    lock.title = record.tabLocked ? "解锁标签" : "锁定标签，防止误关";
    lock.setAttribute("aria-label", `${record.label}：${lock.title}`);
    lock.setAttribute("aria-pressed", String(!!record.tabLocked));
    lock.disabled = !!record.closing;
    const lockBadge = tab.querySelector(".vt-tab-lock-badge");
    if (lockBadge) lockBadge.hidden = !record.tabLocked;
    tab.querySelector(".vt-tab-label").textContent = record.label;
    const badge = tab.querySelector(".vt-tab-badge");
    if (badge) badge.hidden = record.session.mode !== "tmux";
    choose.title = [
      record.label,
      formatVpsEndpoint(record.session) || record.session.device_name || "SSH",
      record.session.mode === "tmux" && "tmux 持久会话",
    ]
      .filter(Boolean)
      .join(" · ");
  }

  syncPaneSlot(record, choose, { primaryId, secondaryId, focused, coarse }) {
    const viewRoot = record.view.root;
    viewRoot.id = `vt-pane-${record.id}`;
    viewRoot.setAttribute("role", "tabpanel");
    viewRoot.setAttribute("aria-labelledby", choose.id);
    let host = this.paneHosts.get(record.id);
    if (!host) {
      host = el("div", "vt-slot");
      host.hidden = true;
      host.append(viewRoot);
      this.deck.append(host);
      this.paneHosts.set(record.id, host);
    }
    const primary = record.id === primaryId;
    const secondary = record.id === secondaryId;
    const displayed = primary || secondary;
    const changed =
      host.hidden !== !displayed ||
      host.classList.contains("vt-primary-slot") !== primary;
    host.classList.toggle("vt-primary-slot", primary);
    host.classList.toggle("vt-secondary-slot", secondary);
    host.hidden = !displayed;
    host.inert = !displayed;
    viewRoot.classList.toggle("vt-pane-focused", focused);
    const gpuEligible =
      displayed &&
      this.visible &&
      (!coarse || record.id === (this.store.focusedId || primaryId));
    record.view.syncHardwareAcceleration?.(gpuEligible);
    if (displayed && this.visible && (changed || !record.view.terminal))
      record.view.show();
  }

  rename(record, tab) {
    if (tab.querySelector("input")) return;
    const select = tab.querySelector(".vt-tab-select"),
      input = el("input", "vt-tab-name");
    input.value = record.label;
    input.maxLength = 40;
    input.setAttribute("aria-label", "会话名称");
    select.hidden = true;
    tab.prepend(input);
    input.focus();
    input.select();
    const save = () => {
      if (!input.isConnected) return;
      this.store.rename(record.id, input.value);
      input.remove();
      select.hidden = false;
      select.focus();
    };
    input.addEventListener("blur", save, { once: true });
    input.addEventListener("keydown", (event) => {
      if (isComposing(event)) return;
      if (event.key === "Enter") {
        event.preventDefault();
        save();
      }
      if (event.key === "Escape") {
        event.preventDefault();
        input.value = record.label;
        save();
      }
    });
  }

  async toggleRecent() {
    this.closeQuickConnect();
    this.closeMoreMenu();
    if (this.settingsDetails) this.settingsDetails.open = false;
    this.recent.hidden = !this.recent.hidden;
    if (this.recent.hidden) return;
    this.recent.replaceChildren(el("p", "vt-muted", "正在读取会话…"));
    await this.run(() => this.store.refreshSessions());
    this.renderRecent();
  }

  renderRecent() {
    const heading = el("div", "vt-recent-heading");
    heading.append(
      el("strong", "", "最近会话"),
      action("收起", () => {
        this.recent.hidden = true;
      }),
    );
    const list = el("div", "vt-recent-list");
    const sessions = this.store
      .list()
      .filter((session) => !this.store.isEnded(session))
      .slice(0, 20);
    for (const session of sessions) {
      const id = session.session_id || session.id,
        row = action(
          "",
          () =>
            this.run(async () => {
              await this.store.restore(id);
              this.recent.hidden = true;
            }),
          "vt-recent-item",
        );
      row.append(
        el("strong", "", session.vps_name || session.name || "VPS"),
        el(
          "span",
          "vt-muted",
          `${formatVpsEndpoint(session) || session.device_name || "SSH"} · ${
            session.mode === "tmux" ? "持久会话" : "普通会话"
          }`,
        ),
      );
      list.append(row);
    }
    if (!sessions.length) list.append(el("p", "vt-muted", "暂无可恢复的会话"));
    this.recent.replaceChildren(heading, list);
  }

  applyThemePreference(value) {
    this.themePreference = value;
    if (this.themeSelect) this.themeSelect.value = value;
    if (this.moreThemeSelect) this.moreThemeSelect.value = value;
    for (const record of this.store.records.values())
      record.view.setTheme(value);
  }

  applyScreenReaderPreference(enabled) {
    this.screenReaderPreference = !!enabled;
    if (this.screenReaderCheckbox)
      this.screenReaderCheckbox.checked = !!enabled;
    if (this.moreScreenReaderCheckbox)
      this.moreScreenReaderCheckbox.checked = !!enabled;
    for (const record of this.store.records.values())
      record.view.setScreenReader(!!enabled);
  }

  setKeepAwake(enabled) {
    this.keepAwake = !!enabled;
    if (this.wakeLockCheckbox) this.wakeLockCheckbox.checked = this.keepAwake;
    if (this.moreWakeLockCheckbox)
      this.moreWakeLockCheckbox.checked = this.keepAwake;
    this.syncWakeLock();
  }

  shouldHoldWakeLock() {
    const current = this.focused() || this.store.active;
    return (
      this.keepAwake &&
      !!this.visible &&
      !this.disposed &&
      globalThis.document?.visibilityState !== "hidden" &&
      current?.state === "ready"
    );
  }

  async syncWakeLock() {
    if (!this.shouldHoldWakeLock()) {
      await this.releaseWakeLock();
      return;
    }
    if (this.wakeLockSentinel || this.wakeLockPending) return;
    const wakeLock = globalThis.navigator?.wakeLock;
    if (typeof wakeLock?.request !== "function") return;
    this.wakeLockPending = true;
    try {
      const sentinel = await wakeLock.request("screen");
      if (!this.shouldHoldWakeLock()) {
        await sentinel?.release?.().catch(() => {});
        return;
      }
      this.wakeLockSentinel = sentinel;
      sentinel?.addEventListener?.("release", () => {
        if (this.wakeLockSentinel === sentinel) this.wakeLockSentinel = null;
      });
    } catch {
      /* Battery saver or browser policy denied wake lock. */
    } finally {
      this.wakeLockPending = false;
    }
  }

  async releaseWakeLock() {
    const sentinel = this.wakeLockSentinel;
    this.wakeLockSentinel = null;
    if (sentinel) await sentinel.release?.().catch(() => {});
  }

  makeMoreMenu() {
    const menu = el("div", "vt-more-menu");
    menu.hidden = true;
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", "更多终端操作");
    this.moreMeta = el("div", "vt-more-meta");
    this.moreMeta.hidden = true;
    const actionsGrid = this.buildMoreActionsGrid();
    const settingsRows = this.buildMoreSettingsRows();
    this.moreEndButton = action(
      "结束会话",
      () => {
        const record = this.focused();
        this.closeMoreMenu();
        this.confirmClose(record, this.moreButton);
      },
      "vt-danger vt-more-end",
    );
    menu.append(
      this.moreMeta,
      actionsGrid,
      ...settingsRows,
      this.moreEndButton,
    );
    return menu;
  }

  buildMoreActionsGrid() {
    const actionsGrid = el("div", "vt-more-actions");
    this.moreFindButton = action("查找", () => {
      this.closeMoreMenu();
      this.focused()?.view.toggleSearch();
    });
    this.moreCopyButton = action("复制选中", () => {
      this.closeMoreMenu();
      this.focused()?.view.copy();
    });
    this.moreFullscreenButton = action("全屏", () => {
      this.closeMoreMenu();
      this.toggleFullscreen();
    });
    this.moreDiagnosticsButton = action("诊断", () => {
      this.closeMoreMenu();
      this.focused()?.view.toggleDiagnostics();
    });
    this.moreLockButton = action("锁定标签", () => {
      const record = this.focused();
      if (record) {
        record.tabLocked = !record.tabLocked;
        this.render();
      }
      this.closeMoreMenu();
    });
    this.moreDetachButton = action("分离标签", () => {
      const record = this.focused();
      this.closeMoreMenu();
      if (record) this.store.detach(record.id);
    });
    this.moreRecentButton = action("最近会话", () => {
      this.closeMoreMenu();
      this.toggleRecent();
    });
    actionsGrid.append(
      this.moreFindButton,
      this.moreCopyButton,
      this.moreFullscreenButton,
      this.moreDiagnosticsButton,
      this.moreLockButton,
      this.moreDetachButton,
      this.moreRecentButton,
    );
    return actionsGrid;
  }

  buildMoreSettingsRows() {
    const fontRow = el("div", "vt-settings-row");
    this.moreFontValue = el("span", "", "字号");
    fontRow.append(
      this.moreFontValue,
      action("−", () => {
        const view = this.focused()?.view;
        if (view) view.setFontSize(view.fontSize - 0.5);
        this.syncMoreMenu();
      }),
      action("＋", () => {
        const view = this.focused()?.view;
        if (view) view.setFontSize(view.fontSize + 0.5);
        this.syncMoreMenu();
      }),
    );
    const themeRow = el("label", "vt-settings-row", "主题");
    this.moreThemeSelect = this.makeThemeSelect();
    themeRow.append(this.moreThemeSelect);

    const wakeRow = el("label", "vt-settings-row");
    this.moreWakeLockCheckbox = el("input");
    this.moreWakeLockCheckbox.type = "checkbox";
    this.moreWakeLockCheckbox.addEventListener("change", () =>
      this.setKeepAwake(this.moreWakeLockCheckbox.checked),
    );
    wakeRow.append(
      this.moreWakeLockCheckbox,
      document.createTextNode("保持屏幕常亮"),
    );

    const readerRow = el("label", "vt-settings-row");
    this.moreScreenReaderCheckbox = el("input");
    this.moreScreenReaderCheckbox.type = "checkbox";
    this.moreScreenReaderCheckbox.addEventListener("change", () =>
      this.applyScreenReaderPreference(this.moreScreenReaderCheckbox.checked),
    );
    readerRow.append(
      this.moreScreenReaderCheckbox,
      document.createTextNode("屏幕阅读器模式"),
    );
    return [fontRow, themeRow, wakeRow, readerRow];
  }

  toggleMoreMenu(force = this.moreMenu.hidden) {
    if (!force) {
      this.closeMoreMenu();
      return;
    }
    this.closeQuickConnect();
    if (this.settingsDetails) this.settingsDetails.open = false;
    this.recent.hidden = true;
    this.syncMoreMenu();
    this.moreMenu.hidden = false;
    this.moreButton.setAttribute("aria-expanded", "true");
  }

  closeMoreMenu({ restoreFocus = false } = {}) {
    if (!this.moreMenu || this.moreMenu.hidden) return;
    this.moreMenu.hidden = true;
    this.moreButton?.setAttribute("aria-expanded", "false");
    if (restoreFocus) this.moreButton?.focus();
  }

  syncMoreMenu(record = this.focused()) {
    if (!this.moreMenu) return;
    const expanded =
      globalThis.document?.fullscreenElement === this.root ||
      this.root.classList.contains("vt-expanded");
    this.moreFullscreenButton.textContent = expanded ? "退出全屏" : "全屏";
    for (const node of [
      this.moreFindButton,
      this.moreCopyButton,
      this.moreDiagnosticsButton,
      this.moreLockButton,
    ])
      node.disabled = !record;
    this.moreLockButton.textContent = record?.tabLocked
      ? "解锁标签"
      : "锁定标签";
    this.moreDetachButton.disabled =
      !record ||
      !!record.tabLocked ||
      !!record.closing ||
      !!record.dismissOnEnd;
    this.moreEndButton.disabled =
      !record ||
      record.closing ||
      ["exited", "closed", "revoked"].includes(record.state);
    this.moreEndButton.textContent = record?.closing ? "正在结束" : "结束会话";
    if (record?.view) {
      this.moreFontValue.textContent = `字号 ${record.view.fontSize}`;
      const parts = [
        formatVpsEndpoint(record.session) || record.session.device_name,
        record.session.mode === "tmux" ? "tmux 持久" : "直连",
        record.rttMs != null ? `${Math.round(record.rttMs)} ms` : null,
        record.view.sizeBadge?.textContent || null,
      ].filter(Boolean);
      this.moreMeta.textContent = parts.join(" · ");
      this.moreMeta.hidden = !parts.length;
    } else {
      this.moreFontValue.textContent = "字号";
      this.moreMeta.hidden = true;
    }
  }

  makeThemeSelect() {
    const themes = el("select");
    themes.setAttribute("aria-label", "终端主题");
    for (const [value, label] of [
      ["system", "跟随面板"],
      ["light", "浅色"],
      ["dark", "深色"],
    ]) {
      const option = el("option", "", label);
      option.value = value;
      themes.append(option);
    }
    themes.addEventListener("change", () =>
      this.applyThemePreference(themes.value),
    );
    return themes;
  }

  makeSettings() {
    const details = el("details", "vt-settings"),
      summary = el("summary", "vt-button", "显示");
    details.addEventListener("toggle", () => {
      if (details.open) {
        this.closeQuickConnect();
        this.closeMoreMenu();
        this.recent.hidden = true;
      }
    });
    const panel = el("div", "vt-settings-panel");
    const sizes = el("div", "vt-settings-row");
    sizes.append(
      el("span", "", "字号"),
      action("−", () => {
        const view = this.focused()?.view;
        if (view) view.setFontSize(view.fontSize - 1);
      }),
      action("＋", () => {
        const view = this.focused()?.view;
        if (view) view.setFontSize(view.fontSize + 1);
      }),
    );
    const themeLabel = el("label", "vt-settings-row", "主题");
    this.themeSelect = this.makeThemeSelect();
    themeLabel.append(this.themeSelect);
    const wakeLabel = el("label", "vt-settings-row");
    this.wakeLockCheckbox = el("input");
    this.wakeLockCheckbox.type = "checkbox";
    this.wakeLockCheckbox.addEventListener("change", () =>
      this.setKeepAwake(this.wakeLockCheckbox.checked),
    );
    wakeLabel.append(
      this.wakeLockCheckbox,
      document.createTextNode("保持屏幕常亮"),
    );
    const accessible = el("label", "vt-settings-row"),
      check = el("input");
    check.type = "checkbox";
    this.screenReaderCheckbox = check;
    check.addEventListener("change", () =>
      this.applyScreenReaderPreference(check.checked),
    );
    accessible.append(check, document.createTextNode("屏幕阅读器模式"));
    this.detachButton = action("分离标签", () => {
      const record = this.focused();
      if (record) this.store.detach(record.id);
      details.open = false;
    });
    this.detachButton.title = "只关闭当前标签，保留远端会话";
    panel.append(sizes, themeLabel, wakeLabel, accessible, this.detachButton);
    details.append(summary, panel);
    return details;
  }

  dismissConfirm(restoreFocus = false) {
    this.confirm.hidden = true;
    this.confirmSessionId = null;
    const trigger = this.confirmTrigger;
    this.confirmTrigger = null;
    this.confirmDismissesTab = false;
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }

  confirmClose(record, trigger, dismiss = false) {
    if (!record || record.closing || (dismiss && record.tabLocked)) return;
    if (this.store.isEnded(record)) {
      if (dismiss) this.store.dismissEnded(record.id);
      return;
    }
    this.closeQuickConnect();
    this.confirmSessionId = record.id;
    this.confirmTrigger = trigger;
    this.confirmDismissesTab = dismiss;
    const title = el("span", "", `结束“${record.label}”的远端会话？`);
    title.id = "vt-close-confirm-title";
    const cancel = action("取消", () => this.dismissConfirm(true));
    const end = action(
      "结束会话",
      () => {
        if (this.confirmSessionId !== record.id) return;
        this.dismissConfirm(true);
        this.run(async () => {
          try {
            await this.store.close(record.id, { dismiss });
          } catch (error) {
            if (record.state !== "close_unknown") throw error;
          }
          if (record.state === "close_unknown")
            throw new Error(`“${record.label}”的结束结果待确认，标签已保留。`);
        });
      },
      "vt-danger",
    );
    this.confirm.replaceChildren(title, cancel, end);
    this.confirm.hidden = false;
    cancel.focus();
  }

  syncFullscreenThemeColor(forceReset = false) {
    const active =
      !forceReset &&
      !!this.visible &&
      !this.disposed &&
      (globalThis.document?.fullscreenElement === this.root ||
        this.root.classList.contains("vt-expanded"));
    if (this.appliedExpandedTheme === active) return;
    this.appliedExpandedTheme = active;
    const appearance =
      globalThis.window?.CodePierAppearance ?? globalThis.CodePierAppearance;
    appearance?.syncThemeColor?.(active ? "#171716" : null);
  }

  syncFullscreenLabel() {
    const active =
      globalThis.document?.fullscreenElement === this.root ||
      this.root.classList.contains("vt-expanded");
    this.fullscreenButton.textContent = active ? "退出全屏" : "全屏";
    if (this.moreFullscreenButton)
      this.moreFullscreenButton.textContent = active ? "退出全屏" : "全屏";
    this.syncFullscreenThemeColor();
  }

  async toggleFullscreen() {
    try {
      if (document.fullscreenElement === this.root)
        await document.exitFullscreen();
      else if (this.root.classList.contains("vt-expanded")) {
        this.root.classList.remove("vt-expanded");
        this.syncFullscreenLabel();
        this.fitViewport();
      } else if (this.root.requestFullscreen)
        await this.root.requestFullscreen();
      else {
        this.root.classList.add("vt-expanded");
        this.syncFullscreenLabel();
        this.fitViewport();
      }
    } catch {
      this.root.classList.toggle("vt-expanded");
      this.syncFullscreenLabel();
      this.fitViewport();
    }
  }

  fitViewport() {
    if (!this.visible || this.disposed || this.resizeFrame) return;
    this.resizeFrame = requestAnimationFrame(() => {
      this.resizeFrame = null;
      if (!this.visible || this.disposed) return;
      const narrow = window.matchMedia(MOBILE_LAYOUT).matches;
      if (this.narrow !== narrow) this.render();
      const expanded =
        document.fullscreenElement === this.root ||
        this.root.classList.contains("vt-expanded");
      const dock = expanded ? null : document.querySelector(".mobile-dock");
      let dockRect = null;
      if (dock) {
        const style = getComputedStyle(dock);
        if (style.display !== "none" && style.visibility !== "hidden")
          dockRect = dock.getBoundingClientRect();
      }
      const focused = document.activeElement;
      this.viewportState = computeTerminalViewport(
        {
          innerHeight: window.innerHeight,
          innerWidth: window.innerWidth,
          visualViewport: globalThis.visualViewport,
          workspaceRect: this.root.getBoundingClientRect(),
          dockRect,
          expanded,
          editableFocused: !!(
            focused &&
            this.root.contains(focused) &&
            focused.matches(EDITABLE)
          ),
          keyboardEligible:
            narrow || window.matchMedia("(pointer: coarse)").matches,
          orientationKey:
            window.screen?.orientation?.angle ?? window.orientation,
          gap: expanded ? 0 : 12,
        },
        this.viewportState,
      );
      this.root.classList.toggle(
        "vt-keyboard-open",
        this.viewportState.keyboardOpen,
      );
      const availableHeight = `${this.viewportState.availableHeight}px`;
      if (
        this.root.style.getPropertyValue("--vt-available-height") !==
        availableHeight
      )
        this.root.style.setProperty("--vt-available-height", availableHeight);
      const fitOpts = {
        keyboardTransition: !!this.viewportState.keyboardTransition,
      };
      this.store.active?.view.syncSoftKeyboardOpen?.(
        this.viewportState.keyboardOpen,
      );
      this.store.secondary?.view.syncSoftKeyboardOpen?.(
        this.viewportState.keyboardOpen,
      );
      if (this.viewportState.zoomed) return;
      this.store.active?.view.fit(fitOpts);
      this.store.secondary?.view.fit(fitOpts);
    });
  }

  setupGutter() {
    const set = (value) => {
      this.ratio = Math.max(25, Math.min(75, value));
      this.deck.style.setProperty("--vt-split", `${this.ratio}%`);
      this.gutter.setAttribute("aria-valuenow", String(Math.round(this.ratio)));
    };
    this.gutter.addEventListener("dblclick", () => {
      set(50);
      this.store.active?.view.fit();
      this.store.secondary?.view.fit();
    });
    this.gutter.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      this.dragging = event.pointerId;
      this.gutter.setPointerCapture(event.pointerId);
    });
    this.gutter.addEventListener("pointermove", (event) => {
      if (this.dragging !== event.pointerId) return;
      const bounds = this.deck.getBoundingClientRect();
      if (bounds.width > 0)
        set(((event.clientX - bounds.left) / bounds.width) * 100);
    });
    const end = (event) => {
      if (this.dragging === event.pointerId) {
        this.dragging = null;
        this.store.active?.view.fit();
        this.store.secondary?.view.fit();
      }
    };
    this.gutter.addEventListener("pointerup", end);
    this.gutter.addEventListener("pointercancel", end);
    this.gutter.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      set((this.ratio || 50) + (event.key === "ArrowRight" ? 5 : -5));
      this.store.active?.view.fit();
      this.store.secondary?.view.fit();
    });
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    if (this.ownsMeasureInitialGrid && this.store)
      this.store.measureInitialGrid = null;
    this.releaseWakeLock();
    this.syncFullscreenThemeColor(true);
    window.removeEventListener("resize", this.viewport);
    window.removeEventListener("orientationchange", this.viewport);
    window.screen?.orientation?.removeEventListener?.("change", this.viewport);
    this.root.removeEventListener("focusin", this.viewport);
    this.root.removeEventListener("focusout", this.viewport);
    globalThis.visualViewport?.removeEventListener?.("resize", this.viewport);
    globalThis.visualViewport?.removeEventListener?.("scroll", this.viewport);
    document.removeEventListener("fullscreenchange", this.fullscreen);
    document.removeEventListener("visibilitychange", this.lifecycleResume);
    window.removeEventListener("pageshow", this.lifecycleResume);
    window.removeEventListener("online", this.lifecycleResume);
    document.removeEventListener("pointerdown", this.outsidePointer);
    document.removeEventListener("keydown", this.escapeHandler);
    if (this.resizeFrame) cancelAnimationFrame(this.resizeFrame);
    this.root.remove();
    this.parking.remove();
    this.tabs.clear();
    this.paneHosts.clear();
  }
}
