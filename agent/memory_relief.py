"""Keep the Agent's own heap from pushing a small node into admission pause.

Diagnosed 2026-11-07 on the Seoul node (952 MiB RAM, no swap): an idle Agent
held 268 MiB of anonymous heap (peak 325 MiB) that glibc never returned to the
OS. MemAvailable fell to ~76 MiB, under the Hub/Agent critical line of
``min(256 MiB, 10 % of RAM)``, so every write was refused with
``DEVICE_BUSY / resource_pressure`` although nothing was running.

Two bounded, content-free measures (Linux/glibc only; no-ops elsewhere):

* ``configure()`` caps malloc arenas at start-up (``mallopt(M_ARENA_MAX)``),
  the in-process equivalent of ``MALLOC_ARENA_MAX=2``. Asyncio ``to_thread``
  workers otherwise each get an arena that fragments and is retained.
* ``MemoryRelief.observe()`` runs ``gc.collect()`` + ``malloc_trim(0)`` when the
  node is below the recovery threshold (rate-limited), and occasionally when
  idle, so freed Python objects become available memory again.

Neither touches operations, journals or user data, and neither changes any
admission threshold: the node simply stops pausing itself.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import os
import sys
import time

LOGGER = logging.getLogger("codepier.agent")
M_ARENA_MAX = -8
ARENA_MAX = 2
PRESSURE_INTERVAL = 30.0
IDLE_INTERVAL = 600.0
LOG_THRESHOLD = 16 * 1024 ** 2

_libc = None
_libc_loaded = False


def _glibc():
    global _libc, _libc_loaded
    if _libc_loaded:
        return _libc
    _libc_loaded = True
    if not sys.platform.startswith("linux"):
        return None
    try:
        library = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        library.malloc_trim.argtypes = [ctypes.c_size_t]
        library.malloc_trim.restype = ctypes.c_int
        library.mallopt.argtypes = [ctypes.c_int, ctypes.c_int]
        library.mallopt.restype = ctypes.c_int
        _libc = library
    except (OSError, AttributeError):
        # musl and other libcs have no malloc_trim/mallopt: stay a no-op.
        _libc = None
    return _libc


def configure() -> bool:
    """Cap glibc malloc arenas. Call once, before worker threads start."""
    if os.environ.get("MALLOC_ARENA_MAX"):
        return False  # an operator setting wins
    library = _glibc()
    if library is None:
        return False
    try:
        return bool(library.mallopt(M_ARENA_MAX, ARENA_MAX))
    except (OSError, ValueError):
        return False


def resident_bytes() -> int | None:
    try:
        with open("/proc/self/statm", "rb") as stream:
            return int(stream.read(256).split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def trim() -> int | None:
    """Return freed heap to the OS; result is the RSS drop in bytes, if known."""
    library = _glibc()
    before = resident_bytes()
    gc.collect()
    if library is not None:
        try:
            library.malloc_trim(0)
        except (OSError, ValueError):
            pass
    after = resident_bytes()
    return None if before is None or after is None else max(0, before - after)


def below_recovery(sample) -> bool:
    """Same recovery line the Hub and Agent use to lift a memory pause."""
    try:
        available, total = sample.memory_available, sample.memory_total
    except AttributeError:
        return False
    if not isinstance(available, int) or not isinstance(total, int) or total <= 0:
        return False
    return available < max(min(512 * 1024 ** 2, total * .20), total * .10)


class MemoryRelief:
    def __init__(self, *, clock=time.monotonic, trimmer=trim):
        self.clock = clock
        self.trimmer = trimmer
        self.last = None

    def observe(self, sample, *, idle: bool) -> int | None:
        """Trim when the node is short of memory, or periodically while idle."""
        if _glibc() is None and self.trimmer is trim:
            return None
        now = self.clock()
        since = None if self.last is None else now - self.last
        pressured = below_recovery(sample)
        if pressured and (since is None or since >= PRESSURE_INTERVAL):
            pass
        elif idle and (since is None or since >= IDLE_INTERVAL):
            pass
        else:
            return None
        self.last = now
        freed = self.trimmer()
        if freed and freed >= LOG_THRESHOLD:
            LOGGER.info("已释放 Agent 空闲内存 %s MiB（%s）", freed // 1024 ** 2,
                        "节点内存紧张" if pressured else "空闲整理")
        return freed
