"""Cooperative incremental search. Saved cursors survive restart; no background model."""
from __future__ import annotations
import fnmatch
import json
import stat
import threading
import time
from agent.symbols import analyze
from shared.crypto import digest
from shared.util import DevError

TTL=3600
MAX_STORAGE=128*1024*1024
READ_BUDGET=128*1024*1024

class Searches:
    def __init__(self,engine):
        self.engine=engine;self.journal=engine.journal;self.lock=threading.RLock()
        with self.journal.lock,self.journal.db:
            self.journal.db.execute('CREATE TABLE IF NOT EXISTS search_sessions (id TEXT PRIMARY KEY, root TEXT NOT NULL, project_id TEXT NOT NULL, args TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, scanned INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0, result_count INTEGER NOT NULL DEFAULT 0, read_bytes INTEGER NOT NULL DEFAULT 0, stored_bytes INTEGER NOT NULL DEFAULT 0, truncated INTEGER NOT NULL DEFAULT 0, error TEXT, file_count INTEGER NOT NULL DEFAULT 0, spent REAL NOT NULL DEFAULT 0)')
            self.journal.db.execute('CREATE TABLE IF NOT EXISTS search_files (search_id TEXT NOT NULL, seq INTEGER NOT NULL, path TEXT NOT NULL, PRIMARY KEY(search_id,seq))')
            self.journal.db.execute('CREATE TABLE IF NOT EXISTS search_hits (search_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(search_id,seq))')

    def row(self,project,identifier):
        root,_=self.engine.root(project)
        with self.journal.lock:
            row=self.journal.db.execute('SELECT * FROM search_sessions WHERE id=?',(identifier,)).fetchone()
        if not row or row['root']!=str(root) or row['project_id']!=project.get('id',project.get('alias','')):
            raise DevError('SEARCH_NOT_FOUND','当前项目没有此搜索会话',404)
        if row['expires']<=time.time():raise DevError('SEARCH_EXPIRED','搜索会话已过期，请开始新搜索',410)
        return dict(row)

    def start(self,identifier,project,args):
        root,_=self.engine.root(project);start=self.engine.path(root,args['path'])
        if not start.is_dir() and not start.is_file():raise DevError('NOT_FOUND','搜索路径不存在',404)
        encoded=json.dumps(args,sort_keys=True,ensure_ascii=False)
        with self.lock:
            with self.journal.lock,self.journal.db:
                expired=[r[0] for r in self.journal.db.execute('SELECT id FROM search_sessions WHERE expires<=?',(time.time(),))]
                for old_id in expired:
                    self.journal.db.execute('DELETE FROM search_hits WHERE search_id=?',(old_id,))
                    self.journal.db.execute('DELETE FROM search_files WHERE search_id=?',(old_id,))
                    self.journal.db.execute('DELETE FROM search_sessions WHERE id=?',(old_id,))
                old=self.journal.db.execute('SELECT * FROM search_sessions WHERE id=?',(identifier,)).fetchone()
                used=self.journal.db.execute('SELECT COUNT(*),COALESCE(SUM(stored_bytes),0) FROM search_sessions').fetchone()
            if old:
                if old['root']!=str(root) or old['args']!=encoded:raise DevError('IDEMPOTENCY_CONFLICT','搜索编号已用于另一请求')
                return {'search_id':identifier,'state':old['state'],'project_root':str(root),'expires':old['expires']}
            if used[0]>=32 or used[1]>=MAX_STORAGE:
                raise DevError('SEARCH_BUSY','搜索保留达到 32 个会话或 128 MiB 结果预算；一小时后过期回收',429)
            paths=[];manifest_bytes=0;truncated=False;error=None;begin=time.monotonic()
            try:
                entries=[(start,start.relative_to(root).as_posix(),start.stat())] if start.is_file() else self.engine.walk(root,start,deadline=begin+min(8,args['timeout_seconds']))
                for _,relative,info in entries:
                    if not stat.S_ISREG(info.st_mode) or not fnmatch.fnmatchcase(relative,args['file_glob']):continue
                    if len(paths)>=args['max_files']:truncated=True;break
                    cost=len(relative.encode('utf-8'))+64
                    if manifest_bytes+cost>4*1024*1024 or used[1]+manifest_bytes+cost>MAX_STORAGE:
                        truncated=True;error='SEARCH_MANIFEST_LIMIT';break
                    paths.append(relative);manifest_bytes+=cost
            except DevError as exc:
                if exc.code not in {'SCAN_LIMIT','SCAN_FAILED'}:raise
                truncated=True;error=exc.code
            now=time.time();state='running' if paths else 'completed'
            with self.journal.lock,self.journal.db:
                self.journal.db.execute('INSERT INTO search_sessions(id,root,project_id,args,state,created,expires,truncated,error,file_count,spent,stored_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identifier,str(root),project.get('id',project.get('alias','')),encoded,state,now,now+TTL,int(truncated),error,len(paths),time.monotonic()-begin,manifest_bytes))
                self.journal.db.executemany('INSERT INTO search_files VALUES (?,?,?)',((identifier,i+1,path) for i,path in enumerate(paths)))
            return {'search_id':identifier,'state':state,'project_root':str(root),'expires':now+TTL,'file_count':len(paths),
                    'truncated':truncated,'next':'searches_get','execution':'Scanning advances on searches_get; no automatic background execution.'}

    def advance(self,project,row):
        args=json.loads(row['args']);root,_=self.engine.root(project);begin=time.monotonic()
        with self.journal.lock:
            files=self.journal.db.execute('SELECT seq,path FROM search_files WHERE search_id=? AND seq>? ORDER BY seq LIMIT 64',(row['id'],row['scanned'])).fetchall()
            storage=self.journal.db.execute('SELECT COALESCE(SUM(stored_bytes),0) FROM search_sessions').fetchone()[0]
        needle=args['query'] if args['case_sensitive'] else args['query'].casefold();hits=[]
        for index,path in files:
            elapsed=time.monotonic()-begin
            if elapsed>=2:break
            if row['spent']+elapsed>=args['timeout_seconds'] or row['read_bytes']>=READ_BUDGET:
                row.update(state='completed',truncated=1,error='SEARCH_BUDGET');break
            row['scanned']=index
            try:
                current_root,_=self.engine.root(project)
                if current_root!=root:raise DevError('ROOT_CHANGED','项目根目录变化')
                data=self.engine.read_bytes(self.engine.path(root,path,False));text=self.engine.text(data)
                row['read_bytes']+=len(data)
                if row['read_bytes']>READ_BUDGET:
                    row.update(state='completed',truncated=1,error='SEARCH_BUDGET');break
                sha=digest(data);lines=text.splitlines()
                if args['mode']=='text':
                    matches=({'line':n,'kind':'text','name':'','snippet':line[:600]} for n,line in enumerate(lines,1)
                             if needle in (line if args['case_sensitive'] else line.casefold()))
                else:
                    remaining=args['timeout_seconds']-row['spent']-(time.monotonic()-begin)
                    if remaining<=0:
                        row.update(state='completed',truncated=1,error='SEARCH_BUDGET');break
                    # The two-second page target is soft for one file. Do not
                    # kill a healthy parser just because earlier files filled
                    # the page; total search time and each worker stay bounded.
                    parsed=analyze(path,data,timeout=min(5,remaining))
                    items=parsed['symbols'] if args['mode']=='symbols' else parsed['references']
                    matches=({**item,'snippet':lines[item['line']-1][:600] if item['line']<=len(lines) else ''} for item in items
                             if needle in ((item.get('qualified_name') or item['name']) if args['case_sensitive'] else (item.get('qualified_name') or item['name']).casefold()))
                for item in matches:
                    payload=json.dumps({'path':path,'sha256':sha,**item},ensure_ascii=False);size=len(payload.encode())
                    if row['result_count']>=args['max_results'] or storage+size>MAX_STORAGE or row['spent']+time.monotonic()-begin>=args['timeout_seconds']:
                        row.update(state='completed',truncated=1,error='SEARCH_BUDGET');break
                    row['result_count']+=1;row['stored_bytes']+=size;storage+=size
                    hits.append((row['id'],row['result_count'],payload))
            except (DevError,OSError,UnicodeError) as exc:
                if isinstance(exc,DevError) and exc.code in {'ROOT_CHANGED','ROOT_NOT_ALLOWED','PROTECTED_ROOT'}:
                    row.update(state='failed',truncated=1,error=exc.code);break
                if isinstance(exc,DevError) and exc.code in {'PARSER_CRASHED','PARSER_TIMEOUT','PARSER_UNAVAILABLE','PARSER_FAILED','PARSER_BUSY'}:
                    row['skipped']+=1
                    row.update(state='failed',truncated=1,error=exc.code);break
                row['skipped']+=1
            if row['state']!='running':break
        if row['state']=='running' and row['scanned']>=row['file_count']:row['state']='completed'
        row['spent']+=time.monotonic()-begin
        with self.journal.lock,self.journal.db:
            self.journal.db.executemany('INSERT INTO search_hits VALUES (?,?,?)',hits)
            self.journal.db.execute('UPDATE search_sessions SET state=?,scanned=?,skipped=?,result_count=?,read_bytes=?,stored_bytes=?,truncated=?,error=?,spent=? WHERE id=?',
                tuple(row[k] for k in ('state','scanned','skipped','result_count','read_bytes','stored_bytes','truncated','error','spent','id')))

    def get(self,project,args):
        with self.lock:
            row=self.row(project,args['search_id'])
            if args['cursor']>row['result_count']:
                raise DevError('INVALID_SEARCH_CURSOR','游标超过已保存结果范围，请使用上一页返回的游标',409)
            if row['state']=='running' and row['result_count']-args['cursor']<args['limit']:
                self.advance(project,row)
            with self.journal.lock:
                hits=self.journal.db.execute('SELECT seq,payload FROM search_hits WHERE search_id=? AND seq>? ORDER BY seq LIMIT ?',
                    (row['id'],args['cursor'],args['limit']+1)).fetchall()
        more=len(hits)>args['limit'];hits=hits[:args['limit']];files={};results=[];root,_=self.engine.root(project)
        for hit in hits:
            item=json.loads(hit[1]);path=item['path']
            if path not in files:
                try:files[path]=digest(self.engine.read_bytes(self.engine.path(root,path,False)))
                except (DevError,OSError):files[path]=None
            item.update(seq=hit[0],current_sha256=files[path],stale=files[path]!=item['sha256']);results.append(item)
        return {'search_id':row['id'],'state':row['state'],'results':results,'cursor':hits[-1][0] if hits else args['cursor'],
                'has_more':more or row['state']=='running','scanned_files':row['scanned'],'file_count':row['file_count'],
                'skipped_files':row['skipped'],'result_count':row['result_count'],'truncated':bool(row['truncated']),
                'error':row['error'],'expires':row['expires'],
                'consistency':'Saved file manifest and per-file content; not an atomic snapshot. Returned hits are SHA-rechecked. New files need a new search.',
                'precision':'References are syntactic candidates, not LSP type resolution; unsupported and unparsable files count as skipped.'}

    def cancel(self,project,args):
        with self.lock:
            row=self.row(project,args['search_id'])
            if row['state']=='running':
                with self.journal.lock,self.journal.db:
                    self.journal.db.execute("UPDATE search_sessions SET state='cancelled' WHERE id=?",(row['id'],))
                row['state']='cancelled'
            return {'search_id':row['id'],'state':row['state'],'next':'searches_get'}
