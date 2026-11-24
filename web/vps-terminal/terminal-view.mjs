import { binaryInput, isSavedVpsLegacyError, reasonText } from "./protocol.mjs";
import { pasteInChunks, pasteSummary } from "./paste.mjs";
import { DiagnosticsPanel } from "./diagnostics.mjs";
import { TerminalComposer } from "./composer.mjs";
import { TouchScrollController, bindDirectionalKey } from "./touch-scroll.mjs";

const encoder = new TextEncoder();
const utf8FatalDecoder = new TextDecoder("utf-8", { fatal: true });
const TRANSIENT_NOTICES = new Set([
  "SESSION_REDRAW",
  "TEMPORARY_SESSION",
  "SESSION_LIVE_TAIL",
]);
const COMPACT_NOTICE_TEXT = {
  SESSION_REDRAW: "已恢复持久会话 · 断线前滚动历史可能不全",
  TEMPORARY_SESSION: "临时直连会话 · 断线可能中断远端任务",
  SESSION_LIVE_TAIL: "已接续实时输出 · 断线期间历史已跳过",
};
const DOUBLE_TAP_MS = 350;
const NOTICE_AUTO_HIDE_MS = 4000;
const OSC_CLIPBOARD_TTL_MS = 10000;
const OSC_CAPSULE_HIDE_MS = 4000;
const OSC_MAX_BYTES = 65536;
const BLOCKED_EVENTS = [
  "keydown",
  "keypress",
  "keyup",
  "beforeinput",
  "input",
  "compositionstart",
  "compositionupdate",
  "compositionend",
  "paste",
  "drop",
  "mousedown",
  "mouseup",
  "wheel",
  "touchstart",
];
const LIGHT = {
  background: "#ffffff",
  foreground: "#272727",
  cursor: "#6e5712",
  cursorAccent: "#ffffff",
  selectionBackground: "#e8dca9",
  black: "#303030",
  red: "#a13c35",
  green: "#25654b",
  yellow: "#806015",
  blue: "#235c90",
  magenta: "#795194",
  cyan: "#176a73",
  white: "#d6d5d2",
  brightBlack: "#626262",
  brightRed: "#c7463c",
  brightGreen: "#267e55",
  brightYellow: "#9a7319",
  brightBlue: "#357bb7",
  brightMagenta: "#9667b2",
  brightCyan: "#168592",
  brightWhite: "#f5f5f4",
};
const DARK = {
  background: "#171716",
  foreground: "#e9e8e4",
  cursor: "#f3d970",
  cursorAccent: "#171716",
  selectionBackground: "#4b4430",
  black: "#252524",
  red: "#e58a82",
  green: "#9ac7a5",
  yellow: "#e5c76f",
  blue: "#91b9df",
  magenta: "#c3a2d5",
  cyan: "#87c5cb",
  white: "#d7d6d1",
  brightBlack: "#8a8a84",
  brightRed: "#f29d96",
  brightGreen: "#b0d7b6",
  brightYellow: "#f3d970",
  brightBlue: "#b0d1ed",
  brightMagenta: "#d5b8e3",
  brightCyan: "#a2d8dc",
  brightWhite: "#ffffff",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, action, className = "") {
  const node = el("button", `vt-button ${className}`, label);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

function formatEndpoint(session) {
  const user = session?.username ? `${session.username}@` : "";
  if (session?.host) return `${user}${session.host}:${session.port || 22}`;
  return session?.username || "";
}

function decodeBase64Bytes(clean) {
  if (typeof globalThis.atob === "function") {
    const binary = globalThis.atob(clean);
    const out = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
    return out;
  }
  if (typeof globalThis.Buffer === "function")
    return new Uint8Array(globalThis.Buffer.from(clean, "base64"));
  return null;
}

export function parseOsc52Write(raw) {
  if (
    typeof raw !== "string" ||
    !raw ||
    raw.length > 98304 ||
    raw.includes("?")
  )
    return null;
  let body = raw;
  const firstSep = body.indexOf(";");
  if (firstSep < 0) return null;
  if (
    body.slice(0, firstSep) === "52" &&
    body.indexOf(";", firstSep + 1) >= 0
  ) {
    body = body.slice(firstSep + 1);
  }
  const sep = body.indexOf(";");
  if (sep < 0) return null;
  const target = body.slice(0, sep);
  if (!/^[cpqs0-7]*$/i.test(target)) return null;
  const clean = body.slice(sep + 1).replace(/[\r\n\s]+/g, "");
  if (!clean || clean.length % 4 === 1 || !/^[A-Za-z0-9+/]+={0,2}$/.test(clean))
    return null;
  try {
    const bytes = decodeBase64Bytes(clean);
    if (!bytes || bytes.byteLength < 1 || bytes.byteLength > OSC_MAX_BYTES)
      return null;
    const text = utf8FatalDecoder.decode(bytes);
    return text ? { text, bytes: bytes.byteLength } : null;
  } catch {
    return null;
  }
}

export class TerminalView {
  constructor({
    session,
    transport,
    runtime,
    onFocus,
    onState,
    onNewSession,
    onTemporarySession,
    onEdit,
    onTakeover,
    onRefreshState,
    onDiagnostics,
    onSwitchTab,
    now = Date.now,
  }) {
    this.session = session;
    this.transport = transport;
    this.runtime = runtime;
    this.onState = onState;
    this.onNewSession = onNewSession;
    this.onTemporarySession = onTemporarySession;
    this.onEdit = onEdit;
    this.onTakeover = onTakeover;
    this.onRefreshState = onRefreshState;
    this.onSwitchTab = onSwitchTab;
    this.now = now;
    this.disposers = [];
    this.pendingWrites = new Set();
    const narrowDefault =
      !!globalThis.matchMedia?.("(max-width: 700px)").matches;
    this.fontSize = narrowDefault ? 13.5 : 14;
    this.lineHeight = narrowDefault ? 1.18 : 1.2;
    this.ctrlState = "idle";
    this.altState = "idle";
    this.ctrlTapAt = 0;
    this.altTapAt = 0;
    this.altScrolledUp = false;
    this.altScrollOffset = 0;
    this.pendingOscClipboard = null;
    this.foregroundActive = true;
    this.hardwareKeyboardCollapsed = false;
    this.mode = "system";
    this.root = el("section", "vt-pane");
    this.root.setAttribute(
      "aria-label",
      `${session.vps_name || "VPS"} SSH 终端`,
    );
    this.root.addEventListener("pointerdown", () => onFocus?.());
    this.root.addEventListener("focusin", () => onFocus?.());
    this.header = el("div", "vt-pane-heading");
    this.identity = el(
      "span",
      "vt-pane-identity",
      session.vps_name || session.name || "VPS",
    );
    this.endpoint = el("span", "vt-pane-endpoint");
    this.modeBadge = el("span", "vt-pane-pill");
    this.rttBadge = el("span", "vt-pane-meta");
    this.rttBadge.hidden = true;
    this.rttBadge.title = "终端连接往返延迟";
    this.sizeBadge = el("span", "vt-pane-meta");
    this.sizeBadge.hidden = true;
    this.sizeBadge.title = "终端网格尺寸";
    this.status = el("span", "vt-state", "正在连接");
    this.status.setAttribute("role", "status");
    this.status.setAttribute("aria-live", "polite");
    this.header.append(
      this.identity,
      this.endpoint,
      this.modeBadge,
      this.rttBadge,
      this.sizeBadge,
      this.status,
    );
    this.progressBar = el("div", "vt-progress");
    this.progressBar.setAttribute("aria-hidden", "true");
    this.progressBar.hidden = true;
    this.syncSession(session);
    this.searchBar = this.makeSearch();
    this.notice = el("div", "vt-notice");
    this.notice.hidden = true;
    this.notice.setAttribute("role", "status");
    this.statusText = el("span");
    this.notice.append(this.statusText);
    this.actions = el("span", "vt-notice-actions");
    this.notice.append(this.actions);
    this.bindNoticeInteractions();
    this.authBox = el("div", "vt-auth");
    this.authBox.hidden = true;
    this.diagnostics = new DiagnosticsPanel({
      sessionId: session.session_id || session.id,
      load: onDiagnostics,
      onResize: () => this.fit(),
    });
    this.canvas = el("div", "vt-canvas");
    this.canvas.tabIndex = -1;
    this.canvas.setAttribute("aria-label", "SSH 终端输入区");
    this.pasteBox = el("div", "vt-paste");
    this.pasteBox.hidden = true;
    this.bottom = button(
      "回到底部 ↓",
      () => this.scrollToBottom(),
      "vt-to-bottom",
    );
    this.bottom.hidden = true;
    this.oscCapsule = this.makeOscCapsule();
    this.mobileKeysPill = button(
      "⌨ 辅助键",
      () => this.setHardwareKeyboardCollapsed(false),
      "vt-mobile-keys-pill",
    );
    this.mobileKeysPill.hidden = true;
    this.mobileKeysPill.setAttribute("aria-label", "展开终端辅助键栏");
    this.composer = new TerminalComposer({
      onSend: (text, opts) => this.sendComposerText(text, opts),
      onClose: () => this.focus(),
      onToggle: (open) => {
        this.composerButton?.setAttribute("aria-expanded", String(open));
        this.composerButton?.setAttribute("aria-pressed", String(open));
        this.fit();
      },
    });
    this.mobileKeys = this.makeMobileKeys();
    this.setupTouchScroll();
    this.bindHardwareKeyboardDetector();
    const screen = el("div", "vt-screen");
    screen.append(
      this.canvas,
      this.progressBar,
      this.notice,
      this.oscCapsule,
      this.bottom,
      this.mobileKeysPill,
    );
    this.root.append(
      this.header,
      this.searchBar,
      this.authBox,
      this.diagnostics.root,
      this.pasteBox,
      screen,
      this.composer.root,
      this.mobileKeys,
    );
    this.blockUserInput = true;
    // Gate user DOM events without suppressing xterm's DSR/DA replies. Replay
    // policy is separately enforced by transport.canReply for onData/onBinary.
    const gate = (event) => {
      if (
        !this.blockUserInput ||
        event.target.closest?.(".vt-search, .vt-paste, .vt-auth")
      )
        return;
      const plainSelection =
        !this.terminal?.modes.mouseTrackingMode ||
        this.terminal.modes.mouseTrackingMode === "none";
      if (
        ["mousedown", "mouseup", "wheel", "touchstart"].includes(event.type) &&
        plainSelection
      )
        return;
      if (
        event.type === "keydown" &&
        event.code === "KeyC" &&
        (event.metaKey || (event.ctrlKey && event.shiftKey))
      ) {
        this.copy();
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    for (const type of BLOCKED_EVENTS) {
      this.canvas.addEventListener(type, gate, {
        capture: true,
        passive: false,
      });
      this.disposers.push(() =>
        this.canvas.removeEventListener(type, gate, { capture: true }),
      );
    }
    const paste = (event) => {
      if (this.blockUserInput || !event.clipboardData) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      this.requestPaste(event.clipboardData.getData("text/plain"));
    };
    this.canvas.addEventListener("paste", paste, { capture: true });
    this.disposers.push(() =>
      this.canvas.removeEventListener("paste", paste, { capture: true }),
    );
    const mobileModifier = (event) => {
      if (
        this.blockUserInput ||
        event.isComposing ||
        !event.data ||
        event.data.length !== 1 ||
        (!this.ctrlLatched && !this.altLatched)
      )
        return;
      const modified = this.transformWithModifiers(event.data);
      if (modified === null) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      this.consumeOneShotModifiers();
      this.sendRawKey(modified);
    };
    this.canvas.addEventListener("beforeinput", mobileModifier, {
      capture: true,
    });
    this.disposers.push(() =>
      this.canvas.removeEventListener("beforeinput", mobileModifier, {
        capture: true,
      }),
    );
  }

  get ctrlLatched() {
    return this.ctrlState === "latched" || this.ctrlState === "locked";
  }
  set ctrlLatched(value) {
    this.ctrlState = value ? "latched" : "idle";
    this.syncModifierButtons();
  }
  get altLatched() {
    return this.altState === "latched" || this.altState === "locked";
  }
  set altLatched(value) {
    this.altState = value ? "latched" : "idle";
    this.syncModifierButtons();
  }

  haptic() {
    try {
      globalThis.navigator?.vibrate?.(6);
    } catch {
      /* Unsupported platform. */
    }
  }

  setupTouchScroll() {
    this.touchScroll = new TouchScrollController({
      target: this.canvas,
      getTerminal: () => this.terminal,
      getFontSize: () => this.fontSize,
      getLineHeight: () => this.lineHeight,
      canInput: () =>
        !!this.transport?.canInput &&
        !this.blockUserInput &&
        !this.pasteController,
      sendRaw: (seq) => this.transport.sendBytes(encoder.encode(seq)),
      onFontSize: (size, opts) => this.setFontSize(size, opts),
      onPinchEnd: () => this.fit(),
      onAltScrollChange: (up, count) => this.handleAltScroll(up, count),
      now: this.now,
    });
    this.disposers.push(this.touchScroll.bind(), () =>
      this.touchScroll.dispose(),
    );
  }

  handleAltScroll(up, count) {
    if (this.terminal?.buffer?.active?.type !== "alternate") {
      this.syncBottomButton();
      return;
    }
    this.altScrollOffset = Math.max(
      0,
      (this.altScrollOffset || 0) + (up ? count : -count),
    );
    this.altScrolledUp = up ? true : this.altScrollOffset > 0;
    this.syncBottomButton();
  }

  syncBottomButton() {
    if (!this.bottom) return;
    const active = this.terminal?.buffer?.active;
    if (!active) {
      this.bottom.hidden = true;
      return;
    }
    if (active.type === "alternate") {
      this.bottom.hidden = !this.altScrolledUp;
      return;
    }
    this.bottom.hidden = active.viewportY >= active.baseY;
  }

  resetAltScroll() {
    this.altScrollOffset = 0;
    this.altScrolledUp = false;
    this.syncBottomButton();
  }

  scrollToBottom() {
    this.touchScroll?.stopMomentum();
    const active = this.terminal?.buffer?.active;
    if (active?.type === "alternate") {
      if (
        this.altScrolledUp &&
        this.transport?.canInput &&
        !this.blockUserInput
      ) {
        const steps = Math.min(
          64,
          Math.max(12, (this.altScrollOffset || 12) + 6),
        );
        const mouseMode = this.terminal.modes?.mouseTrackingMode;
        const unit =
          mouseMode && mouseMode !== "none"
            ? "\x1b[<65;1;1M"
            : this.terminal.modes?.applicationCursorKeysMode
              ? "\x1bOB"
              : "\x1b[B";
        this.transport.sendBytes(encoder.encode(unit.repeat(steps)));
      }
      this.resetAltScroll();
      return;
    }
    this.terminal?.scrollToBottom();
    this.resetAltScroll();
  }

  bindHardwareKeyboardDetector() {
    const onKeydown = (event) => this.handlePhysicalKeydown(event);
    this.root.addEventListener("keydown", onKeydown);
    this.disposers.push(() =>
      this.root.removeEventListener?.("keydown", onKeydown),
    );
  }

  handlePhysicalKeydown(event) {
    if (!event || event.isComposing || event.keyCode === 229) return;
    if (["Shift", "Control", "Alt", "Meta", "CapsLock"].includes(event.key))
      return;
    const innerH =
      globalThis.window?.innerHeight ?? globalThis.innerHeight ?? 0;
    if (innerH <= 0) return;
    const vvH = globalThis.visualViewport?.height ?? innerH;
    if (innerH - vvH < 60) this.setHardwareKeyboardCollapsed(true);
  }

  setHardwareKeyboardCollapsed(collapsed) {
    const next = !!collapsed;
    if (this.hardwareKeyboardCollapsed === next) return;
    this.hardwareKeyboardCollapsed = next;
    if (this.mobileKeys) this.mobileKeys.hidden = next;
    if (this.mobileKeysPill) this.mobileKeysPill.hidden = !next;
    this.root?.classList?.toggle?.("vt-hw-keyboard", next);
    this.fit();
  }

  syncSoftKeyboardOpen(keyboardOpen) {
    if (keyboardOpen && this.hardwareKeyboardCollapsed)
      this.setHardwareKeyboardCollapsed(false);
  }

  makeOscCapsule() {
    const box = el("div", "vt-osc-capsule");
    box.hidden = true;
    box.setAttribute("role", "status");
    this.oscCopyButton = button(
      "远端已复制 · 写入剪贴板",
      () => this.copyPendingOscClipboard(),
      "vt-primary vt-osc-copy",
    );
    const dismiss = button("×", () => this.hideOscCapsule(), "vt-osc-dismiss");
    dismiss.setAttribute("aria-label", "忽略远端复制提示");
    box.append(this.oscCopyButton, dismiss);
    return box;
  }

  handleOsc52(data) {
    const parsed = parseOsc52Write(data);
    if (!parsed) return true;
    clearTimeout(this.oscExpireTimer);
    clearTimeout(this.oscCapsuleTimer);
    this.pendingOscClipboard = {
      text: parsed.text,
      bytes: parsed.bytes,
      expiresAt: this.now() + OSC_CLIPBOARD_TTL_MS,
    };
    if (this.oscCapsule) this.oscCapsule.hidden = false;
    this.oscCapsuleTimer = setTimeout(
      () => this.hideOscCapsule(),
      OSC_CAPSULE_HIDE_MS,
    );
    this.oscExpireTimer = setTimeout(
      () => this.clearOscClipboard(),
      OSC_CLIPBOARD_TTL_MS,
    );
    return true;
  }

  hideOscCapsule() {
    clearTimeout(this.oscCapsuleTimer);
    this.oscCapsuleTimer = null;
    if (this.oscCapsule) this.oscCapsule.hidden = true;
  }

  clearOscClipboard() {
    clearTimeout(this.oscExpireTimer);
    this.oscExpireTimer = null;
    this.hideOscCapsule();
    if (this.pendingOscClipboard) this.pendingOscClipboard.text = "";
    this.pendingOscClipboard = null;
  }

  copyPendingOscClipboard() {
    const pending = this.pendingOscClipboard;
    if (!pending || !pending.text || this.now() > pending.expiresAt) {
      this.clearOscClipboard();
      return false;
    }
    const text = pending.text;
    this.clearOscClipboard();
    const clipboard = globalThis.navigator?.clipboard;
    if (typeof clipboard?.writeText !== "function") {
      this.showNotice("", "当前浏览器环境不支持剪贴板写入");
      return false;
    }
    Promise.resolve(clipboard.writeText(text)).catch(() => {
      this.showNotice("", "未能写入系统剪贴板，请重试");
    });
    return true;
  }

  bindNoticeInteractions() {
    const pause = () => {
      this.noticePaused = true;
      clearTimeout(this.noticeTimer);
      this.noticeTimer = null;
    };
    const resume = () => {
      this.noticePaused = false;
      if (this.currentNoticeTransient && !this.notice.hidden)
        this.scheduleNoticeDismiss();
    };
    for (const type of ["pointerdown", "pointerenter", "focusin"])
      this.notice.addEventListener(type, pause);
    for (const type of [
      "pointerup",
      "pointercancel",
      "pointerleave",
      "focusout",
    ])
      this.notice.addEventListener(type, resume);
  }

  scheduleNoticeDismiss(delayMs = NOTICE_AUTO_HIDE_MS) {
    clearTimeout(this.noticeTimer);
    this.noticeTimer = null;
    if (!this.currentNoticeTransient || this.noticePaused || this.disposed)
      return;
    this.noticeTimer = setTimeout(() => {
      this.noticeTimer = null;
      if (this.currentNoticeTransient && !this.noticePaused)
        this.dismissNotice(this.currentNoticeCode);
    }, delayMs);
  }

  dismissNotice(code = this.currentNoticeCode) {
    clearTimeout(this.noticeTimer);
    this.noticeTimer = null;
    this.currentNoticeTransient = false;
    if (code && TRANSIENT_NOTICES.has(code)) this.dismissedTransientCode = code;
    this.notice.hidden = true;
    this.actions.replaceChildren();
  }

  ensureTerminal() {
    if (this.terminal || !this.canvas.isConnected || this.disposed) return;
    const { Terminal, FitAddon, SearchAddon, WebLinksAddon } = this.runtime;
    const terminal = (this.terminal = new Terminal({
      allowProposedApi: false,
      cursorBlink: false,
      cursorStyle: "bar",
      convertEol: false,
      fontSize: this.fontSize,
      fontFamily: "ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace",
      lineHeight: this.lineHeight || 1.2,
      scrollback: this.transport.limits.scrollback_lines,
      allowTransparency: false,
      screenReaderMode: this.screenReaderMode || false,
      minimumContrastRatio: 4.5,
      theme: this.theme(),
      macOptionIsMeta: true,
    }));
    this.fitAddon = new FitAddon();
    this.searchAddon = new SearchAddon();
    terminal.loadAddon(this.fitAddon);
    terminal.loadAddon(this.searchAddon);
    terminal.loadAddon(
      new WebLinksAddon((event, uri) => {
        if (!event.isTrusted) return;
        let url;
        try {
          url = new URL(uri);
        } catch {
          return;
        }
        if (["https:", "http:"].includes(url.protocol))
          window.open(url.href, "_blank", "noopener,noreferrer");
      }),
    );
    terminal.open(this.canvas);
    // Preserve the published contract: do not consume or offer remote clipboard data.
    this.disposers.push(terminal.parser.registerOscHandler(52, () => true));
    this.bindInputProvenance(terminal);
    this.disposers.push(
      terminal.onData((data) => {
        const wasUser = !!this.rendererUserInput;
        if (!this.acceptRendererInput()) return;
        if (wasUser && this.altScrolledUp) this.resetAltScroll();
        this.lastPasteAccepted = this.transport.sendBytes(
          encoder.encode(data),
          {
            automatic: true,
          },
        );
      }),
    );
    this.disposers.push(
      terminal.onBinary((data) => {
        if (!this.liveTail)
          this.transport.sendBytes(binaryInput(data), { automatic: true });
      }),
    );
    this.disposers.push(
      terminal.onResize(({ cols, rows }) => {
        this.updateSize(cols, rows);
        const coalesceMs = this.pendingResizeCoalesceMs;
        this.pendingResizeCoalesceMs = 0;
        this.transport.resize(cols, rows, { coalesceMs });
      }),
    );
    this.disposers.push(
      terminal.onScroll(() => {
        this.syncBottomButton();
      }),
    );
    this.disposers.push(
      terminal.onTitleChange((title) => {
        this.identity.title = title.slice(0, 240);
      }),
    );
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown") return true;
      const mac = /Mac|iPhone|iPad/.test(navigator.platform),
        copy =
          event.code === "KeyC" &&
          (mac ? event.metaKey : event.ctrlKey && event.shiftKey);
      if (copy) {
        this.copy();
        event.preventDefault();
        return false;
      }
      if (
        (mac ? event.metaKey : event.ctrlKey && event.shiftKey) &&
        event.code === "KeyF"
      ) {
        this.toggleSearch(true);
        event.preventDefault();
        return false;
      }
      if (event.code === "Escape" && !this.searchBar.hidden) {
        this.toggleSearch(false);
        event.preventDefault();
        return false;
      }
      if (
        (this.ctrlLatched || this.altLatched) &&
        !event.ctrlKey &&
        !event.altKey &&
        event.key.length === 1 &&
        !event.isComposing
      ) {
        const modified = this.transformWithModifiers(event.key);
        this.consumeOneShotModifiers();
        if (modified !== null) {
          this.sendRawKey(modified);
          event.preventDefault();
          return false;
        }
      }
      return !this.blockUserInput;
    });
    this.syncHardwareAcceleration(this.foregroundActive);
    this.resizeObserver = new ResizeObserver(() => this.fit());
    this.resizeObserver.observe(this.canvas);
    this.appearanceObserver = new MutationObserver(() => this.applyTheme());
    this.appearanceObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-appearance"],
    });
    this.applyTheme();
    this.updateSize(terminal.cols, terminal.rows);
    this.fit();
  }

  syncHardwareAcceleration(active = this.foregroundActive) {
    this.foregroundActive = !!active;
    if (!this.terminal || this.disposed) return;
    const coarse = !!globalThis.matchMedia?.("(pointer: coarse)").matches;
    const pageVisible = globalThis.document?.visibilityState !== "hidden";
    if (coarse && (!this.foregroundActive || !pageVisible)) {
      if (this.webgl) {
        this.webgl.dispose();
        this.webgl = null;
      }
      return;
    }
    const WebglAddon = this.runtime?.WebglAddon;
    if (
      !this.webgl &&
      WebglAddon &&
      (!coarse || (this.foregroundActive && pageVisible))
    ) {
      try {
        const webgl = new WebglAddon();
        this.webgl = webgl;
        webgl.onContextLoss?.(() => {
          webgl.dispose();
          if (this.webgl === webgl) this.webgl = null;
        });
        this.terminal.loadAddon(webgl);
      } catch {
        this.webgl = null;
      }
    }
  }

  syncSession(session = this.session) {
    this.session = session;
    const name = session.vps_name || session.name || "VPS";
    this.identity.textContent = name;
    this.root.setAttribute("aria-label", `${name} SSH 终端`);
    const endpoint = formatEndpoint(session);
    this.endpoint.textContent = endpoint;
    this.endpoint.hidden = !endpoint;
    const isTmux = session.mode === "tmux";
    this.modeBadge.textContent = isTmux ? "tmux 持久" : "直连";
    this.modeBadge.dataset.mode = isTmux ? "tmux" : "direct";
  }
  setRtt(rtt) {
    if (!Number.isFinite(rtt) || rtt < 0) return;
    this.rttBadge.textContent = `${Math.round(rtt)} ms`;
    this.rttBadge.hidden = false;
  }
  updateSize(cols, rows) {
    if (
      !Number.isSafeInteger(cols) ||
      !Number.isSafeInteger(rows) ||
      cols < 1 ||
      rows < 1
    )
      return;
    this.sizeBadge.textContent = `${cols}×${rows}`;
    this.sizeBadge.hidden = false;
  }

  theme() {
    return this.mode === "dark" ||
      (this.mode === "system" &&
        document.documentElement.dataset.appearance === "dark")
      ? DARK
      : LIGHT;
  }
  applyTheme() {
    const theme = this.theme();
    if (this.appliedTheme === theme) return;
    this.appliedTheme = theme;
    if (this.terminal) this.terminal.options.theme = theme;
    this.canvas.style.background = theme.background;
    this.canvas.style.setProperty("--vt-background", theme.background);
  }
  setTheme(value) {
    this.mode = ["system", "dark", "light"].includes(value) ? value : "system";
    this.applyTheme();
  }
  setScreenReader(value) {
    const enabled = !!value;
    if (this.screenReaderMode === enabled) return;
    this.screenReaderMode = enabled;
    if (this.terminal) this.terminal.options.screenReaderMode = enabled;
  }
  setFontSize(value, { fit = true } = {}) {
    if (!Number.isFinite(value)) return;
    const quantized = Math.round(value * 2) / 2;
    const size = Math.max(11, Math.min(22, quantized));
    if (this.fontSize === size) {
      if (fit && this.pendingPinchFit) {
        this.pendingPinchFit = false;
        this.fit();
      }
      return;
    }
    this.fontSize = size;
    if (this.terminal) {
      this.terminal.options.fontSize = this.fontSize;
      if (fit) {
        this.pendingPinchFit = false;
        this.fit();
      } else {
        this.pendingPinchFit = true;
      }
    }
  }
  fit({ keyboardTransition = false } = {}) {
    if (keyboardTransition) this.keyboardTransitionUntil = this.now() + 260;
    if (this.fitFrame != null || this.disposed || this.replayingHistory) return;
    const sample = (previous) => {
      this.fitFrame = requestAnimationFrame(() => {
        this.fitFrame = null;
        if (!this.canMeasureCanvas()) return;
        const width = this.canvas.clientWidth;
        const height = this.canvas.clientHeight;
        if (width < 40 || height < 40) return;
        if (
          !previous ||
          previous.width !== width ||
          previous.height !== height
        ) {
          sample({ width, height });
          return;
        }
        this.applyMeasuredFit(width, height);
      });
    };
    sample();
  }

  canMeasureCanvas() {
    return (
      !this.disposed &&
      !this.replayingHistory &&
      !!this.terminal &&
      !!this.canvas.isConnected &&
      Math.abs((globalThis.visualViewport?.scale || 1) - 1) <= 0.05
    );
  }

  applyMeasuredFit(width, height) {
    try {
      const size = this.fitAddon.proposeDimensions();
      if (
        !Number.isSafeInteger(size?.cols) ||
        !Number.isSafeInteger(size?.rows)
      )
        return;
      const limits = this.transport.limits;
      const cols = Math.max(
        limits.min_cols,
        Math.min(limits.max_cols, size.cols),
      );
      const rows = Math.max(
        limits.min_rows,
        Math.min(limits.max_rows, size.rows),
      );
      const mobileEligible = !!globalThis.matchMedia?.(
        "(max-width: 700px), (pointer: coarse)",
      ).matches;
      const prevCanvas = this.lastFittedCanvas;
      const deltaH = prevCanvas ? Math.abs(height - prevCanvas.height) : 0;
      const inKeyboardShift =
        mobileEligible &&
        (deltaH >= 80 || (this.keyboardTransitionUntil || 0) > this.now());
      if (
        mobileEligible &&
        prevCanvas &&
        !inKeyboardShift &&
        prevCanvas.width === width &&
        cols === this.terminal.cols &&
        deltaH > 0 &&
        deltaH < 14
      )
        return;
      this.lastFittedCanvas = { width, height };
      if (cols === this.terminal.cols && rows === this.terminal.rows) return;
      this.pendingResizeCoalesceMs = inKeyboardShift ? 140 : 0;
      this.terminal.resize(cols, rows);
    } catch {
      /* Fonts/renderer not ready yet; ResizeObserver or show will retry. */
    }
  }
  show() {
    this.ensureTerminal();
    this.syncHardwareAcceleration(true);
    this.fit();
  }
  focus() {
    if (!this.disposed) this.terminal?.focus();
  }
  write(bytes) {
    if (this.disposed) return Promise.resolve();
    this.ensureTerminal();
    if (!this.terminal)
      return Promise.reject(new Error("Terminal is not mounted"));
    return new Promise((resolve) => {
      this.pendingWrites.add(resolve);
      this.terminal.write(bytes, () => {
        this.pendingWrites.delete(resolve);
        resolve();
      });
    });
  }
  bindInputProvenance(terminal) {
    // xterm 6.0.0 fires the CoreService user-input event immediately before
    // the matching onData, including keyboard, paste and composition. Parser
    // replies never fire it. If a future runtime removes this seam, the tail
    // mode fails closed for renderer data; explicit auxiliary keys still work.
    const core = terminal._core?.coreService;
    if (typeof core?.onUserInput !== "function") return;
    this.disposers.push(
      core.onUserInput(() => {
        this.rendererUserInput = true;
        queueMicrotask(() => {
          this.rendererUserInput = false;
        });
      }),
    );
  }

  acceptRendererInput() {
    const fromUser = !!this.rendererUserInput;
    this.rendererUserInput = false;
    return !this.liveTail || fromUser;
  }

  attached(message) {
    this.ensureTerminal();
    this.syncSession(this.session);
    this.dismissedTransientCode = null;
    this.resetAltScroll();
    this.replayingHistory = message.resume_strategy === "history_replay";
    this.liveTail =
      message.history_recovery === "live_tail" ||
      (!!this.liveTail && message.preserve);
    this.historyRedrawn =
      message.resume_strategy === "redraw" ||
      message.history_incomplete === true;
    if (!message.preserve) this.terminal?.reset();
    if (
      Number.isSafeInteger(message.cols) &&
      Number.isSafeInteger(message.rows)
    ) {
      if (this.replayingHistory)
        this.terminal?.resize(message.cols, message.rows);
      this.updateSize(message.cols, message.rows);
    }
    this.blockUserInput = true;
    this.clearAuth();
    this.fit();
  }
  stateChanged(details) {
    this.syncSession(this.session);
    if (details.state === "ready" && this.replayingHistory) {
      this.replayingHistory = false;
      this.fit();
    }
    this.blockUserInput = details.state !== "ready" || !!this.pasteController;
    this.keyboardButton.disabled = details.state !== "ready";
    const labels = {
      idle: "待连接",
      creating: "创建会话",
      connecting: "正在连接",
      authenticating: "等待认证",
      restoring: "恢复画面",
      reconnecting: "重新连接",
      ready: "已连接",
      detached: "已分离",
      blocked: "连接已暂停",
      disconnected: "未连接",
      closing: "正在结束",
      close_unknown: "结果待确认",
      exited: "已结束",
      closed: "已结束",
      failed: "连接失败",
      revoked: "权限已撤销",
    };
    const nextStatus = labels[details.state] || "正在连接";
    if (this.status.textContent !== nextStatus)
      this.status.textContent = nextStatus;
    this.root.dataset.state = details.state;
    this.progressBar.hidden = ![
      "creating",
      "connecting",
      "authenticating",
      "restoring",
      "reconnecting",
    ].includes(details.state);
    if (
      ["disconnected", "exited", "closed", "failed", "revoked"].includes(
        details.state,
      )
    )
      this.rttBadge.hidden = true;
    this.status.title = [
      formatEndpoint(this.session),
      this.session.device_name && `经由 ${this.session.device_name}`,
      this.session.mode === "tmux"
        ? "会话可在 SSH 断开后恢复"
        : "临时 SSH，关闭网页或断线后可能结束远端任务",
    ]
      .filter(Boolean)
      .join(" · ");
    if (
      details.reason_code ||
      details.message ||
      details.state === "reconnecting"
    )
      this.showNotice(
        details.reason_code,
        details.message,
        details.state,
        details,
      );
    else if (details.state === "ready") {
      const candidate = details.input_uncertain
        ? "INPUT_UNCERTAIN"
        : this.liveTail
          ? "SESSION_LIVE_TAIL"
          : this.historyRedrawn
            ? "SESSION_REDRAW"
            : this.session.mode === "direct"
              ? "TEMPORARY_SESSION"
              : null;
      if (candidate && candidate !== this.dismissedTransientCode) {
        this.showNotice(candidate, "", "ready", details);
      } else {
        this.dismissNotice();
        delete this.notice.dataset?.severity;
      }
    }
    if (
      [
        "blocked",
        "disconnected",
        "exited",
        "closed",
        "failed",
        "revoked",
      ].includes(details.state)
    ) {
      this.pasteController?.abort();
      this.clearAuth();
    }
    this.onState?.(details);
  }
  showNotice(code, fallback, state, details = {}) {
    clearTimeout(this.noticeTimer);
    this.noticeTimer = null;
    this.currentNoticeCode = code || "";
    const isTransient = !!code && TRANSIENT_NOTICES.has(code);
    this.currentNoticeTransient = isTransient;
    this.notice.hidden = false;
    this.notice.setAttribute("data-transient", String(isTransient));
    this.applyNoticeSeverity(code);
    this.statusText.textContent = this.formatNoticeText(
      code,
      fallback,
      state,
      details,
      isTransient,
    );
    this.actions.replaceChildren();
    if (isTransient) {
      this.renderTransientNoticeActions(code, state);
      return;
    }
    this.renderBlockingNoticeActions(code, state);
  }

  applyNoticeSeverity(code) {
    if (code === "AUTH_REVOKED") {
      this.notice.setAttribute("data-severity", "danger");
      if (this.notice.dataset) this.notice.dataset.severity = "danger";
      return;
    }
    this.notice.removeAttribute?.("data-severity");
    if (this.notice.dataset) delete this.notice.dataset.severity;
  }

  formatNoticeText(code, fallback, state, details, isTransient) {
    const narrow = !!globalThis.matchMedia?.("(max-width: 700px)").matches;
    const baseText =
      isTransient && narrow && COMPACT_NOTICE_TEXT[code]
        ? COMPACT_NOTICE_TEXT[code]
        : reasonText(
            code,
            fallback ||
              (state === "reconnecting" ? "网络连接中断，正在恢复连接" : ""),
          );
    const retryText =
      state === "reconnecting" && details?.retry_attempt
        ? `（第 ${details.retry_attempt}/${details.max_retries || 8} 次重试）`
        : "";
    const outputText =
      details.output_complete === false && code !== "OUTPUT_INCOMPLETE"
        ? "；部分输出未完整收到"
        : "";
    return `${baseText}${retryText}${outputText}`;
  }

  renderTransientNoticeActions(code, state) {
    const closeBtn = button(
      "×",
      () => this.dismissNotice(code),
      "vt-notice-close",
    );
    closeBtn.setAttribute("aria-label", "关闭提示");
    this.actions.append(closeBtn);
    const isReady =
      state === "ready" ||
      this.transport?.state === "ready" ||
      this.root?.dataset?.state === "ready";
    if (isReady) this.scheduleNoticeDismiss();
  }

  renderBlockingNoticeActions(code, state) {
    if (state === "reconnecting") {
      this.actions.append(
        button(
          "立即重连",
          () => this.transport.connect({ manual: true }),
          "vt-primary",
        ),
      );
    } else if (
      ["WRITER_CONFLICT", "WRITER_REPLACED", "STALE_WRITER"].includes(code)
    )
      this.actions.append(button("接管", () => this.onTakeover?.()));
    else if (["SSH_AUTH_FAILED", "TARGET_REVISION_CHANGED"].includes(code))
      this.actions.append(button("编辑连接", () => this.onEdit?.()));
    else if (code === "PERSISTENCE_UNAVAILABLE")
      this.actions.append(
        button("使用临时连接（不保留任务）", () => this.onTemporarySession?.()),
      );
    else if (code === "HISTORY_GAP")
      this.actions.append(
        button(
          "继续原会话",
          () => this.transport.resumeAfterHistoryGap(),
          "vt-primary",
        ),
      );
    else if (
      [
        "DETACH_EXPIRED",
        "REMOTE_SESSION_MISSING",
        "SESSION_NOT_FOUND",
        "SESSION_CLOSED",
        "SESSION_ENDED",
        "SESSION_NOT_RECOVERABLE",
        "VPS_AUTHORIZATION_CHANGED",
        "VPS_CONFIGURATION_CHANGED",
      ].includes(code)
    )
      this.actions.append(button("新建终端", () => this.onNewSession?.()));
    else if (code === "CLOSE_UNKNOWN")
      this.actions.append(button("检查状态", () => this.onRefreshState?.()));
    else if (this.canOfferManualReconnect(code, state))
      this.actions.append(
        button("重新连接", () => this.transport.connect({ manual: true })),
      );
    if (code === "INPUT_UNCERTAIN")
      this.actions.append(
        button("知道了", () => {
          this.notice.hidden = true;
          this.transport.inputUncertain = false;
        }),
      );
  }

  canOfferManualReconnect(code, state) {
    return (
      ["blocked", "disconnected", "failed"].includes(state) &&
      !isSavedVpsLegacyError(code) &&
      ![
        "AUTH_REVOKED",
        "FORBIDDEN",
        "AUTH_REQUIRED",
        "LOGIN_REQUIRED",
        "SESSION_EXPIRED",
        "UNAUTHORIZED",
        "FEATURE_DISABLED",
        "AGENT_UPGRADE_REQUIRED",
        "PROTOCOL_MISMATCH",
        "HISTORY_RECOVERY_UNSUPPORTED",
      ].includes(code)
    );
  }

  makeSearch() {
    const bar = el("div", "vt-search");
    bar.hidden = true;
    this.searchInput = el("input");
    this.searchInput.type = "search";
    this.searchInput.placeholder = "查找当前终端";
    this.searchInput.setAttribute("aria-label", "查找当前终端内容");
    const find = (backward) => {
      const found = this.searchAddon?.[backward ? "findPrevious" : "findNext"](
        this.searchInput.value,
        { incremental: false },
      );
      this.searchResult.textContent = found
        ? ""
        : this.searchInput.value
          ? "没有匹配"
          : "";
    };
    this.searchInput.addEventListener("keydown", (event) => {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Enter") {
        event.preventDefault();
        find(event.shiftKey);
      }
      if (event.key === "Escape") {
        this.toggleSearch(false);
        event.preventDefault();
      }
    });
    this.searchResult = el("span", "vt-search-result");
    this.searchResult.setAttribute("role", "status");
    bar.append(
      this.searchInput,
      this.searchResult,
      button("↑", () => find(true)),
      button("↓", () => find(false)),
      button("关闭", () => this.toggleSearch(false)),
    );
    return bar;
  }
  toggleSearch(force = this.searchBar.hidden) {
    this.searchBar.hidden = !force;
    if (force) this.searchInput.focus();
    else {
      this.searchAddon?.clearDecorations();
      this.focus();
    }
    this.fit();
  }
  toggleDiagnostics() {
    this.diagnostics.toggle();
  }
  async copy() {
    const text = this.terminal?.getSelection();
    if (!text) {
      this.showNotice("", "先选择要复制的内容");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      this.showNotice("", "请使用系统复制快捷键");
    }
  }
  async clipboardPaste() {
    if (!this.transport.canInput) return;
    try {
      this.requestPaste(await navigator.clipboard.readText());
    } catch {
      this.showNotice("", "请在终端中使用系统粘贴快捷键");
    }
  }
  requestPaste(text) {
    if (!text || !this.transport.canInput || this.pasteController) return;
    const summary = pasteSummary(text, this.transport.limits.paste_bytes);
    if (summary.tooLarge) {
      this.showNotice(
        "",
        `内容超过 ${Math.floor(this.transport.limits.paste_bytes / 1024)} KiB，请使用文件传输`,
      );
      return;
    }
    if (!summary.preview) {
      this.runPaste(text);
      return;
    }
    this.pasteBox.replaceChildren();
    this.pasteBox.hidden = false;
    const describe = (value) => {
      const current = pasteSummary(value, this.transport.limits.paste_bytes);
      if (current.tooLarge) return "内容过大，请缩短后粘贴";
      const multiline = /[\r\n]/.test(value);
      return multiline
        ? `粘贴 ${current.lines} 行（含换行，粘贴后可能立即执行） · ${current.bytes.toLocaleString()} 字节`
        : `粘贴 ${current.lines} 行 · ${current.bytes.toLocaleString()} 字节`;
    };
    const headingRow = el("div", "vt-paste-header");
    const heading = el("div", "vt-paste-heading", describe(text));
    const shortcutHint = el(
      "span",
      "vt-muted",
      "快捷键 Ctrl+Enter 确认 · Esc 取消",
    );
    headingRow.append(heading, shortcutHint);
    const input = el("textarea");
    input.value = text;
    input.rows = Math.min(6, Math.max(3, summary.lines));
    input.setAttribute("aria-label", "粘贴前查看和编辑内容");
    input.spellcheck = false;
    input.autocapitalize = "off";
    input.autocomplete = "off";
    const controls = el("div", "vt-paste-actions");
    const clear = () => {
      input.value = "";
      this.pasteBox.replaceChildren();
      this.pasteBox.hidden = true;
      this.focus();
    };
    const confirmPaste = () => {
      const edited = input.value;
      if (!edited) {
        clear();
        return;
      }
      if (pasteSummary(edited, this.transport.limits.paste_bytes).tooLarge) {
        heading.textContent = "内容过大，请缩短后粘贴";
        return;
      }
      clear();
      this.runPaste(edited);
    };
    input.addEventListener("input", () => {
      heading.textContent = describe(input.value);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        clear();
      } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        event.stopPropagation();
        confirmPaste();
      }
    });
    controls.append(
      button("取消", clear),
      button("粘贴", confirmPaste, "vt-primary"),
    );
    this.pasteBox.append(headingRow, input, controls);
    input.focus();
  }
  async sendComposerText(text, { execute = true } = {}) {
    if (!text || !this.transport?.canInput || this.pasteController)
      return false;
    const summary = pasteSummary(text, this.transport.limits.paste_bytes);
    if (summary.tooLarge) {
      this.showNotice(
        "",
        `内容超过 ${Math.floor(this.transport.limits.paste_bytes / 1024)} KiB，请缩短后发送`,
      );
      return false;
    }
    return this.runPaste(text, {
      focusAfter: false,
      appendReturn: execute,
    });
  }

  async runPaste(text, { focusAfter = true, appendReturn = false } = {}) {
    if (!this.transport.canInput || this.pasteController) return false;
    const controller = (this.pasteController = new AbortController());
    this.blockUserInput = true;
    const maxChunkBytes = Math.max(
      128,
      Math.min(
        4096,
        this.transport.limits.input_frame_bytes - 16,
        this.transport.limits.pending_input_bytes - 32,
      ),
    );
    const showProgress = encoder.encode(text).byteLength > maxChunkBytes;
    if (showProgress) {
      this.notice.hidden = false;
      this.statusText.textContent = "正在粘贴";
      this.actions.replaceChildren(
        button("停止粘贴", () => controller.abort()),
      );
    }
    let completed = false;
    try {
      const result = await pasteInChunks(text, {
        signal: controller.signal,
        writable: (bytes) =>
          this.transport.pendingInputBytes + bytes <
            this.transport.limits.pending_input_bytes &&
          (this.transport.socket?.bufferedAmount || 0) <
            this.transport.limits.pending_input_bytes,
        connected: () => this.transport.canInput && !this.disposed,
        paste: (chunk) => {
          if (typeof this.terminal?.paste === "function") {
            this.lastPasteAccepted = false;
            this.terminal.paste(chunk);
            return this.lastPasteAccepted;
          }
          const bracketed = this.terminal?.modes?.bracketedPasteMode !== false;
          const wrapped = bracketed ? `\x1b[200~${chunk}\x1b[201~` : chunk;
          return this.transport.sendBytes(encoder.encode(wrapped));
        },
        onProgress: showProgress
          ? (sent, total) => {
              this.statusText.textContent = `正在粘贴 ${Math.round((sent / total) * 100)}%`;
            }
          : undefined,
        maxChunkBytes,
      });
      if (result.cancelled)
        this.showNotice("", "已停止粘贴，已发送的内容保留在远端");
      else if (this.transport.inputUncertain)
        this.showNotice("INPUT_UNCERTAIN");
      else {
        if (showProgress) this.notice.hidden = true;
        if (appendReturn && this.transport.canInput)
          this.transport.sendBytes(encoder.encode("\r"));
        this.resetAltScroll();
        completed = true;
      }
    } catch (error) {
      this.showNotice("", error.message);
    } finally {
      if (this.pasteController === controller) this.pasteController = null;
      this.blockUserInput = !this.transport.canInput;
      if (focusAfter) this.focus();
    }
    return completed;
  }

  toggleModifier(kind) {
    const nowFn = this.now || Date.now;
    const t = nowFn();
    const stateProp = kind === "ctrl" ? "ctrlState" : "altState";
    const tapProp = kind === "ctrl" ? "ctrlTapAt" : "altTapAt";
    const current = this[stateProp] || "idle";
    const lastTap = this[tapProp] || 0;
    if (current === "idle") {
      this[stateProp] = "latched";
      this[tapProp] = t;
    } else if (current === "latched" && t - lastTap <= DOUBLE_TAP_MS) {
      this[stateProp] = "locked";
      this[tapProp] = 0;
    } else {
      this[stateProp] = "idle";
      this[tapProp] = 0;
    }
    this.haptic?.();
    this.syncModifierButtons();
  }

  syncModifierButtons() {
    for (const [btn, state] of [
      [this.ctrlButton, this.ctrlState || "idle"],
      [this.altButton, this.altState || "idle"],
    ]) {
      if (!btn) continue;
      const active = state !== "idle";
      btn.setAttribute("aria-pressed", String(active));
      btn.setAttribute("data-lock-state", state);
    }
  }

  transformWithModifiers(char) {
    let out = char;
    if (this.ctrlLatched) {
      const code = out.toUpperCase().charCodeAt(0);
      if (code < 64 || code > 95) return null;
      out = String.fromCharCode(code - 64);
    }
    if (this.altLatched) out = "\x1b" + out;
    return out;
  }

  consumeOneShotModifiers() {
    if (this.ctrlState === "latched") this.ctrlState = "idle";
    if (this.altState === "latched") this.altState = "idle";
    this.syncModifierButtons();
  }

  sendDirection(direction) {
    const pair = {
      up: ["\x1b[A", "\x1bOA"],
      down: ["\x1b[B", "\x1bOB"],
      right: ["\x1b[C", "\x1bOC"],
      left: ["\x1b[D", "\x1bOD"],
    }[direction];
    if (!pair) return;
    const appMode = !!this.terminal?.modes?.applicationCursorKeysMode;
    this.sendKey(appMode ? pair[1] : pair[0]);
  }

  makeMobileKeys() {
    this.disposers ||= [];
    this.ctrlState ||= "idle";
    this.altState ||= "idle";
    const bar = el("div", "vt-mobile-keys");
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "终端辅助键");
    this.appendPrimaryMobileKeys(bar);
    this.extraKeysRow = this.buildExtraMobileKeys();
    this.moreKeysButton = button(
      "更多",
      () => {
        const open = this.extraKeysRow.hidden;
        this.extraKeysRow.hidden = !open;
        this.moreKeysButton.setAttribute("aria-expanded", String(open));
        this.moreKeysButton.setAttribute("aria-pressed", String(open));
        this.fit?.();
      },
      "vt-key vt-more-keys",
    );
    this.moreKeysButton.setAttribute("aria-expanded", "false");
    this.bindKeyPressFeedback(this.moreKeysButton);
    bar.append(this.moreKeysButton, this.extraKeysRow);
    this.bindKeyBarTabSwipe(bar);
    return bar;
  }

  bindKeyPressFeedback(node) {
    node.addEventListener("pointerdown", (event) => {
      event.preventDefault?.();
      this.haptic?.();
    });
  }

  appendPrimaryMobileKeys(bar) {
    this.composerButton = button(
      "速写",
      () => this.composer?.toggle(),
      "vt-composer-toggle",
    );
    this.composerButton.setAttribute("aria-expanded", "false");
    this.composerButton.setAttribute("aria-label", "展开命令速写板");
    this.bindKeyPressFeedback(this.composerButton);

    this.keyboardButton = button(
      "键盘",
      () => this.toggleSoftKeyboard(),
      "vt-open-keyboard",
    );
    this.keyboardButton.disabled = true;
    this.keyboardButton.setAttribute("aria-label", "切换终端软键盘");
    bar.append(this.composerButton, this.keyboardButton);

    for (const [label, value] of [
      ["Esc", "\x1b"],
      ["Tab", "\t"],
    ]) {
      const key = button(label, () => this.sendKey(value), "vt-key");
      this.bindKeyPressFeedback(key);
      bar.append(key);
    }

    this.ctrlButton = button(
      "Ctrl",
      () => this.toggleModifier("ctrl"),
      "vt-key vt-modifier",
    );
    this.altButton = button(
      "Alt",
      () => this.toggleModifier("alt"),
      "vt-key vt-modifier",
    );
    this.syncModifierButtons();
    for (const mod of [this.ctrlButton, this.altButton])
      mod.addEventListener("pointerdown", (event) => event.preventDefault?.());
    bar.append(this.ctrlButton, this.altButton);

    for (const [label, value] of [
      ["Ctrl-C", "\x03"],
      ["Ctrl-O", "\x0f"],
    ]) {
      const key = button(label, () => this.sendKey(value), "vt-key");
      this.bindKeyPressFeedback(key);
      bar.append(key);
    }

    for (const [label, dir] of [
      ["←", "left"],
      ["↓", "down"],
      ["↑", "up"],
      ["→", "right"],
    ]) {
      const key = el("button", "vt-button vt-key vt-arrow", label);
      key.type = "button";
      key.setAttribute("data-arrow", dir);
      this.disposers.push(
        bindDirectionalKey(key, dir, {
          sendDirection: (resolved) => this.sendDirection(resolved),
          onHaptic: () => this.haptic?.(),
        }),
      );
      bar.append(key);
    }
  }

  buildExtraMobileKeys() {
    const extra = el("div", "vt-mobile-keys-extra");
    extra.hidden = true;
    for (const [label, value] of [
      ["/", "/"],
      ["-", "-"],
      ["~", "~"],
      ["|", "|"],
      ["Ctrl-D", "\x04"],
      ["Ctrl-L", "\x0c"],
      ["Ctrl-Z", "\x1a"],
      ["Ctrl-B", "\x02"],
      ["PgUp", "\x1b[5~"],
      ["PgDn", "\x1b[6~"],
    ]) {
      const key = button(label, () => this.sendKey(value), "vt-key");
      this.bindKeyPressFeedback(key);
      extra.append(key);
    }
    const pasteKey = button("粘贴", () => this.clipboardPaste(), "vt-key");
    this.bindKeyPressFeedback(pasteKey);
    extra.append(pasteKey);
    return extra;
  }

  toggleSoftKeyboard() {
    const active = globalThis.document?.activeElement;
    if (
      active &&
      this.root?.contains?.(active) &&
      active !== this.keyboardButton
    ) {
      active.blur?.();
      return;
    }
    if (this.transport?.canInput && !this.blockUserInput) this.focus();
  }

  bindKeyBarTabSwipe(bar) {
    let swipe = null;
    bar.addEventListener("touchstart", (event) => {
      const touches = event.touches || [];
      if (touches.length !== 1 || event.target?.closest?.(".vt-arrow")) {
        swipe = null;
        return;
      }
      swipe = {
        x: touches[0].clientX || 0,
        y: touches[0].clientY || 0,
        t: (this.now || Date.now)(),
      };
    });
    bar.addEventListener("touchend", (event) => {
      if (!swipe) return;
      const touch = event.changedTouches?.[0];
      const start = swipe;
      swipe = null;
      if (!touch) return;
      const dx = (touch.clientX || 0) - start.x;
      const dy = (touch.clientY || 0) - start.y;
      const dt = (this.now || Date.now)() - start.t;
      if (Math.abs(dx) >= 48 && Math.abs(dx) > 2 * Math.abs(dy) && dt <= 500) {
        this.haptic?.();
        this.onSwitchTab?.(dx < 0 ? 1 : -1);
      }
    });
  }

  clearModifiers() {
    this.ctrlState = "idle";
    this.altState = "idle";
    this.ctrlTapAt = 0;
    this.altTapAt = 0;
    this.syncModifierButtons();
  }

  sendRawKey(value) {
    if (
      this.blockUserInput ||
      !this.transport?.canInput ||
      this.pasteController
    )
      return false;
    this.resetAltScroll?.();
    return this.transport.sendBytes(encoder.encode(value));
  }

  sendKey(value) {
    if (
      this.blockUserInput ||
      !this.transport?.canInput ||
      this.pasteController
    )
      return;
    let payload = value;
    if (
      typeof value === "string" &&
      value.length === 1 &&
      value.charCodeAt(0) >= 32 &&
      (this.ctrlLatched || this.altLatched)
    ) {
      const transformed = this.transformWithModifiers(value);
      if (transformed !== null) payload = transformed;
    }
    this.consumeOneShotModifiers();
    this.sendRawKey(payload);
  }

  authChallenge(message) {
    this.clearAuth();
    if (
      !Array.isArray(message.prompts) ||
      message.prompts.length > 8 ||
      !message.challenge_id
    )
      return;
    this.authBox.hidden = false;
    const totalSeconds = Math.min(
      180,
      Math.max(1, Math.floor(message.deadline_seconds || 60)),
    );
    let remaining = totalSeconds;
    const form = el("form", "vt-auth-form");
    form.autocomplete = "off";
    const header = el("div", "vt-auth-header");
    const countdown = el("span", "vt-auth-countdown", `剩余 ${remaining} 秒`);
    header.append(el("strong", "", "SSH 需要进一步认证"), countdown);
    form.append(header);
    const inputs = [];
    for (const prompt of message.prompts) {
      const label = el("label", "vt-auth-field"),
        text = el("span", "", String(prompt.text || "验证码").slice(0, 240)),
        input = el("input");
      input.type = prompt.echo ? "text" : "password";
      input.autocomplete = "one-time-code";
      input.maxLength = 4096;
      input.autocapitalize = "off";
      input.spellcheck = false;
      input.required = false;
      label.append(text, input);
      form.append(label);
      inputs.push(input);
    }
    const submit = el("button", "vt-button vt-primary", "继续连接");
    submit.type = "submit";
    const cancel = button("取消连接", () => {
      this.clearAuth();
      this.transport.fail("SSH_AUTH_FAILED", { message: "认证已取消" }, true);
    });
    form.append(submit, cancel);
    this.authBox.append(form);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const answers = inputs.map((input) => input.value);
      const sent = this.transport.answerAuth(message.challenge_id, answers);
      inputs.forEach((input) => {
        input.value = "";
      });
      answers.fill("");
      this.clearAuth();
      if (!sent) this.showNotice("", "认证未发送，请重新连接");
    });
    this.authCountdownTimer = setInterval(() => {
      remaining = Math.max(0, remaining - 1);
      countdown.textContent = `剩余 ${remaining} 秒`;
    }, 1000);
    this.authTimer = setTimeout(() => {
      this.clearAuth();
      this.showNotice("", "认证已超时，请重新连接");
    }, totalSeconds * 1000);
    inputs[0]?.focus();
  }
  clearAuth() {
    clearTimeout(this.authTimer);
    clearInterval(this.authCountdownTimer);
    this.authCountdownTimer = null;
    for (const input of this.authBox.querySelectorAll("input"))
      input.value = "";
    this.authBox.replaceChildren();
    this.authBox.hidden = true;
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    clearTimeout(this.noticeTimer);
    this.noticeTimer = null;
    this.clearOscClipboard();
    this.composer?.dispose();
    this.touchScroll?.dispose();
    if (this.webgl) {
      this.webgl.dispose();
      this.webgl = null;
    }
    this.diagnostics.dispose();
    this.pasteController?.abort();
    this.clearAuth();
    if (this.fitFrame != null) cancelAnimationFrame(this.fitFrame);
    this.resizeObserver?.disconnect();
    this.appearanceObserver?.disconnect();
    for (const disposable of this.disposers) {
      if (typeof disposable === "function") disposable();
      else disposable?.dispose();
    }
    this.disposers = [];
    this.terminal?.dispose();
    this.terminal = null;
    for (const resolve of this.pendingWrites) resolve();
    this.pendingWrites.clear();
    for (const textarea of this.pasteBox.querySelectorAll("textarea"))
      textarea.value = "";
    this.root.remove();
  }
}
