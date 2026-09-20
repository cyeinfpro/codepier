"""Project-scoped admission and cancellation with explicit process ownership."""
from __future__ import annotations
import asyncio,json,time,uuid
from pathlib import Path
from shared.util import DevError
from shared.contracts import TOOLS, MUTATING

SAFE_WHILE_PAUSED={'integration_control','readiness_get','browser_status','lsp_status','validations_get','validations_list',
                   'computer_status','computer_session_close','browser_close','searches_cancel','searches_get','execution_info','agent_diagnostics','tasks_list'}

class Controls:
    def __init__(self,agent):
        self.agent=agent
        j=agent.journal
        with j.lock,j.db:
            j.db.execute('CREATE TABLE IF NOT EXISTS integration_admission (project TEXT PRIMARY KEY,root TEXT NOT NULL,paused INTEGER NOT NULL,updated REAL NOT NULL)')
        self.lock=asyncio.Lock()

    def state(self,project):
        root=project.get('_original_root',project.get('root'))
        if not isinstance(root,str) or not root:
            raise DevError('INVALID_REQUEST','接入检查需要已映射的项目目录')
        identifier=project.get('id')
        with self.agent.journal.lock:
            if isinstance(identifier,str) and identifier:
                row=self.agent.journal.db.execute('SELECT * FROM integration_admission WHERE project=?',(identifier,)).fetchone()
            else:
                # Legacy authenticated frames allowed only root/alias. Preserve
                # their file/cancellation behavior, without bypassing a paused
                # mapping to that same directory when its ID is unavailable.
                row=self.agent.journal.db.execute('SELECT * FROM integration_admission WHERE root=? ORDER BY paused DESC,updated DESC LIMIT 1',(root,)).fetchone()
        return {'paused':bool(row and row['root']==root and row['paused']),
                'scope':'project','updated':row['updated'] if row and row['root']==root else None,
                'connection_preserved':True,'native_sessions_automatically_stopped':False}

    def guard(self,name,project):
        # Older frames without identity cannot use a missing flag as an exemption.
        metadata=project.get('_execution_policy') or {}
        panel=metadata.get('origin')=='panel' and project.get('_integration_admin') is True
        if panel or name in SAFE_WHILE_PAUSED:return
        if name in TOOLS and (name in MUTATING or TOOLS[name].scope in {'execute','computer'}) and self.state(project)['paused']:
            raise DevError('REMOTE_PAUSED','该项目已暂停新的 MCP 修改和执行；只读检查、原操作回执及管理面板仍可用',409)

    def set(self,project,paused):
        root=project.get('_original_root',project['root'])
        with self.agent.journal.lock,self.agent.journal.db:
            self.agent.journal.db.execute('INSERT INTO integration_admission VALUES(?,?,?,?) ON CONFLICT(project) DO UPDATE SET root=excluded.root,paused=excluded.paused,updated=excluded.updated',
                (project['id'],root,int(paused),time.time()))

    async def action(self,identifier,project,args):
        if not project.get('_integration_admin'):raise DevError('OWNER_REQUIRED','只有面板或本机主理人可操作接入控制',403)
        if args.get('workspace_id'):raise DevError('PROJECT_CONTROL_SCOPE','接入控制作用于整个项目，请切回原项目后操作')
        action=args['action']
        if action=='status':return self.state(project)
        if args['confirm']!=project.get('alias'):raise DevError('CONFIRMATION_REQUIRED','请准确输入项目名称确认操作',409)
        async with self.lock:
            if action in {'pause','resume'}:
                self.set(project,action=='pause')
                return {**self.state(project),'action':action}
            self.set(project,True)
            candidates={i:p for i,p in self.agent.integration_projects.items() if i!=identifier and p.get('id')==project['id']
                        and p.get('_original_root',p['root'])==project.get('_original_root',project['root'])}
            requested=[];not_cancellable=[]
            for op_id in candidates:
                state=self.agent.journal.status(op_id)
                tool=self.agent.integration_tool_names.get(op_id,'')
                # Do not cancel a thread after a filesystem mutation started.
                if op_id in self.agent.processes or state['status']=='accepted':
                    self.agent.journal.cancel(op_id);self.agent.cancel_call(op_id);requested.append(op_id)
                elif state['status']=='running':not_cancellable.append({'operation_id':op_id,'tool':tool,'reason':'文件或输入操作已开始，保留原回执核查'})
            native=[]
            if args.get('include_native'):
                for row in self.agent.native.live():
                    if row['project_id']!=project['id']:continue
                    current={**project,'root':row['root'],'device_id':row['device_id']}
                    receipt=uuid.uuid4().hex
                    try:
                        writer=uuid.uuid4().hex
                        if row.get('mode')!='chat':
                            await asyncio.to_thread(self.agent.native.action,'lease',current,{'id':row['id'],'writer':writer})
                        result=await asyncio.to_thread(self.agent.native.action,'stop',current,{'id':row['id'],'receipt':receipt,'writer':writer})
                        native.append({'session_id':row['id'],'receipt':receipt,'state':result['state']})
                    except (DevError,ValueError) as exc:native.append({'session_id':row['id'],'state':'unconfirmed','reason':getattr(exc,'code','NATIVE_STOP_UNCONFIRMED')})
                self.agent.native.sync_wake.set()
            browser=await self.agent.integrations.browser.release_project(project)
            deadline=time.monotonic()+5
            while any(i in self.agent.jobs or i in self.agent.processes for i in requested) and time.monotonic()<deadline:
                await asyncio.sleep(.05)
            confirmed=[i for i in requested if i not in self.agent.jobs and i not in self.agent.processes]
            for row in native:
                active=next((r for r in self.agent.native.live() if r['id']==row['session_id']),None)
                row['stopped_verified']=active is None
                if active:row['state']=active['status']
            return {**self.state(project),'action':'stop','cancel_requested':requested,'stopped_verified':confirmed,
                    'unconfirmed':[i for i in requested if i not in confirmed],'non_cancellable':not_cancellable,'native':native,'browser':browser,
                    'complete':not not_cancellable and len(confirmed)==len(requested) and all(n['stopped_verified'] for n in native) and all(b['tab_cleanup_confirmed'] for b in browser),
                    'note':'停止仅针对本服务记录归属的项目任务；暂停保持生效，恢复接入是独立操作。'}
