"""Integration dispatch on the existing Agent, journal, project locks and policies."""
from __future__ import annotations
import asyncio
from shared.integration_contracts import REMOTE_TOOLS
from shared.util import DevError
from agent.integration_state import Records
from agent.source_versions import Validations
from agent.workspaces import Workspaces
from agent.integration_control import Controls
from agent.incoming_artifacts import import_artifact
from shared.file_sources import FILE_SOURCE_POLICY_VERSION, DEFAULT_MAX_IMPORT_BYTES, file_source_hosts
from agent import lsp_navigation

class Integrations:
    def __init__(self,agent):
        from agent.background_browser import BrowserBroker
        self.agent=agent;self.records=Records(agent.journal)
        self.workspaces=Workspaces(agent,self.records);self.validations=Validations(agent,self.records)
        self.control=Controls(agent);self.browser=BrowserBroker(agent,self.records)
        self.local_server=None
        self.agent.journal.db.execute('CREATE TABLE IF NOT EXISTS integration_project_catalog (id TEXT PRIMARY KEY,body TEXT NOT NULL,updated REAL NOT NULL)')

    def remember_project(self,project):
        import json,time
        if not project.get('id') or not project.get('root'):return
        fields={k:project[k] for k in ('id','alias','root','mode','allow_tasks','device_id') if k in project}
        with self.agent.journal.lock,self.agent.journal.db:
            self.agent.journal.db.execute('INSERT INTO integration_project_catalog VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,updated=excluded.updated',(project['id'],json.dumps(fields),time.time()))

    def known_projects(self):
        import json
        with self.agent.journal.lock:
            rows=self.agent.journal.db.execute('SELECT body FROM integration_project_catalog ORDER BY updated DESC LIMIT 200').fetchall()
        result=[]
        for row in rows:
            p=json.loads(row['body'])
            try:self.agent.engine.root(p)
            except DevError:continue
            result.append(p)
        return result

    def project(self,project,args):
        workspace_id=args.get('workspace_id','')
        return self.workspaces.resolve(project,workspace_id) if workspace_id else project

    async def start(self):
        if self.agent.config.get('integrations',{}).get('local_control') or self.agent.config.get('integrations',{}).get('browser',{}).get('enabled'):
            from agent.integration_local import LocalServer
            self.local_server=LocalServer(self.agent,self.browser)
            await self.local_server.start()

    async def close(self):
        await self.browser.close()
        if self.local_server:await self.local_server.close()

    async def threaded(self,fn,*args):
        work=asyncio.create_task(asyncio.to_thread(fn,*args))
        try:return await asyncio.shield(work)
        except asyncio.CancelledError:return await work

    async def execute(self,identifier,name,project,args):
        if name=='download_artifact':return await self.threaded(import_artifact,self.agent.engine,project,args)
        if name=='lsp_status':return lsp_navigation.status(self.agent.config,project)
        if name=='lsp_query':return await lsp_navigation.query(self.agent,identifier,project,args)
        if name=='worktrees_create':return await self.workspaces.create(identifier,project,args)
        if name=='worktrees_list':return await self.workspaces.list(identifier,project)
        if name=='worktrees_remove':return await self.workspaces.remove(identifier,project,args)
        if name=='validation_run':return await self.validations.run(identifier,project,args)
        if name=='validations_get':return await self.threaded(self.validations.get,project,args['validation_id'])
        if name=='validations_list':return self.validations.list(project)
        if name=='validations_accept':return await self.threaded(self.validations.accept,project,args)
        if name=='integration_control':return await self.control.action(identifier,project,args)
        if name.startswith('browser_'):return await self.browser.execute(identifier,name,project,args)
        if name=='readiness_get':
            from agent.shell import execution_info
            root,spec=self.agent.engine.root(project)
            execution=execution_info(self.agent.config,project,spec,root)
            browser=self.browser.status(project);languages=lsp_navigation.status(self.agent.config,project)
            admission=self.control.state(project)
            scopes=project.get('_coding_scopes')
            known=isinstance(scopes,list) and all(isinstance(scope,str) for scope in scopes)
            admin=project.get('_integration_admin') is True
            paused=admission['paused'] and not admin
            def permitted(scope):
                return True if admin else scope in scopes if known else None
            def available(scope,configured=True,unavailable='disabled'):
                permission=permitted(scope)
                if permission is False:return 'denied'
                if not configured:return unavailable
                if permission is None:return 'unknown'
                return 'paused' if paused else 'ready'
            shell_state=available('execute',execution['shell']['enabled'])
            if shell_state=='ready' and not execution['shell']['executable_available']:shell_state='missing'
            execution['shell']['caller_permitted']=permitted('execute')
            execution['shell']['effective_ready']=shell_state=='ready'
            execution['authorization']={'known':known or admin,'granted_scopes':sorted(scopes) if known else [],'panel_owner':admin}
            browser_state=available('computer',browser.get('enabled',False))
            if browser_state=='ready' and not browser.get('connected'):browser_state='not_connected'
            return {'build':self.agent.build.describe(),'execution':execution,'admission':admission,
                'language_servers':languages,'browser':browser,'capabilities':sorted(REMOTE_TOOLS),
                'file_import':{'source_policy_version':FILE_SOURCE_POLICY_VERSION,
                    'allowed_hosts':list(file_source_hosts(self.agent.config.get('integrations',{}))),
                    'max_bytes':self.agent.config.get('integrations',{}).get('max_import_bytes',DEFAULT_MAX_IMPORT_BYTES),
                    'host_roundtrip':'not_run'},
                'checks':[{'name':'project_read','state':'ready'},
                    {'name':'project_write','state':available('write',project.get('mode')=='write' and spec.get('writable',True),'denied')},
                    {'name':'shell','state':shell_state},
                    {'name':'native_model_turn','state':'not_run','reason':'就绪检查不调用模型'},
                    {'name':'host_file_roundtrip','state':'not_run','reason':'需实际宿主附件后才能验收'},
                    {'name':'host_mcp_apps','state':'not_run','reason':'宿主内卡片需真实客户端验收'},
                    {'name':'browser','state':browser_state}],
                'local_control':self.local_server is not None and self.agent.config.get('integrations',{}).get('local_control') is True,'note':'程序存在、协议实现与实际任务成功是不同状态；本检查不修改设置、启动模型或重启服务。'}
        raise DevError('UNKNOWN_INTEGRATION','未知集成工具')
