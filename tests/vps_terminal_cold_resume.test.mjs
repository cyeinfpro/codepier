import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { TerminalStore } from "../web/vps-terminal/store.mjs";

const source = readFileSync(new URL("../web/vps.js", import.meta.url), "utf8");
const sid = "99".repeat(16);
const running = { session_id: sid, vps_id: "saved-vps", state: "ready" };

function panel(api) {
  const routes = [];
  const toasts = [];
  const S = {
    session: { user_id: "same-account" },
    space_id: "scope",
    page: "vps",
  };
  const context = vm.createContext({
    S,
    api,
    window: {},
    document: { addEventListener() {} },
    $: () => null,
    $$: () => [],
    icon: () => "",
    esc: String,
    navigate: async (to) => routes.push(to),
    toast: (message) => toasts.push(message),
  });
  vm.runInContext(source, context);
  return { context, S, routes, toasts };
}

test("other browser waits for session list and opens the exact existing terminal", async () => {
  let deliver;
  const calls = [];
  const fixture = panel((url) => {
    calls.push(url);
    assert.equal(url, "/api/vps-terminal/sessions");
    return new Promise((resolve) => {
      deliver = resolve;
    });
  });
  const background = fixture.context.vpsLoadPresence();
  const tap = fixture.context.vpsOpenTerminal(running.vps_id);
  assert.equal(fixture.routes.length, 0);
  assert.equal(calls.length, 1, "share the in-flight request");
  deliver({ sessions: [running] });
  await Promise.all([background, tap]);
  assert.deepEqual(fixture.routes, ["vps?view=terminal&session=" + sid]);
});

test("presence unavailable never converts resume into another shell", async () => {
  const calls = [];
  const f = panel(async (url) => {
    calls.push(url);
    throw new Error("offline");
  });
  await f.context.vpsOpenTerminal(running.vps_id);
  assert.deepEqual(calls, ["/api/vps-terminal/sessions"]);
  assert.deepEqual(f.routes, []);
  assert.match(f.toasts[0], /无法确认.*运行的终端/);
});

test("one Space's response cannot restore a terminal after scope change", async () => {
  let deliver;
  const f = panel(
    () =>
      new Promise((resolve) => {
        deliver = resolve;
      }),
  );
  const pending = f.context.vpsOpenTerminal(running.vps_id);
  f.S.space_id = "another-scope";
  deliver({ sessions: [running] });
  await pending;
  assert.equal(f.S.vpsPresence, undefined);
  assert.deepEqual(f.routes, []);
});

test("direct session cold restore only calls the session endpoint, even with synthetic project", async () => {
  const calls = [];
  const store = Object.create(TerminalStore.prototype);
  store.records = new Map();
  store.version = 1;
  store.api = async (path) => {
    calls.push(path);
    assert.equal(path, "/api/vps-terminal/sessions/" + sid);
    return {
      ...running,
      id: sid,
      project_id: "direct:synthetic",
      limits: { frame_bytes: 24576 },
    };
  };
  store.adopt = (session, { limits }) => {
    assert.equal(limits.frame_bytes, 24576);
    store.records.set(session.session_id, { session });
    return store.records.get(session.session_id);
  };
  store.activate = (id) => assert.equal(id, sid);
  const session = await store.restore(sid, { isCurrent: () => true });
  assert.equal(session.session_id, sid);
  assert.equal(calls.length, 1);
});
