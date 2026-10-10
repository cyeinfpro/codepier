"""Regression cases for installer recovery; never touch the runner's services."""
from __future__ import annotations
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts import install_agent as installer
from tests.test_installer_management import installation, options, bundle
from tests.test_agent_install_script import _invoke_main, write_archive


def _archive_fixture(f, monkeypatch):
    monkeypatch.setattr(installer.sys, 'platform', 'win32')
    monkeypatch.setattr(installer, 'windows_task_registered', lambda name: False)
    monkeypatch.setattr(installer, 'verify_windows_instance_is_stopped', lambda *a: None)
    return f.base / 'config.json'


def test_archive_rechecks_config_after_confirmation(installation, monkeypatch):
    f = installation
    config = _archive_fixture(f, monkeypatch)
    def confirm(prompt):
        config.write_text(json.dumps({**f.current, 'name': 'concurrent edit'}))
        return f.current['device_id']
    monkeypatch.setattr('builtins.input', confirm)
    with pytest.raises(ValueError, match='changed'):
        installer.archive_windows_install(f.base, options())
    assert f.base.is_dir()
    assert json.loads(config.read_text())['name'] == 'concurrent edit'
    assert not list(f.home.glob('.codepier-agent.recovery-*'))


def test_archive_holds_install_lock_during_confirmation(installation, monkeypatch):
    f = installation
    _archive_fixture(f, monkeypatch)
    def confirm(prompt):
        assert (f.base / '.install.lock').is_dir()
        with pytest.raises(ValueError, match='active|lock'):
            installer.archive_windows_install(f.base, options())
        return 'cancel'
    monkeypatch.setattr('builtins.input', confirm)
    with pytest.raises(ValueError, match='cancelled'):
        installer.archive_windows_install(f.base, options())
    assert not (f.base / '.install.lock').exists()


def test_archive_handoff_keeps_owned_maintenance_lock(installation, monkeypatch, tmp_path):
    f = installation
    _archive_fixture(f, monkeypatch)
    monkeypatch.setattr('builtins.input', lambda _: f.current['device_id'])
    handoff = tmp_path / 'archive-plan.json'
    installer.archive_windows_install(f.base, options(archive_handoff_file=str(handoff)))
    plan = json.loads(handoff.read_text())
    assert plan['schema'] == 1 and len(plan['lock_token']) == 32
    owner = json.loads((f.base / '.install.lock/archive.json').read_text())
    assert owner['lock_token'] == plan['lock_token']
    assert plan['files']['config.json']
    assert f.base.exists() and not Path(plan['backup']).exists()


def test_archive_refuses_destination_created_during_confirmation(installation, monkeypatch):
    import hashlib
    f=installation
    _archive_fixture(f,monkeypatch)
    monkeypatch.setattr(installer.os,'urandom',lambda n:b'x'*n)
    monkeypatch.setattr(installer.time,'strftime',lambda fmt:'20000101-000000')
    target=f.base.with_name(f.base.name+'.recovery-20000101-000000-'+hashlib.sha256(b'x'*16).hexdigest()[:8])
    def answer(prompt):
        target.mkdir();(target/'keep.txt').write_text('keep')
        return f.current['device_id']
    monkeypatch.setattr('builtins.input',answer)
    with pytest.raises(ValueError,match='backup already exists'):
        installer.archive_windows_install(f.base,options())
    assert f.base.exists() and (target/'keep.txt').read_text()=='keep'
    assert not (target/f.base.name).exists()


def test_scheduler_credential_error_is_not_treated_as_missing(monkeypatch):
    captured = []
    def run(cmd, **kwargs):
        captured.append(base64.b64decode(cmd[-1]).decode('utf-16-le'))
        return SimpleNamespace(returncode=11)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    with pytest.raises(ValueError, match='Cannot verify'):
        installer.windows_task_registered('RemoteDevAgent')
    # 0x8004130F means missing account data, not a missing registered task.
    assert '-2147216625' not in captured[0]
    assert '-2147024894' in captured[0]


def test_fresh_enrollment_mismatch_never_initializes_config(tmp_path, monkeypatch):
    archive, sha = write_archive(tmp_path)
    base = tmp_path / 'new-agent'
    base.mkdir()
    calls = []
    def run(command, **kwargs):
        calls.append([str(x) for x in command])
    with pytest.raises(ValueError, match='different|another|mismatch'):
        _invoke_main(monkeypatch, archive=archive, digest=sha, base=base, run=run,
                     expected_device='intended-node',
                     enroll=lambda *args: {'device_id':'other-node','secret':'private',
                                          'name':'Other','hub_url':'https://hub.example'})
    assert not (base/'config.json').exists()
    assert not any('init' in command for command in calls)
    assert not (base/'runtime').exists()


def test_start_service_respects_expected_device_before_any_command(installation, monkeypatch):
    f = installation
    monkeypatch.setattr(installer, 'start_service', lambda *a: pytest.fail('wrong target was started'))
    monkeypatch.setattr(installer.sys, 'argv', ['install_agent.py', '--start-service',
                        '--install-dir', str(f.base), '--expected-device', 'different-node'])
    with pytest.raises(ValueError, match='different|another'):
        installer.main()
    assert not (f.base/'.install.lock').exists()


@pytest.mark.parametrize('header', ['other-device', 'bad;identity'])
def test_hub_mismatch_preserves_ticket_for_correct_device(tmp_path, header):
    import re
    from fastapi.testclient import TestClient
    from tests.install_support import _real_auth_app
    app,store,device,secret,cookie,csrf=_real_auth_app(tmp_path)
    try:
        with TestClient(app) as client:
            client.cookies.set('rd_session',cookie)
            issued=client.post('/api/devices/'+device+'/install-ticket',json={
                'hub_url':'https://hub.invalid','platform':'posix','allow_root':'/projects'},
                headers={'X-RD-CSRF':csrf})
            assert issued.status_code==200
            token=re.search(r'--token\s+(rdi_[A-Za-z0-9_-]{43})',issued.json()['command']).group(1)
            rejected=client.post('/agent/enroll',headers={'Authorization':'Bearer '+token,
                                'X-CodePier-Expected-Device':header})
            assert rejected.status_code==409 and secret not in rejected.text
            assert store.one('SELECT COUNT(*) AS n FROM agent_install_tickets')['n']==1
            accepted=client.post('/agent/enroll',headers={'Authorization':'Bearer '+token,
                                'X-CodePier-Expected-Device':device})
            assert accepted.status_code==200 and accepted.json()['device_id']==device
            assert client.post('/agent/enroll',headers={'Authorization':'Bearer '+token}).status_code==401
    finally:
        store.close()


def test_client_enrollment_sends_expected_identity_and_rejects_wrong_response(monkeypatch):
    from tests.test_agent_install_script import _Opener
    opener=_Opener(json.dumps({'device_id':'other','secret':'private','hub_url':'https://hub.example'}).encode())
    monkeypatch.setattr(installer.urllib.request,'build_opener',lambda *a:opener)
    with pytest.raises(ValueError,match='another device'):
        installer.enroll('https://hub.example','rdi_fixture','expected')
    assert opener.request.get_header('X-codepier-expected-device')=='expected'


def test_same_identity_pending_service_resume_preserves_config(installation, monkeypatch):
    f=installation
    _archive_fixture(f,monkeypatch)
    python=f.base/'runtime/.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True,exist_ok=True);python.write_bytes(b'fixture')
    metadata=json.loads((f.base/'management.json').read_text())
    metadata.update(service_kind='schtasks',service_scope='user',status='service_error')
    (f.base/'management.json').write_text(json.dumps(metadata))
    original=(f.base/'config.json').read_bytes()
    events=[]
    monkeypatch.setattr(installer,'start_service',lambda *a:events.append('start'))
    monkeypatch.setattr(installer,'enroll',lambda *a:pytest.fail('resume must not re-enroll'))
    assert installer.resume_windows_service_install(f.base,f.current)
    assert events==['start'] and (f.base/'config.json').read_bytes()==original
    assert json.loads((f.base/'management.json').read_text())['status']=='ready'
    assert (f.base/'codepier-agent.ps1').is_file()


def test_archive_refuses_new_task_registered_during_confirmation(installation, monkeypatch):
    f=installation
    _archive_fixture(f,monkeypatch)
    state={'registered':False}
    monkeypatch.setattr(installer,'windows_task_registered',lambda name:state['registered'] and name=='CodePierAgent')
    def answer(prompt):
        state['registered']=True
        return f.current['device_id']
    monkeypatch.setattr('builtins.input',answer)
    with pytest.raises(ValueError,match='registration changed'):
        installer.archive_windows_install(f.base,options())
    assert f.base.exists() and not (f.base/'.install.lock').exists()


def test_service_xml_is_preserved_when_archive_removes_registration(installation, monkeypatch):
    f=installation
    _archive_fixture(f,monkeypatch)
    metadata=json.loads((f.base/'management.json').read_text())
    metadata.update(service_kind='schtasks',service_scope='user',service_name='RemoteDevAgent')
    (f.base/'management.json').write_text(json.dumps(metadata))
    service=f.base/'service.xml';service.write_bytes(b'original service definition')
    state={'registered':True}
    monkeypatch.setattr(installer,'windows_task_registered',lambda name:state['registered'] and name=='RemoteDevAgent')
    monkeypatch.setattr(installer,'verify_service_ownership',lambda *a:None)
    monkeypatch.setattr('builtins.input',lambda _:f.current['device_id'])
    def remove(base,*,remove):
        service.unlink();state['registered']=False
    monkeypatch.setattr(installer,'load_lifecycle_helper',lambda _:SimpleNamespace(stop_service=remove))
    installer.archive_windows_install(f.base,options())
    backups=list(f.home.glob('.codepier-agent.recovery-*'))
    assert len(backups)==1 and (backups[0]/'service.xml').read_bytes()==b'original service definition'



def test_recovery_hint_is_literal_powershell_call_not_cmd_syntax(monkeypatch):
    monkeypatch.setattr(installer,'os',SimpleNamespace(name='nt'))
    arguments=["C:/Agent O'Brien/python.exe","C:/Agent/install_agent.py","--start-service"]
    assert installer.display_command(arguments)=="& 'C:/Agent O''Brien/python.exe' 'C:/Agent/install_agent.py' '--start-service'"


def test_failed_task_removal_keeps_original_service_snapshot(installation, monkeypatch):
    f=installation
    _archive_fixture(f,monkeypatch)
    metadata=json.loads((f.base/'management.json').read_text())
    metadata.update(service_kind='schtasks',service_scope='user',service_name='RemoteDevAgent')
    (f.base/'management.json').write_text(json.dumps(metadata))
    service=f.base/'service.xml';service.write_bytes(b'saved registration')
    monkeypatch.setattr(installer,'windows_task_registered',lambda name:name=='RemoteDevAgent')
    monkeypatch.setattr(installer,'verify_service_ownership',lambda *a:None)
    monkeypatch.setattr('builtins.input',lambda _:f.current['device_id'])
    def remove(base,*,remove):
        service.unlink()
        raise RuntimeError('injected failure after task deletion')
    monkeypatch.setattr(installer,'load_lifecycle_helper',lambda _:SimpleNamespace(stop_service=remove))
    with pytest.raises(RuntimeError,match='injected failure'):
        installer.archive_windows_install(f.base,options())
    assert f.base.exists()
    saved=list(f.base.glob('.recovery-service-*.xml'))
    assert len(saved)==1 and saved[0].read_bytes()==b'saved registration'


@pytest.mark.parametrize('action', ['start','preflight'])
def test_scheduler_query_failure_never_triggers_registration(installation, monkeypatch, action):
    f=installation
    _archive_fixture(f,monkeypatch)
    def query(name):
        raise ValueError('Cannot verify Windows scheduled tasks')
    monkeypatch.setattr(installer,'windows_task_registered',query)
    monkeypatch.setattr(installer,'register_windows_task',lambda *a:pytest.fail('must not overwrite task after failed query'))
    if action=='start':
        call=lambda:installer.start_service(f.base,f.base/'runtime/.venv/Scripts/python.exe')
    else:
        call=lambda:installer.service_preflight(f.base)
    with pytest.raises(ValueError,match='Cannot verify'):
        call()
    assert f.base.exists()
