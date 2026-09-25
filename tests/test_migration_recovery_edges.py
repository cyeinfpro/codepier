"""F04/F05/F32/F34 counterexamples, using temporary files and fake services."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import pytest

from tests.test_codepier_brand_migration import make_install
from tests.test_codepier_hub_migration import Docker
from shared import brand_migration as migration, brand_browser
from scripts import agent_lifecycle as lifecycle, migrate_hub as hub, migrate_hub_proxy as proxy


def test_windows_rollback_does_not_stop_an_unregistered_new_task(tmp_path, monkeypatch):
    fixture = make_install(tmp_path,monkeypatch)
    metadata=json.loads((fixture.base/'management.json').read_text())
    metadata.update(service_kind='schtasks',service_name='RemoteDevAgent')
    (fixture.base/'management.json').write_text(json.dumps(metadata))
    ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
    root=ET.Element('{'+ns+'}Task')
    action=ET.SubElement(ET.SubElement(root,'{'+ns+'}Actions'),'{'+ns+'}Exec')
    for key,value in [('Command',str(fixture.base/'runtime/.venv/Scripts/python.exe')),
                      ('Arguments',subprocess.list2cmdline([str(fixture.base/'runtime/run-service.py')])),
                      ('WorkingDirectory',str(fixture.base/'runtime'))]:
        ET.SubElement(action,'{'+ns+'}'+key).text=value
    raw=ET.tostring(root,encoding='utf-16')
    (fixture.base/'service.xml').write_bytes(raw)
    directory=fixture.base/'state/browser-bridge';directory.mkdir()
    launcher=directory/'host.sh';launcher.write_text('user-modified launcher')
    (directory/'install-receipt.json').write_text(json.dumps({
        'config':str(fixture.base/'config.json'),'manifest':str(directory/'legacy.json'),
        'files':{str(launcher):'wrong-checksum'}}))
    tasks={'RemoteDevAgent':{'enabled':True,'running':True}}
    calls=[]
    def run(command, **kwargs):
        command=list(map(str,command));calls.append(command)
        if command[0]=='schtasks.exe':
            name=command[command.index('/TN')+1]
            if '/Query' in command:return 0 if name in tasks else 1
            if '/End' in command:tasks[name]['running']=False;return 0
        if command[0]=='powershell.exe':
            script=command[-1]
            name='CodePierAgent' if "GetTask('CodePierAgent')" in script else 'RemoteDevAgent'
            if name not in tasks:
                if kwargs.get('check'):raise RuntimeError('attempted to stop an unregistered new task')
                return 1
            if '$t.Enabled=$false' in script:tasks[name]['enabled']=False
            if '$t.Enabled=$true' in script:tasks[name]['enabled']=True
            if 'GetInstances' in script:return 10 if tasks[name]['running'] else 0
        return 0
    monkeypatch.setattr(lifecycle,'_run',run)
    monkeypatch.setattr(migration.subprocess,'check_output',lambda *_a,**_k:raw)
    backend=fixture.backend
    for name in ('_service','_service_name','_service_target','stop_service'):
        setattr(backend,name,getattr(lifecycle,name))
    backend._run=run;backend.brand_browser=brand_browser
    def restart(base):
        assert base==fixture.base
        tasks['RemoteDevAgent'].update(enabled=True,running=True)
        fixture.calls.append(('start',str(base)))
    backend.start_service=restart
    with pytest.raises(RuntimeError) as error:
        migration.migrate_agent(fixture.base,0,backend)
    assert 'unregistered new task' not in str(error.value)
    assert fixture.base.is_dir() and not fixture.base.is_symlink()
    assert json.loads((fixture.base/migration.JOURNAL).read_text())['stage']=='rolled_back'
    assert tasks['RemoteDevAgent']=={'enabled':True,'running':True}
    assert not any("GetTask('CodePierAgent')" in str(call) for call in calls)
    assert (fixture.project/'untouched.txt').read_text()=='owner project'


@pytest.mark.parametrize('problem',['exported','expression','duplicate'])
def test_proxy_configuration_is_rejected_before_any_service_stop(tmp_path, monkeypatch, problem):
    monkeypatch.delenv(proxy.KEY,raising=False)
    literal='FORWARDED_ALLOW_IPS=127.0.0.1,172.20.0.1\n'
    (tmp_path/'.env').write_text(literal)
    docker=Docker()
    docker.config['services']['hub'].update(environment={proxy.KEY:'127.0.0.1,172.20.0.1'},networks={'default':{}})
    docker.config['networks']={'default':{'name':'codepier_default'}}
    docker.old[0]['NetworkSettings']={'Networks':{'remote-dev-mcp_default':{'Gateway':'172.20.0.1'}}}
    def inspect(args):
        if args[:2]==['network','inspect']:
            old=args[2]=='remote-dev-mcp_default'
            return [{'Driver':'bridge','Labels':{'com.docker.compose.project':'remote-dev-mcp' if old else 'codepier',
                'com.docker.compose.network':'default'},'IPAM':{'Config':[{'Gateway':'172.20.0.1' if old else '172.21.0.1'}]}}]
        return docker.config
    docker.json=inspect
    if problem=='exported':monkeypatch.setenv(proxy.KEY,'127.0.0.1,172.20.0.1')
    elif problem=='expression':(tmp_path/'.env').write_text('FORWARDED_ALLOW_IPS=${UNRESOLVED_FIXTURE}\n')
    else:(tmp_path/'.env').write_text(literal+literal)
    before=(tmp_path/'.env').read_bytes()
    with pytest.raises(RuntimeError):hub.prepare(tmp_path/hub.STATE,docker)
    assert not docker.stopped
    assert not any(command and command[0]=='stop' for command in docker.commands)
    assert (tmp_path/'.env').read_bytes()==before


def test_tilde_state_migrates_the_installed_bridge_and_remains_uninstallable(tmp_path, monkeypatch):
    from agent import install_browser_bridge as installer
    fixture=make_install(tmp_path,monkeypatch)
    monkeypatch.setenv('HOME',str(fixture.home))
    config={**fixture.config,'state_dir':'~/.remote-dev-agent/state'}
    (fixture.base/'config.json').write_text(json.dumps(config))
    monkeypatch.setattr(installer,'NAME',brand_browser.OLD_NAME)
    monkeypatch.setattr(installer,'ROOT',fixture.base/'runtime')
    result=installer.install(fixture.base/'config.json','a'*32,'b'*32,['demo'],['https://example.test'],
                             root=fixture.home/'Chrome',apply=True)
    fixture.backend.brand_browser=brand_browser
    moved=migration.migrate_agent(fixture.base,0,fixture.backend)
    canonical=Path(moved['install_dir'])
    receipt=json.loads((canonical/'state/browser-bridge/install-receipt.json').read_text())
    assert moved['status']=='completed'
    assert Path(result['manifest']).with_name(brand_browser.NEW_NAME+'.json').is_file()
    assert receipt['config']==str(canonical/'config.json')
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in receipt['files'].items())
    monkeypatch.setattr(installer,'NAME',brand_browser.NEW_NAME)
    result=installer.uninstall(canonical/'config.json',apply=True)
    assert result['changed'] and result['state']=='uninstalled'
    assert not Path(receipt['manifest']).exists()


def test_utf8_bom_python_symbols_keep_line_numbers_and_original_bytes():
    from agent.symbols import analyze
    source=b'\xef\xbb\xbfdef bom_fixture():\n    return 1\n'
    before=hashlib.sha256(source).hexdigest()
    result=analyze('fixture.py',source)
    assert result['symbols'][0]['name']=='bom_fixture'
    assert result['symbols'][0]['line']==1
    assert hashlib.sha256(source).hexdigest()==before
