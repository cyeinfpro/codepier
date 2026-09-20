"""Encrypted-transport adapter to independent native terminal workers."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager, closing
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
import uuid
from shared.native_cli import (database, identifier, public, launch_argv, LIVE, TOTAL_QUOTA, ATTACHMENT_CAP,
                               worker_present, process_exists, retained_bytes)
from shared.util import DevError
from agent.chat_catalog import CatalogCache, probe, validate_settings, COMMANDS
from agent.shell import shell_denial
from agent.native_pi_bridge import prepare_extension, image_manifest, sync_manifest


class NativeCLI:
    def __init__(self, agent):
        self.agent = agent
        self.directory = agent.state_dir / 'native-cli'
        self.children = []
        self.offsets = {}
        self.catalog_cache = CatalogCache()
        self.sync_cursor = 0
        self.launch_gate = threading.Lock()
        self.pending_starts = {}
        self.sync_wake = asyncio.Event()
        with self.connect_db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS session_files (session TEXT, file TEXT, PRIMARY KEY(session,file))')

    @contextmanager
    def connect_db(self):
        with closing(database(self.directory)) as db, db:
            yield db

    def authorize(self, project):
        root, spec = self.agent.engine.root(project, write=True)
        denial = shell_denial(self.agent.config, project, spec)
        if denial:
            raise DevError(*denial,403)
        return root

    def rows(self):
        self.children = [p for p in self.children if p.poll() is None]
        with self.connect_db() as db:
            stale = db.execute("SELECT * FROM sessions WHERE status IN ('starting','running','stopping','orphaned') AND heartbeat<?", (time.time()-30,)).fetchall()
            for row in stale:
                if worker_present(self.directory, row['id']):
                    continue  # A stalled heartbeat does not prove process death.
                if process_exists(row['child_pid']):
                    db.execute("UPDATE sessions SET status='orphaned',error='Worker unavailable but child may still exist; inspect host before maintenance',updated=? WHERE id=?", (time.time(), row['id']))
                else:
                    db.execute("UPDATE sessions SET status='interrupted',error='Worker and child unavailable; use native resume',lease='',lease_until=0,updated=? WHERE id=?", (time.time(), row['id']))
                    db.execute("UPDATE commands SET state='uncertain' WHERE session=? AND state='claimed'", (row['id'],))
                    db.execute("UPDATE commands SET state='cancelled' WHERE session=? AND state='queued'", (row['id'],))
            return [public(r) for r in db.execute('SELECT * FROM sessions ORDER BY created DESC,id')]

    def live(self):
        with self.launch_gate:
            pending = list(self.pending_starts.values())
        running = {r['id']:r for r in self.rows() if r['status'] in LIVE}
        for row in pending:
            running.setdefault(row['id'],row)
        return list(running.values())

    def discover(self):
        env = self.environment()
        result = {}
        for name in ('pi','codex'):
            path = shutil.which(name,path=env.get('PATH'))
            if not path:
                result[name] = {'available':False,'message':f'Install {name} locally or add its bin directory to shell.env.PATH; CodePier never installs it'}
                continue
            try:
                value = subprocess.run([path,'--version'],env=env,capture_output=True,text=True,timeout=10)
                version = value.stdout.strip()[:120] if value.returncode == 0 else 'Version probe failed'
            except (OSError,subprocess.TimeoutExpired):
                version = 'Version probe timed out'
            result[name] = {'available':True,'path':path,'version':version}
        backend = {'available': True, 'backend': 'posix'}
        if os.name == 'nt':
            from agent.native_windows import capabilities
            backend = capabilities()
        return {'programs':result,'pty':backend['backend'] if backend['available'] else 'unavailable',
                'backend':backend,
                'message':backend.get('reason','Native CLI runs with Agent-user OS privileges and native approvals'),
                'filesystem_scope':'Agent OS user, not project sandbox'}

    def environment(self):
        shell = self.agent.config['shell']
        env = dict(os.environ) if shell['inherit_env'] else {'PATH':os.defpath}
        env.update(shell['env'])
        # Common service PATH omissions; do not source shell startup files.
        if 'PATH' not in shell['env']:
            home = Path(env.get('HOME') or Path.home())
            paths = [env.get('PATH',os.defpath)]
            if os.name == 'nt':
                appdata = env.get('APPDATA')
                if appdata: paths.append(str(Path(appdata)/'npm'))
                paths.append(str(home/'AppData/Local/Microsoft/WinGet/Links'))
            else:
                paths.extend((str(home/'.local/bin'), '/opt/homebrew/bin', '/usr/local/bin'))
            env['PATH'] = os.pathsep.join(paths)
        # HOME is never redirected for production launches; native credentials/config are native-owned.
        return env

    def action(self, action, project, args):
        if action != 'start':
            return self._action(action,project,args)
        sid = identifier(args.get('id'))
        ticket = uuid.uuid4().hex
        with self.launch_gate:
            lifecycle = getattr(self.agent,'lifecycle',None)
            if lifecycle and getattr(lifecycle,'handoff_pending',False):
                raise DevError('AGENT_MAINTENANCE','节点正在准备或交接维护，请完成更新/重启后再启动原生会话',409)
            # Admission is visible before disk/process creation, closing the race
            # between a lifecycle busy check and a concurrent first PTY launch.
            self.pending_starts[ticket] = {'id':sid,'project_id':project['id'],'status':'starting'}
        try:
            return self._action(action,project,args)
        finally:
            with self.launch_gate:
                self.pending_starts.pop(ticket,None)

    def _action(self, action, project, args):
        root = self.authorize(project)
        if action == 'chat_catalog':
            cli = args.get('cli')
            if cli not in ('pi', 'codex'): raise ValueError('CLI must be pi or codex')
            cwd = self.agent.engine.path(root, args.get('cwd', '.'))
            if not cwd.is_dir(): raise ValueError('Invalid catalog cwd')
            model = validate_settings({'model': args.get('model', '')}, cli)['model']
            env = self.environment()
            executable = shutil.which(cli, path=env['PATH'])
            if not executable: raise DevError('CLI_MISSING', cli+' unavailable; install/configure native CLI locally', 409)
            include_commands = args.get('include_commands', True)
            if type(include_commands) is not bool: raise ValueError('include_commands must be boolean')
            # Defaults and skills can vary by directory. Cache their complete
            # response locally; the browser shares only model capabilities.
            environment_key = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()
            key = (project['id'], project['device_id'], str(root), cli, str(cwd), model, include_commands, executable, environment_key)
            result = self.catalog_cache.get(key, lambda: probe(cli, executable, cwd, env, model, include_commands=include_commands), bool(args.get('refresh')))
            if str(self.authorize(project)) != str(root): raise DevError('CLI_MAPPING_CHANGED', 'Mapping changed during catalog probe', 403)
            return result
        with self.connect_db() as db:
            if action == 'discover':
                return self.discover()
            if action.startswith('upload_'):
                db.execute('BEGIN IMMEDIATE')
                return self.upload(db,action,project,root,args)
            sid = identifier(args.get('id'))
            if action != 'start':
                # Lease validation and effect admission must share a transaction;
                # another tab cannot acquire the writer lease between them.
                db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
            if action == 'start':
                signature=hashlib.sha256(json.dumps(args,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if row:
                    self.check_row(row,project,root)
                    if row['launch_signature'] and row['launch_signature']!=signature:
                        raise DevError('CLI_START_CONFLICT','此会话编号已用于不同启动参数；请重新启动新会话',409)
                    return public(row)
                mode=args.get('mode','terminal')
                if mode not in {'terminal','chat'}: raise ValueError('Invalid session mode')
                if mode=='chat' and (args.get('argv') or args.get('provider') or args.get('attachments') or args.get('resume')):
                    raise ValueError('Chat uses native defaults; advanced launch options belong in terminal')
                if os.name == 'nt' and mode=='chat':
                    raise DevError('CLI_CHAT_PLATFORM','面板聊天目前支持 macOS/Linux；请选择受支持节点，或在 Windows 电脑本地使用 CLI',409)
                if os.name == 'nt' and mode=='terminal':
                    from agent.native_windows import capabilities
                    backend = capabilities()
                    if not backend['available']:
                        raise DevError('PTY_UNAVAILABLE',backend['reason'],409)
                if len(self.live()) > 8:
                    raise DevError('CLI_LIMIT','最多同时运行 8 个原生会话',409)
                if db.execute("SELECT count(*) FROM sessions WHERE status!='deleted'").fetchone()[0] >= 1000:
                    raise DevError('CLI_RETENTION_LIMIT','会话元数据达到 1000 条上限，请本机归档后维护',409)
                usage = retained_bytes(db)
                if usage >= TOTAL_QUOTA:
                    raise DevError('CLI_QUOTA','CodePier 原生会话磁盘达到 1 GiB 上限，请导出并清理',409)
                cwd = self.agent.engine.path(root,args.get('cwd','.'))
                if not cwd.is_dir():
                    raise DevError('INVALID_CWD','项目子目录不存在；不会回退到 HOME')
                provider = args.get('cli')
                if provider not in ('pi','codex'):
                    raise ValueError('CLI must be pi or codex')
                executable = shutil.which(provider,path=self.environment()['PATH'])
                if not executable:
                    raise DevError('CLI_MISSING',f'本机未找到 {provider}；安装后将目录加入 shell.env.PATH',409)
                files = args.get('attachments',[])
                if not isinstance(files,list) or len(files)>10:
                    raise ValueError('At most ten launch attachments')
                paths=[]
                for fid in files:
                    f = self.file_row(db,fid,project,root)
                    if not f['ready']:
                        raise ValueError('Attachment upload incomplete')
                    if provider == 'codex' and Path(f['path']).suffix.lower() not in ('.png','.jpg','.jpeg','.webp','.gif'):
                        raise ValueError('Codex --image accepts images; insert file paths in the live editor for text files')
                    paths.append(f['path'])
                argv=launch_argv(executable,provider,args,paths)
                settings={}
                native_thread=''
                if mode=='chat':
                    settings={'next': {k:v for k,v in validate_settings(args, provider).items() if v}}
                    if provider=='pi': native_thread=str(self.directory/(sid+'.pi.jsonl'))
                    argv=([executable,'--mode','rpc','--session',native_thread]
                          if provider=='pi' else [executable,'app-server'])
                    previous=args.get('continue_session')
                    if previous:
                        prior=db.execute('SELECT * FROM sessions WHERE id=?',(identifier(previous),)).fetchone()
                        if not prior: raise ValueError('Unknown continuation session')
                        self.check_row(prior,project,root)
                        if prior['mode']!='chat' or prior['provider']!=provider or prior['cwd']!=str(cwd):
                            raise ValueError('Continuation must use same project, directory and CLI')
                        if prior['status'] in LIVE or worker_present(self.directory,previous):
                            raise DevError('CLI_BUSY','原会话仍在运行，请直接打开它',409)
                        settings=json.loads(prior['chat_settings'])
                        for key,value in validate_settings(args,provider).items():
                            if value=='':
                                if key not in settings.get('defaults',{}): raise ValueError('Native default unavailable for continuation reset')
                                value=settings['defaults'][key]
                            settings.setdefault('next',{})[key]=value
                        if provider=='pi':
                            native_thread=prior['native_thread']
                            if not native_thread: raise ValueError('Native Pi history reference unavailable')
                            argv[-1]=native_thread
                        else:
                            native_thread=prior['native_thread']
                            if not native_thread: raise ValueError('Native thread was not confirmed; cannot resume')
                bridge=mode=='terminal' and provider=='pi' and args.get('image_bridge',True)
                if bridge:
                    argv[1:1]=['--extension',str(prepare_extension(self.directory))]
                now=time.time()
                db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,heartbeat,argv) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (sid,project['id'],project['device_id'],str(root),str(cwd),provider,provider+' · '+project['alias'],'starting',now,now,now,json.dumps(argv)))
                db.execute('UPDATE sessions SET launch_signature=?,mode=?,native_thread=?,chat_settings=? WHERE id=?',(signature,mode,native_thread,json.dumps(settings),sid))
                for fid in files: db.execute('INSERT INTO session_files VALUES (?,?)',(sid,fid))
                if mode=='chat' and args.get('continue_session'):
                    db.execute('INSERT OR IGNORE INTO session_files(session,file) SELECT ?,f.file FROM session_files f JOIN attachments a ON a.id=f.file WHERE f.session=?',(sid,args['continue_session']))
                manifest=sync_manifest(self.directory,db,sid) if bridge else None
                db.commit()
                env=self.environment()
                env.pop('RELAY_PI_IMAGE_MANIFEST',None)
                env.pop('CODEPIER_PI_IMAGE_MANIFEST',None)
                if manifest is not None:env['CODEPIER_PI_IMAGE_MANIFEST']=str(manifest)
                try:
                    detached = ({'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
                                if os.name == 'nt' else {'start_new_session':True})
                    p=subprocess.Popen([sys.executable,str(Path(__file__).with_name('chat_worker.py' if mode=='chat' else 'native_worker.py')),str(self.directory),sid,*([str(self.agent.engine.config_path)] if mode=='chat' else [])],
                      cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                      close_fds=True,**detached)
                    self.children.append(p)
                except OSError:
                    db.execute("UPDATE sessions SET status='interrupted',error='Worker launch failed' WHERE id=?",(sid,)); db.commit()
                    raise DevError('CLI_START_FAILED','无法启动独立 PTY worker',409)
                return public(db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())
            if not row:
                raise DevError('CLI_NOT_FOUND','会话不存在',404)
            self.check_row(row,project,root)
            if action.startswith('chat_'):
                return self.chat_action(db,row,action,project,root,args)
            if row['mode']=='chat' and action in {'input','resize','lease'}:
                raise DevError('CLI_CHAT_MODE','结构化聊天不接受终端按键或租约',409)
            if action == 'lease':
                writer=identifier(args.get('writer'))
                row=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
                if row['status'] not in LIVE:
                    raise DevError('CLI_NOT_RUNNING','会话已退出；历史仅供查看，可使用原生恢复',409)
                if row['lease'] != writer and row['lease_until']>time.time():
                    raise DevError('CLI_READ_ONLY','另一个标签页正在输入；等待其释放或 15 秒租约到期',409)
                db.execute('UPDATE sessions SET lease=?,lease_until=? WHERE id=?',(writer,time.time()+15,sid))
                return {'writer':writer,'until':time.time()+15}
            if action == 'detach':
                db.execute("UPDATE sessions SET lease='',lease_until=0 WHERE id=? AND lease=?",(sid,args.get('writer','')))
                return {'detached':True}
            if action == 'rename':
                title=args.get('title','')
                if not isinstance(title,str) or not 1<=len(title)<=120 or any(ord(c)<32 for c in title):
                    raise ValueError('Title must contain 1–120 printable characters')
                db.execute('UPDATE sessions SET title=?,updated=? WHERE id=?',(title,time.time(),sid))
                return {'renamed':True}
            if action in {'clear','delete'}:
                if row['status'] in LIVE or worker_present(self.directory,sid):
                    raise DevError('CLI_BUSY','先显式停止此会话，确认进程已退出后再清理；原生历史不受影响',409)
                if args.get('confirm') != sid:
                    raise ValueError('Explicit session confirmation required')
                from agent.coding_reviews import ReviewStore
                review_count = ReviewStore(self.agent.engine).purge_owner({**project, '_coding_owner': 'native:'+sid})
                db.execute('DELETE FROM output WHERE session=?',(sid,))
                db.execute('DELETE FROM commands WHERE session=?',(sid,))
                db.execute("UPDATE sessions SET status=?,size=0,argv='[]',title=?,updated=? WHERE id=?",('deleted' if action=='delete' else 'cleared', '' if action=='delete' else row['title'],time.time(),sid))
                return {'cleared':True,'native_history':'untouched','review_snapshots_removed':review_count}
            if action == 'receipt':
                r=db.execute('SELECT id,state FROM commands WHERE id=? AND session=?',(identifier(args.get('receipt')),sid)).fetchone()
                return dict(r) if r else {'state':'missing'}
            if action not in {'input','resize','stop'}:
                raise ValueError('Unknown native action')
            receipt=identifier(args.get('receipt'))
            payload={}
            if action=='input':
                text=args.get('text')
                if not isinstance(text,str) or not 1<=len(text.encode('utf-8'))<=16384:
                    raise ValueError('Input must contain 1–16384 UTF-8 bytes')
                payload={'text':text}
            elif action=='resize':
                if any(type(args.get(k)) is not int or not 2<=args[k]<=500 for k in ('rows','cols')):
                    raise ValueError('Terminal size must be 2–500 rows/columns')
                payload={k:args[k] for k in ('rows','cols')}
            packed=json.dumps(payload)
            old=db.execute('SELECT * FROM commands WHERE id=?',(receipt,)).fetchone()
            if old:
                if old['session']!=sid or old['kind']!=action or old['payload']!=packed:
                    raise ValueError('Receipt conflicts with earlier input')
                return {'receipt':receipt,'state':old['state']}
            if row['status'] not in LIVE:
                raise DevError('CLI_NOT_RUNNING','会话已停止；请启动原生 resume',409)
            if row['status']=='orphaned':
                raise DevError('CLI_ORPHANED','运行器失联且进程状态待确认；请在节点本机核实，不能假报停止成功',409)
            if row['status']=='stopping' and action!='stop':
                raise DevError('CLI_STOPPING','会话正在停止，等待进程退出后再操作',409)
            if row['mode']!='chat' and (row['lease']!=args.get('writer') or row['lease_until']<=time.time()):
                raise DevError('CLI_LEASE_REQUIRED','先取得输入租约',409)
            if action!='stop' and db.execute("SELECT count(*) FROM commands WHERE session=? AND state='queued'",(sid,)).fetchone()[0]>=128:
                raise DevError('CLI_BACKPRESSURE','输入队列繁忙；保持原回执重试',429)
            db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)',(receipt,sid,action,packed,time.time()))
            return {'receipt':receipt,'state':'queued'}

    def chat_action(self,db,row,action,project,root,args):
        """Admit a narrow durable command, never arbitrary native RPC or a writer lease."""
        if row['mode']!='chat':
            raise DevError('CLI_TERMINAL_MODE','此记录属于旧终端会话，不能作为聊天会话操作；请新建对话，原记录仍保留',409)
        if action=='chat_review':
            from shared.coding_contracts import ShowChanges
            from agent.coding_reviews import read_review
            values = ShowChanges.model_validate({'project': project['alias'], **{k:v for k,v in args.items() if k!='id'}}).model_dump()
            if values['baseline_ref']:
                raise ValueError('Native review reads require an existing review_ref')
            if row['status'] in {'deleted', 'cleared'}:
                raise DevError('CLI_NOT_FOUND', '会话记录已清理', 404)
            bound = {**project, '_coding_owner': 'native:'+row['id']}
            return read_review(self.agent.engine, bound, values)
        if action=='chat_queue':
            return {'commands':[dict(receipt=r['id'],kind=r['kind'],text=json.loads(r['payload']).get('text',''),state=r['state'],created=r['created']) for r in db.execute("SELECT * FROM commands WHERE session=? AND state IN ('queued','claimed') AND kind IN ('chat_prompt','chat_settings') ORDER BY rowid",(row['id'],))]}
        receipt=identifier(args.get('receipt'))
        if action=='chat_cancel':
            target=identifier(args.get('target'))
            packed=json.dumps({'target':target})
            old=db.execute('SELECT * FROM commands WHERE id=?',(receipt,)).fetchone()
            if old:
                if (old['session'],old['kind'],old['payload'])!=(row['id'],action,packed): raise ValueError('Receipt conflict')
                return {'receipt':receipt,'state':old['state'],'target':target}
            target_row=db.execute('SELECT * FROM commands WHERE session=? AND id=?',(row['id'],target)).fetchone()
            if not target_row: raise ValueError('Unknown queued command in this session')
            if target_row['state']=='claimed': raise DevError('CLI_CANNOT_CANCEL','cannot-cancel-use-interrupt',409)
            if target_row['kind'] not in ('chat_prompt','chat_settings'): raise ValueError('Only queued prompts/settings can be cancelled')
            if target_row['state'] not in ('queued','cancelled'): raise ValueError('Command is already settled')
            db.execute("UPDATE commands SET state='cancelled' WHERE session=? AND id=? AND state='queued'",(row['id'],target))
            db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?)',(receipt,row['id'],action,packed,'completed',time.time()))
            return {'receipt':receipt,'state':'completed','target':target,'target_state':'cancelled'}
        payload={}
        files=[]
        existing=db.execute('SELECT * FROM commands WHERE id=?',(receipt,)).fetchone()
        if existing and (existing['session']!=row['id'] or existing['kind']!=action):
            raise DevError('CLI_RECEIPT_CONFLICT','同一回执已用于不同消息',409)
        if action in ('chat_prompt','chat_steer'):
            text=args.get('text','')
            if not isinstance(text,str) or len(text.encode('utf8'))>65536:
                raise ValueError('Prompt exceeds 64 KiB')
            files=args.get('attachments',[])
            if not isinstance(files,list) or len(files)>10 or len(set(files))!=len(files):
                raise ValueError('At most ten distinct attachments')
            if not text.strip() and not files: raise ValueError('Empty prompt')
            if existing:
                prior=json.loads(existing['payload'])
                if prior.get('text')!=text or prior.get('attachment_ids',[])!=files or args.get('model') or args.get('effort'):
                    raise DevError('CLI_RECEIPT_CONFLICT','同一回执已用于不同消息',409)
                return {'receipt':receipt,'state':existing['state']}
            attachments=[]
            for fid in files:
                f=self.file_row(db,fid,project,root)
                if not f['ready']: raise ValueError('Attachment upload incomplete')
                mime={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg',
                      '.webp':'image/webp','.gif':'image/gif'}.get(Path(f['path']).suffix.lower(),'')
                attachments.append({'path':f['path'],'mime':mime,'name':f['name'],'sha256':f['sha'],'size':f['size']})
            if sum(a['size'] for a in attachments if a['mime'])>20*1024*1024:
                raise ValueError('单条消息的图片总量不能超过 20 MiB')
            payload={'text':text,'attachments':attachments,'attachment_ids':files}
            if args.get('model') or args.get('effort'):
                raise ValueError('Use the native settings command before sending a prompt')
        elif action=='chat_answer':
            request_id=args.get('request_id')
            answer=args.get('answer')
            if not isinstance(request_id,(str,int)) or isinstance(request_id,bool) or len(str(request_id))>256:
                raise ValueError('Invalid native request ID')
            if (answer is not None and not isinstance(answer,(dict,str,bool))) or len(json.dumps(answer))>16384:
                raise ValueError('Invalid native answer')
            payload={'request_id':request_id,'answer':answer}
            if existing:
                prior=json.loads(existing['payload'])
                payload.update({k:prior[k] for k in ('parent_receipt','request_guard') if k in prior})
            else:
                pending=None;buffer=bytearray()
                for chunk in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset',(row['id'],)):
                    buffer.extend(chunk['data'])
                    while b'\n' in buffer:
                        line,_,rest=buffer.partition(b'\n');buffer[:]=rest
                        event=json.loads(line)
                        if event.get('type')=='approval' and event.get('request_id')==str(request_id): pending=None if event.get('resolved') else event
                    if len(buffer)>1024*1024: raise ValueError('Native event exceeds approval guard limit')
                if not pending: raise ValueError('Native request is not observed pending')
                payload['parent_receipt']=pending.get('receipt')
                payload['request_guard']=hashlib.sha256(json.dumps({'method':pending.get('method'),'details':pending.get('details')},sort_keys=True).encode()).hexdigest()
        elif action=='chat_settings':
            payload=validate_settings(args,row['provider'])
            if not payload: raise ValueError('No settings supplied')
        elif action=='chat_command':
            name=args.get('name')
            if name not in COMMANDS[row['provider']]: raise ValueError('Command unavailable for this native provider/version')
            payload={'name':name}
            if name.startswith('set_auto_'):
                if type(args.get('enabled')) is not bool: raise ValueError('enabled must be boolean')
                payload['enabled']=args['enabled']
            if 'instructions' in args:
                if name!='compact' or not isinstance(args['instructions'],str) or len(args['instructions'])>16384: raise ValueError('Invalid compact instructions')
                if row['provider']=='codex' and args['instructions']: raise ValueError('Installed Codex compact does not support instructions')
                payload['instructions']=args['instructions']
        elif action!='chat_interrupt':
            raise ValueError('Unknown chat action')
        packed=json.dumps(payload,sort_keys=True,separators=(',',':'))
        old=db.execute('SELECT * FROM commands WHERE id=?',(receipt,)).fetchone()
        if old:
            if old['session']!=row['id'] or old['kind']!=action or old['payload']!=packed:
                raise DevError('CLI_RECEIPT_CONFLICT','同一回执已用于不同消息',409)
            return {'receipt':receipt,'state':old['state']}
        if row['status'] not in {'starting','running'}:
            raise DevError('CLI_NOT_RUNNING','会话已停止；请恢复原生会话',409)
        if action in {'chat_prompt','chat_settings'} and db.execute("SELECT count(*) FROM commands WHERE session=? AND state='queued'",(row['id'],)).fetchone()[0]>=128:
            raise DevError('CLI_BACKPRESSURE','队列繁忙，请保留原回执重试',429)
        if action not in {'chat_interrupt','chat_answer'} and retained_bytes(db)+len(packed.encode())>TOTAL_QUOTA:
            raise DevError('CLI_QUOTA','原生会话存储已满',409)
        if action=='chat_prompt' and not db.execute("SELECT 1 FROM commands WHERE session=? AND kind='chat_prompt' LIMIT 1",(row['id'],)).fetchone():
            title=' '.join(text.split())[:80] or '图片与文件'
            db.execute('UPDATE sessions SET title=?,updated=? WHERE id=?',(title,time.time(),row['id']))
        for fid in files: db.execute('INSERT OR IGNORE INTO session_files VALUES (?,?)',(row['id'],fid))
        db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)',
                   (receipt,row['id'],action,packed,time.time()))
        return {'receipt':receipt,'state':'queued'}

    @staticmethod
    def check_row(row,project,root):
        if row['project_id']!=project['id'] or row['device_id']!=project['device_id'] or row['root']!=str(root):
            raise DevError('CLI_MAPPING_CHANGED','项目映射已改变；请在原本机检查会话',403)

    def file_row(self,db,fid,project,root):
        row=db.execute('SELECT * FROM attachments WHERE id=?',(identifier(fid),)).fetchone()
        if not row or row['project_id']!=project['id'] or row['root']!=str(root):
            raise DevError('ATTACHMENT_NOT_FOUND','附件不属于当前项目映射',404)
        path=Path(row['path'])
        if path.is_symlink() or path.parent!=self.directory/'uploads' or path.parent.is_symlink():
            raise ValueError('Unsafe attachment path')
        return row

    def upload(self,db,action,project,root,args):
        if action=='upload_list':
            result=[]
            for row in db.execute('SELECT * FROM attachments WHERE project_id=? AND root=? ORDER BY rowid DESC',(project['id'],str(root))):
                result.append({'file':row['id'],'name':row['name'],'size':row['size'],'sha256':row['sha'],
                               'received':row['received'],'ready':bool(row['ready']),
                               'path':row['path'] if row['ready'] else ''})
            return {'files':result}
        fid=identifier(args.get('file'))
        if action=='upload_begin':
            name,size,sha=args.get('name'),args.get('size'),args.get('sha256')
            if not isinstance(name,str) or not 1<=len(name)<=255 or any(ord(c)<32 for c in name):
                raise ValueError('Invalid display filename')
            if type(size) is not int or not 0<size<=ATTACHMENT_CAP:
                raise ValueError('Attachments must be 1 byte–20 MiB')
            import re
            if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha): raise ValueError('Invalid SHA256')
            old=db.execute('SELECT * FROM attachments WHERE id=?',(fid,)).fetchone()
            if old:
                self.file_row(db,fid,project,root)
                if (old['name'],old['size'],old['sha'])!=(name,size,sha): raise ValueError('Upload ID conflict')
                return {'file':fid,'received':old['received'],'ready':bool(old['ready'])}
            if db.execute('SELECT COALESCE(sum(size),0) FROM attachments').fetchone()[0]+size>200*1024*1024:
                raise DevError('ATTACHMENT_QUOTA','附件预留空间达到 200 MiB；请清理附件',409)
            folder=self.directory/'uploads'; folder.mkdir(mode=0o700,exist_ok=True)
            if folder.is_symlink(): raise ValueError('Unsafe upload directory')
            suffix=Path(name).suffix.lower()
            if not re.fullmatch(r'\.[a-z0-9]{1,8}',suffix): suffix='.bin'
            path=folder/(fid+suffix)
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.close(fd)
            db.execute('INSERT INTO attachments(id,project_id,root,name,size,sha,path) VALUES (?,?,?,?,?,?,?)',(fid,project['id'],str(root),name,size,sha,str(path)))
            return {'file':fid,'received':0,'ready':False}
        row=self.file_row(db,fid,project,root)
        if action=='upload_bind':
            sid=identifier(args.get('id'))
            session=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
            if not session: raise DevError('CLI_NOT_FOUND','会话不存在',404)
            self.check_row(session,project,root)
            if session['status'] not in {'starting','running'} or not row['ready']:
                raise DevError('CLI_BUSY','会话未运行或附件上传未完成',409)
            if session['mode']!='chat' and (session['lease']!=args.get('writer') or session['lease_until']<=time.time()):
                raise DevError('CLI_LEASE_REQUIRED','先取得输入租约',409)
            db.execute('INSERT OR IGNORE INTO session_files VALUES (?,?)',(sid,fid))
            if session['mode']=='terminal' and session['provider']=='pi':sync_manifest(self.directory,db,sid)
            return {'bound':True,'file':fid,'path':row['path'],'name':row['name'],
                    'image_token':'[[codepier-image:'+fid+']]' if session['provider']=='pi' else None}
        if action=='upload_delete':
            if args.get('confirm')!=fid: raise ValueError('Confirm attachment ID')
            if db.execute("SELECT 1 FROM session_files f JOIN sessions s ON s.id=f.session WHERE f.file=? AND s.status IN ('starting','running','stopping','orphaned')",(fid,)).fetchone():
                raise DevError('CLI_BUSY','运行会话仍在使用此附件',409)
            Path(row['path']).unlink(missing_ok=True)
            db.execute('DELETE FROM attachments WHERE id=?',(fid,))
            db.execute('DELETE FROM session_files WHERE file=?',(fid,))
            return {'deleted':True}
        if action=='upload_chunk':
            data=base64.b64decode(args.get('data',''),validate=True)
            offset=args.get('offset')
            if not 0<len(data)<=65536 or type(offset) is not int or offset<0 or offset+len(data)>row['size']:
                raise ValueError('Invalid upload chunk')
            if hashlib.sha256(data).hexdigest()!=args.get('sha256'): raise ValueError('Chunk SHA256 mismatch')
            fd=os.open(row['path'],os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
            with os.fdopen(fd,'r+b') as out:
                if offset<row['received']:
                    out.seek(offset)
                    if out.read(len(data))!=data: raise ValueError('Retry content mismatch')
                elif offset==row['received']:
                    out.seek(offset);out.write(data);out.flush();os.fsync(out.fileno())
                    db.execute('UPDATE attachments SET received=? WHERE id=?',(offset+len(data),fid))
                else: raise ValueError('Upload offset gap; retry from acknowledged offset')
            row=self.file_row(db,fid,project,root)
        elif action!='upload_finish': raise ValueError('Unknown upload action')
        if row['received']==row['size']:
            fd=os.open(row['path'],os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            with os.fdopen(fd,'rb') as source:
                digest=hashlib.file_digest(source,'sha256').hexdigest()
            if digest!=row['sha']: raise ValueError('Attachment SHA256 mismatch; delete upload and retry')
            db.execute('UPDATE attachments SET ready=1 WHERE id=?',(fid,))
            return {'file':fid,'received':row['received'],'ready':True,'name':row['name'],'path':row['path']}
        return {'file':fid,'received':row['received'],'ready':False}

    async def handle(self,data,socket):
        request=data.get('request_id')
        try:
            identifier(request)
            # Serialize admission with lifecycle preparation. Session lifetime holds no project lock.
            async with self.agent.lifecycle.lock if data.get('action')=='start' else _NoLock():
                if data.get('action')=='start' and any(self.agent.lifecycle.plan_dir.glob('*.json')):
                    raise DevError('AGENT_BUSY','Agent 生命周期操作等待交接；暂不启动 CLI',409)
                result=await asyncio.to_thread(self.action,data['action'],data['project'],data.get('args',{}))
            self.sync_wake.set()
            response={'ok':True,'data':result}
        except DevError as exc:
            response={'ok':False,'error':{'code':exc.code,'message':exc.message,**exc.details}}
        except (ValueError,TypeError,KeyError) as exc:
            response={'ok':False,'error':{'code':'CLI_INVALID','message':str(exc)[:300]}}
        except Exception:
            response={'ok':False,'error':{'code':'CLI_STORAGE','message':'原生会话存储或运行器错误；检查本机磁盘和权限'}}
        if self.agent.socket is socket:
            await self.agent.send({'type':'native_reply','request_id':request,'result':response})

    def snapshot(self):
        all_rows=self.rows()
        live=[r for r in all_rows if r['status'] in LIVE]
        history=[r for r in all_rows if r['status'] not in LIVE]
        # Every active conversation participates in every cycle, even with a
        # thousand old sessions. Rotate live priority; reserve early positions
        # for old transcripts so a large backfill cannot starve live output or
        # be permanently starved by it.
        if live:
            cursor=getattr(self,'live_sync_cursor',0)%len(live)
            live=live[cursor:]+live[:cursor]
            self.live_sync_cursor=(cursor+1)%len(live)
        if history:
            self.sync_cursor%=len(history)
            history=(history[self.sync_cursor:]+history[:self.sync_cursor])[:max(0,250-len(live))]
            self.sync_cursor=(self.sync_cursor+max(1,min(8,len(history))))%len([r for r in all_rows if r['status'] not in LIVE])
        sessions=live[:4]+history[:4]+live[4:]+history[4:]
        chunks=[]; budget=196608
        with self.connect_db() as db:
            self.sync_active=bool(db.execute("SELECT 1 FROM commands WHERE state IN ('queued','claimed') AND kind IN ('chat_prompt','chat_settings') LIMIT 1").fetchone())
            for row in sessions:
                after=self.offsets.get(row['id'],0)
                packed=bytearray(); start=after; emitted=0
                for out in db.execute('SELECT * FROM output WHERE session=? AND offset>=? ORDER BY offset LIMIT 4096',(row['id'],after)):
                    raw=out['data']
                    if out['offset']!=start+len(packed): break
                    if packed and len(packed)+len(raw)>65536:
                        if len(packed)>budget or len(chunks)>=16: break
                        chunks.append({'id':row['id'],'offset':start,'data':base64.b64encode(packed).decode('ascii')})
                        budget-=len(packed); start+=len(packed); packed.clear(); emitted+=1
                        if emitted>=2: break
                    if len(raw)>budget-len(packed) or len(chunks)>=16: break
                    packed.extend(raw)
                if packed and len(chunks)<16 and len(packed)<=budget:
                    chunks.append({'id':row['id'],'offset':start,'data':base64.b64encode(packed).decode('ascii')});budget-=len(packed)
                if budget<65536 or len(chunks)>=16: break
        return {'type':'native_sync','sessions':sessions,'chunks':chunks}

    async def sync(self):
        self.offsets={}
        sent_metadata={}
        idle=.1
        while not self.agent.stop_event.is_set():
            try:
                packet=await asyncio.to_thread(self.snapshot)
                active=bool(packet['chunks']) or getattr(self,'sync_active',False) or any(r['status']=='starting' for r in packet['sessions'])
                chunk_ids={chunk['id'] for chunk in packet['chunks']}
                packet['sessions']=[row for row in packet['sessions']
                                    if row['id'] in chunk_ids or sent_metadata.get(row['id'])!=row]
                if packet['sessions'] or packet['chunks']:
                    await self.agent.send(packet)
                    for row in packet['sessions']: sent_metadata[row['id']]=row
                idle=.09 if active else min(1.5,idle*1.4)
            except Exception:
                # Durable spool remains authoritative; retry without logging terminal content.
                pass
            try:
                await asyncio.wait_for(self.sync_wake.wait(),timeout=idle)
                self.sync_wake.clear()
                idle=.09
            except asyncio.TimeoutError:
                pass


class _NoLock:
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False
