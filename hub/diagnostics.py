"""Scoped build diagnostics and a durable, bounded operation timeline."""
from __future__ import annotations
import json
import sqlite3
import time
from shared.build_info import BuildIdentity
from shared.contracts import TOOLS
from shared.util import DevError
from shared.computer_diagnostics import COMPUTER_STAGES, safe_detail

LABELS = {'hub_received':'Hub 已接收', 'dispatched':'已投递设备', 'accepted':'Agent 已接收',
          'waiting_project':'等待项目读写锁', 'waiting_worker':'等待本机执行槽', 'executing':'本机正在执行',
          'persisting':'结果正在落盘', 'result_ready':'结果已在本机保存', 'hub_completed':'Hub 已保存最终结果'}
LABELS.update(COMPUTER_STAGES)
AGENT_STAGES = set(LABELS)-{'hub_received','dispatched','hub_completed'}

class Diagnostics:
    def __init__(self, runtime):
        self.runtime = runtime
        self.build = BuildIdentity('hub')
        self.errors = 0

    def record(self, identifier, stage, *, source='hub', seq=None, elapsed_ms=None, detail=None):
        store = self.runtime.store
        try:
            with store.lock, store.db:
                if seq is None:
                    seq = store.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM operation_events WHERE operation_id=? AND source=?',(identifier,source)).fetchone()[0]
                store.db.execute('INSERT OR IGNORE INTO operation_events(operation_id,source,seq,stage,at,elapsed_ms,detail) VALUES (?,?,?,?,?,?,?)',
                                 (identifier,source,seq,stage,time.time(),elapsed_ms,json.dumps(detail or {})))
                store.db.execute('DELETE FROM operation_events WHERE operation_id=? AND id NOT IN (SELECT id FROM operation_events WHERE operation_id=? ORDER BY id DESC LIMIT 200)',(identifier,identifier))
        except (sqlite3.Error,OSError):
            self.errors += 1

    def ingest(self, identifier, trace):
        if not isinstance(trace,list) or len(trace)>32:
            return
        for event in trace:
            if not isinstance(event,dict) or not isinstance(event.get('stage'),str) or event['stage'] not in AGENT_STAGES:
                continue
            seq,elapsed=event.get('seq'),event.get('elapsed_ms')
            if type(seq) is not int or not 0<seq<2**53 or type(elapsed) is not int or not 0<=elapsed<=7*86400000:
                continue
            detail=event.get('detail');detail=detail if isinstance(detail,dict) else {}
            blocked=detail.get('blocked_by',[])
            blocked=[x for x in blocked[:64] if isinstance(x,str) and 1<=len(x)<=100] if isinstance(blocked,list) else []
            self.record(identifier,event['stage'],source='agent',seq=seq,elapsed_ms=elapsed,detail={'blocked_by':blocked, **safe_detail(detail)})

    def trace(self,args,principal):
        runtime=self.runtime
        op=runtime.operation(args['operation_id'],principal,{'include_output':False,'include_result':False})
        rows=runtime.store.all('SELECT * FROM operation_events WHERE operation_id=? AND id>? ORDER BY id LIMIT ?',
                               (op['id'],args['after_event_id'],args['limit']+1))
        more=len(rows)>args['limit'];rows=rows[:args['limit']]
        for row in rows:
            detail=json.loads(row.pop('detail'));visible=[]
            for identifier in detail.get('blocked_by',[]):
                try:
                    block=runtime.operation(identifier,principal,{'include_output':False,'include_result':False})
                    if block['device_id']==op['device_id'] and block['pending']:
                        visible.append({'operation_id':identifier,'tool':block['tool'],'state':block['state']})
                except DevError:
                    pass
            row['blocked_by']=visible;row['label']=LABELS.get(row['stage'],row['stage'])
            row['detail']=safe_detail(detail)
        last=runtime.store.one("SELECT stage,at FROM operation_events WHERE operation_id=? AND source='agent' ORDER BY seq DESC LIMIT 1",(op['id'],))
        online=runtime.online(op['device_id']) if op['device_id'] else False
        if not op['pending']: reason='操作已结束；以最终状态和退出码为准'
        elif op['cancel_requested']: reason='取消请求已保存，等待本机确认'
        elif not online: reason='等待设备连接；已开始的本机任务不因此自动停止'
        elif last: reason=LABELS[last['stage']]
        elif op['accepted_at']: reason='设备已接收，但当前 Agent 未提供阶段明细'
        else: reason='Hub 排队或等待设备接收确认'
        return {'operation_id':op['id'],'current':{'state':op['state'],'reason':reason,'online':online,
                    'last_agent_stage':last['stage'] if last else None,'stage_observed_at':last['at'] if last else None,
                    'transport_error':op['transport_error'],'attempts':op['attempts'],'elapsed_ms':max(0,round(((time.time() if op['pending'] else op['updated'])-op['created'])*1000))},
                'events':rows,'next_after_event_id':rows[-1]['id'] if more and rows else None,'diagnostic_write_errors':self.errors,
                'clock_note':'at is Hub observation time; agent elapsed_ms is relative to a handling attempt, not a synchronized wall clock.',
                'history_limit':200,'history_may_be_pruned':True}

    def get(self,args,principal):
        runtime=self.runtime
        projects=[runtime.project(args['project'],principal)] if args['project'] else runtime.list_projects(principal)
        identifiers={p['device_id'] for p in projects};devices=[];warnings=[]
        hub=self.build.describe()
        if hub['restart_required']: warnings.append('Hub 磁盘代码与运行中版本不同；按升级流程重启后才生效。')
        expected={name for name,tool in TOOLS.items() if not tool.local}
        for identifier in sorted(identifiers):
            row=runtime.store.one('SELECT id,name,enabled,info,last_seen FROM devices WHERE id=?',(identifier,))
            if not row: continue
            info=json.loads(row.pop('info'));capabilities=info.get('capabilities');known=isinstance(capabilities,list)
            missing=sorted(expected-set(capabilities)) if known else []
            build=info.get('build');build=build if isinstance(build,dict) else None
            mismatch=info.get('version')!=hub['runtime']['version']
            devices.append({**row,'enabled':bool(row['enabled']),'online':runtime.online(identifier),'reported_version':info.get('version'),
                            'build':build,'capability_information_available':known,'missing_tools':missing,'version_mismatch':mismatch,
                            'last_seen_age_seconds':max(0,round(time.time()-row['last_seen'])) if row['last_seen'] else None})
            if mismatch: warnings.append(f"{row['name']} 的 Agent 与 Hub 版本不同；此检查不会自动重启。")
            if build and build.get('restart_required'): warnings.append(f"{row['name']} 的磁盘代码已改变，运行进程尚未更新。")
            if not known: warnings.append(f"{row['name']} 未报告工具能力，缺失情况未知。")
        actual=args['client_catalog_sha256']
        return {'hub':hub,'devices':devices,'warnings':warnings,
                'client_catalog':{'reported':actual or None,'matches':actual==hub['catalog_sha256'] if actual else None,
                                  'note':'No client hash means unknown; this endpoint cannot inspect the ChatGPT UI cache.'},
                'tool_count':len(TOOLS),'schema':runtime.store.one("SELECT value FROM meta WHERE key='schema'")['value'],'diagnostic_write_errors':self.errors}
