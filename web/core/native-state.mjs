import {
  assertHex32,
  assertSafeNonNegativeInt,
  decodeSnapshot,
  isVerifiedBatch,
} from './native-contracts.mjs';

const COMPACT_KINDS = new Set(['tool', 'reasoning']);
const DONE_STATUSES = new Set(['completed', 'end', 'succeeded', 'ok']);
const ACTIVE_STATUSES = new Set(['running', 'streaming', 'start', 'pending', 'in_progress']);
const DEFAULT_LEDGER_MAX_COUNT = 128;
const DEFAULT_LEDGER_MAX_BYTES = 64 * 1024;
const TEXT_ENCODER = new TextEncoder();

export const CHANGE_REASONS = Object.freeze([
  'text',
  'status',
  'insert',
  'remove',
  'group_boundary',
  'height',
  'control',
]);

function utf8ByteLength(text) {
  return TEXT_ENCODER.encode(String(text ?? '')).length;
}

function cloneItem(item) {
  return {
    ...item,
    data: item.data && typeof item.data === 'object' ? { ...item.data } : undefined,
    body_ref:
      item.body_ref && typeof item.body_ref === 'object'
        ? { ...item.body_ref }
        : (item.body_ref ?? null),
  };
}

function compareItemOrder(aKey, aItem, bKey, bItem) {
  const aCursor = aItem?.first_cursor ?? 0;
  const bCursor = bItem?.first_cursor ?? 0;
  if (aCursor !== bCursor) return aCursor - bCursor;
  if (aKey < bKey) return -1;
  if (aKey > bKey) return 1;
  return 0;
}

function insertOrderKey(order, items, key, item) {
  if (!order.length) {
    order.push(key);
    return 0;
  }
  const lastKey = order[order.length - 1];
  if (compareItemOrder(lastKey, items.get(lastKey), key, item) <= 0) {
    order.push(key);
    return order.length - 1;
  }
  let lo = 0;
  let hi = order.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    const midKey = order[mid];
    if (compareItemOrder(midKey, items.get(midKey), key, item) <= 0) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  order.splice(lo, 0, key);
  return lo;
}

function isProcessFailure(item) {
  const status = item?.status ?? item?.data?.status;
  return (
    status === 'error' ||
    status === 'failed' ||
    item?.is_error === true ||
    item?.data?.is_error === true
  );
}

function isCompactProcessItem(item) {
  return Boolean(item && COMPACT_KINDS.has(item.kind) && !isProcessFailure(item));
}

function itemReceipt(item) {
  return item?.receipt ?? item?.data?.receipt ?? '';
}

function itemStatus(item) {
  return item?.status ?? item?.data?.status ?? '';
}

function addReason(reasonsMap, key, reason) {
  let set = reasonsMap.get(key);
  if (!set) {
    set = new Set();
    reasonsMap.set(key, set);
  }
  set.add(reason);
}

function ledgerBytes(ledger) {
  let total = 0;
  for (const [k, v] of ledger) {
    total += k.length + (v.bytes || 0);
  }
  return total;
}

export function rememberVerifiedBatch(recentBatches, interval, entry, limits = {}) {
  const maxCount = limits.maxCount ?? DEFAULT_LEDGER_MAX_COUNT;
  const maxBytes = limits.maxBytes ?? DEFAULT_LEDGER_MAX_BYTES;
  const next = new Map(recentBatches);
  next.delete(interval);
  const bytes = utf8ByteLength(JSON.stringify(entry));
  next.set(interval, {
    digest: String(entry.digest),
    fromCursor: assertSafeNonNegativeInt(entry.fromCursor, 'fromCursor'),
    toCursor: assertSafeNonNegativeInt(entry.toCursor, 'toCursor'),
    bytes,
  });
  let totalBytes = ledgerBytes(next);
  for (const [oldKey, oldVal] of next) {
    if (next.size <= maxCount && totalBytes <= maxBytes) break;
    next.delete(oldKey);
    totalBytes -= oldKey.length + (oldVal.bytes || 0);
  }
  return next;
}

export function reconcileProcessGroups(items, order, existingGroups = new Map()) {
  const groups = new Map();
  let current = null;
  for (const key of order) {
    const item = items.get(key);
    if (!isCompactProcessItem(item)) {
      current = null;
      continue;
    }
    const receipt = itemReceipt(item);
    if (!current || current.receipt !== receipt) {
      const prior = existingGroups.get(key);
      current = {
        groupKey: key,
        receipt,
        itemKeys: [],
        toolsCount: 0,
        thoughtsCount: 0,
        live: false,
        expanded: prior ? Boolean(prior.expanded) : false,
      };
      groups.set(key, current);
    }
    current.itemKeys.push(key);
    if (item.kind === 'tool') current.toolsCount += 1;
    else current.thoughtsCount += 1;
    if (!DONE_STATUSES.has(itemStatus(item))) {
      current.live = true;
    }
  }

  const changedGroupKeys = new Set();
  let structureChanged = groups.size !== existingGroups.size;
  for (const [groupKey, group] of groups) {
    const prev = existingGroups.get(groupKey);
    if (!prev) {
      structureChanged = true;
      changedGroupKeys.add(groupKey);
      continue;
    }
    const sameItems =
      prev.itemKeys.length === group.itemKeys.length &&
      prev.itemKeys.every((k, idx) => k === group.itemKeys[idx]);
    if (!sameItems || prev.receipt !== group.receipt) {
      structureChanged = true;
      changedGroupKeys.add(groupKey);
    } else if (
      prev.toolsCount !== group.toolsCount ||
      prev.thoughtsCount !== group.thoughtsCount ||
      prev.live !== group.live
    ) {
      changedGroupKeys.add(groupKey);
    }
  }
  for (const oldKey of existingGroups.keys()) {
    if (!groups.has(oldKey)) {
      structureChanged = true;
      changedGroupKeys.add(oldKey);
    }
  }
  return { groups, structureChanged, changedGroupKeys };
}

export function createInitialNativeState(identity) {
  if (!identity || typeof identity !== 'object') {
    throw new TypeError('identity must be an object');
  }
  if (typeof identity.scopeKey !== 'string' || !identity.scopeKey) {
    throw new TypeError('identity.scopeKey must be a non-empty string');
  }
  const sessionId = assertHex32(identity.sessionId, 'identity.sessionId');
  const streamEpoch = identity.streamEpoch ? String(identity.streamEpoch) : '';
  return {
    scopeKey: identity.scopeKey,
    sessionId,
    schemaVersion: 2,
    streamEpoch,
    revision: 0,
    appliedCursor: 0,
    resumeToken: identity.resumeToken ?? null,
    projection: {
      items: new Map(),
      order: [],
      activeKeys: new Set(),
      pending: new Map(),
      session: null,
      run: null,
      settings: null,
      pendingSettings: null,
      capabilities: null,
      stats: null,
      history: {
        hasMore: false,
        nextToken: null,
        pageRevision: 0,
        pageAppliedCursor: 0,
      },
      groups: new Map(),
    },
    recentBatches: new Map(),
    pendingCommands: new Map(),
    dirtyKeys: new Set(),
    changeReasons: new Map(),
    structureDirty: false,
  };
}

function normalizeSnapshotItem(rawItem) {
  const key = rawItem.item_key ?? rawItem.key;
  if (typeof key !== 'string' || !key) {
    throw new TypeError('Snapshot item missing item_key');
  }
  const firstCursor = assertSafeNonNegativeInt(rawItem.first_cursor, 'item.first_cursor');
  const itemRevision = assertSafeNonNegativeInt(rawItem.item_revision, 'item.item_revision');
  const item = cloneItem(rawItem);
  item.key = key;
  item.item_key = key;
  item.first_cursor = firstCursor;
  item.item_revision = itemRevision;
  if (item.body_ref && item.text == null && item.body == null) {
    item.partial = true;
  } else {
    item.partial = Boolean(item.partial);
  }
  return item;
}

export function installNativeSnapshot(rawSnapshot, identity) {
  if (!identity || typeof identity !== 'object') {
    throw new TypeError('identity must be an object');
  }
  const scopeKey = identity.scopeKey;
  const sessionId = assertHex32(identity.sessionId, 'sessionId');
  const snap = decodeSnapshot(rawSnapshot);
  if (snap.scope_key !== scopeKey) {
    throw new Error('Snapshot scope_key mismatch');
  }
  if (snap.session_id !== sessionId) {
    throw new Error('Snapshot session_id mismatch');
  }

  const items = new Map();
  const order = [];
  for (const rawItem of snap.items) {
    const item = normalizeSnapshotItem(rawItem);
    const prev = items.get(item.key);
    if (!prev || item.item_revision > prev.item_revision) {
      items.set(item.key, item);
      if (!prev) order.push(item.key);
    }
  }
  order.sort((a, b) => compareItemOrder(a, items.get(a), b, items.get(b)));

  const activeKeys = new Set(Array.isArray(snap.active_keys) ? snap.active_keys : []);
  const pending = new Map();
  if (Array.isArray(snap.pending)) {
    for (const entry of snap.pending) {
      if (entry && typeof entry === 'object' && entry.request_id) {
        pending.set(String(entry.request_id), { ...entry });
      }
    }
  } else if (snap.pending && typeof snap.pending === 'object') {
    for (const [k, v] of Object.entries(snap.pending)) {
      pending.set(k, v && typeof v === 'object' ? { ...v } : v);
    }
  }
  for (const [key, item] of items) {
    if (ACTIVE_STATUSES.has(itemStatus(item))) activeKeys.add(key);
    if (item.kind === 'approval' && !item.resolved && !item.answered) {
      const reqId = item.request_id ?? item.data?.request_id ?? key;
      if (!pending.has(String(reqId))) pending.set(String(reqId), { key, ...item });
    }
  }

  const { groups } = reconcileProcessGroups(items, order, new Map());
  const dirtyKeys = new Set(order);
  const changeReasons = new Map();
  for (const key of order) addReason(changeReasons, key, 'insert');

  const historyMeta =
    snap.history && typeof snap.history === 'object'
      ? {
          hasMore: Boolean(snap.history.has_more ?? snap.history.hasMore),
          nextToken: snap.history.next_token ?? snap.history.nextToken ?? null,
          pageRevision: snap.history.page_revision ?? snap.projection_revision,
          pageAppliedCursor: snap.history.page_applied_cursor ?? snap.applied_cursor,
        }
      : {
          hasMore: Boolean(snap.has_more),
          nextToken: snap.history_token ?? null,
          pageRevision: snap.projection_revision,
          pageAppliedCursor: snap.applied_cursor,
        };

  return {
    scopeKey,
    sessionId,
    schemaVersion: 2,
    streamEpoch: snap.stream_epoch,
    revision: snap.projection_revision,
    appliedCursor: snap.applied_cursor,
    resumeToken: snap.resume_token ?? null,
    projection: {
      items,
      order,
      activeKeys,
      pending,
      session: snap.session ? { ...snap.session } : null,
      run: snap.run ? { ...snap.run } : null,
      settings: snap.settings ? { ...snap.settings } : null,
      pendingSettings: snap.pending_settings
        ? { ...snap.pending_settings }
        : snap.pendingSettings
          ? { ...snap.pendingSettings }
          : null,
      capabilities: snap.capabilities ? { ...snap.capabilities } : null,
      stats: snap.stats ? { ...snap.stats } : null,
      history: historyMeta,
      groups,
    },
    recentBatches: new Map(),
    pendingCommands: new Map(),
    dirtyKeys,
    changeReasons,
    structureDirty: order.length > 0,
  };
}

function cloneProjection(projection) {
  const items = new Map();
  for (const [k, v] of projection.items) items.set(k, cloneItem(v));
  const pending = new Map();
  for (const [k, v] of projection.pending)
    pending.set(k, v && typeof v === 'object' ? { ...v } : v);
  return {
    items,
    order: [...projection.order],
    activeKeys: new Set(projection.activeKeys),
    pending,
    session: projection.session ? { ...projection.session } : null,
    run: projection.run ? { ...projection.run } : null,
    settings: projection.settings ? { ...projection.settings } : null,
    pendingSettings: projection.pendingSettings ? { ...projection.pendingSettings } : null,
    capabilities: projection.capabilities ? { ...projection.capabilities } : null,
    stats: projection.stats ? { ...projection.stats } : null,
    history: { ...projection.history },
    groups: new Map(projection.groups),
  };
}

function applyUpsertChange(next, change, dirtyKeys, reasons) {
  const key = change.key ?? change.item_key;
  if (typeof key !== 'string' || !key) throw new TypeError('Change missing item key');
  const itemRev = assertSafeNonNegativeInt(change.item_revision, 'change.item_revision');
  const existing = next.items.get(key);
  if (existing && existing.item_revision >= itemRev) return null;

  const incoming = cloneItem(change.item && typeof change.item === 'object' ? change.item : change);
  incoming.key = key;
  incoming.item_key = key;
  incoming.item_revision = itemRev;
  incoming.first_cursor = existing
    ? existing.first_cursor
    : assertSafeNonNegativeInt(incoming.first_cursor ?? change.first_cursor ?? 0, 'first_cursor');

  next.items.set(key, incoming);
  dirtyKeys.add(key);

  if (!existing) {
    insertOrderKey(next.order, next.items, key, incoming);
    addReason(reasons, key, 'insert');
    addReason(reasons, key, 'height');
  } else {
    if (incoming.text !== existing.text || incoming.body !== existing.body) {
      addReason(reasons, key, 'text');
      addReason(reasons, key, 'height');
    }
    if (itemStatus(incoming) !== itemStatus(existing)) {
      addReason(reasons, key, 'status');
    }
  }

  if (ACTIVE_STATUSES.has(itemStatus(incoming))) next.activeKeys.add(key);
  else next.activeKeys.delete(key);

  if (incoming.kind === 'approval') {
    const reqId = String(incoming.request_id ?? incoming.data?.request_id ?? key);
    if (incoming.resolved || incoming.answered) {
      next.pending.delete(reqId);
      addReason(reasons, key, 'control');
    } else {
      next.pending.set(reqId, { key, ...incoming });
      addReason(reasons, key, 'control');
    }
  }
  return !existing ? 'insert' : 'update';
}

function applyAppendTextChange(next, change, dirtyKeys, reasons) {
  const key = change.key ?? change.item_key;
  if (typeof key !== 'string' || !key) throw new TypeError('append_text missing key');
  const itemRev = assertSafeNonNegativeInt(change.item_revision, 'change.item_revision');
  const existing = next.items.get(key);
  if (!existing) {
    return {
      ok: false,
      result: {
        kind: 'repair_required',
        reason: 'missing_item',
        itemKey: key,
        expectedBaseRevision: change.base_item_revision ?? null,
        actualItemRevision: null,
      },
    };
  }
  if (existing.item_revision >= itemRev) return { ok: true };

  if (change.base_item_revision !== undefined) {
    const baseRev = assertSafeNonNegativeInt(
      change.base_item_revision,
      'change.base_item_revision',
    );
    if (existing.item_revision !== baseRev) {
      return {
        ok: false,
        result: {
          kind: 'repair_required',
          reason: 'base_item_revision_mismatch',
          itemKey: key,
          expectedBaseRevision: baseRev,
          actualItemRevision: existing.item_revision,
        },
      };
    }
  }

  const delta = typeof change.text === 'string' ? change.text : '';
  const updated = cloneItem(existing);
  updated.item_revision = itemRev;
  if (updated.partial) {
    updated.tail_text = (updated.tail_text ?? '') + delta;
  } else {
    updated.text = (updated.text ?? '') + delta;
  }
  if (change.body_end_byte !== undefined) {
    updated.body_end_byte = assertSafeNonNegativeInt(change.body_end_byte, 'change.body_end_byte');
  }
  next.items.set(key, updated);
  dirtyKeys.add(key);
  addReason(reasons, key, 'text');
  addReason(reasons, key, 'height');
  return { ok: true };
}

function applyReplaceBodyChange(next, change, dirtyKeys, reasons) {
  const key = change.key ?? change.item_key;
  if (typeof key !== 'string' || !key) throw new TypeError('replace_body missing key');
  const itemRev = assertSafeNonNegativeInt(change.item_revision, 'change.item_revision');
  const existing = next.items.get(key);
  if (!existing || existing.item_revision >= itemRev) return;

  const updated = cloneItem(existing);
  updated.item_revision = itemRev;
  if (change.text !== undefined) updated.text = change.text;
  if (change.body !== undefined) updated.body = change.body;
  if (change.body_ref !== undefined) updated.body_ref = change.body_ref;
  if (change.content_generation !== undefined) {
    updated.content_generation = change.content_generation;
  }
  if (change.body_end_byte !== undefined) {
    updated.body_end_byte = assertSafeNonNegativeInt(change.body_end_byte, 'change.body_end_byte');
  }
  updated.partial = Boolean(change.partial);
  if (!updated.partial) delete updated.tail_text;
  if (change.status !== undefined) {
    updated.status = change.status;
    if (!ACTIVE_STATUSES.has(change.status)) next.activeKeys.delete(key);
    addReason(reasons, key, 'status');
  }
  next.items.set(key, updated);
  dirtyKeys.add(key);
  addReason(reasons, key, 'text');
  addReason(reasons, key, 'height');
}

function applyRemoveChange(next, change, dirtyKeys, reasons) {
  const key = change.key ?? change.item_key;
  if (typeof key !== 'string' || !key) throw new TypeError('remove missing key');
  if (!next.items.has(key)) return false;
  const item = next.items.get(key);
  next.items.delete(key);
  next.activeKeys.delete(key);
  if (item?.request_id) next.pending.delete(String(item.request_id));
  const idx = next.order.indexOf(key);
  if (idx >= 0) next.order.splice(idx, 1);
  dirtyKeys.add(key);
  addReason(reasons, key, 'remove');
  return true;
}

function applySummaryPatch(next, patch) {
  if (!patch || typeof patch !== 'object') return false;
  let controlChanged = false;
  if (patch.session && typeof patch.session === 'object') {
    next.session = { ...(next.session || {}), ...patch.session };
    controlChanged = true;
  }
  if (patch.run && typeof patch.run === 'object') {
    next.run = { ...(next.run || {}), ...patch.run };
    controlChanged = true;
  } else if (typeof patch.run_status === 'string') {
    next.run = { ...(next.run || {}), status: patch.run_status };
    controlChanged = true;
  }
  if (patch.settings !== undefined) {
    next.settings = patch.settings ? { ...patch.settings } : null;
    controlChanged = true;
  }
  if (patch.pending_settings !== undefined || patch.pendingSettings !== undefined) {
    const val =
      patch.pending_settings !== undefined ? patch.pending_settings : patch.pendingSettings;
    next.pendingSettings = val ? { ...val } : null;
    controlChanged = true;
  }
  if (patch.capabilities && typeof patch.capabilities === 'object') {
    const incomingRev = patch.capabilities.revision ?? 0;
    const currentRev = next.capabilities?.revision ?? -1;
    if (incomingRev >= currentRev) {
      next.capabilities = { ...patch.capabilities };
      controlChanged = true;
    }
  }
  if (patch.stats && typeof patch.stats === 'object') {
    next.stats = { ...(next.stats || {}), ...patch.stats };
  }
  if (patch.pending && typeof patch.pending === 'object') {
    for (const [reqId, val] of Object.entries(patch.pending)) {
      if (val === null) next.pending.delete(reqId);
      else next.pending.set(reqId, typeof val === 'object' ? { ...val } : val);
    }
    controlChanged = true;
  }
  return controlChanged;
}

function reduceProjection(projection, changes, summaryPatch) {
  const next = cloneProjection(projection);
  const dirtyKeys = new Set();
  const reasons = new Map();
  let needsGroupReconcile = false;

  for (const change of changes) {
    if (!change || typeof change !== 'object') {
      throw new TypeError('Invalid batch change entry');
    }
    const op = change.op;
    if (op === 'upsert') {
      const outcome = applyUpsertChange(next, change, dirtyKeys, reasons);
      if (outcome === 'insert') needsGroupReconcile = true;
      else if (outcome === 'update') {
        const item = next.items.get(change.key ?? change.item_key);
        if (item && (COMPACT_KINDS.has(item.kind) || isProcessFailure(item))) {
          needsGroupReconcile = true;
        }
      }
    } else if (op === 'append_text') {
      const res = applyAppendTextChange(next, change, dirtyKeys, reasons);
      if (!res.ok) return { ok: false, result: res.result };
    } else if (op === 'replace_body') {
      applyReplaceBodyChange(next, change, dirtyKeys, reasons);
    } else if (op === 'remove') {
      if (applyRemoveChange(next, change, dirtyKeys, reasons)) {
        needsGroupReconcile = true;
      }
    } else if (op === 'display_ext') {
      // Safe display extension may be skipped if unrecognized.
      continue;
    } else {
      throw new TypeError(`Unsupported projection change op: ${String(op)}`);
    }
  }

  const controlChanged = applySummaryPatch(next, summaryPatch);
  if (controlChanged) {
    addReason(reasons, '__control__', 'control');
  }

  let structureDirty = false;
  if (needsGroupReconcile) {
    const groupRes = reconcileProcessGroups(next.items, next.order, next.groups);
    next.groups = groupRes.groups;
    if (groupRes.structureChanged) {
      structureDirty = true;
      for (const gk of groupRes.changedGroupKeys) {
        dirtyKeys.add(gk);
        addReason(reasons, gk, 'group_boundary');
      }
    }
  }
  return { ok: true, projection: next, dirtyKeys, reasons, structureDirty };
}

function autoReconcileOverlaysForRevision(pendingCommands, revision) {
  if (!pendingCommands.size) return pendingCommands;
  let next = null;
  for (const [receipt, overlay] of pendingCommands) {
    if (
      overlay.status === 'accepted' &&
      overlay.appliedRevision !== null &&
      overlay.appliedRevision !== undefined &&
      revision >= overlay.appliedRevision
    ) {
      if (!next) next = new Map(pendingCommands);
      next.set(receipt, { ...overlay, status: 'applied' });
    }
  }
  return next || pendingCommands;
}

export function applyNativeBatch(current, verifiedBatch, ledgerLimits) {
  if (!isVerifiedBatch(verifiedBatch)) {
    throw new TypeError('unverified batch');
  }
  const batch = verifiedBatch.message;
  if (batch.scope_key !== current.scopeKey || batch.session_id !== current.sessionId) {
    return { kind: 'blocked', reason: 'scope' };
  }
  if (batch.schema_version !== current.schemaVersion) {
    return { kind: 'blocked', reason: 'schema' };
  }
  if (batch.stream_epoch !== current.streamEpoch) {
    return { kind: 'reset', reason: 'stream_epoch' };
  }

  const interval = `${batch.from_revision}:${batch.to_revision}`;
  if (batch.to_revision <= current.revision) {
    const known = current.recentBatches.get(interval);
    if (!known) return { kind: 'resume', reason: 'unproven_duplicate' };
    const same =
      known.digest === verifiedBatch.digest &&
      known.fromCursor === batch.from_cursor &&
      known.toCursor === batch.to_cursor;
    return same ? { kind: 'duplicate' } : { kind: 'reset', reason: 'conflicting_batch' };
  }
  if (batch.from_revision !== current.revision || batch.to_revision !== batch.from_revision + 1) {
    return { kind: 'reset', reason: 'revision_gap' };
  }
  if (batch.from_cursor !== current.appliedCursor || batch.to_cursor < batch.from_cursor) {
    return { kind: 'reset', reason: 'cursor' };
  }

  const reduced = reduceProjection(current.projection, batch.changes, batch.summary_patch);
  if (!reduced.ok) {
    return reduced.result;
  }

  const mergedDirty = new Set(current.dirtyKeys);
  for (const k of reduced.dirtyKeys) mergedDirty.add(k);

  const mergedReasons = new Map(current.changeReasons);
  for (const [k, rset] of reduced.reasons) {
    for (const r of rset) addReason(mergedReasons, k, r);
  }

  const nextValue = {
    ...current,
    revision: batch.to_revision,
    appliedCursor: batch.to_cursor,
    resumeToken: batch.resume_token ?? current.resumeToken,
    projection: reduced.projection,
    recentBatches: rememberVerifiedBatch(
      current.recentBatches,
      interval,
      {
        digest: verifiedBatch.digest,
        fromCursor: batch.from_cursor,
        toCursor: batch.to_cursor,
      },
      ledgerLimits,
    ),
    pendingCommands: autoReconcileOverlaysForRevision(current.pendingCommands, batch.to_revision),
    dirtyKeys: mergedDirty,
    changeReasons: mergedReasons,
    structureDirty: current.structureDirty || reduced.structureDirty,
  };

  return {
    kind: 'applied',
    value: nextValue,
    dirtyKeys: reduced.dirtyKeys,
    changeReasons: reduced.reasons,
    structureDirty: reduced.structureDirty,
  };
}

export function mergeHistoryPage(current, page) {
  if (!page || typeof page !== 'object' || !Array.isArray(page.items)) {
    throw new TypeError('History page must include items array');
  }
  if (page.scope_key !== undefined && page.scope_key !== current.scopeKey) {
    throw new Error('History page scope_key mismatch');
  }
  if (page.session_id !== undefined && page.session_id !== current.sessionId) {
    throw new Error('History page session_id mismatch');
  }
  if (page.stream_epoch !== undefined && page.stream_epoch !== current.streamEpoch) {
    throw new Error('History page stream_epoch mismatch');
  }

  const nextProj = cloneProjection(current.projection);
  const dirtyKeys = new Set(current.dirtyKeys);
  const reasons = new Map(current.changeReasons);
  let inserted = false;

  for (const rawItem of page.items) {
    const incoming = normalizeSnapshotItem(rawItem);
    const existing = nextProj.items.get(incoming.key);
    if (existing && existing.item_revision >= incoming.item_revision) {
      continue;
    }
    nextProj.items.set(incoming.key, incoming);
    dirtyKeys.add(incoming.key);
    if (!existing) {
      insertOrderKey(nextProj.order, nextProj.items, incoming.key, incoming);
      addReason(reasons, incoming.key, 'insert');
      addReason(reasons, incoming.key, 'height');
      inserted = true;
    } else {
      addReason(reasons, incoming.key, 'text');
      addReason(reasons, incoming.key, 'height');
    }
  }

  let structureDirty = current.structureDirty;
  if (inserted) {
    const groupRes = reconcileProcessGroups(nextProj.items, nextProj.order, nextProj.groups);
    nextProj.groups = groupRes.groups;
    if (groupRes.structureChanged) {
      structureDirty = true;
      for (const gk of groupRes.changedGroupKeys) {
        dirtyKeys.add(gk);
        addReason(reasons, gk, 'group_boundary');
      }
    }
  }

  const pageRev =
    page.page_revision !== undefined
      ? assertSafeNonNegativeInt(page.page_revision, 'page_revision')
      : nextProj.history.pageRevision;
  const pageCursor =
    page.page_applied_cursor !== undefined
      ? assertSafeNonNegativeInt(page.page_applied_cursor, 'page_applied_cursor')
      : nextProj.history.pageAppliedCursor;

  nextProj.history = {
    hasMore: Boolean(page.has_more ?? page.hasMore),
    nextToken: page.next_token ?? page.nextToken ?? null,
    pageRevision: pageRev,
    pageAppliedCursor: pageCursor,
  };

  // Never advance main subscription watermarks (revision, appliedCursor, resumeToken)
  // and always preserve activeKeys & pending sets.
  return {
    ...current,
    projection: nextProj,
    dirtyKeys,
    changeReasons: reasons,
    structureDirty,
  };
}

export function mergeBodyChunk(current, itemKey, chunk) {
  if (typeof itemKey !== 'string' || !itemKey) {
    throw new TypeError('itemKey must be a non-empty string');
  }
  if (!chunk || typeof chunk !== 'object') {
    throw new TypeError('chunk must be an object');
  }
  const existing = current.projection.items.get(itemKey);
  if (!existing) {
    throw new Error(`Unknown itemKey for body chunk: ${itemKey}`);
  }
  const startByte = assertSafeNonNegativeInt(chunk.start_byte, 'chunk.start_byte');
  const endByte = assertSafeNonNegativeInt(chunk.end_byte, 'chunk.end_byte');
  const totalBytes = assertSafeNonNegativeInt(chunk.total_bytes, 'chunk.total_bytes');
  if (endByte < startByte || endByte > totalBytes) {
    throw new RangeError('Invalid body chunk byte range');
  }
  const text = typeof chunk.text === 'string' ? chunk.text : '';
  if (utf8ByteLength(text) !== endByte - startByte) {
    throw new RangeError('Body chunk UTF-8 byte length mismatch');
  }

  const generation = chunk.content_generation;
  if (generation === undefined || generation === null || generation === '') {
    throw new TypeError('Body chunk missing content_generation');
  }

  const existingGen = existing.content_generation ?? null;
  const isNewGeneration = existingGen !== null && generation !== existingGen;
  if (isNewGeneration && startByte !== 0) {
    throw new Error('Body content_generation mismatch on non-zero offset');
  }

  const expectedStart =
    isNewGeneration || existingGen === null ? 0 : (existing.body_loaded_bytes ?? 0);
  if (startByte !== expectedStart) {
    throw new Error(
      `Body chunk byte offset discontinuity: expected ${expectedStart}, got ${startByte}`,
    );
  }

  const nextProj = cloneProjection(current.projection);
  const updated = cloneItem(existing);
  updated.content_generation = generation;
  if (chunk.content_revision !== undefined) {
    updated.content_revision = assertSafeNonNegativeInt(
      chunk.content_revision,
      'chunk.content_revision',
    );
  }
  updated.body = startByte === 0 ? text : (updated.body ?? '') + text;
  updated.text = updated.body;
  updated.body_loaded_bytes = endByte;
  updated.total_bytes = totalBytes;
  updated.partial = Boolean(chunk.has_more) || endByte < totalBytes;
  if (!updated.partial) delete updated.tail_text;

  nextProj.items.set(itemKey, updated);
  const dirtyKeys = new Set(current.dirtyKeys);
  dirtyKeys.add(itemKey);
  const reasons = new Map(current.changeReasons);
  addReason(reasons, itemKey, 'text');
  addReason(reasons, itemKey, 'height');

  return {
    ...current,
    projection: nextProj,
    dirtyKeys,
    changeReasons: reasons,
  };
}

export function upsertCommandOverlay(current, overlay) {
  if (!overlay || typeof overlay !== 'object') {
    throw new TypeError('overlay must be an object');
  }
  const receipt = assertHex32(overlay.receipt, 'overlay.receipt');
  const generation = assertSafeNonNegativeInt(overlay.generation ?? 0, 'overlay.generation');
  const existing = current.pendingCommands.get(receipt);
  if (existing && existing.generation > generation) {
    return current;
  }
  const appliedRevision =
    overlay.appliedRevision !== undefined && overlay.appliedRevision !== null
      ? assertSafeNonNegativeInt(overlay.appliedRevision, 'overlay.appliedRevision')
      : (existing?.appliedRevision ?? null);
  let status = overlay.status ?? 'pending';
  if (status === 'accepted' && appliedRevision !== null && current.revision >= appliedRevision) {
    status = 'applied';
  }
  const nextCommands = new Map(current.pendingCommands);
  nextCommands.set(receipt, {
    receipt,
    generation,
    action: String(overlay.action ?? existing?.action ?? 'command'),
    status,
    requested:
      overlay.requested !== undefined ? { ...overlay.requested } : (existing?.requested ?? null),
    actual: overlay.actual !== undefined ? { ...overlay.actual } : (existing?.actual ?? null),
    partial: Boolean(overlay.partial ?? existing?.partial),
    outcome: overlay.outcome ?? existing?.outcome ?? null,
    appliedRevision,
    error: overlay.error ?? null,
  });
  return {
    ...current,
    pendingCommands: nextCommands,
  };
}

export function reconcileCommandOverlay(current, signal) {
  if (!signal || typeof signal !== 'object') {
    throw new TypeError('signal must be an object');
  }
  const receipt = assertHex32(signal.receipt, 'signal.receipt');
  const existing = current.pendingCommands.get(receipt);
  if (!existing) return current;
  if (signal.generation !== undefined) {
    const gen = assertSafeNonNegativeInt(signal.generation, 'signal.generation');
    if (gen < existing.generation) return current;
  }

  let status = existing.status;
  let partial = existing.partial;
  const outcome = signal.outcome ?? existing.outcome;
  const appliedRevision =
    signal.appliedRevision !== undefined && signal.appliedRevision !== null
      ? assertSafeNonNegativeInt(signal.appliedRevision, 'signal.appliedRevision')
      : existing.appliedRevision;

  if (signal.status === 'accepted') {
    status =
      appliedRevision !== null && current.revision >= appliedRevision ? 'applied' : 'accepted';
  } else if (signal.status === 'failed' || outcome === 'rejected') {
    status = 'failed';
  } else if (signal.status === 'uncertain' || outcome === 'unknown') {
    status = 'uncertain';
  } else if (outcome === 'partial') {
    status = 'applied';
    partial = true;
  } else if (signal.status === 'applied' || outcome === 'applied') {
    status = 'applied';
    partial = Boolean(signal.partial);
  }

  const nextCommands = new Map(current.pendingCommands);
  nextCommands.set(receipt, {
    ...existing,
    status,
    partial,
    outcome,
    appliedRevision,
    actual: signal.actual !== undefined ? { ...signal.actual } : existing.actual,
    error: signal.error !== undefined ? signal.error : existing.error,
  });

  const nextProj =
    signal.actual !== undefined && existing.action === 'settings'
      ? {
          ...current.projection,
          settings: {
            ...(current.projection.settings || {}),
            ...signal.actual,
          },
          pendingSettings: status === 'applied' ? null : current.projection.pendingSettings,
        }
      : current.projection;

  return {
    ...current,
    projection: nextProj,
    pendingCommands: nextCommands,
  };
}
