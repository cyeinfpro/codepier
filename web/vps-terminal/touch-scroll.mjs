const FRICTION = 0.92;
const MAX_MOMENTUM_LINES_PER_FRAME = 5;
const MIN_FONT_SIZE = 11;
const MAX_FONT_SIZE = 22;
const HOLD_DELAY_MS = 300;
const HOLD_INTERVAL_MS = 45;

export const DIRECTION_SEQUENCES = {
  up: ['\x1b[A', '\x1bOA'],
  down: ['\x1b[B', '\x1bOB'],
  right: ['\x1b[C', '\x1bOC'],
  left: ['\x1b[D', '\x1bOD'],
};

function touchDistance(a, b) {
  return Math.hypot((b.clientX || 0) - (a.clientX || 0), (b.clientY || 0) - (a.clientY || 0));
}

export function glideGearInterval(distance) {
  if (distance > 56) return 16;
  if (distance >= 28) return 40;
  if (distance >= 10) return 90;
  return 0;
}

export function resolveGlideDirection(dx, dy, fallback = 'right') {
  if (Math.hypot(dx, dy) < 10) return fallback;
  if (Math.abs(dx) >= Math.abs(dy)) return dx >= 0 ? 'right' : 'left';
  return dy >= 0 ? 'down' : 'up';
}

export class TouchScrollController {
  constructor({
    target,
    getTerminal,
    getFontSize,
    getLineHeight,
    canInput,
    sendRaw,
    onFontSize,
    onPinchEnd,
    onAltScrollChange,
    now = Date.now,
    requestFrame = (fn) => (globalThis.requestAnimationFrame || ((cb) => setTimeout(cb, 16)))(fn),
    cancelFrame = (id) => (globalThis.cancelAnimationFrame || clearTimeout)(id),
    reducedMotion = () => !!globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches,
  }) {
    this.target = target;
    this.getTerminal = getTerminal;
    this.getFontSize = getFontSize;
    this.getLineHeight = getLineHeight;
    this.canInput = canInput;
    this.sendRaw = sendRaw;
    this.onFontSize = onFontSize;
    this.onPinchEnd = onPinchEnd;
    this.onAltScrollChange = onAltScrollChange;
    this.now = now;
    this.requestFrame = requestFrame;
    this.cancelFrame = cancelFrame;
    this.reducedMotion = reducedMotion;
    this.momentumFrame = null;
    this.tracking = false;
    this.dragging = false;
    this.pinching = false;
    this.horizontal = false;
    this.accPx = 0;
    this.samples = [];
    this.anchor = { col: 1, row: 1 };
    this.disposed = false;
  }

  bind() {
    if (!this.target?.addEventListener) return () => {};
    const onStart = (event) => this.handleTouchStart(event);
    const onMove = (event) => this.handleTouchMove(event);
    const onEnd = (event) => this.handleTouchEnd(event);
    this.target.addEventListener('touchstart', onStart, { passive: false });
    this.target.addEventListener('touchmove', onMove, { passive: false });
    this.target.addEventListener('touchend', onEnd, { passive: false });
    this.target.addEventListener('touchcancel', onEnd, { passive: false });
    return () => {
      this.stopMomentum();
      this.target.removeEventListener?.('touchstart', onStart);
      this.target.removeEventListener?.('touchmove', onMove);
      this.target.removeEventListener?.('touchend', onEnd);
      this.target.removeEventListener?.('touchcancel', onEnd);
    };
  }

  stopMomentum() {
    if (this.momentumFrame != null) {
      this.cancelFrame(this.momentumFrame);
      this.momentumFrame = null;
    }
  }

  hasSelection() {
    return !!this.getTerminal?.()?.hasSelection?.();
  }

  cellHeight() {
    const size = Number(this.getFontSize?.()) || 13.5;
    const ratio = Number(this.getLineHeight?.()) || 1.18;
    return Math.max(8, size * ratio);
  }

  cellCoords(touch) {
    const terminal = this.getTerminal?.();
    const rect = this.target?.getBoundingClientRect?.();
    const cols = terminal?.cols || 80;
    const rows = terminal?.rows || 24;
    if (!rect || rect.width <= 0 || rect.height <= 0) return { col: 1, row: 1 };
    const relX = Math.max(0, Math.min(rect.width - 1, (touch.clientX || 0) - rect.left));
    const relY = Math.max(0, Math.min(rect.height - 1, (touch.clientY || 0) - rect.top));
    return {
      col: Math.max(1, Math.min(cols, Math.floor((relX / rect.width) * cols) + 1)),
      row: Math.max(1, Math.min(rows, Math.floor((relY / rect.height) * rows) + 1)),
    };
  }

  handleTouchStart(event) {
    if (this.disposed) return;
    this.stopMomentum();
    const touches = event.touches || [];
    if (touches.length === 2) {
      this.startPinch(event, touches);
      return;
    }
    if (touches.length === 1) this.startDrag(touches[0]);
  }

  startPinch(event, touches) {
    event.preventDefault?.();
    this.tracking = false;
    this.dragging = false;
    const dist = touchDistance(touches[0], touches[1]);
    if (dist < 8) return;
    this.pinching = true;
    this.pinchStartDist = dist;
    this.pinchBaseFont = Number(this.getFontSize?.()) || 13.5;
    this.pinchLastFont = this.pinchBaseFont;
  }

  updatePinch(event, touches) {
    event.preventDefault?.();
    const dist = touchDistance(touches[0], touches[1]);
    if (!this.pinchStartDist || dist <= 0) return;
    const scaled = this.pinchBaseFont * (dist / this.pinchStartDist);
    const quantized = Math.max(MIN_FONT_SIZE, Math.min(MAX_FONT_SIZE, Math.round(scaled * 2) / 2));
    if (Math.abs(quantized - this.pinchLastFont) >= 0.5) {
      this.pinchLastFont = quantized;
      this.onFontSize?.(quantized, { fit: false });
    }
  }

  startDrag(touch) {
    if (this.hasSelection()) {
      this.tracking = false;
      return;
    }
    this.pinching = false;
    this.tracking = true;
    this.dragging = false;
    this.horizontal = false;
    this.startX = touch.clientX || 0;
    this.startY = touch.clientY || 0;
    this.lastY = this.startY;
    this.accPx = 0;
    this.anchor = this.cellCoords(touch);
    this.samples = [{ y: this.startY, t: this.now() }];
  }

  handleTouchMove(event) {
    if (this.disposed) return;
    const touches = event.touches || [];
    if (touches.length === 2) {
      if (!this.pinching) this.startPinch(event, touches);
      else this.updatePinch(event, touches);
      return;
    }
    if (
      this.pinching ||
      !this.tracking ||
      touches.length !== 1 ||
      this.horizontal ||
      this.hasSelection()
    )
      return;
    this.updateDrag(event, touches[0]);
  }

  updateDrag(event, touch) {
    const x = touch.clientX || 0;
    const y = touch.clientY || 0;
    const dx = x - this.startX;
    const dy = y - this.startY;
    if (!this.dragging) {
      if (Math.abs(dx) > 10 && Math.abs(dx) >= Math.abs(dy)) {
        this.horizontal = true;
        return;
      }
      if (Math.abs(dy) <= 8 || Math.abs(dy) <= 1.5 * Math.abs(dx)) return;
      this.dragging = true;
    }
    event.preventDefault?.();
    const stepPx = y - this.lastY;
    this.lastY = y;
    this.recordSample(y);
    this.consumePixels(stepPx, 0);
  }

  recordSample(y) {
    const t = this.now();
    this.samples.push({ y, t });
    while (this.samples.length > 2 && t - this.samples[0].t > 100) this.samples.shift();
  }

  consumePixels(px, maxLines = 0) {
    this.accPx += px;
    const height = this.cellHeight();
    let lines = Math.trunc(this.accPx / height);
    if (!lines) return 0;
    this.accPx -= lines * height;
    if (maxLines > 0) lines = Math.max(-maxLines, Math.min(maxLines, lines));
    this.emitLines(lines);
    return lines;
  }

  emitLines(lines) {
    const terminal = this.getTerminal?.();
    if (!terminal || !lines) return;
    const up = lines > 0;
    const count = Math.abs(lines);
    if (terminal.buffer?.active?.type === 'alternate') {
      this.emitAlternateScroll(terminal, up, count);
      return;
    }
    terminal.scrollLines?.(up ? -count : count);
    this.onAltScrollChange?.(false, 0);
  }

  emitAlternateScroll(terminal, up, count) {
    if (!this.canInput?.()) return;
    const mouseMode = terminal.modes?.mouseTrackingMode;
    let unit;
    if (mouseMode && mouseMode !== 'none') {
      const { col, row } = this.anchor || { col: 1, row: 1 };
      unit = `\x1b[<${up ? 64 : 65};${col};${row}M`;
    } else {
      const app = !!terminal.modes?.applicationCursorKeysMode;
      unit = up ? (app ? '\x1bOA' : '\x1b[A') : app ? '\x1bOB' : '\x1b[B';
    }
    if (this.sendRaw?.(unit.repeat(count)) !== false) this.onAltScrollChange?.(up, count);
  }

  handleTouchEnd(event) {
    if (this.disposed) return;
    const remaining = event.touches?.length || 0;
    if (this.pinching && remaining < 2) {
      this.pinching = false;
      this.tracking = false;
      this.onPinchEnd?.();
      return;
    }
    if (remaining > 0) return;
    const wasDragging = this.dragging;
    this.tracking = false;
    this.dragging = false;
    if (wasDragging && event.type === 'touchend' && !this.reducedMotion()) this.startMomentum();
  }

  startMomentum() {
    const now = this.now();
    const recent = this.samples.filter((sample) => now - sample.t <= 80);
    if (recent.length < 2) return;
    const first = recent[0];
    const last = recent[recent.length - 1];
    const dt = last.t - first.t;
    if (dt < 4) return;
    let frameVelocity = ((last.y - first.y) / dt) * 16;
    if (Math.abs(frameVelocity) < this.cellHeight() * 0.35) return;
    const step = () => {
      if (this.disposed) return;
      frameVelocity *= FRICTION;
      if (Math.abs(frameVelocity) < 0.5) {
        this.momentumFrame = null;
        return;
      }
      this.consumePixels(frameVelocity, MAX_MOMENTUM_LINES_PER_FRAME);
      this.momentumFrame = this.requestFrame(step);
    };
    this.momentumFrame = this.requestFrame(step);
  }

  dispose() {
    this.disposed = true;
    this.stopMomentum();
    this.samples.length = 0;
  }
}

export function bindDirectionalKey(node, direction, { sendDirection, onHaptic } = {}) {
  let pointerId = null;
  let startX = 0;
  let startY = 0;
  let holdTimer = null;
  let repeatTimer = null;
  let activeGear = 0;
  let activeDir = direction;
  let suppressClick = false;

  const stopTimers = () => {
    clearTimeout(holdTimer);
    clearInterval(repeatTimer);
    holdTimer = null;
    repeatTimer = null;
    activeGear = 0;
  };
  const stopAll = () => {
    stopTimers();
    pointerId = null;
  };
  const onClick = () => {
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    sendDirection?.(direction);
  };
  const onDown = (event) => {
    event.preventDefault?.();
    if (event.button !== undefined && event.button !== 0) return;
    stopAll();
    suppressClick = true;
    pointerId = event.pointerId ?? 1;
    startX = event.clientX || 0;
    startY = event.clientY || 0;
    activeDir = direction;
    node.setPointerCapture?.(pointerId);
    onHaptic?.();
    sendDirection?.(direction);
    holdTimer = setTimeout(() => {
      holdTimer = null;
      repeatTimer = setInterval(() => {
        sendDirection?.(activeDir);
      }, HOLD_INTERVAL_MS);
    }, HOLD_DELAY_MS);
  };
  const onMove = (event) => {
    if (pointerId === null || (event.pointerId ?? 1) !== pointerId) return;
    const dx = (event.clientX || 0) - startX;
    const dy = (event.clientY || 0) - startY;
    const gear = glideGearInterval(Math.hypot(dx, dy));
    if (!gear) return;
    activeDir = resolveGlideDirection(dx, dy, direction);
    if (gear !== activeGear) {
      stopTimers();
      activeGear = gear;
      repeatTimer = setInterval(() => {
        onHaptic?.();
        sendDirection?.(activeDir);
      }, gear);
    }
  };
  const onUp = (event) => {
    if (pointerId !== null && (event.pointerId ?? 1) === pointerId) stopAll();
  };

  node.addEventListener('click', onClick);
  node.addEventListener('pointerdown', onDown);
  node.addEventListener('pointermove', onMove);
  node.addEventListener('pointerup', onUp);
  node.addEventListener('pointercancel', onUp);
  node.addEventListener('lostpointercapture', stopAll);
  return () => {
    stopAll();
    node.removeEventListener?.('click', onClick);
    node.removeEventListener?.('pointerdown', onDown);
    node.removeEventListener?.('pointermove', onMove);
    node.removeEventListener?.('pointerup', onUp);
    node.removeEventListener?.('pointercancel', onUp);
    node.removeEventListener?.('lostpointercapture', stopAll);
  };
}
