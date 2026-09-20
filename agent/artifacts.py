"""Immutable, bounded artifact snapshots. Binary transport is separate from tool text."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from shared.util import DevError, fsync_directory

CHUNK = 256 * 1024
MAX_BYTES = 512 * 1024 * 1024
QUOTA = 2 * 1024 * 1024 * 1024
RETENTION = 7 * 86400

class Artifacts:
    def __init__(self, engine):
        self.engine = engine
        self.journal = engine.journal
        self.directory = self.journal.directory / 'artifacts'
        self.directory.mkdir(exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.lock = threading.RLock()
        with self.journal.lock, self.journal.db:
            self.journal.db.execute('CREATE TABLE IF NOT EXISTS artifact_snapshots (id TEXT PRIMARY KEY, root TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, chunks TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL)')

    @staticmethod
    def validate_name(name):
        if not name or name in {'.','..'} or len(name)>180 or any(ord(c)<32 or ord(c)==127 or c in '/\\' for c in name):
            raise DevError('INVALID_ARTIFACT_NAME','产物名称不能包含路径或控制字符')
        return name

    def metadata(self, row):
        return {'artifact_id':row['id'],'name':row['name'],'bytes':row['bytes'],'sha256':row['sha256'],
                'created':row['created'],'expires':row['expires'],'immutable':True,
                'download_path':'/api/artifacts/'+row['id']+'/download','retention_days':7}

    def cleanup(self):
        with self.journal.lock:
            expired=self.journal.db.execute('SELECT id FROM artifact_snapshots WHERE expires<?',(time.time(),)).fetchall()
        for row in expired:
            try:
                (self.directory/(row[0]+'.bin')).unlink(missing_ok=True)
            except OSError:
                continue
            with self.journal.lock,self.journal.db:
                self.journal.db.execute('DELETE FROM artifact_snapshots WHERE id=?',(row[0],))
        # Interrupted registrations may leave an unreferenced snapshot; collect only old, known-format files.
        with self.journal.lock:
            known={r[0]+'.bin' for r in self.journal.db.execute('SELECT id FROM artifact_snapshots')}
        for path in self.directory.iterdir():
            if (re.fullmatch(r'[a-f0-9]{32}\.bin',path.name) or path.name.startswith('.artifact-')) and path.name not in known:
                try:
                    if path.stat().st_mtime<time.time()-86400:
                        path.unlink()
                except OSError:
                    pass

    def register(self, identifier, project, args):
        if not re.fullmatch(r'[a-f0-9]{32}',identifier):
            raise DevError('INVALID_ARTIFACT_ID','无效的产物编号')
        root,_=self.engine.root(project,True)
        source=self.engine.path(root,args['path'],False)
        binding_root=project.get('_original_root',str(root))
        # Snapshots survive managed-worktree cleanup, but never mapping revocation.
        if binding_root!=str(root):self.engine.root({**project,'root':binding_root},True)
        name=self.validate_name(args['name'] or source.name)
        with self.lock:
            self.cleanup()
            with self.journal.lock:
                old=self.journal.db.execute('SELECT * FROM artifact_snapshots WHERE id=?',(identifier,)).fetchone()
                used=self.journal.db.execute('SELECT COALESCE(SUM(bytes),0),COUNT(*) FROM artifact_snapshots').fetchone()
            if old:
                if old['root']!=binding_root or old['path']!=args['path'] or old['name']!=name:
                    raise DevError('IDEMPOTENCY_CONFLICT','产物编号已对应另一份快照')
                return self.metadata(old)
            flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
            try:
                descriptor=os.open(source,flags)
            except OSError as exc:
                raise DevError('ARTIFACT_UNREADABLE','无法读取产物文件') from exc
            temporary=None
            try:
                initial=os.fstat(descriptor)
                if not stat.S_ISREG(initial.st_mode) or initial.st_nlink!=1:
                    raise DevError('NOT_REGULAR_ARTIFACT','产物必须是没有额外硬链接的普通文件')
                if initial.st_size>MAX_BYTES:
                    raise DevError('ARTIFACT_TOO_LARGE','单个产物上限为 512 MiB')
                physical=sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file() and not p.is_symlink())
                if max(used[0],physical)+initial.st_size>QUOTA or used[1]>=1000:
                    raise DevError('ARTIFACT_QUOTA','产物快照空间达到 2 GiB 或 1000 项上限；过期快照会自动回收')
                fd,temporary=tempfile.mkstemp(prefix='.artifact-',dir=self.directory)
                full=hashlib.sha256();hashes=[];size=0
                with os.fdopen(fd,'wb') as target:
                    while True:
                        part=b''
                        while len(part)<CHUNK:
                            block=os.read(descriptor,CHUNK-len(part))
                            if not block:break
                            part+=block
                        if not part:break
                        size+=len(part)
                        if size>initial.st_size or size>MAX_BYTES:
                            raise DevError('ARTIFACT_CHANGED','生成快照期间源文件改变，请重新登记')
                        target.write(part);full.update(part);hashes.append(hashlib.sha256(part).hexdigest())
                    target.flush();os.fsync(target.fileno())
                final=os.fstat(descriptor)
                attrs=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
                if size!=initial.st_size or any(getattr(initial,k)!=getattr(final,k) for k in attrs):
                    raise DevError('ARTIFACT_CHANGED','生成快照期间源文件改变，请重新登记')
                now=time.time();sha=full.hexdigest()
                os.chmod(temporary,0o600)
                os.replace(temporary,self.directory/(identifier+'.bin'));temporary=None
                fsync_directory(self.directory)
                with self.journal.lock,self.journal.db:
                    self.journal.db.execute('INSERT INTO artifact_snapshots VALUES (?,?,?,?,?,?,?,?,?)',
                        (identifier,binding_root,args['path'],name,size,sha,json.dumps(hashes),now,now+RETENTION))
                return self.metadata({'id':identifier,'name':name,'bytes':size,'sha256':sha,'created':now,'expires':now+RETENTION})
            finally:
                os.close(descriptor)
                if temporary:
                    try:os.unlink(temporary)
                    except FileNotFoundError:pass

    def chunk(self, project, identifier, offset):
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{32}',identifier) or type(offset) is not int or offset<0 or offset%CHUNK:
            raise DevError('INVALID_ARTIFACT_CHUNK','无效的产物分段请求')
        root,_=self.engine.root(project)
        with self.lock,self.journal.lock:
            row=self.journal.db.execute('SELECT * FROM artifact_snapshots WHERE id=?',(identifier,)).fetchone()
            if not row or row['root']!=str(root):
                raise DevError('ARTIFACT_NOT_FOUND','当前项目没有此产物',404)
            if row['expires']<=time.time():
                raise DevError('ARTIFACT_EXPIRED','产物快照已过期，请重新登记',410)
            if offset>=row['bytes']:
                raise DevError('INVALID_ARTIFACT_RANGE','产物读取位置超出文件',416)
            descriptor=os.open(self.directory/(identifier+'.bin'),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            try:
                st=os.fstat(descriptor)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size!=row['bytes']:
                    raise DevError('ARTIFACT_CORRUPT','产物快照不再完整')
                os.lseek(descriptor,offset,os.SEEK_SET)
                data=b'';needed=min(CHUNK,row['bytes']-offset)
                while len(data)<needed:
                    part=os.read(descriptor,needed-len(data))
                    if not part:break
                    data+=part
                if len(data)!=needed or hashlib.sha256(data).hexdigest()!=json.loads(row['chunks'])[offset//CHUNK]:
                    raise DevError('ARTIFACT_CORRUPT','产物分段校验失败，已停止传输')
                return {'artifact_id':identifier,'offset':offset,'sha256':row['sha256'],'chunk_sha256':hashlib.sha256(data).hexdigest(),
                        'bytes':len(data),'data':base64.b64encode(data).decode('ascii')}
            finally:
                os.close(descriptor)
