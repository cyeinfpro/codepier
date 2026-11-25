"""Bounded SSH attempt metadata, independent of Hub dispatch/replay counts."""

SSH_ATTEMPT_LIMIT = 3
SSH_STAGES = {
    "ssh_attempt": "正在建立 SSH 连接",
    "ssh_attempt_finished": "本次 SSH 尝试已结束",
    "ssh_retry_wait": "等待下一次 SSH 连接尝试",
    "vps_artifact_connect": "正在建立 VPS 传包连接",
    "vps_artifact_write": "正在写入 VPS 临时区",
    "vps_artifact_verify": "正在回读 VPS 文件并核对 SHA-256",
    "vps_artifact_commit": "正在提交已校验的 VPS 产物",
}
SSH_ERRORS = frozenset(
    {
        "SSH_AUTH_FAILED",
        "SSH_AUTH_CHALLENGE_REQUIRED",
        "SSH_CONNECT_TIMEOUT",
        "SSH_CONNECTION_FAILED",
        "SSH_CHANNEL_CLOSED",
        "SSH_PROTOCOL_NEGOTIATION_FAILED",
        "VPS_TRANSPORT_UPGRADE_REQUIRED",
        "SSH_HOST_KEY_CHANGED",
        "SSH_HOST_KEY_UNTRUSTED",
        "SSH_COMMAND_OR_CONNECTION_FAILED",
        "SSH_TIMEOUT",
        "SSH_CANCELLED",
    }
)


def safe_ssh_detail(detail):
    """Accept counters and enums only; no command, endpoint or credential text."""
    if not isinstance(detail, dict):
        return {}
    result = {}
    for key, upper in (
        ("attempt", SSH_ATTEMPT_LIMIT),
        ("attempt_limit", SSH_ATTEMPT_LIMIT),
        ("duration_ms", 86400000),
        ("retry_delay_ms", 10000),
    ):
        value = detail.get(key)
        if type(value) is int and 0 <= value <= upper:
            result[key] = value
    value = detail.get("exit_code")
    if type(value) is int and -(2**31) <= value < 2**31:
        result["exit_code"] = value
    for key, values in (
        ("ssh_error", SSH_ERRORS),
        ("authentication", {"succeeded", "failed", "not_observed"}),
        ("authentication_state", {"accepted", "failed", "not_observed"}),
        ("authentication_method", {"password", "keyboard-interactive", "none", "unknown"}),
        ("transport_runtime", {"asyncssh", "openssh"}),
        ("transport_contract", {"vps_ssh_v2"}),
        ("host_identity_verification_applied", {"disabled"}),
        (
            "remote_execution_state",
            {
                "not_started",
                "running",
                "completed",
                "cancelled",
                "timed_out",
                "remote_outcome_unknown",
            },
        ),
        (
            "failure_stage",
            {
                "authentication",
                "host_verification",
                "connection",
                "after_authentication",
                "not_observed",
            },
        ),
    ):
        value = detail.get(key)
        if isinstance(value, str) and value in values or value is None and key in detail:
            result[key] = value
    if type(detail.get("retryable")) is bool:
        result["retryable"] = detail["retryable"]
    return result
