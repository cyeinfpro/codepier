import {
  assertSafeNonNegativeInt,
  decodeAndVerifyBatch,
} from "./native-contracts.mjs";
import { applyNativeBatch, installNativeSnapshot } from "./native-state.mjs";

const DEFAULT_CATCHUP_MAX_ROWS = 500;
const DEFAULT_CATCHUP_MAX_BYTES = 2 * 1024 * 1024;
const DEFAULT_CATCHUP_MAX_MS = 250;
const DEFAULT_BASE_RETRY_MS = 250;
const DEFAULT_MAX_RETRY_MS = 30_000;
const DEFAULT_TOOL_TAIL_MAX_BYTES = 256 * 1024;
const TEXT_ENCODER = new TextEncoder();

const NON_RETRYABLE_RESET_REASONS = new Set([
  "SOURCE_GAP",
  "PROJECTION_BUILDING",
  "SOURCE_CORRUPT",
]);

function splitLinesPreserveRemainder(buffer, endOfStream = false) {
  const lines = [];
  let start = 0;
  for (let i = 0; i < buffer.length; i += 1) {
    const ch = buffer.charCodeAt(i);
    if (ch === 13 || ch === 10) {
      // A CR at a chunk boundary may still be the first half of CRLF.
      if (ch === 13 && i + 1 === buffer.length && !endOfStream) break;
      lines.push(buffer.slice(start, i));
      if (ch === 13 && buffer.charCodeAt(i + 1) === 10) i += 1;
      start = i + 1;
    }
  }
  return { lines, remainder: buffer.slice(start) };
}

function createSseEventState() {
  return { event: "message", id: null, dataLines: [], retry: null };
}

async function dispatchSseState(state, onFrame) {
  if (
    !state.dataLines.length &&
    state.id === null &&
    state.retry === null &&
    state.event === "message"
  ) {
    return;
  }
  const frame = {
    event: state.event || "message",
    id: state.id,
    data: state.dataLines.join("\n"),
    retry: state.retry,
  };
  await onFrame(frame);
}

function applySseField(state, line) {
  if (!line || line.startsWith(":")) return;
  const colon = line.indexOf(":");
  const field = colon === -1 ? line : line.slice(0, colon);
  let value = colon === -1 ? "" : line.slice(colon + 1);
  if (value.startsWith(" ")) value = value.slice(1);
  if (field === "event") state.event = value || "message";
  else if (field === "data") state.dataLines.push(value);
  else if (field === "id" && !value.includes("\0")) state.id = value;
  else if (field === "retry" && /^\d+$/.test(value))
    state.retry = Number(value);
}

export class NativeTransportReadError extends Error {
  constructor(cause) {
    super("Native stream interrupted", { cause });
    this.name = "NativeTransportReadError";
  }
}

async function readStreamChunk(reader) {
  try {
    return await reader.read();
  } catch (error) {
    throw new NativeTransportReadError(error);
  }
}

async function* iterateChunks(chunkStream, signal) {
  if (chunkStream && typeof chunkStream.getReader === "function") {
    const reader = chunkStream.getReader();
    try {
      while (!signal?.aborted) {
        const { value, done } = await readStreamChunk(reader);
        if (done) break;
        if (value !== undefined) yield value;
      }
    } finally {
      try {
        await reader.cancel();
      } catch {
        // Ignore reader cancel errors during cleanup.
      }
      if (typeof reader.releaseLock === "function") {
        try {
          reader.releaseLock();
        } catch {
          // Ignore releaseLock errors.
        }
      }
    }
    return;
  }
  for await (const chunk of chunkStream) {
    if (signal?.aborted) break;
    yield chunk;
  }
}

export async function parseSseFrames(chunkStream, onFrame, signal = null) {
  if (typeof onFrame !== "function") {
    throw new TypeError("onFrame must be a function");
  }
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let state = createSseEventState();

  for await (const chunk of iterateChunks(chunkStream, signal)) {
    if (signal?.aborted) return;
    const text =
      typeof chunk === "string"
        ? chunk
        : decoder.decode(chunk, { stream: true });
    buffer += text;
    const split = splitLinesPreserveRemainder(buffer);
    buffer = split.remainder;
    for (const line of split.lines) {
      if (signal?.aborted) return;
      if (line === "") {
        await dispatchSseState(state, onFrame);
        state = createSseEventState();
      } else {
        applySseField(state, line);
      }
    }
  }

  if (signal?.aborted) return;
  const tail = buffer + decoder.decode();
  if (tail) {
    const split = splitLinesPreserveRemainder(tail, true);
    for (const line of split.lines) {
      if (line === "") {
        await dispatchSseState(state, onFrame);
        state = createSseEventState();
      } else {
        applySseField(state, line);
      }
    }
  }
  // An event is committed by a blank line, not by the connection closing.
}

export function defaultRetryDelay(
  failures,
  prevDelayMs = DEFAULT_BASE_RETRY_MS,
  random = Math.random,
) {
  const exp = Math.min(
    DEFAULT_MAX_RETRY_MS,
    DEFAULT_BASE_RETRY_MS * 2 ** Math.max(0, failures - 1),
  );
  const upper = Math.min(
    DEFAULT_MAX_RETRY_MS,
    Math.max(DEFAULT_BASE_RETRY_MS, prevDelayMs * 3),
  );
  const cap = Math.max(exp, upper);
  const jittered =
    DEFAULT_BASE_RETRY_MS + random() * (cap - DEFAULT_BASE_RETRY_MS);
  return Math.round(Math.min(DEFAULT_MAX_RETRY_MS, jittered));
}

export function delayAbortable(ms, signal) {
  if (signal?.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    const timer = setTimeout(
      () => {
        cleanup();
        resolve(true);
      },
      Math.max(0, ms),
    );
    const onAbort = () => {
      clearTimeout(timer);
      cleanup();
      resolve(false);
    };
    const cleanup = () => {
      signal?.removeEventListener?.("abort", onAbort);
    };
    signal?.addEventListener?.("abort", onAbort, { once: true });
  });
}

export function resetFingerprint(outcome) {
  if (!outcome || typeof outcome !== "object") return "";
  return [
    outcome.reason ?? "",
    outcome.stream_epoch ?? outcome.epoch ?? "",
    outcome.min_revision ?? "",
    outcome.min_cursor ?? "",
    outcome.target_revision ?? "",
    outcome.retained_from_cursor ?? "",
  ].join("::");
}

export function createCatchUpBuffer(
  authoritative,
  budget = {},
  now = () => Date.now(),
) {
  const maxRows = budget.maxRows ?? DEFAULT_CATCHUP_MAX_ROWS;
  const maxBytes = budget.maxBytes ?? DEFAULT_CATCHUP_MAX_BYTES;
  const maxMs = budget.maxMs ?? DEFAULT_CATCHUP_MAX_MS;
  return {
    working: authoritative,
    rows: 0,
    bytes: 0,
    startedAt: now(),
    maxRows,
    maxBytes,
    maxMs,
    now,
    synchronized: false,
  };
}

export function stageCatchUpBatch(buffer, verifiedBatch, rawBytes = 0) {
  const nextRows = buffer.rows + 1;
  const nextBytes = buffer.bytes + rawBytes;
  const elapsed = buffer.now() - buffer.startedAt;
  if (
    nextRows > buffer.maxRows ||
    nextBytes > buffer.maxBytes ||
    elapsed > buffer.maxMs
  ) {
    return { kind: "budget_exceeded", reason: "catchup_budget_exceeded" };
  }
  const applied = applyNativeBatch(buffer.working, verifiedBatch);
  if (applied.kind === "applied") {
    buffer.working = applied.value;
    buffer.rows = nextRows;
    buffer.bytes = nextBytes;
  } else if (applied.kind === "duplicate") {
    buffer.rows = nextRows;
    buffer.bytes = nextBytes;
  }
  return applied;
}

function defaultCommitApplied(scope, next, owner) {
  if (scope.owner !== owner) return false;
  scope.authoritative = next;
  scope.cache?.enqueue(next, () => scope.owner === owner);
  scope.scheduleRender?.();
  return true;
}

function createRuntimeAttemptIo(scope, io, owner, owns, catchUpBudget, now) {
  const decodeAndVerify =
    io.decodeAndVerify ||
    ((wire, identity) => decodeAndVerifyBatch(wire, identity));
  const commit = io.commitApplied || defaultCommitApplied;
  let synchronized = Boolean(scope.initiallySynchronized);
  let catchUp = synchronized
    ? null
    : createCatchUpBuffer(scope.authoritative, catchUpBudget, now);

  const stagedSummary = [];
  const onVerified = (verified, rawBytes) => {
    const summary = verified.message.summary_patch;
    if (!owns()) return;
    if (synchronized) {
      const outcome = applyNativeBatch(scope.authoritative, verified);
      if (outcome.kind === "applied") {
        commit(scope, outcome.value, owner, summary ? [summary] : [], outcome);
        return;
      }
      if (outcome.kind === "duplicate") return;
      const err = new Error(outcome.reason || outcome.kind);
      err.streamOutcome = outcome;
      throw err;
    }

    const staged = stageCatchUpBatch(catchUp, verified, rawBytes);
    if (staged.kind === "applied") {
      if (summary) stagedSummary.push(summary);
      return;
    }
    if (staged.kind === "duplicate") return;
    const err = new Error(staged.reason || staged.kind);
    err.streamOutcome =
      staged.kind === "budget_exceeded"
        ? { kind: "reset", reason: "catchup_budget_exceeded" }
        : staged;
    throw err;
  };

  const markSynchronized = (wire) => {
    if (!owns()) return;
    const expected = catchUp?.working ?? scope.authoritative;
    if (
      wire.stream_epoch !== expected.streamEpoch ||
      wire.projection_revision !== expected.revision ||
      wire.applied_cursor !== expected.appliedCursor
    ) {
      const err = new Error("synchronized_watermark_mismatch");
      err.streamOutcome = {
        kind: "reset",
        reason: err.message,
        stream_epoch: expected.streamEpoch,
        target_revision: expected.revision,
      };
      throw err;
    }
    if (synchronized) return;
    synchronized = true;
    if (catchUp) {
      catchUp.synchronized = true;
      if (catchUp.working !== scope.authoritative) {
        commit(scope, catchUp.working, owner, stagedSummary);
      }
      catchUp = null;
    }
  };

  return {
    onFrame: async (wire) => {
      if (!owns()) return;
      if (wire && wire.kind === "synchronized") {
        markSynchronized(wire);
        return;
      }
      if (wire && wire.kind === "reset_required") {
        const err = new Error(wire.reason || "reset_required");
        err.streamOutcome = { ...wire, kind: "reset" };
        throw err;
      }
      if (wire && wire.kind === "blocked") {
        const err = new Error(wire.reason || "blocked");
        err.streamOutcome = { ...wire, kind: "blocked" };
        throw err;
      }
      const rawBytes =
        wire?._rawBytes ??
        TEXT_ENCODER.encode(JSON.stringify(wire ?? {})).length;
      const verified = await decodeAndVerify(wire, scope.identity);
      if (!owns()) return;
      onVerified(verified, rawBytes);
    },
    isSynchronized: () => synchronized,
  };
}

export function startNativeTransport(scope, io, options = {}) {
  scope.stopNative?.();
  const owner = Symbol("native-subscription");
  const life = new AbortController();
  scope.owner = owner;
  const owns = () => scope.owner === owner && !life.signal.aborted;
  const stop = () => {
    life.abort();
    if (scope.owner === owner) {
      scope.owner = null;
      scope.stopNative = null;
    }
  };
  scope.stopNative = stop;

  const now = options.now || (() => Date.now());
  const retryDelay = io.retryDelay || defaultRetryDelay;
  const wait = io.delayAbortable || delayAbortable;
  const seenResets = new Set();

  void (async () => {
    let failures = 0;
    let prevDelay = DEFAULT_BASE_RETRY_MS;
    try {
      while (owns()) {
        const runtimeAdapter = createRuntimeAttemptIo(
          scope,
          io,
          owner,
          owns,
          options.catchUpBudget,
          now,
        );
        let outcome;
        try {
          outcome = await io.consumeAttempt({
            identity: scope.identity,
            after: scope.authoritative.resumeToken,
            signal: life.signal,
            onFrame: runtimeAdapter.onFrame,
          });
        } catch (err) {
          if (!owns()) return;
          if (err?.streamOutcome) {
            outcome = err.streamOutcome;
          } else if (err instanceof NativeTransportReadError) {
            outcome = { kind: "resume", stablySynchronized: false };
          } else {
            throw err;
          }
        }
        if (!owns()) return;

        if (outcome?.kind === "blocked") {
          io.publishBlocked?.(scope, outcome);
          return;
        }
        if (outcome?.kind === "reset") {
          const fp = resetFingerprint(outcome);
          const mustHalt =
            NON_RETRYABLE_RESET_REASONS.has(outcome.reason) ||
            (fp && seenResets.has(fp));
          if (mustHalt) {
            io.publishResetHalted?.(scope, outcome);
            return;
          }
          let replaced = false;
          for (let attempt = 0; attempt < 4 && owns(); attempt += 1) {
            const result = await io.replaceSnapshotBounded(
              scope,
              owner,
              life.signal,
              outcome,
            );
            if (!owns() || result === "superseded" || result === "fallback_v1")
              return;
            if (result?.kind === "blocked") {
              io.publishBlocked?.(scope, result);
              return;
            }
            if (result === true || result === "replaced") {
              replaced = true;
              break;
            }
            if (result === "terminal_failure") break;
            if (attempt < 3)
              await wait(retryDelay(attempt + 1, prevDelay), life.signal);
          }
          if (!owns()) return;
          if (!replaced) {
            io.publishResetHalted?.(scope, {
              ...outcome,
              reason: "SNAPSHOT_RECOVERY_FAILED",
            });
            return;
          }
          if (fp) seenResets.add(fp);
          continue;
        }

        const healthy = Boolean(
          outcome?.stablySynchronized && runtimeAdapter.isSynchronized(),
        );
        if (healthy) {
          failures = 0;
          prevDelay = DEFAULT_BASE_RETRY_MS;
          seenResets.clear();
        } else {
          failures += 1;
        }
        const delayMs = retryDelay(failures, prevDelay);
        prevDelay = delayMs;
        await wait(delayMs, life.signal);
      }
    } catch (error) {
      if (owns()) io.publishFatal?.(scope, error);
    } finally {
      if (owns()) stop();
    }
  })();

  return stop;
}

export function acquireScopeConsumer(scope, io, consumerId, options = {}) {
  scope.consumers ??= new Set();
  scope.consumers.add(consumerId);
  if (!scope.stopNative) {
    startNativeTransport(scope, io, options);
  }
  return () => releaseScopeConsumer(scope, consumerId);
}

export function releaseScopeConsumer(scope, consumerId) {
  scope.consumers?.delete(consumerId);
  if (!scope.consumers || scope.consumers.size === 0) {
    scope.stopNative?.();
  }
}

export function applyBoundedSnapshotReplacement(scope, rawSnapshot, owner) {
  if (scope.owner !== owner) return false;
  const next = installNativeSnapshot(rawSnapshot, scope.identity);
  // Preserve pending user commands/receipts & draft across reset.
  next.pendingCommands = new Map(scope.authoritative?.pendingCommands || []);
  scope.authoritative = next;
  scope.cache?.enqueue(next, () => scope.owner === owner);
  scope.scheduleRender?.();
  return true;
}

function trimUtf8Tail(text, maxBytes) {
  const encoded = TEXT_ENCODER.encode(text);
  if (encoded.length <= maxBytes) {
    return { text, bytes: encoded.length, trimmed: false };
  }
  const sliced = encoded.slice(encoded.length - maxBytes);
  // Advance past UTF-8 continuation bytes (10xxxxxx) to keep valid char boundary.
  let offset = 0;
  while (
    offset < sliced.length &&
    (sliced[offset] & 0b1100_0000) === 0b1000_0000
  ) {
    offset += 1;
  }
  const clean = new TextDecoder("utf-8").decode(sliced.subarray(offset));
  return { text: clean, bytes: sliced.length - offset, trimmed: true };
}

export class ToolOutputSubscriptionManager {
  constructor({
    maxTailBytes = DEFAULT_TOOL_TAIL_MAX_BYTES,
    onSubscribe = null,
    onUnsubscribe = null,
  } = {}) {
    this.maxTailBytes = assertSafeNonNegativeInt(maxTailBytes, "maxTailBytes");
    this.onSubscribe = onSubscribe;
    this.onUnsubscribe = onUnsubscribe;
    this.channels = new Map();
  }

  open(toolKey, spec = {}) {
    if (typeof toolKey !== "string" || !toolKey) {
      throw new TypeError("toolKey must be a non-empty string");
    }
    if (!this.channels.has(toolKey)) {
      this.channels.set(toolKey, {
        toolKey,
        outputEpoch: spec.outputEpoch ?? "",
        fromOffset: 0,
        toOffset: 0,
        text: "",
        bytes: 0,
        truncatedBefore: false,
        running: Boolean(spec.running ?? true),
        needsReplace: false,
      });
      this.onSubscribe?.(toolKey, spec);
    }
    return this.channels.get(toolKey);
  }

  close(toolKey) {
    const existed = this.channels.delete(toolKey);
    if (existed) {
      this.onUnsubscribe?.(toolKey);
    }
    return existed;
  }

  closeAll() {
    for (const key of [...this.channels.keys()]) {
      this.close(key);
    }
  }

  getState(toolKey) {
    return this.channels.get(toolKey) || null;
  }

  receiveFrame(toolKey, frame) {
    const channel = this.channels.get(toolKey);
    if (!channel) return null;
    if (!frame || typeof frame !== "object") {
      throw new TypeError("Tool output frame must be an object");
    }
    const fromOffset = assertSafeNonNegativeInt(
      frame.from_offset,
      "frame.from_offset",
    );
    const toOffset = assertSafeNonNegativeInt(
      frame.to_offset,
      "frame.to_offset",
    );
    if (toOffset < fromOffset) {
      throw new RangeError("Tool output frame to_offset < from_offset");
    }
    const epoch = String(frame.output_epoch ?? "");
    const text = typeof frame.text === "string" ? frame.text : "";

    if (frame.kind === "replace") {
      const bounded = trimUtf8Tail(text, this.maxTailBytes);
      channel.outputEpoch = epoch;
      channel.fromOffset = toOffset - bounded.bytes;
      channel.toOffset = toOffset;
      channel.text = bounded.text;
      channel.bytes = bounded.bytes;
      channel.truncatedBefore = Boolean(
        frame.truncated_before || bounded.trimmed || fromOffset > 0,
      );
      channel.running = Boolean(frame.running);
      channel.needsReplace = false;
      return channel;
    }

    if (frame.kind === "append") {
      if (channel.outputEpoch && epoch && channel.outputEpoch !== epoch) {
        channel.needsReplace = true;
        return channel;
      }
      if (fromOffset !== channel.toOffset) {
        channel.needsReplace = true;
        return channel;
      }
      channel.outputEpoch = epoch || channel.outputEpoch;
      const combined = channel.text + text;
      const bounded = trimUtf8Tail(combined, this.maxTailBytes);
      channel.text = bounded.text;
      channel.bytes = bounded.bytes;
      channel.toOffset = toOffset;
      channel.fromOffset = toOffset - bounded.bytes;
      if (frame.truncated_before || bounded.trimmed) {
        channel.truncatedBefore = true;
      }
      channel.running = Boolean(frame.running);
      return channel;
    }

    throw new TypeError(
      `Unsupported tool output frame kind: ${String(frame.kind)}`,
    );
  }
}
