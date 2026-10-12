"""Local-only P2 lifetime negotiation, revocation and recovery decision model.

This module neither installs nor enables long-lived subscriptions. The live
Events implementation retains its finite policy. Every returned action flag is
false. A positive simulated eligibility result is not execution authority.

Callers must supply current server-authenticated scope state and an authoritative
repository that resolves each reference to the latest immutable evidence
revision. These Python records are not authentication or signature verifiers;
they must never be constructed from untrusted frontend approval fields. There is
deliberately no frontend serializer, approval boolean, callback, secret, network,
database write or subscription-creation API here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import re
from typing import Protocol
from types import MappingProxyType

FINITE_TTL_MS = 86_400_000
MINIMUM_TTL_MS = 60_000


class State(str, Enum):
    ACTIVE = "active"
    UNKNOWN = "unknown"
    PAUSED = "paused"
    REVOKED = "revoked"


class EvidenceState(str, Enum):
    VERIFIED = "verified"
    UNKNOWN = "unknown"
    REVOKED = "revoked"
    DENIED = "denied"


class EvidenceKind(str, Enum):
    ADAPTER = "adapter_lifetime_validated"
    HOST = "host_lifetime_verified"
    ACCOUNT = "account_lifetime_verified"
    REVOCATION = "same_scope_revocation_validated"
    RECOVERY = "same_scope_recovery_validated"
    OWNER_APPROVAL = "independent_lifetime_approval"


class EvidenceSource(str, Enum):
    SERVER_VALIDATION = "server_validation_record"
    HOST_RECEIPT = "native_host_receipt"
    ACCOUNT_RECEIPT = "provider_account_receipt"
    OWNER_CONFIRMATION = "authenticated_owner_confirmation"


REQUIRED_SOURCES = MappingProxyType({
    EvidenceKind.ADAPTER: EvidenceSource.SERVER_VALIDATION,
    EvidenceKind.HOST: EvidenceSource.HOST_RECEIPT,
    EvidenceKind.ACCOUNT: EvidenceSource.ACCOUNT_RECEIPT,
    EvidenceKind.REVOCATION: EvidenceSource.SERVER_VALIDATION,
    EvidenceKind.RECOVERY: EvidenceSource.SERVER_VALIDATION,
    EvidenceKind.OWNER_APPROVAL: EvidenceSource.OWNER_CONFIRMATION,
})
APPROVAL_PURPOSE = "same_scope_unbounded_subscription_only"


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Expected a bounded server identifier")


def _revision(value, *, unknown=False):
    if unknown and value is None:
        return
    if type(value) is not int or value < 1:
        raise ValueError("Expected a positive integer revision")


def _generation(value, *, unknown=False):
    if unknown and value is None:
        return
    if type(value) is not int or value < 0:
        raise ValueError("Expected a nonnegative credential generation")


def _time(value, *, unknown=False):
    if unknown and value is None:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Expected a finite server timestamp")


@dataclass(frozen=True)
class Scope:
    space_id: str
    owner_id: str
    grant_id: str
    project_id: str
    environment_id: str
    conversation_id: str
    subscription_id: str
    event_name: str
    filter_digest: str
    callback_binding_digest: str

    def __post_init__(self):
        for value in (self.space_id, self.owner_id, self.grant_id, self.project_id,
                      self.environment_id, self.conversation_id, self.subscription_id, self.event_name):
            _identifier(value)
        for value in (self.filter_digest, self.callback_binding_digest):
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("Expected a server-calculated SHA-256 scope digest")

    @property
    def digest(self):
        values = [getattr(self, name) for name in self.__dataclass_fields__]
        return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Current:
    scope: Scope
    revision: int | None
    grant_revision: int | None
    credential_generation: int | None
    grant_state: State
    subscription_state: State
    host_state: State
    grant_expires_at: float | None
    subscription_expires_at: float | None

    def __post_init__(self):
        if not isinstance(self.scope, Scope):
            raise ValueError("Current scope must come from server state")
        for value in (self.revision, self.grant_revision):
            _revision(value, unknown=True)
        _generation(self.credential_generation, unknown=True)
        for value in (self.grant_state, self.subscription_state, self.host_state):
            if not isinstance(value, State):
                raise ValueError("State must be an explicit server-state enum")
        _time(self.grant_expires_at, unknown=True)
        _time(self.subscription_expires_at, unknown=True)


@dataclass(frozen=True)
class EvidenceReference:
    record_id: str
    expected_version: int
    kind: EvidenceKind

    def __post_init__(self):
        _identifier(self.record_id)
        _revision(self.expected_version)
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("Evidence kind must be explicit")


@dataclass(frozen=True)
class Evidence:
    record_id: str
    version: int
    kind: EvidenceKind
    source: EvidenceSource
    state: EvidenceState
    scope_digest: str
    current_revision: int
    grant_revision: int
    credential_generation: int
    observed_at: float
    valid_until: float
    owner_id: str = ""
    purpose: str = ""

    def __post_init__(self):
        _identifier(self.record_id)
        for value in (self.version, self.current_revision, self.grant_revision):
            _revision(value)
        _generation(self.credential_generation)
        if not isinstance(self.kind, EvidenceKind) or not isinstance(self.source, EvidenceSource) or not isinstance(self.state, EvidenceState):
            raise ValueError("Evidence must use trusted typed states and sources")
        if not re.fullmatch(r"[a-f0-9]{64}", self.scope_digest):
            raise ValueError("Evidence must bind a complete scope digest")
        _time(self.observed_at)
        _time(self.valid_until)
        if self.valid_until <= self.observed_at:
            raise ValueError("Evidence needs an explicit bounded validity window")


class EvidenceRepository(Protocol):
    def read_latest(self, record_id: str) -> Evidence | None:
        """Resolve the latest server record; never return a client-supplied copy."""
        ...


def _current_reasons(current: Current, now: float):
    reasons = []
    for name in ("revision", "grant_revision", "credential_generation"):
        if getattr(current, name) is None:
            reasons.append(name + "_unknown")
    for name in ("grant_state", "subscription_state", "host_state"):
        state = getattr(current, name)
        if state is not State.ACTIVE:
            reasons.append(name + "_" + state.value)
    for name in ("grant_expires_at", "subscription_expires_at"):
        expires = getattr(current, name)
        if expires is None:
            reasons.append(name + "_unknown")
        elif expires <= now:
            reasons.append(name + "_expired")
    return reasons


def _evidence_reason(record, reference, current, now):
    if record is None:
        return "missing"
    if not isinstance(record, Evidence):
        return "untrusted_record_shape"
    if record.record_id != reference.record_id or record.kind is not reference.kind:
        return "identity_mismatch"
    if record.version != reference.expected_version:
        return "evidence_revision_changed"
    if record.state is not EvidenceState.VERIFIED:
        return record.state.value
    if record.source is not REQUIRED_SOURCES[reference.kind]:
        return "wrong_evidence_source"
    if record.scope_digest != current.scope.digest:
        return "scope_changed"
    if (record.current_revision != current.revision or record.grant_revision != current.grant_revision
            or record.credential_generation != current.credential_generation):
        return "current_revision_changed"
    if record.observed_at > now or record.valid_until <= now:
        return "evidence_not_current"
    if reference.kind is EvidenceKind.OWNER_APPROVAL and (
        record.owner_id != current.scope.owner_id or record.purpose != APPROVAL_PURPOSE
    ):
        return "independent_owner_approval_mismatch"
    return "verified"


def negotiate(current: Current, references: tuple[EvidenceReference, ...],
              repository: EvidenceRepository, *, now: float, requested_ttl_ms: int | None = FINITE_TTL_MS):
    """Read latest evidence; return a simulation and a finite-only runtime policy."""
    if not isinstance(current, Current) or not isinstance(references, tuple) or any(
        not isinstance(reference, EvidenceReference) for reference in references
    ):
        raise ValueError("Use current server state and evidence references, never frontend approval values")
    _time(now)
    if requested_ttl_ms is not None and type(requested_ttl_ms) is not int:
        raise ValueError("TTL must be a strict integer or an unbounded request")
    reasons = _current_reasons(current, now)
    duplicate = len({ref.kind for ref in references}) != len(references) or len({ref.record_id for ref in references}) != len(references)
    if duplicate:
        reasons.append("duplicate_evidence_reference")
    by_kind = {ref.kind: ref for ref in references}
    checks = {}
    for kind in EvidenceKind:
        reference = by_kind.get(kind)
        if reference is None:
            checks[kind.value] = "missing"
            continue
        try:
            record = repository.read_latest(reference.record_id)
        except LookupError:
            record = None
        except OSError:
            checks[kind.value] = 'repository_unavailable'
            continue
        checks[kind.value] = _evidence_reason(record, reference, current, now)
    complete = not reasons and all(value == "verified" for value in checks.values())
    requested_unbounded = requested_ttl_ms is None
    return {
        "schema_version": 1, "model": "local_simulation_only", "read_only": True,
        "requested_ttl_ms": requested_ttl_ms,
        "effective_ttl_ms": FINITE_TTL_MS if requested_unbounded else min(max(MINIMUM_TTL_MS, requested_ttl_ms), FINITE_TTL_MS),
        "simulated_long_lived_eligible": bool(requested_unbounded and complete),
        "runtime_long_lived_enabled": False,
        "decision": "candidate_requires_runtime_validation" if requested_unbounded and complete else "retain_finite_policy",
        "downgraded": requested_unbounded, "blockers": reasons,
        "evidence_checks": checks, "changes_subscription": False, "changes_grant": False,
        "changes_credentials": False, "changes_work_lease": False,
        "automatic_activation": False, "resume_paused": False,
    }


class ReceiptState(str, Enum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    PERMANENT_REJECTION = "permanent_rejection"


@dataclass(frozen=True)
class RecoveryObservation:
    subscription_id: str
    scope_digest: str
    current_revision: int
    grant_revision: int
    credential_generation: int
    receipt_state: ReceiptState
    checkpoint_digest: str | None
    observed_at: float
    valid_until: float
    http_status: int | None = None

    def __post_init__(self):
        _identifier(self.subscription_id)
        for value in (self.current_revision, self.grant_revision):
            _revision(value)
        _generation(self.credential_generation)
        if not isinstance(self.receipt_state, ReceiptState):
            raise ValueError("Recovery must use the original server receipt state")
        if not isinstance(self.scope_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", self.scope_digest):
            raise ValueError("Recovery must bind the complete scope digest")
        if self.checkpoint_digest is not None and (
            not isinstance(self.checkpoint_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", self.checkpoint_digest)
        ):
            raise ValueError("Recovery uses a digest, never callback or checkpoint secrets")
        _time(self.observed_at)
        _time(self.valid_until)
        if self.valid_until <= self.observed_at:
            raise ValueError("Recovery observation must have a bounded lifetime")
        if self.http_status is not None and (type(self.http_status) is not int or not 100 <= self.http_status <= 599):
            raise ValueError("Invalid HTTP status")


def recovery_decision(current: Current, observation: RecoveryObservation | None, *, now: float):
    """Inspect the original subscription only; never replay, renew or create it."""
    if not isinstance(current, Current) or (observation is not None and not isinstance(observation, RecoveryObservation)):
        raise ValueError("Recovery requires typed original server evidence")
    _time(now)
    blockers = _current_reasons(current, now)
    if current.grant_state is State.REVOKED or current.subscription_state is State.REVOKED:
        action = "stop_revoked"
    elif current.grant_state is State.PAUSED or current.subscription_state is State.PAUSED:
        action = "keep_paused"
    elif observation is None:
        action = "inspect_original_receipt"
    elif (observation.subscription_id != current.scope.subscription_id or observation.scope_digest != current.scope.digest
          or observation.current_revision != current.revision or observation.grant_revision != current.grant_revision
          or observation.credential_generation != current.credential_generation):
        action = "review_changed_scope"
    elif observation.observed_at > now or observation.valid_until <= now:
        action = "inspect_original_receipt"
    elif observation.receipt_state is ReceiptState.PERMANENT_REJECTION or observation.http_status in (410, 413):
        action = "stop_permanent_rejection"
    elif observation.http_status in (401, 403):
        action = "inspect_current_authority"
    elif observation.http_status is not None and not 200 <= observation.http_status <= 299:
        action = "inspect_original_receipt"
    elif observation.receipt_state is ReceiptState.UNKNOWN or observation.checkpoint_digest is None:
        action = "inspect_original_receipt"
    elif blockers == ["subscription_expires_at_expired"]:
        action = "request_original_scope_finite_renewal"
    elif blockers:
        action = "inspect_current_authority"
    else:
        action = "retain_original_finite_subscription"
    return {
        "schema_version": 1, "model": "local_simulation_only", "read_only": True,
        "action": action, "blockers": blockers, "uses_original_subscription": True,
        "effective_ttl_ceiling_ms": FINITE_TTL_MS, "runtime_long_lived_enabled": False,
        "creates_subscription": False, "renews_subscription": False, "replays_business_work": False,
        "resumes_paused": False, "changes_grant": False, "changes_work_lease": False,
    }
