"""Killable isolation for bounded, read-only project filesystem operations.

This is process isolation from blocked filesystem calls, not an OS sandbox.
Authorization still uses FileEngine and the parent's current guards/queues.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from shared.util import DevError

PROBE_TIMEOUT = 15
READ_TOOLS = frozenset(
    {
        "open_workspace",
        "project_context",
        "read",
        "fs_read",
        "fs_read_many",
        "fs_tree",
        "fs_search",
        "project_knowledge_query",
    }
)


def eligible(tool, args):
    return tool in READ_TOOLS and not args.get("capture_baseline")


def worker_command(entry="main"):
    base = str(Path(__file__).resolve().parents[1])
    # -I ignores user site packages and PYTHONPATH, not installed coverage.
    return base, [
        sys.executable,
        "-I",
        "-c",
        "import sys; sys.path.insert(0, " + repr(base) + "); "
        f"from agent.workspace_probe import {entry}; {entry}()",
    ]


async def probe(engine, tool, project, args, *, phase="execute", expected_root=None, worker=None):
    payload = {
        "config": engine.config,
        "config_path": str(engine.config_path),
        "state_dir": str(engine.journal.directory),
        "tool": tool,
        "project": project,
        "args": args,
        "phase": phase,
        "expected_root": str(expected_root) if expected_root is not None else None,
    }
    if worker is None:
        base, command = worker_command()
        result = await run_worker(command, json.dumps(payload).encode(), cwd=base)
    else:
        result = await worker.request(payload)
    if not result["ok"]:
        error = result["error"]
        raise DevError(
            error["code"], error["message"], error.get("status", 400), **error.get("details", {})
        )
    return result["data"]


async def root(engine, tool, project, args, *, worker=None, expected_root=None):
    result = await probe(
        engine, tool, project, args, phase="root", worker=worker, expected_root=expected_root
    )
    return Path(result["root"]), result["spec"]


async def _finish(task):
    # Cleanup owns the child until it has been reaped, even if shutdown sends
    # another cancellation. This waits for one cleanup, never retries the read.
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _reap(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def run_worker(command, payload, *, cwd, timeout=None):  # noqa: ASYNC109 - Caller budget is enforced by the owned transport/process lifecycle.
    process = None
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    try:
        # Cancellation during creation must still recover and reap that child.
        process = await asyncio.shield(spawning)
        output, _ = await asyncio.wait_for(
            process.communicate(payload), PROBE_TIMEOUT if timeout is None else timeout
        )
        if process.returncode:
            raise DevError("PROJECT_DISCOVERY_FAILED", "项目概览读取进程异常退出")
        return decode_result(output)
    except TimeoutError as exc:
        raise DevError(
            "PROJECT_DISCOVERY_TIMEOUT",
            "项目概览读取超时；已停止读取并释放队列，请检查本机目录访问权限或磁盘状态",
        ) from exc
    finally:
        cancelled = False
        if process is None:
            process, cancelled = await _finish(spawning)
        _, reap_cancelled = await _finish(asyncio.create_task(_reap(process)))
        if cancelled or reap_cancelled:
            raise asyncio.CancelledError


def decode_result(output):
    try:
        result = json.loads(output)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("invalid response")
        if result["ok"]:
            if "data" not in result:
                raise ValueError("missing data")
        else:
            error = result.get("error")
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), str)
                or not isinstance(error.get("message"), str)
                or type(error.get("status", 400)) is not int
                or not isinstance(error.get("details", {}), dict)
            ):
                raise ValueError("invalid error")
        return result
    except (ValueError, UnicodeError) as exc:
        raise DevError("PROJECT_DISCOVERY_FAILED", "项目概览读取进程返回无效结果") from exc


class ReadPool:
    """Lease whole subprocesses. No paths, credentials or returned data live here."""

    def __init__(self, limit=4):
        self.limit = limit
        self.idle = []
        self.leased = 0
        self.epoch = 0
        self.closed = False

    def take(self):
        if self.closed or self.leased >= self.limit:
            return None
        self.epoch += 1
        self.leased += 1
        process, uses = self.idle.pop() if self.idle else (None, 0)
        if process is not None and process.returncode is not None:
            process, uses = None, 0
        return self.epoch, process, uses

    async def close(self):
        self.closed = True
        idle, self.idle = self.idle, []
        await asyncio.gather(*(_reap(process) for process, _ in idle))


class ReadWorker:
    def __init__(self, *, admission=False, pool=None):
        self.pool = pool
        self.epoch = None
        self.uses = 0
        self.complete = False
        self.admission = admission
        self.process = None
        self.index = 0
        self.failed = False

    async def __aenter__(self):
        lease = self.pool.take() if self.pool is not None else None
        if lease:
            self.epoch, self.process, self.uses = lease
            if self.process is not None:
                return self
        base, command = worker_command(
            "pool_main" if lease else "admission_session_main" if self.admission else "session_main"
        )
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=base,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # Core read permits a 16 MiB image before base64/JSON encoding.
                limit=64 * 1024 * 1024,
            )
        )
        try:
            self.process = await asyncio.shield(spawning)
        except BaseException:
            if self.epoch is not None:
                self.pool.leased -= 1
            self.process, _ = await _finish(spawning)
            await _finish(asyncio.create_task(_reap(self.process)))
            raise
        return self

    async def __aexit__(self, *exc):
        if self.epoch is not None:
            self.pool.leased -= 1
            if (
                exc[0] is None
                and not self.pool.closed
                and self.complete
                and not self.failed
                and self.process.returncode is None
                and self.uses + 1 < 256
            ):
                self.pool.idle.append((self.process, self.uses + 1))
                return
        _, cancelled = await _finish(asyncio.create_task(_reap(self.process)))
        if cancelled:
            raise asyncio.CancelledError

    async def abandon_admission(self):
        """Normal contention fallback: EOF, coverage flush, then reap before queuing."""
        if not self.admission or self.index != 1 or self.failed:
            raise DevError("INVALID_REQUEST", "仅已完成的准入会话可正常结束")
        if self.epoch is not None:
            async with asyncio.timeout(PROBE_TIMEOUT):
                self.process.stdin.write(json.dumps({"release_lease": self.epoch}).encode() + b"\n")
                await self.process.stdin.drain()
                ack = decode_result(await self.process.stdout.readline())
                if ack.get("lease_epoch") != self.epoch or not ack["ok"]:
                    raise DevError("PROJECT_DISCOVERY_FAILED", "独立读取进程租约结束回执无效")
                self.complete = True
            return
        self.failed = True
        self.process.stdin.close()
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                await self.process.wait()
        except TimeoutError as exc:
            raise DevError("PROJECT_DISCOVERY_TIMEOUT", "项目准入读取进程结束超时") from exc
        if self.process.returncode:
            raise DevError("PROJECT_DISCOVERY_FAILED", "项目准入读取进程异常退出")

    async def request(self, payload):
        phases = (("admission",) if self.admission else ()) + ("root", "root", "execute")
        if self.failed or self.index >= len(phases) or payload.get("phase") != phases[self.index]:
            self.failed = True
            raise DevError("INVALID_REQUEST", "独立读取会话阶段乱序或重复执行")
        self.index += 1
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                if self.epoch is not None:
                    payload = {**payload, "lease_epoch": self.epoch}
                self.process.stdin.write(json.dumps(payload).encode() + b"\n")
                await self.process.stdin.drain()
                result = decode_result(await self.process.stdout.readline())
                if self.epoch is not None and result.get("lease_epoch") != self.epoch:
                    raise DevError("PROJECT_DISCOVERY_FAILED", "独立读取进程返回了其他租约的数据")
                if not result["ok"]:
                    self.failed = True
                self.complete = self.index == len(phases) and result["ok"]
                if self.epoch is None and (self.index == len(phases) or not result["ok"]):
                    # A terminal frame is not proof of a clean process exit.
                    # Allow normal shutdown (including coverage) before reaping.
                    await self.process.wait()
                    if self.process.returncode:
                        raise DevError("PROJECT_DISCOVERY_FAILED", "项目概览读取会话异常退出")
                return result
        except TimeoutError as exc:
            self.failed = True
            raise DevError(
                "PROJECT_DISCOVERY_TIMEOUT", "项目概览读取超时；已停止读取并释放队列"
            ) from exc
        except (BrokenPipeError, ConnectionResetError, ValueError) as exc:
            self.failed = True
            raise DevError("PROJECT_DISCOVERY_FAILED", "项目概览读取会话异常结束") from exc
        except BaseException:
            self.failed = True
            raise


def dispatch(request):
    from agent.filesystem import FileEngine

    tool, args = request["tool"], request["args"]
    if not eligible(tool, args):
        raise DevError("INVALID_REQUEST", "独立读取进程仅接受无快照的项目概览")
    engine = FileEngine(
        request["config"],
        SimpleNamespace(directory=Path(request["state_dir"])),
        Path(request["config_path"]),
    )
    phase = request.get("phase", "execute")
    if phase in {"root", "claims", "admission"}:
        actual_root, spec = engine.root(request["project"])
        if (
            request.get("expected_root") is not None
            and str(actual_root) != request["expected_root"]
        ):
            raise DevError("ROOT_CHANGED", "项目根目录发生变化，请确认映射后重新提交")
        if phase == "root":
            return {"root": str(actual_root), "spec": spec}
        if phase == "claims" and str(actual_root) != request.get("expected_root"):
            raise DevError("ROOT_CHANGED", "项目根目录发生变化，请确认映射后重新提交")
        from agent.resource_queue import Claim, claims_for, canonical_path, PATH_READ_TOOLS

        claims = (
            claims_for(engine, tool, request["project"], args, actual_root)
            if tool == "read" or tool in PATH_READ_TOOLS
            else [Claim("agent", "path", canonical_path(actual_root), False)]
        )
        return {
            "root": str(actual_root),
            "spec": spec,
            "claims": [asdict(claim) for claim in claims],
            "project_key": request["project"].get("id")
            or canonical_path(request["project"].get("_original_root", actual_root)),
        }
    if phase != "execute":
        raise DevError("INVALID_REQUEST", "未知的独立读取阶段")
    if request.get("expected_root") is not None:
        actual_root, _ = engine.root(request["project"])
        if str(actual_root) != request["expected_root"]:
            raise DevError("ROOT_CHANGED", "项目根目录发生变化，请确认映射后重新提交")
    if tool == "read":
        from agent.core_files import CoreFiles

        return CoreFiles(engine).call(tool, request["project"], args)
    return engine.call(tool, request["project"], args)


def response(request):
    try:
        return {"ok": True, "data": dispatch(request)}
    except DevError as exc:
        return {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "status": exc.status,
                "details": exc.details,
            },
        }


def session_main():
    _session_main(False)


def admission_session_main():
    _session_main(True)


def pool_main():
    # Only an epoch survives between leases. All request and engine state is
    # local to _session_main/dispatch and is discarded when control returns.
    import os
    import tempfile

    os.chdir(tempfile.gettempdir())
    previous = 0
    for _ in range(256):
        first = sys.stdin.buffer.readline(64 * 1024 * 1024 + 1)
        if not first or len(first) > 64 * 1024 * 1024:
            return
        request = json.loads(first)
        epoch, admission = request.get("lease_epoch"), request.get("phase") == "admission"
        del request
        if type(epoch) is not int or epoch <= previous:
            return
        previous = epoch
        ok = _session_main(admission, epoch, first)
        del first
        if not ok:
            return


def _session_main(admission, epoch=None, first=None):
    identity = None
    admitted_root = None
    phases = (("admission",) if admission else ()) + ("root", "root", "execute")
    for phase in phases:
        line = first if first is not None else sys.stdin.buffer.readline(64 * 1024 * 1024 + 1)
        first = None
        if not line:
            return
        try:
            if len(line) > 64 * 1024 * 1024:
                raise ValueError("frame too large")
            request = json.loads(line)
            if epoch is not None:
                if (
                    admission
                    and phase == "root"
                    and identity is not None
                    and request == {"release_lease": epoch}
                ):
                    print(json.dumps({"lease_epoch": epoch, "ok": True, "data": {}}), flush=True)
                    return True
                if request.get("lease_epoch") != epoch:
                    raise ValueError("wrong lease")
            current = (request["tool"], request["args"])
            if request.get("phase") != phase or (identity is not None and current != identity):
                raise ValueError("invalid order or changed operation")
            expected = request["expected_root"]
            if phase == "admission":
                if expected is not None:
                    raise ValueError("admission cannot select its root")
            else:
                if not isinstance(expected, str) or not expected:
                    raise ValueError("missing admitted root")
                if admitted_root is not None and expected != admitted_root:
                    raise ValueError("changed admitted root")
                admitted_root = expected
            identity = current
            result = response(request)
            if phase == "admission" and result["ok"]:
                admitted_root = result["data"]["root"]
        except (ValueError, KeyError, TypeError):
            result = {
                "ok": False,
                "error": {"code": "INVALID_REQUEST", "message": "独立读取会话阶段或请求无效"},
            }
        if epoch is not None:
            result["lease_epoch"] = epoch
        sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
        sys.stdout.flush()
        if not result["ok"]:
            return
    return True  # Exactly one execute per lease.


def main():
    sys.stdout.write(json.dumps(response(json.load(sys.stdin)), ensure_ascii=True))


if __name__ == "__main__":
    main()
