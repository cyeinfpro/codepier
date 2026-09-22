"""Real SQLite and file copies; deterministic fail-closed Docker orchestration."""
from __future__ import annotations
import copy
import json
import shutil
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest
from scripts import migrate_hub as upgrade
from scripts import migrate_hub_data as worker


def old_data(tmp_path):
    source,dest,backup=[tmp_path/n for n in ('old','new','backup')]
    for root in (source,dest,backup):root.mkdir()
    db=sqlite3.connect(source/'hub.sqlite3')
    try:
        db.execute('CREATE TABLE users(id TEXT PRIMARY KEY,name TEXT)');db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY,path TEXT)')
        db.execute('INSERT INTO users VALUES (?,?)',('original-user','Owner'));db.execute('INSERT INTO projects VALUES (?,?)',('original-project','/owner/project'));db.commit()
    finally:db.close()
    (source/'master.key').write_bytes(b'fixture-key-only'*3);(source/'uploads').mkdir();(source/'uploads/file.bin').write_bytes(bytes(range(256))*128)
    return source,dest,backup


def test_verified_copy_preserves_database_ids_keys_and_files(tmp_path):
    source,dest,backup=old_data(tmp_path);before=worker.regular_tree(source)
    result=worker.copy_store(source,dest,backup,'old-data','backup-data')
    assert result['sqlite_integrity']=='ok' and result['backup_verified'] and result['destination_verified']
    assert worker.regular_tree(source)==before
    for directory in (dest,backup):
        assert (directory/'master.key').read_bytes()==(source/'master.key').read_bytes()
        db=sqlite3.connect(directory/'hub.sqlite3')
        try:
            assert db.execute('SELECT id FROM users').fetchall()==[('original-user',)]
            assert db.execute('SELECT id FROM projects').fetchall()==[('original-project',)]
        finally:db.close()
        assert json.loads((directory/worker.MARKER).read_text())['files']==before


@pytest.mark.parametrize('include_shm',[True,False])
def test_wal_only_rows_and_complete_copy_hashes_survive_integrity_probe(tmp_path,include_shm):
    source,dest,backup=old_data(tmp_path);live=tmp_path/'live';shutil.copytree(source,live)
    db=sqlite3.connect(live/'hub.sqlite3')
    try:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal';db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('INSERT INTO users VALUES (?,?)',('wal-only-user','WAL'));db.commit()
        for file in live.iterdir():
            if file.is_file():shutil.copy2(file,source/file.name)
        if not include_shm:(source/'hub.sqlite3-shm').unlink()
        before=worker.regular_tree(source)
        assert 'hub.sqlite3-wal' in before
        assert ('hub.sqlite3-shm' in before)==include_shm
        worker.copy_store(source,dest,backup,'old-data','backup-data')
        for directory in (dest,backup):
            after=worker.regular_tree(directory);after.pop(worker.MARKER)
            assert after==before
            reader=sqlite3.connect(directory/'hub.sqlite3')
            try:assert reader.execute('SELECT COUNT(*) FROM users WHERE id=?',('wal-only-user',)).fetchone()[0]==1
            finally:reader.close()
        assert worker.regular_tree(source)==before
    finally:db.close()


@pytest.mark.parametrize('problem',['occupied','same-volume','symlink','missing-key','corrupt-database'])
def test_invalid_data_refused_without_changing_source(tmp_path,problem):
    source,dest,backup=old_data(tmp_path)
    if problem=='occupied':(dest/'owner-file').write_text('preserve')
    if problem=='symlink':(source/'linked').symlink_to(dest)
    if problem=='missing-key':(source/'master.key').unlink()
    if problem=='corrupt-database':(source/'hub.sqlite3').write_bytes(b'not a database')
    original=(source/'hub.sqlite3').read_bytes()
    with pytest.raises((RuntimeError,sqlite3.DatabaseError)):worker.copy_store(source,source if problem=='same-volume' else dest,backup,'old','backup')
    assert (source/'hub.sqlite3').read_bytes()==original and not (dest/worker.MARKER).exists()


def test_tls_volume_copy_needs_no_database(tmp_path):
    source,dest,backup=[tmp_path/n for n in ('old','new','backup')]
    for root in (source,dest,backup):root.mkdir()
    (source/'certificate.key').write_bytes(b'fixture-tls-private-key')
    result=worker.copy_store(source,dest,backup,'old-caddy','backup',require_db=False)
    assert result['sqlite_integrity']=='not-applicable' and (dest/'certificate.key').read_bytes()==(source/'certificate.key').read_bytes()


class Docker:
    def __init__(self):
        self.commands=[];self.fail=None
        self.config={'name':'codepier','services':{'hub':{'image':'codepier:1.9.0','volumes':[{'type':'volume','source':'hub-data','target':'/app/data'}]}},
                     'volumes':{'hub-data':{'external':True,'name':'codepier-hub-data'}}}
        self.old=[{'Id':'a'*64,'Config':{'Labels':{'com.docker.compose.service':'hub'}},'State':{'Running':True},
                   'HostConfig':{'RestartPolicy':{'Name':'unless-stopped'}},
                   'Mounts':[{'Type':'volume','Name':'remote-dev-mcp_hub-data','Destination':'/app/data'}]}]
        self.volumes={'remote-dev-mcp_hub-data':{'Labels':{}}};self.stopped=False
    def json(self,args):return copy.deepcopy(self.config)
    def containers(self,project):
        result=copy.deepcopy(self.old) if project=='remote-dev-mcp' else []
        if self.stopped:
            for c in result:c['State']['Running']=False
        return result
    def volume(self,name):return self.volumes.get(name)
    def create_volume(self,name,transaction):
        assert name not in self.volumes;self.volumes[name]={'Labels':{'com.codepier.migration':transaction}};self.commands.append(['create-volume',name])
    def run(self,args,**kwargs):
        self.commands.append(args)
        if args[0]=='stop':self.stopped=True
        if args[0]=='start':self.stopped=False
        if args[:2]==['volume','rm']:self.volumes.pop(args[2],None)
        out=''
        if args[:2]==['ps','-q'] and not self.stopped and args[-1]=='volume=remote-dev-mcp_hub-data':out='a'*12
        return SimpleNamespace(returncode=0,stdout=out)
    def worker(self,image,action,dest,source=None,backup=None):
        self.commands.append(['worker',action,dest])
        if action==self.fail:raise RuntimeError('injected copy failure')
        if action=='copy':assert self.stopped
        return SimpleNamespace(returncode=0,stdout='{}')


def test_old_service_stops_after_preflight_before_verified_copy(tmp_path):
    docker=Docker();path=tmp_path/upgrade.STATE;result=upgrade.prepare(path,docker)
    assert result['stage']=='prepared' and not result['write_boundary'] and result['volumes'][0]['verified']
    assert docker.commands.index(['worker','copy','codepier-hub-data'])>next(k for k,c in enumerate(docker.commands) if c[0]=='stop')
    assert 'remote-dev-mcp_hub-data' in docker.volumes


def test_copy_failure_restores_old_service_and_keeps_original_and_backup(tmp_path):
    docker=Docker();docker.fail='copy';path=tmp_path/upgrade.STATE
    with pytest.raises(RuntimeError,match='injected'):upgrade.prepare(path,docker)
    assert not docker.stopped and 'remote-dev-mcp_hub-data' in docker.volumes and 'codepier-hub-data' not in docker.volumes
    assert any(k.startswith('codepier-backup-') for k in docker.volumes)
    assert json.loads(path.read_text())['stage']=='rolled_back'
    docker.fail=None;assert upgrade.prepare(path,docker)['stage']=='prepared'


def test_new_writes_never_roll_back_to_stale_history_and_retry_uses_new_store(tmp_path):
    docker=Docker();path=tmp_path/upgrade.STATE;state=upgrade.prepare(path,docker)
    upgrade.save(path,state,stage='starting',write_boundary=True);before=len(docker.commands)
    with pytest.raises(RuntimeError,match='written data'):upgrade.rollback(path,docker)
    assert not any(c[0]=='start' for c in docker.commands[before:])
    assert {'remote-dev-mcp_hub-data','codepier-hub-data'}<=set(docker.volumes)
    assert json.loads(path.read_text())['stage']=='recovery_required'
    before=len(docker.commands);resumed=upgrade.prepare(path,docker)
    assert resumed['resumed'] and resumed['write_boundary'] and resumed['stage']=='prepared'
    assert not any(c[:2]==['worker','copy'] for c in docker.commands[before:])


@pytest.mark.parametrize('problem',['duplicate-hubs','custom-bind','not-external','wrong-project','both-data'])
def test_ambiguous_installation_refuses_before_stop(tmp_path,problem):
    docker=Docker()
    if problem=='duplicate-hubs':docker.old.append(copy.deepcopy(docker.old[0]))
    elif problem=='custom-bind':docker.old[0]['Mounts'][0]['Type']='bind'
    elif problem=='not-external':docker.config['volumes']['hub-data']['external']=False
    elif problem=='wrong-project':docker.config['name']='other'
    else:docker.volumes['codepier-hub-data']={'Labels':{}}
    with pytest.raises(RuntimeError):upgrade.prepare(tmp_path/upgrade.STATE,docker)
    assert not docker.stopped and not any(c[0]=='stop' for c in docker.commands)


def test_proxy_plan_is_durable_before_cutover_and_reused_on_resume(tmp_path,monkeypatch):
    docker=Docker();path=tmp_path/upgrade.STATE
    plan=[{'network':'default','target':'codepier_default','old_gateway':'172.20.0.1','ip_version':4}]
    (tmp_path/'.env').write_text('FORWARDED_ALLOW_IPS=127.0.0.1\n',encoding='utf-8')
    monkeypatch.setattr(upgrade.proxy,'preflight',lambda *args:copy.deepcopy(plan))
    original_worker=docker.worker
    def check_journal(*args):
        assert json.loads(path.read_text())['proxy_trust']==plan
        return original_worker(*args)
    docker.worker=check_journal
    state=upgrade.prepare(path,docker)
    upgrade.save(path,state,stage='starting',write_boundary=True)
    def retired_networks(*args):
        raise AssertionError('Resume must use the saved plan, not inspect retired networks')
    monkeypatch.setattr(upgrade.proxy,'preflight',retired_networks)
    resumed=upgrade.prepare(path,docker)
    assert resumed['resumed'] and resumed['proxy_trust']==plan
