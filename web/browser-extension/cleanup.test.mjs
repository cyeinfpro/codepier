import test from 'node:test';
import assert from 'node:assert/strict';
import {BrowserWorkspace} from './workspace.js';

const id = n => String(n).padStart(32, '0');
function fixture(change) {
  const data = {}, tabs = new Map(), updates = [];
  const api = {
    runtime: {getURL: name => 'chrome-extension://fixture/' + name},
    storage: {local: {
      get: async key => ({[key]: structuredClone(data[key])}),
      set: async values => Object.assign(data, structuredClone(values)),
    }},
    tabs: {
      get: async key => {
        if (!tabs.has(key)) throw Error('closed fixture');
        return {...tabs.get(key)};
      },
      update: async (key, values) => {
        updates.push(key);
        if (change === 'update-failed') throw Error('fixture update rejected');
        Object.assign(tabs.get(key), values);
      },
    },
  };
  const state = {version: 1, profile_id: id(99), origins: [], seen: [], pool: []};
  function add(n) {
    tabs.set(n, {id: n, windowId: change === 'moved' ? 2 : 1,
                 active: change === 'active', url: 'https://fixture.invalid/'});
    if (change === 'closed') tabs.delete(n);
    state.pool.push({tab_id: n, window_id: 1, lease_id: id(n), expires: 10000});
  }
  add(1);
  data.codepierWorkspace = structuredClone(state);
  const create = () => new BrowserWorkspace(api);
  return {data, updates, create, state, add, api};
}

for (const change of ['active', 'moved', 'closed', 'update-failed']) {
  test('unconfirmed cleanup remains uncertain after duplicate and restart: ' + change, async () => {
    const f = fixture(change), workspace = f.create();
    const first = await workspace.release(id(1));
    assert.equal(first.tab_cleanup_confirmed, false);
    const calls = f.updates.length;
    assert.deepEqual(await workspace.release(id(1)), first);
    assert.deepEqual(await f.create().release(id(1)), first);
    assert.equal(f.updates.length, calls, 'released user tab is never touched again');
  });
}

test('confirmed cleanup receipt survives duplicate and extension restart', async () => {
  const f = fixture(), workspace = f.create();
  const first = await workspace.release(id(1));
  assert.equal(first.tab_cleanup_confirmed, true);
  assert.deepEqual(await workspace.release(id(1)), first);
  assert.deepEqual(await f.create().release(id(1)), first);
  assert.equal(f.updates.length, 1);
});

test('unknown lease is explicitly unconfirmed, including legacy state after restart', async () => {
  const f = fixture();
  assert.equal((await f.create().release(id(800))).tab_cleanup_confirmed, false);
  assert.equal(f.updates.length, 0);
});

test('cleanup receipts are bounded; evicted receipt never becomes fabricated success', async () => {
  const f = fixture();
  for (let n = 2; n <= 258; n++) f.add(n);
  f.data.codepierWorkspace = structuredClone(f.state);
  const workspace = f.create();
  for (let n = 1; n <= 258; n++) assert.equal((await workspace.release(id(n))).tab_cleanup_confirmed, true);
  const saved = f.data.codepierWorkspace;
  assert.ok(saved.cleanup_receipts.length <= 256);
  assert.equal((await f.create().release(id(1))).tab_cleanup_confirmed, false);
  assert.equal((await f.create().release(id(258))).tab_cleanup_confirmed, true);
  assert.equal(f.updates.length, 258);
});

test('storage error before releasing a lease prevents tab mutation', async () => {
  const f = fixture(), workspace = f.create();
  await workspace.load();
  f.api.storage.local.set = async () => {throw Error('fixture storage full');};
  await assert.rejects(() => workspace.release(id(1)));
  assert.equal(f.updates.length, 0);
});


test('lost final persistence never reclaims a tab after extension restart', async () => {
  const f = fixture(), workspace = f.create();
  await workspace.load();
  const save = f.api.storage.local.set;
  let writes = 0;
  f.api.storage.local.set = async values => {
    if (++writes === 2) throw Error('fixture final receipt write failed');
    return save(values);
  };
  await assert.rejects(() => workspace.release(id(1)));
  assert.equal(f.updates.length, 1);
  f.api.storage.local.set = save;
  assert.equal((await f.create().release(id(1))).tab_cleanup_confirmed, false);
  assert.equal(f.updates.length, 1, 'an uncertain release must not replay tab input');
});
