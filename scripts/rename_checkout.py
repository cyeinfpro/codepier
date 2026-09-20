#!/usr/bin/env python3
"""Rename a recognized legacy checkout; keep an owned compatibility alias.

Custom directory names stay put. No service, database, credential or grant is
changed. Only generated environment launchers are rewritten with preimages.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
LEGACY='remote-dev-mcp'
CANONICAL='codepier'
MARKER='.codepier-checkout-migration.json'


def sha(raw):return hashlib.sha256(raw).hexdigest()


def atomic(path,raw,mode=0o600):
    if path.is_symlink():raise RuntimeError('Refusing a symlinked checkout metadata file')
    fd,name=tempfile.mkstemp(prefix='.codepier-checkout-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,mode);os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


def save(root,record):atomic(root/MARKER,(json.dumps(record,indent=2)+'\n').encode())


def recognized(root):
    for name in ('install.sh','compose.yml','agent/__main__.py','hub/__main__.py','shared/brand_migration.py'):
        path=root/name
        if path.is_symlink() or not path.is_file():return False
    return 'CodePier' in (root/'shared/brand_migration.py').read_text(encoding='utf-8')


def alias(old,new):
    if old.is_symlink() or getattr(old,'is_junction',lambda:False)():
        if old.resolve()==new:return
        raise RuntimeError('Legacy checkout alias points elsewhere')
    if old.exists():raise RuntimeError('Legacy checkout path became occupied')
    if os.name=='nt':
        literal=lambda value:"'"+str(value).replace("'","''")+"'"
        script="$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "+literal(old)+' -Value '+literal(new)+' | Out-Null'
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16le')).decode()],check=True,timeout=30,capture_output=True)
    else:old.symlink_to(new.name,target_is_directory=True)


def recover(root):
    path=root/MARKER
    if not path.is_file():return None
    if path.is_symlink():raise RuntimeError('Checkout journal is a symlink')
    record=json.loads(path.read_text())
    if record.get('stage')=='completed':return None
    old,new=Path(record.get('old_root','')),Path(record.get('new_root',''))
    if record.get('product')!='CodePier' or new!=root or old!=new.with_name(LEGACY) or new.name!=CANONICAL or not recognized(root):raise RuntimeError('Unrecognized interrupted checkout migration')
    backup=Path(record['backup'])
    if backup.is_absolute() or '..' in backup.parts or backup.parts[:1]!=('.work',):raise RuntimeError('Unsafe checkout recovery backup')
    planned=[]
    for entry in record.get('files',[]):
        relative=Path(entry['path']);path=root/relative
        if relative.is_absolute() or '..' in relative.parts or not relative.parts[0].startswith('.venv'):raise RuntimeError('Unsafe generated-file recovery path')
        before=(root/backup/relative).read_bytes()
        if sha(before)!=entry['before'] or path.is_symlink() or not path.is_file():raise RuntimeError('Checkout preimage is invalid')
        current=path.read_bytes()
        if sha(current) not in {entry['before'],entry['after']}:raise RuntimeError('An environment launcher changed; local edit preserved')
        updated=before.decode().replace(str(old)+os.sep,str(new)+os.sep).encode()
        if sha(updated)!=entry['after']:raise RuntimeError('Checkout rewrite checksum mismatch')
        planned.append((path,updated,entry['mode']))
    alias(old,new)
    for path,raw,mode in planned:atomic(path,raw,mode)
    record['stage']='completed';record['recovered_at']=time.time();save(root,record)
    return {'changed':True,'state':'recovered','root':str(new),'legacy_alias':str(old)}


def rename(root,apply=False):
    root=Path(root).expanduser().absolute()
    if root.name==LEGACY and (root.is_symlink() or getattr(root,'is_junction',lambda:False)()):
        target=root.with_name(CANONICAL)
        if root.resolve()!=target or not recognized(target):raise RuntimeError('Unrecognized checkout alias')
        if apply:
            result=recover(target)
            if result:return result
        return {'changed':False,'state':'already-current','root':str(target),'legacy_alias':str(root)}
    if root.name==CANONICAL and apply:
        result=recover(root)
        if result:return result
    if root.name!=LEGACY:return {'changed':False,'state':'custom-or-current','root':str(root)}
    target=root.with_name(CANONICAL)
    if root.is_symlink() or root.resolve()!=root or len(root.parts)<3 or not recognized(root):raise RuntimeError('Unrecognized or linked source directory; no rename performed')
    if target.exists() or target.is_symlink():raise RuntimeError('CodePier source destination already exists; no merge performed')
    if os.name=='nt' and Path(sys.executable).resolve().is_relative_to(root):raise RuntimeError('Run checkout migration with Python outside this checkout on Windows')
    result={'changed':False,'state':'planned','root':str(target),'legacy_alias':str(root)}
    if not apply:return result
    generated=[]
    for environment in root.iterdir():
        if not (environment.name=='.venv' or environment.name.startswith('.venv-')) or environment.is_symlink() or not environment.is_dir():continue
        entries=[environment/'pyvenv.cfg']
        for directory in ('bin','Scripts'):
            folder=environment/directory
            if folder.is_dir() and not folder.is_symlink():entries.extend(folder.iterdir())
        for path in entries:
            if path.is_symlink() or not path.is_file() or path.stat().st_size>128*1024:continue
            raw=path.read_bytes()
            if b'\x00' in raw:continue
            try:text=raw.decode('utf-8')
            except UnicodeError:continue
            if path.name!='pyvenv.cfg' and not (text.startswith('#!') or path.name.lower().startswith('activate')):continue
            updated=text.replace(str(root)+os.sep,str(target)+os.sep)
            if updated!=text:generated.append((path.relative_to(root),raw,updated.encode(),path.stat().st_mode&0o777))
    backup=root/'.work'/('codepier-checkout-'+str(time.time_ns()));backup.mkdir(parents=True,mode=0o700)
    for relative,raw,_,mode in generated:
        path=backup/relative;path.parent.mkdir(parents=True,exist_ok=True);atomic(path,raw,mode)
    record={'schema':1,'product':'CodePier','old_root':str(root),'new_root':str(target),'stage':'prepared',
            'files':[{'path':str(r),'before':sha(b),'after':sha(a),'mode':m} for r,b,a,m in generated],'backup':str(backup.relative_to(root)),'at':time.time()}
    save(root,record);moved=False
    try:
        os.replace(root,target);moved=True;alias(root,target)
        for relative,raw,updated,mode in generated:
            path=target/relative
            if path.is_symlink() or path.read_bytes()!=raw:raise RuntimeError('Environment launcher changed during rename')
            atomic(path,updated,mode)
        record['stage']='completed';save(target,record)
        return {**result,'changed':True,'state':'completed','generated_files':len(generated)}
    except Exception:
        if moved:
            for relative,raw,updated,mode in generated:
                path=target/relative
                if path.is_file() and not path.is_symlink() and path.read_bytes() in (raw,updated):atomic(path,raw,mode)
            if root.is_symlink() and root.resolve()==target:root.unlink()
            elif getattr(root,'is_junction',lambda:False)() and root.resolve()==target:root.rmdir()
            if root.exists():raise RuntimeError('Original source path became occupied; new source preserved')
            os.replace(target,root)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    try:print(json.dumps(rename(args.root,args.apply),ensure_ascii=False));return 0
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:print('CodePier source rename: '+str(exc),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
