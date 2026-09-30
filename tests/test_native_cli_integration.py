from tests.evidence import evidence_path
from tests.support_legacy_terminal import mount_archived_terminal
"""Native protocol real Hub/Agent and real Chromium; only disposable fixture processes."""
import base64
from contextlib import closing
import hashlib
import json
import os
import sys
from pathlib import Path
import time
import uuid

import httpx
import pytest
from playwright.sync_api import sync_playwright,expect
from shared.native_cli import database
from shared.util import atomic_json
from tests.support import running_stack,wait_for
from tests.test_native_cli import FAKE


@pytest.fixture(scope='module')
def cli_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('native-stack')) as s:
        s.stop_agent()
        bindir=s.directory/'fake-bin';bindir.mkdir();home=s.directory/'private-home';home.mkdir()
        for cli in ('codex','pi'):
            executable=bindir/cli;executable.write_text(FAKE);executable.chmod(0o700)
        s.config['shell']={'enabled':True,'projects':['*'],'inherit_env':False,'env':{'PATH':str(bindir)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.defpath,'HOME':str(home)}}
        atomic_json(s.config_path,s.config);s.start_agent()
        yield s
        # Workers deliberately outlive Agent. Explicitly stop only this fixture's owned workers.
        with closing(database(s.directory/'agent-state'/'native-cli')) as db,db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT id FROM sessions WHERE status IN ('starting','running')").fetchall():
                db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)',(uuid.uuid4().hex,row['id'],'stop','{}',time.time()))
        def stopped():
            with closing(database(s.directory/'agent-state'/'native-cli')) as db:
                return not db.execute("SELECT 1 FROM sessions WHERE status IN ('starting','running')").fetchone()
        wait_for(stopped,10)


def call(s,action,args=None,project=None):
    return s.client.post('/api/native/'+action,json={'project':project or s.project['id'],'args':args or {}})


def start(s):
    sid=uuid.uuid4().hex;r=call(s,'start',{'id':sid,'cli':'codex','cwd':'src','model':'model space;$(no-shell)','effort':'high'});s.must(r)
    wait_for(lambda:len(read(s,sid))>40)
    return sid


def read(s,sid):
    offset=0;chunks=[]
    while True:
        result=s.must(s.client.get('/api/native/sessions/'+sid+'/output',params={'offset':offset}))
        chunks.extend(base64.b64decode(c) for c in result['chunks'])
        if result['next']==offset:break
        offset=result['next']
    return b''.join(chunks)


def test_encrypted_transport_offline_agent_hub_reconnect_and_no_duplicates(cli_stack):
    s=cli_stack;sid=start(s)
    assert ('READY:'+str(s.imago/'src')).encode() in read(s,sid)
    before=read(s,sid)
    # Stop disposable fixture Agent, not production. Worker must continue with no transport.
    s.stop_agent();time.sleep(.7)
    cache=s.must(s.client.get('/api/native/sessions'))['sessions'];assert any(r['id']==sid for r in cache)
    with closing(database(s.directory/'agent-state'/'native-cli')) as db:
        worker_size=db.execute('SELECT size FROM sessions WHERE id=?',(sid,)).fetchone()[0]
    assert worker_size>len(before)
    s.start_agent();wait_for(lambda:len(read(s,sid))>worker_size)
    after=read(s,sid);assert after.startswith(before)
    # Hub process restart cannot terminate worker; durable cache and offsets survive.
    s.hub.terminate();s.hub.wait(timeout=15);time.sleep(.5);s.start_hub();s.login()
    # Persisted output can advance before shutdown; it does not prove that
    # the Agent has reconnected to the replacement Hub process.
    wait_for(lambda:any(d['id']==s.device and d['online'] for d in s.must(s.client.get('/api/devices'))['devices']),20)
    wait_for(lambda:len(read(s,sid))>len(after),20)
    all_output=read(s,sid).decode();ticks=[line for line in all_output.splitlines() if line.startswith('TICK:')]
    assert len(ticks)==len(set(ticks))
    # Independent file operations still work while native CLI is alive.
    result=s.fs('fs_read',path='README.md');assert result.get('content') or result.get('pending')
    writer=uuid.uuid4().hex;s.must(call(s,'lease',{'id':sid,'writer':writer}))
    receipt=uuid.uuid4().hex;args={'id':sid,'writer':writer,'receipt':receipt,'text':'UNIQUE-INPUT\n'}
    s.must(call(s,'input',args));s.must(call(s,'input',args));wait_for(lambda:b'ECHO:UNIQUE-INPUT' in read(s,sid))
    assert read(s,sid).count(b'ECHO:UNIQUE-INPUT')==1
    denied=call(s,'lease',{'id':sid,'writer':uuid.uuid4().hex});assert denied.status_code==409
    # No native input/output appears in the regular audit / operation journal.
    with closing(__import__('sqlite3').connect(s.hubdir/'hub.sqlite3')) as db:
        assert not db.execute("SELECT 1 FROM audit WHERE detail LIKE '%UNIQUE-INPUT%'").fetchone()
    s.must(call(s,'stop',{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex}))
    wait_for(lambda:any(x['id']==sid and x['status']=='exited' for x in s.must(s.client.get('/api/native/sessions'))['sessions']))
    s.must(call(s,'clear',{'id':sid,'confirm':sid}));assert read(s,sid)==b''


def test_admin_csrf_mapping_permissions_and_upload(cli_stack):
    s=cli_stack
    with httpx.Client(base_url=s.url,trust_env=False) as client:
        assert client.get('/api/native/sessions').status_code==401
        client.cookies.update(s.client.cookies)
        assert client.post('/api/native/discover',json={'project':s.project['id']}).status_code==403
    sid=start(s)
    wrong=call(s,'rename',{'id':sid,'title':'bad'},s.projects[1]['id']);assert wrong.status_code==403
    raw=b'\x89PNG fake native fixture';fid=uuid.uuid4().hex;sha=hashlib.sha256(raw).hexdigest()
    s.must(call(s,'upload_begin',{'file':fid,'name':'../image name.png','size':len(raw),'sha256':sha}))
    chunk={'file':fid,'offset':0,'data':base64.b64encode(raw).decode(),'sha256':sha}
    r=s.must(call(s,'upload_chunk',chunk));assert r['ready'];assert s.must(call(s,'upload_chunk',chunk))['received']==len(raw)
    assert Path(r['path']).name==fid+'.png'
    project=s.project
    s.must(s.client.put('/api/projects/'+project['id'],json={k:project[k] for k in ('alias','device_id','root','description','mode','allow_tasks')}|{'allow_tasks':False}))
    assert s.client.get('/api/native/sessions/'+sid+'/output').status_code==403
    s.must(s.client.put('/api/projects/'+project['id'],json={k:project[k] for k in ('alias','device_id','root','description','mode','allow_tasks')}))
    s.must(s.client.patch('/api/devices/'+s.device,json={'enabled':False}))
    assert s.client.get('/api/native/sessions/'+sid+'/output').status_code==403
    s.must(s.client.patch('/api/devices/'+s.device,json={'enabled':True}))
    wait_for(lambda:any(d['online'] for d in s.must(s.client.get('/api/devices'))['devices']),20)


def test_real_chromium_terminal_mobile_detach_and_escape_policy(cli_stack):
    s=cli_stack
    with sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page(viewport={'width':1400,'height':1000})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            page.goto(s.url+'/#projects');page.fill('#username', 'admin');page.fill('#password',s.password);page.click('#login-form button')
            expect(page.locator('[data-native-launch="codex"]').first).to_be_visible()
            page.locator('[data-native-launch="codex"][data-project="'+s.project['id']+'"]').click()
            mount_archived_terminal(page)
            expect(page.locator('#native-root')).to_be_visible()
            page.fill('#native-model','native-model');page.click('#native-start')
            expect(page.locator('.xterm')).to_be_visible();expect(page.locator('#native-status')).to_contain_text('running',timeout=15000)
            page.click('#native-write');expect(page.locator('#native-status')).to_contain_text('可输入',timeout=10000)
            page.fill('#native-compose','BROWSER-SENT');page.click('#native-insert');page.locator('[data-native-key="DQ=="]').click()
            wait_for(lambda:any(b'ECHO:BROWSER-SENT' in read(s,x['id']) for x in s.must(s.client.get('/api/native/sessions'))['sessions']),10)
            page.fill('#native-compose','DRAFT-MUST-SURVIVE');page.evaluate('nativePage()');expect(page.locator('#native-compose')).to_have_value('DRAFT-MUST-SURVIVE')
            # Untrusted OSC/title/hyperlinks and HTML never activate browser APIs.
            original=page.title()
            page.evaluate("NativeCLIUI.term.write('\\x1b]2;PWNED\\x07\\x1b]52;c;UE9JU09O\\x07<img src=x onerror=window.pwned=1>')")
            assert page.title()==original and page.evaluate('window.pwned') is None
            assert page.locator('#native-terminal img').count()==0
            selected=page.evaluate('NativeCLIUI.selected.id');before=len(read(s,selected))
            page.click('#native-detach');wait_for(lambda:len(read(s,selected))>before+30)
            page.set_viewport_size({'width':390,'height':844});expect(page.locator('#native-keys')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+2')
            page.screenshot(path=evidence_path(str(Path('docs/evidence/chat-ui-20260915/mobile-terminal.png'))))
            page.reload();expect(page.locator('#chat-compose')).to_be_visible();mount_archived_terminal(page);expect(page.locator('#native-list .native-session').first).to_be_visible(timeout=15000)
            assert errors==[]
        finally:browser.close()


def test_retired_terminal_hash_opens_chat_without_terminal_assets(cli_stack):
    s=cli_stack
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();assets=[]
        page.on('request',lambda request:assets.append(request.url))
        try:
            page.goto(s.url+'/#terminal');page.fill('#username', 'admin');page.fill('#password',s.password);page.click('#login-form button')
            expect(page.locator('#chat-root')).to_be_visible()
            assert page.evaluate('location.hash')=='#native'
            page.evaluate("navigate('terminal')")
            expect(page.locator('#chat-root')).to_be_visible()
            expect(page.locator('#native-root')).to_have_count(0)
            assert page.evaluate('typeof NativeCLIUI')=='undefined'
            assert not any(any(term in url for term in ['xterm','native-cli.js','addon-fit']) for url in assets)
        finally:
            browser.close()
