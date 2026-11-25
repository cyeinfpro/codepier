import {
  assertHex32,
  assertSafeNonNegativeInt,
  decodeSnapshot,
} from "./native-contracts.mjs";

const DEFAULT_MAX_ENTRIES = 20;
const DEFAULT_MAX_BYTES = 8 * 1024 * 1024;
const DEFAULT_TTL_MS = 3600_000;
const TEXT_ENCODER = new TextEncoder();

function assertSerializableValue(value, seen = new Set(), depth = 0) {
  if (depth > 64) throw new RangeError("Cache envelope nesting too deep");
  if (value === null || value === undefined) return;
  const kind = typeof value;
  if (kind === "boolean" || kind === "number" || kind === "string") return;
  if (kind === "function" || kind === "symbol" || kind === "bigint") {
    throw new TypeError(`Non-cacheable value type: ${kind}`);
  }
  if (
    typeof globalThis.Node === "function" &&
    value instanceof globalThis.Node
  ) {
    throw new TypeError("DOM Node cannot be stored in NativeSnapshotCache");
  }
  if (
    typeof globalThis.EventSource === "function" &&
    value instanceof globalThis.EventSource
  ) {
    throw new TypeError("EventSource cannot be stored in NativeSnapshotCache");
  }
  if (
    value &&
    typeof value.nodeType === "number" &&
    typeof value.nodeName === "string"
  ) {
    throw new TypeError(
      "DOM-like node cannot be stored in NativeSnapshotCache",
    );
  }
  if (seen.has(value)) {
    throw new TypeError(
      "Circular reference cannot be stored in NativeSnapshotCache",
    );
  }
  seen.add(value);
  if (Array.isArray(value)) {
    for (const item of value) assertSerializableValue(item, seen, depth + 1);
  } else if (value instanceof Map || value instanceof Set) {
    throw new TypeError(
      "Map/Set must be serialized into plain JSON structures before caching",
    );
  } else {
    for (const val of Object.values(value)) {
      assertSerializableValue(val, seen, depth + 1);
    }
  }
  seen.delete(value);
}

function serializeStateSnapshot(stateOrEnvelope) {
  if (
    stateOrEnvelope.snapshot &&
    typeof stateOrEnvelope.snapshot === "object"
  ) {
    const snap = decodeSnapshot(stateOrEnvelope.snapshot);
    return {
      snapshot: snap,
      revision: assertSafeNonNegativeInt(
        stateOrEnvelope.revision ?? snap.projection_revision,
        "revision",
      ),
      appliedCursor: assertSafeNonNegativeInt(
        stateOrEnvelope.appliedCursor ?? snap.applied_cursor,
        "appliedCursor",
      ),
      streamEpoch: String(stateOrEnvelope.streamEpoch ?? snap.stream_epoch),
      resumeToken: stateOrEnvelope.resumeToken ?? snap.resume_token ?? null,
    };
  }

  const state = stateOrEnvelope;
  if (!state || typeof state !== "object" || !state.projection) {
    throw new TypeError(
      "Cache entry must be a native state or snapshot envelope",
    );
  }
  const items = [];
  for (const key of state.projection.order || []) {
    const item = state.projection.items.get(key);
    if (item) items.push({ ...item });
  }
  const pending = [];
  for (const [requestId, val] of state.projection.pending || []) {
    pending.push(
      val && typeof val === "object"
        ? { request_id: requestId, ...val }
        : { request_id: requestId },
    );
  }
  const snapshot = decodeSnapshot({
    schema_version: state.schemaVersion ?? 2,
    scope_key: state.scopeKey,
    session_id: state.sessionId,
    stream_epoch: state.streamEpoch,
    projection_revision: state.revision,
    applied_cursor: state.appliedCursor,
    resume_token: state.resumeToken ?? null,
    items,
    active_keys: [...(state.projection.activeKeys || [])],
    pending,
    session: state.projection.session ?? null,
    run: state.projection.run ?? null,
    settings: state.projection.settings ?? null,
    pending_settings: state.projection.pendingSettings ?? null,
    capabilities: state.projection.capabilities ?? null,
    stats: state.projection.stats ?? null,
    history: state.projection.history ?? null,
  });
  return {
    snapshot,
    revision: assertSafeNonNegativeInt(state.revision, "revision"),
    appliedCursor: assertSafeNonNegativeInt(
      state.appliedCursor,
      "appliedCursor",
    ),
    streamEpoch: String(state.streamEpoch),
    resumeToken: state.resumeToken ?? null,
  };
}

export class NativeSnapshotCache {
  constructor({
    maxEntries = DEFAULT_MAX_ENTRIES,
    maxBytes = DEFAULT_MAX_BYTES,
    ttlMs = DEFAULT_TTL_MS,
    storage = null,
    now = () => Date.now(),
  } = {}) {
    this.maxEntries = assertSafeNonNegativeInt(maxEntries, "maxEntries");
    this.maxBytes = assertSafeNonNegativeInt(maxBytes, "maxBytes");
    this.ttlMs = assertSafeNonNegativeInt(ttlMs, "ttlMs");
    this.storage = storage;
    this.now = now;
    this.entries = new Map();
    this.pendingWrites = new Map();
    this.scheduledFlush = null;
  }

  static makeKey({ scopeKey, sessionId, schemaVersion = 2 }) {
    if (typeof scopeKey !== "string" || !scopeKey) {
      throw new TypeError("scopeKey must be a non-empty string");
    }
    const sid = assertHex32(sessionId, "sessionId");
    const ver = assertSafeNonNegativeInt(schemaVersion, "schemaVersion");
    return `${scopeKey}::${sid}::v${ver}`;
  }

  _deleteKey(key) {
    this.entries.delete(key);
    this.pendingWrites.delete(key);
    if (this.storage) {
      if (typeof this.storage.removeItem === "function")
        this.storage.removeItem(key);
      else if (typeof this.storage.delete === "function")
        this.storage.delete(key);
    }
  }

  _totalBytes() {
    let total = 0;
    for (const record of this.entries.values()) {
      total += record.bytes;
    }
    return total;
  }

  _writeRecord(key, serialized, bytes, savedAt) {
    this.entries.delete(key);
    this.entries.set(key, { serialized, bytes, savedAt });
    if (this.storage) {
      if (typeof this.storage.setItem === "function")
        this.storage.setItem(key, serialized);
      else if (typeof this.storage.set === "function")
        this.storage.set(key, serialized);
    }
    this.prune(savedAt);
  }

  _validateEnvelope(rawEnvelope, identity) {
    const parsed =
      typeof rawEnvelope === "string" ? JSON.parse(rawEnvelope) : rawEnvelope;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
      return null;
    const savedAt = parsed.savedAt;
    if (typeof savedAt !== "number" || !Number.isFinite(savedAt)) return null;
    if (this.now() - savedAt > this.ttlMs) return null;
    const snap = decodeSnapshot(parsed.snapshot);
    if (
      snap.scope_key !== identity.scopeKey ||
      snap.session_id !== identity.sessionId
    ) {
      return null;
    }
    const revision = assertSafeNonNegativeInt(parsed.revision, "revision");
    const appliedCursor = assertSafeNonNegativeInt(
      parsed.appliedCursor,
      "appliedCursor",
    );
    if (typeof parsed.streamEpoch !== "string" || !parsed.streamEpoch)
      return null;
    return {
      snapshot: snap,
      revision,
      appliedCursor,
      streamEpoch: parsed.streamEpoch,
      resumeToken: parsed.resumeToken ?? null,
      savedAt,
    };
  }

  get(identity) {
    const key = NativeSnapshotCache.makeKey(identity);
    let record = this.entries.get(key);
    let raw = record ? record.serialized : null;
    if (!raw && this.storage) {
      if (typeof this.storage.getItem === "function")
        raw = this.storage.getItem(key);
      else if (typeof this.storage.get === "function")
        raw = this.storage.get(key);
    }
    if (!raw) return null;
    try {
      const envelope = this._validateEnvelope(raw, identity);
      if (!envelope) {
        this._deleteKey(key);
        return null;
      }
      if (record) {
        this.entries.delete(key);
        this.entries.set(key, record);
      } else {
        const bytes = TEXT_ENCODER.encode(raw).length;
        this.entries.set(key, {
          serialized: raw,
          bytes,
          savedAt: envelope.savedAt,
        });
        this.prune();
      }
      return envelope;
    } catch {
      this._deleteKey(key);
      return null;
    }
  }

  set(stateOrEnvelope, ownerCheck = null) {
    if (typeof ownerCheck === "function" && !ownerCheck()) return false;
    assertSerializableValue(
      stateOrEnvelope?.snapshot ?? stateOrEnvelope?.projection?.session ?? null,
    );
    const core = serializeStateSnapshot(stateOrEnvelope);
    const envelope = {
      ...core,
      savedAt: this.now(),
    };
    assertSerializableValue(envelope);
    const key = NativeSnapshotCache.makeKey({
      scopeKey: envelope.snapshot.scope_key,
      sessionId: envelope.snapshot.session_id,
      schemaVersion: envelope.snapshot.schema_version,
    });
    const serialized = JSON.stringify(envelope);
    const bytes = TEXT_ENCODER.encode(serialized).length;
    if (bytes > this.maxBytes || this.maxEntries === 0) {
      this._deleteKey(key);
      return false;
    }
    this._writeRecord(key, serialized, bytes, envelope.savedAt);
    return true;
  }

  enqueue(stateOrEnvelope, ownerCheck = null, options = {}) {
    if (typeof ownerCheck === "function" && !ownerCheck()) return false;
    const defer = options.immutableRevision === true && !stateOrEnvelope.snapshot;
    const core = defer ? null : serializeStateSnapshot(stateOrEnvelope);
    const envelope = core && { ...core, savedAt: this.now() };
    if (envelope) assertSerializableValue(envelope);
    const key = NativeSnapshotCache.makeKey({
      scopeKey: envelope?.snapshot.scope_key ?? stateOrEnvelope.scopeKey,
      sessionId: envelope?.snapshot.session_id ?? stateOrEnvelope.sessionId,
      schemaVersion: envelope?.snapshot.schema_version ?? stateOrEnvelope.schemaVersion ?? 2,
    });
    this.pendingWrites.set(key, {
      envelope,
      revisionState: defer ? stateOrEnvelope : null,
      ownerCheck,
    });
    if (!this.scheduledFlush) {
      const flush = () => {
        this.scheduledFlush = null;
        this.flush();
      };
      this.scheduledFlush = defer ? setTimeout(flush, 24) : Promise.resolve().then(flush);
    }
    return true;
  }

  flush() {
    if (!this.pendingWrites.size) return 0;
    let written = 0;
    const batch = [...this.pendingWrites.entries()];
    this.pendingWrites.clear();
    for (const [key, entry] of batch) {
      if (typeof entry.ownerCheck === "function" && !entry.ownerCheck())
        continue;
      let envelope = entry.envelope;
      if (!envelope) {
        try {
          envelope = { ...serializeStateSnapshot(entry.revisionState), savedAt: this.now() };
          assertSerializableValue(envelope);
        } catch {
          // Snapshot persistence is best effort; never cache an invalid revision.
          continue;
        }
      }
      const serialized = JSON.stringify(envelope);
      const bytes = TEXT_ENCODER.encode(serialized).length;
      if (bytes > this.maxBytes || this.maxEntries === 0) {
        this._deleteKey(key);
        continue;
      }
      this._writeRecord(key, serialized, bytes, envelope.savedAt);
      written += 1;
    }
    return written;
  }

  invalidateScope(scopeKey) {
    if (typeof scopeKey !== "string" || !scopeKey) return 0;
    const prefix = `${scopeKey}::`;
    let removed = 0;
    for (const key of [...this.entries.keys()]) {
      if (key.startsWith(prefix)) {
        this._deleteKey(key);
        removed += 1;
      }
    }
    for (const key of [...this.pendingWrites.keys()]) {
      if (key.startsWith(prefix)) this.pendingWrites.delete(key);
    }
    if (this.storage && typeof this.storage.keys === "function") {
      for (const key of [...this.storage.keys()]) {
        if (typeof key === "string" && key.startsWith(prefix)) {
          this._deleteKey(key);
        }
      }
    }
    return removed;
  }

  invalidateAll() {
    const keys = [...this.entries.keys()];
    this.pendingWrites.clear();
    for (const key of keys) {
      this._deleteKey(key);
    }
    if (this.storage && typeof this.storage.clear === "function") {
      this.storage.clear();
    }
    return keys.length;
  }

  prune(referenceTime = this.now()) {
    for (const [key, record] of [...this.entries.entries()]) {
      if (referenceTime - record.savedAt > this.ttlMs) {
        this._deleteKey(key);
      }
    }
    let totalBytes = this._totalBytes();
    for (const [key, record] of [...this.entries.entries()]) {
      if (this.entries.size <= this.maxEntries && totalBytes <= this.maxBytes)
        break;
      this._deleteKey(key);
      totalBytes -= record.bytes;
    }
  }

  stats() {
    return {
      entries: this.entries.size,
      bytes: this._totalBytes(),
      maxEntries: this.maxEntries,
      maxBytes: this.maxBytes,
      pending: this.pendingWrites.size,
    };
  }
}
