"""Exercise real runtime commands with a deterministic Docker boundary, not a live deployment."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from scripts import panel_updater as updater
from scripts.panel_update_source import UpdateError
from scripts.panel_update_runtime import DockerRuntime, atomic_json, fingerprint, load_compose_config, read_json, save_compose
from shared.util import VERSION
from tests.test_panel_update import TARGET, body_for, manager_fixture


def deployment(root):
    home=root/'.codepier-updater';(home/'run').mkdir(parents=True)
    (root/'.env').write_text('HUB_PUBLIC_URL=https://panel.example\nSECRET=\'keep$me\'\n')
    (root/'compose.yml').write_text('# trusted deployment\n')
    spec={'name':'fixture','services':{'hub':{'image':'codepier:'+VERSION,'build':{'context':str(root)},
        'user':'10001:10001','environment':{'HUB_PORT':'8765','HUB_PANEL_UPDATE_SOCKET':'/run/codepier-updater/updater.sock','SECRET':'keep$me'},
        'ports':[{'target':8765,'published':'18888','host_ip':'127.0.0.1','protocol':'tcp'}],
        'read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],
        'volumes':[{'type':'volume','source':'hub-data','target':'/app/data'},
                   {'type':'bind','source':str(home/'run'),'target':'/run/codepier-updater','read_only':True}],
        'networks':{'default':None}},
        'proxy':{'image':'existing-proxy','ports':[{'target':443,'published':'443'}],'networks':{'default':None}}},
        'volumes':{'hub-data':{'external':True,'name':'original-data'}},'networks':{'default':{'name':'existing-net'}}}
    current={'version':VERSION,'compose':spec,'files':{name:fingerprint(root/name) for name in ('.env','compose.yml')}}
    atomic_json(home/'current.json',current)
    config={'project':'fixture','repository':'cyeinfpro/codepier'};atomic_json(home/'config.json',config)
    job={'id':'f'*32,'from_version':VERSION,'target_version':TARGET,'package':{'agent_sha256':'a'*64,'agent_bytes':123}}
    (home/'jobs'/job['id']).mkdir(parents=True)
    atomic_json(home/'jobs'/job['id']/'current.before.json',current)
    return home,config,job,spec


class DockerBoundary:
    def __init__(self,root,spec):self.root,self.spec,self.commands,self.phase=root,spec,[], 'old'
    def __call__(self,command,**kwargs):
        self.commands.append(command)
        if command[:2]==['docker','inspect']:
            return json.dumps([{'Image':'sha256:'+'b'*64,'State':{'Running':False},'Config':{'Labels':{
                'com.docker.compose.project':'fixture','com.docker.compose.service':'hub'}},
                'Mounts':[{'Destination':'/app/data','Name':'original-data'},
                          {'Destination':'/run/codepier-updater','RW':False,'Source':str(self.root/'.codepier-updater/run')}]}])
        if command[:2]==['docker','exec']:
            if '/agent/manifest.json' in command[-1]:return json.dumps({'version':TARGET,'sha256':'a'*64,'bytes':123})
            return json.dumps({'status':'ok','version':VERSION if self.phase=='old' else TARGET,'panel_update':{'maintenance':True,'ready':True}})
        if command[:3]==['docker','volume','ls']:return '"original-data"'
        if command[:3]==['docker','volume','create']:return command[-1]
        if command[:2]==['docker','run']:
            if '/backup' in command[-1]:return '{"files":4,"bytes":512,"integrity":"ok"}'
            return json.dumps({'version':TARGET,'agent_sha256':'a'*64,'agent_bytes':123})
        if command[:2]==['docker','build']:return 'built'
        if 'ps' in command:return 'container-id'
        if 'up' in command:
            self.phase='target' if 'target.json' in ' '.join(command) else 'old'
            return 'healthy'
        if 'stop' in command:return 'stopped'
        raise AssertionError(command)


def test_real_runtime_pipeline_preserves_proxy_ports_configuration_and_backup(tmp_path):
    home,config,job,spec=deployment(tmp_path);docker=DockerBoundary(tmp_path,spec)
    runtime=DockerRuntime(tmp_path,home,config,execute=docker)
    source=home/'releases'/job['id']/'source';source.mkdir(parents=True)
    job.update(runtime.prepare(job,source))
    old=read_json(home/'jobs'/job['id']/'old.spec.json')
    target=read_json(home/'jobs'/job['id']/'target.spec.json')
    assert old['services']['hub']['image'].startswith('sha256:')
    assert 'build' not in target['services']['hub']
    assert target['services']['proxy']==spec['services']['proxy']
    assert target['services']['hub']['ports']==spec['services']['hub']['ports']
    assert target['services']['hub']['environment']==spec['services']['hub']['environment']
    job['package']=runtime.build(job,source)
    runtime.quiesce(job);runtime.stop(job);runtime.copy_data(job);runtime.start(job,'target');runtime.verify(job);runtime.commit(job)
    assert runtime.gate.exists()  # Only Manager's durable commit decision opens admission.
    assert read_json(home/'current.json')['version']==TARGET
    assert 'SECRET=\'keep$me\'' in (tmp_path/'.env').read_text()
    assert 'CODEPIER_HUB_DATA_VOLUME='+job['new_volume'] in (tmp_path/'.env').read_text()
    starts=[c for c in docker.commands if 'up' in c]
    assert len(starts)==1 and starts[0][-1]=='hub' and '--no-deps' in starts[0] and '--no-build' in starts[0]
    assert '--pull' in starts[0] and 'never' in starts[0]
    assert not any('rm' in c or 'down' in c or 'prune' in c for c in docker.commands)
    assert not any('sh' in c or 'bash' in c for c in docker.commands)
    runtime.rollback(job)
    assert read_json(home/'current.json')['version']==VERSION
    assert (tmp_path/'.env').read_text()=='HUB_PUBLIC_URL=https://panel.example\nSECRET=\'keep$me\'\n'
    assert not runtime.gate.exists()
    stops=[c for c in docker.commands if 'stop' in c]
    assert all('old.json' in ' '.join(c) for c in stops)


def test_environment_drift_detected_before_service_stop(tmp_path):
    home,config,job,spec=deployment(tmp_path);docker=DockerBoundary(tmp_path,spec)
    runtime=DockerRuntime(tmp_path,home,config,execute=docker)
    (tmp_path/'compose.yml').write_text('# edited by operator\n')
    with pytest.raises(UpdateError) as e:runtime.quiesce(job)
    assert e.value.code=='DEPLOYMENT_CHANGED' and not docker.commands and not runtime.gate.exists()


def test_compose_cli_keeps_literal_dollars_without_docker_daemon(tmp_path):
    docker=shutil.which('docker')
    standalone=shutil.which('docker-compose')
    candidates=([[docker,'compose']] if docker else [])+([[standalone]] if standalone else [])
    command=next((candidate for candidate in candidates if subprocess.run(candidate+['version'],capture_output=True,text=True,timeout=10).returncode==0),None)
    if command is None:pytest.skip('Neither Docker Compose plugin nor standalone CLI is installed; a daemon is not required')
    spec={'services':{'hub':{'image':'fixture:local','environment':{'LITERAL':'a$B${SECRET}$$'}}}}
    path=tmp_path/'target.json';save_compose(path,spec)
    result=subprocess.run([*command,'--project-name','fixture','-f',str(path),'config','--format','json'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    # `config` serializes the resolved model with dollars escaped for reuse.
    # Decode that transport representation, not the literal serialized string.
    resolved=load_compose_config(result.stdout)
    assert resolved['services']['hub']['environment']['LITERAL']=='a$B${SECRET}$$'
    # Repeated snapshot/save cycles must never accumulate dollar escaping.
    for _ in range(2):
        save_compose(path,resolved)
        result=subprocess.run([*command,'--project-name','fixture','-f',str(path),'config','--format','json'],capture_output=True,text=True,timeout=15)
        assert result.returncode==0,result.stderr
        resolved=load_compose_config(result.stdout)
        assert resolved['services']['hub']['environment']['LITERAL']=='a$B${SECRET}$$'


def test_compose_config_decodes_values_and_keys_once(tmp_path):
    spec={'services':{'hub':{'image':'fixture:local','environment':{
        'DOLLAR$KEY':'a$B${SECRET}$$','JSON':'{"path":"C:\\\\folder","quote":"\\\"$value"}',
        'UNICODE':'密码$保留','TRAILING':'$$$',
    }}}}
    raw=json.dumps(spec,ensure_ascii=False).replace('$','$$')
    decoded=load_compose_config(raw)
    assert decoded==spec
    target=tmp_path/'target.json';save_compose(target,decoded)
    saved=json.loads(target.read_text())['services']['hub']['environment']
    assert set(saved)==set(spec['services']['hub']['environment'])
    assert saved['DOLLAR$KEY']=='a$$B$${SECRET}$$$$'
    assert read_json(target.with_suffix('.spec.json'))==spec


def test_admission_fence_and_terminal_lock_cleanup(tmp_path):
    manager,release=manager_fixture(tmp_path)
    with updater.admission_lock(manager.home):
        with pytest.raises(UpdateError) as e:manager.submit('check',body_for())
        assert e.value.code=='UPDATER_INSTALLING'
    manager.submit('apply',body_for(release));job=manager.current_job();manager.acquire_install_lock(job)
    manager.save(job,state='succeeded',phase='done',commit_decided=True)
    manager.recover()
    assert not (tmp_path/'.codepier-install.lock').exists()
    assert manager.runtime.calls==[]


def test_installer_generates_private_service_and_fences_reinstallation(monkeypatch):
    # Real filesystem/locks and generated unit; Linux/Docker/systemctl boundaries only are simulated.
    with tempfile.TemporaryDirectory(prefix='cp-install-',dir='/tmp') as directory:
        root=Path(directory);home,config,job,spec=deployment(root)
        units=root/'systemd';units.mkdir();systemd=root/'running-systemd';systemd.mkdir()
        original_path=Path
        def paths(value):
            if str(value)=='/etc/systemd/system':return units
            if str(value)=='/run/systemd/system':return systemd
            return original_path(value)
        monkeypatch.setattr(updater,'Path',paths)
        monkeypatch.setattr(updater.sys,'platform','linux')
        monkeypatch.setattr(updater.os,'geteuid',lambda:0)
        monkeypatch.setattr(updater,'secure_host_path',lambda path:None)
        commands=[]
        def execute(command,**kwargs):
            commands.append(command)
            if command[:2]==['systemctl','stop']:
                manager=updater.Manager(root,start_workers=False)
                with pytest.raises(UpdateError) as error:manager.submit('check',body_for())
                assert error.value.code=='UPDATER_INSTALLING'
                return ''
            if command[0]=='systemctl':return ''
            if 'ps' in command:return 'container-id'
            if command[:2]==['docker','inspect']:
                return json.dumps([{'Config':{'Labels':{'com.docker.compose.project.config_files':str(root/'compose.yml')}}}])
            if 'config' in command:return json.dumps(spec)
            if command[:2]==['docker','exec']:
                compile(command[-1],'<probe>','exec')
                return json.dumps(VERSION) if 'VERSION' in command[-1] else ''
            raise AssertionError(command)
        monkeypatch.setattr(updater,'run',execute)
        updater.install(root,'cyeinfpro/codepier')
        unit=units/updater.unit_name(root);assert unit.is_file()
        assert 'KillMode=control-group' in unit.read_text()
        assert '.codepier-updater/service/panel_updater.py' in unit.read_text()
        assert not (unit.stat().st_mode&0o022)
        assert (home/'config.json').stat().st_mode&0o777==0o600
        assert all((home/'service'/name).is_file() for name in ('panel_updater.py','panel_update_runtime.py','panel_update_source.py'))
        updater.install(root,'cyeinfpro/codepier')
        assert any(c[:2]==['systemctl','stop'] for c in commands)
        manager=updater.Manager(root,start_workers=False)
        manager.submit('check',body_for())
        commands.clear()
        with pytest.raises(UpdateError) as error:updater.install(root,'cyeinfpro/codepier')
        assert error.value.code=='UPDATER_BUSY' and not commands


def test_first_install_channel_remains_readable_with_private_umask(tmp_path):
    installer=(Path(__file__).resolve().parents[1]/'deploy/install-hub.sh').read_text()
    start=installer.index('[[ ! -L "$root/.codepier-updater"')
    end=installer.index('if ((enable_panel_update == 1)); then',start)
    script='set -e; umask 077; root="$1";\n'+installer[start:end]
    result=subprocess.run(['bash','-c',script,'fixture',str(tmp_path)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    home=tmp_path/'.codepier-updater'
    assert home.stat().st_mode&0o777==0o700
    assert (home/'run').stat().st_mode&0o777==0o755
    assert not (home/'run/maintenance.json').exists()
