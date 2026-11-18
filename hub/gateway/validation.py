"""Bounded process isolation for backend-controlled regex/ref/combinator schemas."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from hub.gateway.catalog import encoded
from shared.util import DevError


class Validator:
    def __init__(self, limit=4, timeout=8):
        self.active = 0
        self.limit, self.timeout = limit, timeout
        self.idle = []
        self.workers = set()
        self.closed = False

    async def discard(self, worker):
        self.workers.discard(worker)
        if worker.returncode is None:
            try:
                worker.kill()
            except ProcessLookupError:
                pass
        await worker.wait()

    async def close(self):
        self.closed = True
        self.idle.clear()
        await asyncio.gather(*(self.discard(worker) for worker in tuple(self.workers)))

    async def acquire(self):
        while self.idle:
            worker, uses = self.idle.pop()
            if worker.returncode is None:
                return worker, uses
            self.workers.discard(worker)
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__).with_name("validate_worker.py")),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=128,
            )
        )
        try:
            worker = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            await self.discard(await spawning)
            raise
        self.workers.add(worker)
        return worker, 0

    async def validate(self, schema, value, code="GATEWAY_ARGUMENTS_INVALID"):
        if self.closed or self.active >= self.limit:
            raise DevError("GATEWAY_BUSY", "schema 验证容量已满；未发送新操作", 429)
        self.active += 1
        worker = None
        try:
            frame = encoded({"schema": schema, "value": value}).encode()
            if len(frame) > 2 * 1024 * 1024:
                raise DevError(code, "数据超出验证预算")
            async with asyncio.timeout(self.timeout):
                worker, uses = await self.acquire()
                if self.closed:
                    raise DevError("GATEWAY_BUSY", "schema 验证服务正在关闭", 503)
                worker.stdin.write(frame + b"\n")
                await worker.stdin.drain()
                reply = await worker.stdout.readline()
            if reply not in {b"1\n", b"0\n"}:
                raise DevError(code, "验证进程退出或超过预算")
            if not self.closed and uses + 1 < 64:
                self.idle.append((worker, uses + 1))
                worker = None
            if reply == b"0\n":
                raise DevError(code, "数据不符合已发布 schema 或超出验证预算")
        except TimeoutError as exc:
            raise DevError(code, "schema 验证超过时间预算") from exc
        except (OSError, ValueError) as exc:
            raise DevError(code, "受限 schema 验证失败") from exc
        finally:
            try:
                if worker is not None:
                    await self.discard(worker)
            finally:
                self.active -= 1
