import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/vps.js", import.meta.url), "utf8");
const fixture = {
  id: "vps-1",
  name: "Example VPS",
  host: "example.invalid",
  port: 22,
  username: "fixture",
  enabled: true,
  project_ids: [],
  projects: [],
};

function load(overrides = {}) {
  const context = vm.createContext({
    document: { addEventListener() {} },
    window: {},
    S: { session: { user_id: "fixture" }, space_id: "space-1", renderSeq: 4 },
    icon: () => "",
    esc: String,
    $: () => null,
    $$: () => [],
    ...overrides,
  });
  vm.runInContext(source, context, { filename: "web/vps.js" });
  return context;
}

function openingButton(html, action) {
  return html.match(new RegExp('<button[^>]*data-vps-action="' + action + '"[^>]*>'))?.[0] || "";
}

test("authorized, unavailable terminals are not counted as ready; connection checks are separate", () => {
  const panel = load();
  for (const code of ["AGENT_OFFLINE", "AGENT_UPGRADE_REQUIRED", "FEATURE_DISABLED", "NO_TERMINAL_AGENT", "SHELL_DISABLED"]) {
    const vps = {
      ...fixture,
      terminal: { eligible: true, available: false, check_ready: false, direct: true, reason_code: code },
    };
    const actions = panel.vpsActionsHTML(vps);
    assert.ok(panel.vpsTerminalReason(vps));
    assert.equal(panel.vpsChannelLabel(vps), "当前不可直连");
    assert.match(panel.vpsCardHTML(vps, []), /未就绪/);
    assert.match(openingButton(actions, "terminal"), /disabled/);
    assert.match(openingButton(actions, "check"), /disabled/);
    if (code !== "FEATURE_DISABLED") assert.match(actions, /电脑管理/);
  }
  const probeOnly = {
    ...fixture,
    terminal: { eligible: true, available: false, check_ready: true, reason_code: "AGENT_UPGRADE_REQUIRED" },
  };
  const actions = panel.vpsActionsHTML(probeOnly);
  assert.match(openingButton(actions, "terminal"), /disabled/);
  assert.doesNotMatch(openingButton(actions, "check"), /disabled/);
  const ready = { ...fixture, terminal: { eligible: true, available: true, check_ready: true } };
  assert.doesNotMatch(openingButton(panel.vpsActionsHTML(ready), "terminal"), /disabled/);
});

test("inventory loads saved sessions before xterm, deduplicates local updates, and labels only actual tmux sessions", async () => {
  const direct = { id: "one", vps_id: fixture.id, state: "attached", mode: "direct" };
  const tmux = { session_id: "two", vps_id: fixture.id, state: "detached", mode: "tmux" };
  const panel = load({ api: async () => ({ sessions: [direct, tmux] }) });
  await panel.vpsLoadPresence(4);
  assert.equal(panel.vpsTerminalCount(), 2);
  assert.equal((panel.vpsActiveSessionsInnerHTML().match(/tmux 持久/g) || []).length, 1);
  panel.window.CodePierVpsTerminal = { list: () => [{ ...direct, state: "closed" }, tmux] };
  vm.runInContext("vpsTerminalOwner = vpsTerminalIdentity()", panel);
  assert.equal(panel.vpsActiveSessions(fixture.id).length, 1);
  assert.equal(panel.vpsTerminalCount(), 1);
});

test("a late session list cannot populate another Space, nor survive logout", async () => {
  let finish;
  const panel = load({ api: () => new Promise((resolve) => { finish = resolve; }) });
  const before = panel.vpsLoadPresence(4);
  panel.S.space_id = "space-2";
  finish({ sessions: [{ id: "one", vps_id: fixture.id, state: "attached" }] });
  await before;
  assert.equal(panel.vpsTerminalCount(), 0);
  panel.api = async () => ({ sessions: [{ id: "two", vps_id: fixture.id, state: "attached" }] });
  await panel.vpsLoadPresence(4);
  assert.equal(panel.vpsTerminalCount(), 1);
  panel.vpsTerminalForget();
  assert.equal(panel.vpsTerminalCount(), 0);
});

test("save-and-open does not override navigation that occurred while refreshing", async () => {
  let resume;
  const save = {}, open = {}, error = {};
  const form = { reportValidity: () => true, reset() {}, addEventListener() {}, elements: { enabled: { checked: true }, password: {} } };
  const dialog = { isConnected: true, classList: { add() {} }, dataset: {} };
  const selectors = { "#vps-form": form, "#vps-save": save, "#vps-save-terminal": open, "#vps-form-error": error };
  let opened = 0, navigated = 0;
  const panel = load({
    S: { session: { user_id: "fixture" }, space_id: "space-1", page: "vps", projects: [] },
    $: (selector) => selectors[selector],
    loadBasics: async () => {},
    modal: () => dialog,
    closeModal: () => { dialog.isConnected = false; },
    toast() {},
    FormData: class { *[Symbol.iterator]() { yield ["port", "22"]; } },
    busy: async (_button, fn) => fn(),
    api: async () => ({ id: fixture.id, enabled: true }),
    renderPage: () => new Promise((resolve) => { resume = resolve; }),
    navigate: async () => { navigated++; },
  });
  panel.vpsOpenTerminal = async () => { opened++; };
  await panel.vpsEdit();
  const submitted = open.onclick();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof resume, "function");
  panel.S.page = "projects";
  resume();
  await submitted;
  assert.equal(opened, 0);
  assert.equal(navigated, 0);
});
