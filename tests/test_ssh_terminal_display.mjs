import test from "node:test";
import assert from "node:assert/strict";
import { TerminalView } from "../web/vps-terminal/terminal-view.mjs";

function fixture(t) {
  const oldRaf = globalThis.requestAnimationFrame;
  const remaining = [];
  globalThis.requestAnimationFrame = (callback) => (remaining.push(callback), remaining.length);
  t.after(() => { globalThis.requestAnimationFrame = oldRaf; });
  const changes = [];
  const terminal = {
    cols: 80,
    rows: 24,
    resize(cols, rows) {
      changes.push([cols, rows]);
      this.cols = cols;
      this.rows = rows;
    },
  };
  const view = Object.assign(Object.create(TerminalView.prototype), {
    terminal,
    canvas: { isConnected: true, clientWidth: 800, clientHeight: 480 },
    transport: { limits: { min_cols: 20, max_cols: 200, min_rows: 5, max_rows: 100 } },
  });
  let measurements = 0;
  view.fitAddon = { proposeDimensions() {
    measurements++;
    return { cols: view.canvas.clientWidth / 10, rows: view.canvas.clientHeight / 20 };
  } };
  const tick = () => {
    const callback = remaining.shift();
    assert.ok(callback, "a geometry sample was scheduled");
    callback();
  };
  return { view, changes, remaining, tick, measurements: () => measurements };
}

test("transient and hidden resize does not clear or resize xterm", (t) => {
  const { view, changes, remaining, tick } = fixture(t);
  view.fit();
  view.fit();
  assert.equal(remaining.length, 1);
  tick();
  view.canvas.clientWidth = 0;
  view.canvas.clientHeight = 0;
  tick();
  assert.deepEqual(changes, []);
  assert.equal(remaining.length, 0);
  assert.equal(view.terminal.cols, 80);
});

test("fit waits for stable visible measurements, caps size, deduplicates unchanged grid", (t) => {
  const { view, changes, tick, measurements } = fixture(t);
  view.canvas.clientWidth = 900;
  view.fit();
  tick();
  view.canvas.clientWidth = 1000;
  tick();
  view.canvas.clientWidth = 2500;
  view.canvas.clientHeight = 3000;
  tick();
  assert.deepEqual(changes, []);
  tick();
  assert.deepEqual(changes, [[200, 100]]);
  view.fit();
  tick();
  tick();
  assert.equal(measurements(), 2);
  assert.deepEqual(changes, [[200, 100]]);
});

test("history replay and disposal invalidate a scheduled fit", (t) => {
  const { view, changes, remaining, tick } = fixture(t);
  view.fit();
  tick();
  view.replayingHistory = true;
  tick();
  view.fit();
  assert.equal(remaining.length, 0);
  view.replayingHistory = false;
  view.fit();
  view.disposed = true;
  tick();
  assert.deepEqual(changes, []);
  assert.equal(remaining.length, 0);
});
