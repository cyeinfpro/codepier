"""Unified vps_ssh_v2 transport contract: no SSH host key verification.

All first-party CodePier VPS SSH operations share this single contract:
- protocol: "vps_ssh_v2"
- host_identity_verification: "disabled"

Legacy host trust fields (host_key_policy, host_key_fingerprint, known_hosts_file)
are accepted in compatibility decoders with bounded shape checks and ignored
before entering internal transport models.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

VPS_SSH_PROTOCOL = "vps_ssh_v2"
VPS_HOST_IDENTITY_VERIFICATION = "disabled"
VPS_TRANSPORT_RUNTIME = "asyncssh"

LEGACY_TRUST_FIELDS = frozenset(
    {
        "host_key_policy",
        "host_key_fingerprint",
        "known_hosts_file",
    }
)


class VpsTransportContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["vps_ssh_v2"] = VPS_SSH_PROTOCOL
    host_identity_verification: Literal["disabled"] = VPS_HOST_IDENTITY_VERIFICATION


class ResolvedVpsTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vps_id: str = Field(min_length=1, max_length=128)
    source: Literal["project_bound", "direct"] = "project_bound"
    project_id: str | None = Field(default=None, max_length=128)
    binding_id: str | None = Field(default=None, max_length=128)
    connection_revision: int = Field(default=1, ge=1)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    transport: VpsTransportContract = Field(default_factory=VpsTransportContract)


class SavedCredentialReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vps_id: str = Field(min_length=1, max_length=128)
    credential_source: Literal["saved_vps"] = "saved_vps"


class VpsCapabilityEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    supported: bool
    reason_code: str | None = None


class VpsCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    exec: VpsCapabilityEntry
    terminal: VpsCapabilityEntry
    probe: VpsCapabilityEntry


class VpsExecutionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=128)
    transport_runtime: Literal["asyncssh"] = VPS_TRANSPORT_RUNTIME
    transport_contract: Literal["vps_ssh_v2"] = VPS_SSH_PROTOCOL
    host_identity_verification_applied: Literal["disabled"] = VPS_HOST_IDENTITY_VERIFICATION
    credential_source: Literal["saved_vps", "inline_compat"] = "saved_vps"
    authentication_state: Literal["accepted", "failed", "not_observed"] = "not_observed"
    authentication_method: Literal["password", "keyboard-interactive", "none", "unknown"] = (
        "unknown"
    )
    remote_execution_state: Literal[
        "not_started",
        "running",
        "completed",
        "cancelled",
        "timed_out",
        "remote_outcome_unknown",
    ] = "not_started"
    exit_code: int | None = None
    exit_signal: str | None = None


def capability_entry(supported: bool, reason_code: str | None = None) -> dict[str, Any]:
    return {
        "supported": bool(supported),
        "reason_code": None if supported else reason_code,
    }


def agent_vps_ssh_capability(
    *,
    available: bool,
    exec_supported: bool = True,
    terminal_supported: bool = True,
    probe_supported: bool = True,
) -> dict[str, Any]:
    return {
        "protocol": VPS_SSH_PROTOCOL,
        "host_identity_verification": VPS_HOST_IDENTITY_VERIFICATION,
        "exec": bool(available and exec_supported),
        "terminal": bool(available and terminal_supported),
        "probe": bool(available and probe_supported),
    }


def hub_native_vps_ssh_capability(*, available: bool) -> dict[str, Any]:
    return {
        "protocol": VPS_SSH_PROTOCOL,
        "host_identity_verification": VPS_HOST_IDENTITY_VERIFICATION,
        "readiness": bool(available),
    }


def decode_legacy_ssh_request(raw: dict[str, Any]) -> dict[str, Any]:
    """Strip retired host-trust fields after bounded type/size checks.

    Never validates pin syntax or opens known_hosts_file paths so stale or
    malformed legacy trust metadata can never block a VPS SSH operation.
    """
    if not isinstance(raw, dict):
        raise ValueError("SSH request payload must be a mapping")
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key in LEGACY_TRUST_FIELDS:
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be a string when supplied")
                limit = 4096 if key == "known_hosts_file" else 256
                if len(value) > limit or "\x00" in value:
                    raise ValueError(f"{key} exceeds bounds or contains NUL")
            continue
        cleaned[key] = value
    return cleaned


def normalize_legacy_policy_ignored(
    host_key_policy: str | None = None,
    host_key_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build explicit ignored-legacy metadata when compatibility callers inspect it."""
    payload: dict[str, Any] = {"ignored": True}
    if host_key_policy:
        payload["host_key_policy"] = str(host_key_policy)
    if host_key_fingerprint:
        payload["host_key_fingerprint"] = str(host_key_fingerprint)
    return payload
