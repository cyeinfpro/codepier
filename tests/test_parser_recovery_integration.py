"""Real Hub/Agent receipts stay terminal after parser failure and Agent restart."""
import subprocess
import sys
import uuid

from tests.support import BASE, wait_for


def test_parser_failures_leave_agent_online_and_are_not_replayed(stack):
    s = stack
    filename = 'parser-fault-' + uuid.uuid4().hex + '.ts'
    (s.imago/filename).write_text('export function healthy(){return 1;}\n')
    s.stop_agent()
    # Only this fixture Agent injects failures into its first two parser child
    # launches. Production code has no fault-injection environment switch.
    bootstrap = '''
import sys
from agent import symbol_worker
original = symbol_worker._worker_command
faults = ['import os; os._exit(17)', 'import time; time.sleep(60)']
def command():
    if faults:
        return [sys.executable, '-I', '-c', faults.pop(0)]
    return original()
symbol_worker._worker_command = command
symbol_worker.TIMEOUT_SECONDS = 1
sys.argv = ['agent', '--config', CONFIG_PATH, 'run']
from agent.__main__ import main
main()
'''.replace('CONFIG_PATH', repr(str(s.config_path)))
    s.agent = subprocess.Popen([sys.executable, '-c', bootstrap], cwd=BASE, env=s.env,
                               stdout=s.agent_log, stderr=subprocess.STDOUT)
    pid = s.agent.pid
    def device():
        return next(d for d in s.client.get('/api/devices').json()['devices'] if d['id'] == s.device)
    wait_for(lambda: device()['online'])
    receipts = []
    try:
        for code in ('PARSER_CRASHED', 'PARSER_TIMEOUT'):
            args = {'project': 'Imago', 'path': filename, 'idempotency_key': uuid.uuid4().hex}
            response = s.call('code_symbols', args, raw=True)
            assert response.status_code == 409, response.text
            error = response.json()['error']
            assert error['code'] == code
            identifier = error['operation_id']
            operation = s.poll(identifier)
            assert operation['state'] == 'failed' and not operation['pending']
            assert operation['result']['error']['code'] == code
            duplicate = s.call('code_symbols', args, raw=True).json()['error']
            assert duplicate['operation_id'] == identifier and duplicate['code'] == code
            assert device()['online'] and s.agent.poll() is None and s.agent.pid == pid
            receipts.append((args, identifier, code))
        assert s.fs('code_symbols', path=filename)['symbols'][0]['name'] == 'healthy'
        assert '# Imago' in s.fs('fs_read', path='README.md')['content']
    finally:
        s.stop_agent()
        s.start_agent()
    for args, identifier, code in receipts:
        duplicate = s.call('code_symbols', args, raw=True).json()['error']
        assert duplicate['operation_id'] == identifier and duplicate['code'] == code
    assert s.fs('code_symbols', path=filename)['symbols'][0]['name'] == 'healthy'
