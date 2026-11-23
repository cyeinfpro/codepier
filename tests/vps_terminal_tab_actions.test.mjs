import assert from "node:assert/strict";
import test from "node:test";
import { TerminalStore } from "../web/vps-terminal/store.mjs";

const record = (id, props = {}) => ({
  id,
  label: id,
  state: "ready",
  session: {},
  view: { stateChanged() {}, dispose() {} },
  transport: { detach() {}, dispose() {} },
  ...props,
});

test("tab pin denies direct close/detach but does not block an explicit end", async () => {
  const calls = [];
  const store = new TerminalStore({
    api: async (path) => {
      calls.push(path);
      return { state: "close_unknown" };
    },
  });
  const pinned = record("pinned", { tabLocked: true });
  store.records.set(pinned.id, pinned);
  await store.close(pinned.id, { dismiss: true });
  store.detach(pinned.id);
  assert.deepEqual(calls, []);
  await store.close(pinned.id);
  assert.deepEqual(calls, ["/api/vps-terminal/sessions/pinned/close"]);
  assert.equal(store.records.get(pinned.id), pinned);
  assert.equal(pinned.state, "close_unknown");
});

test("remote close from an inactive tab waits for confirmation and preserves active split", async () => {
  const calls = [];
  let release;
  const store = new TerminalStore({
    api: (path, opts) => {
      if (!opts) return Promise.resolve({ sessions: [] });
      calls.push([path, JSON.parse(opts.body).request_id]);
      return new Promise((resolve) => { release = resolve; });
    },
    onNavigate: () => assert.fail("Closing a background tab must not change route"),
  });
  const left = record("left");
  const right = record("right");
  const target = record("target");
  target.transport.dispose = () => calls.push("dispose");
  target.transport.detach = () => assert.fail("Remote close must not send detach");
  for (const item of [left, right, target]) store.records.set(item.id, item);
  store.activeId = left.id;
  store.secondaryId = right.id;
  store.focusedId = right.id;
  const first = store.close(target.id, { dismiss: true });
  await store.close(target.id, { dismiss: true });
  assert.equal(calls.length, 1);
  release({ state: "close_unknown" });
  await first;
  assert.equal(store.records.has(target.id), true);
  const second = store.close(target.id, { dismiss: true });
  assert.deepEqual(calls[0], calls[1]);
  // Simulate the ordered terminal message and an independently late HTTP result.
  target.state = "closed";
  store.dismissEnded(target.id);
  release({ state: "close_unknown" });
  await second;
  assert.equal(store.records.has(target.id), false);
  assert.equal(store.activeId, left.id);
  assert.equal(store.secondaryId, right.id);
  assert.equal(store.focusedId, right.id);
  assert.equal(calls[2], "dispose");
});
