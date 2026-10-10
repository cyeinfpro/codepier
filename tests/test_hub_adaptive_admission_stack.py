"""Loopback Hub + Agent, real journals and OS counters; no model invocation."""
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from agent.resource_pressure import ResourceSampler
from shared.util import atomic_json
from tests.support import running_stack, wait_for


pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.serial_regression]


def burst_capacity_ready(limits):
    # 128 new reads plus the one running holder, independent of host CPU count.
    return limits["reason"] == "adaptive" and limits["project_pending_limit"] >= 129


def admission_probe_due(now, previous):
    return now - previous >= 3


def owned_cpu_percent(stack):
    """Diagnostic-only percentages for this test's three processes; no argv."""
    roles = {os.getpid(): "test", stack.hub.pid: "hub", stack.agent.pid: "agent"}
    try:
        output = subprocess.check_output(
            ["ps", "-p", ",".join(map(str, roles)), "-o", "pid=,pcpu="],
            text=True, timeout=1,
        )
        return {roles[int(pid)]: float(cpu) for pid, cpu in
                (line.split() for line in output.splitlines()) if int(pid) in roles}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def settings(stack, client=None):
    return stack.must((client or stack.client).get("/api/settings/scheduler", params={"device": stack.device}))


async def burst(stack, requests):
    async with httpx.AsyncClient(base_url=stack.url, cookies=stack.client.cookies,
            headers={"X-RD-CSRF": stack.client.headers["X-RD-CSRF"]},
            timeout=30, trust_env=False,
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=128)) as client:
        async def submit(args):
            response = await client.post("/api/tools/call", json={"tool": "read", "arguments": args})
            assert response.status_code == 200, response.text
            return response.json()
        return await asyncio.gather(*(submit(args) for args in requests))


def test_real_128_waiting_burst_other_project_cancel_restart_and_exact_drain(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_CALL_WAIT_SECONDS", "0")
    with running_stack(tmp_path / "admission-stack") as stack:
        release = stack.projectalpha / "release-hold"
        counter = stack.projectalpha / "hold-count"
        stack.stop_agent()
        code = ("from pathlib import Path; import time; "
                "p=Path('hold-count'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
                "print('HOLDING',flush=True); "
                "\nwhile not Path('release-hold').exists(): time.sleep(.05)")
        stack.config["tasks"]["admission_hold"] = {
            "command": [sys.executable, "-u", "-c", code],
            "projects": ["ProjectAlpha"], "timeout": 180,
        }
        atomic_json(stack.config_path, stack.config)
        stack.start_agent()
        first = settings(stack)
        assert first["durable_admission"]["project_pending_limit"] == 32

        # Read-only independent OS observations diagnose an unmet prerequisite.
        # They are not the Agent heartbeat and never feed or bypass admission.
        observer = ResourceSampler()
        observed_at = time.monotonic()
        last_observation = -float("inf")
        probe_count = 0

        def healthy():
            nonlocal last_observation, probe_count
            now = time.monotonic()
            if not admission_probe_due(now, last_observation):
                return None
            last_observation = now
            probe_count += 1
            state = settings(stack, probe_client)
            limits = state["durable_admission"]
            sample = observer.sample()
            reported = state["reported"] or {}
            print(json.dumps({"admission_prerequisite": {
                "elapsed_seconds": round(now - observed_at, 3), "probe_count": probe_count,
                "api_state": state["state"], "limits": limits,
                "reported_reason": reported.get("reason"),
                "reported_lanes": reported.get("lanes"),
                "owned_ps_cpu_percent": owned_cpu_percent(stack), "cpu_count": os.cpu_count(),
                "independent_os_observer": {
                    "source": sample.source, "cpu_busy": sample.cpu_busy,
                    "memory_available": sample.memory_available,
                    "memory_total": sample.memory_total, "io_stall": sample.io_stall,
                    "age_seconds": round(time.monotonic() - sample.measured_at, 3),
                },
            }}, sort_keys=True), flush=True)
            return state if burst_capacity_ready(limits) else None
        # Observe no faster than the real sampler, with one reused connection.
        # Actual authenticated Agent heartbeats still determine health/capacity.
        with httpx.Client(base_url=stack.url, cookies=stack.client.cookies,
                timeout=35, trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as probe_client:
            expanded = wait_for(healthy, timeout=100)
        assert sum(lane["capacity"] for lane in expanded["reported"]["lanes"].values()) < expanded["durable_admission"]["node_pending_limit"]
        held = stack.call("exec", {"project": "ProjectAlpha", "task": "admission_hold",
                                  "idempotency_key": uuid.uuid4().hex})
        hold_id = held["operation_id"]
        wait_for(lambda: counter.exists(), timeout=15)
        requests = [{"project": "ProjectAlpha", "path": "README.md", "idempotency_key": uuid.uuid4().hex}
                    for _ in range(128)]
        ids = []
        try:
            receipts = asyncio.run(burst(stack, requests))
            ids = [item["operation_id"] for item in receipts]
            assert len(set(ids)) == 128
            assert all(item["pending"] for item in receipts)
            with sqlite3.connect(stack.hubdir / "hub.sqlite3") as db:
                pending = db.execute("SELECT count(*) FROM operations WHERE project_id=? AND state IN ('queued','running','reconnecting','cancelling')",
                                     (stack.project["id"],)).fetchone()[0]
            assert pending == 129

            # The second project is independent of the held resource and queue.
            other = stack.call("read", {"project": "ProjectGamma", "path": "README.md",
                                       "idempotency_key": uuid.uuid4().hex})
            assert stack.poll(other["operation_id"], timeout=15)["state"] == "succeeded"
            replay = asyncio.run(burst(stack, requests[:8]))
            assert [item["operation_id"] for item in replay] == ids[:8]
            cancelled = ids[:4]
            for identifier in cancelled:
                stack.call("operations_cancel", {"operation_id": identifier})
            for identifier in cancelled:
                assert stack.poll(identifier, timeout=15)["state"] == "cancelled"

            # Kill/restart only the fixture Hub. Existing Agent work/journal live.
            stack.hub.terminate()
            stack.hub.wait(timeout=12)
            stack.start_hub()
            wait_for(lambda: any(d["id"] == stack.device and d["online"]
                                for d in stack.client.get("/api/devices").json()["devices"]), timeout=20)
            fallback = settings(stack)["durable_admission"]
            assert fallback["project_pending_limit"] == 32
            replay = asyncio.run(burst(stack, requests[4:8]))
            assert [item["operation_id"] for item in replay] == ids[4:8]
            blocked = stack.call("read", {"project": "ProjectAlpha", "path": "README.md",
                                         "idempotency_key": uuid.uuid4().hex}, raw=True)
            assert blocked.status_code == 429
            assert blocked.json()["error"]["admitted"] is False
        finally:
            release.write_text("release", encoding="utf-8")
        assert stack.poll(hold_id, timeout=30)["state"] == "succeeded"
        expected = hashlib.sha256((stack.projectalpha / "README.md").read_bytes()).hexdigest()
        for identifier in ids[4:]:
            result = stack.poll(identifier, timeout=30)
            assert result["state"] == "succeeded", result
            assert result["result"]["data"]["sha256"] == expected
        assert counter.read_text() == "x"
        with sqlite3.connect(stack.hubdir / "hub.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM operations WHERE state IN ('queued','running','reconnecting','cancelling')").fetchone()[0] == 0
        summary = {"requested": 128, "rejected": 0, "queued_peak_with_holder": 129,
                   "completed": 124, "explicitly_cancelled": 4, "holder_effects": 1,
                   "other_project_completed": True, "restart_preserved_ids": True,
                   "host_os_telemetry": True, "expanded_limits": expanded["durable_admission"]}
        (tmp_path / "admission-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, sort_keys=True))
