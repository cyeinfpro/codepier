"""Deterministic helpers; untrusted text is never an executable argument."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pydantic import ValidationError
from shared.util import DevError

TASK_EVENT = 'codepier.collaboration.task_available.v1'
RESULT_EVENT = 'codepier.monitor.result_ready.v1'
INCIDENT_EVENT = 'codepier.monitor.incident_changed.v1'
STATUS_EVENT = 'codepier.monitor.status_changed.v1'
MESSAGE_EVENT = 'codepier.collaboration.message_mentioned.v1'
WORK_EVENT = 'codepier.collaboration.work_available.v1'
EVENTS = (TASK_EVENT, RESULT_EVENT, INCIDENT_EVENT, STATUS_EVENT, MESSAGE_EVENT, WORK_EVENT)
TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'expired', 'dead_letter'})
ACTIVE = ('queued', 'leased', 'running', 'retry_wait', 'blocked')
SEVERITIES = {'low': 0, 'medium': 1, 'high': 2, 'critical': 3}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode()).hexdigest()


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def validate(model, value):
    try:
        return model.model_validate(value).model_dump()
    except ValidationError as exc:
        issues = [{'field': '.'.join(map(str, issue['loc'])), 'message': issue['msg']} for issue in exc.errors()]
        raise DevError('INVALID_ARGUMENTS', '参数不符合协作契约', 422, issues=issues) from None


SENSITIVE_KEY = re.compile(r'(?i)(authorization|cookie|password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|private[_-]?key)')
ASSIGNMENT = re.compile(r'''(?ix)\b(authorization|cookie|password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\b\s*[=:]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''')
BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')
KEY_TOKEN = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|whsec_[A-Za-z0-9+/=]{20,})')
PEM = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', re.S)
URL_CREDENTIAL = re.compile(r'(https?://)[^\s/:@]+:[^\s/@]+@', re.I)


def redact(value):
    """Defense in depth for shared prose, not a license to ingest raw logs.

    The collector records scalar aggregates only; headers, response/request
    bodies, environment dumps and customer records have no ingestion API.
    """
    if isinstance(value, dict):
        return {key: '[REDACTED]' if SENSITIVE_KEY.fullmatch(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = PEM.sub('[REDACTED PRIVATE KEY]', value)
        text = URL_CREDENTIAL.sub(r'\1[REDACTED]@', text)
        text = BEARER.sub('Bearer [REDACTED]', text)
        text = ASSIGNMENT.sub(lambda match: match[1] + '=[REDACTED]', text)
        return KEY_TOKEN.sub('[REDACTED]', text)
    return value


def sign_cursor(secret, binding, position):
    body = canonical({'v': 1, 'binding': binding, 'position': position}).encode()
    tag = hmac.new(secret, body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + tag).decode().rstrip('=')


def read_cursor(secret, binding, value):
    try:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError()
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        body, supplied = raw[:-32], raw[-32:]
        if not hmac.compare_digest(hmac.new(secret, body, hashlib.sha256).digest(), supplied):
            raise ValueError()
        parsed = json.loads(body)
        if set(parsed) != {'v', 'binding', 'position'} or parsed['v'] != 1 or parsed['binding'] != binding:
            raise ValueError()
        return parsed['position']
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise DevError('INVALID_CURSOR', '游标不属于当前授权范围或已经损坏', 400) from None
