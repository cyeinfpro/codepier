"""Small OS counters only; never collect process command lines or user content."""
from __future__ import annotations

import ctypes
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from agent.scheduler import Pressure


class ResourceSampler:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.previous = None
        self.memory_total = None
        self.cgroup_cpu = {}
        self.previous_cgroup_cpu = {}
        self.cpu_scope = None

    @staticmethod
    def _read(path):
        return Path(path).read_text(encoding="ascii")[:65536]

    @staticmethod
    def _command(args):
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=.6).stdout[:65536]

    def _cgroup_paths(self):
        root = Path("/sys/fs/cgroup")
        try:
            membership = next(line.split(":", 2)[2] for line in self._read("/proc/self/cgroup").splitlines()
                              if line.startswith("0::"))
            relative = Path(membership)
            if not relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid cgroup membership")
            leaf = root / str(relative).lstrip("/")
            # Observe parent limits as well as a leaf's unlimited value.
            return [str(leaf), *[str(parent) for parent in leaf.parents if parent == root or root in parent.parents]][:64]
        except (OSError, ValueError, KeyError, StopIteration):
            return [str(root)]

    def _linux(self):
        cpu_lines = self._read("/proc/stat").splitlines()
        values = [int(part) for part in cpu_lines[0].split()[1:9]]
        scope = None
        try:
            affinity = os.sched_getaffinity(0)
            selected = [[int(part) for part in line.split()[1:9]] for line in cpu_lines[1:]
                        if line.split() and line.split()[0][3:].isdigit()
                        and int(line.split()[0][3:]) in affinity]
            if selected and len(selected) == len(affinity):
                values = [sum(row[index] for row in selected) for index in range(min(map(len, selected)))]
                scope = tuple(sorted(affinity))
        except (AttributeError, OSError, ValueError):
            pass
        if scope != self.cpu_scope:
            self.previous = None
            self.cpu_scope = scope
        if len(values) < 4 or min(values) < 0:
            raise ValueError("Invalid CPU counters")
        ticks = (sum(values), values[3] + (values[4] if len(values) > 4 else 0))
        memory = {}
        for line in self._read("/proc/meminfo").splitlines():
            key, rest = line.split(":", 1)
            parts = rest.split()
            if parts and parts[0].isdigit():
                memory[key] = int(parts[0]) * 1024
        total, available = memory["MemTotal"], memory.get("MemAvailable")
        if available is None:
            raise ValueError("Available memory counter missing")
        io = None
        try:
            first = self._read("/proc/pressure/io").splitlines()[0]
            match = re.search(r"\bavg10=([0-9.]+)", first)
            io = float(match.group(1)) / 100 if match else None
        except (OSError, ValueError, IndexError):
            pass
        # Reclaimable clean inactive file cache is not committed working memory.
        # Do not count anonymous/shmem, dirty or writeback pages as available.
        self.cgroup_cpu = {}
        for group in self._cgroup_paths():
            try:
                maximum = self._read(group + "/memory.max").strip()
                current = int(self._read(group + "/memory.current").strip())
                if maximum.isdigit() and int(maximum) > 0 and current >= 0:
                    maximum = int(maximum)
                    reclaimable = 0
                    try:
                        stats = {key: int(value) for key, value in
                                 (line.split() for line in self._read(group + "/memory.stat").splitlines())}
                        inactive = max(0, stats.get("inactive_file", 0))
                        file_cache = max(0, stats.get("file", inactive) - max(0, stats.get("shmem", 0)))
                        unsafe = max(0, stats.get("file_dirty", 0)) + max(0, stats.get("file_writeback", 0))
                        reclaimable = min(current, max(0, min(inactive, file_cache) - unsafe))
                    except (OSError, ValueError, KeyError):
                        pass
                    total = min(total, maximum)
                    available = min(available, total, max(0, maximum - current + reclaimable))
            except (OSError, ValueError, KeyError):
                pass
            try:
                quota, period = self._read(group + "/cpu.max").split()
                if quota != "max" and int(quota) > 0 and int(period) > 0:
                    stats = {key: int(value) for key, value in
                             (line.split() for line in self._read(group + "/cpu.stat").splitlines())}
                    if stats["usage_usec"] >= 0:
                        self.cgroup_cpu[group] = (stats["usage_usec"], int(quota) / int(period))
            except (OSError, ValueError, KeyError):
                pass
        return ticks, available, total, io, "linux_os_counters"

    def _darwin(self):
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        library.mach_host_self.restype = ctypes.c_uint
        library.host_statistics.argtypes = [ctypes.c_uint, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)]
        host = library.mach_host_self()
        counters = (ctypes.c_uint * 4)()
        count = ctypes.c_uint(4)
        try:
            result = library.host_statistics(host, 3, ctypes.cast(counters, ctypes.POINTER(ctypes.c_int)), ctypes.byref(count))
        finally:
            task = ctypes.c_uint.in_dll(library, "mach_task_self_").value
            library.mach_port_deallocate(task, host)
        if result != 0 or count.value != 4:
            raise ValueError("CPU counters unavailable")
        ticks = (sum(counters), counters[2])
        if self.memory_total is None:
            self.memory_total = int(self._command(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip())
        memory = self._command(["/usr/bin/vm_stat"])
        page = re.search(r"page size of ([0-9]+) bytes", memory)
        if not page:
            raise ValueError("VM page size unavailable")
        pages = {}
        for name, number in re.findall(r"^([^:\n]+):\s*([0-9]+)\.", memory, re.MULTILINE):
            pages[name] = int(number)
        # Do not count compressed/wired pages as free or claim I/O pressure.
        available = sum(pages[key] for key in ("Pages free", "Pages inactive", "Pages speculative")) * int(page.group(1))
        return ticks, available, self.memory_total, None, "darwin_os_counters"

    def _windows(self):
        from ctypes import wintypes

        class Memory(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "available", "page_total", "page_available",
                                                       "virtual_total", "virtual_available", "extended")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        idle, system, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
            raise OSError("CPU counters unavailable")
        number = lambda value: (value.dwHighDateTime << 32) | value.dwLowDateTime
        ticks = (number(system) + number(user), number(idle))
        memory = Memory()
        memory.length = ctypes.sizeof(Memory)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
            raise OSError("Memory counters unavailable")
        return ticks, int(memory.available), int(memory.total), None, "windows_os_counters"

    def sample(self):
        now = self.clock()
        try:
            if sys.platform == "linux":
                counters, available, total, io, source = self._linux()
            elif sys.platform == "darwin":
                counters, available, total, io, source = self._darwin()
            elif sys.platform == "win32":
                counters, available, total, io, source = self._windows()
            else:
                raise ValueError("Unsupported resource counters")
            cpu = None
            if self.previous is not None:
                previous_at, old = self.previous
                elapsed = now - previous_at
                delta, idle = counters[0] - old[0], counters[1] - old[1]
                if 0 < elapsed <= 15 and delta > 0 and 0 <= idle <= delta:
                    cpu = 1 - idle / delta
                    if not math.isfinite(cpu):
                        cpu = None
            if sys.platform == "linux":
                for group, (usage, budget) in self.cgroup_cpu.items():
                    previous = self.previous_cgroup_cpu.get(group)
                    if previous is None:
                        continue
                    previous_at, old_usage, old_budget = previous
                    elapsed = now - previous_at
                    if 0 < elapsed <= 15 and usage >= old_usage and budget == old_budget:
                        busy = min(1.0, (usage - old_usage) / (elapsed * 1000000 * budget))
                        if math.isfinite(busy):
                            cpu = max(cpu, busy) if cpu is not None else busy
                self.previous_cgroup_cpu = {group: (now, usage, budget)
                                           for group, (usage, budget) in self.cgroup_cpu.items()}
            self.previous = (now, counters)
            return Pressure(now, cpu, available, total, io, source)
        except (OSError, ValueError, KeyError, IndexError, AttributeError, subprocess.SubprocessError):
            self.previous = None
            self.previous_cgroup_cpu = {}
            return Pressure(now)
