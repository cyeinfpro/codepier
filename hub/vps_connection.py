"""Shared Hub VPS target resolution, credential references, and vps_ssh_v2 capability checks.

All first-party Hub VPS entrypoints (MCP exec, panel probe check, Web SSH terminal,
readiness, and delegation) share this module for resolving authorized VPS targets and
verifying executor vps_ssh_v2 capabilities. Legacy host_key_policy and
host_key_fingerprint fields are never read to make execution or admission decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from shared.util import DevError
from shared.vps_transport import (
    VPS_HOST_IDENTITY_VERIFICATION,
    VPS_SSH_PROTOCOL,
    ResolvedVpsTarget,
    SavedCredentialReference,
    VpsTransportContract,
    agent_vps_ssh_capability,
    capability_entry,
    hub_native_vps_ssh_capability,
)


@dataclass(frozen=True)
class VpsConnectionPlan:
    """Authorized target plan with deferred credential reference (never plaintext secret)."""

    target: ResolvedVpsTarget
    credential: SavedCredentialReference
    source: Literal["project_bound", "direct"]
    row: dict[str, Any]


def parse_agent_vps_ssh(hello: dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse and validate an Agent's advertised agent_vps_ssh capability block."""
    if not isinstance(hello, dict):
        return None
    raw = hello.get("agent_vps_ssh")
    if isinstance(raw, dict):
        if (
            raw.get("protocol") != VPS_SSH_PROTOCOL
            or raw.get("host_identity_verification") != VPS_HOST_IDENTITY_VERIFICATION
        ):
            return None
        return agent_vps_ssh_capability(
            available=True,
            exec_supported=raw.get("exec") is True,
            terminal_supported=raw.get("terminal") is True,
            probe_supported=raw.get("probe") is True,
        )
    if hello.get("vps_ssh_protocol") == VPS_SSH_PROTOCOL:
        return agent_vps_ssh_capability(
            available=True,
            exec_supported=hello.get("vps_exec") is True,
            terminal_supported=hello.get("vps_terminal") is True,
            probe_supported=hello.get("probe") is True,
        )
    return None


def supports_vps_ssh_v2(
    agent: Any,
    operation: Literal["exec", "terminal", "probe"],
) -> bool:
    """Return True iff the live agent connection explicitly advertises vps_ssh_v2 for operation."""
    if (
        agent is None
        or getattr(agent, "closed", False)
        or getattr(agent, "unusable", False)
    ):
        return False
    caps = getattr(agent, "agent_vps_ssh", None)
    if (
        isinstance(caps, dict)
        and caps.get("protocol") == VPS_SSH_PROTOCOL
        and caps.get("host_identity_verification") == VPS_HOST_IDENTITY_VERIFICATION
    ):
        return caps.get(operation) is True
    if getattr(agent, "vps_ssh_v2_supported", False) is True:
        if operation == "terminal":
            return True
        if operation == "probe":
            return getattr(agent, "probe_supported", False) is True
        if operation == "exec":
            return getattr(agent, "vps_exec_supported", False) is True
    return False


def require_vps_capability(
    agent: Any,
    operation: Literal["exec", "terminal", "probe"],
    *,
    device_online: bool = False,
) -> None:
    """Enforce vps_ssh_v2 capability on the selected agent; never silently fall back to strict."""
    if supports_vps_ssh_v2(agent, operation):
        return
    if (
        agent is None
        or getattr(agent, "closed", False)
        or getattr(agent, "unusable", False)
    ) and not device_online:
        raise DevError("AGENT_OFFLINE", "关联节点当前离线，无法执行 VPS SSH 操作", 409)
    raise DevError(
        "VPS_TRANSPORT_UPGRADE_REQUIRED",
        f"当前节点缺少 {VPS_SSH_PROTOCOL} ({operation}) 能力，请升级 Agent 后重试",
        409,
    )


def build_resolved_target(
    row: dict[str, Any],
    *,
    source: Literal["project_bound", "direct"],
    project_id: str | None = None,
    binding_id: str | None = None,
) -> ResolvedVpsTarget:
    """Construct an immutable ResolvedVpsTarget without reading retired host-trust columns."""
    vps_id = str(row["id"])
    if source == "project_bound":
        if not project_id:
            raise DevError("PROJECT_NOT_FOUND", "项目绑定 VPS 缺少有效项目标识", 400)
        resolved_binding = str(binding_id or row.get("binding_id") or "")
        if not resolved_binding:
            raise DevError("VPS_AUTHORIZATION_CHANGED", "VPS 项目绑定无效", 403)
        resolved_project = str(project_id)
    elif source == "direct":
        resolved_binding = str(binding_id or row.get("binding_id") or f"direct:{vps_id}")
        resolved_project = None
    else:
        raise ValueError(f"Unknown VPS authorization source: {source}")
    return ResolvedVpsTarget(
        vps_id=vps_id,
        source=source,
        project_id=resolved_project,
        binding_id=resolved_binding,
        connection_revision=int(row["connection_revision"]),
        host=str(row["host"]),
        port=int(row["port"]),
        username=str(row["username"]),
        transport=VpsTransportContract(),
    )


def resolve_vps_connection_plan(
    vps_service: Any,
    *,
    request: dict[str, Any],
    source: Literal["project_bound", "direct"] = "project_bound",
    project_id: str | None = None,
    space_id: str | None = None,
    agent: Any = None,
    operation: Literal["exec", "terminal", "probe"] | None = None,
    device_online: bool = False,
) -> VpsConnectionPlan:
    """Resolve an authorized VPS target and credential reference without decrypting secrets.

    Distinguishes project-bound and direct VPS authorization sources while applying
    the same vps_ssh_v2 transport contract. Never inspects host_key_policy or
    host_key_fingerprint.
    """
    if source == "project_bound":
        row = vps_service._authorized_row(request, project_id, credential=False)
    elif source == "direct":
        if not space_id:
            raise DevError("VPS_AUTHORIZATION_CHANGED", "直连 VPS 缺少当前空间授权上下文", 403)
        row = vps_service._authorized_direct_row(request, space_id, credential=False)
    else:
        raise ValueError(f"Unknown VPS authorization source: {source}")

    if operation is not None:
        require_vps_capability(agent, operation, device_online=device_online)

    target = build_resolved_target(
        row,
        source=source,
        project_id=project_id,
        binding_id=row.get("binding_id"),
    )
    public_row = {k: v for k, v in dict(row).items() if k != "secret"}
    return VpsConnectionPlan(
        target=target,
        credential=SavedCredentialReference(vps_id=target.vps_id),
        source=source,
        row=public_row,
    )


def compute_exec_capability(
    runtime: Any,
    row: dict[str, Any],
    eligible_projects: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute real vps_ssh_v2 exec capability from currently connected project Agents."""
    if not row.get("enabled"):
        return capability_entry(False, "VPS_DISABLED")
    if not eligible_projects:
        return capability_entry(False, "VPS_NOT_ASSIGNED")
    any_online = False
    for project in eligible_projects:
        device_id = project["device_id"]
        if not runtime.online(device_id):
            continue
        any_online = True
        con = runtime.connections.get(device_id)
        if supports_vps_ssh_v2(con, "exec"):
            return capability_entry(True)
    if any_online:
        return capability_entry(False, "VPS_TRANSPORT_UPGRADE_REQUIRED")
    return capability_entry(False, "AGENT_OFFLINE")


def compute_vps_capabilities(
    runtime: Any,
    row: dict[str, Any],
    *,
    eligible_exec_projects: list[dict[str, Any]],
    terminal_summary: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build per-entrypoint capabilities (exec/terminal/probe) from real runtime state."""
    exec_cap = compute_exec_capability(runtime, row, eligible_exec_projects)
    terminal_available = bool(terminal_summary.get("available"))
    terminal_reason = None if terminal_available else terminal_summary.get("reason_code")
    probe_available = bool(terminal_summary.get("check_ready"))
    probe_reason = (
        None
        if probe_available
        else (terminal_summary.get("reason_code") or "VPS_TRANSPORT_UPGRADE_REQUIRED")
    )
    return {
        "exec": exec_cap,
        "terminal": capability_entry(terminal_available, terminal_reason),
        "probe": capability_entry(probe_available, probe_reason),
    }


def hub_native_readiness_capability(runtime: Any) -> dict[str, Any]:
    """Report Hub-native readiness capability independently of project Agent status."""
    readiness = getattr(runtime, "vps_readiness", None)
    available = bool(readiness is not None and getattr(readiness, "available", False))
    return hub_native_vps_ssh_capability(available=available)
