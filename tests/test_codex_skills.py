import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.skills import Skills, parse_yaml, validate_skills
from shared.contracts import TOOLS
from shared.util import DevError
from tests.test_agentdock_context import workspace, context


def params(tool, **values):
    return TOOLS[tool].model.model_validate({'project':'P',**values}).model_dump()


def skill(directory, name='demo', text='Use the checklist.', extra=''):
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'SKILL.md').write_text('---\nname: '+name+'\ndescription: |\n  Useful local skill\n  with multiline metadata\n'+extra+'---\n'+text,encoding='utf-8')
    return directory


@pytest.fixture
def local(workspace, tmp_path, monkeypatch):
    engine, project, root = workspace
    home = tmp_path/'home';home.mkdir()
    monkeypatch.setattr(Path,'home',classmethod(lambda _:home))
    monkeypatch.delenv('CODEX_HOME',raising=False)
    return engine,project,root,home


def enable(local):
    engine,project,root,home=local
    engine.config['skills']={'codex_enabled':True,'codex_projects':['p']}
    return Skills(engine)


def test_default_is_project_only_and_global_opt_in_is_scoped(local):
    engine,p,root,home=local
    skill(root/'.codex/skills/local')
    skill(home/'.codex/skills/personal',name='personal')
    manager=Skills(engine)
    assert [s['name'] for s in manager.list(p,params('skills_list'))['skills']]==['demo']
    manager=enable(local)
    assert {s['source'] for s in manager.list(p,params('skills_list'))['skills']}=={'project','codex'}
    denied=dict(p,id='another',alias='Other')
    assert [s['name'] for s in manager.list(denied,params('skills_list'))['skills']]==['demo']


def test_user_system_and_nested_project_skills_preserve_same_name(local):
    engine,p,root,home=local;manager=enable(local)
    skill(home/'.codex/skills/.system/creator',name='same')
    skill(home/'.agents/skills/personal',name='same')
    skill(root/'.agents/skills/repo',name='same')
    skill(root/'src/.agents/skills/feature',name='feature')
    skill(root.parent/'.agents/skills/outside',name='not-allowed')
    result=manager.list(p,params('skills_list',cwd='src'))
    assert len(result['skills'])==4 and len({s['skill_id'] for s in result['skills']})==4
    assert all(s['name_conflict'] for s in result['skills'] if s['name']=='same')
    assert 'not-allowed' not in json.dumps(result)
    assert all('multiline metadata' in s['description'] for s in result['skills'])


def test_read_resources_pagination_sha_changes_and_no_execution(local):
    engine,p,root,home=local;manager=enable(local)
    folder=skill(home/'.codex/skills/demo',text='long 中文 ' * 100)
    (folder/'scripts').mkdir();(folder/'scripts/check.py').write_text("raise RuntimeError('NOT EXECUTED')")
    (folder/'references').mkdir();(folder/'references/doc.md').write_text('reference')
    listed=manager.list(p,params('skills_list'));s=listed['skills'][0]
    chunks=[];offset=0;sha=s['sha256']
    while True:
        r=manager.read(p,params('skills_read',skill_id=s['skill_id'],offset=offset,max_chars=100,expected_sha256=sha))
        chunks.append(r['content'])
        if not r['truncated']:break
        offset=r['next_offset']
    assert ''.join(chunks)==(folder/'SKILL.md').read_text()
    assert not r['execution']['automatic']
    assert 'scripts/check.py' in {f['path'] for f in r['resources']}
    script=manager.read(p,params('skills_read',skill_id=s['skill_id'],resource_path='scripts/check.py'))
    assert 'NOT EXECUTED' in script['content']
    (folder/'SKILL.md').write_text((folder/'SKILL.md').read_text()+'changed')
    with pytest.raises(DevError) as exc:manager.read(p,params('skills_read',skill_id=s['skill_id'],expected_sha256=sha))
    assert exc.value.code=='SKILL_FILE_CHANGED'


def test_disabled_codex_settings_are_honored_and_private_values_not_returned(local):
    engine,p,root,home=local;manager=enable(local)
    folder=skill(home/'.codex/skills/demo')
    config=home/'.codex/config.toml'
    config.write_text('secret = "NEVER RETURN ME"\n[[skills.config]]\npath = '+json.dumps(str(folder/'SKILL.md'))+'\nenabled = false\n')
    assert not manager.list(p,params('skills_list'))['skills']
    data=manager.list(p,params('skills_list',include_disabled=True))
    s=data['skills'][0];assert not s['enabled']
    assert 'NEVER RETURN ME' not in json.dumps(data)
    with pytest.raises(DevError) as exc:manager.read(p,params('skills_read',skill_id=s['skill_id'],explicit=True))
    assert exc.value.code=='SKILL_DISABLED'
    config.write_text('bad=[')
    data=manager.list(p,params('skills_list',include_disabled=True))
    assert not data['skills'][0]['enabled'] and data['warnings']


def test_explicit_only_policy_and_dependency_metadata(local):
    engine,p,root,home=local;manager=enable(local)
    folder=skill(home/'.codex/skills/demo');(folder/'agents').mkdir()
    (folder/'agents/openai.yaml').write_text('interface:\n  display_name: Friendly Skill\npolicy:\n  allow_implicit_invocation: false\ndependencies:\n  tools:\n    - type: mcp\n      value: anotherServer\n      url: https://example.invalid/secret-token\n')
    s=manager.list(p,params('skills_list'))['skills'][0]
    assert s['display_name']=='Friendly Skill' and not s['allow_implicit_invocation']
    assert s['dependencies']==[{'type':'mcp','value':'anotherServer'}]
    with pytest.raises(DevError) as exc:manager.read(p,params('skills_read',skill_id=s['skill_id']))
    assert exc.value.code=='SKILL_EXPLICIT_REQUIRED'
    assert manager.read(p,params('skills_read',skill_id=s['skill_id'],explicit=True))['content']


@pytest.mark.parametrize('text',['a: &a [*a]','a: !!python/object/apply:os.system [echo BAD]','a: 1\na: 2','a: '+('['*30)+'1'+(']'*30)])
def test_yaml_complexity_and_object_construction_rejected(text):
    with pytest.raises(DevError):parse_yaml(text)


@pytest.mark.parametrize('path',['../config.toml','/etc/passwd','scripts/../../config.toml','.env','auth.json','scripts/key.pem'])
def test_skill_reads_do_not_become_arbitrary_home_read(local,path):
    engine,p,root,home=local;manager=enable(local);folder=skill(home/'.codex/skills/demo')
    s=manager.list(p,params('skills_list'))['skills'][0]
    with pytest.raises(DevError):manager.read(p,params('skills_read',skill_id=s['skill_id'],resource_path=path))


def test_links_inside_approved_collections_work_external_links_do_not(local,tmp_path):
    engine,p,root,home=local;manager=enable(local)
    target=skill(home/'.codex/skills/target');(home/'.codex/skills/alias').symlink_to(target,target_is_directory=True)
    outside=skill(tmp_path/'outside',name='leaked');(home/'.codex/skills/leak').symlink_to(outside,target_is_directory=True)
    result=manager.list(p,params('skills_list'))
    assert len(result['skills'])==1 and result['skills'][0]['name']=='demo'
    assert any(w['code']=='SKILL_LINK_TARGET_NOT_ALLOWED' for w in result['warnings'])
    (target/'references').symlink_to(outside,target_is_directory=True)
    with pytest.raises(DevError):manager.read(p,params('skills_read',skill_id=result['skills'][0]['skill_id'],resource_path='references/SKILL.md'))


def test_catalog_changed_and_enablement_revoked_between_reads(local):
    engine,p,root,home=local;manager=enable(local)
    skill(home/'.codex/skills/a',name='a');skill(home/'.codex/skills/b',name='b')
    first=manager.list(p,params('skills_list',limit=1))
    assert first['next_offset']==1
    skill(home/'.codex/skills/c',name='c')
    with pytest.raises(DevError) as exc:manager.list(p,params('skills_list',offset=1,catalog_sha256=first['catalog_sha256']))
    assert exc.value.code=='SKILLS_CATALOG_CHANGED'
    engine.config['skills']['codex_enabled']=False
    with pytest.raises(DevError):manager.read(p,params('skills_read',skill_id=first['skills'][0]['skill_id']))


def test_project_context_discovers_global_only_when_enabled(local):
    engine,p,root,home=local
    skill(home/'.codex/skills/demo')
    assert not context((engine,p,root))['codex_skills']
    enable(local)
    r=context((engine,p,root));assert r['codex_skills'][0]['name']=='demo'
    assert r['skills_catalog']['codex_enabled_for_project']
    assert context((engine,p,root),include_skills=False)['codex_skills']==[]


def test_custom_codex_home_and_scoped_extra_sources(local,monkeypatch,tmp_path):
    engine,p,root,home=local
    custom=tmp_path/'codex-other';skill(custom/'skills/demo')
    extra=skill(tmp_path/'custom-skills',name='extra')
    manager=enable(local);monkeypatch.setenv('CODEX_HOME',str(custom))
    engine.config['skills']['extra_roots']=[{'path':str(extra),'projects':['p']}]
    result=manager.list(p,params('skills_list'))
    assert {s['name'] for s in result['skills']}=={'demo','extra'}
    engine.config['skills']['codex_home']=str(home/'.codex')
    assert {s['name'] for s in manager.list(p,params('skills_list'))['skills']}=={'extra'}


@pytest.mark.parametrize('value',[None,{'codex_enabled':'true'},{'codex_projects':'*'},{'codex_home':'relative'},{'codex_home':4},{'unknown':True},{'extra_roots':[{'path':'relative','projects':['*']}]}])
def test_invalid_local_config_is_rejected(value):
    with pytest.raises(ValueError):validate_skills(value)


def test_cli_optin_does_not_prompt_or_change_shell_and_connection(local,tmp_path):
    from shared.util import atomic_json
    from shared.crypto import token
    engine,p,root,home=local
    config=tmp_path/'fixture-config.json'
    value={'device_id':'test','secret':token(),'hub_url':'http://127.0.0.1:9','state_dir':str(tmp_path/'fixture-state'),
        'allowed_roots':[{'path':str(root),'writable':True,'allow_tasks':False}], 'shell':{'enabled':False},'tasks':{}}
    atomic_json(config,value)
    command=[sys.executable,'-m','agent','--config',str(config),'configure','--codex-skills','enabled','--skill-project','P']
    result=subprocess.run(command,input='',text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    changed=json.loads(config.read_text());assert changed['skills']['codex_projects']==['P']
    assert changed['hub_url']==value['hub_url'] and changed['secret']==value['secret']
    assert not changed['shell']['enabled'] and not changed['allowed_roots'][0]['allow_tasks']
    assert value['secret'] not in result.stdout+result.stderr


def test_linked_skill_markdown_requires_explicit_source_and_uses_target_resources(local,tmp_path):
    engine,p,root,home=local;manager=enable(local)
    target=skill(tmp_path/'outside-browser',name='browser');(target/'references').mkdir();(target/'references/guide.md').write_text('browser reference')
    alias=home/'.codex/skills/browser';alias.mkdir(parents=True)
    (alias/'SKILL.md').symlink_to(target/'SKILL.md')
    assert not manager.list(p,params('skills_list'))['skills']
    engine.config['skills']['extra_roots']=[{'path':str(target),'projects':['p']}]
    r=manager.list(p,params('skills_list'));assert len(r['skills'])==1
    s=r['skills'][0];assert s['skill_dir']==str(target)
    content=manager.read(p,params('skills_read',skill_id=s['skill_id'],resource_path='references/guide.md'))
    assert content['content']=='browser reference'
    engine.config['skills']['extra_roots']=[]
    with pytest.raises(DevError):manager.read(p,params('skills_read',skill_id=s['skill_id']))


def test_read_rejects_hardlinks_binary_and_resource_symlinks(local,tmp_path):
    engine,p,root,home=local;manager=enable(local);folder=skill(home/'.codex/skills/demo')
    (folder/'binary.png').write_bytes(b'\x89PNG\x00binary')
    (folder/'plain.txt').write_text('ordinary')
    os.link(folder/'plain.txt',folder/'hard.txt')
    (folder/'link.txt').symlink_to(folder/'SKILL.md')
    s=manager.list(p,params('skills_list'))['skills'][0]
    for name in ('binary.png','hard.txt','link.txt'):
        with pytest.raises(DevError):manager.read(p,params('skills_read',skill_id=s['skill_id'],resource_path=name))
