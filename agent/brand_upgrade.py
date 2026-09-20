"""Finish a verified old-version update after its legacy helper has exited.

The first CodePier release can be installed by helpers shipped before CodePier
existed. They must finish their old-service health check before we hand over to
an independent migration process. No active task or native session is stopped.
"""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from shared.brand_migration import SERVICE_NAMES, LEGACY_DIRECTORY, read_json, write_json


def needed(manager):
    if not manager.base:
        return False
    metadata=manager._metadata()
    if not metadata.get('managed') or not metadata.get('service'):
        return False
    if metadata.get('brand_migration') in {'completed','rollback','error','recovery_required'}:
        return False
    kind,scope=manager._service_kind()
    if kind not in SERVICE_NAMES:
        return False
    from shared.brand_migration import service_name
    return manager.base.name==LEGACY_DIRECTORY or service_name(manager.base,kind,scope)!=SERVICE_NAMES[kind][0]


def stage(manager):
    """Copy only the verified helper; Windows also needs Python outside the move."""
    base=manager.base
    python=manager._external_python()
    if not base or not python:
        raise RuntimeError('Independent Python is unavailable for CodePier migration')
    folder=Path(tempfile.mkdtemp(prefix='codepier-brand-'))
    folder.chmod(0o700)
    try:
        helper=folder/'agent_lifecycle.py'
        shutil.copyfile(manager.runtime_root/'scripts/agent_lifecycle.py',helper)
        if os.name=='nt' and python.resolve().is_relative_to(base.resolve()):
            managed=base/'python'
            relative=python.resolve().relative_to(managed.resolve())
            if len(relative.parts)<2 or not relative.parts[0].startswith('cpython-'):
                raise RuntimeError('Unrecognized managed Python layout; installation was not moved')
            distribution=managed/relative.parts[0]
            shutil.copytree(distribution,folder/'python',symlinks=False)
            python=folder/'python'/Path(*relative.parts[1:])
            subprocess.run([str(python),'-I','-c','import json,sqlite3,shutil,ctypes'],check=True,
                           stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        write_json(folder/'owner.json',{'pid':os.getpid(),'created_at':time.time(),'purpose':'CodePier Agent brand migration'})
        return [str(python),str(helper),'--install-dir',str(base),'--wait-pid',str(os.getpid()),'--migrate-brand']
    except BaseException:
        shutil.rmtree(folder,ignore_errors=True)
        raise


def idle(agent, handing_off=False):
    manager=agent.lifecycle
    return (not agent.jobs and not agent.processes and not agent.transfer_tasks and not agent.finishing
            and not agent.native.live() and not manager.lock.locked() and (handing_off or not manager.handoff_pending)
            and not getattr(getattr(agent, "computer", None), "session", None)
            and not (manager.base/'.install.lock').exists()
            and not (manager.plan_dir.is_dir() and any(p.suffix in {'.json','.claimed'} for p in manager.plan_dir.iterdir())))


async def watch(agent):
    manager=agent.lifecycle
    if not manager.base:
        return
    # Old lifecycle helpers verify startup after two seconds, then set ready.
    # Waiting also lets the journal/outbox recover before an optional rename.
    await asyncio.sleep(12)
    while not agent.stop_event.is_set():
        try:
            if not needed(manager): return
            if manager._metadata().get('status','ready')!='ready' or not idle(agent):
                await asyncio.sleep(5)
                continue
            # No await between the final idle check and the request gate.
            manager.handoff_pending=True
            command=await asyncio.to_thread(stage,manager)
            if not idle(agent, handing_off=True):
                manager.handoff_pending=False
                folder=Path(command[1]).parent
                if folder.name.startswith('codepier-brand-') and (folder/'owner.json').is_file():
                    owner=read_json(folder/'owner.json')
                    if owner.get('pid')==os.getpid():shutil.rmtree(folder)
                await asyncio.sleep(5)
                continue
            manager._launch_helper('brand-'+uuid.uuid4().hex,command)
            agent.stop_event.set()
            return
        except asyncio.CancelledError:
            manager.handoff_pending=False
            raise
        except Exception as exc:
            manager.handoff_pending=False
            path=manager.base/'management.json'
            metadata=read_json(path)
            metadata.update(brand_migration='error',brand_migration_error=type(exc).__name__)
            write_json(path,metadata)
            print('[CodePier] 自动命名迁移未启动；原安装继续运行。请查看管理状态并在本机重试升级。',file=sys.stderr,flush=True)
            return
