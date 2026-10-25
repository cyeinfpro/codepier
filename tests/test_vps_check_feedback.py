"""Deterministic VPS dialog lifecycle checks; no browser, Hub or SSH server."""
from pathlib import Path
import subprocess
import unittest


class VPSCheckFeedbackTests(unittest.TestCase):
    def test_pending_terminal_and_recovery_feedback(self):
        source = Path(__file__).resolve().parents[1] / 'web' / 'vps.js'
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function fixture(receipts) {
  const nodes = Object.fromEntries(['run', 'state', 'output', 'project'].map(name =>
    ['#vps-check-' + name, {textContent: '', disabled: false, value: 'fixture-project'}]));
  const dialog = {isConnected: true};
  const submissions = [], outputs = [];
  let uid = 0, sequence = 0;
  const scope = {
    S: {session: 'fixture-session', page: 'vps'},
    document: {addEventListener() {}},
    $: selector => nodes[selector],
    esc: value => String(value), notice: value => value,
    modal: () => dialog, uid: () => String(++uid),
    stateNames: {running: '运行中', failed: '失败', succeeded: '成功', cancelled: '已取消'},
    busy: async (button, run) => {button.disabled = true; try {await run();} finally {button.disabled = false;}},
    api: async () => ({name: 'Fixture', host: 'fixture.invalid', username: 'fixture', port: 22,
      enabled: true, projects: [{id: 'fixture-project', alias: 'Fixture', device_name: 'Fixture',
      mode: 'write', allow_tasks: true, online: true}]}),
    tool: async (name, args) => {
      if (name === 'exec') {submissions.push({...args}); return {operation_id: 'op-' + submissions.length};}
      assert.equal(name, 'operations_wait');
      outputs.push(nodes['#vps-check-output'].textContent);
      const receipt = receipts[sequence++];
      assert.ok(receipt, 'unexpected polling or resubmission');
      if (receipt instanceof Error) throw receipt;
      if (typeof receipt === 'function') return receipt(dialog, scope);
      return receipt;
    },
  };
  vm.createContext(scope); vm.runInContext(source, scope);
  return {scope, nodes, dialog, submissions, outputs,
    open: () => scope.vpsCheck('fixture-vps'), click: () => nodes['#vps-check-run'].onclick()};
}
const terminal = (state, extra = {}) => ({state, pending: false, output: '', ...extra});
(async () => {
  const missing = '密码 SSH 缺少依赖；尚未连接服务器';
  for (const receipt of [terminal('failed', {result: {error: {code: 'SSH_DEPENDENCY_MISSING', message: missing}}}),
    terminal('cancelled'), terminal('succeeded'), terminal('failed', {error: 'fixture failure'})]) {
    const f = fixture([receipt]); await f.open(); await f.click();
    const text = f.nodes['#vps-check-output'].textContent;
    assert.ok(text && !text.includes('等待'), 'terminal empty output must not claim it is waiting');
    if (receipt.result?.error) assert.equal(text, missing);
    if (receipt.error) assert.equal(text, receipt.error);
    assert.equal(f.nodes['#vps-check-run'].textContent, '重新检查');
    assert.equal(f.nodes['#vps-check-project'].disabled, false);
    assert.equal(f.submissions.length, 1);
  }
  for (const extra of [{output: 'stream result'}, {result: {data: {output: 'stored result'}}}]) {
    const f = fixture([terminal('failed', extra)]); await f.open(); await f.click();
    assert.equal(f.nodes['#vps-check-output'].textContent, extra.output || extra.result.data.output);
  }
  const pending = fixture([{state: 'running', pending: true, output: ''}, terminal('failed')]);
  await pending.open(); await pending.click();
  assert.equal(pending.outputs[1], '等待 Agent 返回…');
  assert.ok(!pending.nodes['#vps-check-output'].textContent.includes('等待'));
  assert.equal(pending.submissions.length, 1);
  const again = fixture([terminal('failed'), terminal('succeeded')]);
  await again.open(); await again.click(); await again.click();
  assert.equal(again.submissions.length, 2);
  assert.notEqual(again.submissions[0].idempotency_key, again.submissions[1].idempotency_key);
  const recovery = fixture([new Error('temporary read failure'), terminal('succeeded')]);
  await recovery.open(); await recovery.click();
  assert.equal(recovery.nodes['#vps-check-run'].textContent, '恢复本次检查');
  assert.equal(recovery.nodes['#vps-check-project'].disabled, true);
  await recovery.click();
  assert.equal(recovery.submissions.length, 1, 'recovery must poll the original operation');
  const closed = fixture([(dialog) => {dialog.isConnected = false; return terminal('failed');}]);
  await closed.open(); await closed.click();
  assert.equal(closed.submissions.length, 1);
  assert.ok(!closed.nodes['#vps-check-state'].textContent.includes('检查未完成'));
  console.log('VPS feedback: 10 synthetic lifecycle scenarios passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
