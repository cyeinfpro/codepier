import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/vps.js", import.meta.url), "utf8");
const endpoint = "/api/vps/saved/readiness";
const job = (state, fields = {}) => ({ id: "job-fixture", state, connection_revision: 3, ...fields });
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
    S, document, window: {}, AbortController, setTimeout, clearTimeout, esc: String,
    $$: () => [card], $: (selector) => selector === "#vps-query" ? query : null, icon: () => "",
    api: async (url, opts) => {
      requests.push({ url, method: opts?.method || "GET", signal: opts?.signal });
      return api(requests.at(-1));
    },
  });
  vm.runInContext(source, ctx);
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
  assert.equal(denied.ctx.vpsReadinessText(job("failed", { reason_code: "HOST_KEY_UNVERIFIED" })), "主机指纹未核验；未安装");
});


function seedBackfill(f, extras = {}) {
  f.ctx.backfillSeed = extras;
  return vm.runInContext(
    'vpsReadinessBackfill = { owner: vpsTerminalIdentity(), available: true, ...backfillSeed }',
    f.ctx,
  );
}

test("API job envelope feeds the existing individual readiness display", async () => {
  const f = fixture(() => ({ available: true, job: job("ready") }));
  await f.ctx.vpsReadinessWatch("saved");
  assert.equal(f.status.textContent, "tmux 已就绪");
  assert.equal(f.S.vps[0].readiness.id, "job-fixture");
});

test("fleet controls require explicit admin availability and show authoritative activation", () => {
  const f = fixture(() => assert.fail("unexpected network operation"));
  assert.equal(f.ctx.vpsReadinessBackfillHTML({ available: false }), "");
  assert.match(f.ctx.vpsReadinessBackfillHTML({ available: true }), /检查全部环境/);
  assert.match(f.ctx.vpsReadinessBackfillHTML({ available: true, enabled: true }), /disabled>已开启/);
  assert.equal(f.requests.length, 0);
});

test("fleet submits once per activation, reports only scoped queued count and performs no polling", async () => {
  let deliver;
  const f = fixture(() => new Promise((resolve) => { deliver = resolve; }));
  const state = seedBackfill(f);
  const first = f.ctx.vpsReadinessBackfillStart();
  await f.ctx.vpsReadinessBackfillStart();
  assert.equal(state.pending, true);
  assert.deepEqual(f.requests.map(({ url, method }) => [url, method]), [["/api/vps/readiness/backfill", "POST"]]);
  deliver({ enabled: true, queued_count: 7 });
  await first;
  assert.match(state.message, /7 台加入后台检查/);
  assert.equal(state.enabled, true);
  await f.ctx.vpsReadinessBackfillStart();
  assert.equal(f.requests.length, 1);
});

test("fleet drops a result from another Space or unknown submission outcome", async () => {
  let deliver;
  const f = fixture(() => new Promise((resolve) => { deliver = resolve; }));
  const previous = seedBackfill(f);
  const first = f.ctx.vpsReadinessBackfillStart();
  f.S.space_id = "new-space";
  const current = seedBackfill(f);
  deliver({ enabled: true, queued_count: 12 });
  await first;
  assert.equal(previous.enabled, undefined);
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
