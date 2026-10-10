"""Bounded, project-fair admission; resource locks stay outside this scheduler.

Shrinking capacity never cancels admitted operations. A reserved admission per
lane prevents one project's backlog from occupying every slot before another
project arrives. No caller-supplied priority or shell-command classification.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass

from shared.util import DevError
from shared.scheduler_config import validate_scheduler

LANES = ("execution", "read", "remote")




@dataclass(frozen=True)
class Pressure:
    measured_at: float
    cpu_busy: float | None = None
    memory_available: int | None = None
    memory_total: int | None = None
    io_stall: float | None = None
    source: str = "unavailable"

    def valid(self, now):
        def ratio(value):
            return value is None or (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1)
        return (type(self.measured_at) in (int, float) and math.isfinite(self.measured_at)
                and 0 <= now - self.measured_at <= 15
                and ratio(self.cpu_busy) and ratio(self.io_stall)
                and type(self.memory_available) is int and type(self.memory_total) is int
                and 0 <= self.memory_available <= self.memory_total and self.memory_total > 0)


class AdaptiveCapacity:
    """Hysteresis: three healthy samples and cooldown to grow; fast safe shrink."""
    def __init__(self, config, clock=time.monotonic):
        self.config = validate_scheduler(config, effective=True)
        self.clock = clock
        self.limit = self.config["initial"]
        self.reason = "starting"
        self.healthy = 0
        self.changed_at = clock()
        self.last_sample = None
        self.memory_paused = False

    def update(self, sample):
        now = self.clock()
        self.last_sample = sample
        cfg = self.config
        valid = isinstance(sample, Pressure) and sample.valid(now)
        # Absolute reserves must remain reachable on small, real cgroup budgets.
        critical = valid and (sample.memory_available < min(256 * 1024**2, sample.memory_total * .10)
                              or sample.memory_available / sample.memory_total < .025)
        if critical:
            self.healthy = 0
            self.limit = 0
            self.memory_paused = True
            self.reason = "memory_critical"
            self.changed_at = now
            return self.limit
        if self.memory_paused:
            # An unavailable counter cannot clear a known emergency. Require
            # healthy observations and cooldown even when adaptive mode is off.
            safe = (valid and sample.cpu_busy is not None and sample.cpu_busy < .90
                    and sample.memory_available >= max(min(512 * 1024**2, sample.memory_total * .20),
                                                       sample.memory_total * .10))
            self.healthy = self.healthy + 1 if safe else 0
            self.reason = "memory_recovery" if safe else "memory_critical"
            if self.healthy >= 3 and now - self.changed_at >= 15:
                self.memory_paused = False
                self.limit = cfg["minimum"] if cfg["adaptive"] else cfg["initial"]
                self.changed_at = now
                self.healthy = 0
                self.reason = "recovering"
            return self.limit
        if not cfg["adaptive"]:
            self.limit = cfg["initial"]
            self.reason = "fixed"
            return self.limit
        if not valid or sample.cpu_busy is None:
            self.healthy = 0
            self.limit = min(self.limit, max(cfg["minimum"], min(4, cfg["maximum"])))
            self.reason = "metrics_unavailable"
            self.changed_at = now
            return self.limit
        available = sample.memory_available
        fraction = available / sample.memory_total
        high = fraction < .10 or sample.cpu_busy >= .90 or (sample.io_stall or 0) >= .25
        if high:
            self.healthy = 0
            self.limit = max(cfg["minimum"], self.limit - max(1, self.limit // 4))
            self.reason = "resource_pressure"
            self.changed_at = now
            return self.limit
        healthy = fraction >= .20 and available >= 1024**3 and sample.cpu_busy <= .65 and (sample.io_stall or 0) < .10
        self.healthy = self.healthy + 1 if healthy else 0
        self.reason = "healthy" if healthy else "cooldown"
        if self.healthy >= 3 and now - self.changed_at >= 15:
            memory_ceiling = max(cfg["minimum"], available // (512 * 1024**2))
            self.limit = min(cfg["maximum"], memory_ceiling, max(cfg["minimum"], self.limit + 1))
            self.changed_at = now
            self.healthy = 0
        return self.limit


@dataclass(frozen=True)
class Ticket:
    token: object
    operation_id: str
    project: str
    lane: str
    target: str


class ProjectScheduler:
    def __init__(self, config=None, clock=time.monotonic):
        self.config = validate_scheduler(config, effective=True)
        self.capacity = AdaptiveCapacity(self.config, clock)
        self.condition = asyncio.Condition()
        self.waiting = []
        self.active = {}
        self.turn = Counter()
        self.sequence = 0
        self.closed = False

    def lane_limit(self, lane):
        if lane == "execution":
            return self.capacity.limit
        if self.capacity.memory_paused:
            return 1 if lane == "read" else 0
        return self.config["read_limit" if lane == "read" else "remote_limit"]

    def _reason(self, item):
        held = list(self.active.values())
        limit = self.lane_limit(item.lane)
        same_lane = [entry for entry in held if entry.lane == item.lane]
        # Leave one slot for another project even before it joins the queue.
        # Other idle capacity can be borrowed, up to the project owner's cap.
        project_cap = min(self.config["project_limits"].get(item.project, 64), max(1, limit - 1))
        if sum(entry.project == item.project for entry in same_lane) >= project_cap:
            return "project_limit"
        if item.lane == "remote" and sum(entry.lane == "remote" and entry.target == item.target for entry in held) >= self.config["remote_target_limit"]:
            return "remote_target_limit"
        if len(same_lane) >= limit:
            return "resource_pressure" if limit == 0 else "lane_capacity"
        return None

    def _selected(self):
        eligible = []
        seen = set()
        counts = Counter((entry.lane, entry.project) for entry in self.active.values())
        for index, item in enumerate(self.waiting):
            key = (item.lane, item.project)
            if key in seen:
                continue
            # A blocked remote target must not block this project's other target.
            if self._reason(item):
                continue
            seen.add(key)
            eligible.append((counts[key], self.turn[key], index, item))
        return min(eligible, key=lambda row: row[:3])[-1] if eligible else None

    async def reconfigure(self, value):
        config = validate_scheduler(value, effective=True)
        async with self.condition:
            self.config = config
            self.capacity.config = config
            self.capacity.limit = (0 if self.capacity.memory_paused else config["initial"] if not config["adaptive"]
                                   else max(config["minimum"], min(self.capacity.limit, config["maximum"])))
            self.capacity.healthy = 0
            self.capacity.changed_at = self.capacity.clock()
            self.condition.notify_all()

    async def update_pressure(self, sample):
        async with self.condition:
            self.capacity.update(sample)
            self.condition.notify_all()

    async def close(self):
        async with self.condition:
            self.closed = True
            self.condition.notify_all()

    @asynccontextmanager
    async def slot(self, operation_id, project, lane="execution", target="", on_wait=None):
        if lane not in LANES or not isinstance(project, str) or not project:
            raise ValueError("Invalid scheduler admission identity")
        item = Ticket(object(), operation_id, project, lane, target)
        added = False
        try:
            async with self.condition:
                if self.closed:
                    raise DevError("AGENT_STOPPING", "Agent 正在停止，尚未开始执行", 409)
                if len(self.waiting) >= 1024:
                    raise DevError("SCHEDULER_QUEUE_FULL", "节点等待队列已满，请稍后重试", 429)
                self.waiting.append(item)
                added = True
                while self._selected() is not item:
                    if self.closed:
                        raise DevError("AGENT_STOPPING", "Agent 正在停止，尚未开始执行", 409)
                    if on_wait:
                        on_wait(self._reason(item) or "project_fairness")
                    await self.condition.wait()
                if self.closed:
                    raise DevError("AGENT_STOPPING", "Agent 正在停止，尚未开始执行", 409)
                self.waiting.remove(item)
                self.active[item.token] = item
                self.sequence += 1
                self.turn[(lane, project)] = self.sequence
                self.condition.notify_all()
            yield
        finally:
            if added:
                async with self.condition:
                    if item in self.waiting:
                        self.waiting.remove(item)
                    self.active.pop(item.token, None)
                    # Bound fairness history to active/queued project-lane pairs.
                    live = {(entry.lane, entry.project) for entry in [*self.waiting, *self.active.values()]}
                    self.turn = Counter({key: value for key, value in self.turn.items() if key in live})
                    self.condition.notify_all()

    def snapshot(self, project=None):
        active = [entry for entry in self.active.values() if project is None or entry.project == project]
        waiting = [entry for entry in self.waiting if project is None or entry.project == project]
        return {
            "running": len(active), "queued": len(waiting),
            "reason": self.capacity.reason,
            "lanes": {lane: {"running": sum(entry.lane == lane for entry in active),
                             "queued": sum(entry.lane == lane for entry in waiting),
                             "capacity": self.lane_limit(lane)} for lane in LANES},
            "scope": "node" if project is None else "project",
        }
