"""Bounded retention decisions for recovery ledgers.

Pending/unknown payloads are never deleted automatically. Limits describe this
deployment, not a promise about a provider's server retention or file limits.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RetentionPolicy:
    pending_max_age_seconds: float = 7 * 86400
    confirmed_max_age_seconds: float = 30 * 86400
    max_pending_items: int = 1000
    max_pending_bytes: int = 16 * 1024 * 1024
    max_attempts: int = 6

    def __post_init__(self):
        for value in (self.pending_max_age_seconds, self.confirmed_max_age_seconds):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Retention ages must be positive finite numbers")
        for value in (self.max_pending_items, self.max_pending_bytes, self.max_attempts):
            if type(value) is not int or value <= 0:
                raise ValueError("Retention limits must be positive integers")

    def admission(self, *, pending_items, pending_bytes, incoming_bytes):
        """No eviction to make room: reject admission while preserving old data."""
        if any(type(value) is not int or value < 0
               for value in (pending_items, pending_bytes, incoming_bytes)):
            raise ValueError("Sizes must be nonnegative integers")
        if pending_items >= self.max_pending_items:
            return "pending_item_limit"
        if pending_bytes + incoming_bytes > self.max_pending_bytes:
            return "pending_byte_limit"
        return None

    def decision(self, record, now):
        """Return a review plan, never authorization to delete or transmit."""
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("A finite observation time is required")
        state = record["state"]
        confirmed = state in {"committed", "api_accepted"}
        anchor = record.get("confirmed_at") if confirmed else record.get("created")
        age = max(0, now - anchor) if isinstance(anchor, (int, float)) and math.isfinite(anchor) else None
        reason = None
        if confirmed and age is not None and age >= self.confirmed_max_age_seconds:
            reason = "confirmed_retention_review"
        elif not confirmed and age is not None and age >= self.pending_max_age_seconds:
            reason = "pending_retention_limit"
        elif not confirmed and record.get("attempt_count", 0) >= self.max_attempts:
            reason = "retry_budget_exhausted"
        return {
            "record_id": record["id"], "reason": reason,
            "needs_attention": bool(reason),
            "preserve_payload": True,
            "automatic_delete_allowed": False,
            "cleanup_requires_explicit_action": True,
            "delivery_uncertain": state in {"sending", "checking", "unknown"},
        }


def retry_delay(attempt, *, retry_after=0, jitter=0):
    """Persist the returned next-attempt time; restart must not reset budgets."""
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Attempt must be a positive integer")
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in (retry_after, jitter)):
        raise ValueError("Retry delay must be finite")
    delays = (5, 15, 30, 60, 120, 300)
    return max(delays[min(attempt - 1, len(delays) - 1)] * (1 + min(1, max(0, jitter))),
               max(0, retry_after))
