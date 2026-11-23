import assert from "node:assert/strict";
import test from "node:test";

import { TerminalLayout } from "../web/vps-terminal/layout.mjs";
import { TerminalStore } from "../web/vps-terminal/store.mjs";
import { TerminalView } from "../web/vps-terminal/terminal-view.mjs";

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
  assert.deepEqual(one.map((vps) => vps.id), ["old", "new"]);
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
