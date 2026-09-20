"""Hub-owned admission, compact handoff and authorized timing evidence."""
from __future__ import annotations
import hashlib,hmac,json,secrets,time
from shared.util import DevError
from shared.contracts import TOOLS,MUTATING
from shared.integration_contracts import ADMIN_TOOLS

SAFE={'workspace_status','integration_control','readiness_get','browser_status','lsp_status','validations_get','validations_list',
      'computer_status','computer_session_close','browser_close','searches_get','searches_cancel','execution_info','agent_diagnostics','tasks_list'}

class HubIntegrations:
    def __init__(self,runtime):
        self.runtime=runtime;self.store=runtime.store;self.salt=secrets.token_bytes(32);self.previous={};self.active={}
        self.write_errors=0
        self.store.execute('CREATE TABLE IF NOT EXISTS integration_admission (project TEXT PRIMARY KEY,root TEXT NOT NULL,device TEXT NOT NULL,paused INTEGER NOT NULL,last_operation TEXT NOT NULL,updated REAL NOT NULL,phase TEXT NOT NULL)')
        self.store.execute('CREATE TABLE IF NOT EXISTS mcp_activity (id INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,root TEXT NOT NULL,device TEXT NOT NULL,grant_id TEXT,actor TEXT NOT NULL,window_key TEXT,tool TEXT NOT NULL,started REAL NOT NULL,service_ms INTEGER,next_call_gap_ms INTEGER,cycle_ms INTEGER,status TEXT NOT NULL,transition TEXT NOT NULL,operation_id TEXT,meaningful INTEGER NOT NULL)')
        self.store.execute('CREATE INDEX IF NOT EXISTS mcp_activity_scope ON mcp_activity(grant_id,project,id)')

    def state(self,project):
        row=self.store.one('SELECT * FROM integration_admission WHERE project=?',(project['id'],))
        current=bool(row and row['root']==project['root'] and row['device']==project['device_id'])
        return {'paused':bool(current and row['paused']),'phase':row['phase'] if current else 'ready',
                'last_operation':row['last_operation'] if current else None,'updated':row['updated'] if current else None}

    def guard(self,name,args,project,principal):
        if name in ADMIN_TOOLS and not principal.admin:raise DevError('OWNER_REQUIRED','此操作只允许面板主理人执行',403)
        if principal.admin or name in SAFE:return
        if name in MUTATING or TOOLS[name].scope in {'execute','computer'}:
            if self.state(project)['paused']:raise DevError('REMOTE_PAUSED','该项目已暂停新的 MCP 修改和执行；只读与原操作状态查询仍可用',409)

    def prepare(self,identifier,name,args,project,principal):
        if name!='integration_control' or args['action']=='status':return
        if not principal.admin:raise DevError('OWNER_REQUIRED','需要面板权限',403)
        if args.get('workspace_id') or args.get('confirm')!=project['alias']:raise DevError('CONFIRMATION_REQUIRED','请在原项目准确输入项目名称确认',409)
        # Pause immediately at Hub. A resume opens admission only after Agent ACK.
        self.store.execute('INSERT INTO integration_admission VALUES(?,?,?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET root=excluded.root,device=excluded.device,paused=excluded.paused,last_operation=excluded.last_operation,updated=excluded.updated,phase=excluded.phase',
            (project['id'],project['root'],project['device_id'],1,identifier,time.time(),'pending_'+args['action']))

    def complete(self,op,result):
        if op['tool']!='integration_control':return
        row=self.store.one('SELECT * FROM integration_admission WHERE project=?',(op['project_id'],))
        if not row or row['last_operation']!=op['id']:return
        data=result.get('data') or {};paused=True
        if result.get('ok') and type(data.get('paused')) is bool:paused=data['paused'];phase='paused' if paused else 'ready'
        else:phase='needs_review'
        self.store.execute('UPDATE integration_admission SET paused=?,phase=?,updated=? WHERE project=? AND last_operation=?',
            (int(paused),phase,time.time(),op['project_id'],op['id']))

    def handoff(self,args,principal):
        row=self.runtime.workflows.get({'workflow_id':args['workflow_id'],'before_event_id':None,'event_limit':5},principal)
        project=self.runtime.project(row['project_id'],principal)
        recent=self.runtime.list_operations({'project':project['alias'],'state':'','tool':'','idempotency_key':'','limit':40,'before_created':None},principal)['operations']
        unfinished=[];validations=[];changes=[]
        for op in recent:
            if op['created']<row['created']:continue
            if op['state'] in {'queued','running','reconnecting','cancelling','unknown','needs_review','interrupted'}:
                unfinished.append({'operation_id':op['id'],'tool':op['tool'],'state':op['state'],
                    'next':{'tool':'operations_get','arguments':{'operation_id':op['id'],'output_limit':8000}}})
            if op['tool']=='validation_run':validations.append({'operation_id':op['id'],'state':op['state'],'freshness':'not_checked'})
            if op['tool']=='show_changes':changes.append({'operation_id':op['id'],'state':op['state']})
        return {'workflow_id':row['workflow_id'],'version':row['version'],'project':project['alias'],
            'original_goal':row['goal'],'state':row['state'],'summary':row.get('summary',''),
            'completed':[{'id':s['id'],'title':s['title'],'summary':s.get('summary',''),'evidence':s.get('evidence',[])} for s in row['steps'] if s['state']=='completed'],
            'remaining':[s for s in row['steps'] if s['state'] not in {'completed','skipped'}],
            'pending_or_uncertain':unfinished,'recent_validation_operations':validations[:5],'recent_review_operations':changes[:5],
            'mapping_changed':row['mapping_changed'],'next_step':row.get('next_step'),
            'next':unfinished[0]['next'] if unfinished else {'tool':'workflows_get','arguments':{'workflow_id':row['workflow_id']}},
            'execution_started':False,'trust':'摘要只是已保存的工作记录；重新读取当前代码核实，不得重放结果不明的写入。'}

    def begin(self,principal,project,name,metadata):
        meaningful=name not in SAFE and name not in {'operations_get','operations_wait','operations_list','activity_list','diagnostics_get','operations_trace'}
        window=metadata.get('openai/session') if isinstance(metadata,dict) else None
        if not isinstance(window,str) or not 1<=len(window)<=512:window=None
        identity=json.dumps([principal.grant_id or principal.actor,project['id'],project['root'],project['device_id'],window])
        key=hmac.new(self.salt,identity.encode(),'sha256').hexdigest() if window else None
        now=time.monotonic();transition='no_host_correlation' if key is None else 'gap_unavailable'
        if meaningful and key:
            prior=self.previous.pop(key,None)
            if self.active.get(key):
                transition='overlap'
                # Both participants overlap, regardless of their completion order.
                # Neither can establish the next serial timing anchor.
                for existing in self.active[key].values():
                    existing['transition']='overlap'
                    self.store.execute("UPDATE mcp_activity SET transition='overlap' WHERE id=?",(existing['id'],))
            elif prior:
                delta=max(0,round((now-prior['ended'])*1000));cycle=max(0,round((now-prior['started'])*1000))
                self.store.execute('UPDATE mcp_activity SET next_call_gap_ms=?,cycle_ms=? WHERE id=?',(delta,cycle,prior['id']))
                transition='serial'
        row=self.store.execute('INSERT INTO mcp_activity(project,root,device,grant_id,actor,window_key,tool,started,status,transition,meaningful) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (project['id'],project['root'],project['device_id'],principal.grant_id,principal.actor,key,name,time.time(),'running',transition,int(meaningful)))
        trace={'id':row.lastrowid,'key':key,'started':now,'meaningful':meaningful,'transition':transition,'finished':False,'status':'complete','operation_id':None}
        if meaningful and key:self.active.setdefault(key,{})[trace['id']]=trace
        return trace

    def finish(self,trace,*,status=None):
        if not trace or trace['finished']:return
        trace['finished']=True;now=time.monotonic();key=trace['key'];actual=status or trace['status']
        # Release in-memory ownership even if persistence fails; diagnostics must
        # not leave a phantom in-flight call or alter execution/authorization.
        if trace['meaningful'] and key:
            active=self.active.get(key,{})
            active.pop(trace['id'],None)
            if not active:self.active.pop(key,None)
        try:
            self.store.execute('UPDATE mcp_activity SET service_ms=?,status=?,operation_id=? WHERE id=?',
                (max(0,round((now-trace['started'])*1000)),actual,trace['operation_id'],trace['id']))
            if trace['meaningful'] and key and not self.active.get(key) and actual=='complete' and trace['transition']!='overlap':
                self.previous[key]={'id':trace['id'],'started':trace['started'],'ended':now}
            if len(self.previous)>1000:self.previous.pop(next(iter(self.previous)))
            # Bounded retained metadata only; no tool arguments or outputs.
            self.store.execute('DELETE FROM mcp_activity WHERE id < (SELECT COALESCE(MAX(id),0)-10000 FROM mcp_activity)')
        except Exception:self.write_errors+=1

    def activity(self,args,principal):
        project=self.runtime.project(args['project'],principal)
        clauses=['project=?','root=?','device=?'];values=[project['id'],project['root'],project['device_id']]
        if not principal.admin:clauses.append('grant_id=?');values.append(principal.grant_id)
        if args['before_id'] is not None:clauses.append('id<?');values.append(args['before_id'])
        rows=self.store.all('SELECT id,tool,window_key,started,service_ms,next_call_gap_ms,cycle_ms,status,transition,operation_id,meaningful FROM mcp_activity WHERE '+' AND '.join(clauses)+' ORDER BY id DESC LIMIT ?',(*values,args['limit']+1))
        more=len(rows)>args['limit'];rows=rows[:args['limit']]
        return {'activities':rows,'next_before_id':rows[-1]['id'] if more and rows else None,'write_errors':self.write_errors,
            'timing_note':'service_ms 为请求进入工具服务到响应交接；next_call_gap_ms 为服务外间隔，不是模型思考时间。',
            'correlation_note':'宿主关联元数据不可信，只作相关性观察，不授予权限；重启后不连接旧时间段。'}

class CallTimingMiddleware:
    def __init__(self,app,runtime):self.app,self.runtime=app,runtime
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('path')!='/mcp':return await self.app(scope,receive,send)
        async def observed(message):
            await send(message)
            if message['type']=='http.response.body' and not message.get('more_body',False):
                self.runtime.integrations.finish(scope.get('state',{}).get('codepier_call_trace'))
        try:await self.app(scope,receive,observed)
        finally:
            trace=scope.get('state',{}).get('codepier_call_trace')
            if trace and not trace['finished']:self.runtime.integrations.finish(trace,status='interrupted')
