"""Bounded real LSP stdio client with resident pooling and incremental sync.

Servers are configured by the local owner, never supplied by a tool argument.
Pooled servers stay warm across queries per (project_root, language) with idle
TTL, LRU eviction, memory-pressure reclamation, and clean respawn on crash.
"""

from __future__ import annotations
import asyncio, contextlib, hashlib, json, os, shutil, time, weakref
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit, unquote
from agent.async_work import disk_call as owned_disk_call
from agent.owned_subprocess import OwnedProcess, create_owned_process
from agent.resource_queue import finish_cleanup
from shared.config import env_float, env_int
from shared.execution_policy import enforce_argv
from shared.util import DevError, valid_json_value

LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascriptreact",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".vue": "vue",
}
MAX_MESSAGE = 4 * 1024 * 1024
DEFAULT_IDLE_TTL = 300.0
DEFAULT_MAX_INSTANCES = 3
MAX_OPEN_DOCUMENTS = 64
MAX_TRACKED_DOCUMENT_URIS = 4096
MAX_DIAGNOSTIC_REPORTS = 16
PoolKey = tuple[str, str, int, int]
_LIVE_POOLS: weakref.WeakSet[LspPool] = weakref.WeakSet()


def _server_language(config, language):
    servers = config.get("integrations", {}).get("language_servers", {})
    if language not in servers and isinstance(language, str) and language.endswith("react"):
        return language[:-5]
    return language


def _spec_fingerprint(spec):
    return json.dumps(
        {
            key: spec.get(key)
            for key in ("command", "env", "settings", "initialization_options", "timeout_seconds")
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def selected(config, project, language):
    servers = config.get("integrations", {}).get("language_servers", {})
    spec = servers.get(language)
    if spec is None and isinstance(language, str) and language.endswith("react"):
        spec = servers.get(language[:-5])
    if not spec or not spec.get("enabled", True):
        return None
    projects = spec.get("projects", [])
    if (
        "*" not in projects
        and project.get("alias") not in projects
        and project.get("id") not in projects
    ):
        return None
    return spec


def status(config, project):
    rows = []
    for language in sorted(config.get("integrations", {}).get("language_servers", {})):
        spec = selected(config, project, language)
        if spec:
            executable = shutil.which(spec["command"][0])
            rows.append(
                {
                    "language": language,
                    "configured": True,
                    "executable_available": bool(executable),
                    "protocol_verified": False,
                    "reason": "本次只探测程序存在，未执行协议查询"
                    if executable
                    else "语言服务程序未找到",
                }
            )
    return {
        "servers": rows,
        "starts_process": False,
        "semantic_queries_available": any(r["executable_available"] for r in rows),
        "column_unit": "one-based Unicode scalar",
        "note": "查询会启动配置的语言服务；不是操作系统沙箱。",
    }


class Peer:
    def __init__(self, agent, identifier, project, root, spec):
        self.agent, self.identifier, self.project, self.root, self.spec = (
            agent,
            identifier,
            project,
            root,
            spec,
        )
        self.process = None
        self.reader = None
        self.stderr = None
        self.counter = 0
        self.pending = {}
        self.diagnostics = OrderedDict()
        self.changed = asyncio.Event()
        self.notifications = 0
        self.error = None
        self.capabilities = {}
        self.documents = OrderedDict()
        self.document_counter = 0
        self.opened_uris: set[str] = set()
        self.write_lock = asyncio.Lock()
        self.close_lock = asyncio.Lock()
        self.closed = False

    def is_alive(self) -> bool:
        return (
            not self.closed
            and self.error is None
            and self.process is not None
            and self.process.returncode is None
            and (self.reader is None or not self.reader.done())
            and (self.process.stdin is None or not self.process.stdin.is_closing())
        )

    async def start(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "PATH",
                "HOME",
                "USER",
                "USERPROFILE",
                "SYSTEMROOT",
                "SystemRoot",
                "TEMP",
                "TMP",
                "TMPDIR",
                "LANG",
                "LC_ALL",
            }
        }
        env.update(self.spec.get("env", {}))
        command = self.spec["command"]
        enforce_argv(self.agent.config, self.project, command, self.root, env)
        kwargs = (
            {"start_new_session": True}
            if os.name != "nt"
            else {"creationflags": __import__("subprocess").CREATE_NEW_PROCESS_GROUP}
        )
        spawn = asyncio.create_subprocess_exec if os.name == "nt" else create_owned_process
        spawning = asyncio.create_task(
            spawn(
                *command,
                cwd=self.root,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_MESSAGE + 8192,
                **kwargs,
            )
        )
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            await finish_cleanup(self._close_after_spawn(spawning))
            raise
        except OSError as exc:
            raise DevError("LSP_UNAVAILABLE", "无法启动已配置语言服务", 409) from exc
        self.agent.processes[self.identifier] = self.process
        self.reader = asyncio.create_task(self.read_loop())
        self.stderr = asyncio.create_task(self.drain_stderr())
        try:
            response = await self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": self.root.as_uri(),
                    "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-16"]},
                        "workspace": {
                            "configuration": True,
                            "workspaceFolders": True,
                            "applyEdit": False,
                        },
                        "textDocument": {
                            "publishDiagnostics": {"versionSupport": True},
                            "hover": {"contentFormat": ["plaintext", "markdown"]},
                            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                            "diagnostic": {},
                        },
                    },
                    "initializationOptions": self.spec.get("initialization_options", {}),
                },
            )
            if not isinstance(response, dict):
                raise DevError("LSP_PROTOCOL", "initialize 返回类型无效")
            capabilities = response.get("capabilities", {})
            if (
                not isinstance(capabilities, dict)
                or capabilities.get("positionEncoding", "utf-16") != "utf-16"
            ):
                raise DevError("LSP_ENCODING", "语言服务未使用协商的 UTF-16 坐标")
            await self.notify("initialized", {})
            if self.spec.get("settings"):
                await self.notify(
                    "workspace/didChangeConfiguration", {"settings": self.spec["settings"]}
                )
            self.capabilities = capabilities
            return capabilities
        except BaseException:
            await self.close()
            raise

    async def _close_after_spawn(self, spawning):
        self.process = await spawning
        await self.close()

    async def send(self, message):
        body = json.dumps(message, ensure_ascii=False).encode()
        if len(body) > MAX_MESSAGE:
            raise DevError("LSP_LIMIT", "语言服务请求超过大小限制")
        async with self.write_lock:
            if not self.process or self.process.returncode is not None:
                raise DevError("LSP_DISCONNECTED", "语言服务已退出", 409)
            try:
                self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                self.error = DevError("LSP_DISCONNECTED", "语言服务已退出", 409)
                raise self.error from exc

    async def notify(self, method, params):
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method, params):
        if self.error:
            raise self.error
        self.counter += 1
        identifier = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send(
                {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, self.spec.get("timeout_seconds", 20))
        except asyncio.TimeoutError as exc:
            raise DevError("LSP_TIMEOUT", "语言服务未在期限内返回；没有伪造空结果", 409) from exc
        finally:
            self.pending.pop(identifier, None)

    async def read_loop(self):
        try:
            while True:
                header = await self.process.stdout.readuntil(b"\r\n\r\n")
                if len(header) > 8192:
                    raise DevError("LSP_PROTOCOL", "语言服务头部过长")
                lengths = [
                    l.split(b":", 1)[1].strip()
                    for l in header.split(b"\r\n")
                    if l.lower().startswith(b"content-length:")
                ]
                if len(lengths) != 1:
                    raise DevError("LSP_PROTOCOL", "语言服务长度头无效")
                try:
                    length = int(lengths[0])
                except ValueError as exc:
                    raise DevError("LSP_PROTOCOL", "无效消息长度") from exc
                if not 0 < length <= MAX_MESSAGE:
                    raise DevError("LSP_LIMIT", "语言服务响应超过 4 MiB")
                message = json.loads(await self.process.stdout.readexactly(length))
                if not isinstance(message, dict) or not valid_json_value(message):
                    raise DevError("LSP_PROTOCOL", "语言服务返回无效 JSON")
                if "method" in message:
                    self.notifications += 1
                    if self.notifications > 20000:
                        raise DevError("LSP_LIMIT", "语言服务通知超过预算")
                    if "id" in message:
                        await self.server_request(message)
                    elif message["method"] == "textDocument/publishDiagnostics":
                        self._accept_diagnostics(message.get("params", {}))
                elif type(message.get("id")) is int and message["id"] in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            err = message["error"]
                            code = (
                                "LSP_UNSUPPORTED"
                                if isinstance(err, dict) and err.get("code") == -32601
                                else "LSP_QUERY_FAILED"
                            )
                            future.set_exception(
                                DevError(code, "语言服务拒绝该查询；没有退回同名文本匹配", 409)
                            )
                        else:
                            future.set_result(message.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = (
                exc
                if isinstance(exc, DevError)
                else DevError("LSP_DISCONNECTED", "语言服务流中断或协议无效", 409)
            )
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(self.error)
            self.changed.set()

    def _accept_diagnostics(self, report):
        if not isinstance(report, dict) or not isinstance(report.get("diagnostics"), list):
            return
        uri = report.get("uri")
        if not isinstance(uri, str) or uri not in self.documents:
            return
        doc = self.documents[uri]
        version = report.get("version")
        # An explicitly versioned report must match. Once content changes or
        # a document is reopened, an unversioned report cannot prove freshness.
        if version is None:
            if not doc["accept_unversioned"]:
                return
        elif type(version) is not int or version != doc["version"]:
            return
        self.diagnostics[uri] = report
        self.diagnostics.move_to_end(uri)
        while len(self.diagnostics) > MAX_DIAGNOSTIC_REPORTS:
            self.diagnostics.popitem(last=False)
        self.changed.set()

    async def wait_diagnostics(self, uri, version, budget):
        deadline = time.monotonic() + budget
        while not self.error:
            report = self.diagnostics.get(uri)
            doc = self.documents.get(uri, {})
            if report is not None and doc.get("version") == version:
                return report
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                break
        return None

    async def server_request(self, message):
        method = message["method"]
        p = message.get("params", {})
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "workspace/configuration":
            settings = self.spec.get("settings", {})
            items = p.get("items", []) if isinstance(p, dict) else []
            values = []
            for item in items[:100]:
                value = settings
                for key in (item.get("section") or "").split("."):
                    if key:
                        value = value.get(key) if isinstance(value, dict) else None
                values.append(value)
            reply["result"] = values
        elif method == "workspace/workspaceFolders":
            reply["result"] = [{"uri": self.root.as_uri(), "name": self.root.name}]
        elif method == "workspace/applyEdit":
            reply["result"] = {
                "applied": False,
                "failureReason": "CodePier semantic navigation does not apply server edits",
            }
        elif method in {
            "window/workDoneProgress/create",
            "client/registerCapability",
            "client/unregisterCapability",
        }:
            reply["result"] = None
        else:
            reply["error"] = {"code": -32601, "message": "Unsupported read-only client callback"}
        await self.send(reply)

    async def await_source_diagnostics(self, uri, capabilities, *, expected_version=1):
        """Do not query a cold workspace index before the seed was analyzed.

        A positive protocol observation is required, never an arbitrary sleep or
        an empty workspace/symbol result treated as a completed index scan.
        """
        if capabilities.get("diagnosticProvider"):
            result = await self.request("textDocument/diagnostic", {"textDocument": {"uri": uri}})
            if not isinstance(result, dict) or result.get("kind") != "full":
                raise DevError(
                    "LSP_NOT_READY", "语言服务尚未确认当前源码的分析结果，请稍后重试", 409
                )
            return "source_diagnostics_observed"
        report = await self.wait_diagnostics(
            uri, expected_version, self.spec.get("timeout_seconds", 20)
        )
        if report is None:
            if self.error:
                raise self.error
            raise DevError("LSP_NOT_READY", "语言服务尚未确认当前源码的分析结果，请稍后重试", 409)
        return "source_diagnostics_observed"

    async def sync_document(
        self, *, uri: str, language_id: str, source: str, sha: str, stat_info=None, force=False
    ) -> int:
        mtime_ns = getattr(stat_info, "st_mtime_ns", None)
        size = getattr(stat_info, "st_size", None)
        existing = self.documents.get(uri)
        if existing is not None and existing["sha256"] == sha and not force:
            existing["mtime_ns"] = mtime_ns
            existing["size"] = size
            self.documents.move_to_end(uri)
            return existing["version"]
        self.document_counter += 1
        version = self.document_counter
        self.diagnostics.pop(uri, None)
        if existing is None:
            # Version sequence is peer-wide; first-open eligibility is per URI.
            # Never evict this history while old server notifications can arrive.
            first_open = (
                uri not in self.opened_uris and len(self.opened_uris) < MAX_TRACKED_DOCUMENT_URIS
            )
            if first_open:
                self.opened_uris.add(uri)
            if len(self.documents) >= MAX_OPEN_DOCUMENTS:
                await self._close_document(next(iter(self.documents)))
            self.documents[uri] = {
                "sha256": sha,
                "version": version,
                "language_id": language_id,
                "mtime_ns": mtime_ns,
                "size": size,
                "accept_unversioned": first_open,
            }
            await self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": version,
                        "text": source,
                    }
                },
            )
            return version
        existing.update(
            sha256=sha, version=version, mtime_ns=mtime_ns, size=size, accept_unversioned=False
        )
        self.documents.move_to_end(uri)
        sync = self.capabilities.get("textDocumentSync")
        change_kind = sync.get("change", 1) if isinstance(sync, dict) else sync
        if change_kind == 0:
            await self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            await self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": existing["language_id"],
                        "version": version,
                        "text": source,
                    }
                },
            )
        else:
            await self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": source}],
                },
            )
        await self.notify(
            "workspace/didChangeWatchedFiles",
            {"changes": [{"uri": uri, "type": 2}]},
        )
        return version

    async def _close_document(self, uri):
        self.documents.pop(uri, None)
        self.diagnostics.pop(uri, None)
        await self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def _document_change(self, uri, doc):
        engine = self.agent.engine
        path = _uri_to_path(engine, self.root, uri)
        if path is None:
            return None
        try:
            st = path.lstat()
            if (st.st_mtime_ns, st.st_size) == (doc["mtime_ns"], doc["size"]):
                return ()
            raw = engine.read_bytes(path)
            return st, engine.text(raw), hashlib.sha256(raw).hexdigest()
        except (DevError, OSError):
            return None

    async def refresh_other_documents(self, active_uri: str | None):
        disk_call = getattr(self.agent, "disk_call", owned_disk_call)
        changed = False
        for open_uri, doc in list(self.documents.items()):
            if open_uri == active_uri:
                continue
            update = await disk_call(self._document_change, open_uri, dict(doc))
            if update is None:
                changed = True
                self.diagnostics.clear()
                await self._close_document(open_uri)
                await self.notify(
                    "workspace/didChangeWatchedFiles",
                    {"changes": [{"uri": open_uri, "type": 3}]},
                )
            elif update:
                st, source, sha = update
                if sha != doc["sha256"]:
                    changed = True
                    self.diagnostics.clear()
                    await self.sync_document(
                        uri=open_uri,
                        language_id=doc["language_id"],
                        source=source,
                        sha=sha,
                        stat_info=st,
                    )
                else:
                    doc.update(mtime_ns=st.st_mtime_ns, size=st.st_size)
        return changed

    async def drain_stderr(self):
        while await self.process.stderr.read(65536):
            pass

    async def close(self):
        await finish_cleanup(self._close())

    async def _close(self):
        async with self.close_lock:
            self.closed = True
            process = self.process
            try:
                if process is not None:
                    await self._stop_process(process)
            finally:
                await self._release_io(process)

    async def _stop_process(self, process):
        try:
            if process.returncode is None and self.reader and not self.reader.done():
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(2):
                        await self.request("shutdown", None)
                        await self.notify("exit", {})
                        await process.wait()
        finally:
            await self.agent.kill_process(process, include_finished=True)

    async def _release_io(self, process):
        try:
            if isinstance(process, OwnedProcess):
                process.close_stdio()
            tasks = [task for task in (self.reader, self.stderr) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.reader = self.stderr = self.process = None
            self.documents.clear()
            self.opened_uris.clear()
            self.diagnostics.clear()
            if process is not None:
                for key, current in list(self.agent.processes.items()):
                    if current is process:
                        self.agent.processes.pop(key, None)


class Projector:
    def __init__(self, engine, root, limit):
        self.engine, self.root, self.limit = engine, root, limit
        self.count = 0
        self.omitted = 0
        self.cache = {}

    def path(self, uri):
        try:
            p = urlsplit(uri)
            if p.scheme != "file" or p.netloc not in {"", "localhost"} or p.query or p.fragment:
                raise ValueError()
            text = unquote(p.path)
            if os.name == "nt" and len(text) > 2 and text[0] == "/" and text[2] == ":":
                text = text[1:]
            rel = Path(text).relative_to(self.root).as_posix()
            actual = self.engine.path(self.root, rel, False)
            if not actual.is_file():
                raise ValueError()
            return rel
        except (ValueError, TypeError, DevError):
            self.omitted += 1
            return None

    def source(self, path):
        if path not in self.cache:
            self.cache[path] = self.engine.text(
                self.engine.read_bytes(self.engine.path(self.root, path, False))
            ).split("\n")
        return self.cache[path]

    def position(self, path, p):
        if (
            not isinstance(p, dict)
            or type(p.get("line")) is not int
            or type(p.get("character")) is not int
        ):
            raise ValueError()
        lines = self.source(path)
        line = p["line"]
        units = p["character"]
        if not 0 <= line < len(lines) or units < 0:
            raise ValueError()
        text = lines[line]
        offset = 0
        column = 0
        for char in text:
            if offset == units:
                break
            offset += len(char.encode("utf-16-le")) // 2
            column += 1
            if offset > units:
                raise ValueError()
        if offset != units:
            raise ValueError()
        return {"line": line + 1, "column": column + 1}

    def range(self, path, value):
        start = self.position(path, value["start"])
        end = self.position(path, value["end"])
        if (end["line"], end["column"]) < (start["line"], start["column"]):
            raise ValueError()
        return {"start": start, "end": end}

    def location(self, item):
        if not isinstance(item, dict):
            self.omitted += 1
            return None
        path = self.path(item.get("uri") or item.get("targetUri") or "")
        if path is None:
            return None
        try:
            return {
                "path": path,
                "range": self.range(path, item.get("targetSelectionRange") or item.get("range")),
            }
        except (ValueError, KeyError, TypeError, DevError):
            self.omitted += 1
            return None

    def take(self, item):
        if self.count >= self.limit:
            self.omitted += 1
            return False
        self.count += 1
        return True

    def symbols(self, items, path=None, depth=0):
        if items is None:
            return []
        if not isinstance(items, list):
            raise DevError("LSP_PROTOCOL", "符号响应必须是数组")
        if depth > 24:
            self.omitted += len(items)
            return []
        result = []
        for item in items:
            if not isinstance(item, dict):
                self.omitted += 1
                continue
            loc = self.location(item.get("location", {})) if "location" in item else None
            if loc is None and path and "location" not in item:
                try:
                    loc = {
                        "path": path,
                        "range": self.range(path, item.get("selectionRange") or item["range"]),
                    }
                except (ValueError, KeyError, TypeError, DevError):
                    self.omitted += 1
                    continue
            if loc and self.take(item):
                row = {
                    **loc,
                    "name": str(item.get("name", ""))[:300],
                    "kind": item.get("kind"),
                    "detail": str(item.get("detail", ""))[:1000],
                }
                if item.get("children"):
                    row["children"] = self.symbols(item["children"], loc["path"], depth + 1)
                result.append(row)
        return result


def _uri_to_path(engine, root, uri):
    try:
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        text = unquote(parsed.path)
        if os.name == "nt" and len(text) > 2 and text[0] == "/" and text[2] == ":":
            text = text[1:]
        rel = Path(text).relative_to(root).as_posix()
        actual = engine.path(root, rel, False)
        return actual if actual.is_file() else None
    except (ValueError, TypeError, DevError, OSError):
        return None


_FATAL_POOL_ERRORS = frozenset(
    {
        "LSP_TIMEOUT",
        "LSP_DISCONNECTED",
        "LSP_PROTOCOL",
        "LSP_LIMIT",
        "LSP_ENCODING",
        "LSP_UNAVAILABLE",
    }
)


class _PoolEntry:
    def __init__(self, key: PoolKey, spec_fingerprint: str, clock):
        self.key = key
        self.spec_fingerprint = spec_fingerprint
        self.lock = asyncio.Lock()
        self.peer: Peer | None = None
        self.last_used: float = clock()
        self.waiters: int = 0

    @property
    def in_use(self) -> bool:
        return self.waiters > 0 or self.lock.locked()

    async def close(self):
        peer = self.peer
        self.peer = None
        if peer is not None:
            await peer.close()


class LspPool:
    """Bounded resident pool of LSP servers keyed by (project_root, language)."""

    def __init__(
        self,
        *,
        idle_ttl: float | None = None,
        max_instances: int | None = None,
        clock=time.monotonic,
    ):
        self.idle_ttl = (
            float(idle_ttl)
            if idle_ttl is not None
            else env_float("CODEPIER_LSP_IDLE_TTL_SECONDS", DEFAULT_IDLE_TTL, 0.01, 86400.0)
        )
        self.max_instances = (
            int(max_instances)
            if max_instances is not None
            else env_int("CODEPIER_LSP_MAX_INSTANCES", DEFAULT_MAX_INSTANCES, 1, 32)
        )
        self.clock = clock
        self._entries: OrderedDict[PoolKey, _PoolEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None
        self._reaper_wake = asyncio.Event()
        self._loop = None
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        _LIVE_POOLS.add(self)

    def _sync_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # Peers of the previous loop cannot be awaited from this one; stop
            # their language-server processes instead of orphaning them.
            for entry in self._entries.values():
                process = getattr(entry.peer, "process", None)
                if process is not None and getattr(process, "returncode", 0) is None:
                    with contextlib.suppress(Exception):
                        process.kill()
            self._entries.clear()
            self._reaper_task = None
            self._reaper_wake = asyncio.Event()
            self._loop = loop

    def _ensure_reaper(self):
        if self.idle_ttl <= 0 or not self._entries:
            return
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())
        else:
            self._reaper_wake.set()

    async def _close_entries(self, entries):
        if entries:
            await finish_cleanup(self._close_entries_owned(entries))

    async def _close_entries_owned(self, entries):
        for entry in entries:
            with contextlib.suppress(Exception):
                await entry.close()

    def _pop_expired_locked(self, now: float) -> list[_PoolEntry]:
        expired_keys = [
            key
            for key, entry in self._entries.items()
            if not entry.in_use
            and (
                entry.peer is None
                or not entry.peer.is_alive()
                or (now - entry.last_used) >= self.idle_ttl
            )
        ]
        to_close = [self._entries.pop(key) for key in expired_keys]
        self.evictions += len(to_close)
        return to_close

    async def _reaper_loop(self):
        try:
            while True:
                now = self.clock()
                next_wait = None
                async with self._lock:
                    if not self._entries:
                        self._reaper_task = None
                        return
                    to_close = self._pop_expired_locked(now)
                    for entry in self._entries.values():
                        if not entry.in_use:
                            remaining = (entry.last_used + self.idle_ttl) - now
                            if next_wait is None or remaining < next_wait:
                                next_wait = remaining
                    if not self._entries and not to_close:
                        self._reaper_task = None
                        return
                    self._reaper_wake.clear()
                await self._close_entries(to_close)
                async with self._lock:
                    if not self._entries:
                        self._reaper_task = None
                        return
                wait_seconds = (
                    max(0.01, next_wait) if next_wait is not None else max(0.05, self.idle_ttl)
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._reaper_wake.wait(), wait_seconds)
        except asyncio.CancelledError:
            raise

    async def evict_for_memory_pressure(self, *, keep: int = 0) -> int:
        self._sync_loop()
        target_keep = max(0, keep)
        async with self._lock:
            idle_keys = [key for key, entry in self._entries.items() if not entry.in_use]
            drop_count = max(0, len(idle_keys) - target_keep)
            to_close = [self._entries.pop(key) for key in idle_keys[:drop_count]]
            self.evictions += len(to_close)
        await self._close_entries(to_close)
        return len(to_close)

    async def shutdown_all(self) -> int:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            self._entries.clear()
            self._reaper_task = None
            self._loop = loop
            return 0
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            reaper = self._reaper_task
            self._reaper_task = None
            self._reaper_wake.set()
        closed = sum(entry.peer is not None for entry in entries)
        await finish_cleanup(self._shutdown_entries(reaper, entries))
        return closed

    async def _shutdown_entries(self, reaper, entries):
        if reaper is not None and reaper is not asyncio.current_task():
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reaper
        await self._close_entries(entries)

    async def _checkout(self, key: PoolKey, spec_fp: str) -> tuple[_PoolEntry, bool]:
        self._sync_loop()
        now = self.clock()
        async with self._lock:
            to_close = self._pop_expired_locked(now)
            entry = self._entries.get(key)
            if entry is not None and not entry.in_use:
                if entry.spec_fingerprint != spec_fp or (
                    entry.peer is not None and not entry.peer.is_alive()
                ):
                    to_close.append(self._entries.pop(key))
                    self.evictions += 1
                    entry = None
            if entry is None:
                if len(self._entries) >= self.max_instances:
                    lru_key = next(
                        (k for k, item in self._entries.items() if not item.in_use), None
                    )
                    if lru_key is not None:
                        to_close.append(self._entries.pop(lru_key))
                        self.evictions += 1
                if len(self._entries) < self.max_instances:
                    entry = _PoolEntry(key, spec_fp, self.clock)
                    self._entries[key] = entry
                    pooled = True
                else:
                    entry = _PoolEntry(key, spec_fp, self.clock)
                    pooled = False
            else:
                self._entries.move_to_end(key)
                pooled = True
            entry.waiters += 1
        try:
            await self._close_entries(to_close)
        except BaseException:
            entry.waiters -= 1
            async with self._lock:
                if not entry.in_use and entry.peer is None and self._entries.get(key) is entry:
                    self._entries.pop(key, None)
            raise
        return entry, pooled

    async def _spawn_peer(
        self, entry: _PoolEntry, agent, identifier, project, root, config: dict, spec_fp: str
    ) -> Peer:
        _raise_if_stopped(agent, identifier)
        self.misses += 1
        peer = Peer(agent, identifier, project, root, config)
        await peer.start()
        entry.peer = peer
        entry.spec_fingerprint = spec_fp
        return peer

    async def _release_query_peer(self, agent, identifier, key, entry, pooled):
        if entry.peer is None or agent.processes.get(identifier) is entry.peer.process:
            agent.processes.pop(identifier, None)
        if not pooled or entry.peer is None or not entry.peer.is_alive():
            async with self._lock:
                if self._entries.get(key) is entry:
                    self._entries.pop(key, None)
            await entry.close()
        else:
            async with self._lock:
                if self._entries.get(key) is entry:
                    entry.last_used = self.clock()
                    self._entries.move_to_end(key)
                    self._ensure_reaper()

    async def _run_peer_request(
        self,
        entry,
        agent,
        identifier,
        project,
        root,
        config,
        spec_fp,
        runner,
        reused,
        admission=None,
    ):
        _raise_if_stopped(agent, identifier)
        entry.peer.notifications = 0
        agent.processes[identifier] = entry.peer.process
        try:
            return await runner(entry.peer)
        except DevError as exc:
            if reused and exc.code == "LSP_DISCONNECTED":
                _raise_if_stopped(agent, identifier)
                await entry.close()
                if admission is not None:
                    await admission()
                peer = await self._spawn_peer(
                    entry, agent, identifier, project, root, config, spec_fp
                )
                agent.processes[identifier] = peer.process
                return await runner(peer)
            raise

    async def _ensure_query_peer(self, entry, agent, identifier, project, root, config, spec_fp):
        if entry.peer is not None and (
            not entry.peer.is_alive()
            or entry.spec_fingerprint != spec_fp
            or len(entry.peer.opened_uris) >= MAX_TRACKED_DOCUMENT_URIS
        ):
            await entry.close()
        reused = entry.peer is not None
        if entry.peer is None:
            await self._spawn_peer(entry, agent, identifier, project, root, config, spec_fp)
        else:
            self.hits += 1
        return reused

    async def run_query(
        self,
        agent,
        identifier,
        project,
        root,
        key: PoolKey,
        config: dict,
        runner,
        *,
        admission=None,
    ):
        spec_fp = _spec_fingerprint(config)
        phase = getattr(agent, "phase", None)
        reported_waiting = False
        while True:
            _raise_if_stopped(agent, identifier)
            entry, pooled = await self._checkout(key, spec_fp)
            try:
                if callable(phase) and entry.lock.locked() and not reported_waiting:
                    phase(
                        identifier,
                        "waiting_resource",
                        queue_reason="lsp_busy",
                        resource_kinds=["service"],
                    )
                    reported_waiting = True
                async with entry.lock:
                    if pooled and self._entries.get(key) is not entry:
                        continue
                    if reported_waiting:
                        phase(identifier, "executing")
                    if admission is not None:
                        await admission()
                    reused = await self._ensure_query_peer(
                        entry, agent, identifier, project, root, config, spec_fp
                    )
                    try:
                        return await self._run_peer_request(
                            entry,
                            agent,
                            identifier,
                            project,
                            root,
                            config,
                            spec_fp,
                            runner,
                            reused,
                            admission,
                        )
                    except DevError as exc:
                        if exc.code in _FATAL_POOL_ERRORS or (
                            entry.peer is not None and not entry.peer.is_alive()
                        ):
                            async with self._lock:
                                if self._entries.get(key) is entry:
                                    self._entries.pop(key, None)
                                    self.evictions += 1
                            await entry.close()
                        raise
                    except BaseException:
                        async with self._lock:
                            if self._entries.get(key) is entry:
                                self._entries.pop(key, None)
                                self.evictions += 1
                        await entry.close()
                        raise
                    finally:
                        await self._release_query_peer(agent, identifier, key, entry, pooled)
            finally:
                entry.waiters -= 1


def _raise_if_stopped(agent, identifier):
    journal = getattr(agent, "journal", None)
    stop = getattr(agent, "stop_event", None)
    cancelled = getattr(journal, "is_cancelled", None)
    if (
        identifier in getattr(agent, "cancelled", ())
        or (stop is not None and stop.is_set())
        or (callable(cancelled) and cancelled(identifier))
    ):
        raise DevError("CANCELLED", "语义查询已取消", 409)


_DEFAULT_POOL: LspPool | None = None


def apply_pool_config(pool: LspPool, config) -> None:
    """Hot-apply integrations.lsp_pool from config.json; absent keeps env/defaults."""
    integrations = config.get("integrations") if isinstance(config, dict) else None
    settings = integrations.get("lsp_pool") if isinstance(integrations, dict) else None
    if not isinstance(settings, dict):
        return
    if type(settings.get("max_instances")) is int:
        pool.max_instances = settings["max_instances"]
    if type(settings.get("idle_ttl_seconds")) in (int, float):
        pool.idle_ttl = float(settings["idle_ttl_seconds"])


def get_pool(agent=None) -> LspPool:
    global _DEFAULT_POOL
    if agent is not None:
        pool = getattr(agent, "lsp_pool", None)
        if not isinstance(pool, LspPool):
            pool = LspPool()
            agent.lsp_pool = pool
        apply_pool_config(pool, getattr(agent, "config", None))
        return pool
    if _DEFAULT_POOL is None:
        _DEFAULT_POOL = LspPool()
    return _DEFAULT_POOL


async def evict_for_memory_pressure(agent=None, *, keep: int = 0) -> int:
    if agent is not None:
        pool = getattr(agent, "lsp_pool", None)
        return await pool.evict_for_memory_pressure(keep=keep) if isinstance(pool, LspPool) else 0
    loop = asyncio.get_running_loop()
    total = 0
    for pool in list(_LIVE_POOLS):
        if pool._loop is None or pool._loop is loop:
            total += await pool.evict_for_memory_pressure(keep=keep)
    return total


async def shutdown_all(agent=None) -> int:
    if agent is not None:
        pool = getattr(agent, "lsp_pool", None)
        return await pool.shutdown_all() if isinstance(pool, LspPool) else 0
    loop = asyncio.get_running_loop()
    total = 0
    for pool in list(_LIVE_POOLS):
        if pool._loop is None or pool._loop is loop:
            total += await pool.shutdown_all()
    return total


async def _query_locations(peer, projector, action, params, disk):
    if action == "references":
        params["context"] = {"includeDeclaration": True}
    result = await peer.request("textDocument/" + action, params)
    return await disk(_project_locations, projector, result)


def _project_locations(projector, result):
    items = []
    if result is None:
        result = []
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        raise DevError("LSP_PROTOCOL", "位置响应格式无效")
    seen = set()
    for item in result:
        loc = projector.location(item)
        if loc:
            key = json.dumps(loc, sort_keys=True)
            if key not in seen and projector.take(loc):
                items.append(loc)
                seen.add(key)
    return items


async def _query_diagnostics(peer, projector, args, uri, doc_version, capabilities, config, disk):
    if capabilities.get("diagnosticProvider"):
        response = await peer.request("textDocument/diagnostic", {"textDocument": {"uri": uri}})
        rawitems = response.get("items", []) if isinstance(response, dict) else []
        fresh = isinstance(response, dict) and response.get("kind") == "full"
    else:
        reported = await peer.wait_diagnostics(
            uri, doc_version, min(config.get("timeout_seconds", 20), 12)
        )
        fresh = reported is not None
        rawitems = reported.get("diagnostics", []) if reported else []
    items = await disk(_project_diagnostics, projector, args["path"], rawitems)
    extra = {
        "diagnostics_fresh": fresh,
        "diagnostics_state": "observed" if fresh else "not_received",
        "not_a_test_run": True,
    }
    return items, fresh, extra


def _project_diagnostics(projector, path, rawitems):
    items = []
    for item in rawitems:
        try:
            row = {
                "path": path,
                "range": projector.range(path, item["range"]),
                "message": str(item.get("message", ""))[:2000],
                "severity": item.get("severity"),
                "code": str(item.get("code", ""))[:100],
            }
            if projector.take(row):
                items.append(row)
        except (TypeError, ValueError, KeyError, DevError):
            projector.omitted += 1
    return items


async def _query_calls(peer, projector, action, params, disk):
    items = []
    prepared = await peer.request("textDocument/prepareCallHierarchy", params)
    if prepared is None:
        prepared = []
    if not isinstance(prepared, list):
        raise DevError("LSP_PROTOCOL", "调用层级响应无效")
    for item in prepared[:8]:
        if not await disk(projector.location, item):
            continue
        method = (
            "callHierarchy/incomingCalls"
            if action == "incoming_calls"
            else "callHierarchy/outgoingCalls"
        )
        calls = await peer.request(method, {"item": item}) or []
        if not isinstance(calls, list):
            raise DevError("LSP_PROTOCOL", "调用层级数据格式错误")
        items.extend(await disk(_project_calls, projector, action, calls))
    return items


def _project_calls(projector, action, calls):
    items = []
    for call in calls:
        if not isinstance(call, dict):
            projector.omitted += 1
            continue
        target = call.get("from" if action == "incoming_calls" else "to", {})
        loc = projector.location(target)
        if loc and projector.take(loc):
            items.append(
                {
                    **loc,
                    "name": str(target.get("name", ""))[:300],
                    "kind": target.get("kind"),
                }
            )
    return items


async def _execute_query(
    peer: Peer,
    agent,
    root: Path,
    args: dict,
    language: str,
    source: str | None,
    sha: str | None,
    uri: str | None,
    position: dict | None,
    stat_info,
    config: dict,
):
    projector = Projector(agent.engine, root, args["limit"])
    disk = getattr(agent, "disk_call", owned_disk_call)
    action = args["action"]
    fresh = True
    capabilities = peer.capabilities
    dependencies_changed = await peer.refresh_other_documents(uri)
    doc_version = 1
    if uri:
        doc_version = await peer.sync_document(
            uri=uri,
            language_id=language,
            source=source,
            sha=sha,
            stat_info=stat_info,
            force=dependencies_changed,
        )
    params = {"textDocument": {"uri": uri}, "position": position}
    items = []
    extra = {}
    if action in {"workspace_symbols", "references", "incoming_calls", "outgoing_calls"}:
        extra = {
            "index_state": await peer.await_source_diagnostics(
                uri, capabilities, expected_version=doc_version
            ),
            "index_seed": args["path"],
            "workspace_completeness": "language-server reported; not a proof of complete repository coverage",
        }
    if action == "workspace_symbols":
        result = await peer.request("workspace/symbol", {"query": args["query"]})
        items = await disk(projector.symbols, result)
    elif action == "symbols":
        result = await peer.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        items = await disk(projector.symbols, result, args["path"])
    elif action in {"definition", "references"}:
        items = await _query_locations(peer, projector, action, params, disk)
    elif action == "hover":
        result = await peer.request("textDocument/hover", params)
        content = result.get("contents", []) if isinstance(result, dict) else []
        if not isinstance(content, list):
            content = [content]
        text = "\n".join(
            str(c.get("value", "")) if isinstance(c, dict) else str(c) for c in content
        )
        extra = {"text": text[:16000], "text_truncated": len(text) > 16000}
    elif action == "diagnostics":
        items, fresh, extra = await _query_diagnostics(
            peer, projector, args, uri, doc_version, capabilities, config, disk
        )
    else:
        items = await _query_calls(peer, projector, action, params, disk)
    if source is not None:

        def current_sha():
            return hashlib.sha256(
                agent.engine.read_bytes(
                    agent.engine.path(root, args["path"], False), verify_path=True
                )
            ).hexdigest()

        latest = await disk(current_sha)
        fresh = fresh and latest == sha
    return {
        "action": action,
        "language": language,
        "backend": "lsp",
        "precision": "semantic",
        "items": items,
        "source_sha256": sha,
        "source_current": fresh,
        "truncated": projector.omitted > 0,
        "omitted": projector.omitted,
        "column_unit": "one-based Unicode scalar",
        **extra,
    }


def _authorized_query_root(agent, project):
    root, spec = agent.engine.root(project)
    if not project.get("allow_tasks") or not spec.get("allow_tasks"):
        raise DevError("TASKS_DISABLED", "语义查询需本机与项目执行许可", 403)
    info = root.stat()
    return root, (info.st_dev, info.st_ino)


def _prepare_query(agent, project, args):
    root, identity = _authorized_query_root(agent, project)
    path_arg = args.get("path", "")
    language = args.get("language") or LANGUAGES.get(Path(path_arg).suffix.lower())
    config = selected(agent.config, project, language)
    if not config:
        raise DevError(
            "LSP_NOT_CONFIGURED", "此项目未配置并授权对应语言服务；没有自动安装或改用文本匹配", 409
        )
    source = None
    sha = None
    uri = None
    position = None
    stat_info = None
    if args["action"] == "workspace_symbols" and not path_arg:
        # A file-scoped protocol barrier also works when a workspace query had
        # no path. Discover a bounded allowed seed, never a private config file.
        from agent.coding_reviews import excluded
        import stat

        for candidate, relative, info in agent.engine.walk(
            root, root, exclude=excluded, deadline=time.monotonic() + 2
        ):
            if stat.S_ISREG(info.st_mode) and LANGUAGES.get(candidate.suffix.lower()) == language:
                args = {**args, "path": relative, "line": 1, "column": 1}
                path_arg = relative
                break
        if not path_arg:
            raise DevError(
                "LSP_WORKSPACE_SEED",
                "未找到可分析的授权源码，请提供本语言的项目相对路径作为初始化依据",
                409,
            )
    if path_arg:
        path = agent.engine.path(root, path_arg, False)
        raw = agent.engine.read_bytes(path, verify_path=True)
        stat_info = path.lstat()
        source = agent.engine.text(raw)
        sha = hashlib.sha256(raw).hexdigest()
        uri = path.as_uri()
        expected_sha256 = args.get("expected_sha256", "")
        if expected_sha256 and sha != expected_sha256:
            raise DevError("LSP_SOURCE_CHANGED", "源码 SHA 已变化，请重新读取后定位", 409)
        lines = source.split("\n")
        line = args.get("line", 1) - 1
        column = args.get("column", 1) - 1
        if not 0 <= line < len(lines) or not 0 <= column <= len(lines[line]):
            raise DevError("LSP_POSITION", "查询位置超出源码")
        position = {"line": line, "character": len(lines[line][:column].encode("utf-16-le")) // 2}
    normalized_args = {
        "limit": 100,
        "query": "",
        **args,
        "path": path_arg,
    }
    return root, identity, normalized_args, language, source, sha, uri, position, stat_info, config


async def query(agent, identifier, project, args, *, pool: LspPool | None = None):
    # Start the outer budget before disk work. The owned disk helper shields
    # resource cleanup; no cancelled thread can outlive the query's ownership.
    path = args.get("path", "")
    requested_language = args.get("language") or LANGUAGES.get(Path(path).suffix.lower())
    candidate = selected(agent.config, project, requested_language) or {}
    disk = getattr(agent, "disk_call", owned_disk_call)
    budget = min(candidate.get("timeout_seconds", 20) * 3, 120)
    try:
        async with asyncio.timeout(budget):
            prepared = await disk(_prepare_query, agent, project, args)
            root, identity, normalized, language, source, sha, uri, pos, info, config = prepared
            _raise_if_stopped(agent, identifier)
            key = (str(root), _server_language(agent.config, language), *identity)
            active_pool = pool or get_pool(agent)

            async def admission():
                if await disk(_authorized_query_root, agent, project) != (root, identity):
                    raise DevError("LSP_SOURCE_CHANGED", "项目目录已变化，请重新定位", 409)
                _raise_if_stopped(agent, identifier)

            async def execute(peer):
                return await _execute_query(
                    peer, agent, root, normalized, language, source, sha, uri, pos, info, config
                )

            return await active_pool.run_query(
                agent, identifier, project, root, key, config, execute, admission=admission
            )
    except asyncio.TimeoutError as exc:
        raise DevError("LSP_TIMEOUT", "语义查询超过总期限，已回收语言服务", 409) from exc
