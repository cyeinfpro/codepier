"""Metadata-only source fingerprints. Completeness and freshness are explicit."""
from __future__ import annotations
import hashlib,json,stat,time
from agent.coding_reviews import excluded
from shared.util import DevError

MAX_FILES=20000
MAX_BYTES=256*1024*1024

def source_version(engine,project):
    root,_=engine.root(project);entries={};skipped=[];total=0;complete=True
    try:
        for path,rel,st in engine.walk(root,root,exclude=excluded,deadline=time.monotonic()+8):
            if not stat.S_ISREG(st.st_mode):continue
            if len(entries)>=MAX_FILES or total+st.st_size>MAX_BYTES:
                complete=False;skipped.append({'path':'.','code':'SOURCE_BUDGET'});break
            try:
                data=engine.read_bytes(engine.path(root,rel,False))
                entries[rel]={'sha256':hashlib.sha256(data).hexdigest(),'mode':stat.S_IMODE(st.st_mode)};total+=len(data)
            except (DevError,OSError) as exc:
                complete=False;skipped.append({'path':rel,'code':getattr(exc,'code','SOURCE_UNREADABLE')})
                if len(skipped)>=100:break
    except (DevError,OSError) as exc:
        complete=False;skipped.append({'path':'.','code':getattr(exc,'code','SOURCE_SCAN_FAILED')})
    serial=json.dumps(entries,sort_keys=True,separators=(',',':')).encode()
    return {'sha256':hashlib.sha256(serial).hexdigest(),'complete':complete and not skipped,
            'files':len(entries),'bytes':total,'skipped':skipped,'observed_at':time.time(),
            'scope':'允许读取的源码；排除凭据、依赖、构建产物及 docs/evidence。逐文件采样，不是 OS 原子快照。'}

class Validations:
    def __init__(self,agent,records):self.agent,self.records=agent,records

    async def run(self,identifier,project,args):
        import asyncio
        from pathlib import Path
        root,_=self.agent.engine.root(project)
        selected=Path(args['cwd']).expanduser()
        if selected.is_absolute():
            try:relative=selected.relative_to(root).as_posix()
            except ValueError as exc:raise DevError('VALIDATION_CWD','验收工作目录必须位于所绑定的项目内；普通 Shell 权限保持不变',403) from exc
        else:relative=selected.as_posix()
        cwd=self.agent.engine.path(root,relative)
        if not cwd.is_dir():raise DevError('VALIDATION_CWD','验收工作目录不存在或不是目录',409)
        args={**args,'cwd':str(cwd)}
        before=await asyncio.to_thread(source_version,self.agent.engine,project)
        # Preserve a pre-execution record. A crash cannot turn it into a passing test.
        record={'validation_id':identifier,'label':args['label'],'state':'executing','before':before,
                'created':time.time(),'decision':None,'execution_operation_id':identifier}
        self.records.save('validation',identifier,project,record)
        try:
            result=await self.agent.execute(identifier,'shell_exec',project,{k:args[k] for k in ('command','cwd','timeout_seconds','env')})
        except BaseException as exc:
            record.update(state='interrupted',finished=time.time(),reason=getattr(exc,'code',type(exc).__name__))
            self.records.save('validation',identifier,project,record,replace=True)
            raise
        after=await asyncio.to_thread(source_version,self.agent.engine,project)
        success=result.get('exit_code')==0 and not result.get('timed_out') and not result.get('cancelled')
        state=('failed' if not success else 'unverified' if not before['complete'] or not after['complete'] else
               'stale' if before['sha256']!=after['sha256'] else 'passed')
        record.update(state=state,after=after,finished=time.time(),exit_code=result.get('exit_code'),
                      timed_out=result.get('timed_out',False),cancelled=result.get('cancelled',False),
                      duration_ms=result.get('duration_ms'),output_truncated=result.get('output_truncated',False))
        self.records.save('validation',identifier,project,record,replace=True)
        return {**result,**record,'next':{'tool':'validations_get','arguments':{'project':args['project'],
                'workspace_id':args.get('workspace_id',''),'validation_id':identifier}}}

    def get(self,project,identifier):
        record=self.records.load('validation',identifier,project)
        current=source_version(self.agent.engine,project)
        after=record.get('after',{})
        fresh=current['complete'] and after.get('complete') and current['sha256']==after.get('sha256')
        current_state=record['state']
        if record['state']=='passed':current_state='passed' if fresh else 'stale' if current['complete'] else 'unverified'
        return {**record,'historical_state':record['state'],'state':current_state,'source_current':bool(fresh),
                'current':current,'accepted_current':bool(record.get('decision') and record['decision']['action']=='accept' and current_state=='passed')}

    def list(self,project):
        rows=self.records.list('validation',project,100)
        return {'validations':[{'validation_id':r['validation_id'],'label':r['label'],'historical_state':r['state'],
                  'created':r['created'],'exit_code':r.get('exit_code'),'decision':r.get('decision'),'freshness':'not_checked'} for r in rows],
                'limit':100,'note':'列表为历史记录；打开详情才重新核对当前源码。'}

    def accept(self,project,args):
        if not project.get('_integration_admin'):raise DevError('OWNER_REQUIRED','只有面板主理人可接受或拒绝验收',403)
        if args['confirm']!=args['validation_id']:raise DevError('CONFIRMATION_REQUIRED','确认编号不匹配',409)
        checked=self.get(project,args['validation_id'])
        if args['decision']=='accept' and checked['state']!='passed':raise DevError('VALIDATION_NOT_CURRENT','验证未通过、已过期或覆盖不完整，不能接受',409)
        record=self.records.load('validation',args['validation_id'],project)
        record['decision']={'action':args['decision'],'at':time.time(),'note':args['note']}
        self.records.save('validation',args['validation_id'],project,record,replace=True)
        return {**checked,'decision':record['decision'],'accepted_current':args['decision']=='accept'}
