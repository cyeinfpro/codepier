import json, os, uuid, zipfile
from pathlib import Path
import pytest
from agent.filesystem import FileEngine, relative_path
from agent.journal import Journal
from shared.crypto import SecureChannel, digest, password_hash, password_verify, token
from shared.util import DevError, normalize_url, atomic_json, safe_summary
from shared.contracts import TOOLS, tool_definitions

@pytest.fixture
def engine(tmp_path):
    root=tmp_path/'workspace';root.mkdir();(root/'src').mkdir()
    (root/'src'/'app.py').write_text('one\ntwo\nthree\n',encoding='utf-8')
    config=tmp_path/'private'/'config.json';config.parent.mkdir();config.write_text('{}')
    journal=Journal(tmp_path/'state')
    c={'allowed_roots':[{'path':str(root),'writable':True,'allow_tasks':False}]}
    e=FileEngine(c,journal,config)
    yield e,{'root':str(root),'mode':'write','alias':'Imago'},root
    journal.db.close()

@pytest.mark.parametrize('given,expected',[
 ('192.0.2.20:8765','http://192.0.2.20:8765'),('http://localhost:80/','http://localhost:80'),
 ('https://mcp.example.com','https://mcp.example.com'),('http://[::1]:8765','http://[::1]:8765')])
def test_url_supported(given,expected): assert normalize_url(given)==expected

@pytest.mark.parametrize('given',['ftp://localhost','http://user:pass@host','http://host/mcp','http://host?q=1','http://host#fragment','http://host:65536','http://host:0','http://'])
def test_url_rejected(given):
    with pytest.raises(ValueError):normalize_url(given)

@pytest.mark.parametrize('given',['../x','/etc/passwd','x/../../a','C:/Projects/a','x\\file','.env','.git/config','.ssh/config','a/private.pem','a/.env.production','a/.aws/config'])
def test_path_constraints(given):
    with pytest.raises(DevError):relative_path(given)

@pytest.mark.parametrize('given',['README.md','src/a.py','.env.example','docs/example.md','目录/文件.txt'])
def test_valid_paths(given): assert relative_path(given)==given

def test_crypto_roundtrip_replay_direction_tamper():
    secret,challenge=token(),token();a=SecureChannel(secret,challenge,'device','agent');h=SecureChannel(secret,challenge,'device','hub')
    packet=a.pack({'type':'hello','data':'你好'})
    assert '你好' not in packet and h.unpack(packet)['data']=='你好'
    with pytest.raises(Exception):h.unpack(packet)
    response=h.pack({'type':'ready'});assert a.unpack(response)['type']=='ready'
    wrong=SecureChannel(token(),challenge,'device','hub')
    with pytest.raises(Exception):wrong.unpack(a.pack({'type':'hello'}))
    with pytest.raises(Exception):a.unpack(a.pack({'type':'reflection'}))
    wrong_device=SecureChannel(secret,challenge,'another','hub')
    with pytest.raises(Exception):wrong_device.unpack(packet)

def test_password_and_redaction():
    h=password_hash('owner-password-long');assert password_verify('owner-password-long',h);assert not password_verify('wrong',h)
    assert not password_verify('x','malformed');assert h!=password_hash('owner-password-long')
    assert 'testsecret' not in json.dumps(safe_summary({'secret':'testsecret','content':'testsecret','path':'ok.py'}))

def test_contracts():
    from shared.core_contracts import CORE_TOOLS
    definitions=tool_definitions();assert {d['name'] for d in definitions} == CORE_TOOLS
    for tool in definitions:
        assert tool['inputSchema']['type']=='object'
        assert tool['inputSchema']['additionalProperties'] is False
        assert tool['outputSchema']['type']=='object'
        assert tool['outputSchema']['additionalProperties'] is True
        assert tool['annotations']['readOnlyHint']==(tool['name'] in {'read','vps'})

def test_read_pagination_and_search(engine):
    e,p,r=engine
    read=e.read(p,{'path':'src/app.py','start_line':2,'max_lines':1})
    assert read['content']=='two\n' and read['truncated'] and read['next_start_line']==3
    assert read['sha256']==digest((r/'src/app.py').read_bytes())
    args=TOOLS['fs_search'].model(project='Imago',query='TWO').model_dump()
    assert e.search(p,args)['matches'][0]['line']==2

def test_write_cas_restore(engine):
    e,p,r=engine;before=(r/'src/app.py').read_bytes()
    result=e.mutate(p,'src/app.py',digest(before),b'new\n')
    assert (r/'src/app.py').read_bytes()==b'new\n'
    with pytest.raises(DevError):e.mutate(p,'src/app.py',digest(before),b'overwrite')
    restored=e.call('history_restore',p,{'backup_id':result['backup_id'],'expected_sha256':digest(b'new\n')})
    assert restored['sha256']==digest(before) and (r/'src/app.py').read_bytes()==before

def test_create_delete_move_and_new_backup(engine):
    e,p,r=engine
    n=e.mutate(p,'src/new.py','new',b'abc\n')
    with pytest.raises(DevError):e.mutate(p,'src/new.py','new',b'overwritten')
    e.call('history_restore',p,{'backup_id':n['backup_id'],'expected_sha256':n['sha256']})
    assert not (r/'src/new.py').exists()
    sha=digest((r/'src/app.py').read_bytes())
    e.move(p,{'path':'src/app.py','destination':'src/moved.py','expected_sha256':sha})
    assert not (r/'src/app.py').exists() and (r/'src/moved.py').exists()
    e.mutate(p,'src/moved.py',sha,None);assert not (r/'src/moved.py').exists()

def test_exact_edit_ambiguity(engine):
    e,p,r=engine
    (r/'src/app.py').write_text('foo foo')
    args={'path':'src/app.py','expected_sha256':digest(b'foo foo'),'edits':[{'old_text':'foo','new_text':'bar','replace_all':False}]}
    with pytest.raises(DevError):e.edit(p,args)
    args['edits'][0]['replace_all']=True;e.edit(p,args);assert (r/'src/app.py').read_text()=='bar bar'

def test_readonly_and_nested_override(engine):
    e,p,r=engine
    with pytest.raises(DevError):e.mutate({**p,'mode':'read'},'src/app.py',digest(b'one\ntwo\nthree\n'),b'x')
    e.config['allowed_roots'].append({'path':str(r/'src'),'writable':False})
    with pytest.raises(DevError):e.root({**p,'root':str(r/'src')},True)

def test_root_protected_and_config(engine):
    e,p,r=engine
    (r/'.git').mkdir();(r/'.git/config').write_text('secret')
    with pytest.raises(DevError):e.root({**p,'root':str(r/'.git')})
    with pytest.raises(DevError):e.root({**p,'root':str(r.parent)})

def test_links_and_file_limits(engine):
    e,p,r=engine
    outside=r.parent/'outside.txt';outside.write_text('secret')
    (r/'link.txt').symlink_to(outside)
    with pytest.raises(DevError):e.read(p,{'path':'link.txt'})
    os.link(outside,r/'hard.txt')
    with pytest.raises(DevError):e.read(p,{'path':'hard.txt'})
    (r/'large.txt').write_bytes(b'x'*(1048576+1))
    with pytest.raises(DevError):e.read(p,{'path':'large.txt'})
    (r/'binary').write_bytes(b'\x00data')
    with pytest.raises(DevError):e.read(p,{'path':'binary'})
    (r/'invalid.txt').write_bytes(b'\xff\xfe')
    with pytest.raises(DevError):e.read(p,{'path':'invalid.txt'})

def test_checkpoint_exclusions(engine):
    e,p,r=engine;(r/'.env').write_text('secret');(r/'node_modules').mkdir();(r/'node_modules/x').write_text('dependency')
    c=e.checkpoint(p,{'label':'unit'})
    with zipfile.ZipFile(c['local_archive']) as z:
        assert 'files/src/app.py' in z.namelist();assert 'files/.env' not in z.namelist()
        assert not any('node_modules' in x for x in z.namelist())
        manifest=json.loads(z.read('manifest.json'));assert manifest['files'][0]['sha256']

def test_checkpoint_prunes_generated_artifacts_but_keeps_sources(engine):
    e,p,r=engine
    for directory in ['apps/web/.next', 'apps/web/.next-e2e', 'apps/api/.mypy_cache', 'coverage', 'test-results']:
        artifact=r/directory/'chunk.js';artifact.parent.mkdir(parents=True);artifact.write_text('generated')
    (r/'src'/'types.tsbuildinfo').write_text('cache')
    (r/'src'/'coverage.py').write_text('source')
    c=e.checkpoint(p,{'label':'pruned'})
    with zipfile.ZipFile(c['local_archive']) as z:
        names=z.namelist()
        assert 'files/src/app.py' in names and 'files/src/coverage.py' in names
        assert not any('chunk.js' in name or name.endswith('.tsbuildinfo') for name in names)
        manifest=json.loads(z.read('manifest.json'))
        assert len([x for x in manifest['skipped'] if x['reason']=='GENERATED_ARTIFACT'])==6
    # Checkpoint filtering does not hide artifacts from ordinary file inspection.
    assert e.read(p,{'path':'apps/web/.next/chunk.js'})['content']=='generated'

def test_checkpoint_configurable_limit_and_partial_cleanup(engine):
    e,p,r=engine
    (r/'a.txt').write_bytes(b'a'*700000);(r/'b.txt').write_bytes(b'b'*700000)
    e.config['checkpoint_max_mib']=1
    with pytest.raises(DevError, match='1 MiB'):e.checkpoint(p,{'label':'too-large'})
    assert not list(e.journal.checkpoints.iterdir())
    e.config['checkpoint_max_mib']=2
    c=e.checkpoint(p,{'label':'fits'})
    assert c['bytes']>1048576 and c['max_bytes']==2097152
    with zipfile.ZipFile(c['local_archive']) as z:assert z.testzip() is None

def test_tree_pagination(engine):
    e,p,r=engine
    args=TOOLS['fs_tree'].model(project='Imago',limit=1).model_dump()
    first=e.tree(p,args);assert first['truncated'] and first['next_offset']==1
    second=e.tree(p,{**args,'offset':1});assert second['entries'][0]['path']!=first['entries'][0]['path']

def test_search_single_file_filters_paginates_and_preserves_protection(engine):
    e,p,r=engine
    (r/'src'/'app.py').write_text('match one\nmatch two\nmatch three\n')
    args=TOOLS['fs_search'].model(project='Imago',path='src/app.py',query='MATCH',limit=1).model_dump()
    first=e.search(p,args)
    assert first['scanned_files']==1 and first['matches'][0]['line']==1 and first['next_offset']==1
    assert e.search(p,{**args,'offset':1})['matches'][0]['line']==2
    assert e.search(p,{**args,'file_glob':'*.ts'})['scanned_files']==0
    with pytest.raises(DevError,match='搜索路径不存在'):e.search(p,{**args,'path':'missing.py'})
    (r/'.env').write_text('match secret')
    with pytest.raises(DevError):e.search(p,{**args,'path':'.env'})
    (r/'linked.py').symlink_to(r/'src'/'app.py')
    with pytest.raises(DevError):e.search(p,{**args,'path':'linked.py'})
    (r/'binary').write_bytes(b'\x00match')
    assert e.search(p,{**args,'path':'binary'})['skipped_files']==1

def test_journal_idempotency_and_restart(tmp_path):
    j=Journal(tmp_path/'journal');request={'tool':'fs_write','args':{'path':'x'}}
    assert j.start('one',request) is None
    j.finish('one',{'ok':True,'data':{'sha':'abc'}})
    assert j.start('one',request)['ok'];assert len(j.outbox())==1
    with pytest.raises(DevError):j.start('one',{'different':True})
    j.ack('one');assert not j.outbox();j.start('in-flight',request);j.mark_running('in-flight');j.db.close()
    k=Journal(tmp_path/'journal');assert k.outbox()[0]['result']['error']['code']=='INTERRUPTED';k.db.close()

def test_atomic_config_permissions(tmp_path):
    p=tmp_path/'private'/'config.json';atomic_json(p,{'hub_url':'http://192.0.2.1:99'})
    assert json.loads(p.read_text())['hub_url'].endswith(':99')
    if os.name!='nt':assert p.stat().st_mode&0o777==0o600

def test_nested_readonly_applies_inside_parent_mapping(engine):
    e,p,r=engine;e.config['allowed_roots'].append({'path':str(r/'src'),'writable':False})
    with pytest.raises(DevError):e.mutate(p,'src/app.py',digest((r/'src/app.py').read_bytes()),b'forbidden')

def test_process_lock(tmp_path):
    from shared.instance_lock import InstanceLock
    a=InstanceLock(tmp_path/'instance.lock')
    with pytest.raises(RuntimeError):InstanceLock(tmp_path/'instance.lock')
    a.close();b=InstanceLock(tmp_path/'instance.lock');b.close()

def test_bounded_diff_and_multibyte_limit(engine):
    e,p,r=engine
    d=e.diff('x',b'a\nb',b'a\nc');assert d['added_lines']==1 and d['removed_lines']==1
    assert 'No newline at end of file' in d['diff']
    d=e.diff('x',b'a\n'*21000,b'b\n'*21000);assert d['diff_truncated']
    with pytest.raises(DevError):e.preview(p,{'path':'new.txt','content':'中'*500000})


def test_blank_mcp_public_url_falls_back_to_hub_url(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from hub.app import create_app
    monkeypatch.setenv('HUB_PUBLIC_URL','http://192.0.2.20:8765')
    monkeypatch.setenv('MCP_PUBLIC_URL','')
    with TestClient(create_app(str(tmp_path/'hub'))) as client:
        response=client.get('/.well-known/oauth-protected-resource/mcp')
        assert response.status_code==200
        assert response.json()['resource']=='http://192.0.2.20:8765/mcp'
