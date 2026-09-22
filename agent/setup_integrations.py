#!/usr/bin/env python3
"""Explicit local-owner configuration. Preview by default; no service restart.

Only the integrations section is changed. Authentication, project roots, shell,
CLI configuration and all unrelated fields are preserved.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from agent.config import validate_config
from agent.integration_config import validate_integrations
from shared.instance_lock import InstanceLock
from shared.util import atomic_json, fsync_directory, safe_summary


def read_config(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024:
        raise ValueError('配置必须是现有普通文件，不能是链接')
    raw=path.read_bytes();value=json.loads(raw);validate_config(value,path)
    return raw,value


def write_config(path,before,value):
    path=Path(path);validate_config(value,path)
    if path.is_symlink() or path.read_bytes()!=before:raise ValueError('配置已变化，未覆盖其他修改')
    backup=path.parent/'integration-backups'
    if backup.is_symlink():raise ValueError('备份目录不能是链接')
    backup.mkdir(mode=0o700,exist_ok=True);os.chmod(backup,0o700)
    target=backup/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex+'.json')
    with target.open('xb') as file:file.write(before);file.flush();os.fsync(file.fileno())
    os.chmod(target,0o600);fsync_directory(backup)
    if path.read_bytes()!=before:raise ValueError('配置在备份期间变化，原文件未覆盖')
    atomic_json(path,value)
    return str(target)


def configure(path,fragment,apply=False,replace_projects=False):
    path=Path(path).expanduser().absolute();before,value=read_config(path)
    if not isinstance(fragment,dict):raise ValueError('配置片段必须是 JSON 对象')
    def merge(previous,patch):
        combined=copy.deepcopy(previous)
        for key,item in patch.items():
            if key == 'projects' and isinstance(item,list) and isinstance(combined.get(key),list) and not replace_projects:
                combined[key] = list(dict.fromkeys([*combined[key],*item]))
                continue
            combined[key]=merge(combined.get(key,{}),item) if isinstance(item,dict) and isinstance(combined.get(key,{}),dict) else copy.deepcopy(item)
        return combined
    normalized=validate_integrations(merge(value.get('integrations',{}),fragment))
    result={'changed':False,'service_restarted':False,'planned_integrations':safe_summary(normalized),
            'before_sha256':hashlib.sha256(before).hexdigest(),
            'project_merge':'replace explicitly' if replace_projects else 'additive; existing project grants retained',
            'activation':'文件已保存不等于运行程序已更新；本机控制与浏览器监听的启用需要正常重启 Agent。'}
    if apply:
        with InstanceLock(path.parent/'.integration-setup.lock'):
            result.update(changed=True,backup=write_config(path,before,{**value,'integrations':normalized}))
    return result


def restore(path,backup,expected_sha256,apply=False):
    path=Path(path).expanduser().absolute();raw,current=read_config(path);_,saved=read_config(Path(backup))
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('当前配置 SHA 不匹配，未覆盖后续修改')
    result={'changed':False,'service_restarted':False,'restores':'integrations only'}
    updated=copy.deepcopy(current)
    if 'integrations' in saved:updated['integrations']=saved['integrations']
    else:updated.pop('integrations',None)
    if apply:
        with InstanceLock(path.parent/'.integration-setup.lock'):
            result.update(changed=True,backup=write_config(path,raw,updated))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    commands=parser.add_subparsers(dest='action',required=True);commands.add_parser('status')
    config=commands.add_parser('configure');config.add_argument('--settings',required=True);config.add_argument('--apply',action='store_true');config.add_argument('--replace-projects',action='store_true',help='Explicitly replace project allowlists instead of extending them')
    recovery=commands.add_parser('restore');recovery.add_argument('--backup',required=True);recovery.add_argument('--expected-sha256',required=True);recovery.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:
        if args.action=='status':
            raw,value=read_config(Path(args.config).expanduser().absolute())
            result={'changed':False,'sha256':hashlib.sha256(raw).hexdigest(),'integrations':safe_summary(validate_integrations(value.get('integrations',{})))}
        elif args.action=='configure':
            path=Path(args.settings).expanduser()
            if path.stat().st_size>1024*1024:raise ValueError('配置片段超过大小限制')
            result=configure(args.config,json.loads(path.read_text(encoding='utf-8')),args.apply,args.replace_projects)
        else:result=restore(args.config,args.backup,args.expected_sha256,args.apply)
    except (ValueError,OSError,RuntimeError) as exc:
        print(json.dumps({'error':str(exc),'service_restarted':False},ensure_ascii=False),file=sys.stderr);return 1
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
