"""Shared pinned-public-HTTPS file transport; no Agent filesystem dependencies."""

from __future__ import annotations
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit, urljoin
from shared.util import DevError
from shared.file_sources import normalize_file_host, source_metadata, source_host_allowed

DOWNLOAD_SECONDS = 180


def validate_url(value, hosts, *, stage="source_validation", providers=()):
    metadata = source_metadata(value)
    reason = "invalid_url"
    try:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 8192
            or not value.isascii()
            or any(ord(c) < 33 or ord(c) == 127 for c in value)
            or "\\" in value
        ):
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.scheme != "https":
            reason = "unsupported_scheme"
            raise ValueError()
        if (
            parsed.username is not None
            or parsed.password is not None
            or "#" in value
            or parsed.port not in (None, 443)
        ):
            raise ValueError()
        host = normalize_file_host(parsed.hostname or "")
        if not source_host_allowed(host, hosts, providers):
            reason = "host_not_allowed"
            raise ValueError()
    except (ValueError, UnicodeError):
        recovery = (
            "review_local_file_sources" if reason == "host_not_allowed" else "provide_native_file"
        )
        message = (
            "文件下载主机尚未获此节点批准；先用 source_check 预检并汇总同批来源，经本人核验批准后仅追加精确主机至 integrations.extra_file_hosts；不要覆盖 file_hosts 或关闭校验"
            if reason == "host_not_allowed"
            else "文件引用不是有效的原生 HTTPS 下载地址；请通过宿主重新提供文件"
        )
        raise DevError(
            "ARTIFACT_SOURCE_DENIED",
            message + "；未请求被拒绝的地址，未发布目标文件",
            403,
            **metadata,
            reason=reason,
            stage=stage,
            request_sent=False,
            recovery=recovery,
        ) from None
    return parsed, host


def remaining_timeout(deadline):
    if deadline is None:
        return 15
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DevError("ARTIFACT_TIMEOUT", "下载超时，临时文件未发布")
    return min(15, remaining)


# getaddrinfo has no portable timeout. Bound callers AND outstanding resolvers;
# daemon workers cannot delay shutdown if the OS resolver itself stays stuck.
_DNS_SLOTS = threading.BoundedSemaphore(4)


def resolve_addresses(host, port, deadline):
    metadata = {"source_host": host, "source_scheme": "https", "request_sent": False}
    timeout = remaining_timeout(deadline)
    expires = time.monotonic() + timeout
    slots = _DNS_SLOTS
    if not slots.acquire(timeout=timeout):
        raise DevError(
            "ARTIFACT_TIMEOUT",
            "DNS 解析资源繁忙；未请求文件、未发布目标",
            504,
            **metadata,
            reason="dns_timeout",
            stage="dns",
            recovery="check_agent_network",
        )
    done = threading.Event()
    result = []

    def resolve():
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception:
            result.append(None)
        finally:
            slots.release()
            done.set()

    try:
        threading.Thread(target=resolve, name="codepier-import-dns", daemon=True).start()
    except BaseException:
        slots.release()
        raise
    if not done.wait(max(0, expires - time.monotonic())):
        raise DevError(
            "ARTIFACT_TIMEOUT",
            "文件下载主机 DNS 解析超时；未发布目标",
            504,
            **metadata,
            reason="dns_timeout",
            stage="dns",
            recovery="check_agent_network",
        )
    if not result or result[0] is None:
        raise DevError(
            "ARTIFACT_NETWORK",
            "文件下载主机 DNS 解析失败；未发布目标文件",
            502,
            **metadata,
            reason="dns_failed",
            stage="dns",
            recovery="check_agent_network",
        )
    remaining_timeout(deadline)
    return result[0]


class PublicTLSConnection(http.client.HTTPSConnection):
    def connect(self):
        metadata = {"source_host": self.host, "source_scheme": "https", "request_sent": False}
        # Resolve once, validate ALL answers, connect only to those exact IPs.
        # Original hostname remains the TLS identity. Never inherit proxies.
        addresses = resolve_addresses(
            self.host, self.port, getattr(self, "download_deadline", None)
        )
        try:
            # IPv6's deprecated fec0::/10 site-local range is deliberately
            # excluded from ipaddress.is_private and can report is_global.
            public = bool(addresses) and all(
                (address := ipaddress.ip_address(a[4][0])).is_global
                and not address.is_multicast
                and not getattr(address, "is_site_local", False)
                and "%" not in a[4][0]
                for a in addresses
            )
        except (ValueError, IndexError):
            public = False
        if not public:
            raise DevError(
                "ARTIFACT_SOURCE_DENIED",
                "文件下载地址解析到非公网单播网络，已拒绝",
                403,
                **metadata,
                reason="non_public_address",
                stage="dns",
                recovery="check_agent_network",
            )
        reason = "connect_failed"
        for family, kind, proto, _, address in addresses[:8]:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(remaining_timeout(getattr(self, "download_deadline", None)))
                sock.connect(address)
                sock.settimeout(remaining_timeout(getattr(self, "download_deadline", None)))
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except ssl.SSLError:
                reason = "tls_failed"
                sock.close()
            except OSError:
                sock.close()
            except BaseException:
                sock.close()
                raise
        raise DevError(
            "ARTIFACT_NETWORK",
            "原生文件下载 TLS 校验失败；未发布目标文件"
            if reason == "tls_failed"
            else "原生文件下载连接失败；未发布目标文件",
            502,
            **metadata,
            reason=reason,
            stage="connect",
            recovery="check_agent_network",
        ) from None


def download_chunks(file, hosts, max_bytes, *, providers=()):
    url = file["download_url"]
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    for redirects in range(4):
        parsed, host = validate_url(
            url,
            hosts,
            stage="redirect_validation" if redirects else "source_validation",
            providers=providers,
        )
        connection = PublicTLSConnection(
            host, 443, timeout=remaining_timeout(deadline), context=ssl.create_default_context()
        )
        connection.download_deadline = deadline
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(
                "GET",
                path,
                headers={
                    "Accept-Encoding": "identity",
                    "User-Agent": "CodePier-Native-File/1",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            remaining_timeout(deadline)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirects == 3:
                    raise DevError("ARTIFACT_REDIRECT", "下载重定向数量无效，未发布文件")
                # Validate the raw Location too: urljoin can discard control characters.
                if any(ord(c) < 33 or ord(c) == 127 for c in location) or "\\" in location:
                    raise DevError(
                        "ARTIFACT_SOURCE_DENIED",
                        "下载重定向地址无效，未请求该地址",
                        403,
                        reason="invalid_url",
                        stage="redirect_validation",
                        request_sent=False,
                        recovery="provide_native_file",
                    )
                url = urljoin(url, location)
                validate_url(url, hosts, stage="redirect_validation", providers=providers)
                continue
            if response.status != 200:
                refresh = response.status in {401, 403, 404, 410}
                recovery = (
                    "refresh_native_file"
                    if refresh
                    else "retry_later"
                    if response.status in {408, 429, 500, 502, 503, 504}
                    else "check_file_source"
                )
                message = (
                    "文件链接无权访问或已失效；请通过宿主重新提供文件"
                    if refresh
                    else "文件源暂时不可用，请稍后重新发起导入"
                    if recovery == "retry_later"
                    else "文件源未返回完整下载响应"
                )
                raise DevError(
                    "ARTIFACT_DOWNLOAD_FAILED",
                    f"原生文件源返回 HTTP {response.status}；{message}；未发布文件",
                    502,
                    source_host=host,
                    source_scheme="https",
                    http_status=response.status,
                    reason="source_access_or_expiry" if refresh else "http_error",
                    stage="response",
                    request_sent=True,
                    recovery=recovery,
                )
            if response.getheader("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise DevError("ARTIFACT_ENCODING", "不接受未经大小校验的压缩下载响应")
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    expected = int(length)
                except ValueError:
                    raise DevError("ARTIFACT_SIZE", "无效下载长度") from None
                if not 0 <= expected <= max_bytes:
                    raise DevError("ARTIFACT_TOO_LARGE", "下载文件超出大小限制")
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
                    raise DevError("ARTIFACT_TOO_LARGE", "流式下载超过大小上限")
                yield block
            if expected is not None and total != expected:
                raise DevError("ARTIFACT_SIZE", "下载长度与响应不符")
            return
        except DevError:
            raise
        except (OSError, http.client.HTTPException, UnicodeError):
            raise DevError(
                "ARTIFACT_NETWORK",
                "文件传输中断；未把不完整文件发布为成功",
                502,
                source_host=host,
                source_scheme="https",
                reason="transfer_failed",
                stage="transfer",
                recovery="check_agent_network",
            ) from None
        finally:
            connection.close()
    raise DevError("ARTIFACT_REDIRECT", "重定向超过上限")
