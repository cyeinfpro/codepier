"""Native host file import: pinned public TLS connections and anchored publication.

No browser cookies, bearer headers, arbitrary URLs, overwrites or extraction.
The native file object is transport input, never part of a returned receipt.
"""
from __future__ import annotations
import contextlib,hashlib,http.client,ipaddress,os,socket,ssl,stat,time,uuid
from pathlib import Path
from urllib.parse import urlsplit,urljoin
from agent.filesystem import relative_path
from shared.util import DevError

DEFAULT_HOSTS=('files.oaiusercontent.com','cdn.openai.com')
MAX_BYTES=128*1024*1024


def validate_url(value,hosts):
    try:
        parsed=urlsplit(value)
        host=(parsed.hostname or '').lower()
        if parsed.scheme!='https' or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None,443):raise ValueError()
        if not host or host not in hosts or any(ord(c)<33 for c in value):raise ValueError()
        host.encode('ascii')
    except (ValueError,UnicodeError) as exc:
        raise DevError('ARTIFACT_SOURCE_DENIED','文件来源不是本机允许的原生 HTTPS 下载源；没有请求该地址',403) from exc
    return parsed,host


class PublicTLSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once, validate all addresses, then connect to that exact address.
        # TLS still verifies the original host; DNS cannot rebind during connect.
        addresses=socket.getaddrinfo(self.host,self.port,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0].split('%')[0]).is_global for a in addresses):
            raise DevError('ARTIFACT_SOURCE_DENIED','文件下载地址解析到非公网网络，已拒绝',403)
        error=None
        for family,kind,proto,_,address in addresses[:8]:
            sock=socket.socket(family,kind,proto);sock.settimeout(self.timeout)
            try:
                sock.connect(address)
                self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
                return
            except OSError as exc:
                error=exc;sock.close()
        raise DevError('ARTIFACT_NETWORK','原生文件下载连接失败；未发布目标文件',502) from error


def download_chunks(file,hosts,max_bytes):
    url=file['download_url'];started=time.monotonic()
    for redirects in range(4):
        parsed,host=validate_url(url,hosts)
        connection=PublicTLSConnection(host,443,timeout=15,context=ssl.create_default_context())
        try:
            path=parsed.path or '/'
            if parsed.query:path+='?'+parsed.query
            connection.request('GET',path,headers={'Accept-Encoding':'identity','User-Agent':'CodePier-Native-File/1','Connection':'close'})
            response=connection.getresponse()
            if response.status in {301,302,303,307,308}:
                location=response.getheader('Location')
                if not location or redirects==3:raise DevError('ARTIFACT_REDIRECT','下载重定向数量无效，未发布文件')
                url=urljoin(url,location);validate_url(url,hosts);continue
            if response.status!=200:raise DevError('ARTIFACT_DOWNLOAD_FAILED',f'原生文件源返回 HTTP {response.status}；凭据可能已过期，未发布文件',502)
            if response.getheader('Content-Encoding','identity').lower() not in {'identity',''}:
                raise DevError('ARTIFACT_ENCODING','不接受未经大小校验的压缩下载响应')
            length=response.getheader('Content-Length')
            if length is not None:
                try:expected=int(length)
                except ValueError as exc:raise DevError('ARTIFACT_SIZE','无效下载长度') from exc
                if not 0<=expected<=max_bytes:raise DevError('ARTIFACT_TOO_LARGE','下载文件超出大小限制')
            else:expected=None
            total=0
            while True:
                if time.monotonic()-started>180:raise DevError('ARTIFACT_TIMEOUT','下载超时，临时文件未发布')
                block=response.read(65536)
                if not block:break
                total+=len(block)
                if total>max_bytes:raise DevError('ARTIFACT_TOO_LARGE','流式下载超过大小上限')
                yield block
            if expected is not None and total!=expected:raise DevError('ARTIFACT_SIZE','下载长度与响应不符')
            return
        except DevError:raise
        except (OSError,http.client.HTTPException) as exc:
            raise DevError('ARTIFACT_NETWORK','文件传输中断；未把不完整文件发布为成功',502) from exc
        finally:connection.close()
    raise DevError('ARTIFACT_REDIRECT','重定向超过上限')


class AnchoredDestination:
    def __init__(self,engine,root,relative):
        self.engine,self.root=engine,root;self.parts=relative.split('/');self.fds=[];self.handles=[]
        self.parent=root;self.fd=None;self.temporary='.rd-import-'+uuid.uuid4().hex+'.part';self.published=False

    def __enter__(self):
        try:
            if os.name=='nt':self._windows_open()
            else:
                flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
                fd=os.open(self.root,flags);self.fds.append(fd)
                for part in self.parts[:-1]:
                    self.parent/=part;self.engine.check_local_write(self.parent)
                    try:os.mkdir(part,mode=0o700,dir_fd=fd)
                    except FileExistsError:pass
                    fd=os.open(part,flags,dir_fd=fd);self.fds.append(fd)
                self.fd=os.open(self.temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.fds[-1])
            return self
        except BaseException as exc:
            self.__exit__(type(exc),exc,exc.__traceback__);raise

    def _windows_open(self):
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.kernel=kernel
        def pin(path):
            # FILE_SHARE_DELETE deliberately absent: all destination ancestors stay pinned.
            handle=kernel.CreateFileW(str(path),0,3,None,3,0x02000000|0x00200000,None)
            if handle==wintypes.HANDLE(-1).value:raise OSError(ctypes.get_last_error(),'Cannot pin destination directory')
            self.handles.append(handle)
            if path.is_symlink() or path.is_junction():raise DevError('SYMLINK_BLOCKED','目标父目录是链接或联接',403)
        current=Path(self.root.anchor);pin(current)
        for part in self.root.parts[1:]:current/=part;pin(current)
        for part in self.parts[:-1]:
            self.parent/=part;self.engine.check_local_write(self.parent)
            self.parent.mkdir(mode=0o700,exist_ok=True);pin(self.parent)
        self.fd=os.open(self.parent/self.temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_BINARY,0o600)

    def write(self,data):
        view=memoryview(data)
        while view:
            written=os.write(self.fd,view)
            if written<=0:raise OSError('Short write')
            view=view[written:]

    def publish(self):
        os.fsync(self.fd)
        original=os.fstat(self.fd)
        if not stat.S_ISREG(original.st_mode) or original.st_nlink!=1:raise DevError('ARTIFACT_DESTINATION_CHANGED','临时文件身份异常')
        target=self.engine.path(self.root,'/'.join(self.parts),False)
        self.engine.check_local_write(target)
        # The on-disk name must still refer to the pinned directory/file.
        if os.name=='nt':
            if self.parent.resolve()!=self.parent:raise DevError('ARTIFACT_DESTINATION_CHANGED','目标路径已改变')
            current=os.stat(self.parent/self.temporary,follow_symlinks=False)
        else:
            parent_stat=self.parent.stat();pinned=os.fstat(self.fds[-1])
            if (parent_stat.st_dev,parent_stat.st_ino)!=(pinned.st_dev,pinned.st_ino):raise DevError('ARTIFACT_DESTINATION_CHANGED','下载期间目标目录发生变化')
            current=os.stat(self.temporary,dir_fd=self.fds[-1],follow_symlinks=False)
        if (current.st_dev,current.st_ino)!=(original.st_dev,original.st_ino):raise DevError('ARTIFACT_DESTINATION_CHANGED','临时文件被替换')
        try:
            if os.name=='nt':
                os.close(self.fd);self.fd=None
                # os.rename on Windows refuses an existing target.
                os.rename(self.parent/self.temporary,target)
            else:
                os.link(self.temporary,self.parts[-1],src_dir_fd=self.fds[-1],dst_dir_fd=self.fds[-1],follow_symlinks=False)
                os.unlink(self.temporary,dir_fd=self.fds[-1])
            self.published=True
        except FileExistsError as exc:raise DevError('ARTIFACT_DESTINATION_EXISTS','目标文件已存在，未覆盖',409) from exc
        self.engine.sync_directory(self.parent)

    def __exit__(self, exc_type, exc_value, traceback):
        # Cleanup failures must never strand pinned descriptors/Windows handles.
        # Preserve the primary download error, but do report standalone cleanup
        # failures instead of silently claiming a completely cleaned destination.
        failures = []
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except OSError as exc:
                failures.append(exc)
        if not self.published:
            try:
                if self.fds:
                    os.unlink(self.temporary, dir_fd=self.fds[-1])
                elif os.name == 'nt':
                    (self.parent / self.temporary).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                failures.append(exc)
        fds, self.fds = self.fds, []
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError as exc:
                failures.append(exc)
        handles, self.handles = self.handles, []
        for handle in reversed(handles):
            try:
                if not self.kernel.CloseHandle(handle):
                    raise OSError('Cannot close pinned destination handle')
            except OSError as exc:
                failures.append(exc)
        if failures:
            if exc_value is not None:
                exc_value.add_note('Destination cleanup also failed: ' + type(failures[0]).__name__)
            else:
                raise failures[0]


def import_artifact(engine,project,args,*,stream=None):
    root,_=engine.root(project,True)
    relative=relative_path(args['path'],False);destination=engine.path(root,relative,False)
    engine.check_local_write(destination)
    if destination.exists():raise DevError('ARTIFACT_DESTINATION_EXISTS','目标文件已存在；请选择未使用的路径',409)
    config=engine.config.get('integrations',{});limit=config.get('max_import_bytes',MAX_BYTES)
    file=args['file'];expected_size=file.get('size')
    if expected_size is not None and expected_size>limit:raise DevError('ARTIFACT_TOO_LARGE','原生文件大小超过本机上限')
    hosts=tuple(config.get('file_hosts',DEFAULT_HOSTS))
    validate_url(file['download_url'],hosts)
    total=0;sha=hashlib.sha256()
    with engine.mutation_lock,AnchoredDestination(engine,root,relative) as target:
        for block in (stream if stream is not None else download_chunks(file,hosts,limit)):
            if not isinstance(block,bytes):raise DevError('ARTIFACT_STREAM','下载流类型错误')
            total+=len(block)
            if total>limit:raise DevError('ARTIFACT_TOO_LARGE','下载超过本机文件大小上限')
            target.write(block);sha.update(block)
        if expected_size is not None and total!=expected_size:raise DevError('ARTIFACT_SIZE','原生文件大小与下载结果不一致')
        actual=sha.hexdigest()
        if args.get('expected_sha256') and args['expected_sha256']!=actual:raise DevError('ARTIFACT_INTEGRITY','SHA-256 不匹配，未发布文件')
        engine.root(project,True);target.publish()
    return {'path':relative,'bytes':total,'sha256':actual,'created':True,'overwritten':False,
            'extracted':False,'executed':False,'name':destination.name}
