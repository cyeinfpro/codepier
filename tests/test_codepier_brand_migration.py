"""Real filesystem/SQLite migrations with isolated, fault-injected OS services."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace
import sqlite3
import sys
import pytest
from shared import brand_migration as migration
from scripts import install_agent as installer
ROOT=Path(__file__).resolve().parents[1]


def make_install(tmp_path,monkeypatch,kind='systemd',directory='.remote-dev-agent'):
    home=tmp_path/'home';home.mkdir();monkeypatch.setattr(Path,'home',classmethod(lambda cls:home))
    base=home/directory;runtime=base/'runtime';(runtime/'agent').mkdir(parents=True)
    (runtime/'agent/__main__.py').write_text('# fixture\n');(runtime/'.venv/bin').mkdir(parents=True)
    (runtime/'.venv/bin/python').symlink_to(Path(sys.executable).resolve())
    (runtime/'.venv/pyvenv.cfg').write_text('home = '+str(base/'python/bin')+'\n')
    (runtime/'.venv/bin/example').write_text('#!'+str(runtime/'.venv/bin/python')+'\nprint(1)\n');(runtime/'.venv/bin/example').chmod(0o700)
    (runtime/'shared').mkdir();(runtime/'shared/util.py').write_text('VERSION="1.9.0"\n')
    project=tmp_path/'real-project';project.mkdir();(project/'untouched.txt').write_text('owner project')
    config={'device_id':'d'*32,'secret':'s'*64,'hub_url':'https://hub.example','state_dir':str(base/'state'),'name':'Owner Node',
            'allowed_roots':[{'path':str(project),'writable':True,'allow_tasks':True}],'tasks':{},
            'custom':{'name':'RELAY was my note','path':str(tmp_path/'custom')}}
    (base/'config.json').write_text(json.dumps(config,ensure_ascii=False,indent=3))
    metadata={'managed':True,'service':True,'layout':'managed-runtime','service_kind':kind,'service_scope':'user',
              'helper_python':str(base/'python/bin/python3'),'uv':str(base/'tools/uv'),'status':'ready'}
    (base/'management.json').write_text(json.dumps(metadata));(base/'state/native-cli').mkdir(parents=True)
    for file in ['agent.sqlite3','native-cli/native.sqlite3']:
        table='calls' if file=='agent.sqlite3' else 'sessions';db=sqlite3.connect(base/'state'/file)
        try:
            db.execute('CREATE TABLE '+table+'(id TEXT PRIMARY KEY,status TEXT)')
            db.execute('INSERT INTO '+table+' VALUES (?,?)',('original-history','succeeded' if table=='calls' else 'exited'));db.commit()
        finally:db.close()
    name=migration.SERVICE_NAMES[kind][1];target=migration.service_target(base,kind,'user',name);target.parent.mkdir(parents=True,exist_ok=True)
    command=[str(runtime/'.venv/bin/python'),'-m','agent','--config',str(base/'config.json'),'run']
    if kind=='launchd':raw=plistlib.dumps({'Label':name,'ProgramArguments':command,'WorkingDirectory':str(runtime),'StandardOutPath':str(base/'logs/stdout.log'),'KeepAlive':True})
    else:raw=('[Unit]\nDescription=Remote Dev Agent\n[Service]\nWorkingDirectory='+str(runtime)+'\nExecStart='+' '.join(migration.systemd_quote(x) for x in command)+'\n[Install]\nWantedBy=default.target\n').encode()
    target.write_bytes(raw);target.chmod(0o600);calls=[]
    backend=SimpleNamespace(_service=lambda b:(kind,'user'),_service_name=lambda b:migration.service_name(b,kind,'user'),
        _service_target=lambda b,k,s:migration.service_target(b,k,s,migration.service_name(b,k,s)),
        stop_service=lambda b,**kw:calls.append(('stop',str(b))),start_service=lambda b:calls.append(('start',str(b))),
        verify_service=lambda b:calls.append(('verify',str(b))),refresh_cli_commands=lambda b:None,_wait_for_exit=lambda pid:None,
        _move=lambda a,b:a.rename(b),_process_alive=lambda pid:False,_systemctl=lambda scope:['systemctl','--user'],_run=lambda cmd,**kw:0)
    return SimpleNamespace(home=home,base=base,config=config,target=target,raw=raw,project=project,backend=backend,calls=calls)


@pytest.mark.parametrize('kind',['launchd','systemd'])
def test_directory_database_and_service_migration_preserves_identity(tmp_path,monkeypatch,kind):
    f=make_install(tmp_path,monkeypatch,kind);result=migration.migrate_agent(f.base,0,f.backend);new=f.home/'.codepier-agent'
    assert result['status']=='completed' and new.is_dir() and f.base.is_symlink() and f.base.resolve()==new
    assert json.loads((new/'config.json').read_text())=={**f.config,'state_dir':str(new/'state')}
    assert (f.project/'untouched.txt').read_text()=='owner project' and not f.target.exists()
    target=migration.service_target(new,kind,'user',migration.SERVICE_NAMES[kind][0])
    assert migration.definition_owned(target.read_bytes(),new,kind,migration.SERVICE_NAMES[kind][0])
    assert str(new/'runtime/.venv/bin/python') in (new/'runtime/.venv/bin/example').read_text()
    assert (new/'runtime/.venv/bin/example').stat().st_mode&0o777==0o700
    db=sqlite3.connect(new/'state/agent.sqlite3')
    try:
        assert db.execute('SELECT id FROM calls').fetchall()==[('original-history',)]
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:db.close()
    journal=json.loads((new/migration.JOURNAL).read_text());assert journal['stage']=='completed' and len(journal['databases'])==2
    assert Path(result['backup']).joinpath('service.before').read_bytes()==f.raw
    before=len(f.calls);assert migration.migrate_agent(new,0,f.backend)['status']=='already_current';assert len(f.calls)==before


def test_custom_install_directory_is_not_arbitrarily_renamed(tmp_path,monkeypatch):
    f=make_install(tmp_path,monkeypatch,directory='owner-selected');result=migration.migrate_agent(f.base,0,f.backend)
    assert result['install_dir']==str(f.base) and result['legacy_alias'] is None
    assert f.base.is_dir() and not f.base.is_symlink() and json.loads((f.base/'config.json').read_text())==f.config


@pytest.mark.parametrize('conflict',['directory','new-service','wrong-command','symlink'])
def test_conflicts_refuse_before_stopping_services(tmp_path,monkeypatch,conflict):
    f=make_install(tmp_path,monkeypatch);new=f.home/'.codepier-agent'
    if conflict=='directory':new.mkdir()
    elif conflict=='symlink':new.symlink_to(f.project)
    elif conflict=='new-service':migration.service_target(new,'systemd','user','codepier-agent.service').write_text('other installation')
    else:f.target.write_text(f.target.read_text().replace('"-m" "agent"','"-m" "other"'))
    before=(f.base/'config.json').read_bytes()
    with pytest.raises(RuntimeError):migration.migrate_agent(f.base,0,f.backend)
    assert not f.calls and (f.base/'config.json').read_bytes()==before


@pytest.mark.parametrize('failure',['start','verify','move'])
def test_failure_restores_original_paths_service_and_configuration(tmp_path,monkeypatch,failure):
    f=make_install(tmp_path,monkeypatch);before=(f.base/'config.json').read_bytes();once=[True]
    attribute={'start':'start_service','verify':'verify_service','move':'_move'}[failure];original=getattr(f.backend,attribute)
    def fail(*args,**kwargs):
        if once:
            once.pop();raise RuntimeError('injected '+failure)
        return original(*args,**kwargs)
    setattr(f.backend,attribute,fail)
    with pytest.raises(RuntimeError,match='injected'):migration.migrate_agent(f.base,0,f.backend)
    assert not f.base.is_symlink() and not (f.home/'.codepier-agent').exists()
    assert (f.base/'config.json').read_bytes()==before and f.target.read_bytes()==f.raw
    assert json.loads((f.base/migration.JOURNAL).read_text())['stage']=='rolled_back'
    assert (f.project/'untouched.txt').read_text()=='owner project'
    assert any(c[0]=='start' and c[1]==str(f.base) for c in f.calls)


@pytest.mark.parametrize('table,status',[('calls','accepted'),('calls','running'),('sessions','running'),('sessions','orphaned')])
def test_busy_agent_is_never_stopped_by_rename(tmp_path,monkeypatch,table,status):
    f=make_install(tmp_path,monkeypatch);database=f.base/'state'/('agent.sqlite3' if table=='calls' else 'native-cli/native.sqlite3')
    db=sqlite3.connect(database)
    try:db.execute('UPDATE '+table+' SET status=?',(status,));db.commit()
    finally:db.close()
    with pytest.raises(RuntimeError,match='active|session|idle'):migration.migrate_agent(f.base,0,f.backend)
    assert not f.calls and not f.base.is_symlink()


def test_interrupted_move_can_be_recovered_from_canonical_directory(tmp_path,monkeypatch):
    f=make_install(tmp_path,monkeypatch);before=(f.base/'config.json').read_bytes()
    def interruption(old,new):old.rename(new);raise KeyboardInterrupt('simulated power loss')
    f.backend._move=interruption
    with pytest.raises(KeyboardInterrupt):migration.migrate_agent(f.base,0,f.backend)
    new=f.home/'.codepier-agent';assert new.exists() and not f.base.exists()
    result=migration.recover_agent(new,f.backend)
    assert result==f.base and f.base.exists() and not new.exists()
    assert (f.base/'config.json').read_bytes()==before and json.loads((f.base/migration.JOURNAL).read_text())['stage']=='rolled_back'


def test_default_directory_recognizes_only_owned_legacy_alias(tmp_path,monkeypatch):
    f=make_install(tmp_path,monkeypatch);assert migration.default_base()==f.base and installer.default_install_base()==f.base
    migration.migrate_agent(f.base,0,f.backend)
    assert migration.default_base()==f.home/'.codepier-agent' and installer.validate_install_base(f.base)==f.home/'.codepier-agent'
    f.base.unlink();f.base.mkdir()
    with pytest.raises((RuntimeError,ValueError)):installer.default_install_base()


def test_legacy_mcp_resources_and_binding_remain_readable():
    from hub import mcp_apps
    for kind in ('workspace','changes'):
        old=mcp_apps.read_resource('ui://relay/'+kind+'-v1.html',lambda:'https://hub.example')
        new=mcp_apps.read_resource('ui://codepier/'+kind+'-v1.html',lambda:'https://hub.example')
        assert old['text']==new['text'] and 'CodePier' in new['text']
    result=mcp_apps.attach({},'open_workspace',{'project':'fixture'},{},lambda:'https://hub.example')
    assert result['_meta']['com.codepier/binding']==result['_meta']['me.infpro.relay/binding']
    assert all(x['uri'].startswith('ui://codepier/') for x in mcp_apps.list_resources())


def test_exact_old_updater_accepts_new_agent_package(tmp_path):
    from hub.agent_install import AgentPackage
    source=ROOT/'tests/fixtures/legacy-agent-lifecycle-v180.py';spec=importlib.util.spec_from_file_location('legacy_codepier_compat',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    package=AgentPackage(ROOT).build();archive=tmp_path/'agent.zip';archive.write_bytes(package.content)
    module.LifecycleManager._extract(archive,package.sha256,tmp_path/'candidate')
    assert (tmp_path/'candidate/shared/brand_migration.py').is_file() and (tmp_path/'candidate/agent/brand_upgrade.py').is_file()
    assert (tmp_path/'candidate/scripts/agent_lifecycle.py').is_file()


def test_new_agent_recognizes_legacy_service_before_auto_handoff(tmp_path,monkeypatch):
    from agent.lifecycle import LifecycleManager
    from agent.brand_upgrade import needed
    f=make_install(tmp_path,monkeypatch);manager=LifecycleManager(f.base/'config.json',f.base/'state',lambda:f.config)
    manager.base=f.base;manager.runtime_root=f.base/'runtime';monkeypatch.setattr(manager,'_service_kind',lambda:('systemd','user'))
    assert manager._service_target()==f.target and needed(manager) is True
    migration.migrate_agent(f.base,0,f.backend);manager.base=f.home/'.codepier-agent';manager.runtime_root=manager.base/'runtime'
    assert needed(manager) is False


@pytest.mark.parametrize('external',[False,True])
@pytest.mark.parametrize('fail',[False,True])
def test_receipt_owned_browser_registration_migrates_and_rolls_back(tmp_path,monkeypatch,external,fail):
    from shared import brand_browser
    import hashlib
    f=make_install(tmp_path,monkeypatch)
    state=tmp_path/'owner-external-state' if external else f.base/'state'
    state.mkdir(exist_ok=True);config={**f.config,'state_dir':str(state)}
    (f.base/'config.json').write_text(json.dumps(config))
    directory=state/'browser-bridge';directory.mkdir();launcher=directory/'host.sh'
    launcher.write_text('#!/bin/sh\nexec '+str(f.base/'runtime/.venv/bin/python')+' '+str(f.base/'runtime/agent/relay_browser_host.py')+'\n')
    manifest=f.home/'NativeMessagingHosts/me.infpro.relay.browser.json';manifest.parent.mkdir()
    origins=['chrome-extension://'+'a'*32+'/']
    manifest.write_text(json.dumps({'name':'me.infpro.relay.browser','path':str(launcher),'type':'stdio','allowed_origins':origins}))
    receipt=directory/'install-receipt.json';receipt.write_text(json.dumps({'config':str(f.base/'config.json'),
        'manifest':str(manifest),'browser':'chrome','browser_after':{'enabled':True,'origins':['https://example.test']},
        'files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [launcher,manifest]}}))
    before={str(p):p.read_bytes() for p in (launcher,manifest,receipt)};f.backend.brand_browser=brand_browser
    if fail:
        once=[True]
        def verify(base):
            if once:once.pop();raise RuntimeError('injected browser cutover failure')
        f.backend.verify_service=verify
        with pytest.raises(RuntimeError,match='injected'):migration.migrate_agent(f.base,0,f.backend)
        assert all(Path(p).read_bytes()==raw for p,raw in before.items())
        assert not manifest.with_name('com.codepier.browser.json').exists()
    else:
        migration.migrate_agent(f.base,0,f.backend)
        new=f.home/'.codepier-agent';new_state=state if external else new/'state'
        new_receipt=json.loads((new_state/'browser-bridge/install-receipt.json').read_text())
        data=json.loads(Path(new_receipt['manifest']).read_text())
        assert data['name']=='com.codepier.browser' and data['allowed_origins']==origins
        assert new_receipt['config']==str(new/'config.json')
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in new_receipt['files'].items())
        assert str(new/'runtime/agent/codepier_browser_host.py') in (new_state/'browser-bridge/host.sh').read_text()
        assert manifest.read_bytes()==before[str(manifest)]


def test_windows_task_definition_preserves_owner_and_arguments(tmp_path):
    from pathlib import PureWindowsPath
    import xml.etree.ElementTree as ET
    import subprocess
    old=PureWindowsPath(r'C:\Users\Owner Name\.remote-dev-agent');new=old.with_name('.codepier-agent')
    ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
    root=ET.Element('{'+ns+'}Task')
    principal=ET.SubElement(ET.SubElement(root,'{'+ns+'}Principals'),'{'+ns+'}Principal')
    ET.SubElement(principal,'{'+ns+'}UserId').text='fixture-original-user'
    action=ET.SubElement(ET.SubElement(root,'{'+ns+'}Actions'),'{'+ns+'}Exec')
    for key,value in [('Command',str(old/'runtime/.venv/Scripts/pythonw.exe')),('Arguments',subprocess.list2cmdline([str(old/'runtime/run-service.py')])),('WorkingDirectory',str(old/'runtime'))]:
        ET.SubElement(action,'{'+ns+'}'+key).text=value
    raw=ET.tostring(root,encoding='utf-16')
    # Cross-platform string path boundaries, including backslash on Windows.
    output=migration.new_definition(raw,old,new,'schtasks');text=output.decode('utf-16')
    assert 'fixture-original-user' in text and 'CodePierAgent' in text and '.codepier-agent' in text
    assert '.remote-dev-agent' not in text


def test_failed_recovery_never_recreates_an_empty_canonical_directory(tmp_path,monkeypatch):
    f=make_install(tmp_path,monkeypatch);before=(f.base/'config.json').read_bytes()
    def start(base):raise RuntimeError('injected both service starts failed')
    f.backend.start_service=start
    with pytest.raises(RuntimeError,match='recovery needs attention'):migration.migrate_agent(f.base,0,f.backend)
    assert f.base.is_dir() and not f.base.is_symlink() and not (f.home/'.codepier-agent').exists()
    assert (f.base/'config.json').read_bytes()==before
    assert json.loads((f.base/migration.JOURNAL).read_text())['stage']=='recovery_required'
    assert json.loads((f.base/'management.json').read_text())['brand_migration']=='recovery_required'
