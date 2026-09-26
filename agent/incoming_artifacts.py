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

# Compatibility exports; configuration and runtime share the same registry.
from shared.file_sources import (DEFAULT_FILE_HOSTS, DEFAULT_MAX_IMPORT_BYTES,
    file_source_hosts, normalize_file_host, source_metadata)

DEFAULT_HOSTS = DEFAULT_FILE_HOSTS
MAX_BYTES = DEFAULT_MAX_IMPORT_BYTES
DOWNLOAD_SECONDS = 180


def validate_url(value, hosts, *, stage='source_validation'):
    metadata = source_metadata(value)
    reason = 'invalid_url'
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 8192 or not value.isascii()
                or any(ord(c) < 33 or ord(c) == 127 for c in value) or '\\' in value):
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.scheme != 'https':
            reason = 'unsupported_scheme'
            raise ValueError()
        if (parsed.username is not None or parsed.password is not None or '#' in value
                or parsed.port not in (None, 443)):
            raise ValueError()
        host = normalize_file_host(parsed.hostname or '')
        if host not in hosts:
            reason = 'host_not_allowed'
            raise ValueError()
    except (ValueError, UnicodeError):
        recovery = 'review_local_file_sources' if reason == 'host_not_allowed' else 'provide_native_file'
        message = ('文件下载主机不在本机允许的来源中；请核对来源策略或由主理人批准精确主机'
                   if reason == 'host_not_allowed' else '文件引用不是有效的原生 HTTPS 下载地址；请通过宿主重新提供文件')
        raise DevError('ARTIFACT_SOURCE_DENIED', message + '；未请求被拒绝的地址，未发布目标文件', 403,
            **metadata, reason=reason, stage=stage, request_sent=False, recovery=recovery) from None
    return parsed, host


def remaining_timeout(deadline):
    if deadline is None:
        return 15
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DevError('ARTIFACT_TIMEOUT', '下载超时，临时文件未发布')
    return min(15, remaining)


class PublicTLSConnection(http.client.HTTPSConnection):
    def connect(self):
        metadata = {'source_host': self.host, 'source_scheme': 'https', 'request_sent': False}
        # Resolve once, validate ALL answers, connect only to those exact IPs.
        # Original hostname remains the TLS identity. Never inherit proxies.
        try:
            addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        except OSError:
            raise DevError('ARTIFACT_NETWORK', '文件下载主机 DNS 解析失败；未发布目标文件', 502,
                **metadata, reason='dns_failed', stage='dns', recovery='check_agent_network') from None
        try:
            public = bool(addresses) and all(
                (address := ipaddress.ip_address(a[4][0])).is_global and not address.is_multicast
                and '%' not in a[4][0] for a in addresses)
        except (ValueError, IndexError):
            public = False
        if not public:
            raise DevError('ARTIFACT_SOURCE_DENIED', '文件下载地址解析到非公网单播网络，已拒绝', 403,
                **metadata, reason='non_public_address', stage='dns', recovery='check_agent_network')
        reason = 'connect_failed'
        for family, kind, proto, _, address in addresses[:8]:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(remaining_timeout(getattr(self, 'download_deadline', None)))
                sock.connect(address)
                sock.settimeout(remaining_timeout(getattr(self, 'download_deadline', None)))
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except ssl.SSLError:
                reason = 'tls_failed'
                sock.close()
            except OSError:
                sock.close()
            except BaseException:
                sock.close()
                raise
        raise DevError('ARTIFACT_NETWORK', '原生文件下载 TLS 校验失败；未发布目标文件' if reason == 'tls_failed'
            else '原生文件下载连接失败；未发布目标文件', 502,
            **metadata, reason=reason, stage='connect', recovery='check_agent_network') from None


def download_chunks(file, hosts, max_bytes):
    url = file['download_url']
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    for redirects in range(4):
        parsed, host = validate_url(url, hosts, stage='redirect_validation' if redirects else 'source_validation')
        connection = PublicTLSConnection(host, 443, timeout=remaining_timeout(deadline), context=ssl.create_default_context())
        connection.download_deadline = deadline
        try:
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path, headers={'Accept-Encoding': 'identity',
                'User-Agent': 'CodePier-Native-File/1', 'Connection': 'close'})
            response = connection.getresponse()
            remaining_timeout(deadline)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location or redirects == 3:
                    raise DevError('ARTIFACT_REDIRECT', '下载重定向数量无效，未发布文件')
                # Validate the raw Location too: urljoin can discard control characters.
                if any(ord(c) < 33 or ord(c) == 127 for c in location) or '\\' in location:
                    raise DevError('ARTIFACT_SOURCE_DENIED', '下载重定向地址无效，未请求该地址', 403,
                        reason='invalid_url', stage='redirect_validation', request_sent=False,
                        recovery='provide_native_file')
                url = urljoin(url, location)
                validate_url(url, hosts, stage='redirect_validation')
                continue
            if response.status != 200:
                refresh = response.status in {401, 403, 404, 410}
                recovery = 'refresh_native_file' if refresh else 'retry_later' if response.status in {408, 429, 500, 502, 503, 504} else 'check_file_source'
                message = ('文件链接无权访问或已失效；请通过宿主重新提供文件' if refresh
                           else '文件源暂时不可用，请稍后重新发起导入' if recovery == 'retry_later' else '文件源未返回完整下载响应')
                raise DevError('ARTIFACT_DOWNLOAD_FAILED', f'原生文件源返回 HTTP {response.status}；{message}；未发布文件', 502,
                    source_host=host, source_scheme='https', http_status=response.status,
                    reason='source_access_or_expiry' if refresh else 'http_error', stage='response',
                    request_sent=True, recovery=recovery)
            if response.getheader('Content-Encoding', 'identity').lower() not in {'identity', ''}:
                raise DevError('ARTIFACT_ENCODING', '不接受未经大小校验的压缩下载响应')
            length = response.getheader('Content-Length')
            if length is not None:
                try:
                    expected = int(length)
                except ValueError:
                    raise DevError('ARTIFACT_SIZE', '无效下载长度') from None
                if not 0 <= expected <= max_bytes:
                    raise DevError('ARTIFACT_TOO_LARGE', '下载文件超出大小限制')
            else:
                expected = None
            total = 0
            while True:
                timeout = remaining_timeout(deadline)
                if connection.sock is not None:
                    connection.sock.settimeout(timeout)
                # read1 performs at most one raw read, so slow trickle responses
                # cannot hide indefinitely inside a large buffered read().
                block = response.read1(65536)
                remaining_timeout(deadline)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise DevError('ARTIFACT_TOO_LARGE', '流式下载超过大小上限')
                yield block
            if expected is not None and total != expected:
                raise DevError('ARTIFACT_SIZE', '下载长度与响应不符')
            return
        except DevError:
            raise
        except (OSError, http.client.HTTPException, UnicodeError):
            raise DevError('ARTIFACT_NETWORK', '文件传输中断；未把不完整文件发布为成功', 502,
                source_host=host, source_scheme='https', reason='transfer_failed',
                stage='transfer', recovery='check_agent_network') from None
        finally:
            connection.close()
    raise DevError('ARTIFACT_REDIRECT', '重定向超过上限')


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
    hosts=file_source_hosts(config)
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
