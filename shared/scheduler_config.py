"""Typed scheduler preferences, shared by Hub forms and the Agent resolver."""

from __future__ import annotations

import os

QUEUE_REASONS = {
    "resource_conflict": "等待冲突的文件或服务资源释放",
    "project_limit": "等待本项目并发额度",
    "remote_target_limit": "等待该远程目标并发额度",
    "resource_pressure": "等待节点资源压力回落",
    "lane_capacity": "等待同类执行通道额度",
    "project_fairness": "等待项目公平轮转",
}
SCHEDULER_LANES = frozenset({"execution", "read", "remote"})


def safe_queue_detail(detail):
    """Only fixed enums and bounded counts cross the diagnostic boundary."""
    if not isinstance(detail, dict):
        return {}
    result = {}
    reason, lane = detail.get("queue_reason"), detail.get("lane")
    if isinstance(reason, str) and reason in QUEUE_REASONS:
        result["queue_reason"] = reason
    if isinstance(lane, str) and lane in SCHEDULER_LANES:
        result["lane"] = lane
    kinds = detail.get("resource_kinds")
    if isinstance(kinds, list):
        kinds = sorted(
            {
                value
                for value in kinds[:64]
                if isinstance(value, str) and value in {"path", "service"}
            }
        )
        if kinds:
            result["resource_kinds"] = kinds
    for key, maximum in (
        ("blocked_count", 65536),
        ("bypassed_count", 65536),
        ("queue_age_ms", 7 * 86400000),
    ):
        number = detail.get(key)
        if type(number) is int and 0 <= number <= maximum:
            result[key] = number
    return result


def is_metadata_observation(tool, args):
    """Minimal project indexing is a bounded observation, never a baseline."""
    return (
        tool == "open_workspace"
        and isinstance(args, dict)
        and not args.get("capture_baseline")
        and args.get("context_detail", "minimal") == "minimal"
    )


FIELDS = frozenset(
    {
        "adaptive",
        "minimum",
        "maximum",
        "initial",
        "read_limit",
        "remote_limit",
        "remote_target_limit",
        "project_limits",
        "resource_semantics",
    }
)


def validate_scheduler_overrides(value, *, effective=False):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("scheduler 必须是仅含已知设置的对象")
    result = dict(value)
    if "adaptive" in result and type(result["adaptive"]) is not bool:
        raise ValueError("scheduler.adaptive 必须是 true/false")
    for key in FIELDS - {"adaptive", "project_limits", "resource_semantics"}:
        if key in result and (type(result[key]) is not int or not 1 <= result[key] <= 64):
            raise ValueError(f"scheduler.{key} 必须是 1–64 的整数")
    if "resource_semantics" in result and (
        not isinstance(result["resource_semantics"], str)
        or result["resource_semantics"] not in {"strict", "relaxed"}
    ):
        raise ValueError("scheduler.resource_semantics 必须是 strict 或 relaxed")
    projects = result.get("project_limits", {})
    # Each source remains bounded to 256 entries. The effective configuration
    # may retain the disjoint union of both sources without dropping local caps.
    project_count = 512 if effective else 256
    if not isinstance(projects, dict) or len(projects) > project_count:
        raise ValueError(f"scheduler.project_limits 必须是最多 {project_count} 个项目的对象")
    for key, limit in projects.items():
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 200
            or type(limit) is not int
            or not 1 <= limit <= 64
        ):
            raise ValueError("项目并发限额必须为 1–64，且使用真实项目 ID")
    if "project_limits" in result:
        result["project_limits"] = dict(projects)
    minimum = result.get("minimum", 1)
    maximum = result.get("maximum", 64)
    if minimum > maximum or not minimum <= result.get("initial", minimum) <= maximum:
        raise ValueError("scheduler 必须满足 minimum ≤ initial ≤ maximum")
    return result


def validate_scheduler(value, *, effective=False):
    overrides = validate_scheduler_overrides({} if value is None else value, effective=effective)
    defaults = {
        "adaptive": True,
        "minimum": 2,
        "maximum": min(32, max(4, (os.cpu_count() or 2) * 2)),
        "initial": 8,
        "read_limit": 8,
        "remote_limit": 12,
        "remote_target_limit": 3,
        "project_limits": {},
        "resource_semantics": "strict",
    }
    result = {**defaults, **overrides}
    if "initial" not in overrides:
        result["initial"] = min(result["initial"], result["maximum"])
    if not result["minimum"] <= result["initial"] <= result["maximum"]:
        raise ValueError("scheduler 必须满足 minimum ≤ initial ≤ maximum")
    return result


def effective_scheduler(local, requested):
    """Panel preferences cannot exceed this Agent owner's configured ceilings."""
    local = validate_scheduler(local)
    requested = validate_scheduler_overrides(requested)
    merged = {**local, **requested}
    for field in ("maximum", "read_limit", "remote_limit", "remote_target_limit"):
        merged[field] = min(local[field], merged[field])
    merged["minimum"] = min(merged["minimum"], merged["maximum"])
    merged["initial"] = max(merged["minimum"], min(merged["initial"], merged["maximum"]))
    merged["project_limits"] = {**local["project_limits"], **requested.get("project_limits", {})}
    for key, limit in local["project_limits"].items():
        merged["project_limits"][key] = min(limit, merged["project_limits"][key])
    return validate_scheduler(merged, effective=True)
