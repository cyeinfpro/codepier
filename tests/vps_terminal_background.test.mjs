import assert from "node:assert/strict";
import test from "node:test";
import { TerminalStore } from "../web/vps-terminal/store.mjs";
import { TerminalTransport } from "../web/vps-terminal/transport.mjs";

test("new defaults to tmux; explicit temporary fallback uses a distinct request identity", async () => {
  const attempts = [];
  const store = new TerminalStore({
    api: async (url, options) => {
      if (url.includes("capabilities")) return { available: true, saved_vps_host_verification: 'skip', limits: {} };
      const body = JSON.parse(options.body);
      attempts.push(body);
      if (attempts.length === 1) throw Error("response lost");
      return { session_id: "2".repeat(32), state: "ready" };
    },
  });
  store.adopt = (session) => ({ id: session.session_id });
  store.activate = () => {};
  const fresh = { vps: { id: "fixture" }, newSession: true };
  await assert.rejects(store.open(fresh));
  assert.equal(attempts[0].mode, "tmux");
  assert.equal(attempts.length, 1, "no silent direct fallback");
  await store.open({ ...fresh, mode: "direct" });
  assert.equal(attempts[1].mode, "direct");
  assert.notEqual(attempts[0].request_id, attempts[1].request_id);
});

test("incomplete output does not hide the remote session loss reason", async () => {
  const states = [];
  const transport = new TerminalTransport({
    session: { session_id: "1".repeat(32), mode: "tmux" },
    callbacks: { onState: (state) => states.push(state) },
  });
  transport.finish({
    state: "failed",
    reason_code: "REMOTE_SESSION_MISSING",
    output_complete: false,
  });
  await transport.renderQueue;
  assert.equal(states.at(-1).reason_code, "REMOTE_SESSION_MISSING");
  assert.equal(states.at(-1).output_complete, false);
  transport.dispose();
});
