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
  assert.equal(recovery.nodes['#vps-check-run'].textContent, '重试');
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

    def test_unassigned_direct_check_ssh_command_and_terminal_direct_open(self):
        source = Path(__file__).resolve().parents[1] / 'web' / 'vps.js'
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');

function createUnassignedCheckFixture(checkResponses, waitResponses = []) {
  const nodes = Object.fromEntries(['run', 'state', 'output', 'project'].map(name =>
    ['#vps-check-' + name, {textContent: '', disabled: false, value: ''}]));
  const dialog = {isConnected: true};
  const checkPosts = [];
  let uid = 0, checkIdx = 0, waitIdx = 0, modalBody = '', modalFooter = '';
  const scope = {
    S: {session: 'fixture-session', space_id: 'space-1', page: 'vps'},
    document: {addEventListener() {}},
    $: selector => nodes[selector],
    $$: () => [],
    esc: value => String(value),
    uid: () => String(++uid),
    modal: (_title, body, footer) => { modalBody = body; modalFooter = footer; return dialog; },
    busy: async (button, run) => {
      button.disabled = true;
      try { await run(); } finally { button.disabled = false; }
    },
    api: async (url, options = {}) => {
      if (url === '/api/vps/unassigned-vps' && !options.method) {
        return {
          id: 'unassigned-vps', name: 'Unassigned', host: '198.51.100.20', username: 'ubuntu',
          port: 2222, enabled: true, project_ids: [], projects: [],
          terminal: {eligible: true, online: true, device_name: 'Studio-Mac'},
        };
      }
      assert.equal(url, '/api/vps/unassigned-vps/check');
      assert.equal(options.method, 'POST');
      const payload = JSON.parse(options.body);
      checkPosts.push(payload);
      const next = checkResponses[checkIdx++];
      if (next instanceof Error) throw next;
      return next;
    },
    tool: async (name) => {
      assert.equal(name, 'operations_wait');
      return waitResponses[waitIdx++];
    },
  };
  vm.createContext(scope);
  vm.runInContext(source, scope);
  return {
    scope, nodes, checkPosts, getBody: () => modalBody, getFooter: () => modalFooter,
    open: () => scope.vpsCheck('unassigned-vps'),
    click: () => nodes['#vps-check-run'].onclick(),
  };
}

(async () => {
  const direct = createUnassignedCheckFixture([
    {state: 'succeeded', pending: false, output: 'CODEPIER_VPS_OK\nhost-direct\n', error: null},
    {state: 'failed', pending: false, output: '', error: 'SSH 认证失败'},
  ]);
  await direct.open();
  assert.ok(direct.getBody().includes('<div class="field" hidden><label for="vps-check-project">'));
  assert.ok(direct.getBody().includes('等待检查。'));
  assert.ok(direct.getFooter().includes('data-vps-action="check-terminal"'));
  assert.ok(!direct.getFooter().includes('id="vps-check-run" disabled'));
  await direct.click();
  assert.equal(direct.nodes['#vps-check-state'].textContent, '连接检查成功');
  assert.equal(direct.nodes['#vps-check-output'].textContent, 'CODEPIER_VPS_OK\nhost-direct\n');
  assert.equal(direct.nodes['#vps-check-run'].textContent, '重新检查');
  assert.equal(direct.nodes['#vps-check-project'].disabled, false);
  await direct.click();
  assert.equal(direct.nodes['#vps-check-state'].textContent, 'SSH 认证失败');
  assert.equal(direct.nodes['#vps-check-output'].textContent, 'SSH 认证失败');
  assert.equal(direct.checkPosts.length, 2);
  assert.equal(direct.checkPosts[0].timeout_seconds, 30);
  assert.notEqual(direct.checkPosts[0].idempotency_key, direct.checkPosts[1].idempotency_key);

  const asyncCheck = createUnassignedCheckFixture(
    [{pending: true, operation_id: 'op-direct-9'}],
    [{state: 'succeeded', pending: false, output: 'CODEPIER_VPS_OK\nasync-host\n'}],
  );
  await asyncCheck.open();
  await asyncCheck.click();
  assert.equal(asyncCheck.nodes['#vps-check-state'].textContent, '连接检查成功');
  assert.equal(asyncCheck.nodes['#vps-check-output'].textContent, 'CODEPIER_VPS_OK\nasync-host\n');

  assert.equal(
    direct.scope.vpsSshCommand({username: 'root', host: 'vps.example.invalid', port: 22}),
    'ssh root@vps.example.invalid',
  );
  assert.equal(
    direct.scope.vpsSshCommand({username: 'ubuntu', host: '198.51.100.8', port: 2222}),
    'ssh -p 2222 ubuntu@198.51.100.8',
  );
  assert.equal(
    direct.scope.vpsSshCommand({username: 'root', host: '2001:db8::1', port: 22}),
    'ssh -6 root@2001:db8::1',
  );
  assert.equal(
    direct.scope.vpsSshCommand({username: 'root', host: '[2001:db8::1]', port: 22022}),
    'ssh -6 -p 22022 root@2001:db8::1',
  );

  assert.equal(typeof direct.scope.vpsTerminalChoose, 'undefined');
  assert.equal(
    direct.scope.vpsTerminalReason({enabled: true, project_ids: [], terminal: {eligible: true}}),
    '',
  );
  assert.equal(
    direct.scope.vpsTerminalReason({
      enabled: true, project_ids: [], terminal: {eligible: false, reason_code: 'NO_TERMINAL_AGENT'},
    }),
    '当前空间暂无可用电脑，请先添加电脑。',
  );
  assert.equal(
    direct.scope.vpsTerminalReason({
      enabled: true, project_ids: [], terminal: {eligible: false, reason_code: 'SHELL_DISABLED'},
    }),
    '连接电脑尚未开启命令执行权限。',
  );
  assert.equal(
    direct.scope.vpsTerminalReason({
      enabled: true, project_ids: [], terminal: {eligible: false, reason_code: 'SSH_TERMINAL_FORBIDDEN'},
    }),
    '连接项目需要写入和执行命令权限。',
  );

  const navigated = [], openedCalls = [];
  let apiReads = 0, presenceReads = 0;
  const termScope = {
    S: {session: {user_id: 'u-1'}, space_id: 'space-1', page: 'vps'},
    location: {hash: '#vps'},
    document: {
      addEventListener() {},
      createElement: () => ({setAttribute() {}, append() {}, addEventListener() {}}),
    },
    $: () => null,
    $$: () => [],
    esc: value => String(value),
    icon: name => `<i data-icon="${name}"></i>`,
    navigate: async dest => { navigated.push(dest); },
    api: async url => {
      if (url === '/api/vps-terminal/sessions') {
        presenceReads++;
        return {sessions: [{session_id: 'sess-77', vps_id: 'vps-1', state: 'running'}]};
      }
      apiReads++;
      assert.equal(url, '/api/vps/vps-1');
      return {
        id: 'vps-1', name: 'Tokyo-1', enabled: true,
        project_ids: [], terminal: {eligible: true, device_name: 'Mac'},
      };
    },
    window: {
      CodePierAssets: {load: async () => {}},
      CodePierVpsTerminal: {
        list: () => [{session_id: 'sess-77', vps_id: 'vps-1', state: 'running'}],
        count: () => 1,
        configure() {},
        open: async opts => { openedCalls.push(opts); },
      },
    },
  };
  vm.createContext(termScope);
  vm.runInContext(source, termScope);
  // A local session is visible only after the module belongs to this login/space.
  await termScope.vpsTerminalModule();
  await termScope.vpsOpenTerminal('vps-1');
  assert.equal(presenceReads, 0, 'existing local session must not query Hub presence');
  assert.equal(apiReads, 0, 'existing local session must not fetch VPS metadata');
  assert.deepEqual(navigated, ['vps?view=terminal&session=sess-77']);
  assert.equal(openedCalls.length, 0, 'returning to a local session must not create a new one');

  // A cold browser does not own the local record and instead finds its session at the Hub.
  termScope.vpsTerminalForget();
  await termScope.vpsOpenTerminal('vps-1');
  assert.equal(presenceReads, 1);
  assert.equal(apiReads, 0, 'discovered existing session must not fetch VPS metadata');
  assert.deepEqual(navigated, [
    'vps?view=terminal&session=sess-77', 'vps?view=terminal&session=sess-77',
  ]);
  assert.equal(openedCalls.length, 0, 'returning to a discovered session must not create a new one');
  await termScope.vpsOpenTerminal('vps-1', null, {newSession: true});
  assert.equal(presenceReads, 1);
  assert.equal(apiReads, 1);
  assert.equal(openedCalls.length, 1);
  assert.equal(openedCalls[0].newSession, true);
  assert.equal('project' in openedCalls[0], false);
  assert.equal('projectId' in openedCalls[0], false);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
