import assert from "node:assert/strict";
import test from "node:test";
import { TerminalTransport } from "../web/vps-terminal/transport.mjs";
import { TerminalView } from "../web/vps-terminal/terminal-view.mjs";

const id = "1".repeat(32);
class Socket {
  readyState = 1;
  sent = [];
  send(data) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
}

test("only the explicit gap action asks for the same session's live tail", async (t) => {
  const requests = [];
  const attachments = [];
  const transport = new TerminalTransport({
    session: { session_id: id, mode: "direct" },
    baseUrl: "http://127.0.0.1",
    WebSocketImpl: Socket,
    callbacks: { onAttached: (message) => attachments.push(message) },
    api: async (path, options) => {
      requests.push([path, JSON.parse(options.body)]);
      return { ticket: "synthetic", ws_path: "/ws/vps-terminal" };
    },
  });
  t.after(() => transport.dispose());
  await transport.resumeAfterHistoryGap();
  assert.deepEqual(requests, []);
  transport.fail("HISTORY_GAP");
  assert.equal(transport.state, "blocked");
  assert.deepEqual(requests, []);
  await transport.resumeAfterHistoryGap();
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], "/api/vps-terminal/sessions/" + id + "/attach-ticket");
  assert.equal(requests[0][1].resume_from_live, true);
  transport.handleControl({
    type: "attached", session_id: id, mode: "direct", state: "ready",
    runtime_generation: "2".repeat(32), attachment_id: "3".repeat(32),
    render_epoch: "2", stream_epoch: "1", writer_epoch: "2",
    history_recovery: "live_tail", resume_strategy: "live",
    start_offset: "555", output_end_offset: "555",
  }, transport.connectionVersion);
  assert.equal(attachments[0].preserve, false);
  assert.equal(transport.renderedOffset, 555n);
  await new Promise(setImmediate);
  assert.equal(transport.canInput, true);
  assert.equal(transport.socket.sent.some((frame) => ArrayBuffer.isView(frame)), false);
});

test("direct-tail accepts only xterm data with the immediately matching user signal", async () => {
  const view = Object.create(TerminalView.prototype);
  view.disposers = [];
  let userInput;
  view.bindInputProvenance({
    _core: { coreService: { onUserInput(fn) { userInput = fn; return { dispose() {} }; } } },
  });
  assert.equal(view.acceptRendererInput(), true);
  view.liveTail = true;
  assert.equal(view.acceptRendererInput(), false);
  userInput();
  assert.equal(view.acceptRendererInput(), true);
  assert.equal(view.acceptRendererInput(), false);
  userInput();
  await new Promise(setImmediate);
  assert.equal(view.acceptRendererInput(), false);
});
