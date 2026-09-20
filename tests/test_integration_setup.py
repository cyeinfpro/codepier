"""Owner installation uses temporary config/profile paths, never personal Chrome."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from agent.integration_config import validate_integrations, origin
from scripts.setup_integrations import configure, restore, write_config
from scripts.install_browser_bridge import install, uninstall, profile_root
from shared.util import atomic_json
from shared.instance_lock import InstanceLock


@pytest.fixture
def configuration(tmp_path):
    source=tmp_path/'projects';source.mkdir()
    value={'hub_url':'http://127.0.0.1:9876','device_id':'fixture-device','secret':'a'*64,
           'state_dir':str(tmp_path/'state'),'allowed_roots':[{'path':str(source),'writable':True,'allow_tasks':True}],
           'shell':{'enabled':True},'mcp_policy':{'block_local_codex':True},
           'integrations':{'local_control':True,'max_import_bytes':123456}}
    path=tmp_path/'agent'/'config.json';atomic_json(path,value)
    return path,value


def test_config_preview_is_read_only_and_apply_keeps_unrelated_fields(configuration):
    path,value=configuration;before=path.read_bytes()
    fragment={'language_servers':{'python':{'command':['pyright-langserver','--stdio'],'projects':['Imago']}}}
    preview=configure(path,fragment)
    assert not preview['changed'] and path.read_bytes()==before and not (path.parent/'integration-backups').exists()
    result=configure(path,fragment,True);now=json.loads(path.read_text())
    assert result['changed'] and not result['service_restarted']
    assert {k:v for k,v in now.items() if k!='integrations'}=={k:v for k,v in value.items() if k!='integrations'}
    assert Path(result['backup']).read_bytes()==before
    if os.name!='nt':assert Path(result['backup']).stat().st_mode&0o777==0o600
    # Revert only the optional integration block, even after an unrelated edit.
    now['tasks']={'owner':{'command':['echo','kept'],'projects':['Imago']}};atomic_json(path,now)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    restored=restore(path,result['backup'],digest,True)
    assert restored['changed'] and json.loads(path.read_text())['tasks']==now['tasks']
    assert json.loads(path.read_text())['integrations']==value['integrations']


def test_config_conflict_and_invalid_settings_do_not_write(configuration):
    path,value=configuration;before=path.read_bytes()
    for fragment in ({'browser':{'enabled':True}}, {'unknown':True}, {'language_servers':{'python':{'command':[]}}}):
        with pytest.raises(ValueError):configure(path,fragment,True)
        assert path.read_bytes()==before
    with pytest.raises(ValueError):write_config(path,b'outdated',value)
    with pytest.raises(ValueError):restore(path,path,'0'*64,True)
    assert path.read_bytes()==before


def test_setup_lock_is_released_and_excludes_other_writers(tmp_path):
    lock_path=tmp_path/'owner.lock'
    with InstanceLock(lock_path):
        script='from pathlib import Path; from shared.instance_lock import InstanceLock; InstanceLock(Path('+repr(str(lock_path))+'))'
        result=subprocess.run([sys.executable,'-c',script],capture_output=True)
        assert result.returncode!=0
    with InstanceLock(lock_path):pass


def test_browser_install_and_uninstall_restore_only_owned_settings(configuration,tmp_path):
    path,value=configuration;before=path.read_bytes();profile=tmp_path/'ChromeProfile';profile.mkdir()
    (profile/'Owner Login Data').write_text('fixture login remains untouched')
    kwargs={'root':profile,'browser':'chromium'}
    extension='a'*32;identity='b'*32
    preview=install(path,extension,identity,['Imago'],['https://example.com'],**kwargs)
    assert not preview['changed'] and path.read_bytes()==before and not Path(preview['manifest']).exists()
    if os.name=='nt':pytest.skip('POSIX wrapper test; native Windows executable is separately built on Windows')
    result=install(path,extension,identity,['Imago'],['https://example.com'],apply=True,**kwargs)
    manifest=Path(result['manifest']);document=json.loads(manifest.read_text());launcher=Path(document['path'])
    assert result['changed'] and not result['service_restarted']
    assert document['allowed_origins']==['chrome-extension://'+extension+'/']
    assert launcher.is_file() and launcher.stat().st_mode&0o777==0o700
    assert '--extension-id' in launcher.read_text() and 'codepier_browser_host.py' in launcher.read_text()
    assert json.loads(path.read_text())['integrations']['local_control'] is True
    with pytest.raises(ValueError):install(path,extension,identity,['Imago'],['https://example.com'],apply=True,**kwargs)
    current=json.loads(path.read_text());current['integrations']['max_import_bytes']=654321;atomic_json(path,current)
    assert not uninstall(path)['changed'] and manifest.exists()
    removed=uninstall(path,True)
    assert removed['browser_profile_preserved'] and not manifest.exists() and not launcher.exists()
    final=json.loads(path.read_text())
    assert final['integrations']['max_import_bytes']==654321 and 'browser' not in final['integrations']
    assert final['secret']==value['secret'] and (profile/'Owner Login Data').exists()


def test_browser_uninstall_refuses_changed_owned_files(configuration,tmp_path):
    if os.name=='nt':pytest.skip('POSIX host wrapper only')
    path,_=configuration
    result=install(path,'a'*32,'b'*32,['Imago'],['http://127.0.0.1:9001'],root=tmp_path/'profile',apply=True)
    manifest=Path(result['manifest']);original=manifest.read_text();manifest.write_text(original+'\n')
    before=path.read_bytes()
    with pytest.raises(ValueError):uninstall(path,True)
    assert path.read_bytes()==before and manifest.exists()
    manifest.write_text(original);uninstall(path,True)


@pytest.mark.parametrize('site',['https://example.com/path','https://*.example.com','https://u:p@example.com','file:///private','https://example.com?a=b','http://example.com:70000'])
def test_browser_origin_configuration_rejects_ambiguous_authority(site):
    with pytest.raises(ValueError):origin(site)


def test_platform_paths_are_explicit(tmp_path):
    assert str(profile_root('chrome','darwin',tmp_path)).endswith('Library/Application Support/Google/Chrome')
    assert 'Chromium' in str(profile_root('chromium','win32',tmp_path))
    assert str(profile_root('chrome-for-testing','linux',tmp_path)).endswith('google-chrome-for-testing')
