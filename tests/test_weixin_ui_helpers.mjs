import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const channels = readFileSync(new URL('../web/collaboration-room/channels.mjs', import.meta.url), 'utf8');
const identitySource = channels.slice(channels.indexOf('export function channelIdentity('), channels.indexOf('export function channelScopeMarkup(')).replace('export ', '');
const { channelIdentity } = vm.runInNewContext(identitySource + '; ({ channelIdentity })');
test('WeChat identifiers are masked, including identifier-valued provider labels', () => {
  assert.equal(channelIdentity('小林', 'wxid_abcdefghijk'), '小林');
  assert.equal(channelIdentity(null, 'wxid_abcdefghijk'), 'wxid…hijk');
  assert.equal(channelIdentity('wxid_abcdefghijk', 'wxid_abcdefghijk'), 'wxid…hijk');
  assert.equal(channelIdentity('peer_id:abcdefghijk'), 'peer…hijk');
  assert.equal(channelIdentity(null, 'short'), 's…');
  assert.equal(channelIdentity(null, ''), '');
});
test('every visible account label passes through masking', () => {
  assert.ok(!channels.includes('account.label || account.bot_id'));
  assert.ok(!channels.includes('flow.account?.label ||'));
  assert.ok(channels.includes('channelIdentity(account.label, account.bot_id)'));
});
test('wizard keeps pairing and owner verification in the final step', () => {
  const index = readFileSync(new URL('../web/collaboration-room/index.mjs', import.meta.url), 'utf8');
  const source = index.slice(index.indexOf('const channelSteps ='), index.indexOf('const channelCopy ='));
  const steps = vm.runInNewContext(source + '; channelSteps');
  assert.equal(steps.length, 3);
  assert.ok(steps[1][1].includes('confirm_binding'));
  for (const stage of ['pairing', 'pairing_expired', 'confirm_owner', 'ready']) assert.ok(steps[2][1].includes(stage));
});
test('available online dot is preferred without overwriting an explicit choice or submitting', async () => {
  const source = readFileSync(new URL('../web/collaboration-room/connections.mjs', import.meta.url), 'utf8');
  const fn = source.slice(source.indexOf('export async function loadConnections('), source.indexOf('export function hostListPanel(')).replace('export ', '');
  const events = [];
  const select = { value: '', options: [{ value: '' }, { value: 'online' }, { value: 'paused' }], dispatchEvent: () => events.push('selection') };
  const status = {}, button = {}, existing = { value: 'existing', dispatchEvent: () => events.push('mode') };
  const panel = { name: 'add-member', roomId: 'current', connections: [] };
  const context = { panel, E: (v) => v, Event: class {}, members: () => [], connectionListPath: () => '/connections',
    supportsHostReuse: () => true, reuseAnchor: (i) => i.id, connectionChoice: (i) => i.id, connectionChoiceLabel: (i) => i.id,
    api: async () => ({ items: [{ id: 'paused', state: 'paused' }, { id: 'online', state: 'ready' }] }),
    root: { querySelector: (q) => q.includes('reuse_member_id') ? select : q.includes('status') ? status : button, querySelectorAll: () => [existing] } };
  const load = vm.runInNewContext(fn + '; loadConnections', context);
  await load();
  assert.equal(select.value, 'online');
  assert.deepEqual(events, ['mode', 'selection']);
  select.value = 'paused'; panel.modeTouched = true; events.length = 0;
  await load();
  assert.equal(select.value, 'paused');
  assert.deepEqual(events, []);
});
