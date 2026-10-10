"""Bounded durable admission, independent of Agent execution slots.

Only authenticated connection heartbeats may feed AdmissionWindow. Telemetry is
advisory, not authority: Hub count/byte ceilings apply even to dishonest peers.
No request priorities, shell inspection, stored device info, or remote clocks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from hub.scheduler_settings import public_snapshot
from shared.util import DevError

READ_TOOLS = frozenset({"read", "fs_read", "fs_read_many", "fs_tree", "tasks_list"})
ACTIVE_SQL = "state IN ('queued','running','reconnecting','cancelling')"
MAX_NODE = 512
MAX_PROJECT = 256
OVERLOAD_READS = 8
OVERLOAD_PROJECT_READS = 7
MAX_GLOBAL = 4096
NODE_BYTES = 64 * 1024**2
PROJECT_BYTES = 32 * 1024**2
GLOBAL_BYTES = 256 * 1024**2
REPORT_TTL = 45
SOURCES = frozenset({"linux_os_counters", "darwin_os_counters", "windows_os_counters"})


@dataclass(frozen=True)
class Budget:
    node: int = 64
    project: int = 32
    reserve: int = 8
    read_reserve: int = 0
    reason: str = "legacy"
    paused: bool = False

    @property
    def overload_read_reserve(self):
        return OVERLOAD_READS if self.paused or self.reason == "pressure" else 0

    @property
    def retry_after(self):
        return 15 if self.paused else 5 if self.reason == "pressure" else 3


def resource_report(value):
    """Strict, versioned OS sample; age is measured on the Agent, receipt on Hub."""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        return None
    source = value.get("source")
    if not isinstance(source, str) or source not in SOURCES:
        return None
    result = {"source": source}
    for key in ("sample", "age_seconds", "cpu_busy", "io_stall"):
        number = value.get(key)
        if key in {"cpu_busy", "io_stall"} and number is None:
            result[key] = None
            continue
        if (type(number) not in (int, float) or not -2**53 <= number <= 2**53
                or not math.isfinite(number)):
            return None
        if key in {"cpu_busy", "io_stall"} and not 0 <= number <= 1:
            return None
        if key == "age_seconds" and not 0 <= number <= 15 or key == "sample" and number < 0:
            return None
        result[key] = number
    available, total = value.get("memory_available"), value.get("memory_total")
    if type(available) is not int or type(total) is not int or not 0 <= available <= total <= 2**60 or total == 0:
        return None
    result.update(memory_available=available, memory_total=total)
    return result


class AdmissionWindow:
    """Shrink immediately; grow after three distinct healthy samples and 15s."""
    def __init__(self):
        self.received = None
        self.revision = None
        self.budget = Budget()
        self.healthy = 0
        self.changed = None
        self.last_sample = None
        self.last_source = None
        self.sample_age = 0
        self.paused = False

    def observe(self, snapshot, telemetry, revision, expected_revision, now):
        sample = resource_report(telemetry)
        snapshot = public_snapshot(snapshot) if isinstance(snapshot, dict) and snapshot.get("scope") == "node" else None
        valid = (sample is not None and snapshot is not None and revision == expected_revision
                 and isinstance(revision, str) and len(revision) == 64
                 and all(snapshot[key] == sum(lane[key] for lane in snapshot["lanes"].values())
                         for key in ("running", "queued")))
        if not valid:
            self.received = None
            self.healthy = 0
            self.budget = Budget(paused=self.paused, reason="unavailable")
            return
        if (self.received is None or not 0 <= now - self.received
                or now - self.received + self.sample_age > REPORT_TTL
                or self.revision != revision or self.last_source != sample["source"]):
            self.healthy = 0
            self.changed = now
            self.budget = Budget(paused=self.paused)
        if self.last_source == sample["source"] and self.last_sample is not None:
            if sample["sample"] < self.last_sample:
                self.received, self.healthy = None, 0
                self.budget = Budget(reason="unavailable", paused=self.paused)
                return
            if sample["sample"] == self.last_sample:
                # Replayed samples do not refresh freshness or grow capacity.
                return
        self.last_sample, self.last_source = sample["sample"], sample["source"]
        self.received, self.revision = now, revision
        self.sample_age = sample["age_seconds"]
        available, total = sample["memory_available"], sample["memory_total"]
        critical = (available < min(256 * 1024**2, total * .10)
                    or available / total < .025 or snapshot["reason"] in {"memory_critical", "memory_recovery"})
        high = (available / total < .10 or (sample["cpu_busy"] is not None and sample["cpu_busy"] >= .90)
                or (sample["io_stall"] or 0) >= .25 or snapshot["reason"] == "resource_pressure")
        healthy = (available >= 1024**3 and available / total >= .20
                   and sample["cpu_busy"] is not None and sample["cpu_busy"] <= .65 and (sample["io_stall"] or 0) < .10)
        if critical:
            self.paused = True
        if critical or high:
            self.budget = Budget(read_reserve=8, reason="pressure", paused=self.paused)
            self.healthy, self.changed = 0, now
            return
        if sample["cpu_busy"] is None or snapshot["reason"] in {"starting", "metrics_unavailable"}:
            self.budget = Budget(reason="unavailable", paused=self.paused)
            self.healthy = 0
            return
        capacity = sum(lane["capacity"] for lane in snapshot["lanes"].values())
        target = min(MAX_NODE, max(64, capacity * 8))
        if self.paused:
            # Clearing an emergency is not permission to enlarge the queue.
            # Match Agent recovery reserves, reachable on small real cgroups;
            # the >=1GiB expansion threshold must never permanently latch them.
            safe = (sample["cpu_busy"] < .90 and
                    available >= max(min(512 * 1024**2, total * .20), total * .10))
            self.healthy = self.healthy + 1 if safe else 0
            if self.healthy >= 3 and now - self.changed >= 15:
                self.paused = False
                self.budget = Budget(reason="recovering")
                self.healthy, self.changed = 0, now
            return
        if target < self.budget.node:
            self.budget = self.expanded(target)
            self.healthy, self.changed = 0, now
        if not healthy:
            self.healthy = 0
            return
        self.healthy += 1
        if self.healthy >= 3 and now - self.changed >= 15:
            self.paused = False
            self.budget = self.expanded(target)
            self.healthy, self.changed = 0, now

    @staticmethod
    def expanded(node):
        reserve = min(32, max(8, node // 8))
        return Budget(node, min(MAX_PROJECT, node - reserve), reserve, 8, "adaptive")

    def current(self, now, expected_revision):
        if (self.received is None or not 0 <= now - self.received or now - self.received + self.sample_age > REPORT_TTL
                or self.revision != expected_revision):
            return Budget(reason="unavailable", paused=self.paused)
        return self.budget


def usage(store, device_id, project_id):
    # Durable tool identity suffices for protected read classification. No schema
    # change, payload decryption, caller-provided lane, or in-memory count cache.
    # Ciphertext is ASCII. BLOB length uses byte size, avoiding repeated Unicode
    # character scans of multi-MiB TEXT payloads on the single DB worker.
    reads = ",".join("'" + name + "'" for name in sorted(READ_TOOLS))
    return store.one(f"""SELECT count(*) AS global_count,
        coalesce(sum(length(CAST(payload AS BLOB))),0) AS global_bytes,
        coalesce(sum(device_id=?),0) AS node_count,
        coalesce(sum(CASE WHEN device_id=? THEN length(CAST(payload AS BLOB)) ELSE 0 END),0) AS node_bytes,
        coalesce(sum(device_id=? AND project_id IS ?),0) AS project_count,
        coalesce(sum(CASE WHEN device_id=? AND project_id IS ? THEN length(CAST(payload AS BLOB)) ELSE 0 END),0) AS project_bytes,
        coalesce(sum(device_id=? AND tool IN ({reads})),0) AS node_reads,
        coalesce(sum(device_id=? AND project_id IS ? AND tool IN ({reads})),0) AS project_reads,
        coalesce(sum(device_id=? AND tool='integration_control'),0) AS controls
        FROM operations WHERE {ACTIVE_SQL}""",
        (device_id, device_id, device_id, project_id, device_id, project_id,
         device_id, device_id, project_id, device_id))


def check_admission(budget, counts, tool, payload_bytes):
    """Call under the same DB transaction as idempotency lookup and INSERT."""
    read = tool in READ_TOOLS
    control = tool == "integration_control"
    protected = read or control

    def busy(scope, reason):
        code = "PROJECT_BUSY" if scope == "project" else "HUB_BUSY" if scope == "hub" else "DEVICE_BUSY"
        raise DevError(code, "待完成操作准入队列已满或资源压力过高；请查询原回执，稍后重试新操作",
                       429, retryable=True, admitted=False, retry_after_seconds=budget.retry_after,
                       queue_scope=scope, queue_reason=reason)

    # Absolute caps include control operations; existing receipts/cancel/status
    # never call this function. Keep a small read/control reserve at Hub scale.
    if counts["global_count"] >= MAX_GLOBAL - (0 if protected else 32):
        busy("hub", "admission_count")
    if counts["global_bytes"] + payload_bytes > GLOBAL_BYTES - (0 if protected else 4 * 1024**2):
        busy("hub", "admission_bytes")
    if counts["node_count"] >= MAX_NODE:
        busy("node", "admission_count")
    if counts["project_count"] >= MAX_PROJECT:
        busy("project", "admission_count")
    if counts["node_bytes"] + payload_bytes > NODE_BYTES - (0 if protected else 1024**2):
        busy("node", "admission_bytes")
    if counts["project_bytes"] + payload_bytes > PROJECT_BYTES - (0 if protected else 256 * 1024):
        busy("project", "admission_bytes")
    if control:
        if counts["controls"] >= 8:
            busy("node", "control_reserve")
        return
    if budget.paused and not read:
        busy("node", "resource_pressure")
    if (read and budget.overload_read_reserve
            and counts["node_reads"] < budget.overload_read_reserve
            and counts["project_reads"] < OVERLOAD_PROJECT_READS):
        # A pre-existing heavy backlog may exceed a shrunken ordinary budget.
        # Permit at most eight node-wide reads, seven per project, still behind all
        # hard count/byte ceilings. Never grant this exception to old Agents or
        # treat it as additional execution slots.
        return
    project_limit = budget.project - (budget.read_reserve if not read else 0)
    if counts["project_count"] >= project_limit:
        busy("project", "admission_count")
    limit = budget.node - (budget.read_reserve if not read else 0)
    if counts["node_count"] >= limit:
        busy("node", "admission_count")
    if counts["project_count"] and counts["node_count"] >= budget.node - budget.reserve:
        # A project may use only its reserved read tranche after ordinary
        # capacity fills, never consume the rest of the new-project reserve.
        if not (read and budget.read_reserve and counts["project_reads"] < budget.read_reserve
                and counts["node_reads"] < budget.read_reserve):
            busy("node", "project_fairness")
