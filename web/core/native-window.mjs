const DEFAULT_ESTIMATED_HEIGHT = 64;
const DEFAULT_OVERSCAN_PX = 200;
const DEFAULT_MAX_PINNED = 16;

function assertPositiveHeight(height, label = 'height') {
  if (typeof height !== 'number' || !Number.isFinite(height) || height <= 0) {
    throw new RangeError(`${label} must be a finite positive number`);
  }
  return height;
}

export class HeightIndex {
  constructor(keys = [], { defaultHeight = DEFAULT_ESTIMATED_HEIGHT, heights = null } = {}) {
    this.defaultHeight = assertPositiveHeight(defaultHeight, 'defaultHeight');
    this.keys = [];
    this.keyToIndex = new Map();
    this.rowHeights = new Float64Array(0);
    this.tree = new Float64Array(1);
    this.rebuild(keys, heights, this.defaultHeight);
  }

  get count() {
    return this.keys.length;
  }

  keyAt(i) {
    if (i < 0 || i >= this.keys.length) return null;
    return this.keys[i];
  }

  indexOfKey(key) {
    const idx = this.keyToIndex.get(key);
    return idx === undefined ? -1 : idx;
  }

  heightAt(i) {
    if (i < 0 || i >= this.keys.length) return this.defaultHeight;
    return this.rowHeights[i];
  }

  rebuild(keys = [], knownHeights = null, defaultHeight = this.defaultHeight) {
    this.defaultHeight = assertPositiveHeight(defaultHeight, 'defaultHeight');
    const n = keys.length;
    this.keys = [...keys];
    this.keyToIndex = new Map();
    this.rowHeights = new Float64Array(n);
    this.tree = new Float64Array(n + 1);

    for (let i = 0; i < n; i += 1) {
      const key = this.keys[i];
      this.keyToIndex.set(key, i);
      const raw = knownHeights instanceof Map ? knownHeights.get(key) : knownHeights?.[key];
      const h =
        typeof raw === 'number' && Number.isFinite(raw) && raw > 0 ? raw : this.defaultHeight;
      this.rowHeights[i] = h;
      this.tree[i + 1] += h;
      const parent = i + 1 + ((i + 1) & -(i + 1));
      if (parent <= n) this.tree[parent] += this.tree[i + 1];
    }
  }

  setHeight(i, height) {
    if (i < 0 || i >= this.keys.length) return false;
    const next = assertPositiveHeight(height, 'height');
    const delta = next - this.rowHeights[i];
    if (delta === 0) return false;
    this.rowHeights[i] = next;
    let idx = i + 1;
    while (idx <= this.keys.length) {
      this.tree[idx] += delta;
      idx += idx & -idx;
    }
    return true;
  }

  offset(i) {
    const n = this.keys.length;
    if (i <= 0 || n === 0) return 0;
    let idx = Math.min(i, n);
    let sum = 0;
    while (idx > 0) {
      sum += this.tree[idx];
      idx -= idx & -idx;
    }
    return sum;
  }

  _lowerBoundPrefix(predicate) {
    const n = this.keys.length;
    if (n === 0) return 0;
    let pos = 0;
    let sum = 0;
    let bit = 1;
    while (bit << 1 <= n) bit <<= 1;
    for (; bit > 0; bit >>= 1) {
      const next = pos + bit;
      if (next <= n && predicate(sum + this.tree[next])) {
        pos = next;
        sum += this.tree[next];
      }
    }
    return pos;
  }

  firstRowWhoseEndExceeds(y) {
    const n = this.keys.length;
    if (n === 0 || y < 0) return 0;
    const total = this.offset(n);
    if (y >= total) return n;
    // Count rows whose cumulative end <= y; that index is the first row whose end > y.
    return Math.min(
      n,
      this._lowerBoundPrefix((prefix) => prefix <= y),
    );
  }

  firstRowStartingAtOrAfter(y) {
    const n = this.keys.length;
    if (n === 0 || y <= 0) return 0;
    const total = this.offset(n);
    if (y >= total) return n;
    // Largest pos with offset(pos) < y; therefore offset(pos + 1) >= y.
    return Math.min(n, this._lowerBoundPrefix((prefix) => prefix < y) + 1);
  }
}

export function visibleRange(index, scrollTop, viewportHeight, overscanPx = DEFAULT_OVERSCAN_PX) {
  const count = index.count;
  if (!count) return { start: 0, end: 0, top: 0, bottom: 0 };
  const total = index.offset(count);
  const viewport = Math.max(1, viewportHeight);
  const y = Math.max(0, Math.min(scrollTop, Math.max(0, total - viewport)));
  const low = Math.max(0, y - overscanPx);
  const high = Math.min(total, y + viewport + overscanPx);
  const start = index.firstRowWhoseEndExceeds(low);
  const end = Math.max(start, index.firstRowStartingAtOrAfter(high));
  return {
    start,
    end,
    top: index.offset(start),
    bottom: Math.max(0, total - index.offset(end)),
  };
}

export function captureAnchor(view, neighborLookahead = 4) {
  const count = view.heights.count;
  const scrollTop = view.el.scrollTop;
  const i = count ? Math.min(count - 1, view.heights.firstRowWhoseEndExceeds(scrollTop)) : -1;
  const prevKeys = [];
  const nextKeys = [];
  if (i >= 0) {
    for (let k = 1; k <= neighborLookahead && i - k >= 0; k += 1) {
      prevKeys.push(view.heights.keyAt(i - k));
    }
    for (let k = 1; k <= neighborLookahead && i + k < count; k += 1) {
      nextKeys.push(view.heights.keyAt(i + k));
    }
  }
  return {
    owner: view.owner,
    userScrollGeneration: view.userScrollGeneration,
    followBottom: Boolean(view.followBottom),
    key: i >= 0 ? view.heights.keyAt(i) : null,
    within: i >= 0 ? scrollTop - view.heights.offset(i) : 0,
    prevKeys,
    nextKeys,
  };
}

export function resolveSurvivingAnchor(view, anchor, fallbackTop = 0) {
  const heights = view.heights;
  const total = heights.offset(heights.count);
  const viewport = Math.max(1, view.el?.clientHeight ?? 1);
  const maxTop = Math.max(0, total - viewport);

  const mapKey = (candidate) => {
    const direct = heights.indexOfKey(candidate);
    if (direct >= 0) return direct;
    if (typeof view.groupKeyFor === 'function') {
      const gk = view.groupKeyFor(candidate);
      if (gk) {
        const grouped = heights.indexOfKey(gk);
        if (grouped >= 0) return grouped;
      }
    }
    return -1;
  };

  if (anchor?.key) {
    const idx = mapKey(anchor.key);
    if (idx >= 0) {
      const clampedWithin = Math.max(0, Math.min(anchor.within ?? 0, heights.heightAt(idx) - 1));
      return Math.max(0, Math.min(maxTop, heights.offset(idx) + clampedWithin));
    }
  }
  for (const nextKey of anchor?.nextKeys || []) {
    const idx = mapKey(nextKey);
    if (idx >= 0) return Math.max(0, Math.min(maxTop, heights.offset(idx)));
  }
  for (const prevKey of anchor?.prevKeys || []) {
    const idx = mapKey(prevKey);
    if (idx >= 0) return Math.max(0, Math.min(maxTop, heights.offset(idx)));
  }
  return Math.max(0, Math.min(maxTop, fallbackTop));
}

export function commitHeightBatch(view, measurements, anchor) {
  if (!anchor || anchor.owner !== view.owner) return null;
  for (const entry of measurements) {
    if (!entry) continue;
    const i = view.heights.indexOfKey(entry.key);
    if (
      i >= 0 &&
      typeof entry.height === 'number' &&
      Number.isFinite(entry.height) &&
      entry.height > 0
    ) {
      view.heights.setHeight(i, entry.height);
    }
  }
  const canRestore = anchor.userScrollGeneration === view.userScrollGeneration;
  let targetTop = view.el.scrollTop;
  if (canRestore && anchor.followBottom) {
    targetTop = Math.max(0, view.heights.offset(view.heights.count) - view.el.clientHeight);
  } else if (canRestore && anchor.key !== null) {
    targetTop = resolveSurvivingAnchor(view, anchor, targetTop);
  }
  const range = visibleRange(
    view.heights,
    targetTop,
    view.el.clientHeight,
    view.overscanPx ?? DEFAULT_OVERSCAN_PX,
  );
  view.renderWindow(range);
  if (canRestore) {
    view.el.scrollTop = targetTop;
  }
  return { range, targetTop, restored: canRestore };
}

export class PinSet {
  constructor(maxPinned = DEFAULT_MAX_PINNED) {
    if (!Number.isSafeInteger(maxPinned) || maxPinned < 0) {
      throw new RangeError('maxPinned must be a non-negative safe integer');
    }
    this.maxPinned = maxPinned;
    this.entries = new Map();
    this.evictions = 0;
  }

  get size() {
    return this.entries.size;
  }

  has(key) {
    return this.entries.has(key);
  }

  pin(key, reason = 'focus') {
    if (typeof key !== 'string' || !key || this.maxPinned === 0) return false;
    const existing = this.entries.get(key);
    if (existing) {
      existing.add(reason);
      this.entries.delete(key);
      this.entries.set(key, existing);
      return true;
    }
    while (this.entries.size >= this.maxPinned) {
      let victim = null;
      for (const [candidateKey, reasons] of this.entries) {
        if (!reasons.has('approval')) {
          victim = candidateKey;
          break;
        }
      }
      if (!victim) {
        victim = this.entries.keys().next().value;
      }
      this.entries.delete(victim);
      this.evictions += 1;
    }
    this.entries.set(key, new Set([reason]));
    return true;
  }

  unpin(key, reason = null) {
    const existing = this.entries.get(key);
    if (!existing) return false;
    if (reason === null) {
      this.entries.delete(key);
      return true;
    }
    existing.delete(reason);
    if (existing.size === 0) {
      this.entries.delete(key);
    }
    return true;
  }

  keys() {
    return [...this.entries.keys()];
  }

  discreteRanges(index, visibleStart, visibleEnd) {
    const extraIndices = [];
    for (const key of this.entries.keys()) {
      const idx = index.indexOfKey(key);
      if (idx >= 0 && (idx < visibleStart || idx >= visibleEnd)) {
        extraIndices.push(idx);
      }
    }
    extraIndices.sort((a, b) => a - b);
    const ranges = [];
    for (const idx of extraIndices) {
      const last = ranges[ranges.length - 1];
      if (last && last.end === idx) {
        last.end = idx + 1;
      } else {
        ranges.push({ start: idx, end: idx + 1 });
      }
    }
    return ranges;
  }
}

export function flushVisibleDirty(view, runtime) {
  runtime.frame = 0;
  const generation = runtime.generation;
  const now = runtime.now || (() => performance.now());
  const scheduleRaf = runtime.requestFrame || globalThis.requestAnimationFrame;

  if (runtime.structureDirty) {
    runtime.reconcileVisibleStructure?.(view, runtime.viewport);
    runtime.structureDirty = false;
  }

  const keys = [...runtime.dirtyKeys];
  runtime.dirtyKeys.clear();
  const sliceMs = runtime.budget?.renderSliceMs ?? 8;
  const deadline = now() + sliceMs;
  let updated = 0;

  for (let i = 0; i < keys.length; i += 1) {
    if (generation !== runtime.generation) return { aborted: true, updated };
    if (i > 0 && now() >= deadline) {
      for (let j = i; j < keys.length; j += 1) {
        runtime.dirtyKeys.add(keys[j]);
      }
      break;
    }
    const key = keys[i];
    const item = view.items.get(key);
    if (item && runtime.viewport.isMounted(key)) {
      runtime.updateItem(item);
      updated += 1;
    }
  }

  if (runtime.dirtyKeys.size > 0 && !runtime.frame && typeof scheduleRaf === 'function') {
    runtime.frame = scheduleRaf(() => flushVisibleDirty(view, runtime));
  }
  return { aborted: false, updated, remaining: runtime.dirtyKeys.size };
}
