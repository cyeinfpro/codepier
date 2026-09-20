"""Owner-bound managed Git worktrees; IDs never grant source-root authority."""
from __future__ import annotations
import asyncio,hashlib,os,re,time
from pathlib import Path
from shared.util import DevError
from agent.filesystem import within

class Workspaces:
    def __init__(self,agent,records):self.agent,self.records=agent,records

    def parent_project(self,project):
        return {**project,'root':project.get('_original_root',project['root']),'_workspace_id':''}

    async def git(self,identifier,project,cwd,args):
        flags=['git','-c','core.fsmonitor=false','-c','core.hooksPath='+str(self.agent.state_dir/'no-hooks'),
               '-c','core.pager=cat','-c','submodule.recurse=false']
        result=await self.agent.run_process(identifier,flags+args,cwd,45,execution_project=project)
        if result['exit_code']!=0 or result.get('timed_out') or result.get('cancelled'):
            raise DevError('WORKTREE_GIT_FAILED',result['output'][-1200:] or 'Git 操作未完成',409)
        if result.get('output_truncated'):raise DevError('WORKTREE_OUTPUT_LIMIT','Git 输出超出范围，不能核实完整状态',409)
        return result['output'].strip()

    def resolve(self,project,workspace_id):
        parent=self.parent_project(project);self.agent.engine.root(parent)
        record=self.records.load('workspace',workspace_id,parent)
        if record['state']!='ready':raise DevError('WORKSPACE_NOT_READY','隔离目录不是就绪状态，请检查原创建操作',409)
        path=Path(record['path'])
        if path.is_symlink() or path.resolve()!=path or not path.is_dir():raise DevError('WORKSPACE_CHANGED','隔离目录已移动或被替换',409)
        st=path.stat()
        if [st.st_dev,st.st_ino]!=record['identity']:raise DevError('WORKSPACE_CHANGED','隔离目录身份不匹配',409)
        actual={**parent,'_original_root':parent['root'],'root':str(path),'_workspace_id':workspace_id}
        self.agent.engine.root(actual)
        return actual

    async def create(self,identifier,project,args):
        if args.get('workspace_id'):raise DevError('NESTED_WORKSPACE','从原项目创建隔离目录，不在隔离目录中嵌套创建')
        parent=self.parent_project(project);root,spec=self.agent.engine.root(parent,True)
        if not project.get('allow_tasks') or not spec.get('allow_tasks'):raise DevError('TASKS_DISABLED','创建 Git 工作目录需要执行权限',403)
        if args['base_ref'].startswith('-') or any(c in args['base_ref'] for c in '\x00\r\n'):raise DevError('INVALID_GIT_REF','无效 Git 基线')
        try: commit=await self.git(identifier,project,root,['rev-parse','--verify','--end-of-options',args['base_ref']+'^{commit}'])
        except DevError as exc:raise DevError('GIT_BASE_REQUIRED','需要已有提交的 Git 项目和有效 base_ref；不会自动初始化或提交',409) from exc
        if not re.fullmatch('[a-f0-9]{40,64}',commit):raise DevError('INVALID_GIT_COMMIT','Git 未返回精确提交编号')
        dirty=bool(await self.git(identifier,project,root,['status','--porcelain=v1','--untracked-files=normal']))
        configured=self.agent.config.get('integrations',{}).get('worktree_directory','')
        base=Path(configured).expanduser() if configured else root.parent/'CodePier-Worktrees'
        if not base.is_absolute():raise DevError('WORKTREE_DIRECTORY','隔离目录配置必须是绝对路径')
        parentdir=base/hashlib.sha256(str(root).encode()).hexdigest()[:16]
        target=parentdir/identifier
        if within(target,root) or within(root,target):raise DevError('WORKTREE_OVERLAP','隔离目录不能与源项目嵌套')
        # Every existing ancestor must be non-symlink; local nested read-only roots remain authoritative.
        for p in [target,*target.parents]:
            if p.is_symlink():raise DevError('SYMLINK_BLOCKED','隔离目录父路径包含符号链接',403)
            if p.exists():self.agent.engine.check_local_write(p);break
        self.agent.engine.check_local_write(target)
        if target.exists():raise DevError('WORKSPACE_EXISTS','目标目录已存在，未覆盖',409)
        if sum(r['state']!='removed' for r in self.records.list('workspace',parent,2000))>=64:raise DevError('WORKSPACE_QUOTA','该项目最多保留 64 个隔离目录记录')
        parentdir.mkdir(parents=True,exist_ok=True)
        record={'workspace_id':identifier,'path':str(target),'base_commit':commit,'label':args['label'],
                'source_dirty':dirty,'created':time.time(),'state':'preparing','identity':None}
        self.records.save('workspace',identifier,parent,record)
        try:
            await self.git(identifier,project,root,['worktree','add','--detach',str(target),commit])
            st=target.stat();record.update(state='ready',identity=[st.st_dev,st.st_ino])
            self.agent.engine.root({**parent,'root':str(target)},True)
        except BaseException:
            record['state']='needs_review';self.records.save('workspace',identifier,parent,record,replace=True);raise
        self.records.save('workspace',identifier,parent,record,replace=True)
        return {**record,'project':args['project'],'note':'未提交的源目录修改未复制；不自动合并、提交或删除。',
                'next':{'tool':'open_workspace','arguments':{'project':args['project'],'workspace_id':identifier}}}

    async def list(self,identifier,project):
        parent=self.parent_project(project);root,_=self.agent.engine.root(parent)
        rows=self.records.list('workspace',parent,100)
        visible=[]
        for r in rows:
            result={**r}
            if r['state']=='ready':
                try:
                    resolved=self.resolve(parent,r['workspace_id'])
                    result['dirty']=bool(await self.git(identifier,resolved,Path(resolved['root']),['status','--porcelain=v1','--untracked-files=all']))
                except DevError as exc:result.update(state='needs_review',error=exc.code)
            visible.append(result)
        return {'workspaces':visible,'limit':100}

    async def remove(self,identifier,project,args):
        if args.get("workspace_id"):
            raise DevError("PROJECT_CONTROL_SCOPE","请从原项目移除隔离工作目录，不能在待移除目录内执行",409)
        if args['target_workspace_id']!=args['confirm']:raise DevError('CONFIRMATION_REQUIRED','隔离目录编号确认不匹配',409)
        parent=self.parent_project(project);target_id=args['target_workspace_id'];record=self.records.load('workspace',target_id,parent)
        if record['state']=='removed':return {'workspace_id':target_id,'removed':True,'already_removed':True}
        resolved=self.resolve(parent,target_id);target=Path(resolved['root']);root,_=self.agent.engine.root(parent,True)
        self.agent.engine.root(resolved,True)
        for r in self.agent.native.live():
            cwd=Path(r.get('cwd') or r.get('root') or '/')
            if cwd==target or target in cwd.parents:raise DevError('WORKSPACE_BUSY','隔离目录仍有原生会话，先显式停止',409)
        if any(self.agent._projects_overlap(target,r) for r,_ in self.agent._active_slots.values()):
            raise DevError('WORKSPACE_BUSY','隔离目录仍有读写或命令在执行',409)
        async with self.agent.project_slot(target,write=True):
            if await self.git(identifier,resolved,target,['status','--porcelain=v1','--untracked-files=all']):
                raise DevError('WORKSPACE_DIRTY','隔离目录有改动或未跟踪文件；不会强制删除，请先处理',409)
            await self.git(identifier,parent,root,['worktree','remove',str(target)])
            if target.exists():raise DevError('WORKSPACE_REMOVE_UNCONFIRMED','Git 返回后目录仍存在，请核查',409)
            record['state']='removed';record['removed_at']=time.time();self.records.save('workspace',target_id,parent,record,replace=True)
        return {'workspace_id':target_id,'removed':True,'source_preserved':True}
