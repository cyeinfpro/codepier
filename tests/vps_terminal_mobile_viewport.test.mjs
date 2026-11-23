import assert from "node:assert/strict";
import test from "node:test";
import { computeTerminalViewport } from "../web/vps-terminal/viewport.mjs";

const phone = {
  innerHeight: 780,
  innerWidth: 390,
  orientationKey: 0,
  workspaceRect: { top: 40, left: 0, right: 390 },
  visualViewport: { height: 780, width: 390, scale: 1 },
};

test("only a focused terminal editor allows compact keyboard mode", () => {
  const baseline = computeTerminalViewport(phone);
  assert.equal(baseline.availableHeight, 728);
  const smaller = { ...phone, visualViewport: { height: 620 } };
  assert.equal(computeTerminalViewport(smaller, baseline).keyboardOpen, false);
  assert.equal(
    computeTerminalViewport({ ...smaller, editableFocused: true }, baseline).keyboardOpen,
    true,
  );
});

test("Android resizes both viewports; baseline and hysteresis still classify the keyboard", () => {
  const baseline = computeTerminalViewport(phone);
  const frame = (height, focused = true) => ({
    ...phone,
    innerHeight: height,
    visualViewport: { height },
    editableFocused: focused,
  });
  const opened = computeTerminalViewport(frame(640), baseline);
  assert.equal(opened.keyboardOpen, true);
  assert.equal(computeTerminalViewport(frame(720), opened).keyboardOpen, true);
  assert.equal(computeTerminalViewport(frame(759), opened).keyboardOpen, false);
  assert.equal(computeTerminalViewport(frame(650, false), opened).keyboardOpen, false);
});

test("rotation resets portrait baseline; shallow landscape keyboard still detected", () => {
  const baseline = computeTerminalViewport(phone);
  const landscape = {
    ...phone,
    innerHeight: 390,
    innerWidth: 844,
    orientationKey: 90,
    visualViewport: { height: 390, width: 844, scale: 1 },
    editableFocused: true,
  };
  const turned = computeTerminalViewport(landscape, baseline);
  assert.equal(turned.keyboardOpen, false);
  assert.equal(
    computeTerminalViewport({
      ...landscape,
      visualViewport: { ...landscape.visualViewport, height: 300 },
    }, turned).keyboardOpen,
    true,
  );
});

test("pinch zoom does not shrink an already measured PTY viewport or fake an IME", () => {
  const baseline = computeTerminalViewport(phone);
  const zoomed = computeTerminalViewport({
    ...phone,
    visualViewport: { height: 400, width: 220, scale: 1.7, offsetTop: 52 },
    editableFocused: true,
  }, baseline);
  assert.equal(zoomed.zoomed, true);
  assert.equal(zoomed.availableHeight, baseline.availableHeight);
  assert.equal(zoomed.keyboardOpen, false);
});

test("panned visible rectangle and actual dock intersection determine usable height", () => {
  const input = {
    ...phone,
    visualViewport: { height: 480, width: 390, offsetTop: 100, offsetLeft: 15 },
    dockRect: { left: 0, right: 390, top: 542, bottom: 575 },
  };
  assert.equal(computeTerminalViewport(input).availableHeight, 430);
  assert.equal(computeTerminalViewport({ ...input, expanded: true, gap: 0 }).availableHeight, 480);
  assert.equal(computeTerminalViewport({
    ...input,
    dockRect: { left: 0, right: 12, top: 542, bottom: 575 },
  }).availableHeight, 468);
});

test("a dock outside the visible frame and a workspace below the keyboard never yield negative size", () => {
  const input = {
    ...phone,
    visualViewport: { height: 420, width: 390 },
    workspaceRect: { top: 600, left: 0, right: 390 },
    dockRect: { left: 0, right: 390, top: 725, bottom: 780 },
  };
  assert.equal(computeTerminalViewport(input).availableHeight, 0);
  assert.equal(computeTerminalViewport({ ...phone, visualViewport: null }).availableHeight, 728);
});
