const MAX_HISTORY_ITEMS = 20;
const MAX_HISTORY_BYTES = 8192;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: false });

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, action, className = "") {
  const node = el("button", `vt-button ${className}`.trim(), label);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

export function sliceUtf8Text(text, maxBytes = MAX_HISTORY_BYTES) {
  const bytes = encoder.encode(text);
  if (bytes.byteLength <= maxBytes) return text;
  let end = maxBytes;
  while (end > 0 && bytes[end] >= 0x80 && bytes[end] < 0xc0) end--;
  return decoder.decode(bytes.subarray(0, end));
}

// Ephemeral in-memory command and prompt composer. Never touches browser storage.
export class TerminalComposer {
  constructor({ onSend, onClose, onToggle } = {}) {
    this.onSend = onSend;
    this.onClose = onClose;
    this.onToggle = onToggle;
    this.history = [];
    this.historyCursor = 0;
    this.draft = "";
    this.sending = false;
    this.disposed = false;

    this.root = el("div", "vt-composer");
    this.root.hidden = true;
    this.root.setAttribute("role", "region");
    this.root.setAttribute("aria-label", "命令速写板");

    const bar = el("div", "vt-composer-bar");
    const title = el("strong", "vt-composer-title", "命令速写板");
    this.prevButton = button(
      "上一条",
      () => this.stepHistory(-1),
      "vt-composer-prev",
    );
    this.prevButton.setAttribute("aria-label", "上一条内存指令");
    this.nextButton = button(
      "下一条",
      () => this.stepHistory(1),
      "vt-composer-next",
    );
    this.nextButton.setAttribute("aria-label", "下一条内存指令");
    this.closeButton = button(
      "收起",
      () => this.close({ restoreFocus: true }),
      "vt-composer-close",
    );
    bar.append(title, this.prevButton, this.nextButton, this.closeButton);

    this.textarea = el("textarea", "vt-composer-input");
    this.textarea.rows = 2;
    this.textarea.placeholder = "输入中文指令、语音听写或多行命令…";
    this.textarea.setAttribute("aria-label", "命令速写板输入内容");
    this.textarea.spellcheck = false;
    this.textarea.autocapitalize = "none";
    this.textarea.autocomplete = "off";
    this.textarea.addEventListener("input", () => this.syncHeight());
    this.textarea.addEventListener("keydown", (event) =>
      this.handleKeydown(event),
    );

    const actions = el("div", "vt-composer-actions");
    this.clearButton = button(
      "清空",
      () => {
        this.textarea.value = "";
        this.syncHeight();
        this.textarea.focus?.();
      },
      "vt-composer-clear",
    );
    this.insertButton = button(
      "仅填入",
      () => this.submit({ execute: false }),
      "vt-composer-insert",
    );
    this.sendButton = button(
      "发送并执行",
      () => this.submit({ execute: true }),
      "vt-primary vt-composer-send",
    );
    actions.append(this.clearButton, this.insertButton, this.sendButton);

    this.root.append(bar, this.textarea, actions);
    this.syncButtons();
  }

  isOpen() {
    return !this.root.hidden;
  }

  toggle(force = this.root.hidden) {
    if (force) this.open();
    else this.close({ restoreFocus: true });
  }

  open() {
    if (this.disposed) return;
    this.root.hidden = false;
    this.syncHeight();
    this.syncButtons();
    this.onToggle?.(true);
    this.textarea.focus?.();
  }

  close({ restoreFocus = false } = {}) {
    if (this.root.hidden) return;
    this.root.hidden = true;
    this.onToggle?.(false);
    if (restoreFocus) this.onClose?.();
  }

  syncHeight() {
    const lines = (this.textarea.value || "").split("\n").length;
    this.textarea.rows = Math.min(5, Math.max(2, lines));
    this.syncButtons();
  }

  syncButtons() {
    const hasText = !!(this.textarea.value || "").length;
    this.prevButton.disabled =
      this.sending || !this.history.length || this.historyCursor <= 0;
    this.nextButton.disabled =
      this.sending || this.historyCursor >= this.history.length;
    this.insertButton.disabled = this.sending || !hasText;
    this.sendButton.disabled = this.sending || !hasText;
    this.clearButton.disabled = this.sending || !hasText;
  }

  handleKeydown(event) {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "Escape") {
      event.preventDefault?.();
      event.stopPropagation?.();
      this.close({ restoreFocus: true });
      return;
    }
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault?.();
      event.stopPropagation?.();
      this.submit({ execute: true });
    }
  }

  recordHistory(rawText) {
    if (!rawText || !rawText.trim()) return;
    const bounded = sliceUtf8Text(rawText, MAX_HISTORY_BYTES);
    if (!bounded) return;
    if (this.history[this.history.length - 1] !== bounded) {
      if (this.history.length >= MAX_HISTORY_ITEMS) {
        this.history[0] = "";
        this.history.shift();
      }
      this.history.push(bounded);
    }
    this.historyCursor = this.history.length;
    this.draft = "";
  }

  stepHistory(delta) {
    if (!this.history.length || this.sending) return;
    if (this.historyCursor === this.history.length && delta < 0)
      this.draft = this.textarea.value || "";
    const next = Math.max(
      0,
      Math.min(this.history.length, this.historyCursor + delta),
    );
    if (next === this.historyCursor) return;
    this.historyCursor = next;
    this.textarea.value =
      next === this.history.length ? this.draft : this.history[next];
    this.syncHeight();
  }

  async submit({ execute = true } = {}) {
    const value = this.textarea.value || "";
    if (!value || this.sending || this.disposed) return false;
    this.sending = true;
    this.syncButtons();
    try {
      const ok = await this.onSend?.(value, { execute });
      if (ok === false || this.disposed) return false;
      this.recordHistory(value);
      this.textarea.value = "";
      this.syncHeight();
      return true;
    } finally {
      this.sending = false;
      if (!this.disposed) this.syncButtons();
    }
  }

  reset() {
    for (let i = 0; i < this.history.length; i++) this.history[i] = "";
    this.history.length = 0;
    this.historyCursor = 0;
    this.draft = "";
    if (this.textarea) this.textarea.value = "";
    this.syncButtons();
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.reset();
    this.root.hidden = true;
    this.onSend = null;
    this.onClose = null;
    this.onToggle = null;
  }
}
