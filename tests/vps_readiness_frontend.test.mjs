import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { esc } from "../web/core/ui.mjs";

const source = readFileSync(new URL("../web/vps.js", import.meta.url), "utf8");
const endpoint = "/api/vps/saved/readiness";
const job = (state, fields = {}) => ({ id: "job-fixture", state, connection_revision: 3, host_verification: "skip", ...fields });
function fixture(api) {
  const requests = [];
  const query = { value: "" };
  const status = { textContent: "" };
  const button = { disabled: false };
  const more = { open: true };
  const card = {
    isConnected: true, hidden: false, dataset: { vpsCard: "saved" },
    querySelector(selector) {
      return { "[data-vps-env-status]": status, '[data-vps-action="readiness"]': button, ".vps-card-more": more }[selector];
    },
  };
  const S = {
    page: "vps", session: { user_id: "account" }, space_id: "space",
    vps: [{ id: "saved", enabled: true, readiness_available: true, project_ids: [] }],
  };
  const document = { hidden: false, addEventListener() {} };
  const ctx = vm.createContext({
    S, document, window: {}, AbortController, setTimeout, clearTimeout, esc,
    $$: () => [card], $: (selector) => selector === "#vps-query" ? query : null, icon: () => "",
    api: async (url, opts) => {
      requests.push({ url, method: opts?.method || "GET", signal: opts?.signal });
      return api(requests.at(-1));
    },
  });
  vm.runInContext(source, ctx);
  vm.runInContext('vpsReadinessBackfill = { available: true, owner: vpsTerminalIdentity() }', ctx);
  ctx.vpsReadinessDelay = async () => true;
  return { ctx, requests, S, status, button, document, more, card, query };
}

test("feature is absent without explicit API negotiation", () => {
  const { ctx } = fixture(() => assert.fail("unexpected request"));
  assert.equal(ctx.vpsReadinessHTML({ enabled: true }), "");
  assert.equal(ctx.vpsReadinessHTML({ enabled: false, readiness_available: true }), "");
  assert.match(ctx.vpsReadinessHTML({ id: "saved", readiness_available: true }), /检查环境/);
  assert.match(ctx.vpsReadinessHTML({ id: "saved", readiness: job("ready") }), /tmux 已就绪/);
});

test("one explicit POST, then identical-job polling through check/install/ready", async () => {
  let deliver;
  const results = [job("checking"), { readiness: job("installing") }, {
    readiness: job("ready", { reason_code: "INSTALLED", tmux_version: "3.5a" }),
  }];
  const f = fixture(({ method }) => method === "POST"
    ? new Promise((resolve) => { deliver = resolve; })
    : results.shift());
  const initial = f.ctx.vpsReadinessWatch("saved", true);
  await f.ctx.vpsReadinessWatch("saved", true);
  assert.equal(f.requests.length, 1);
  assert.equal(f.button.disabled, true);
  deliver({ readiness: job("queued") });
  await initial;
  assert.deepEqual(f.requests.map(x => x.method), ["POST", "GET", "GET", "GET"]);
  assert.equal(f.requests.every(x => x.url === endpoint), true);
  assert.equal(f.status.textContent, "tmux 已安装并验证 · 3.5a");
  assert.equal(f.button.disabled, false);
});

test("opening More observes a conflicting job without accepting it or POSTing", async () => {
  const entries = [job("running"), job("ready", { connection_revision: 4 })];
  const f = fixture(() => entries.shift());
  await f.ctx.vpsReadinessWatch("saved");
  assert.deepEqual(f.requests.map(x => x.method), ["GET", "GET"]);
  assert.match(f.status.textContent, /任务已变化/);
  assert.equal(f.S.vps[0].readiness.connection_revision, 3);
});

test("closing More aborts observation and discards the late response", async () => {
  let deliver;
  const f = fixture(() => new Promise((resolve) => { deliver = resolve; }));
  const pending = f.ctx.vpsReadinessWatch("saved");
  f.more.open = false;
  f.ctx.vpsReadinessCancelInvisible();
  assert.equal(f.requests[0].signal.aborted, true);
  deliver({ readiness: job("ready") });
  await pending;
  assert.equal(f.S.vps[0].readiness, undefined);
  assert.equal(f.requests.length, 1);
});

test("identity change discards result and permission failures do not retry POST", async () => {
  let deliver;
  const f = fixture(() => new Promise((resolve) => { deliver = resolve; }));
  const pending = f.ctx.vpsReadinessWatch("saved", true);
  f.S.space_id = "another";
  deliver({ readiness: job("ready") });
  await pending;
  assert.equal(f.S.vps[0].readiness, undefined);
  assert.equal(f.requests.length, 1);

  const denied = fixture(() => { throw Object.assign(new Error("raw server detail"), { status: 403 }); });
  await denied.ctx.vpsReadinessWatch("saved", true);
  assert.deepEqual(denied.requests.map(x => x.method), ["POST"]);
  assert.equal(denied.status.textContent, "没有检查或安装权限");
  assert.equal(denied.ctx.vpsReadinessText(job("failed", { reason_code: "HOST_KEY_UNVERIFIED" })), "旧版连接检查未完成，请更新面板和连接电脑后重试；未安装");
});


function seedBackfill(f, extras = {}) {
  f.ctx.backfillSeed = extras;
  return vm.runInContext(
    'vpsReadinessBackfill = { owner: vpsTerminalIdentity(), available: true, fleetPolicy: "skip", ...backfillSeed }',
    f.ctx,
  );
}

test("API job envelope feeds the existing individual readiness display", async () => {
  const f = fixture(() => ({ available: true, job: job("ready") }));
  await f.ctx.vpsReadinessWatch("saved");
  assert.equal(f.status.textContent, "tmux 已就绪");
  assert.equal(f.S.vps[0].readiness.id, "job-fixture");
});

test("fleet actions require exact negotiated capability", () => {
  const f = fixture(() => assert.fail("unexpected network operation"));
  assert.equal(f.ctx.vpsReadinessBackfillHTML({ available: false }), "");
  const html = f.ctx.vpsReadinessBackfillHTML({ available: true, fleetPolicy: "skip" });
  assert.match(html, /当前空间 · 批量终端环境/);
  assert.match(html, /不受项目筛选影响/);
  assert.match(html, /扫描全部 SSH/);
  assert.match(html, /disabled>一键安装/);
  vm.runInContext("vpsReadinessBackfill = null", f.ctx);
  assert.equal(f.ctx.vpsReadinessHTML({ id: "saved", readiness_available: true }), "");
  assert.equal(f.requests.length, 0);
});

test("fleet scan posts once without enabling or initiating installation", async () => {
  let deliver;
  const f = fixture(() => new Promise((resolve) => { deliver = resolve; }));
  const state = seedBackfill(f);
  const first = f.ctx.vpsReadinessBackfillStart();
  await f.ctx.vpsReadinessBackfillStart();
  await f.ctx.vpsReadinessBackfillStart(true);
  assert.equal(state.pending, true);
  assert.deepEqual(f.requests.map(({ url, method }) => [url, method]), [["/api/vps/readiness/scan", "POST"]]);
  deliver({ queued_count: 7 });
  await first;
  assert.match(state.message, /7 台加入只读扫描队列/);
  assert.equal(state.awaiting, true);
  assert.equal(f.requests.length, 1);
});

test("fleet install is a separate request and disabled until missing is confirmed", async () => {
  const f = fixture(() => ({ queued_count: 1 }));
  const state = seedBackfill(f, { result: { eligible: 0, staleMissing: 1 } });
  await f.ctx.vpsReadinessBackfillStart(true);
  assert.equal(f.requests.length, 0);
  state.result.eligible = 1;
  await f.ctx.vpsReadinessBackfillStart(true);
  assert.deepEqual(f.requests.map(({ url, method }) => [url, method]), [["/api/vps/readiness/install", "POST"]]);
  assert.match(state.message, /1 台加入安装队列/);
});

test("summary separates positive missing from failed trust and shows each phase", () => {
  const { ctx } = fixture(() => assert.fail("unexpected request"));
  const j = (state, reason_code, intent) => ({
    enabled: true, readiness_available: true, readiness: job(state, { reason_code, intent, install_eligible: state === "missing" }),
  });
  const result = ctx.vpsFleetResult([
    j("ready"), j("missing", "TMUX_MISSING", "scan"), j("queued"),
    j("running", undefined, "scan"), j("running", undefined, "install"),
    j("failed", "HUB_SSH_TRUST_MIGRATION_REQUIRED"), j("failed", "HUB_SSH_TRUST_MIGRATION_REQUIRED"),
    { enabled: true, readiness_available: true },
    { enabled: false, readiness_available: true, readiness: job("ready") },
  ]);
  assert.equal(result.counts, "就绪 1 · 缺少 1 · 排队 1 · 扫描中 1 · 安装中 1 · 失败 2 · 未检查 1");
  assert.equal(result.reasons, "2 台：旧版扫描未完成，请重新扫描");
  assert.doesNotMatch(ctx.vpsReadinessBackfillHTML({ available: true, fleetPolicy: "skip", result }), /指纹|主机身份核验/);
  assert.equal(result.eligible, 1);
  assert.equal(result.staleMissing, 0);
  assert.equal(result.active, true);
});

test("verification-only retries and unchecked install outcomes remain distinct from installs", () => {
  const { ctx } = fixture(() => assert.fail("unexpected request"));
  const awaiting = job("failed", { reason_code: "INSTALL_OUTCOME_UNCONFIRMED" });
  const queued = job("queued", { intent: "install", reason_code: "INSTALL_OUTCOME_UNCONFIRMED" });
  const checking = job("running", { intent: "install", verification_only: true });
  const installing = job("installing", { intent: "install", verification_only: true });
  const transient = job("queued", { intent: "install", reason_code: "VPS_PROBE_INTERRUPTED" });
  const failed = job("failed", { reason_code: "TMUX_INSTALL_FAILED", verification_only: true });
  const onlyVerify = job("missing", { verification_only: true, install_eligible: true });
  const items = [awaiting, queued, checking, installing, transient, failed, onlyVerify].map((readiness, i) => ({
    id: `server-${i}`, name: `VPS ${i}`, enabled: true, readiness,
  }));
  const result = ctx.vpsFleetResult(items);
  assert.equal(result.counts, "就绪 0 · 缺少 0 · 排队 1 · 扫描中 0 · 安装中 1 · 复查 2 · 待核验 2 · 失败 1 · 未检查 0");
  assert.match(result.reasons, /安装结果待核验.*（VPS 0、VPS 6）/);
  assert.match(result.reasons, /tmux 安装失败.*（VPS 5）/);
  assert.equal(result.active, true);
  assert.equal(result.eligible, 0);
  assert.match(ctx.vpsReadinessText(queued), /已排队复查安装结果/);
  assert.match(ctx.vpsReadinessText(checking), /正在复查安装结果/);
  assert.match(ctx.vpsReadinessText(installing), /正在安装 tmux/);
  assert.match(ctx.vpsReadinessText(transient), /中断，已排队重试/);

  const terminalResult = ctx.vpsFleetResult([items[0], items[6]]);
  const buttons = ctx.vpsFleetButtons({ available: true, fleetPolicy: "skip", result: terminalResult });
  assert.equal(buttons.installDisabled, true);
  assert.equal(buttons.scanDisabled, false);
});

test("specific readiness outcomes identify an interrupted check, unsupported path, or old tmux", () => {
  const { ctx } = fixture(() => assert.fail("unexpected request"));
  for (const [reason_code, expected] of [
    ["TMUX_LOCATION_UNSUPPORTED", /不支持其安装路径/],
    ["TMUX_VERSION_UNSUPPORTED", /低于 2\.6/],
    ["VPS_PROBE_INTERRUPTED", /检查超时或中断/],
    ["VPS_ENV_UNSUPPORTED", /包管理器不支持自动安装/],
  ]) {
    assert.match(ctx.vpsReadinessText(job("failed", { reason_code })), expected);
  }
});

test("fleet failure details expose bounded, escaped names and no connection details", () => {
  const f = fixture(() => assert.fail("unexpected request"));
  const longName = "長".repeat(80);
  const unsafeName = '<img src=x onerror="alert(1)">&';
  const reason_code = "VPS_PROBE_INTERRUPTED";
  const items = [unsafeName, longName, ...Array.from({ length: 8 }, (_, i) => `Probe-${i}`)].map((name, i) => ({
    id: `stored-${i}`, name, username: "secret-user", host: "secret-host", notes: "secret-notes",
    readiness: job("failed", { reason_code }),
  }));
  items.push(...Array.from({ length: 9 }, (_, i) => ({
    name: `Installer-${i}`, readiness: job("failed", { reason_code: "TMUX_INSTALL_FAILED" }),
  })), { name: "Hidden by fleet name cap", readiness: job("failed", { reason_code: "SSH_AUTH_FAILED" }) });
  const result = f.ctx.vpsFleetResult(items);
  const html = f.ctx.vpsReadinessBackfillHTML({ available: true, fleetPolicy: "skip", result });
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;&amp;/);
  assert.doesNotMatch(html, /<img|secret-|Hidden by fleet name cap/);
  assert.match(result.reasons, /Probe-3、另 4 台/);
  assert.doesNotMatch(result.reasons, /Probe-4|Installer-6/);
  assert.match(result.reasons, /Installer-5、另 3 台/);
  assert.ok(result.reasons.includes("長".repeat(47) + "…"));
  assert.doesNotMatch(result.reasons, /stored-0|secret-/);

  const state = seedBackfill(f, { result });
  const reasonElement = { textContent: "", hidden: true };
  f.ctx.$ = (selector) => selector === "[data-vps-fleet-reasons]" ? reasonElement : null;
  f.ctx.vpsReadinessBackfillPaint(state);
  assert.equal(reasonElement.textContent, result.reasons);
  assert.equal(reasonElement.hidden, false);
});

test("known stale missing remains visible but cannot initiate installation", () => {
  const { ctx } = fixture(() => assert.fail("unexpected request"));
  const result = ctx.vpsFleetResult([{ enabled: true, readiness_available: true, readiness: job("missing", { install_eligible: false }) }]);
  assert.equal(result.eligible, 0);
  assert.equal(result.staleMissing, 1);
  assert.match(ctx.vpsReadinessBackfillHTML({ available: true, fleetPolicy: "skip", result }), /1 台的缺失结果已过期或不可安装/);
  assert.equal(ctx.vpsFleetButtons({ result }).installDisabled, true);
});

test("fleet watcher follows inventory GETs, settles missing, and never installs", async () => {
  const missing = { id: "saved", enabled: true, readiness_available: true, readiness: job("missing", { install_eligible: true }) };
  const f = fixture(() => ({
    readiness_manual_actions: true, readiness_fleet_host_verification: "skip", vps: [missing], next_offset: null,
  }));
  const state = seedBackfill(f, { awaiting: true });
  const counts = { textContent: "" };
  f.ctx.$ = (selector) => selector === "[data-vps-fleet-counts]" ? counts : null;
  await f.ctx.vpsObserveFleet(state);
  assert.deepEqual(f.requests.map(({ url, method }) => [url, method]), [["/api/vps?limit=200&offset=0", "GET"]]);
  assert.equal(state.awaiting, false);
  assert.equal(state.result.eligible, 1);
  assert.match(counts.textContent, /缺少 1/);
});

function virtualReadinessClock(f) {
  let now = 0;
  let nextId = 0;
  const timers = new Map();
  f.ctx.setTimeout = (callback, delay) => {
    const id = ++nextId;
    timers.set(id, { callback, at: now + delay });
    return id;
  };
  f.ctx.clearTimeout = (id) => timers.delete(id);
  f.ctx.vpsReadinessDelay = async (delay, signal) => {
    now += delay;
    for (const [id, timer] of timers) {
      if (timer.at <= now) {
        timers.delete(id);
        timer.callback();
      }
    }
    return !signal.aborted;
  };
  return { now: () => now, timers };
}

test("a visible per-VPS observer can see success after the default retry backoff and probe time", async () => {
  const f = fixture(() => clock.now() < 375_000
    ? job("queued", { reason_code: "VPS_PROBE_INTERRUPTED" })
    : job("ready"));
  const clock = virtualReadinessClock(f);
  await f.ctx.vpsReadinessWatch("saved");
  assert.ok(clock.now() >= 375_000);
  assert.equal(f.status.textContent, "tmux 已就绪");
  assert.equal(f.button.disabled, false);
  assert.equal(clock.timers.size, 0);
  assert.ok(f.requests.every(({ method, signal }) => method === "GET" && !signal.aborted));
});

test("fleet observation stops at its deadline without unlocking unsafe actions; a fresh read can recover", async () => {
  const installing = { id: "saved", readiness: job("installing", { intent: "install" }) };
  const ready = { id: "saved", readiness: job("ready") };
  let items = [installing];
  const f = fixture(() => ({
    readiness_manual_actions: true, readiness_fleet_host_verification: "skip", vps: items, next_offset: null,
  }));
  const clock = virtualReadinessClock(f);
  const state = seedBackfill(f, { result: f.ctx.vpsFleetResult(items) });
  const counts = { textContent: "" };
  const scan = { disabled: false };
  const install = { disabled: false };
  f.ctx.$ = (selector) => ({
    "[data-vps-fleet-counts]": counts,
    '[data-vps-action="readiness-backfill"]': scan,
    '[data-vps-action="readiness-backfill-install"]': install,
  })[selector] || null;
  await f.ctx.vpsObserveFleet(state);
  assert.ok(clock.now() >= 720_000 && clock.now() < 727_000);
  assert.match(state.message, /刷新页面查看最新进度/);
  assert.equal(scan.disabled, true);
  assert.equal(install.disabled, true);
  assert.equal(clock.timers.size, 0);
  assert.ok(f.requests.every(({ method, signal }) => method === "GET" && signal.aborted));
  const previousRequests = f.requests.length;
  items = [ready];
  await f.ctx.vpsObserveFleet(state);
  assert.equal(f.requests.length, previousRequests + 1);
  assert.equal(f.requests.at(-1).method, "GET");
  assert.match(counts.textContent, /就绪 1/);
  assert.equal(scan.disabled, false);
  assert.equal(install.disabled, true);
});

test("fleet drops a result from another Space or unknown submission outcome", async () => {
  let deliver;
  const f = fixture(() => new Promise((resolve) => { deliver = resolve; }));
  const previous = seedBackfill(f);
  const first = f.ctx.vpsReadinessBackfillStart();
  f.S.space_id = "new-space";
  const current = seedBackfill(f);
  deliver({ queued_count: 12 });
  await first;
  assert.equal(previous.awaiting, undefined);
  assert.equal(current.message, undefined);

  const uncertain = fixture(() => { throw new Error("connection interrupted"); });
  const state = seedBackfill(uncertain);
  await uncertain.ctx.vpsReadinessBackfillStart();
  assert.equal(state.uncertain, true);
  assert.match(state.message, /无需重复提交/);
  await uncertain.ctx.vpsReadinessBackfillStart();
  assert.equal(uncertain.requests.length, 1);
});


test("filtering an open card cancels observation; revealing it checks again without installing", async () => {
  let late;
  const f = fixture(() => f.requests.length === 1
    ? new Promise((resolve) => { late = resolve; })
    : job("ready"));
  const first = f.ctx.vpsReadinessWatch("saved");
  f.query.value = "no match";
  f.ctx.vpsFilter();
  assert.equal(f.card.hidden, true);
  assert.equal(f.requests[0].signal.aborted, true);
  // This may settle before the hidden card is revealed.
  late(job("failed"));
  await first;
  f.query.value = "";
  f.ctx.vpsFilter();
  await new Promise(setImmediate);
  assert.deepEqual(f.requests.map(({ method }) => method), ["GET", "GET"]);
  assert.equal(f.status.textContent, "tmux 已就绪");
  assert.equal(f.button.disabled, false);
});

test("a stale observation cannot paint the replacement inventory record", async () => {
  let late;
  const f = fixture(() => new Promise((resolve) => { late = resolve; }));
  const initial = f.ctx.vpsReadinessWatch("saved");
  const old = f.S.vps[0];
  f.S.vps = [{ ...old, readiness: job("ready", { connection_revision: 4 }) }];
  f.status.textContent = "新连接";
  late(job("failed"));
  await initial;
  assert.equal(old.readiness, undefined);
  assert.equal(f.status.textContent, "新连接");
  assert.equal(f.S.vps[0].readiness.connection_revision, 4);
});


test("an uncertain POST reconciles a newer job with GET only", async () => {
  let release;
  const f = fixture(({method}) => {
    if (method === "POST") throw new Error("receipt lost");
    return new Promise((resolve) => { release = resolve; });
  });
  const state = seedBackfill(f, { result: f.ctx.vpsFleetResult([]) });
  const counts = {textContent: ""};
  f.ctx.$ = (selector) => selector === "[data-vps-fleet-counts]" ? counts : null;
  await f.ctx.vpsReadinessBackfillStart();
  assert.equal(state.uncertain, true);
  await new Promise(setImmediate);
  release({
    readiness_manual_actions: true, readiness_fleet_host_verification: "skip", next_offset: null,
    vps: [{id: "saved", enabled: true, readiness_available: true, readiness: job("ready", { updated: 7 })}],
  });
  await new Promise(setImmediate);
  assert.deepEqual(f.requests.map(({method}) => method), ["POST", "GET"]);
  assert.equal(state.uncertain, false);
  assert.match(counts.textContent, /就绪 1/);
});

test("ambiguous submission with unchanged inventory remains unknown after four GETs", async () => {
  const f = fixture(() => ({readiness_manual_actions: true, readiness_fleet_host_verification: "skip", next_offset: null, vps: []}));
  const state = seedBackfill(f, { uncertain: true, ambiguityBaseline: "" });
  f.ctx.$ = (selector) => selector === "[data-vps-fleet-counts]" ? {textContent: ""} : null;
  await f.ctx.vpsObserveFleet(state);
  assert.equal(state.uncertain, true);
  assert.match(state.message, /尚无新的任务记录/);
  assert.deepEqual(f.requests.map(({method}) => method), ["GET", "GET", "GET", "GET"]);
});

test("fleet polling tolerates a bounded transient inventory read failure", async () => {
  const f = fixture(() => {
    if (f.requests.length === 1) throw new Error("temporary network issue");
    return {readiness_manual_actions: true, readiness_fleet_host_verification: "skip", next_offset: null, vps: []};
  });
  const state = seedBackfill(f, { awaiting: true });
  f.ctx.$ = (selector) => selector === "[data-vps-fleet-counts]" ? {textContent: ""} : null;
  await f.ctx.vpsObserveFleet(state);
  assert.equal(state.awaiting, false);
  assert.deepEqual(f.requests.map(({method}) => method), ["GET", "GET"]);
});

test("a positive per-VPS or old scan must be confirmed by bulk scan before install", async () => {
  const f = fixture(() => assert.fail("must not submit"));
  const result = f.ctx.vpsFleetResult([{ enabled: true, readiness_available: true, readiness: job("missing", {
    host_verification: "required", install_eligible: true,
  }) }]);
  assert.equal(result.needsBulkRescan, 1);
  assert.equal(result.eligible, 0);
  assert.equal(result.staleMissing, 0);
  const state = seedBackfill(f, { result });
  assert.match(f.ctx.vpsReadinessBackfillHTML(state), /需要批量重扫/);
  await f.ctx.vpsReadinessBackfillStart(true);
  assert.equal(f.requests.length, 0);
});

test("stale backend cannot silently run a differently scoped bulk scan", async () => {
  const f = fixture(() => assert.fail("must not submit"));
  const state = seedBackfill(f, { fleetPolicy: null });
  assert.equal(f.ctx.vpsReadinessBackfillHTML(state), "");
  await f.ctx.vpsReadinessBackfillStart();
  assert.equal(f.requests.length, 0);
});

test("saved connection accepts only fixed managed-VPS capability and reports legacy refusals honestly", () => {
  const { ctx } = fixture(() => assert.fail("unexpected request"));
  assert.equal(ctx.vpsSavedSupportsConnection({host_key_policy: "accept-new"}), false);
  assert.equal(ctx.vpsSavedSupportsConnection({saved_vps_host_verification: "skip"}), true);
  const output = { textContent: "" }, status = { textContent: "" };
  ctx.vpsPaintCheckReceipt({state: "failed", error: {code: "SSH_HOST_KEY_CHANGED"}, output: "raw obsolete refusal"}, false, output, status);
  assert.match(status.textContent, /请更新后重试/);
  assert.doesNotMatch(status.textContent + output.textContent, /指纹|raw obsolete refusal|成功/);
  ctx.vpsPaintCheckReceipt({state: "succeeded", output: "real response"}, true, output, status);
  assert.match(status.textContent, /成功/);
  assert.equal(output.textContent, "real response");
});
