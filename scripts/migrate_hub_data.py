#!/usr/bin/env python3
"""Offline, checksum-verified volume copy. Source is read-only; no Docker socket."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
MARKER='.codepier-volume.json'


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def regular_tree(root):
    if root.is_symlink() or not root.is_dir():raise RuntimeError('Volume root is not a regular directory')
    result={}
    for base,dirs,files in os.walk(root,followlinks=False):
        for name in [*dirs,*files]:
            path=Path(base)/name
            if path.is_symlink() or not (path.is_file() or path.is_dir()):raise RuntimeError('Non-regular volume entry; original data was preserved')
        for name in files:
            path=Path(base)/name;result[path.relative_to(root).as_posix()]={'sha256':digest(path),'bytes':path.stat().st_size}
    return result


def copy_tree(source,destination,manifest,*,require_db=False):
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):raise RuntimeError('Destination or backup is not empty; refusing to merge or overwrite')
    for base,dirs,files in os.walk(source,followlinks=False):
        relative=Path(base).relative_to(source);target=destination/relative;target.mkdir(parents=True,exist_ok=True)
        for name in dirs:
            if (Path(base)/name).is_symlink():raise RuntimeError('Source changed during copy')
        for name in files:
            src,dst=Path(base)/name,target/name
            if src.is_symlink() or not src.is_file():raise RuntimeError('Source changed during copy')
            shutil.copy2(src,dst,follow_symlinks=False);info=src.stat();os.chown(dst,info.st_uid,info.st_gid)
            with dst.open('rb') as stream:os.fsync(stream.fileno())
        info=Path(base).stat();shutil.copystat(base,target,follow_symlinks=False);os.chown(target,info.st_uid,info.st_gid)
    if regular_tree(destination)!=manifest:raise RuntimeError('Volume SHA-256 verification failed')
    database=destination/'hub.sqlite3'
    if require_db:
        if not database.is_file() or not (destination/'master.key').is_file():raise RuntimeError('Hub database/master key is missing; original volume preserved')
        # SQLite may rewrite or remove WAL sidecars even for a read-only
        # connection. Probe an isolated copy so the verified store stays exact.
        with tempfile.TemporaryDirectory(prefix='.codepier-sqlite-check-',dir=destination) as temporary:
            probe=Path(temporary)/database.name
            for suffix in ('','-wal','-shm'):
                original=destination/(database.name+suffix)
                if original.exists():shutil.copy2(original,Path(str(probe)+suffix))
            db=sqlite3.connect(probe.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
            try:
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Hub SQLite integrity check failed')
            finally:db.close()
        if regular_tree(destination)!=manifest:raise RuntimeError('Volume changed during SQLite validation')
    for base,_,_ in os.walk(destination):
        fd=os.open(base,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)


def marker(root,value):
    path=root/MARKER
    if path.is_symlink():raise RuntimeError('Volume marker is a symlink')
    fd,name=tempfile.mkstemp(prefix=MARKER+'.',dir=root)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600);info=root.stat();os.chown(name,info.st_uid,info.st_gid);os.replace(name,path)
        fd=os.open(root,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:Path(name).unlink(missing_ok=True)


def copy_store(source,destination,backup,source_name,backup_name,*,require_db=True):
    roots=[Path(source),Path(destination),Path(backup)]
    if any(a.samefile(b) for k,a in enumerate(roots) for b in roots[k+1:]):raise RuntimeError('Source, destination and backup must be distinct volumes')
    source,destination,backup=roots
    for target in (destination,backup):
        if target.is_symlink() or any(target.iterdir()):raise RuntimeError('Destination/backup is not empty')
    manifest=regular_tree(source)
    if require_db and not {'hub.sqlite3','master.key'}<=set(manifest):raise RuntimeError('Legacy volume has no complete Hub database and master key')
    copy_tree(source,backup,manifest,require_db=require_db);copy_tree(source,destination,manifest,require_db=require_db)
    if regular_tree(source)!=manifest:raise RuntimeError('Source changed during migration; destination is not approved')
    report={'schema':1,'product':'CodePier','stage':'verified-copy','created_at':time.time(),'source_volume':source_name,
            'backup_volume':backup_name,'files':manifest,'hub_database':require_db}
    marker(destination,report);marker(backup,report)
    return {'files':len(manifest),'bytes':sum(x['bytes'] for x in manifest.values()),
            'sqlite_integrity':'ok' if require_db else 'not-applicable','backup_verified':True,'destination_verified':True}


def main():
    action=sys.argv[1] if len(sys.argv)>1 else 'copy';destination=Path('/destination')
    if action=='check':
        if (destination/MARKER).is_symlink():raise RuntimeError('Volume marker is a symlink')
        value=json.loads((destination/MARKER).read_text())
        if value.get('product')!='CodePier' or value.get('stage') not in {'initialized','verified-copy'}:raise RuntimeError('Unrecognized or incomplete CodePier data volume')
        print(json.dumps({'product':'CodePier','stage':value['stage']}));return
    if action in {'initialize','initialize-tree'}:
        if destination.is_symlink() or any(destination.iterdir()):raise RuntimeError('New volume is not empty')
        if action=='initialize':os.chown(destination,10001,10001)
        destination.chmod(0o700);marker(destination,{'schema':1,'product':'CodePier','stage':'initialized','created_at':time.time()});return
    if action not in {'copy','copy-tree'} or len(sys.argv)!=4:raise RuntimeError('Invalid migration worker arguments')
    print(json.dumps(copy_store(Path('/source'),destination,Path('/backup'),sys.argv[2],sys.argv[3],require_db=action=='copy')))


if __name__=='__main__':main()
