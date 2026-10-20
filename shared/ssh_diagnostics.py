"""Bounded SSH attempt metadata, independent of Hub dispatch/replay counts."""

SSH_ATTEMPT_LIMIT = 3
SSH_STAGES = {
    'ssh_attempt': '正在建立 SSH 连接',
    'ssh_attempt_finished': '本次 SSH 尝试已结束',
    'ssh_retry_wait': '等待下一次 SSH 连接尝试',
}
SSH_ERRORS = frozenset({'SSH_AUTH_FAILED', 'SSH_HOST_KEY_CHANGED', 'SSH_HOST_KEY_UNTRUSTED',
    'SSH_COMMAND_OR_CONNECTION_FAILED', 'SSH_TIMEOUT', 'SSH_CANCELLED'})


def safe_ssh_detail(detail):
    """Accept counters and enums only; no command, endpoint or credential text."""
    if not isinstance(detail, dict):
        return {}
    result = {}
    for key, upper in (('attempt', SSH_ATTEMPT_LIMIT), ('attempt_limit', SSH_ATTEMPT_LIMIT),
                       ('duration_ms', 86400000), ('retry_delay_ms', 10000)):
        value = detail.get(key)
        if type(value) is int and 0 <= value <= upper:
            result[key] = value
    value = detail.get('exit_code')
    if type(value) is int and -(2**31) <= value < 2**31:
        result['exit_code'] = value
    for key, values in (
        ('ssh_error', SSH_ERRORS),
        ('authentication', {'succeeded', 'failed', 'not_observed'}),
        ('failure_stage', {'authentication', 'host_verification', 'connection', 'after_authentication', 'not_observed'}),
    ):
        value = detail.get(key)
        if isinstance(value, str) and value in values or value is None and key in detail:
            result[key] = value
    if type(detail.get('retryable')) is bool:
        result['retryable'] = detail['retryable']
    return result
