"""Immutable, explicitly approved managed-command budget (wire protocol 1).

The consumer lease only admits a *start*. A started process owns a separate,
non-renewable deadline. Reconnection, output, and heartbeat never extend it.
No shell isolation or new authority is implied by this transport contract.
"""
from __future__ import annotations

import math

from shared.util import DevError

PROTOCOL = 1
IDENTITIES = ('goal_id', 'work_item_id', 'approval_id', 'project_id', 'grant_id')
FIELDS = frozenset((*IDENTITIES, 'version', 'attempt', 'fencing_token',
    'admitted_at', 'hard_until', 'goal_expires_at', 'timeout_seconds',
    'consumer_lease_until', 'consumer_loss_policy'))


def validate_budget(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise DevError('BOUNDED_EXECUTION_INVALID', '有界命令预算缺失或格式无效', 422)
    result = dict(value)
    if (type(result['version']) is not int or result['version'] != PROTOCOL
            or result['consumer_loss_policy'] != 'finish_started_within_budget'):
        raise DevError('BOUNDED_EXECUTION_UNSUPPORTED', '不支持此有界命令协议或失联策略', 409)
    if any(not isinstance(result[k], str) or not 1 <= len(result[k]) <= 128 for k in IDENTITIES):
        raise DevError('BOUNDED_EXECUTION_INVALID', '原工作身份无效', 422)
    if any(type(result[k]) is not int or result[k] < 1 for k in ('attempt', 'fencing_token', 'timeout_seconds')):
        raise DevError('BOUNDED_EXECUTION_INVALID', '原执行世代或期限无效', 422)
    if result['attempt'] > 3 or result['timeout_seconds'] > 86400:
        raise DevError('BOUNDED_EXECUTION_INVALID', '命令预算超出协议上限', 422)
    for name in ('admitted_at', 'hard_until', 'goal_expires_at', 'consumer_lease_until'):
        number = result[name]
        if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
            raise DevError('BOUNDED_EXECUTION_INVALID', '命令预算必须使用有限时间戳', 422)
    start, hard = result['admitted_at'], result['hard_until']
    if (not start < hard <= result['goal_expires_at']
            or hard - start > result['timeout_seconds'] + 0.001
            or not start < result['consumer_lease_until'] <= result['goal_expires_at']):
        raise DevError('BOUNDED_EXECUTION_INVALID', '命令不能越过原批准、原领取期限或预算', 422)
    return result


def make_budget(goal: dict, item: dict, grant_id: str, now: float,
                requested_seconds: int, approved_seconds: int) -> dict:
    if (type(approved_seconds) is not int or not 1 <= approved_seconds <= 86400
            or type(requested_seconds) is not int or requested_seconds < 1):
        raise DevError('BOUNDED_EXECUTION_NOT_APPROVED', '需要明确的房主命令时间预算', 403)
    timeout = min(requested_seconds, approved_seconds, int(goal['expires_at'] - now))
    if timeout < 1 or (item['lease_until'] or 0) <= now:
        raise DevError('WORK_LEASE_EXPIRED', '原批准或工作租约已过期，不能开始命令', 409)
    return validate_budget({
        'version': PROTOCOL, 'goal_id': goal['id'], 'work_item_id': item['id'],
        'approval_id': goal['approval_id'], 'project_id': item['project_id'],
        'grant_id': grant_id, 'attempt': item['attempt'], 'fencing_token': item['fencing_token'],
        'admitted_at': now, 'hard_until': now + timeout, 'goal_expires_at': goal['expires_at'],
        'timeout_seconds': timeout, 'consumer_lease_until': item['lease_until'],
        'consumer_loss_policy': 'finish_started_within_budget',
    })


def execution_timeout(value: object, *, project_id: str, requested_seconds: int,
                      now: float) -> float:
    budget = validate_budget(value)
    if budget['project_id'] != project_id or requested_seconds != budget['timeout_seconds']:
        raise DevError('BOUNDED_EXECUTION_MISMATCH', '命令身份或期限与原持久预算不一致', 409)
    if type(now) not in (int, float) or not math.isfinite(now) or now < budget['admitted_at'] - 5:
        raise DevError('BOUNDED_EXECUTION_CLOCK', '执行时钟不能确认原时间预算', 409)
    remaining = min(float(requested_seconds), budget['hard_until'] - now)
    if remaining <= 0:
        raise DevError('BOUNDED_EXECUTION_EXPIRED', '原命令时间预算已用尽，没有开始新的执行', 409)
    return remaining
