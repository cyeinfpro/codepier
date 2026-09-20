#!/usr/bin/env python3
"""Actual installed native chat worker smoke; localhost mock, isolated homes.

Run on macOS with: .venv/bin/python scripts/check_native_chat_complete_real.py
Fails closed if Seatbelt external-network denial cannot be established. No model
credentials are inherited and no personal config, install, or production changes
are made. Evidence is written to a new timestamped directory per invocation.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.native_cli import NativeCLI
from agent.filesystem import FileEngine
from agent.journal import Journal
from agent.shell import validate_shell
from shared.native_cli import database, process_exists
from scripts import check_native_cli_real as fixture


def wait(predicate, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Native chat condition not observed before deadline')


def snapshot(manager, sid):
    with closing(database(manager.directory)) as db:
        row = dict(db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())
        raw=b''.join(bytes(r[0]) for r in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset',(sid,)))
        events=[json.loads(line) for line in raw.splitlines()]
        receipts = [dict(r) for r in db.execute(
            'SELECT id,kind,state FROM commands WHERE session=? ORDER BY rowid', (sid,))]
    return row, events, receipts


def run_case(base, profile, url, cli):
    home = base / cli
    home.mkdir()
    pi, codex, project = [home / name for name in ('pi', 'codex', 'project')]
    for directory in (pi, codex, project):
        directory.mkdir()
    pi.joinpath('models.json').write_text(json.dumps({'providers': {'fixture': {
        'baseUrl': url, 'api': 'openai-completions', 'apiKey': 'local-dummy-not-secret',
        'models': [{'id': 'gpt-5.4', 'name': 'Fixture Vision', 'input': ['text', 'image'],
                    'reasoning': True, 'contextWindow': 128000, 'maxTokens': 1024}, {'id':'gpt-5.6','name':'Selected Fixture Vision','input':['text','image'],'reasoning':True,'contextWindow':128000,'maxTokens':1024}]}}}))
    pi.joinpath('settings.json').write_text(json.dumps({
        'defaultProvider': 'fixture', 'defaultModel': 'gpt-5.4', 'quietStartup': True}))
    codex.joinpath('config.toml').write_text(f'''model = "gpt-5.4"
model_provider = "fixture"
model_reasoning_effort = "medium"
check_for_update_on_startup = false
web_search = "disabled"
[analytics]
enabled = false
[model_providers.fixture]
name = "Local Fixture"
base_url = "{url}"
wire_api = "responses"
requires_openai_auth = false
''')
    env = {'PATH': str(Path(fixture.BINS[cli]).parent) + ':/opt/homebrew/bin:/usr/bin:/bin', 'HOME': str(home),
           'PI_CODING_AGENT_DIR': str(pi), 'CODEX_HOME': str(codex),
           'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_CACHE_HOME': str(home / 'cache'),
           'XDG_DATA_HOME': str(home / 'data'), 'TMPDIR': str(home),
           'LANG': 'en_US.UTF-8', 'PI_OFFLINE': '1', 'PI_TELEMETRY': '0'}
    cfg = {'allowed_roots': [{'path': str(project), 'writable': True, 'allow_tasks': True}],
           'shell': validate_shell({'enabled': True, 'projects': ['*'], 'inherit_env': False, 'env': env})}
    state = home / 'state'
    journal = Journal(state)
    agent = SimpleNamespace(state_dir=state, config=cfg,
                            engine=FileEngine(cfg, journal, home / 'agent.json'))
    manager = NativeCLI(agent)
    project_row = {'id': uuid.uuid4().hex, 'device_id': uuid.uuid4().hex,
                   'root': str(project), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True}
    children, sessions, events_saved, errors = [], [], [], []
    start = len(fixture.REQUESTS)
    versions = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), fixture.BINS[cli], '--version'], env=env, capture_output=True,
                              text=True, timeout=15)
    case = {'cli': cli, 'version': versions.stdout.strip(), 'errors': errors}
    native_popen = subprocess.Popen
    def guarded_popen(argv, **kwargs):
        # The entire owned worker inherits loopback-only network restrictions.
        return native_popen(['/usr/bin/sandbox-exec', '-f', str(profile), *argv], **kwargs)
    def launch(previous=None):
        sid = uuid.uuid4().hex
        with patch('agent.native_cli.subprocess.Popen', guarded_popen):
            manager.action('start', project_row, {'id': sid, 'cli': cli, 'mode': 'chat',
                           **({'continue_session': previous} if previous else {'model':'fixture/gpt-5.6' if cli=='pi' else 'gpt-5.6','effort':'low'})})
        children.append(manager.children[-1])
        sessions.append(sid)
        return sid
    def completed(sid, receipt):
        row, events, receipts = snapshot(manager, sid)
        if row['status'] not in ('starting', 'running'):
            raise AssertionError('Worker stopped: ' + row['error'])
        return any(r['id'] == receipt and r['state'] == 'completed' for r in receipts)
    def stop(sid, child):
        if child.poll() is None:
            manager.action('stop', project_row, {'id': sid, 'receipt': uuid.uuid4().hex})
            child.wait(timeout=10)
    try:
        assert versions.returncode == 0, versions.stderr
        with patch('agent.native_cli.subprocess.Popen', guarded_popen):
            catalog=manager.action('chat_catalog',project_row,{'cli':cli,'cwd':'.','model':'fixture/gpt-5.6' if cli=='pi' else 'gpt-5.6'})
        assert catalog['models'] and len(fixture.REQUESTS)==start, 'Catalog must not send model requests'
        assert 'local-dummy-not-secret' not in json.dumps(catalog)
        case['catalog_no_inference']=True
        sid = launch()
        wait(lambda:any(e.get('type')=='settings' for e in snapshot(manager,sid)[1]))
        refresh=uuid.uuid4().hex
        manager.action('chat_command',project_row,{'id':sid,'receipt':refresh,'name':'refresh'})
        wait(lambda:completed(sid,refresh))
        assert len(fixture.REQUESTS)==start, 'Refresh must not infer'
        case['refresh_no_inference']=True
        raw = fixture.png()
        fid, sha = uuid.uuid4().hex, hashlib.sha256(raw).hexdigest()
        manager.action('upload_begin', project_row, {'file': fid, 'name': 'fixture image.png',
                       'size': len(raw), 'sha256': sha})
        manager.action('upload_chunk', project_row, {'file': fid, 'offset': 0,
                       'data': base64.b64encode(raw).decode(), 'sha256': sha})
        manager.action('upload_bind', project_row, {'file': fid, 'id': sid})
        first, second = uuid.uuid4().hex, uuid.uuid4().hex
        prompt = {'id': sid, 'receipt': first, 'text': 'Describe fixture image.', 'attachments': [fid]}
        manager.action('chat_prompt', project_row, prompt)
        manager.action('chat_prompt', project_row, prompt)
        manager.action('chat_prompt', project_row, {'id': sid, 'receipt': second, 'text': 'Follow up.'})
        # Simulate Agent adapter replacement: worker has its own process, native
        # pipes and durable queue; a new adapter sees the same existing state.
        manager = NativeCLI(agent)
        wait(lambda: completed(sid, first) and completed(sid, second))
        row, events, receipts = snapshot(manager, sid)
        first_native_thread = row['native_thread']
        case['two_receipts_completed'] = True
        assert fixture.REQUESTS[start]['model']=='gpt-5.6', 'Initial selected model must differ from configured gpt-5.4'
        case['preselected_model_verified']=True
        reasoning=fixture.REQUESTS[start]['reasoning']
        assert (reasoning.get('effort') if isinstance(reasoning,dict) else reasoning)=='low', reasoning
        case['native_thinking_change_verified']=True
        case['adapter_replacement_survived'] = True
        case['first_session_model_requests'] = len(fixture.REQUESTS) - start
        assert case['first_session_model_requests'] == 2, 'Duplicate receipt caused extra request'
        assert any(e.get('type') == 'delta' and 'ACTUAL_NATIVE_FIXTURE_OK' in e.get('text', '') for e in events)
        assert fixture.REQUESTS[start]['images'][0]['sha256'] == sha, 'Native image bytes not delivered'
        selection=uuid.uuid4().hex
        manager.action('chat_settings',project_row,{'id':sid,'receipt':selection,'model':'fixture/gpt-5.4' if cli=='pi' else 'gpt-5.4','effort':'high'})
        wait(lambda:completed(sid,selection))
        switched=uuid.uuid4().hex
        manager.action('chat_prompt',project_row,{'id':sid,'receipt':switched,'text':'Verify a different model and thinking setting.'})
        wait(lambda:completed(sid,switched))
        changed=fixture.REQUESTS[-1]
        assert changed['model']=='gpt-5.4', changed
        strength=changed['reasoning']
        assert (strength.get('effort') if isinstance(strength,dict) else strength)=='high', changed
        case['live_model_and_effort_switch_verified']=True
        row,events,receipts=snapshot(manager,sid)
        stop(sid, children[-1])
        events_saved.append({'events': events, 'receipts': receipts})
        resumed = launch(sid)
        third = uuid.uuid4().hex
        manager.action('chat_prompt', project_row, {'id': resumed, 'receipt': third, 'text': 'Continue the same history.'})
        wait(lambda: completed(resumed, third))
        row, events, receipts = snapshot(manager, resumed)
        case['same_native_history'] = (row['native_thread'] == first_native_thread if cli == 'codex'
            else row['native_thread']==first_native_thread)
        assert case['same_native_history'], 'Continuation created a different native history'
        reasoning=fixture.REQUESTS[-1]['reasoning']
        assert (reasoning.get('effort') if isinstance(reasoning,dict) else reasoning)=='high'
        case['resumed_settings_verified']=True
        case['image_sha256'] = sha
        case['reply_observed'] = any(e.get('type') == 'delta' and 'ACTUAL_NATIVE_FIXTURE_OK' in e.get('text', '') for e in events)
        events_saved.append({'events': events, 'receipts': receipts})
    except Exception as exc:
        errors.append(type(exc).__name__ + ': ' + str(exc))
    finally:
        reaped = []
        for sid, child in zip(sessions, children):
            try:
                stop(sid, child)
            except Exception as exc:
                errors.append('Cleanup: ' + type(exc).__name__ + ': ' + str(exc))
                # Only this invocation's owned worker is targeted, never a
                # process discovered by name or PID from another session.
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=8)
            row, events, receipts = snapshot(manager, sid)
            reaped.append(not process_exists(row['child_pid']))
            if errors:
                events_saved.append({'events': events, 'receipts': receipts, 'error': row['error']})
        case['real_children_reaped'] = bool(reaped) and all(reaped)
        case['calls'] = fixture.REQUESTS[start:]
        journal.db.close()
    return case, events_saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', choices=('pi', 'codex', 'both'), default='both')
    args = parser.parse_args()
    evidence = ROOT / 'docs/evidence/chat-complete-20260915' / (
        'native-chat-real-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    evidence.mkdir(parents=True)
    result = {'scope': 'actual standalone chat worker and installed native CLI against localhost mock',
              'paid_requests': False, 'production_changed': False, 'cases': [],
              'command': '.venv/bin/python scripts/check_native_chat_complete_real.py --cli ' + args.cli}
    exit_code = 1
    try:
        if sys.platform != 'darwin':
            raise RuntimeError('Requires macOS Seatbelt; no unguarded fallback')
        with tempfile.TemporaryDirectory(prefix='codepier native chat ') as temp:
            base = Path(temp).resolve()
            profile = base / 'network.sb'
            profile.write_text('(version 1)\n(allow default)\n(deny network*)\n'
                '(allow network-outbound (remote ip "localhost:*"))\n'
                '(allow network-bind (local ip "localhost:*"))\n'
                '(allow network-inbound (local ip "localhost:*"))\n')
            preflight = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), '/usr/bin/python3', '-c',
                'import socket; s=socket.socket(); s.settimeout(1); print(s.connect_ex(("192.0.2.1",443)))'],
                env={'PATH': '/usr/bin:/bin', 'HOME': str(base)}, capture_output=True, text=True, timeout=10)
            if preflight.stdout.strip() != '1':
                raise RuntimeError('External network denial preflight failed: ' + preflight.stdout + preflight.stderr)
            result['external_network_denied'] = True
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), fixture.Mock)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                for cli in (('pi', 'codex') if args.cli == 'both' else (args.cli,)):
                    case, events = run_case(base, profile, f'http://127.0.0.1:{server.server_port}/v1', cli)
                    result['cases'].append(case)
                    (evidence / (cli + '-events.json')).write_text(json.dumps(events, indent=2) + '\n')
                    print(json.dumps(case), flush=True)
            finally:
                server.shutdown()
                server.server_close()
            assert all(not c['errors'] and c['reply_observed'] and c['same_native_history']
                       and c['real_children_reaped'] for c in result['cases']), result
            exit_code = 0
    except Exception as exc:
        result['failure'] = type(exc).__name__ + ': ' + str(exc)
        print(result['failure'], file=sys.stderr)
    finally:
        result['exit_code'] = exit_code
        (evidence / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
        print('Evidence: ' + str(evidence), flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
