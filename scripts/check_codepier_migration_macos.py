#!/usr/bin/env python3
"""Real launchd rename/rollback with unique temporary service names only.

Never uses, stops or rewrites the installed Agent service. The fixture process
writes a heartbeat; it has no network, model, GUI or project access code.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from shared import brand_migration as migration
from shared import brand_browser
from scripts import agent_lifecycle as helper


FIXTURE='''import json,sys,time,os
from pathlib import Path
config=Path(sys.argv[sys.argv.index('--config')+1])
value=json.loads(config.read_text());state=Path(value['state_dir']);state.mkdir(exist_ok=True)
while True:
    temp=state/'heartbeat.tmp';temp.write_text(json.dumps({'pid':os.getpid(),'cwd':str(Path.cwd()),'at':time.time()}));temp.replace(state/'heartbeat.json');time.sleep(.2)
'''


def one(failure=False):
    nonce=uuid.uuid4().hex[:16]
    old_name='com.codepier.test.old.'+nonce;new_name='com.codepier.test.new.'+nonce
    original_home=next(cls.__dict__['home'] for cls in Path.__mro__ if 'home' in cls.__dict__);original_names=migration.SERVICE_NAMES['launchd'];original_resolver=helper._service_name;original_run=helper._run
    with tempfile.TemporaryDirectory(prefix='codepier-launchd-') as directory:
        home=Path(directory).resolve();Path.home=classmethod(lambda cls:home)
        migration.SERVICE_NAMES['launchd']=(new_name,old_name)
        helper._service_name=lambda base:migration.service_name(base,'launchd','user')
        def guarded(command, **kwargs):
            parts=list(map(str,command))
            if parts[0]!='launchctl':raise AssertionError('Only the unique launchd fixture may be used')
            if parts[1]!='print':
                if not any(label in parts[-1] for label in (old_name,new_name)):raise AssertionError('Refusing a non-fixture launchd mutation')
                if parts[1]=='bootstrap' and not Path(parts[-1]).is_relative_to(home):raise AssertionError('Fixture plist escaped temporary home')
            return original_run(command,**kwargs)
        helper._run=guarded
        base=home/'.remote-dev-agent';runtime=base/'runtime';(runtime/'agent').mkdir(parents=True)
        (runtime/'agent/__main__.py').write_text(FIXTURE);(runtime/'agent/__init__.py').write_text('')
        python=runtime/'.venv/bin/python';python.parent.mkdir(parents=True);python.symlink_to(Path(sys.executable).resolve())
        config={'device_id':'fixture-'+nonce,'secret':'fixture-only-'+'s'*48,'hub_url':'http://127.0.0.1:9',
                'allowed_roots':[],'tasks':{},'state_dir':str(base/'state')}
        (base/'config.json').write_text(json.dumps(config,indent=3));before=(base/'config.json').read_bytes()
        (base/'management.json').write_text(json.dumps({'managed':True,'service':True,'service_kind':'launchd','service_scope':'user',
                                                      'service_name':old_name,'status':'ready'}))
        old_target=migration.service_target(base,'launchd','user',old_name);old_target.parent.mkdir(parents=True)
        command=[str(python),'-m','agent','--config',str(base/'config.json'),'run']
        old_target.write_bytes(plistlib.dumps({'Label':old_name,'ProgramArguments':command,'WorkingDirectory':str(runtime),
                                              'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':1}))
        backend=SimpleNamespace(**{name:getattr(helper,name) for name in ('_run','_service','_service_name','_service_target','_systemctl',
                  '_process_alive','_move','_wait_for_exit','stop_service','start_service','verify_service','refresh_cli_commands')},brand_browser=brand_browser)
        injected=[failure]
        def verify(path):
            helper.verify_service(path)
            if injected[0] and path.name=='.codepier-agent':
                injected[0]=False;raise RuntimeError('injected new-service verification failure')
        backend.verify_service=verify
        try:
            helper.start_service(base);helper.verify_service(base)
            old_pid=json.loads((base/'state/heartbeat.json').read_text())['pid']
            if failure:
                try:migration.migrate_agent(base,old_pid,backend)
                except RuntimeError as exc:
                    if 'injected' not in str(exc):raise
                else:raise AssertionError('Injected failure did not abort the migration')
                assert base.is_dir() and not base.is_symlink() and (base/'config.json').read_bytes()==before
                assert json.loads((base/migration.JOURNAL).read_text())['stage']=='rolled_back'
                helper.verify_service(base)
                return {'case':'new-service-failure','result':'passed','real_service_restored':True,'config_byte_identical':True}
            result=migration.migrate_agent(base,old_pid,backend);new=home/'.codepier-agent'
            assert result['status']=='completed' and base.is_symlink() and base.resolve()==new
            assert not old_target.exists()
            helper.verify_service(new)
            current=json.loads((new/'state/heartbeat.json').read_text())
            assert current['pid']!=old_pid and current['cwd']==str(new/'runtime')
            assert json.loads((new/'config.json').read_text())=={**config,'state_dir':str(new/'state')}
            assert migration.migrate_agent(new,0,backend)['status']=='already_current'
            return {'case':'legacy-to-codepier','result':'passed','real_new_service_running':True,'old_service_removed':True,
                    'canonical_working_directory':True,'identity_and_permissions_preserved':True,'idempotent':True}
        finally:
            for label in (old_name,new_name):helper._run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'])
            Path.home=original_home;migration.SERVICE_NAMES['launchd']=original_names;helper._service_name=original_resolver;helper._run=original_run


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if sys.platform!='darwin':raise SystemExit('This isolated real-service check requires macOS')
    helper._run(['launchctl','print',f'gui/{os.getuid()}'],check=True)
    results=[one(False),one(True)]
    report={'platform':'macOS','scope':'Real launchd; unique temporary names; production service untouched',
            'paid_models_called':False,'cases':results,'passed':len(results),'failed':0}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
