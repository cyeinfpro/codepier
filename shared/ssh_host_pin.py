"""Validation for an SSH public-key fingerprint confirmed from a trusted console."""

import base64
import binascii
import re


CONFIRM_CHANGE = "confirm-change"
_OPENSSH_SHA256 = re.compile(r"SHA256:([A-Za-z0-9+/]{43})\Z")


def normalize_host_pin(value: str) -> str:
    match = _OPENSSH_SHA256.fullmatch(value.strip()) if isinstance(value, str) else None
    message = "请填写从 VPS 云控制台核验的 SHA256 主机指纹"
    if match is None:
        raise ValueError(message)
    payload = match[1]
    try:
        decoded = base64.b64decode(payload + "=", validate=True)
    except binascii.Error:
        raise ValueError(message) from None
    if base64.b64encode(decoded).decode("ascii").rstrip("=") != payload:
        raise ValueError(message)
    return "SHA256:" + payload


def validate_optional_host_pin(policy: str, fingerprint: str = "") -> str:
    if policy == CONFIRM_CHANGE:
        return normalize_host_pin(fingerprint)
    if fingerprint != "":
        raise ValueError("只有指纹变化时确认模式可保存控制台核验的指纹")
    return ""


def host_pin_supported(agent) -> bool:
    """The capability belongs to the current authenticated Agent connection."""
    return (
        agent is not None
        and not agent.closed
        and getattr(agent, "host_pin_supported", False) is True
    )
