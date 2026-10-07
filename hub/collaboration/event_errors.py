"""Events-only protocol errors; never expose callback diagnostic strings."""
import ssl

import httpx
from hub.mcp_request_audit import CALLBACK_REASONS


class CallbackEndpointError(Exception):
    def __init__(self, reason, http_status=None):
        self.reason = reason if isinstance(reason, str) and reason in CALLBACK_REASONS else 'challenge_failed'
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        super().__init__('Callback verification failed')


def callback_reason(exc):
    # HTTP libraries preserve TLS causes; inspect types, never exception text.
    current, seen = exc, set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return 'tls_error'
        current = current.__cause__ or current.__context__
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return 'timeout'
    if isinstance(exc, (ConnectionRefusedError, httpx.ConnectError)):
        return 'connection_refused'
    return 'challenge_failed'


def event_error(exc):
    """Preserve HTTP status/authorization while adapting the Events contract."""
    data = {'code': exc.code}
    if exc.code in {'INVALID_ARGUMENTS', 'INVALID_SIGNING_KEY', 'CALLBACK_URL_REJECTED',
                    'INVALID_CURSOR', 'PROJECT_ID_REQUIRED'}:
        return -32602, data
    if exc.code == 'UNSUPPORTED_DELIVERY_MODE':
        return -32014, {**data, 'feature': 'deliveryMode', 'value': exc.details['value']}
    if exc.status in {401, 403}:
        return -32012, data
    if exc.status == 404:
        if exc.code == 'EVENT_NOT_FOUND':
            data['kind'] = 'event'
        elif exc.code == 'SUBSCRIPTION_NOT_FOUND':
            data['kind'] = 'subscription'
        return -32011, data
    limits = {'SUBSCRIPTION_LIMIT': ('subscriptions', 32),
              'CHALLENGE_RATE_LIMIT': ('callback_challenges_per_minute', 6),
              'ROOM_LIMIT': ('rooms', 64), 'JOIN_ROUTE_LIMIT': ('slot_routes', 8)}
    if exc.code in limits:
        limit, maximum = limits[exc.code]
        return -32013, {**data, 'limit': limit, 'max': maximum}
    return -32000, data
