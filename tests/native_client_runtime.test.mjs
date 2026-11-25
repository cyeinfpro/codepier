import assert from 'node:assert/strict';
import test from 'node:test';

import { computeSemanticBatchDigestSync, decodeAndVerifyBatch } from '../web/core/native-contracts.mjs';
import { NativeSnapshotCache } from '../web/core/native-cache.mjs';
import {
  ToolOutputSubscriptionManager,
  acquireScopeConsumer,
  applyBoundedSnapshotReplacement,
  parseSseFrames,
  startNativeTransport,
} from '../web/core/native-runtime.mjs';
import {
  applyNativeBatch,
  createInitialNativeState,
  installNativeSnapshot,
  mergeBodyChunk,
  mergeHistoryPage,
  reconcileCommandOverlay,
  reconcileProcessGroups,
  upsertCommandOverlay,
} from '../web/core/native-state.mjs';
import {
  HeightIndex,
  PinSet,
  captureAnchor,
  commitHeightBatch,
  flushVisibleDirty,
  resolveSurvivingAnchor,
  visibleRange,
} from '../web/core/native-window.mjs';

const SID = 'a'.repeat(32);
const RECEIPT_1 = 'b'.repeat(32);
const RECEIPT_2 = 'c'.repeat(32);
const SCOPE_KEY = 'scope-alpha';

function makeSnapshot(overrides = {}) {
  return {
    schema_version: 2,
    scope_key: SCOPE_KEY,
    session_id: SID,
    stream_epoch: 'epoch-1',
    projection_revision: 10,
    applied_cursor: 1000,
    resume_token: 'tok-10',
    items: [
      {
        item_key: 'msg:1',
        kind: 'user',
        first_cursor: 100,
        item_revision: 1,
        text: 'hello',
      },
      {
        item_key: 'tool:1',
        kind: 'tool',
        receipt: RECEIPT_1,
        status: 'completed',
        first_cursor: 200,
        item_revision: 2,
        text: 'ok',
      },
      {
        item_key: 'msg:2',
        kind: 'assistant',
        first_cursor: 300,
        item_revision: 3,
        text: 'world',
      },
    ],
    settings: { model: 'pi/default', effort: 'medium' },
    capabilities: { revision: 1, steer: true },
    ...overrides,
  };
}

async function makeVerifiedBatch(fields, identity = { scopeKey: SCOPE_KEY, sessionId: SID }) {
  const base = {
    kind: 'batch',
    schema_version: 2,
    scope_key: SCOPE_KEY,
    session_id: SID,
    stream_epoch: 'epoch-1',
    from_revision: 10,
    to_revision: 11,
    from_cursor: 1000,
    to_cursor: 1200,
    resume_token: 'tok-11',
    changes: [],
    summary_patch: {},
    ...fields,
  };
  const digest = computeSemanticBatchDigestSync(base);
  return decodeAndVerifyBatch({ ...base, payload_digest: digest }, identity);
}

test('native-state enforces verified batches, watermarks, ledger duplicates vs conflicts, and repair', async () => {
  const initial = createInitialNativeState({ scopeKey: SCOPE_KEY, sessionId: SID, streamEpoch: 'epoch-1' });
  assert.equal(initial.revision, 0);
  assert.equal(initial.appliedCursor, 0);

  const state = installNativeSnapshot(makeSnapshot(), { scopeKey: SCOPE_KEY, sessionId: SID });
  assert.equal(state.revision, 10);
  assert.equal(state.appliedCursor, 1000);
  assert.deepEqual(state.projection.order, ['msg:1', 'tool:1', 'msg:2']);
  assert.equal(state.projection.groups.size, 1);

  assert.throws(
    () =>
      applyNativeBatch(state, {
        kind: 'batch',
        scope_key: SCOPE_KEY,
        session_id: SID,
      }),
    /unverified batch/,
  );

  // Scope / schema / epoch guards.
  const wrongScope = await makeVerifiedBatch({ scope_key: 'other-scope' }, null);
  assert.deepEqual(applyNativeBatch(state, wrongScope), { kind: 'blocked', reason: 'scope' });

  const wrongEpoch = await makeVerifiedBatch({ stream_epoch: 'epoch-2' });
  assert.deepEqual(applyNativeBatch(state, wrongEpoch), { kind: 'reset', reason: 'stream_epoch' });

  // Normal sequential batch.
  const batch11 = await makeVerifiedBatch({
    from_revision: 10,
    to_revision: 11,
    from_cursor: 1000,
    to_cursor: 1100,
    changes: [
      {
        op: 'append_text',
        key: 'msg:2',
        base_item_revision: 3,
        item_revision: 11,
        text: '!',
      },
    ],
    summary_patch: { run_status: 'running' },
  });
  const res11 = applyNativeBatch(state, batch11);
  assert.equal(res11.kind, 'applied');
  assert.equal(res11.value.revision, 11);
  assert.equal(res11.value.appliedCursor, 1100);
  assert.equal(res11.value.projection.items.get('msg:2').text, 'world!');
  assert.ok(res11.dirtyKeys.has('msg:2'));
  assert.ok(res11.changeReasons.get('msg:2').has('text'));

  // Exact duplicate within ledger -> duplicate.
  assert.deepEqual(applyNativeBatch(res11.value, batch11), { kind: 'duplicate' });

  // Conflicting digest for same interval -> reset.
  const conflicting11 = await makeVerifiedBatch({
    from_revision: 10,
    to_revision: 11,
    from_cursor: 1000,
    to_cursor: 1100,
    changes: [],
  });
  assert.deepEqual(applyNativeBatch(res11.value, conflicting11), {
    kind: 'reset',
    reason: 'conflicting_batch',
  });

  // Unproven old revision (not in ledger) -> resume.
  const oldBatch = await makeVerifiedBatch({
    from_revision: 1,
    to_revision: 2,
    from_cursor: 10,
    to_cursor: 20,
    changes: [],
  });
  assert.deepEqual(applyNativeBatch(res11.value, oldBatch), {
    kind: 'resume',
    reason: 'unproven_duplicate',
  });

  // Revision gap or cursor regression -> reset.
  const gapBatch = await makeVerifiedBatch({
    from_revision: 12,
    to_revision: 13,
    from_cursor: 1100,
    to_cursor: 1200,
  });
  assert.deepEqual(applyNativeBatch(res11.value, gapBatch), {
    kind: 'reset',
    reason: 'revision_gap',
  });

  const cursorGapBatch = await makeVerifiedBatch({
    from_revision: 11,
    to_revision: 12,
    from_cursor: 1099,
    to_cursor: 1200,
  });
  assert.deepEqual(applyNativeBatch(res11.value, cursorGapBatch), {
    kind: 'reset',
    reason: 'cursor',
  });

  // append_text base_item_revision mismatch -> repair_required without mutating state.
  const badBaseBatch = await makeVerifiedBatch({
    from_revision: 11,
    to_revision: 12,
    from_cursor: 1100,
    to_cursor: 1200,
    changes: [
      {
        op: 'append_text',
        key: 'msg:2',
        base_item_revision: 99,
        item_revision: 12,
        text: 'bad',
      },
    ],
  });
  const repairRes = applyNativeBatch(res11.value, badBaseBatch);
  assert.equal(repairRes.kind, 'repair_required');
  assert.equal(repairRes.reason, 'base_item_revision_mismatch');
  assert.equal(res11.value.projection.items.get('msg:2').text, 'world!');
});

test('recentBatches ledger evicts deterministically by count and byte limits', async () => {
  let state = installNativeSnapshot(makeSnapshot(), { scopeKey: SCOPE_KEY, sessionId: SID });
  for (let rev = 10; rev < 15; rev += 1) {
    const b = await makeVerifiedBatch({
      from_revision: rev,
      to_revision: rev + 1,
      from_cursor: 1000 + (rev - 10) * 10,
      to_cursor: 1000 + (rev - 9) * 10,
    });
    const out = applyNativeBatch(state, b, { maxCount: 2, maxBytes: 4096 });
    assert.equal(out.kind, 'applied');
    state = out.value;
  }
  assert.equal(state.recentBatches.size, 2);
  assert.ok(state.recentBatches.has('13:14'));
  assert.ok(state.recentBatches.has('14:15'));
});

test('mergeHistoryPage preserves watermarks, active/pending sets, and stable first_cursor order', () => {
  const state = installNativeSnapshot(
    makeSnapshot({
      active_keys: ['msg:2'],
      pending: [{ request_id: 'req-1', key: 'approval:1' }],
    }),
    { scopeKey: SCOPE_KEY, sessionId: SID },
  );

  const merged = mergeHistoryPage(state, {
    scope_key: SCOPE_KEY,
    session_id: SID,
    stream_epoch: 'epoch-1',
    has_more: true,
    next_token: 'page-2',
    page_revision: 12,
    page_applied_cursor: 1200,
    items: [
      { item_key: 'msg:0', kind: 'user', first_cursor: 50, item_revision: 1, text: 'earlier' },
      // Older revision for msg:1 must be ignored.
      { item_key: 'msg:1', kind: 'user', first_cursor: 100, item_revision: 1, text: 'stale' },
      // Higher revision for msg:2 updates text.
      { item_key: 'msg:2', kind: 'assistant', first_cursor: 300, item_revision: 5, text: 'updated' },
    ],
  });

  // Main watermarks never advance on history paging.
  assert.equal(merged.revision, 10);
  assert.equal(merged.appliedCursor, 1000);
  assert.equal(merged.resumeToken, 'tok-10');
  assert.deepEqual(merged.projection.order, ['msg:0', 'msg:1', 'tool:1', 'msg:2']);
  assert.equal(merged.projection.items.get('msg:1').text, 'hello');
  assert.equal(merged.projection.items.get('msg:2').text, 'updated');
  assert.ok(merged.projection.activeKeys.has('msg:2'));
  assert.ok(merged.projection.pending.has('req-1'));
  assert.equal(merged.projection.history.hasMore, true);
  assert.equal(merged.projection.history.nextToken, 'page-2');
});

test('mergeBodyChunk enforces content_generation, UTF-8 byte continuity, and partial flag', () => {
  let state = installNativeSnapshot(
    makeSnapshot({
      items: [
        {
          item_key: 'msg:long',
          kind: 'assistant',
          first_cursor: 100,
          item_revision: 2,
          body_ref: 'ref-1',
        },
      ],
    }),
    { scopeKey: SCOPE_KEY, sessionId: SID },
  );
  assert.equal(state.projection.items.get('msg:long').partial, true);

  const part1 = '你好'; // 6 UTF-8 bytes
  const part2 = '世界'; // 6 UTF-8 bytes
  state = mergeBodyChunk(state, 'msg:long', {
    content_generation: 1,
    content_revision: 2,
    start_byte: 0,
    end_byte: 6,
    total_bytes: 12,
    has_more: true,
    text: part1,
  });
  assert.equal(state.projection.items.get('msg:long').partial, true);
  assert.equal(state.projection.items.get('msg:long').body, '你好');

  assert.throws(
    () =>
      mergeBodyChunk(state, 'msg:long', {
        content_generation: 1,
        start_byte: 7,
        end_byte: 12,
        total_bytes: 12,
        has_more: false,
        text: 'bad',
      }),
    /discontinuity|length mismatch/,
  );

  assert.throws(
    () =>
      mergeBodyChunk(state, 'msg:long', {
        content_generation: 2,
        start_byte: 6,
        end_byte: 12,
        total_bytes: 12,
        has_more: false,
        text: part2,
      }),
    /content_generation mismatch/,
  );

  state = mergeBodyChunk(state, 'msg:long', {
    content_generation: 1,
    content_revision: 2,
    start_byte: 6,
    end_byte: 12,
    total_bytes: 12,
    has_more: false,
    text: part2,
  });
  assert.equal(state.projection.items.get('msg:long').partial, false);
  assert.equal(state.projection.items.get('msg:long').body, '你好世界');
});

test('command overlay lifecycle handles accepted->applied, generation fencing, and partial readback', async () => {
  let state = installNativeSnapshot(makeSnapshot(), { scopeKey: SCOPE_KEY, sessionId: SID });
  state = upsertCommandOverlay(state, {
    receipt: RECEIPT_1,
    generation: 2,
    action: 'settings',
    status: 'pending',
    requested: { model: 'pi/pro', effort: 'high' },
  });
  assert.equal(state.pendingCommands.get(RECEIPT_1).status, 'pending');

  // Stale generation ignored.
  state = reconcileCommandOverlay(state, {
    receipt: RECEIPT_1,
    generation: 1,
    status: 'failed',
    error: 'stale',
  });
  assert.equal(state.pendingCommands.get(RECEIPT_1).status, 'pending');

  // Accepted with appliedRevision waits for projection catch-up.
  state = reconcileCommandOverlay(state, {
    receipt: RECEIPT_1,
    generation: 2,
    status: 'accepted',
    appliedRevision: 11,
  });
  assert.equal(state.pendingCommands.get(RECEIPT_1).status, 'accepted');

  const batch11 = await makeVerifiedBatch({ from_revision: 10, to_revision: 11 });
  state = applyNativeBatch(state, batch11).value;
  assert.equal(state.pendingCommands.get(RECEIPT_1).status, 'applied');

  // Partial settings readback preserves actuals.
  state = upsertCommandOverlay(state, {
    receipt: RECEIPT_2,
    generation: 3,
    action: 'settings',
    status: 'pending',
    requested: { model: 'pi/new', effort: 'max' },
  });
  state = reconcileCommandOverlay(state, {
    receipt: RECEIPT_2,
    generation: 3,
    outcome: 'partial',
    actual: { model: 'pi/new', effort: 'medium' },
  });
  const ov2 = state.pendingCommands.get(RECEIPT_2);
  assert.equal(ov2.status, 'applied');
  assert.equal(ov2.partial, true);
  assert.deepEqual(ov2.actual, { model: 'pi/new', effort: 'medium' });
  assert.deepEqual(state.projection.settings, { model: 'pi/new', effort: 'medium' });
});

test('reconcileProcessGroups keeps stable group keys and expansion state across updates', () => {
  const items = new Map([
    ['t1', { key: 't1', kind: 'tool', receipt: RECEIPT_1, status: 'running' }],
    ['r1', { key: 'r1', kind: 'reasoning', receipt: RECEIPT_1, status: 'completed' }],
  ]);
  const first = reconcileProcessGroups(items, ['t1', 'r1'], new Map());
  assert.equal(first.groups.size, 1);
  first.groups.get('t1').expanded = true;

  items.set('t1', { key: 't1', kind: 'tool', receipt: RECEIPT_1, status: 'completed' });
  const second = reconcileProcessGroups(items, ['t1', 'r1'], first.groups);
  assert.equal(second.structureChanged, false);
  assert.equal(second.groups.get('t1').expanded, true);
  assert.equal(second.groups.get('t1').live, false);
});

test('NativeSnapshotCache enforces keying, coalesced owner-checked writes, TTL, eviction, and serialization safety', () => {
  let clock = 1000;
  const backing = new Map();
  const cache = new NativeSnapshotCache({
    maxEntries: 2,
    maxBytes: 10000,
    ttlMs: 5000,
    storage: backing,
    now: () => clock,
  });

  const state1 = installNativeSnapshot(makeSnapshot(), { scopeKey: SCOPE_KEY, sessionId: SID });
  let owned = true;
  cache.enqueue(state1, () => owned);
  owned = false;
  assert.equal(cache.flush(), 0); // Aborted owner must not write!

  owned = true;
  cache.enqueue(state1, () => owned);
  assert.equal(cache.flush(), 1);
  const hit = cache.get({ scopeKey: SCOPE_KEY, sessionId: SID, schemaVersion: 2 });
  assert.equal(hit.revision, 10);
  assert.equal(hit.resumeToken, 'tok-10');

  // Rejects DOM-like nodes and functions.
  assert.throws(
    () =>
      cache.set({
        snapshot: makeSnapshot({ session: { bad: { nodeType: 1, nodeName: 'DIV' } } }),
        revision: 10,
        appliedCursor: 1000,
        streamEpoch: 'epoch-1',
      }),
    /DOM-like node/,
  );

  // TTL expiry.
  clock += 6000;
  assert.equal(cache.get({ scopeKey: SCOPE_KEY, sessionId: SID, schemaVersion: 2 }), null);

  // Scope invalidation.
  const state2 = installNativeSnapshot(makeSnapshot({ scope_key: 'scope-beta' }), {
    scopeKey: 'scope-beta',
    sessionId: SID,
  });
  cache.set(state1);
  cache.set(state2);
  cache.invalidateScope(SCOPE_KEY);
  assert.equal(cache.get({ scopeKey: SCOPE_KEY, sessionId: SID }), null);
  assert.ok(cache.get({ scopeKey: 'scope-beta', sessionId: SID }));
  cache.invalidateAll();
  assert.equal(cache.stats().entries, 0);
});

test('parseSseFrames handles split UTF-8 byte chunks cleanly', async () => {
  const payload = JSON.stringify({ text: '中文🚀流式测试' });
  const rawBytes = new TextEncoder().encode(`event: batch\nid: tok-1\ndata: ${payload}\n\n`);
  // Split right inside multi-byte UTF-8 characters.
  const chunks = [];
  for (let i = 0; i < rawBytes.length; i += 2) {
    chunks.push(rawBytes.subarray(i, i + 2));
  }
  const frames = [];
  await parseSseFrames(chunks, async (frame) => {
    frames.push(frame);
  });
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, 'batch');
  assert.equal(frames[0].id, 'tok-1');
  assert.deepEqual(JSON.parse(frames[0].data), { text: '中文🚀流式测试' });
});

test('parseSseFrames accepts split CRLF and discards uncommitted frames at EOF', async () => {
  const committed = 'event: batch\r\nid: 12\r\ndata: {"complete":true}\r\n\r\n';
  const truncated = 'event: batch\r\nid: 13\r\ndata: {"truncated":true}\r\n';
  const bytes = new TextEncoder().encode(committed + truncated);
  for (let split = 1; split < bytes.length; split += 1) {
    const received = [];
    await parseSseFrames([bytes.subarray(0, split), bytes.subarray(split)], (frame) => {
      received.push(frame);
    });
    assert.deepEqual(received, [
      { event: 'batch', id: '12', data: '{"complete":true}', retry: null },
    ], `split at byte ${split}`);
  }
});

test('startNativeTransport uses authoritative resumeToken, bounded catch-up, and halts on reset loop/blocked', async () => {
  const initial = installNativeSnapshot(makeSnapshot(), { scopeKey: SCOPE_KEY, sessionId: SID });
  const cache = new NativeSnapshotCache();
  const seenAfter = [];
  let renderCalls = 0;
  const scope = {
    identity: { scopeKey: SCOPE_KEY, sessionId: SID },
    authoritative: initial,
    cache,
    scheduleRender: () => {
      renderCalls += 1;
    },
  };

  const b11 = {
    kind: 'batch',
    schema_version: 2,
    scope_key: SCOPE_KEY,
    session_id: SID,
    stream_epoch: 'epoch-1',
    from_revision: 10,
    to_revision: 11,
    from_cursor: 1000,
    to_cursor: 1050,
    resume_token: 'tok-11',
    changes: [{ op: 'append_text', key: 'msg:2', base_item_revision: 3, item_revision: 11, text: '!' }],
    summary_patch: {},
  };
  b11.payload_digest = computeSemanticBatchDigestSync(b11);

  let attempt = 0;
  let resetHalted = null;
  const stop = startNativeTransport(
    scope,
    {
      consumeAttempt: async ({ after, onFrame }) => {
        seenAfter.push(after);
        attempt += 1;
        if (attempt === 1) {
          await onFrame(b11);
          // Not yet synchronized: authoritative revision stays at 10!
          assert.equal(scope.authoritative.revision, 10);
          await onFrame({ kind: 'synchronized', stream_epoch: 'epoch-1', projection_revision: 11, applied_cursor: 1050 });
          assert.equal(scope.authoritative.revision, 11);
          return { kind: 'resume', stablySynchronized: true };
        }
        // Second attempt must reconnect with updated authoritative resumeToken ('tok-11'),
        // then receive SOURCE_GAP reset_required and halt instead of looping!
        await onFrame({ kind: 'reset_required', reason: 'SOURCE_GAP', epoch: 'epoch-1' });
        return { kind: 'done' };
      },
      delayAbortable: async () => true,
      retryDelay: () => 1,
      replaceSnapshotBounded: async () => true,
      publishResetHalted: (_s, outcome) => {
        resetHalted = outcome;
      },
    },
  );

  await new Promise((r) => setTimeout(r, 30));
  stop();
  assert.deepEqual(seenAfter, ['tok-10', 'tok-11']);
  assert.equal(resetHalted?.reason, 'SOURCE_GAP');
  assert.equal(renderCalls, 1);

  // Ref-counted consumer lifecycle stops only when last consumer releases.
  let stopped = false;
  const refScope = {
    identity: { scopeKey: SCOPE_KEY, sessionId: SID },
    authoritative: initial,
  };
  const rel1 = acquireScopeConsumer(
    refScope,
    {
      consumeAttempt: async ({ signal }) => {
        await new Promise((resolve) => signal.addEventListener('abort', resolve, { once: true }));
        stopped = true;
        return { kind: 'blocked' };
      },
      publishBlocked: () => {},
    },
    'c1',
  );
  const rel2 = acquireScopeConsumer(refScope, {}, 'c2');
  rel1();
  assert.equal(stopped, false);
  rel2();
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(stopped, true);

  // Snapshot replacement preserves pending commands/receipts.
  refScope.owner = Symbol('own');
  refScope.authoritative = upsertCommandOverlay(refScope.authoritative, {
    receipt: RECEIPT_1,
    generation: 1,
    action: 'prompt',
    status: 'accepted',
  });
  applyBoundedSnapshotReplacement(refScope, makeSnapshot({ projection_revision: 20 }), refScope.owner);
  assert.equal(refScope.authoritative.revision, 20);
  assert.ok(refScope.authoritative.pendingCommands.has(RECEIPT_1));
});

test('native transport retries a read failure without committing staged items or controls', async () => {
  const initial = installNativeSnapshot(makeSnapshot(), { scopeKey: SCOPE_KEY, sessionId: SID });
  const scope = { identity: { scopeKey: SCOPE_KEY, sessionId: SID }, authoritative: initial };
  const resumed = [];
  const commits = [];
  let finish;
  const done = new Promise((resolve) => { finish = resolve; });
  const marker = {
    kind: 'synchronized', stream_epoch: 'epoch-1', projection_revision: 11, applied_cursor: 1050,
  };
  const makeWire = (commandResult) => {
    const wire = {
      kind: 'batch', schema_version: 2, scope_key: SCOPE_KEY, session_id: SID,
      stream_epoch: 'epoch-1', from_revision: 10, to_revision: 11, from_cursor: 1000, to_cursor: 1050,
      resume_token: 'tok-11', changes: [], summary_patch: { command_result: commandResult },
    };
    return { ...wire, payload_digest: computeSemanticBatchDigestSync(wire) };
  };
  const abandoned = makeWire({ receipt: RECEIPT_1, state: 'error' });
  const committed = makeWire({ receipt: RECEIPT_2, state: 'completed' });
  const stop = startNativeTransport(scope, {
    consumeAttempt: async ({ after, onFrame, signal }) => {
      resumed.push(after);
      if (resumed.length === 1) {
        await onFrame(abandoned);
        const broken = new ReadableStream({
          pull(controller) { controller.error(new TypeError('network disconnected')); },
        });
        await parseSseFrames(broken, () => assert.fail('broken read emitted a frame'), signal);
      }
      await onFrame(committed);
      await onFrame(marker);
      await onFrame(committed); // duplicate: must not re-emit command controls
      return { kind: 'blocked' };
    },
    commitApplied: (_scope, next, _owner, patches) => {
      scope.authoritative = next;
      commits.push({ revision: next.revision, patches });
      return true;
    },
    delayAbortable: async () => true,
    publishFatal: (_s, error) => finish({ error }),
    publishBlocked: () => finish({}),
  });
  try {
    assert.deepEqual(await done, {});
    assert.deepEqual(resumed, ['tok-10', 'tok-10']);
    assert.deepEqual(commits, [{
      revision: 11,
      patches: [{ command_result: { receipt: RECEIPT_2, state: 'completed' } }],
    }]);
  } finally {
    stop();
  }
});

test('native transport never commits catch-up on a marker with a different watermark', async () => {
  const scope = {
    identity: { scopeKey: SCOPE_KEY, sessionId: SID },
    authoritative: installNativeSnapshot(makeSnapshot(), { scopeKey: SCOPE_KEY, sessionId: SID }),
  };
  let resolveOutcome;
  const outcome = new Promise((resolve) => { resolveOutcome = resolve; });
  const stop = startNativeTransport(scope, {
    consumeAttempt: async ({ onFrame }) => {
      await onFrame({ kind: 'synchronized', stream_epoch: 'epoch-1', projection_revision: 11, applied_cursor: 1200 });
      return { kind: 'blocked' };
    },
    replaceSnapshotBounded: async (_scope, _owner, _signal, reset) => {
      resolveOutcome(reset.reason);
      return false;
    },
    publishFatal: (_scope, error) => resolveOutcome(error.message),
    publishBlocked: () => resolveOutcome('mistakenly synchronized'),
  });
  try {
    assert.equal(await outcome, 'synchronized_watermark_mismatch');
    assert.equal(scope.authoritative.resumeToken, 'tok-10');
  } finally {
    stop();
  }
});

test('ToolOutputSubscriptionManager manages replace/append tails, discontinuities, and panel close cleanup', () => {
  const unsubscribed = [];
  const mgr = new ToolOutputSubscriptionManager({
    maxTailBytes: 10,
    onUnsubscribe: (k) => unsubscribed.push(k),
  });
  mgr.open('tool:1', { outputEpoch: 'out-1' });
  mgr.receiveFrame('tool:1', {
    kind: 'replace',
    output_epoch: 'out-1',
    from_offset: 0,
    to_offset: 5,
    text: 'hello',
    truncated_before: false,
    running: true,
  });
  let st = mgr.getState('tool:1');
  assert.equal(st.text, 'hello');
  assert.equal(st.toOffset, 5);

  // Continuous append that exceeds maxTailBytes trims from front and marks truncatedBefore.
  mgr.receiveFrame('tool:1', {
    kind: 'append',
    output_epoch: 'out-1',
    from_offset: 5,
    to_offset: 13,
    text: '-world!!',
    running: false,
  });
  st = mgr.getState('tool:1');
  assert.equal(st.truncatedBefore, true);
  assert.ok(st.bytes <= 10);
  assert.equal(st.toOffset, 13);

  // Discontinuous append marks needsReplace rather than silently concatenating.
  mgr.receiveFrame('tool:1', {
    kind: 'append',
    output_epoch: 'out-1',
    from_offset: 99,
    to_offset: 102,
    text: 'gap',
    running: false,
  });
  assert.equal(mgr.getState('tool:1').needsReplace, true);

  assert.equal(mgr.close('tool:1'), true);
  assert.equal(mgr.getState('tool:1'), null);
  assert.deepEqual(unsubscribed, ['tool:1']);
});

test('HeightIndex, visibleRange, anchor survival, PinSet, and flushVisibleDirty work accurately', () => {
  const keys = ['k0', 'k1', 'k2', 'k3', 'k4'];
  const index = new HeightIndex(keys, { defaultHeight: 50 });
  assert.equal(index.count, 5);
  assert.equal(index.indexOfKey('k3'), 3);
  assert.equal(index.offset(5), 250);

  index.setHeight(1, 100); // offsets: [0..50), [50..150), [150..200), [200..250), [250..300)
  assert.equal(index.offset(2), 150);
  assert.equal(index.offset(5), 300);
  assert.equal(index.firstRowWhoseEndExceeds(0), 0);
  assert.equal(index.firstRowWhoseEndExceeds(50), 1);
  assert.equal(index.firstRowWhoseEndExceeds(149), 1);
  assert.equal(index.firstRowWhoseEndExceeds(300), 5);
  assert.equal(index.firstRowStartingAtOrAfter(0), 0);
  assert.equal(index.firstRowStartingAtOrAfter(50), 1);
  assert.equal(index.firstRowStartingAtOrAfter(51), 2);
  assert.equal(index.firstRowStartingAtOrAfter(300), 5);

  const range = visibleRange(index, 60, 100, 10);
  assert.deepEqual(range, { start: 1, end: 3, top: 50, bottom: 100 });

  // Anchor capture and surviving anchor when target row is deleted.
  const view = {
    owner: Symbol('view'),
    userScrollGeneration: 1,
    followBottom: false,
    heights: index,
    el: { scrollTop: 170, clientHeight: 100 },
    overscanPx: 0,
    renderedRange: null,
    renderWindow(r) {
      this.renderedRange = r;
    },
  };
  const anchor = captureAnchor(view);
  assert.equal(anchor.key, 'k2');
  assert.equal(anchor.within, 20);

  // Delete k2 via rebuild, then commitHeightBatch resolves surviving neighbor k3.
  view.heights.rebuild(['k0', 'k1', 'k3', 'k4'], { k1: 100 }, 50);
  const commit = commitHeightBatch(view, [{ key: 'k1', height: 120 }], anchor);
  assert.equal(commit.restored, true);
  assert.equal(view.el.scrollTop, 170); // k3 starts at 50 + 120 = 170

  // If user scrolled concurrently, scrollTop is NOT clobbered.
  const anchor2 = captureAnchor(view);
  view.userScrollGeneration += 1;
  view.el.scrollTop = 10;
  const commitUserScrolled = commitHeightBatch(view, [{ key: 'k0', height: 80 }], anchor2);
  assert.equal(commitUserScrolled.restored, false);
  assert.equal(view.el.scrollTop, 10);

  // PinSet keeps bounded entries and prefers evicting non-approval pins.
  const pins = new PinSet(2);
  pins.pin('k0', 'approval');
  pins.pin('k1', 'focus');
  pins.pin('k4', 'media');
  assert.equal(pins.size, 2);
  assert.equal(pins.has('k0'), true);
  assert.equal(pins.has('k1'), false);
  assert.deepEqual(pins.discreteRanges(view.heights, 1, 3), [
    { start: 0, end: 1 },
    { start: 3, end: 4 },
  ]);

  // flushVisibleDirty respects renderSliceMs budget and preserves overflow dirtyKeys.
  let t = 0;
  const updated = [];
  let scheduled = 0;
  const runtime = {
    frame: 0,
    generation: 1,
    structureDirty: false,
    dirtyKeys: new Set(['k0', 'k1', 'k3']),
    budget: { renderSliceMs: 5 },
    now: () => t,
    requestFrame: (cb) => {
      scheduled += 1;
      return scheduled;
    },
    viewport: { isMounted: () => true },
    updateItem: (item) => {
      updated.push(item.key);
      t += 6; // Exhaust 5ms slice after first item.
    },
  };
  const flushView = {
    items: new Map([
      ['k0', { key: 'k0' }],
      ['k1', { key: 'k1' }],
      ['k3', { key: 'k3' }],
    ]),
  };
  const flushRes = flushVisibleDirty(flushView, runtime);
  assert.equal(flushRes.updated, 1);
  assert.equal(flushRes.remaining, 2);
  assert.deepEqual([...runtime.dirtyKeys], ['k1', 'k3']);
  assert.equal(scheduled, 1);
  assert.equal(resolveSurvivingAnchor(view, { key: 'missing' }, 42), 42);
});

test("transient snapshot replacement failure retries without losing the reset owner", async () => {
  const scope = { identity: { scopeKey: SCOPE_KEY, sessionId: SID }, authoritative: { resumeToken: "known" } };
  const events = [];
  let stream = 0;
  let resolveBlocked;
  const blocked = new Promise(resolve => { resolveBlocked = resolve; });
  let tries = 0;
  startNativeTransport(scope, {
    consumeAttempt: async () => (++stream === 1 ? { kind: "reset", reason: "TRANSIENT_GAP" } : { kind: "blocked" }),
    replaceSnapshotBounded: async () => (++tries === 1 ? "retryable_failure" : "replaced"),
    delayAbortable: async delay => { events.push(delay); },
    publishBlocked: () => { resolveBlocked(); },
  });
  await blocked;
  await Promise.resolve();
  assert.equal(stream, 2);
  assert.equal(tries, 2);
  assert.equal(events.length, 1);
  assert.equal(scope.stopNative, null);
});

test("snapshot access denial reaches the cache invalidation hook without retry", async () => {
  const scope = { identity: { scopeKey: SCOPE_KEY, sessionId: SID }, authoritative: { resumeToken: "known" } };
  const events = [];
  const denied = { kind: "blocked", status: 403 };
  startNativeTransport(scope, {
    consumeAttempt: async () => ({ kind: "reset", reason: "TRANSIENT_GAP" }),
    replaceSnapshotBounded: async () => {
      events.push("snapshot");
      return denied;
    },
    delayAbortable: async () => { events.push("unexpected-retry"); },
    publishResetHalted: () => { events.push("unexpected-halt"); },
    publishBlocked: (sc, outcome) => {
      assert.equal(outcome, denied);
      events.push(sc.identity.scopeKey);
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(events, ["snapshot", SCOPE_KEY]);
  assert.equal(scope.stopNative, null);
});
