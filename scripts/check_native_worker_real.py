#!/usr/bin/env python3
"""Real CodePier worker -> real Pi/Codex -> localhost image protocol acceptance.
Isolated homes/config, no paid prompts, macOS external-network-deny preflight.
"""
from __future__ import annotations
import base64
from contextlib import closing
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
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from agent.native_cli import NativeCLI
from agent.filesystem import FileEngine
from agent.journal import Journal
from agent.shell import validate_shell
from shared.native_cli import database,process_exists
from scripts import check_native_cli_real as fixture


def wait(predicate,seconds=20):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=predicate()
        if result:return result
        time.sleep(.1)
    raise AssertionError('Native worker fixture condition not observed before deadline')


def output(obj,sid):
    with closing(database(obj.directory)) as db:
        return b''.join(r[0] for r in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset',(sid,))).decode(errors='replace')


def main():
    if sys.platform!='darwin':raise RuntimeError('This real native fixture requires macOS Seatbelt; no unguarded fallback')
    result={'scope':'CodePier actual standalone worker + actual interactive CLI + local mock image payload',
            'mock':True,'paid_requests':False,'production_changed':False,'cases':[]}
    out=ROOT/'docs/evidence/native-cli-20260914'
    with tempfile.TemporaryDirectory(prefix='codepier native worker ') as temp:
        base=Path(temp).resolve();profile=base/'network.sb'
        profile.write_text('(version 1)\n(allow default)\n(deny network*)\n(allow network-outbound (remote ip "localhost:*"))\n(allow network-bind (local ip "localhost:*"))\n(allow network-inbound (local ip "localhost:*"))\n')
        preflight=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),'/usr/bin/python3','-c','import socket; print(socket.socket().connect_ex(("192.0.2.1",443)))'],
            env={'PATH':'/usr/bin:/bin','HOME':str(base)},capture_output=True,text=True,timeout=10)
        if preflight.stdout.strip()!='1':raise RuntimeError('External network denial preflight failed')
        result['external_network_denied']=True
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),fixture.Mock)
        threading.Thread(target=server.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{server.server_port}/v1'
        try:
            for cli in ('pi','codex'):
                home=base/cli;home.mkdir();pi=home/'pi';pi.mkdir();codex=home/'codex';codex.mkdir()
                project=home/'project';project.mkdir();bin=home/'bin';bin.mkdir()
                executable=bin/cli
                executable.write_text('#!/usr/bin/python3\nimport os,sys\nos.execv("/usr/bin/sandbox-exec",'+repr(['sandbox-exec','-f',str(profile),fixture.BINS[cli]])+'+sys.argv[1:])\n');executable.chmod(0o700)
                pi.joinpath('models.json').write_text(json.dumps({'providers':{'fixture':{'baseUrl':url,'api':'openai-completions','apiKey':'local-dummy-not-secret','models':[{'id':'gpt-5.4','name':'Fixture Vision','input':['text','image'],'reasoning':True,'contextWindow':128000,'maxTokens':1024}]}}}))
                pi.joinpath('settings.json').write_text(json.dumps({'quietStartup':True,'enableTerminalTitle':False}))
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
[projects.{json.dumps(str(project))}]
trust_level = "trusted"
''')
                env={'PATH':str(bin)+':/opt/homebrew/bin:/usr/bin:/bin','HOME':str(home),'PI_CODING_AGENT_DIR':str(pi),'CODEX_HOME':str(codex),
                     'XDG_CONFIG_HOME':str(home/'config'),'XDG_CACHE_HOME':str(home/'cache'),'XDG_DATA_HOME':str(home/'data'),
                     'TMPDIR':str(home),'LANG':'en_US.UTF-8','PI_OFFLINE':'1','PI_TELEMETRY':'0'}
                cfg={'allowed_roots':[{'path':str(project),'writable':True,'allow_tasks':True}],
                     'shell':validate_shell({'enabled':True,'projects':['*'],'inherit_env':False,'env':env})}
                state=home/'state';journal=Journal(state);agent=SimpleNamespace(state_dir=state,config=cfg,engine=FileEngine(cfg,journal,home/'agent.json'))
                obj=NativeCLI(agent);p={'id':uuid.uuid4().hex,'device_id':uuid.uuid4().hex,'root':str(project),'alias':'fixture','mode':'write','allow_tasks':True}
                sid=uuid.uuid4().hex;writer=uuid.uuid4().hex;start=len(fixture.REQUESTS);errors=[]
                args=['--offline','--no-extensions','--no-skills','--no-prompt-templates','--no-themes','--no-context-files','--no-approve','--no-tools'] if cli=='pi' else ['--no-alt-screen','--sandbox','read-only','--ask-for-approval','never']
                child=None;native_pid=0
                try:
                    obj.action('start',p,{'id':sid,'cli':cli,'model':'gpt-5.4','provider':'fixture','argv':args})
                    child=obj.children[-1]
                    wait(lambda:('gpt-5.4' in output(obj,sid) if cli=='pi' else 'OpenAI Codex' in output(obj,sid)),35)
                    def send(text):
                        obj.action('lease',p,{'id':sid,'writer':writer})
                        return obj.action('input',p,{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex,'text':text})
                    if cli=='codex':
                        # The real CLI may open its model-migration selector.
                        # Only this isolated local-provider fixture is confirmed;
                        # no personal configuration or external model is involved.
                        def ready_or_migration():
                            screen=fixture.clean(output(obj,sid),base)
                            return 'Tip:' in screen or 'Trynewmodel' in ''.join(screen.split())
                        wait(ready_or_migration,35)
                        screen=fixture.clean(output(obj,sid),base)
                        if 'Trynewmodel' in ''.join(screen.split()) and 'Tip:' not in screen:
                            send('\r')
                            wait(lambda:'Tip:' in fixture.clean(output(obj,sid),base),20)
                    time.sleep(2)
                    obj.action('lease',p,{'id':sid,'writer':writer})
                    raw=fixture.png();fid=uuid.uuid4().hex;sha=hashlib.sha256(raw).hexdigest()
                    obj.action('upload_begin',p,{'file':fid,'name':'fixture image.png','size':len(raw),'sha256':sha})
                    obj.action('upload_chunk',p,{'file':fid,'offset':0,'data':base64.b64encode(raw).decode(),'sha256':sha})
                    bound=obj.action('upload_bind',p,{'file':fid,'id':sid,'writer':writer})
                    text=bound['image_token'] if cli=='pi' else json.dumps(bound['path'])
                    send('\x1b[200~'+text+'\x1b[201~');time.sleep(1)
                    send(' Describe fixture image.');time.sleep(.3);send('\r')
                    # Immediately detach input; worker must continue draining and
                    # answering terminal queries throughout the model operation.
                    obj.action('detach',p,{'id':sid,'writer':writer})
                    wait(lambda:len(fixture.REQUESTS)>start,25)
                    wait(lambda:'ACTUAL_NATIVE_FIXTURE_OK' in output(obj,sid),20)
                except Exception as exc:
                    errors.append(type(exc).__name__+': '+str(exc))
                finally:
                    screen=output(obj,sid)
                    (out/f'worker-real-{cli}.ansi').write_text(screen)
                    with closing(database(obj.directory)) as db:
                        receipts=[dict(r) for r in db.execute('SELECT kind,state,length(payload) AS bytes FROM commands WHERE session=? ORDER BY rowid',(sid,))]
                        saved=db.execute('SELECT child_pid FROM sessions WHERE id=?',(sid,)).fetchone();native_pid=saved[0] if saved else 0
                    if child is not None and child.poll() is None:
                        obj.action('lease',p,{'id':sid,'writer':writer})
                        obj.action('stop',p,{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex})
                        child.wait(timeout=8)
                    journal.db.close()
                calls=fixture.REQUESTS[start:]
                case={'cli':cli,'calls':calls,'errors':errors,'image_payload_observed':any(r['images'] for r in calls),
                      'first_request_image_count':len(calls[0]['images']) if calls else 0,
                      'reply_observed':'ACTUAL_NATIVE_FIXTURE_OK' in screen,
                      'real_child_reaped':not process_exists(native_pid),
                      'private_path_contains_spaces':True,'image_sha256':sha if calls else None,'receipts':receipts}
                result['cases'].append(case)
                (out/f'worker-real-{cli}-screen.txt').write_text(fixture.clean(screen,base))
                print(json.dumps(case),flush=True)
        finally:server.shutdown();server.server_close()
    (out/'worker-real-results.json').write_text(json.dumps(result,indent=2)+'\n')
    assert all(x['image_payload_observed'] and x['reply_observed'] and x['real_child_reaped'] and not x['errors'] for x in result['cases']),result


if __name__=='__main__':main()
