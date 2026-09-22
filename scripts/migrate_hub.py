#!/usr/bin/env python3
"""Installer-only, recoverable CodePier Compose cutover.

Original and backup stores are retained. Before the first new write a failure
restores the old containers; afterwards retries use the new store, never stale
history. No credentials are printed and no Docker socket is mounted in a worker.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

try:
    from . import migrate_hub_networks as networks
    from . import migrate_hub_proxy as proxy
except ImportError:
    import migrate_hub_networks as networks
    import migrate_hub_proxy as proxy

STATE='.codepier-hub-upgrade.json'
LEGACY_PROJECT='remote-dev-mcp'


def write_state(path,value):
    path=Path(path)
    if path.is_symlink():raise RuntimeError('Migration journal must not be a symlink')
    fd,name=tempfile.mkstemp(prefix='.codepier-upgrade-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600);os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


class Docker:
    def run(self,args,*,check=True,timeout=180):
        result=subprocess.run(['docker',*map(str,args)],text=True,capture_output=True,timeout=timeout)
        if check and result.returncode:
            raise RuntimeError('Docker '+str(args[0])+' failed (exit '+str(result.returncode)+'); inspect local Docker logs')
        return result
    def json(self,args):return json.loads(self.run(args).stdout)
    def containers(self,project):
        ids=self.run(['ps','-aq','--filter','label=com.docker.compose.project='+project]).stdout.split()
        return self.json(['inspect',*ids]) if ids else []
    def volume(self,name):
        names=self.run(['volume','ls','--format','{{.Name}}']).stdout.splitlines()
        return self.json(['volume','inspect',name])[0] if name in names else None
    def create_volume(self,name,transaction):
        if self.volume(name):raise RuntimeError('Migration volume name became occupied')
        self.run(['volume','create','--label','com.codepier.product=CodePier','--label','com.codepier.migration='+transaction,name])
    def worker(self,image,action,destination,source=None,backup=None):
        args=['run','--rm','--network=none','--user','0:0','--read-only','--cap-drop=ALL',
              '--cap-add=CHOWN','--cap-add=FOWNER','--cap-add=DAC_OVERRIDE','--security-opt=no-new-privileges',
              '--mount','type=volume,src='+destination+',dst=/destination,volume-nocopy','--entrypoint','python']
        if source:args+=['--mount','type=volume,src='+source+',dst=/source,readonly,volume-nocopy',
                         '--mount','type=volume,src='+backup+',dst=/backup,volume-nocopy']
        args+=[image,'/app/scripts/migrate_hub_data.py',action]
        if source:args+=[source,backup]
        return self.run(args,timeout=1800)


def volume_name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,199}',value):raise RuntimeError('Invalid Docker volume name')
    return value


def inspect_layout(config,legacy):
    if config.get('name')!='codepier':raise RuntimeError('Installer must use the CodePier Compose project name')
    hub=config.get('services',{}).get('hub',{})
    mounts=[x for x in hub.get('volumes',[]) if x.get('target')=='/app/data']
    if len(mounts)!=1 or mounts[0].get('type')!='volume':raise RuntimeError('Custom Hub data bind mounts are preserved; configure an explicit named volume before managed migration')
    declared=config.get('volumes',{}).get(mounts[0]['source'],{})
    if declared.get('external') is not True:raise RuntimeError('Hub volume must be external to prevent accidental empty data initialization')
    target=volume_name(declared.get('name'));image=hub.get('image')
    if not image or not isinstance(image,str):raise RuntimeError('Hub image is missing')
    old_hubs=[c for c in legacy if c.get('Config',{}).get('Labels',{}).get('com.docker.compose.service')=='hub']
    if len(old_hubs)>1:raise RuntimeError('Multiple legacy Hubs found; select the intended installation before migration')
    source=None
    if old_hubs:
        old_mounts=[x for x in old_hubs[0].get('Mounts',[]) if x.get('Destination')=='/app/data']
        if len(old_mounts)!=1 or old_mounts[0].get('Type')!='volume':raise RuntimeError('Legacy Hub has a custom data mount; nothing was stopped or moved')
        source=volume_name(old_mounts[0].get('Name'))
        if source==target:source=None
    plans=[{'destination':target,'source':source,'hub':True}]
    for key,value in config.get('volumes',{}).items():
        dest=volume_name(value.get('name'))
        if dest==target:continue
        if value.get('external') is not True:raise RuntimeError('All managed volumes must be external; preserve auxiliary service data before rename')
        sources=set()
        for service,definition in config.get('services',{}).items():
            targets={m.get('target') for m in definition.get('volumes',[]) if m.get('type')=='volume' and m.get('source')==key}
            for old in legacy:
                if old.get('Config',{}).get('Labels',{}).get('com.docker.compose.service')!=service:continue
                sources.update(volume_name(m.get('Name')) for m in old.get('Mounts',[]) if m.get('Destination') in targets and m.get('Type')=='volume')
        if len(sources)>1:raise RuntimeError('An auxiliary volume maps to multiple legacy stores')
        plans.append({'destination':dest,'source':next(iter(sources),None),'hub':False,'key':key})
    return plans,image


def save(path,state,**patch):
    state.update(patch);state['updated_at']=time.time();write_state(path,state)


def rollback(path,docker):
    path=Path(path)
    if not path.exists():return {'stage':'nothing_to_restore'}
    state=json.loads(path.read_text())
    if state.get('stage') in {'completed','rolled_back'}:return state
    if state.get('write_boundary'):
        save(path,state,stage='recovery_required',error='New Hub may have written data; both volumes retained. No stale database was restored.')
        raise RuntimeError(state['error'])
    errors=[]
    try:networks.restore(state.get('networks',[]),docker)
    except Exception as exc:
        save(path,state,stage='recovery_required',error=str(exc));raise
    for row in reversed(state.get('containers',[])):
        if not row.get('stop_requested'):continue
        try:
            docker.run(['update','--restart='+row['restart'],row['id']])
            if row['running']:docker.run(['start',row['id']])
        except Exception:errors.append(row['id'])
    for plan in state.get('volumes',[]):
        if not plan.get('create_requested'):continue
        volume=docker.volume(plan['destination'])
        if not volume:continue
        if volume.get('Labels',{}).get('com.codepier.migration')!=state.get('transaction'):
            errors.append('changed-volume-ownership');continue
        if docker.run(['ps','-aq','--filter','volume='+plan['destination']]).stdout.strip():
            errors.append('volume-in-use');continue
        try:docker.run(['volume','rm',plan['destination']])
        except Exception:errors.append('copy-cleanup-failed')
    save(path,state,stage='recovery_required' if errors else 'rolled_back',restore_errors=errors)
    if errors:raise RuntimeError('Legacy container restoration needs attention; data volumes retained')
    return state


def prepare(path,docker):
    path=Path(path);prior=json.loads(path.read_text()) if path.exists() else None
    unfinished=bool(prior and prior.get('stage') not in {'completed','rolled_back'})
    if unfinished and not prior.get('write_boundary'):raise RuntimeError('An unfinished Hub migration exists; inspect '+STATE+' and run rollback before retrying')
    config=docker.json(['compose','config','--format','json']);legacy=docker.containers(LEGACY_PROJECT)
    plans,image=inspect_layout(config,legacy);target=plans[0]['destination']
    proxy_plans = (prior.get('proxy_trust') if prior and prior.get('stage') != 'rolled_back' else None)
    if proxy_plans is None:
        proxy_plans = proxy.preflight(config, legacy, docker, LEGACY_PROJECT)
        if prior:
            save(path, prior, proxy_trust=proxy_plans)
    if proxy_plans:
        proxy.source_preflight(path.parent, proxy.trust(config))
    legacy_running=any(c.get('State',{}).get('Running') for c in legacy)
    if unfinished:
        expected={p['destination'] for p in prior.get('volumes',[])}
        if (prior.get('product')!='CodePier' or expected!={p['destination'] for p in plans}
                or prior.get('destination')!=target or legacy_running
                or not all(p.get('verified') or p.get('existing') for p in prior.get('volumes',[]))):
            raise RuntimeError('Cannot resume ambiguous Hub cutover; all data was preserved')
        for plan in prior['volumes']:
            volume=docker.volume(plan['destination'])
            if not volume or plan.get('create_requested') and volume.get('Labels',{}).get('com.codepier.migration')!=prior.get('transaction'):
                raise RuntimeError('New volume ownership changed; cutover was not resumed')
            docker.worker(image,'check',plan['destination'])
        save(path,prior,stage='prepared',image=image,resumed=True);return prior
    for plan in plans:
        source,dest=plan['source'],plan['destination']
        if not source:
            conventional=LEGACY_PROJECT+'_'+('hub-data' if plan['hub'] else plan['key'])
            if docker.volume(conventional):source=plan['source']=conventional
        existing=docker.volume(dest);plan['existing']=bool(existing)
        if existing:
            docker.worker(image,'check',dest)
            if source and (not prior or prior.get('stage')!='completed' or legacy_running):raise RuntimeError('Both legacy and CodePier data exist; no data was merged or overwritten')
        if source and not docker.volume(source):raise RuntimeError('Legacy data volume is missing')
    if all(p['existing'] for p in plans):
        if prior and prior.get('stage')=='rolled_back':save(path,prior,stage='completed',destination=target,adopted_existing=True)
        return {'stage':'current','destination':target}
    desired=set(config.get('services',{}));unexpected={c.get('Config',{}).get('Labels',{}).get('com.docker.compose.service') for c in legacy}-desired
    if unexpected:raise RuntimeError('Legacy stack has additional services; preserve the same COMPOSE_FILE selection before upgrading')
    for plan in plans:
        if not plan['source']:continue
        users=docker.run(['ps','-q','--filter','volume='+plan['source']]).stdout.split();owned={c['Id'] for c in legacy}
        if any(not any(c.startswith(x) or x.startswith(c) for c in owned) for x in users):raise RuntimeError('Legacy volume is in use by another container; migration refused')
    network_plans=networks.preflight(config,legacy,docker,LEGACY_PROJECT)
    transaction=uuid.uuid4().hex
    state={'schema':1,'product':'CodePier','transaction':transaction,'stage':'preparing','volumes':plans,'destination':target,
           'image':image,'write_boundary':False,'containers':[],'networks':network_plans,'proxy_trust':proxy_plans,'created_at':time.time()}
    for c in legacy:
        policy=c.get('HostConfig',{}).get('RestartPolicy',{});restart=policy.get('Name') or 'no'
        if restart=='on-failure' and policy.get('MaximumRetryCount'):restart+=':'+str(policy['MaximumRetryCount'])
        state['containers'].append({'id':c['Id'],'running':bool(c.get('State',{}).get('Running')),'restart':restart,'stop_requested':False})
    save(path,state)
    try:
        for row in state['containers']:
            row['stop_requested']=True;save(path,state);docker.run(['update','--restart=no',row['id']])
            if row['running']:docker.run(['stop','--time','45',row['id']],timeout=90)
        networks.retire(network_plans,docker,lambda:save(path,state))
        for plan in plans:
            if plan['existing']:continue
            source,dest=plan['source'],plan['destination']
            if source and docker.run(['ps','-q','--filter','volume='+source]).stdout.strip():raise RuntimeError('Legacy volume is still being written; original data preserved')
            plan['create_requested']=True;save(path,state);docker.create_volume(dest,transaction)
            if source:
                backup='codepier-backup-'+time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
                docker.create_volume(backup,transaction);plan['backup']=backup;save(path,state)
                docker.worker(image,'copy' if plan['hub'] else 'copy-tree',dest,source,backup)
            else:docker.worker(image,'initialize' if plan['hub'] else 'initialize-tree',dest)
            plan['verified']=True;save(path,state)
        save(path,state,stage='prepared');return state
    except BaseException:
        rollback(path,docker);raise


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['prepare','probe-volume','write-boundary','proxy-trust','commit','rollback'])
    parser.add_argument('--root',type=Path,default=Path.cwd());args=parser.parse_args();root=args.root.resolve();path=root/STATE
    os.chdir(root);os.environ['COMPOSE_PROJECT_NAME']='codepier';docker=Docker()
    try:
        if args.action=='probe-volume':
            plans,_=inspect_layout(docker.json(['compose','config','--format','json']),[])
            print(plans[0]['destination']);return 0
        if args.action=='prepare':result=prepare(path,docker)
        elif args.action=='rollback':result=rollback(path,docker)
        elif args.action=='proxy-trust':
            state=json.loads(path.read_text()) if path.exists() else {}
            result=proxy.apply(root,state.get('proxy_trust',[]),docker)
            if state and result.get('changed'):
                save(path,state,proxy_trust_update=result)
        elif not path.exists():result={'stage':'current'}
        else:
            state=json.loads(path.read_text())
            if state.get('stage')=='completed':result=state
            elif args.action=='write-boundary':
                if state.get('stage')!='prepared':raise RuntimeError('Hub data migration has not been prepared')
                save(path,state,stage='starting',write_boundary=True);result=state
            else:
                if state.get('stage')!='starting':raise RuntimeError('Hub cutover has not started')
                hubs=[c for c in docker.containers('codepier') if c.get('Config',{}).get('Labels',{}).get('com.docker.compose.service')=='hub']
                if len(hubs)!=1 or not hubs[0].get('State',{}).get('Running') or hubs[0]['State'].get('Health',{}).get('Status')!='healthy':raise RuntimeError('New CodePier Hub health was not confirmed')
                save(path,state,stage='completed',completed_at=time.time());result=state
        print(json.dumps({k:result[k] for k in ('stage','destination','backup') if k in result},ensure_ascii=False))
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print('CodePier Hub upgrade: '+str(exc),file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
