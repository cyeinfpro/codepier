import assert from "node:assert/strict";
import test from "node:test";

import { TerminalLayout } from "../web/vps-terminal/layout.mjs";
import { TerminalStore } from "../web/vps-terminal/store.mjs";
import { TerminalView } from "../web/vps-terminal/terminal-view.mjs";
import { TerminalTransport } from "../web/vps-terminal/transport.mjs";
import { TerminalComposer } from "../web/vps-terminal/composer.mjs";
import {
  TouchScrollController,
  glideGearInterval,
  resolveGlideDirection,
} from "../web/vps-terminal/touch-scroll.mjs";
import { DEFAULT_LIMITS } from "../web/vps-terminal/protocol.mjs";

function fakeElement(tag) {
  return {
    tag,
    value: "",
    children: [],
    listeners: {},
    setAttribute() {},
    classList: { add() {} },
    append(...children) {
      this.children.push(...children);
    },
    replaceChildren(...children) {
      this.children = children;
    },
    insertBefore(child, before) {
      this.children.splice(this.children.indexOf(before), 0, child);
    },
    addEventListener(key, listener) {
      this.listeners[key] = listener;
    },
  };
}

test("quick connect highlights and opens only rendered VPS and respects IME", () => {
  globalThis.document = { createElement: fakeElement };
  try {
    const layout = Object.create(TerminalLayout.prototype);
    const opened = [];
    let closed = 0;
    layout.quickIndex = 0;
    layout.availableServers = () =>
      Array.from({ length: 41 }, (_, i) => ({ id: i, name: "VPS " + i }));
    layout.syncQuickSelection = () => {};
    layout.openFromQuickConnect = (vps) => opened.push(vps.id);
    layout.closeQuickConnect = () => closed++;
    const quick = layout.makeQuickConnect();
    layout.renderQuickConnect();
    assert.equal(layout.quickList.children.length, 40);
    function key(key, isComposing = false, keyCode = 0) {
      quick.listeners.keydown({
        key,
        isComposing,
        keyCode,
        target: layout.quickSearchInput,
        preventDefault() {},
        stopPropagation() {},
      });
    }
    key("Enter", true);
    key("Escape", false, 229);
    key("ArrowDown", true);
    assert.deepEqual(opened, []);
    assert.equal(closed, 0);
    assert.equal(layout.quickIndex, 0);
    for (let i = 0; i < 40; i++) key("ArrowDown");
    key("Enter");
    assert.deepEqual(opened, [0]);
    layout.quickSearchInput.value = "VPS 40";
    quick.children[0].listeners.input();
    key("Enter");
    assert.deepEqual(opened, [0, 40]);
  } finally {
    delete globalThis.document;
  }
});

test("terminal catalog follows cursor, coalesces reads, deduplicates and preserves cache on failure", async () => {
  const requests = [];
  let fail = false;
  const store = new TerminalStore({
    api: async (url) => {
      requests.push(url);
      if (url.endsWith("=0"))
        return {
          vps: [{ id: "old" }, { id: "disabled", enabled: false }],
          next_offset: 200,
        };
      if (fail) throw new Error("offline");
      return { vps: [{ id: "old" }, { id: "new" }], next_offset: null };
    },
  });
  const [one, two] = await Promise.all([
    store.refreshVpsCatalog(),
    store.refreshVpsCatalog(),
  ]);
  assert.deepEqual(
    one.map((vps) => vps.id),
    ["old", "new"],
  );
  assert.equal(one, two);
  assert.equal(requests.length, 2);
  fail = true;
  assert.equal(await store.refreshVpsCatalog(), one);
});

test("active session count is deduped and local terminal state wins", () => {
  const store = new TerminalStore({});
  store.remoteSessions = [
    { session_id: "ended", vps_id: "first", state: "ready" },
    { session_id: "remote", vps_id: "first", state: "ready" },
    { id: "remote", vps_id: "first", state: "ready" },
  ];
  store.records.set("ended", {
    id: "ended",
    session: { session_id: "ended", vps_id: "first" },
    state: "closed",
  });
  store.records.set("local", {
    id: "local",
    session: { session_id: "local", vps_id: "second" },
    state: "ready",
  });
  assert.equal(store.count(), 2);
  assert.deepEqual({ ...store.activeCountsByVPS() }, { first: 1, second: 1 });
});

test("WS closed state wins over later HTTP rejection and close_unknown", async () => {
  for (const outcome of ["network lost", "unknown"]) {
    let reply, reject;
    const transitions = [];
    const store = new TerminalStore({
      api: () =>
        new Promise((resolve, fail) => {
          reply = resolve;
          reject = fail;
        }),
    });
    const record = {
      id: "test",
      state: "ready",
      session: { id: "test" },
      transport: {},
      view: { stateChanged: ({ state }) => transitions.push(state) },
    };
    store.records.set(record.id, record);
    const close = store.close(record.id);
    record.state = "closed";
    record.view.stateChanged({ state: "closed" });
    if (outcome === "unknown") reply({ state: "close_unknown" });
    else reject(new Error(outcome));
    await close;
    assert.equal(store.count(), 0);
    assert.deepEqual(transitions, ["closing", "closed"]);
  }
});

test("mobile direction key matches xterm application cursor mode", () => {
  globalThis.document = { createElement: fakeElement };
  try {
    const view = Object.create(TerminalView.prototype);
    const sent = [];
    view.sendKey = (bytes) => sent.push(bytes);
    view.terminal = { modes: { applicationCursorKeysMode: false } };
    const bar = view.makeMobileKeys();
    const up = bar.children.find((node) => node.textContent === "↑");
    up.listeners.click();
    view.terminal.modes.applicationCursorKeysMode = true;
    up.listeners.click();
    assert.deepEqual(sent, ["\x1b[A", "\x1bOA"]);
  } finally {
    delete globalThis.document;
  }
});

const decoder = new TextDecoder();

function createDomEnv({ narrow = true, coarse = true } = {}) {
  const docListeners = new Map();
  const winListeners = new Map();
  function makeNode(tag) {
    const attrs = new Map();
    const classes = new Set();
    const multiListeners = new Map();
    const node = {
      tag: String(tag || "div").toUpperCase(),
      value: "",
      textContent: "",
      hidden: false,
      disabled: false,
      checked: false,
      rows: 2,
      parentNode: null,
      isConnected: true,
      dataset: {},
      style: {
        setProperty(k, v) {
          this[k] = v;
        },
        getPropertyValue(k) {
          return this[k] || "";
        },
      },
      children: [],
      listeners: {},
      get className() {
        return [...classes].join(" ");
      },
      set className(val) {
        classes.clear();
        String(val || "")
          .split(/\s+/)
          .filter(Boolean)
          .forEach((c) => classes.add(c));
      },
      classList: {
        add(...items) {
          items.forEach((c) => c && classes.add(c));
        },
        remove(...items) {
          items.forEach((c) => classes.delete(c));
        },
        toggle(c, force) {
          const next = force === undefined ? !classes.has(c) : !!force;
          if (next) classes.add(c);
          else classes.delete(c);
          return next;
        },
        contains(c) {
          return classes.has(c);
        },
      },
      setAttribute(k, v) {
        attrs.set(k, String(v));
        if (k.startsWith("data-")) {
          const camel = k
            .slice(5)
            .replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
          node.dataset[camel] = String(v);
        }
      },
      getAttribute(k) {
        return attrs.get(k) ?? null;
      },
      removeAttribute(k) {
        attrs.delete(k);
      },
      append(...items) {
        for (const item of items) {
          if (item && typeof item === "object") item.parentNode = node;
          node.children.push(item);
        }
      },
      prepend(...items) {
        for (const item of items) {
          if (item && typeof item === "object") item.parentNode = node;
        }
        node.children.unshift(...items);
      },
      replaceChildren(...items) {
        for (const item of items) {
          if (item && typeof item === "object") item.parentNode = node;
        }
        node.children = [...items];
      },
      insertBefore(child, before) {
        child.parentNode = node;
        const idx = node.children.indexOf(before);
        node.children.splice(idx < 0 ? node.children.length : idx, 0, child);
      },
      remove() {
        if (node.parentNode?.children) {
          const idx = node.parentNode.children.indexOf(node);
          if (idx >= 0) node.parentNode.children.splice(idx, 1);
        }
        node.parentNode = null;
      },
      contains(target) {
        let cur = target;
        while (cur) {
          if (cur === node) return true;
          cur = cur.parentNode;
        }
        return false;
      },
      closest(selector) {
        const parts = selector.split(",").map((s) => s.trim());
        let cur = node;
        while (cur) {
          if (
            parts.some((sel) =>
              sel.startsWith(".")
                ? cur.classList?.contains(sel.slice(1))
                : false,
            )
          )
            return cur;
          cur = cur.parentNode;
        }
        return null;
      },
      querySelector(selector) {
        return node.querySelectorAll(selector)[0] || null;
      },
      querySelectorAll(selector) {
        const out = [];
        const match = (el) => {
          if (!el || typeof el !== "object") return false;
          if (selector.startsWith("."))
            return !!el.classList?.contains(selector.slice(1));
          if (selector === "[role=tab]")
            return el.getAttribute?.("role") === "tab";
          return el.tag === selector.toUpperCase();
        };
        const walk = (parent) => {
          for (const child of parent.children || []) {
            if (match(child)) out.push(child);
            walk(child);
          }
        };
        walk(node);
        return out;
      },
      addEventListener(type, fn) {
        node.listeners[type] = fn;
        if (!multiListeners.has(type)) multiListeners.set(type, []);
        multiListeners.get(type).push(fn);
      },
      removeEventListener(type, fn) {
        const arr = multiListeners.get(type);
        if (!arr) return;
        const idx = arr.indexOf(fn);
        if (idx >= 0) arr.splice(idx, 1);
      },
      dispatch(type, event = {}) {
        const payload = {
          type,
          target: event.target || node,
          preventDefault() {
            this.defaultPrevented = true;
          },
          stopPropagation() {},
          stopImmediatePropagation() {},
          ...event,
        };
        for (const fn of [...(multiListeners.get(type) || [])]) fn(payload);
        return payload;
      },
      getBoundingClientRect() {
        return (
          node._rect || {
            left: 0,
            top: 0,
            right: 390,
            bottom: 760,
            width: 390,
            height: 760,
          }
        );
      },
      matches() {
        return false;
      },
      focus() {
        doc.activeElement = node;
      },
      blur() {
        if (doc.activeElement === node) doc.activeElement = null;
      },
      select() {},
    };
    return node;
  }

  const matchMedia = (query) => ({
    matches:
      (query.includes("max-width: 700px") && narrow) ||
      (query.includes("pointer: coarse") && coarse) ||
      false,
    addEventListener() {},
    removeEventListener() {},
  });

  const doc = {
    visibilityState: "visible",
    fullscreenElement: null,
    activeElement: null,
    documentElement: makeNode("html"),
    body: makeNode("body"),
    createElement: makeNode,
    createTextNode: (text) => ({ textContent: text }),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, new Set());
      docListeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      docListeners.get(type)?.delete(fn);
    },
    dispatch(type, event = {}) {
      for (const fn of [...(docListeners.get(type) || [])]) fn(event);
    },
  };

  const win = {
    innerWidth: narrow ? 390 : 1200,
    innerHeight: 844,
    matchMedia,
    addEventListener(type, fn) {
      if (!winListeners.has(type)) winListeners.set(type, new Set());
      winListeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      winListeners.get(type)?.delete(fn);
    },
    dispatch(type, event = {}) {
      for (const fn of [...(winListeners.get(type) || [])]) fn(event);
    },
  };

  const saved = {
    document: globalThis.document,
    window: globalThis.window,
    matchMedia: globalThis.matchMedia,
    visualViewport: globalThis.visualViewport,
    navigator: Object.getOwnPropertyDescriptor(globalThis, "navigator"),
    requestAnimationFrame: globalThis.requestAnimationFrame,
    cancelAnimationFrame: globalThis.cancelAnimationFrame,
    CodePierAppearance: globalThis.CodePierAppearance,
    getComputedStyle: globalThis.getComputedStyle,
    Node: globalThis.Node,
    WebSocket: globalThis.WebSocket,
    location: globalThis.location,
  };

  globalThis.document = doc;
  globalThis.window = win;
  globalThis.matchMedia = matchMedia;
  globalThis.visualViewport = {
    height: 844,
    width: win.innerWidth,
    scale: 1,
    offsetTop: 0,
    offsetLeft: 0,
  };
  globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
  globalThis.getComputedStyle = () => ({
    display: "none",
    visibility: "hidden",
  });
  globalThis.Node = Object;

  return {
    doc,
    win,
    makeNode,
    restore() {
      if (saved.document === undefined) delete globalThis.document;
      else globalThis.document = saved.document;
      if (saved.window === undefined) delete globalThis.window;
      else globalThis.window = saved.window;
      if (saved.matchMedia === undefined) delete globalThis.matchMedia;
      else globalThis.matchMedia = saved.matchMedia;
      if (saved.visualViewport === undefined) delete globalThis.visualViewport;
      else globalThis.visualViewport = saved.visualViewport;
      if (saved.requestAnimationFrame === undefined)
        delete globalThis.requestAnimationFrame;
      else globalThis.requestAnimationFrame = saved.requestAnimationFrame;
      if (saved.cancelAnimationFrame === undefined)
        delete globalThis.cancelAnimationFrame;
      else globalThis.cancelAnimationFrame = saved.cancelAnimationFrame;
      if (saved.CodePierAppearance === undefined)
        delete globalThis.CodePierAppearance;
      else globalThis.CodePierAppearance = saved.CodePierAppearance;
      if (saved.getComputedStyle === undefined)
        delete globalThis.getComputedStyle;
      else globalThis.getComputedStyle = saved.getComputedStyle;
      if (saved.Node === undefined) delete globalThis.Node;
      else globalThis.Node = saved.Node;
      if (saved.WebSocket === undefined) delete globalThis.WebSocket;
      else globalThis.WebSocket = saved.WebSocket;
      if (saved.location === undefined) delete globalThis.location;
      else globalThis.location = saved.location;
      if (saved.navigator)
        Object.defineProperty(globalThis, "navigator", saved.navigator);
    },
  };
}

test("test_alternate_buffer_touch_drag_emits_sgr_wheel_sequences", () => {
  const env = createDomEnv({ narrow: true, coarse: true });
  try {
    const sent = [];
    const scrolled = [];
    let clock = 1000;
    const fakeTransport = {
      canInput: true,
      limits: { ...DEFAULT_LIMITS },
      sendBytes(bytes) {
        sent.push(decoder.decode(bytes));
        return true;
      },
      resize() {},
    };
    const view = new TerminalView({
      session: {
        id: "11111111-1111-4111-8111-111111111111",
        mode: "tmux",
        vps_name: "Prod",
      },
      transport: fakeTransport,
      runtime: {},
      now: () => clock,
    });
    assert.equal(view.fontSize, 13.5);
    assert.equal(view.lineHeight, 1.18);

    let fitCalls = 0;
    view.fit = () => {
      fitCalls++;
    };
    view.terminal = {
      cols: 48,
      rows: 24,
      options: { fontSize: view.fontSize },
      modes: { mouseTrackingMode: "sgr", applicationCursorKeysMode: false },
      buffer: { active: { type: "alternate", viewportY: 0, baseY: 0 } },
      hasSelection: () => false,
      scrollLines: (n) => scrolled.push(n),
      scrollToBottom: () => scrolled.push("bottom"),
      dispose() {},
    };
    view.canvas._rect = {
      left: 0,
      top: 0,
      right: 384,
      bottom: 384,
      width: 384,
      height: 384,
    };

    // Blocked input must never emit SGR wheel frames.
    view.blockUserInput = true;
    view.canvas.dispatch("touchstart", {
      touches: [{ clientX: 80, clientY: 40 }],
    });
    view.canvas.dispatch("touchmove", {
      touches: [{ clientX: 80, clientY: 90 }],
    });
    view.canvas.dispatch("touchend", { touches: [] });
    assert.deepEqual(sent, []);

    // Unblocked alternate screen emits SGR wheel up (64) and reveals return-to-bottom.
    view.blockUserInput = false;
    view.canvas.dispatch("touchstart", {
      touches: [{ clientX: 40, clientY: 40 }],
    });
    clock += 16;
    const moveUp = view.canvas.dispatch("touchmove", {
      touches: [{ clientX: 40, clientY: 76 }],
    });
    assert.equal(moveUp.defaultPrevented, true);
    assert.equal(sent.length, 1);
    assert.match(sent[0], /^\x1b\[<64;\d+;\d+M/);
    assert.equal(view.altScrolledUp, true);
    assert.equal(view.bottom.hidden, false);

    // Drag upward emits SGR wheel down (65).
    clock += 16;
    view.canvas.dispatch("touchmove", {
      touches: [{ clientX: 40, clientY: 48 }],
    });
    assert.equal(sent.length, 2);
    assert.match(sent[1], /^\x1b\[<65;\d+;\d+M/);
    view.canvas.dispatch("touchcancel", { touches: [] });

    // Clicking bottom button in alternate buffer sends wheel-down burst and clears state.
    const beforeBottom = sent.length;
    view.bottom.dispatch("click");
    assert.equal(sent.length, beforeBottom + 1);
    assert.match(sent.at(-1), /^(?:\x1b\[<65;1;1M)+$/);
    assert.equal(view.altScrolledUp, false);
    assert.equal(view.bottom.hidden, true);

    // Alternate screen without mouse tracking falls back to application cursor keys.
    view.terminal.modes.mouseTrackingMode = "none";
    view.terminal.modes.applicationCursorKeysMode = true;
    view.canvas.dispatch("touchstart", {
      touches: [{ clientX: 20, clientY: 20 }],
    });
    clock += 16;
    view.canvas.dispatch("touchmove", {
      touches: [{ clientX: 20, clientY: 45 }],
    });
    view.canvas.dispatch("touchcancel", { touches: [] });
    assert.equal(sent.at(-1), "\x1bOA");
    assert.equal(view.altScrolledUp, true);
    view.sendKey("a");
    assert.equal(view.altScrolledUp, false);

    // Normal buffer uses terminal.scrollLines and supports kinetic momentum capped at 5 lines/frame.
    const frames = [];
    const kinetic = new TouchScrollController({
      target: env.makeNode("div"),
      getTerminal: () => ({
        cols: 48,
        rows: 24,
        buffer: { active: { type: "normal", viewportY: 10, baseY: 20 } },
        scrollLines: (n) => scrolled.push(n),
        hasSelection: () => false,
      }),
      getFontSize: () => 13.5,
      getLineHeight: () => 1.18,
      canInput: () => true,
      sendRaw: () => true,
      now: () => clock,
      requestFrame: (fn) => {
        frames.push(fn);
        return frames.length;
      },
      cancelFrame: () => {
        frames.length = 0;
      },
      reducedMotion: () => false,
    });
    kinetic.handleTouchStart({ touches: [{ clientX: 30, clientY: 20 }] });
    clock += 20;
    kinetic.handleTouchMove({
      touches: [{ clientX: 30, clientY: 220 }],
      preventDefault() {},
    });
    assert.ok(scrolled.some((n) => typeof n === "number" && n < 0));
    scrolled.length = 0;
    kinetic.handleTouchEnd({ type: "touchend", touches: [] });
    assert.equal(frames.length, 1);
    const firstMomentum = frames.shift();
    firstMomentum();
    assert.ok(scrolled.every((n) => Math.abs(n) <= 5));
    // Touching again immediately halts momentum.
    kinetic.handleTouchStart({ touches: [{ clientX: 30, clientY: 50 }] });
    assert.equal(frames.length, 0);
    kinetic.dispose();

    // Two-finger pinch updates fontSize in 0.5 steps (11..22) and calls fit() once on release.
    fitCalls = 0;
    const pinchStart = view.canvas.dispatch("touchstart", {
      touches: [
        { clientX: 0, clientY: 0 },
        { clientX: 100, clientY: 0 },
      ],
    });
    assert.equal(pinchStart.defaultPrevented, true);
    view.canvas.dispatch("touchmove", {
      touches: [
        { clientX: 0, clientY: 0 },
        { clientX: 120, clientY: 0 },
      ],
    });
    assert.equal(view.fontSize, 16);
    assert.equal(fitCalls, 0);
    view.canvas.dispatch("touchend", { touches: [] });
    assert.equal(fitCalls, 1);

    view.dispose();
  } finally {
    env.restore();
  }
});

test("test_composer_sheet_sends_cjk_and_multiline_via_chunked_paste_and_clears_on_dispose", async () => {
  const env = createDomEnv({ narrow: true, coarse: true });
  try {
    const frames = [];
    const fakeTransport = {
      canInput: true,
      pendingInputBytes: 0,
      socket: { bufferedAmount: 0 },
      limits: { ...DEFAULT_LIMITS },
      sendBytes(bytes) {
        frames.push(decoder.decode(bytes));
        return true;
      },
      resize() {},
    };
    const view = new TerminalView({
      session: {
        id: "22222222-2222-4222-8222-222222222222",
        mode: "tmux",
        vps_name: "AI",
      },
      transport: fakeTransport,
      runtime: {},
    });
    view.blockUserInput = false;
    view.composer.open();
    assert.equal(view.composer.isOpen(), true);

    const cjkPrompt =
      "请检查线上慢查询，保留全部全角标点：，。！？\necho '执行完成'";
    view.composer.textarea.value = cjkPrompt;
    view.composer.syncHeight();
    await view.composer.submit({ execute: true });

    const combined = frames.join("");
    assert.ok(combined.includes("\x1b[200~" + cjkPrompt + "\x1b[201~"));
    assert.ok(combined.endsWith("\r"));
    assert.equal(view.composer.textarea.value, "");
    assert.equal(view.composer.history.length, 1);

    // Insert-only mode omits trailing carriage return.
    frames.length = 0;
    view.composer.textarea.value = "git status";
    await view.composer.submit({ execute: false });
    assert.equal(frames.join(""), "\x1b[200~git status\x1b[201~");

    // Memory-only bounded history (<= 20 entries, <= 8 KiB per entry) and recall.
    for (let i = 0; i < 25; i++) {
      view.composer.recordHistory(`cmd-${i}-` + "汉".repeat(3000));
    }
    assert.equal(view.composer.history.length, 20);
    for (const item of view.composer.history) {
      assert.ok(new TextEncoder().encode(item).byteLength <= 8192);
    }
    view.composer.stepHistory(-1);
    assert.ok(view.composer.textarea.value.startsWith("cmd-24-"));

    view.dispose();
    assert.equal(view.composer.history.length, 0);
    assert.equal(view.composer.textarea.value, "");
  } finally {
    env.restore();
  }
});

test("test_sticky_modifier_double_tap_locks_until_toggled", () => {
  const env = createDomEnv({ narrow: true, coarse: true });
  try {
    let clock = 5000;
    const sent = [];
    const fakeTransport = {
      canInput: true,
      limits: { ...DEFAULT_LIMITS },
      sendBytes(bytes) {
        sent.push(decoder.decode(bytes));
        return true;
      },
      resize() {},
    };
    const view = new TerminalView({
      session: {
        id: "33333333-3333-4333-8333-333333333333",
        mode: "tmux",
        vps_name: "Dev",
      },
      transport: fakeTransport,
      runtime: {},
      now: () => clock,
    });
    view.blockUserInput = false;

    // Verify ergonomic 2-row key bar structure and More toggle.
    const labels = view.mobileKeys.children
      .filter((c) => c.tag === "BUTTON")
      .map((c) => c.textContent);
    assert.deepEqual(labels, [
      "速写",
      "键盘",
      "Esc",
      "Tab",
      "Ctrl",
      "Alt",
      "Ctrl-C",
      "Ctrl-O",
      "←",
      "↓",
      "↑",
      "→",
      "更多",
    ]);
    assert.equal(view.extraKeysRow.hidden, true);
    view.moreKeysButton.dispatch("click");
    assert.equal(view.extraKeysRow.hidden, false);

    // Single tap -> LATCHED_ONCE -> resets after 1 character.
    assert.equal(view.ctrlButton.getAttribute("aria-pressed"), "false");
    assert.equal(view.ctrlButton.getAttribute("data-lock-state"), "idle");
    view.ctrlButton.dispatch("click");
    assert.equal(view.ctrlButton.getAttribute("aria-pressed"), "true");
    assert.equal(view.ctrlButton.getAttribute("data-lock-state"), "latched");
    view.sendKey("o");
    assert.equal(sent.at(-1), "\x0f");
    assert.equal(view.ctrlButton.getAttribute("aria-pressed"), "false");
    assert.equal(view.ctrlButton.getAttribute("data-lock-state"), "idle");

    // Double tap within 350ms -> LOCKED -> persists across multiple keys until tapped again.
    clock += 1000;
    view.ctrlButton.dispatch("click");
    clock += 200;
    view.ctrlButton.dispatch("click");
    assert.equal(view.ctrlButton.getAttribute("aria-pressed"), "true");
    assert.equal(view.ctrlButton.getAttribute("data-lock-state"), "locked");
    view.sendKey("c");
    view.sendKey("o");
    assert.deepEqual(sent.slice(-2), ["\x03", "\x0f"]);
    assert.equal(view.ctrlButton.getAttribute("data-lock-state"), "locked");
    view.ctrlButton.dispatch("click");
    assert.equal(view.ctrlButton.getAttribute("aria-pressed"), "false");
    assert.equal(view.ctrlButton.getAttribute("data-lock-state"), "idle");

    // Verify 3-gear directional glide thresholds.
    assert.equal(glideGearInterval(6), 0);
    assert.equal(glideGearInterval(18), 90);
    assert.equal(glideGearInterval(42), 40);
    assert.equal(glideGearInterval(72), 16);
    assert.equal(resolveGlideDirection(-30, 5, "up"), "left");
    assert.equal(resolveGlideDirection(4, 32, "up"), "down");

    view.dispose();
  } finally {
    env.restore();
  }
});

test("test_visibilitychange_wakes_backoff_timer_and_probes_stale_socket", async () => {
  const env = createDomEnv({ narrow: true, coarse: true });
  const realSetTimeout = globalThis.setTimeout;
  const realClearTimeout = globalThis.clearTimeout;
  const timers = new Map();
  let nextTimerId = 1;
  let clock = 10000;
  globalThis.setTimeout = (fn, ms) => {
    const id = nextTimerId++;
    timers.set(id, { fn, due: clock + (ms || 0) });
    return id;
  };
  globalThis.clearTimeout = (id) => {
    timers.delete(id);
  };
  const advance = (ms) => {
    clock += ms;
    for (const [id, item] of [...timers.entries()]) {
      if (item.due <= clock) {
        timers.delete(id);
        item.fn();
      }
    }
  };

  try {
    let ticketCalls = 0;
    const sockets = [];
    class FakeSocket {
      constructor(url) {
        this.url = url;
        this.readyState = 1;
        this.sent = [];
        sockets.push(this);
      }
      send(data) {
        this.sent.push(data);
      }
      close() {
        this.readyState = 3;
      }
    }

    const transport = new TerminalTransport({
      session: {
        session_id: "55555555-5555-4555-8555-555555555555",
        mode: "tmux",
      },
      api: async () => {
        ticketCalls++;
        return { ticket: "tok-" + ticketCalls, ws_path: "/ws/vps-terminal" };
      },
      WebSocketImpl: FakeSocket,
      baseUrl: "https://panel.example.com/vps",
      now: () => clock,
    });

    // 1) Disconnected in retry backoff -> visibilitychange wakes immediately with zero backoff.
    transport.state = "reconnecting";
    transport.retryAttempts = 4;
    transport.retryTimer = setTimeout(() => {}, 8000);
    env.doc.visibilityState = "visible";
    env.doc.dispatch("visibilitychange");
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(transport.retryAttempts, 0);
    assert.equal(transport.retryTimer, null);
    assert.equal(ticketCalls, 1);
    assert.equal(sockets.length, 1);

    // Open and stale (>= 2500ms) -> immediate ping + 1200ms probeTimer fails stale socket.
    sockets[0].onopen();
    transport.state = "ready";
    transport.attachment = "att-1";
    clock += 3000;
    const beforeSent = sockets[0].sent.length;
    env.win.dispatch("pageshow");
    assert.equal(sockets[0].sent.length, beforeSent + 1);
    assert.match(String(sockets[0].sent.at(-1)), /"type":"ping"/);
    assert.ok(transport.probeTimer != null);
    advance(1200);
    assert.equal(transport.state, "reconnecting");
    assert.equal(transport.lastFailureCode, "NETWORK_ERROR");

    // STOP_RETRY_CODES (e.g. HISTORY_GAP / AUTH_REVOKED) must NEVER auto-reconnect on foreground.
    const callsBeforeBlocked = ticketCalls;
    transport.fail("HISTORY_GAP", {}, true);
    assert.equal(transport.pauseReconnect, true);
    env.doc.dispatch("visibilitychange");
    env.win.dispatch("online");
    await Promise.resolve();
    assert.equal(ticketCalls, callsBeforeBlocked);

    transport.dispose();
  } finally {
    globalThis.setTimeout = realSetTimeout;
    globalThis.clearTimeout = realClearTimeout;
    env.restore();
  }
});

test("mobile HUD, transient notice, initial grid, coarse WebGL, WakeLock, swipe, hw-keyboard and SITE-36 theme-color", async () => {
  const env = createDomEnv({ narrow: true, coarse: true });
  const themeColors = [];
  let wakeReleases = 0;
  let wakeRequests = 0;
  env.win.CodePierAppearance = {
    syncThemeColor(color) {
      themeColors.push(color);
    },
  };
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      platform: "iPad",
      wakeLock: {
        async request() {
          wakeRequests++;
          return {
            addEventListener() {},
            async release() {
              wakeReleases++;
            },
          };
        },
      },
    },
  });

  try {
    globalThis.location = new URL("https://panel.example.com/vps");
    globalThis.WebSocket = class {
      constructor() {
        this.readyState = 1;
      }
      send() {}
      close() {
        this.readyState = 3;
      }
    };
    // MOB-04: measureInitialGrid clamped to limits and falls back to 100x30.
    const postedBodies = [];
    const store = new TerminalStore({
      measureInitialGrid: () => ({ cols: 48.9, rows: 22.4 }),
      api: async (url, opts) => {
        if (url.endsWith("/attach-ticket"))
          return { ticket: "tok", ws_path: "/ws/vps-terminal" };
        if (url.startsWith("/api/vps/")) return { id: "v1", name: "Edge" };
        if (url.startsWith("/api/vps-terminal/capabilities"))
          return {
            protocol_version: 1,
            available: true,
            saved_vps_host_verification: "skip",
            limits: DEFAULT_LIMITS,
          };
        if (url === "/api/vps-terminal/sessions" && opts?.method === "POST") {
          const parsed = JSON.parse(opts.body);
          postedBodies.push(parsed);
          return {
            session_id:
              postedBodies.length === 1
                ? "66666666-6666-4666-8666-666666666666"
                : "77777777-7777-4777-8777-777777777777",
            vps_id: "v1",
            vps_name: "Edge-" + postedBodies.length,
            mode: "tmux",
            state: "ready",
          };
        }
        return { sessions: [] };
      },
      runtime: {
        Terminal: class {
          constructor(opts) {
            this.options = opts;
            this.cols = 48;
            this.rows = 22;
            this.modes = {};
            this.buffer = {
              active: { type: "normal", viewportY: 0, baseY: 0 },
            };
            this.parser = { registerOscHandler: () => () => {} };
          }
          loadAddon(addon) {
            this.lastAddon = addon;
          }
          open() {}
          onData() {
            return () => {};
          }
          onBinary() {
            return () => {};
          }
          onResize() {
            return () => {};
          }
          onScroll() {
            return () => {};
          }
          onTitleChange() {
            return () => {};
          }
          attachCustomKeyEventHandler() {}
          focus() {}
          dispose() {}
        },
        FitAddon: class {
          proposeDimensions() {
            return { cols: 48, rows: 22 };
          }
        },
        SearchAddon: class {
          clearDecorations() {}
        },
        WebLinksAddon: class {},
        WebglAddon: class {
          constructor() {
            this.disposed = false;
          }
          onContextLoss() {
            return () => {};
          }
          dispose() {
            this.disposed = true;
          }
        },
      },
    });
    globalThis.ResizeObserver = class {
      observe() {}
      disconnect() {}
    };
    globalThis.MutationObserver = class {
      observe() {}
      disconnect() {}
    };

    const fallbackStore = new TerminalStore({});
    assert.deepEqual(fallbackStore.resolveInitialGrid(DEFAULT_LIMITS), {
      cols: 100,
      rows: 30,
    });

    const layout = new TerminalLayout({ store, onBack() {} });
    const host = env.makeNode("main");
    layout.mount(host);

    await store.open({ vps: { id: "v1", name: "Edge-1" }, newSession: true });
    await store.open({ vps: { id: "v1", name: "Edge-2" }, newSession: true });
    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(
      { cols: postedBodies[0].cols, rows: postedBodies[0].rows },
      { cols: 48, rows: 22 },
    );

    const [firstRec, secondRec] = [...store.records.values()];
    firstRec.state = "ready";
    secondRec.state = "ready";
    layout.render();

    // MOB-18: Only focused foreground slot holds WebGL on coarse pointer.
    assert.equal(firstRec.view.webgl, null);
    assert.ok(secondRec.view.webgl);

    // MOB-01: Tab lock badge reflects record.tabLocked and overflow menu toggles.
    secondRec.tabLocked = true;
    layout.render();
    const secondTab = layout.tabs.get(secondRec.id);
    assert.equal(secondTab.querySelector(".vt-tab-lock-badge").hidden, false);
    layout.toggleMoreMenu(true);
    assert.equal(layout.moreMenu.hidden, false);

    // MOB-20: Horizontal swipe on mobile key bar switches adjacent session tab.
    let swipeClock = 1000;
    secondRec.view.now = () => swipeClock;
    secondRec.view.mobileKeys.dispatch("touchstart", {
      touches: [{ clientX: 200, clientY: 20 }],
    });
    swipeClock += 120;
    secondRec.view.mobileKeys.dispatch("touchend", {
      changedTouches: [{ clientX: 110, clientY: 22 }],
    });
    assert.equal(store.activeId, firstRec.id);

    // MOB-03: Transient notice has close button, whereas WRITER_CONFLICT stays blocking.
    firstRec.view.showNotice("SESSION_REDRAW", "", "ready");
    assert.equal(firstRec.view.notice.getAttribute("data-transient"), "true");
    assert.equal(firstRec.view.actions.children[0].textContent, "×");
    firstRec.view.notice.dispatch("pointerenter");
    assert.equal(firstRec.view.noticeTimer, null);
    firstRec.view.actions.children[0].dispatch("click");
    assert.equal(firstRec.view.notice.hidden, true);
    firstRec.view.showNotice("WRITER_CONFLICT", "", "blocked");
    assert.equal(firstRec.view.notice.getAttribute("data-transient"), "false");

    // SITE-33: Physical keydown without viewport shrink collapses mobileKeys to pill.
    env.win.innerHeight = 844;
    globalThis.visualViewport.height = 844;
    firstRec.view.root.dispatch("keydown", { key: "ls" });
    assert.equal(firstRec.view.mobileKeys.hidden, true);
    assert.equal(firstRec.view.mobileKeysPill.hidden, false);
    firstRec.view.syncSoftKeyboardOpen(true);
    assert.equal(firstRec.view.mobileKeys.hidden, false);
    assert.equal(firstRec.view.mobileKeysPill.hidden, true);

    // MOB-19: Screen Wake Lock acquired when ready & visible, released on background.
    layout.setKeepAwake(true);
    await layout.syncWakeLock();
    assert.equal(wakeRequests, 1);
    env.doc.visibilityState = "hidden";
    await layout.syncWakeLock();
    assert.equal(wakeReleases, 1);
    env.doc.visibilityState = "visible";

    // SITE-36: Fullscreen .vt-expanded syncs theme-color #171716 and resets to null.
    await layout.toggleFullscreen();
    assert.equal(layout.root.classList.contains("vt-expanded"), true);
    assert.equal(themeColors.at(-1), "#171716");
    await layout.toggleFullscreen();
    assert.equal(themeColors.at(-1), null);

    layout.dispose();
    store.dispose();
  } finally {
    delete globalThis.ResizeObserver;
    delete globalThis.MutationObserver;
    env.restore();
  }
});
