"""Small, allowlisted connection-failure metadata for ordinary Agent logs.

WebSocket exception strings can contain the peer's arbitrary close reason.
Never stringify an exception, walk its cause chain, or return a wire reason.
The category describes a recognized signal, not a diagnosis of its cause.
"""

from __future__ import annotations

import re

from websockets.exceptions import ConnectionClosed
from websockets.frames import Close


_KEEPALIVE_REASONS = frozenset(
    {
        "keepalive ping timeout",
        "keepalive timeout",
        "ping timeout",
        "pong timeout",
    }
)
_TIMEOUT_REASONS = frozenset(
    {
        "timeout",
        "timed out",
        "connection timeout",
        "connection timed out",
        "idle timeout",
        "read timeout",
        "write timeout",
        "handshake timeout",
        "opening handshake timeout",
        "closing handshake timeout",
    }
)


def _close_code(frame: Close | None) -> int | None:
    if not isinstance(frame, Close):
        return None
    code = frame.code
    # CloseCode is an IntEnum. Reject bool, strings, and out-of-range values;
    # never coerce arbitrary objects into a value which could reach the log.
    if isinstance(code, int) and not isinstance(code, bool) and 1000 <= code <= 4999:
        return int(code)
    return None


def _reason_category(frame: Close | None) -> str | None:
    if not isinstance(frame, Close) or type(frame.reason) is not str:
        return None
    # A wire close reason fits in 123 bytes. Apply a small character bound even
    # to locally constructed exceptions, then compare only exact known phrases.
    if len(frame.reason) > 123:
        return None
    reason = frame.reason.strip().casefold()
    if reason in _KEEPALIVE_REASONS:
        return "keepalive_timeout"
    if reason in _TIMEOUT_REASONS:
        return "timeout"
    return None


def safe_connection_error(exc: BaseException) -> dict[str, str | int | None]:
    """Return bounded diagnostic fields without exception or frame text.

    ``received_close_code`` and ``sent_close_code`` are the actual optional
    frame fields, not ConnectionClosed.code's synthetic 1006 fallback. Reason
    categories are limited to keepalive_timeout, timeout, no_close_frame,
    closed, and other. Unknown peer text always remains undisclosed.
    """
    exception_type = type(exc).__name__
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", exception_type) is None:
        exception_type = "Exception"
    result: dict[str, str | int | None] = {
        "exception_type": exception_type,
        "received_close_code": None,
        "sent_close_code": None,
        "reason_category": "timeout" if isinstance(exc, TimeoutError) else "other",
    }
    if not isinstance(exc, ConnectionClosed):
        return result
    result["received_close_code"] = _close_code(exc.rcvd)
    result["sent_close_code"] = _close_code(exc.sent)
    categories = {_reason_category(exc.rcvd), _reason_category(exc.sent)}
    if "keepalive_timeout" in categories:
        result["reason_category"] = "keepalive_timeout"
    elif "timeout" in categories:
        result["reason_category"] = "timeout"
    elif exc.rcvd is None and exc.sent is None:
        result["reason_category"] = "no_close_frame"
    else:
        result["reason_category"] = "closed"
    return result
