import { binaryInput, reasonText } from './protocol.mjs';
import { pasteInChunks, pasteSummary } from './paste.mjs';
import { DiagnosticsPanel } from './diagnostics.mjs';

const encoder = new TextEncoder();
const BLOCKED_EVENTS = [
  'keydown',
  'keypress',
  'keyup',
  'beforeinput',
  'input',
  'compositionstart',
  'compositionupdate',
  'compositionend',
  'paste',
  'drop',
  'mousedown',
  'mouseup',
  'wheel',
  'touchstart',
];
const LIGHT = {
  background: '#ffffff',
  foreground: '#272727',
  cursor: '#6e5712',
  cursorAccent: '#ffffff',
  selectionBackground: '#e8dca9',
  black: '#303030',
  red: '#a13c35',
  green: '#25654b',
  yellow: '#806015',
  blue: '#235c90',
  magenta: '#795194',
  cyan: '#176a73',
  white: '#d6d5d2',
  brightBlack: '#626262',
  brightRed: '#c7463c',
  brightGreen: '#267e55',
  brightYellow: '#9a7319',
  brightBlue: '#357bb7',
  brightMagenta: '#9667b2',
  brightCyan: '#168592',
  brightWhite: '#f5f5f4',
};
const DARK = {
  background: '#171716',
  foreground: '#e9e8e4',
  cursor: '#f3d970',
  cursorAccent: '#171716',
  selectionBackground: '#4b4430',
  black: '#252524',
  red: '#e58a82',
  green: '#9ac7a5',
  yellow: '#e5c76f',
  blue: '#91b9df',
  magenta: '#c3a2d5',
  cyan: '#87c5cb',
  white: '#d7d6d1',
  brightBlack: '#8a8a84',
  brightRed: '#f29d96',
  brightGreen: '#b0d7b6',
  brightYellow: '#f3d970',
  brightBlue: '#b0d1ed',
  brightMagenta: '#d5b8e3',
  brightCyan: '#a2d8dc',
  brightWhite: '#ffffff',
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, action, className = '') {
  const node = el('button', `vt-button ${className}`, label);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

function formatEndpoint(session) {
  const user = session?.username ? `${session.username}@` : '';
  if (session?.host) return `${user}${session.host}:${session.port || 22}`;
  return session?.username || '';
}

export class TerminalView {
  constructor({
    session,
    transport,
    runtime,
    onFocus,
    onState,
    onNewSession,
    onEdit,
    onTakeover,
    onRefreshState,
    onDiagnostics,
  }) {
    this.session = session;
    this.transport = transport;
    this.runtime = runtime;
    this.onState = onState;
    this.onNewSession = onNewSession;
    this.onEdit = onEdit;
    this.onTakeover = onTakeover;
    this.onRefreshState = onRefreshState;
    this.disposers = [];
    this.pendingWrites = new Set();
    this.fontSize = globalThis.matchMedia?.('(max-width: 700px)').matches ? 16 : 14;
    this.mode = 'system';
    this.root = el('section', 'vt-pane');
    this.root.setAttribute('aria-label', `${session.vps_name || 'VPS'} SSH 终端`);
    this.root.addEventListener('pointerdown', () => onFocus?.());
    this.root.addEventListener('focusin', () => onFocus?.());
    this.header = el('div', 'vt-pane-heading');
    this.identity = el('span', 'vt-pane-identity', session.vps_name || session.name || 'VPS');
    this.endpoint = el('span', 'vt-pane-endpoint');
    this.modeBadge = el('span', 'vt-pane-pill');
    this.rttBadge = el('span', 'vt-pane-meta');
    this.rttBadge.hidden = true;
    this.rttBadge.title = '终端连接往返延迟';
    this.sizeBadge = el('span', 'vt-pane-meta');
    this.sizeBadge.hidden = true;
    this.sizeBadge.title = '终端网格尺寸';
    this.status = el('span', 'vt-state', '正在连接');
    this.status.setAttribute('role', 'status');
    this.status.setAttribute('aria-live', 'polite');
    this.header.append(
      this.identity,
      this.endpoint,
      this.modeBadge,
      this.rttBadge,
      this.sizeBadge,
      this.status,
    );
    this.progressBar = el('div', 'vt-progress');
    this.progressBar.setAttribute('aria-hidden', 'true');
    this.syncSession(session);
    this.searchBar = this.makeSearch();
    this.notice = el('div', 'vt-notice');
    this.notice.hidden = true;
    this.notice.setAttribute('role', 'status');
    this.statusText = el('span');
    this.notice.append(this.statusText);
    this.actions = el('span', 'vt-notice-actions');
    this.notice.append(this.actions);
    this.authBox = el('div', 'vt-auth');
    this.authBox.hidden = true;
    this.diagnostics = new DiagnosticsPanel({
      sessionId: session.session_id || session.id,
      load: onDiagnostics,
      onResize: () => this.fit(),
    });
    this.canvas = el('div', 'vt-canvas');
    this.canvas.tabIndex = -1;
    this.canvas.setAttribute('aria-label', 'SSH 终端输入区');
    this.pasteBox = el('div', 'vt-paste');
    this.pasteBox.hidden = true;
    this.bottom = button(
      '回到底部 ↓',
      () => {
        this.terminal?.scrollToBottom();
        this.bottom.hidden = true;
      },
      'vt-to-bottom',
    );
    this.bottom.hidden = true;
    this.mobileKeys = this.makeMobileKeys();
    this.root.append(
      this.header,
      this.progressBar,
      this.searchBar,
      this.notice,
      this.authBox,
      this.diagnostics.root,
      this.pasteBox,
      this.canvas,
      this.bottom,
      this.mobileKeys,
    );
    this.blockUserInput = true;
    // Gate user DOM events without suppressing xterm's DSR/DA replies. Replay
    // policy is separately enforced by transport.canReply for onData/onBinary.
    const gate = (event) => {
      if (!this.blockUserInput || event.target.closest?.('.vt-search, .vt-paste, .vt-auth')) return;
      const plainSelection =
        !this.terminal?.modes.mouseTrackingMode || this.terminal.modes.mouseTrackingMode === 'none';
      if (['mousedown', 'mouseup', 'wheel', 'touchstart'].includes(event.type) && plainSelection)
        return;
      if (
        event.type === 'keydown' &&
        event.code === 'KeyC' &&
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
      this.disposers.push(() => this.canvas.removeEventListener(type, gate, { capture: true }));
    }
    const paste = (event) => {
      if (this.blockUserInput || !event.clipboardData) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      this.requestPaste(event.clipboardData.getData('text/plain'));
    };
    this.canvas.addEventListener('paste', paste, { capture: true });
    this.disposers.push(() => this.canvas.removeEventListener('paste', paste, { capture: true }));
    const mobileModifier = (event) => {
      if (
        this.blockUserInput ||
        event.isComposing ||
        !event.data ||
        event.data.length !== 1 ||
        (!this.ctrlLatched && !this.altLatched)
      )
        return;
      let data = event.data;
      if (this.ctrlLatched) {
        const code = data.toUpperCase().charCodeAt(0);
        if (code < 64 || code > 95) return;
        data = String.fromCharCode(code - 64);
      }
      if (this.altLatched) data = '\x1b' + data;
      event.preventDefault();
      event.stopImmediatePropagation();
      this.clearModifiers();
      this.sendKey(data);
    };
    this.canvas.addEventListener('beforeinput', mobileModifier, {
      capture: true,
    });
    this.disposers.push(() =>
      this.canvas.removeEventListener('beforeinput', mobileModifier, {
        capture: true,
      }),
    );
  }

  ensureTerminal() {
    if (this.terminal || !this.canvas.isConnected || this.disposed) return;
    const { Terminal, FitAddon, SearchAddon, WebLinksAddon, WebglAddon } = this.runtime;
    const terminal = (this.terminal = new Terminal({
      allowProposedApi: false,
      cursorBlink: false,
      cursorStyle: 'bar',
      convertEol: false,
      fontSize: this.fontSize,
      fontFamily: "ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace",
      lineHeight: 1.2,
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
        if (['https:', 'http:'].includes(url.protocol))
          window.open(url.href, '_blank', 'noopener,noreferrer');
      }),
    );
    terminal.open(this.canvas);
    // Remote OSC 52 may not silently read/write the browser clipboard.
    this.disposers.push(terminal.parser.registerOscHandler(52, () => true));
    this.disposers.push(
      terminal.onData((data) => {
        this.lastPasteAccepted = this.transport.sendBytes(encoder.encode(data), {
          automatic: true,
        });
      }),
    );
    this.disposers.push(
      terminal.onBinary((data) => this.transport.sendBytes(binaryInput(data), { automatic: true })),
    );
    this.disposers.push(
      terminal.onResize(({ cols, rows }) => {
        this.updateSize(cols, rows);
        this.transport.resize(cols, rows);
      }),
    );
    this.disposers.push(
      terminal.onScroll(() => {
        this.bottom.hidden = terminal.buffer.active.viewportY >= terminal.buffer.active.baseY;
      }),
    );
    this.disposers.push(
      terminal.onTitleChange((title) => {
        this.identity.title = title.slice(0, 240);
      }),
    );
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== 'keydown') return true;
      const mac = /Mac|iPhone|iPad/.test(navigator.platform),
        copy = event.code === 'KeyC' && (mac ? event.metaKey : event.ctrlKey && event.shiftKey);
      if (copy) {
        this.copy();
        event.preventDefault();
        return false;
      }
      if ((mac ? event.metaKey : event.ctrlKey && event.shiftKey) && event.code === 'KeyF') {
        this.toggleSearch(true);
        event.preventDefault();
        return false;
      }
      if (event.code === 'Escape' && !this.searchBar.hidden) {
        this.toggleSearch(false);
        event.preventDefault();
        return false;
      }
      if (this.ctrlLatched && !event.ctrlKey && event.key.length === 1 && !event.isComposing) {
        const code = event.key.toUpperCase().charCodeAt(0);
        this.clearModifiers();
        if (code >= 64 && code <= 95) {
          this.sendKey(String.fromCharCode(code - 64));
          event.preventDefault();
          return false;
        }
      }
      if (this.altLatched && !event.altKey && event.key.length === 1 && !event.isComposing) {
        this.clearModifiers();
        this.sendKey('\x1b' + event.key);
        event.preventDefault();
        return false;
      }
      return !this.blockUserInput;
    });
    try {
      if (WebglAddon) {
        const webgl = new WebglAddon();
        this.webgl = webgl;
        this.disposers.push(
          webgl.onContextLoss(() => {
            webgl.dispose();
            if (this.webgl === webgl) this.webgl = null;
          }),
        );
        terminal.loadAddon(webgl);
      }
    } catch {
      this.webgl = null;
    }
    this.resizeObserver = new ResizeObserver(() => this.fit());
    this.resizeObserver.observe(this.canvas);
    this.appearanceObserver = new MutationObserver(() => this.applyTheme());
    this.appearanceObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-appearance'],
    });
    this.applyTheme();
    this.updateSize(terminal.cols, terminal.rows);
    this.fit();
  }

  syncSession(session = this.session) {
    this.session = session;
    const name = session.vps_name || session.name || 'VPS';
    this.identity.textContent = name;
    this.root.setAttribute('aria-label', `${name} SSH 终端`);
    const endpoint = formatEndpoint(session);
    this.endpoint.textContent = endpoint;
    this.endpoint.hidden = !endpoint;
    const isTmux = session.mode === 'tmux';
    this.modeBadge.textContent = isTmux ? 'tmux 持久' : '直连';
    this.modeBadge.dataset.mode = isTmux ? 'tmux' : 'direct';
  }
  setRtt(rtt) {
    if (!Number.isFinite(rtt) || rtt < 0) return;
    this.rttBadge.textContent = `${Math.round(rtt)} ms`;
    this.rttBadge.hidden = false;
  }
  updateSize(cols, rows) {
    if (!Number.isSafeInteger(cols) || !Number.isSafeInteger(rows) || cols < 1 || rows < 1) return;
    this.sizeBadge.textContent = `${cols}×${rows}`;
    this.sizeBadge.hidden = false;
  }

  theme() {
    return this.mode === 'dark' ||
      (this.mode === 'system' && document.documentElement.dataset.appearance === 'dark')
      ? DARK
      : LIGHT;
  }
  applyTheme() {
    const theme = this.theme();
    if (this.terminal) this.terminal.options.theme = theme;
    this.canvas.style.background = theme.background;
    this.canvas.style.setProperty('--vt-background', theme.background);
  }
  setTheme(value) {
    this.mode = ['system', 'dark', 'light'].includes(value) ? value : 'system';
    this.applyTheme();
  }
  setScreenReader(value) {
    this.screenReaderMode = !!value;
    if (this.terminal) this.terminal.options.screenReaderMode = !!value;
  }
  setFontSize(value) {
    this.fontSize = Math.max(12, Math.min(24, value));
    if (this.terminal) {
      this.terminal.options.fontSize = this.fontSize;
      this.fit();
    }
  }
  fit() {
    if (this.fitFrame || this.disposed || this.replayingHistory) return;
    this.fitFrame = requestAnimationFrame(() => {
      this.fitFrame = null;
      if (
        this.replayingHistory ||
        !this.terminal ||
        !this.canvas.isConnected ||
        this.canvas.clientWidth < 40 ||
        this.canvas.clientHeight < 40
      )
        return;
      try {
        this.fitAddon.fit();
        this.updateSize(this.terminal.cols, this.terminal.rows);
      } catch {
        /* Hidden/transitioning layout has no reliable size yet. */
      }
    });
  }
  show() {
    this.ensureTerminal();
    this.fit();
  }
  focus() {
    if (!this.disposed) this.terminal?.focus();
  }
  write(bytes) {
    if (this.disposed) return Promise.resolve();
    this.ensureTerminal();
    if (!this.terminal) return Promise.reject(new Error('Terminal is not mounted'));
    return new Promise((resolve) => {
      this.pendingWrites.add(resolve);
      this.terminal.write(bytes, () => {
        this.pendingWrites.delete(resolve);
        resolve();
      });
    });
  }
  attached(message) {
    this.ensureTerminal();
    this.syncSession(this.session);
    this.replayingHistory = message.resume_strategy === 'history_replay';
    if (!message.preserve) this.terminal?.reset();
    if (Number.isSafeInteger(message.cols) && Number.isSafeInteger(message.rows)) {
      if (this.replayingHistory) this.terminal?.resize(message.cols, message.rows);
      this.updateSize(message.cols, message.rows);
    }
    this.blockUserInput = true;
    this.clearAuth();
    this.fit();
  }
  stateChanged(details) {
    this.syncSession(this.session);
    if (details.state === 'ready' && this.replayingHistory) {
      this.replayingHistory = false;
      this.fit();
    }
    this.blockUserInput = details.state !== 'ready' || !!this.pasteController;
    const labels = {
      idle: '待连接',
      creating: '创建会话',
      connecting: '正在连接',
      authenticating: '等待认证',
      restoring: '恢复画面',
      reconnecting: '重新连接',
      ready: '已连接',
      detached: '已分离',
      blocked: '连接已暂停',
      disconnected: '未连接',
      closing: '正在结束',
      close_unknown: '结果待确认',
      exited: '已结束',
      closed: '已结束',
      failed: '连接失败',
      revoked: '权限已撤销',
    };
    this.status.textContent = labels[details.state] || '正在连接';
    this.root.dataset.state = details.state;
    this.progressBar.hidden = ![
      'creating',
      'connecting',
      'authenticating',
      'restoring',
      'reconnecting',
    ].includes(details.state);
    if (['disconnected', 'exited', 'closed', 'failed', 'revoked'].includes(details.state))
      this.rttBadge.hidden = true;
    this.status.title = [
      formatEndpoint(this.session),
      this.session.device_name && `经由 ${this.session.device_name}`,
      this.session.mode === 'tmux' ? '会话可在 SSH 断开后恢复' : '直连 SSH，会话依赖当前连接',
    ]
      .filter(Boolean)
      .join(' · ');
    if (details.reason_code || details.message || details.state === 'reconnecting')
      this.showNotice(details.reason_code, details.message, details.state, details);
    else if (details.state === 'ready') {
      if (details.input_uncertain) this.showNotice('INPUT_UNCERTAIN');
      else {
        this.notice.hidden = true;
        delete this.notice.dataset.severity;
        this.actions.replaceChildren();
      }
    }
    if (
      ['blocked', 'disconnected', 'exited', 'closed', 'failed', 'revoked'].includes(details.state)
    ) {
      this.pasteController?.abort();
      this.clearAuth();
    }
    this.onState?.(details);
  }
  showNotice(code, fallback, state, details = {}) {
    this.notice.hidden = false;
    if (['HOST_KEY_CHANGED', 'AUTH_REVOKED'].includes(code)) {
      this.notice.dataset.severity = 'danger';
    } else {
      delete this.notice.dataset.severity;
    }
    const baseText = reasonText(
      code,
      fallback || (state === 'reconnecting' ? '网络连接中断，正在恢复连接' : ''),
    );
    const retryText =
      state === 'reconnecting' && details?.retry_attempt
        ? `（第 ${details.retry_attempt}/${details.max_retries || 8} 次重试）`
        : '';
    this.statusText.textContent = `${baseText}${retryText}`;
    this.actions.replaceChildren();
    if (state === 'reconnecting') {
      this.actions.append(
        button('立即重连', () => this.transport.connect({ manual: true }), 'vt-primary'),
      );
    } else if (['WRITER_CONFLICT', 'WRITER_REPLACED', 'STALE_WRITER'].includes(code))
      this.actions.append(button('接管', () => this.onTakeover?.()));
    else if (
      [
        'SSH_AUTH_FAILED',
        'HOST_KEY_UNKNOWN',
        'HOST_KEY_CHANGED',
        'TARGET_REVISION_CHANGED',
      ].includes(code)
    )
      this.actions.append(button('编辑连接', () => this.onEdit?.()));
    else if (
      [
        'HISTORY_GAP',
        'REMOTE_SESSION_MISSING',
        'SESSION_NOT_FOUND',
        'SESSION_CLOSED',
        'SESSION_ENDED',
        'SESSION_NOT_RECOVERABLE',
        'VPS_AUTHORIZATION_CHANGED',
        'VPS_CONFIGURATION_CHANGED',
      ].includes(code)
    )
      this.actions.append(button('新建终端', () => this.onNewSession?.()));
    else if (code === 'CLOSE_UNKNOWN')
      this.actions.append(button('检查状态', () => this.onRefreshState?.()));
    else if (
      ['blocked', 'disconnected', 'failed'].includes(state) &&
      ![
        'AUTH_REVOKED',
        'FORBIDDEN',
        'AUTH_REQUIRED',
        'LOGIN_REQUIRED',
        'SESSION_EXPIRED',
        'UNAUTHORIZED',
        'FEATURE_DISABLED',
        'AGENT_UPGRADE_REQUIRED',
        'PROTOCOL_MISMATCH',
      ].includes(code)
    )
      this.actions.append(button('重新连接', () => this.transport.connect({ manual: true })));
    if (code === 'INPUT_UNCERTAIN')
      this.actions.append(
        button('知道了', () => {
          this.notice.hidden = true;
          this.transport.inputUncertain = false;
        }),
      );
  }

  makeSearch() {
    const bar = el('div', 'vt-search');
    bar.hidden = true;
    this.searchInput = el('input');
    this.searchInput.type = 'search';
    this.searchInput.placeholder = '查找当前终端';
    this.searchInput.setAttribute('aria-label', '查找当前终端内容');
    const find = (backward) => {
      const found = this.searchAddon?.[backward ? 'findPrevious' : 'findNext'](
        this.searchInput.value,
        { incremental: false },
      );
      this.searchResult.textContent = found ? '' : this.searchInput.value ? '没有匹配' : '';
    };
    this.searchInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        find(event.shiftKey);
      }
      if (event.key === 'Escape') {
        this.toggleSearch(false);
        event.preventDefault();
      }
    });
    this.searchResult = el('span', 'vt-search-result');
    this.searchResult.setAttribute('role', 'status');
    bar.append(
      this.searchInput,
      this.searchResult,
      button('↑', () => find(true)),
      button('↓', () => find(false)),
      button('关闭', () => this.toggleSearch(false)),
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
      this.showNotice('', '先选择要复制的内容');
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      this.showNotice('', '请使用系统复制快捷键');
    }
  }
  async clipboardPaste() {
    if (!this.transport.canInput) return;
    try {
      this.requestPaste(await navigator.clipboard.readText());
    } catch {
      this.showNotice('', '请在终端中使用系统粘贴快捷键');
    }
  }
  requestPaste(text) {
    if (!text || !this.transport.canInput || this.pasteController) return;
    const summary = pasteSummary(text, this.transport.limits.paste_bytes);
    if (summary.tooLarge) {
      this.showNotice(
        '',
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
      if (current.tooLarge) return '内容过大，请缩短后粘贴';
      const multiline = /[\r\n]/.test(value);
      return multiline
        ? `粘贴 ${current.lines} 行（含换行，粘贴后可能立即执行） · ${current.bytes.toLocaleString()} 字节`
        : `粘贴 ${current.lines} 行 · ${current.bytes.toLocaleString()} 字节`;
    };
    const headingRow = el('div', 'vt-paste-header');
    const heading = el('div', 'vt-paste-heading', describe(text));
    const shortcutHint = el('span', 'vt-muted', 'Ctrl/⌘ + Enter 确认 · Esc 取消');
    headingRow.append(heading, shortcutHint);
    const input = el('textarea');
    input.value = text;
    input.rows = Math.min(6, Math.max(3, summary.lines));
    input.setAttribute('aria-label', '粘贴前查看和编辑内容');
    input.spellcheck = false;
    input.autocapitalize = 'off';
    input.autocomplete = 'off';
    const controls = el('div', 'vt-paste-actions');
    const clear = () => {
      input.value = '';
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
        heading.textContent = '内容过大，请缩短后粘贴';
        return;
      }
      clear();
      this.runPaste(edited);
    };
    input.addEventListener('input', () => {
      heading.textContent = describe(input.value);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        clear();
      } else if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        event.stopPropagation();
        confirmPaste();
      }
    });
    controls.append(button('取消', clear), button('粘贴', confirmPaste, 'vt-primary'));
    this.pasteBox.append(headingRow, input, controls);
    input.focus();
  }
  async runPaste(text) {
    if (!this.transport.canInput || this.pasteController) return;
    const controller = (this.pasteController = new AbortController());
    this.blockUserInput = true;
    this.notice.hidden = false;
    this.statusText.textContent = '正在粘贴';
    this.actions.replaceChildren(button('停止粘贴', () => controller.abort()));
    try {
      const result = await pasteInChunks(text, {
        signal: controller.signal,
        writable: (bytes) =>
          this.transport.pendingInputBytes + bytes < this.transport.limits.pending_input_bytes &&
          this.transport.socket?.bufferedAmount < this.transport.limits.pending_input_bytes,
        connected: () => this.transport.canInput && !this.disposed,
        paste: (chunk) => {
          this.lastPasteAccepted = false;
          this.terminal.paste(chunk);
          return this.lastPasteAccepted;
        },
        onProgress: (sent, total) => {
          this.statusText.textContent = `正在粘贴 ${Math.round((sent / total) * 100)}%`;
        },
        maxChunkBytes: Math.max(
          128,
          Math.min(
            4096,
            this.transport.limits.input_frame_bytes - 16,
            this.transport.limits.pending_input_bytes - 32,
          ),
        ),
      });
      if (result.cancelled) this.showNotice('', '已停止粘贴，已发送的内容保留在远端');
      else if (this.transport.inputUncertain) this.showNotice('INPUT_UNCERTAIN');
      else this.notice.hidden = true;
    } catch (error) {
      this.showNotice('', error.message);
    } finally {
      if (this.pasteController === controller) this.pasteController = null;
      this.blockUserInput = !this.transport.canInput;
      this.focus();
    }
  }

  makeMobileKeys() {
    const bar = el('div', 'vt-mobile-keys');
    bar.setAttribute('aria-label', '终端辅助键');
    for (const [label, value] of [
      ['Esc', '\x1b'],
      ['Tab', '\t'],
      ['↑', '\x1b[A'],
      ['↓', '\x1b[B'],
      ['←', '\x1b[D'],
      ['→', '\x1b[C'],
      ['Ctrl-C', '\x03'],
    ]) {
      const key = button(label, () => this.sendKey(value));
      key.addEventListener('pointerdown', (event) => event.preventDefault());
      bar.append(key);
    }
    this.ctrlButton = button('Ctrl', () => {
      this.ctrlLatched = !this.ctrlLatched;
      this.ctrlButton.setAttribute('aria-pressed', String(this.ctrlLatched));
      this.focus();
    });
    this.altButton = button('Alt', () => {
      this.altLatched = !this.altLatched;
      this.altButton.setAttribute('aria-pressed', String(this.altLatched));
      this.focus();
    });
    this.ctrlButton.setAttribute('aria-pressed', 'false');
    this.altButton.setAttribute('aria-pressed', 'false');
    for (const key of [this.ctrlButton, this.altButton])
      key.addEventListener('pointerdown', (event) => event.preventDefault());
    bar.insertBefore(this.ctrlButton, bar.children[2]);
    bar.insertBefore(this.altButton, bar.children[3]);
    const pasteKey = button('粘贴', () => this.clipboardPaste());
    pasteKey.addEventListener('pointerdown', (event) => event.preventDefault());
    bar.append(pasteKey);
    for (const [label, value] of [
      ['/', '/'],
      ['-', '-'],
      ['|', '|'],
      ['~', '~'],
      ['Ctrl-D', '\x04'],
      ['Ctrl-L', '\x0c'],
      ['PgUp', '\x1b[5~'],
      ['PgDn', '\x1b[6~'],
    ]) {
      const key = button(label, () => this.sendKey(value));
      key.addEventListener('pointerdown', (event) => event.preventDefault());
      bar.append(key);
    }
    return bar;
  }
  clearModifiers() {
    this.ctrlLatched = false;
    this.altLatched = false;
    this.ctrlButton.setAttribute('aria-pressed', 'false');
    this.altButton.setAttribute('aria-pressed', 'false');
  }
  sendKey(value) {
    if (!this.transport.canInput || this.pasteController) return;
    this.transport.sendBytes(encoder.encode(value));
    this.clearModifiers();
    this.focus();
  }

  authChallenge(message) {
    this.clearAuth();
    if (!Array.isArray(message.prompts) || message.prompts.length > 8 || !message.challenge_id)
      return;
    this.authBox.hidden = false;
    const totalSeconds = Math.min(180, Math.max(1, Math.floor(message.deadline_seconds || 60)));
    let remaining = totalSeconds;
    const form = el('form', 'vt-auth-form');
    form.autocomplete = 'off';
    const header = el('div', 'vt-auth-header');
    const countdown = el('span', 'vt-auth-countdown', `剩余 ${remaining} 秒`);
    header.append(el('strong', '', 'SSH 需要进一步认证'), countdown);
    form.append(header);
    const inputs = [];
    for (const prompt of message.prompts) {
      const label = el('label', 'vt-auth-field'),
        text = el('span', '', String(prompt.text || '验证码').slice(0, 240)),
        input = el('input');
      input.type = prompt.echo ? 'text' : 'password';
      input.autocomplete = 'one-time-code';
      input.maxLength = 4096;
      input.autocapitalize = 'off';
      input.spellcheck = false;
      input.required = false;
      label.append(text, input);
      form.append(label);
      inputs.push(input);
    }
    const submit = el('button', 'vt-button vt-primary', '继续连接');
    submit.type = 'submit';
    const cancel = button('取消连接', () => {
      this.clearAuth();
      this.transport.fail('SSH_AUTH_FAILED', { message: '认证已取消' }, true);
    });
    form.append(submit, cancel);
    this.authBox.append(form);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const answers = inputs.map((input) => input.value);
      const sent = this.transport.answerAuth(message.challenge_id, answers);
      inputs.forEach((input) => {
        input.value = '';
      });
      answers.fill('');
      this.clearAuth();
      if (!sent) this.showNotice('', '认证未发送，请重新连接');
    });
    this.authCountdownTimer = setInterval(() => {
      remaining = Math.max(0, remaining - 1);
      countdown.textContent = `剩余 ${remaining} 秒`;
    }, 1000);
    this.authTimer = setTimeout(() => {
      this.clearAuth();
      this.showNotice('', '认证已超时，请重新连接');
    }, totalSeconds * 1000);
    inputs[0]?.focus();
  }
  clearAuth() {
    clearTimeout(this.authTimer);
    clearInterval(this.authCountdownTimer);
    this.authCountdownTimer = null;
    for (const input of this.authBox.querySelectorAll('input')) input.value = '';
    this.authBox.replaceChildren();
    this.authBox.hidden = true;
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.diagnostics.dispose();
    this.pasteController?.abort();
    this.clearAuth();
    if (this.fitFrame) cancelAnimationFrame(this.fitFrame);
    this.resizeObserver?.disconnect();
    this.appearanceObserver?.disconnect();
    for (const disposable of this.disposers) {
      if (typeof disposable === 'function') disposable();
      else disposable?.dispose();
    }
    this.disposers = [];
    this.terminal?.dispose();
    this.terminal = null;
    for (const resolve of this.pendingWrites) resolve();
    this.pendingWrites.clear();
    for (const textarea of this.pasteBox.querySelectorAll('textarea')) textarea.value = '';
    this.root.remove();
  }
}
