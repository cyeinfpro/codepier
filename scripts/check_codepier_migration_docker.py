#!/usr/bin/env python3
"""Real Docker data/HTTPS-network migration with uniquely named fixture stores.

No production container, original volume, Compose namespace or network is used.
The image must be a source build supplied explicitly. All fixture names include
one random nonce; cleanup never prunes resources or touches a non-fixture name.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.migrate_hub import Docker
from scripts import migrate_hub_networks as networks


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--image',required=True);parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    docker=Docker();prefix='codepier-fixture-'+uuid.uuid4().hex[:12];volumes=[prefix+'-'+n for n in ('old','new','backup')]
    old,new,backup=volumes;hub_volume=prefix+'-hub-store';volumes.append(hub_volume);hub_container=prefix+'-hub';net=prefix+'-proxy';container=prefix+'-heartbeat';report={'scope':'Unique temporary Docker fixtures; no production services or data','image':args.image,'cases':[]}
    def cleanup():
        for command in (['rm','-f','-v',container],['rm','-f','-v',hub_container],['network','rm',net],*[['volume','rm',v] for v in volumes]):
            if not command[-1].startswith(prefix):raise AssertionError('Non-fixture cleanup is forbidden')
            docker.run(command,check=False)
    try:
        for volume in volumes:docker.create_volume(volume,prefix)
        seed="import sqlite3,pathlib,os; p=pathlib.Path('/fixture'); s=sqlite3.connect(p/'hub.sqlite3'); s.execute('CREATE TABLE users(id TEXT PRIMARY KEY)'); s.execute(\"INSERT INTO users VALUES ('original-user')\"); s.commit(); s.close(); (p/'master.key').write_text('fixture-only-not-a-real-key'); (p/'attachment.bin').write_bytes(bytes(range(256))*16); [os.chown(q,10001,10001) for q in p.iterdir()]; os.chown(p,10001,10001)"
        docker.run(['run','--rm','--network=none','--user','0:0','--mount','type=volume,src='+old+',dst=/fixture','--entrypoint','python',args.image,'-c',seed])
        copied=docker.worker(args.image,'copy',new,old,backup);result=json.loads(copied.stdout)
        assert result['backup_verified'] and result['destination_verified'] and result['sqlite_integrity']=='ok'
        for volume in (old,new,backup):
            check="import sqlite3,pathlib,json; p=pathlib.Path('/fixture'); s=sqlite3.connect((p/'hub.sqlite3').as_uri()+'?mode=ro',uri=True); assert s.execute('SELECT id FROM users').fetchall()==[('original-user',)]; s.close(); assert (p/'master.key').read_text()=='fixture-only-not-a-real-key'; print(json.dumps({'uid':p.stat().st_uid,'verified':True}))"
            response=docker.run(['run','--rm','--network=none','--read-only','--user','10001:10001','--mount','type=volume,src='+volume+',dst=/fixture,readonly','--entrypoint','python',args.image,'-c',check])
            assert json.loads(response.stdout)['verified']
        report['cases'].append({'case':'read-only-old-volume-to-canonical-and-backup','result':'passed',**result,'service_uid_can_read':True})
        try:docker.worker(args.image,'copy',new,old,backup)
        except RuntimeError:report['cases'].append({'case':'occupied-destination-refused','result':'passed'})
        else:raise AssertionError('A repeated copy merged an occupied destination')
        docker.run(['network','create','--label','com.docker.compose.project='+prefix,'--label','com.docker.compose.network=proxy',net])
        network=docker.json(['network','inspect',net])[0];subnet=network['IPAM']['Config'][0]['Subnet']
        docker.run(['run','-d','--name',container,'--label','com.codepier.fixture='+prefix,'--network',net,'--entrypoint','python',args.image,'-c','import time;time.sleep(300)'])
        original=docker.json(['inspect',container])[0];old_ip=original['NetworkSettings']['Networks'][net]['IPAddress']
        config={'networks':{'proxy':{'ipam':{'config':[{'subnet':subnet}]}}}}
        plan=networks.preflight(config,[original],docker,prefix);assert len(plan)==1
        docker.run(['stop','--time','2',container]);journal=[]
        networks.retire(plan,docker,lambda:journal.append(json.loads(json.dumps(plan))))
        assert net not in docker.run(['network','ls','--format','{{.Name}}']).stdout.splitlines()
        # Inject a cutover failure by restoring the recorded original network.
        networks.restore(plan,docker);docker.run(['start',container])
        restored=docker.json(['inspect',container])[0]
        assert restored['State']['Running'] and restored['NetworkSettings']['Networks'][net]['IPAddress']==old_ip
        report['cases'].append({'case':'fixed-subnet-bridge-retire-and-rollback','result':'passed','original_ip_preserved':True,'container_restarted':True,'journal_transitions':len(journal)})
        docker.worker(args.image,'initialize',hub_volume)
        docker.run(['run','--rm','--network=none','--mount','type=volume,src='+hub_volume+',dst=/app/data',
                    '-e','CODEPIER_ADMIN_PASSWORD=fixture-only-installation-password','--entrypoint','python',args.image,'-m','hub','init','--username','fixture'])
        docker.run(['run','-d','--name',hub_container,'--network=none','--label','com.codepier.fixture='+prefix,
                    '--health-interval=1s','--health-start-period=0s','--health-retries=20','--mount','type=volume,src='+hub_volume+',dst=/app/data',args.image])
        for _ in range(50):
            state=docker.json(['inspect',hub_container])[0]['State']
            if state.get('Health',{}).get('Status')=='healthy':break
            if not state.get('Running'):raise AssertionError('Fixture Hub exited during startup')
            time.sleep(.3)
        else:raise AssertionError('Fixture Hub did not become healthy')
        check="import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8765/healthz')); m=json.load(urllib.request.urlopen('http://127.0.0.1:8765/agent/manifest.json')); assert h['version']=='1.9.0' and m['version']=='1.9.0'; print(json.dumps({'version':h['version'],'agent_package_bytes':m['bytes']}))"
        healthy=json.loads(docker.run(['exec',hub_container,'python','-c',check]).stdout)
        report['cases'].append({'case':'canonical-volume-hub-startup-and-agent-package','result':'passed',**healthy})
        report.update(passed=len(report['cases']),failed=0,docker_version=docker.run(['version','--format','{{.Server.Version}}']).stdout.strip())
    finally:cleanup()
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
