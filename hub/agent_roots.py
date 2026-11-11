"""Panel-only device authorization and durable directory-grant receipts."""

from hub import iam
from hub.principal import refresh_principal
from shared.agent_roots_contracts import PANEL_ROOTS_TOOLS
from shared.tool_protocol import require_compatible
from shared.util import DevError


def authorize(runtime, principal, device_id):
    principal = refresh_principal(runtime.store, principal)
    if principal.grant_id or not principal.admin or not principal.actor.startswith("panel:"):
        raise DevError(
            "OWNER_REQUIRED", "目录授权仅允许已登录面板的管理员操作，MCP 不能扩大目录权限", 403
        )
    device = iam.require_device(runtime.store, principal, device_id, manage=True)
    return principal, device


def availability(runtime, device):
    if not device["enabled"]:
        return "disabled"
    device_id = device["id"]
    if not runtime.online(device_id):
        return "offline"
    connection = runtime.connections.get(device_id)
    if connection is None or not runtime.connection_authorized(device_id, connection):
        return "offline"
    peer = getattr(connection, "tool_protocol", None)
    if peer is None or peer.legacy:
        return "unsupported"
    try:
        for name in PANEL_ROOTS_TOOLS:
            require_compatible(peer, name)
    except DevError:
        return "unsupported"
    return "supported"


def device_project(runtime, principal, device_id, *, require_online=True):
    principal, device = authorize(runtime, principal, device_id)
    if require_online:
        state = availability(runtime, device)
        if state != "supported":
            codes = {
                "offline": "AGENT_OFFLINE",
                "disabled": "DEVICE_DISABLED",
                "unsupported": "AGENT_UPGRADE_REQUIRED",
            }
            raise DevError(
                codes[state],
                "节点离线、停用或尚未支持目录管理；未提交新的授权操作",
                409,
                admitted=False,
            )
    return {
        "id": None,
        "alias": "",
        "root": "",
        "mode": "write",
        "allow_tasks": False,
        "device_id": device_id,
    }


def receipt(runtime, principal, device_id, key):
    principal, _ = authorize(runtime, principal, device_id)
    row = runtime.store.one(
        """SELECT id FROM operations WHERE space_id=? AND actor=?
        AND device_id=? AND project_id IS NULL AND grant_id IS NULL
        AND tool='agent_roots_set' AND idem=?""",
        (principal.space_id, principal.actor, device_id, key),
    )
    if row is None:
        return {
            "state": "not_found",
            "pending": False,
            "operation_id": None,
            "outcome_confirmed": False,
            "replay_recommended": False,
        }
    operation = runtime.operation(row["id"], principal, {"include_output": False})
    result = operation.get("result") or {}
    data = result.get("data") or {}
    uncertain = data.get("outcome_confirmed") is False or data.get("state") == "needs_review"
    return {
        "state": operation["state"],
        "pending": operation.get("pending", False),
        "operation_id": row["id"],
        "result": operation.get("result"),
        "error": operation.get("error"),
        "outcome_confirmed": operation["state"] in {"succeeded", "failed", "cancelled"}
        and not uncertain,
        "replay_recommended": False,
    }
