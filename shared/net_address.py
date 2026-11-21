"""Pure public-address and Retry-After helpers shared by every outbound transport.

No I/O happens here. Callers keep their own DNS, pinning and error contracts;
these functions only make the address and back-off decisions identical.
"""

from __future__ import annotations

import ipaddress
import time
from email.utils import parsedate_to_datetime

DEFAULT_RETRY_AFTER_MAX = 86400


class TransportNotSent(OSError):
    """DNS or TCP/TLS connection failed before any request byte was written.

    It deliberately stays an ``OSError`` (never a ``DevError``) so existing
    network-error handling keeps treating it as transient, while callers that
    must decide about replay can prove nothing reached the remote side.
    """

    request_sent = False


def is_public_unicast_ip(value) -> bool:
    """True only for a global unicast address; IPv4-mapped IPv6 is unwrapped."""
    try:
        text = str(value)
        if "%" in text:  # Zone identifiers name a local interface.
            return False
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        # IPv6's deprecated fec0::/10 site-local range can report is_global.
        and not getattr(address, "is_site_local", False)
    )


def parse_retry_after(value, *, max_seconds=DEFAULT_RETRY_AFTER_MAX, default=None, now=None):
    """Bounded seconds from a Retry-After header (delta-seconds or HTTP-date)."""
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return default
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return min(max_seconds, int(value))
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError, OSError):
        return default
    if parsed is None or parsed.tzinfo is None:
        return default
    try:
        delay = parsed.timestamp() - (time.time() if now is None else now)
    except (OverflowError, OSError, ValueError):
        return default
    return max(0, min(max_seconds, delay))
