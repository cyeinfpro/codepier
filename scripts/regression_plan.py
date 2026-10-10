"""Deterministic, exhaustive pytest grouping and evidence aggregation.

A shard is not a full-suite verification. Only a merge with every shard,
identical source/collection fingerprints, and exactly-once passing outcomes is.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def available_workers() -> int:
    return max(1, (getattr(os, "process_cpu_count", os.cpu_count)() or 1))


RESOURCE_LAYOUT = "four-plus-resource-v1"
RESOURCE_NODEIDS = frozenset({
    "tests/test_hub_adaptive_admission_stack.py::test_real_128_waiting_burst_other_project_cancel_restart_and_exact_drain",
})


def plan_jobs(nodeids, markers=None, *, shard_index=0, shard_count=1, resource_shard=False):
    """Keep module fixtures together, except explicitly independent matrix cases."""
    if not 1 <= shard_count <= 64 or not 0 <= shard_index < shard_count:
        raise ValueError("Require 1 <= shard_count <= 64 and 0 <= shard_index < shard_count")
    if len(set(nodeids)) != len(nodeids):
        raise ValueError("Collection contains duplicate node IDs")
    markers = markers or {}
    if resource_shard:
        if shard_count != 5:
            raise ValueError("Resource layout requires four ordinary shards and one resource shard")
        marked = {node for node in nodeids if "resource_regression" in markers.get(node, [])}
        if marked != RESOURCE_NODEIDS or not RESOURCE_NODEIDS <= set(nodeids):
            raise ValueError("Resource test collection/markers differ from the explicit release contract")
    grouped: dict[str, list[str]] = {}
    by_module: dict[str, list[str]] = {}
    for nodeid in nodeids:
        module = nodeid.split("::", 1)[0]
        if not module.startswith("tests/") or ".." in Path(module).parts:
            raise ValueError("Unexpected test path")
        by_module.setdefault(module, []).append(nodeid)
        key = nodeid if "isolated_case" in markers.get(nodeid, []) else module
        grouped.setdefault(key, []).append(nodeid)
    jobs = []
    ordinary_ordinal = 0
    for ordinal, (key, selected) in enumerate(sorted(grouped.items())):
        if resource_shard:
            resource = bool(set(selected) & RESOURCE_NODEIDS)
            if resource and not set(selected) <= RESOURCE_NODEIDS:
                raise ValueError("Resource tests must own their complete isolated job")
            assigned = 4 if resource else ordinary_ordinal % 4
            if not resource:
                ordinary_ordinal += 1
        else:
            assigned = ordinal % shard_count
        if assigned != shard_index:
            continue
        module = selected[0].split("::", 1)[0]
        name = Path(module).with_suffix("").as_posix().removeprefix("tests/").replace("/", "__")
        if key != module:
            name += "--" + fingerprint(key)[:16]
        # A module containing isolated cases must select the remaining node IDs,
        # not run the complete module again and duplicate those cases.
        full_module = by_module[module]
        selectors = [module] if selected == full_module else selected
        marker_names = {marker for nodeid in selected for marker in markers.get(nodeid, [])}
        exclusive = "serial_regression" in marker_names
        limited = not exclusive and bool(marker_names & {"browser", "integration"})
        jobs.append({"name": name, "module": module, "nodeids": selected, "selectors": selectors,
                     "exclusive": exclusive, "limited": limited})
    return jobs


def pytest_command(prefix, directory, selectors, *, coverage=False):
    directory = Path(directory)
    command = [*prefix, "-v", "--tb=short", "-o", "cache_dir=" + str(directory / "cache"),
               "--junitxml=" + str(directory / "results.xml"), "--basetemp=" + str(directory / "tmp")]
    if coverage:
        command += ["--cov=agent", "--cov=hub", "--cov=shared", "--cov=scripts", "--cov-report="]
    return command + list(selectors)


def classify_events(expected, records):
    outcomes, duplicate_phases = {}, []
    for nodeid in expected:
        events = records.get(nodeid, [])
        phases = Counter(event["when"] for event in events)
        if any(count != 1 for count in phases.values()):
            duplicate_phases.append(nodeid)
        if any(event["outcome"] == "failed" for event in events):
            state = "failed"
        elif any(event["outcome"] == "skipped" or event.get("xfail") for event in events):
            state = "skipped"
        elif all(any(event["when"] == phase and event["outcome"] == "passed" for event in events)
                 for phase in ("setup", "call", "teardown")):
            state = "passed"
        else:
            state = "missing"
        outcomes[nodeid] = state
    return outcomes, duplicate_phases


def merge_summaries(summaries):
    """Reject missing/duplicate shards, stale sources, omissions and extra tests."""
    if not summaries:
        raise ValueError("No shard reports supplied")
    first = summaries[0]
    count = first["shard"]["count"]
    if len(summaries) != count or {s["shard"]["index"] for s in summaries} != set(range(count)):
        raise ValueError("Missing or duplicate shard reports")
    layout = first.get("layout", "ordinary")
    if layout not in {"ordinary", RESOURCE_LAYOUT} or (layout == RESOURCE_LAYOUT and count != 5):
        raise ValueError("Unknown or incomplete regression layout")
    expected = first["full_collection"]
    if layout == RESOURCE_LAYOUT and not RESOURCE_NODEIDS <= set(expected):
        raise ValueError("Required resource tests are absent from collection")
    if len(expected) != len(set(expected)) or not expected:
        raise ValueError("Invalid full collection")
    outcomes: dict[str, str] = {}
    for summary in summaries:
        if (summary.get("layout", "ordinary") != layout or
                summary["shard"]["count"] != count or summary["full_collection"] != expected or
                summary["collection_sha256"] != fingerprint(expected) or
                summary["source_inventory_sha256"] != first["source_inventory_sha256"]):
            raise ValueError("Shard source or collection fingerprints differ")
        if layout == RESOURCE_LAYOUT:
            resource_outcomes = set(summary["outcomes"]) & RESOURCE_NODEIDS
            if ((summary["shard"]["index"] == 4 and set(summary["outcomes"]) != RESOURCE_NODEIDS) or
                    (summary["shard"]["index"] != 4 and resource_outcomes)):
                raise ValueError("Resource tests must execute exactly once in the dedicated shard")
        if not summary.get("verified") or summary.get("source_changed_during_run"):
            raise ValueError("A shard is failed, skipped, incomplete or stale")
        if summary.get("unexpected_tests") or summary.get("duplicate_phases") or summary.get("module_failures"):
            raise ValueError("A shard has unexpected execution evidence")
        if set(outcomes) & summary["outcomes"].keys():
            raise ValueError("A test was executed in more than one shard")
        outcomes.update(summary["outcomes"])
    if set(outcomes) != set(expected) or any(value != "passed" for value in outcomes.values()):
        raise ValueError("Merged test outcomes are missing, unexpected or not passing")
    return {"verified": True, "scope": "complete", "layout": layout, "shards": count, "collected": len(expected),
            "counts": {"passed": len(expected), "failed": 0, "skipped": 0, "missing": 0},
            "collection_sha256": fingerprint(expected),
            "source_inventory_sha256": first["source_inventory_sha256"]}
