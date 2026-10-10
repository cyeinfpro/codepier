"""Owner-managed performance preferences; no project or filesystem authority."""
from __future__ import annotations

import hashlib
import json

from hub import iam
from shared.scheduler_config import validate_scheduler_overrides
from shared.util import DevError


def revision(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stored_config(store, device_id):
    row = store.one("SELECT value FROM meta WHERE key=?", ("device_scheduler:" + device_id,))
    try:
        value = validate_scheduler_overrides(json.loads(row["value"])) if row else {}
    except (ValueError, TypeError):
        # Invalid stored preferences cannot open capacity or erase the Agent's
        # own ceilings. Surface corruption to the panel, never silently replace.
        raise DevError("SCHEDULER_SETTINGS_INVALID", "已保存的调度设置无效，请由节点管理者核对", 409)
    return {"revision": revision(value), "config": value}


def packet(store, device_id):
    try:
        return {"type": "scheduler_config", **stored_config(store, device_id)}
    except DevError:
        # An explicit rejected packet keeps the Agent's last good limits.
        # Omitting the field would look like an old Hub resetting overrides.
        return {"type": "scheduler_config", "revision": "", "error": "SCHEDULER_SETTINGS_INVALID"}


def public_snapshot(value):
    if not isinstance(value, dict):
        return None
    allowed_reasons = {"starting", "fixed", "metrics_unavailable", "memory_critical",
                       "memory_recovery", "recovering", "resource_pressure", "healthy", "cooldown"}
    reason = value.get("reason")
    result = {"scope": "node", "reason": reason if isinstance(reason, str) and reason in allowed_reasons else "metrics_unavailable"}
    for key in ("running", "queued"):
        number = value.get(key)
        if type(number) is not int or not 0 <= number <= 100000:
            return None
        result[key] = number
    lanes = value.get("lanes")
    if not isinstance(lanes, dict):
        return None
    result["lanes"] = {}
    for name in ("execution", "read", "remote"):
        lane = lanes.get(name)
        if not isinstance(lane, dict):
            return None
        numbers = {}
        for key in ("running", "queued", "capacity"):
            number = lane.get(key)
            if type(number) is not int or not 0 <= number <= (64 if key == "capacity" else 100000):
                return None
            numbers[key] = number
        result["lanes"][name] = numbers
    return result


def view(store, runtime, principal, device_id):
    iam.require_device(store, principal, device_id, manage=True)
    row = store.one("SELECT info FROM devices WHERE id=? AND space_id=?", (device_id, principal.space_id))
    if not row:
        raise DevError("NOT_FOUND", "设备不存在", 404)
    try:
        info = json.loads(row["info"] or "{}")
    except (TypeError, ValueError):
        info = {}
    saved = stored_config(store, device_id)
    reported = public_snapshot(info.get("scheduler")) if isinstance(info, dict) else None
    supported = isinstance(info, dict) and info.get("scheduler_protocol") == 1
    applied = supported and info.get("scheduler_revision") == saved["revision"]
    rejected = isinstance(info, dict) and info.get("scheduler_error") == "SCHEDULER_SETTINGS_INVALID"
    # Return only IDs already present in the saved preferences, never names or
    # details from projects that moved outside this node/tenant. Removal remains
    # an explicit owner edit; merely reading this view must not change quotas.
    stale_project_limits = sorted(project_id for project_id in saved["config"].get("project_limits", {})
        if not store.one("SELECT id FROM projects WHERE id=? AND device_id=? AND space_id=?",
                         (project_id, device_id, principal.space_id)))
    admission = runtime._admission_budget(device_id)
    return {"device_id": device_id, **saved, "reported": reported,
            "durable_admission": {"node_pending_limit": admission.node,
                "project_pending_limit": admission.project, "reason": admission.reason,
                "new_heavy_admission_paused": admission.paused,
                "overload_read_reserve": admission.overload_read_reserve,
                "counts_include": ["queued", "running", "reconnecting", "cancelling"]},
            "stale_project_limits": stale_project_limits,
            "state": "offline" if not runtime.online(device_id) else "unsupported" if not supported else "applied" if applied else "rejected" if rejected else "pending"}


def save(store, principal, device_id, config, expected_revision):
    iam.require_device(store, principal, device_id, manage=True)
    try:
        config = validate_scheduler_overrides(config)
    except ValueError as exc:
        raise DevError("SCHEDULER_SETTINGS_INVALID", str(exc), 422) from exc
    # A project quota must belong to this exact node and tenant.
    for project_id in config.get("project_limits", {}):
        if not store.one("SELECT id FROM projects WHERE id=? AND device_id=? AND space_id=?", (project_id, device_id, principal.space_id)):
            raise DevError("SCHEDULER_PROJECT_MISMATCH", "项目并发限额必须属于当前节点和空间", 422)
    current = stored_config(store, device_id)
    if current["revision"] != expected_revision and config != current["config"]:
        raise DevError("SETTINGS_CHANGED", "调度设置已在其他窗口修改，草稿已保留，请重新读取", 409)
    if config != current["config"]:
        store.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      ("device_scheduler:" + device_id, json.dumps(config, sort_keys=True, separators=(",", ":"))))
        store.audit(principal.actor, "scheduler.settings.updated", device_id,
                    detail={"fields": sorted(config), "project_count": len(config.get("project_limits", {})),
                            "revision": revision(config)})
    return {"revision": revision(config), "config": config}
