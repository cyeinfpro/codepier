"""Private Hub↔Agent lifecycle contract.

These actions are deliberately not MCP tools. Only an authenticated panel admin
can queue them for a concrete device, and the Agent accepts only this fixed set.
"""
from __future__ import annotations

DEVICE_ACTIONS = frozenset({"agent_update", "agent_restart", "agent_uninstall"})
ACTIVE_DEVICE_ACTION_STATES = frozenset({"queued", "running", "reconnecting", "cancelling"})
MAX_AGENT_PACKAGE_BYTES = 8 * 1024 * 1024
MANAGEMENT_SCHEMA = 1

ACTION_LABELS = {
    "agent_update": "更新 Agent",
    "agent_restart": "重启 Agent",
    "agent_uninstall": "卸载 Agent",
}


def public_management(value: object) -> dict:
    """Return a small, path-free management declaration safe for the Hub/UI."""
    if not isinstance(value, dict):
        return {"managed": False, "service": False, "reason": "该 Agent 未声明受管安装信息"}
    result = {
        "managed": bool(value.get("managed")),
        "service": bool(value.get("service")),
        "service_kind": str(value.get("service_kind", ""))[:40],
        "installed_version": str(value.get("installed_version", ""))[:80],
        "layout": str(value.get("layout", ""))[:40],
        "status": str(value.get("status", ""))[:40],
        "update_ready": bool(value.get("update_ready")),
        "control_ready": bool(value.get("control_ready")),
        "reason": str(value.get("reason", ""))[:300],
        "last_error": str(value.get("last_error", ""))[:500],
    }
    states={'pending','migrating','completed','rollback','error','recovery_required'}
    result['product']='CodePier' if value.get('product')=='CodePier' else ''
    result['brand_migration']=value.get('brand_migration') if value.get('brand_migration') in states else ''
    return result
