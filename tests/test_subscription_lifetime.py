"""Deterministic local simulation only; no Hub, host account, network or grants."""
from dataclasses import FrozenInstanceError, replace
from itertools import product

import pytest

from shared.subscription_lifetime import (
    APPROVAL_PURPOSE, FINITE_TTL_MS, REQUIRED_SOURCES, Current, Evidence,
    EvidenceKind, EvidenceReference, EvidenceSource, EvidenceState,
    ReceiptState, RecoveryObservation, Scope, State, negotiate, recovery_decision,
)

NOW = 1000.0


class Repository:
    """Fake server evidence store: always resolves latest, never supplied copies."""
    def __init__(self, records):
        self.records = {record.record_id: record for record in records}
        self.reads = []

    def read_latest(self, record_id):
        self.reads.append(record_id)
        return self.records.get(record_id)


def setup():
    scope = Scope("space", "owner", "grant", "project", "production", "conversation",
                  "subscription", "message", "a" * 64, "b" * 64)
    current = Current(scope, 7, 3, 2, State.ACTIVE, State.ACTIVE, State.ACTIVE, 10_000, 2_000)
    records = [
        Evidence("evidence-" + kind.value, 1, kind, REQUIRED_SOURCES[kind],
                 EvidenceState.VERIFIED, scope.digest, 7, 3, 2, 900, 3000,
                 scope.owner_id if kind is EvidenceKind.OWNER_APPROVAL else "",
                 APPROVAL_PURPOSE if kind is EvidenceKind.OWNER_APPROVAL else "")
        for kind in EvidenceKind
    ]
    refs = tuple(EvidenceReference(record.record_id, record.version, record.kind) for record in records)
    return current, refs, Repository(records)


def propose(current, refs, store):
    return negotiate(current, refs, store, now=NOW, requested_ttl_ms=None)


def assert_no_actions(result):
    for name in ("runtime_long_lived_enabled", "changes_subscription", "changes_grant",
                 "changes_credentials", "changes_work_lease", "automatic_activation",
                 "resume_paused", "resumes_paused", "creates_subscription",
                 "renews_subscription", "replays_business_work"):
        if name in result:
            assert result[name] is False, name
    assert result["read_only"] is True
    assert result["model"] == "local_simulation_only"


def observation(current, **changes):
    value = RecoveryObservation(
        current.scope.subscription_id, current.scope.digest, current.revision,
        current.grant_revision, current.credential_generation, ReceiptState.CONFIRMED,
        "c" * 64, 900, 3000, 204)
    return replace(value, **changes)


def test_complete_server_evidence_only_produces_simulated_candidate_not_live_enable():
    current, refs, store = setup()
    result = propose(current, refs, store)
    assert result["simulated_long_lived_eligible"] is True
    assert result["decision"] == "candidate_requires_runtime_validation"
    assert result["effective_ttl_ms"] == FINITE_TTL_MS
    assert result["downgraded"] is True
    assert all(value == "verified" for value in result["evidence_checks"].values())
    assert store.reads == [ref.record_id for ref in refs]
    assert_no_actions(result)


def test_default_request_retains_finite_policy_even_with_complete_evidence():
    current, refs, store = setup()
    result = negotiate(current, refs, store, now=NOW)
    assert result["effective_ttl_ms"] == FINITE_TTL_MS
    assert not result["simulated_long_lived_eligible"] and not result["downgraded"]
    assert_no_actions(result)


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_every_independent_evidence_kind_is_required(kind):
    current, refs, store = setup()
    filtered = tuple(ref for ref in refs if ref.kind is not kind)
    result = propose(current, filtered, store)
    assert not result["simulated_long_lived_eligible"]
    assert result["evidence_checks"][kind.value] == "missing"
    assert_no_actions(result)


@pytest.mark.parametrize("kind,state", product(EvidenceKind, [EvidenceState.UNKNOWN, EvidenceState.REVOKED, EvidenceState.DENIED]))
def test_unknown_denied_and_revoked_evidence_never_enables(kind, state):
    current, refs, store = setup()
    ref = next(ref for ref in refs if ref.kind is kind)
    store.records[ref.record_id] = replace(store.records[ref.record_id], state=state)
    result = propose(current, refs, store)
    assert result["evidence_checks"][kind.value] == state.value
    assert not result["simulated_long_lived_eligible"]
    assert_no_actions(result)


@pytest.mark.parametrize("field,value", [
    ("space_id", "another"), ("owner_id", "another"), ("grant_id", "another"),
    ("project_id", "another"), ("environment_id", "another"), ("conversation_id", "another"),
    ("subscription_id", "another"), ("event_name", "another"), ("filter_digest", "d" * 64),
    ("callback_binding_digest", "d" * 64),
])
def test_each_scope_dimension_is_bound_exactly(field, value):
    current, refs, store = setup()
    changed = replace(current, scope=replace(current.scope, **{field: value}))
    assert changed.scope.digest != current.scope.digest
    result = propose(changed, refs, store)
    assert set(result["evidence_checks"].values()) == {"scope_changed"}
    assert not result["simulated_long_lived_eligible"]


@pytest.mark.parametrize("field", ["revision", "grant_revision", "credential_generation"])
@pytest.mark.parametrize("value", [None, 99])
def test_unknown_or_changed_current_revisions_keep_lifetime_disabled(field, value):
    current, refs, store = setup()
    result = propose(replace(current, **{field: value}), refs, store)
    assert not result["simulated_long_lived_eligible"]
    assert all(value == "current_revision_changed" for value in result["evidence_checks"].values())
    if value is None:
        assert field + "_unknown" in result["blockers"]


@pytest.mark.parametrize("field,state", product(
    ["grant_state", "subscription_state", "host_state"],
    [State.UNKNOWN, State.PAUSED, State.REVOKED],
))
def test_current_pause_revocation_or_unknown_cannot_be_overridden_by_positive_evidence(field, state):
    current, refs, store = setup()
    result = propose(replace(current, **{field: state}), refs, store)
    assert not result["simulated_long_lived_eligible"]
    assert field + "_" + state.value in result["blockers"]
    assert_no_actions(result)


@pytest.mark.parametrize("field,value", product(["grant_expires_at", "subscription_expires_at"], [None, NOW - 1, NOW]))
def test_current_expiry_or_missing_expiry_is_not_lifetime_approval(field, value):
    current, refs, store = setup()
    result = propose(replace(current, **{field: value}), refs, store)
    assert not result["simulated_long_lived_eligible"]
    assert_no_actions(result)


@pytest.mark.parametrize("kind", list(EvidenceKind))
@pytest.mark.parametrize("changes", [{"observed_at": NOW + 1}, {"valid_until": NOW}])
def test_expired_or_future_evidence_is_not_reusable(kind, changes):
    current, refs, store = setup()
    ref = next(ref for ref in refs if ref.kind is kind)
    store.records[ref.record_id] = replace(store.records[ref.record_id], **changes)
    result = propose(current, refs, store)
    assert result["evidence_checks"][kind.value] == "evidence_not_current"
    assert not result["simulated_long_lived_eligible"]


def test_latest_owner_revocation_rejects_cached_version_and_revoked_new_version():
    current, refs, store = setup()
    assert propose(current, refs, store)["simulated_long_lived_eligible"]
    approval = next(ref for ref in refs if ref.kind is EvidenceKind.OWNER_APPROVAL)
    store.records[approval.record_id] = replace(store.records[approval.record_id], version=2, state=EvidenceState.REVOKED)
    stale = propose(current, refs, store)
    assert stale["evidence_checks"][approval.kind.value] == "evidence_revision_changed"
    updated_refs = tuple(replace(ref, expected_version=2) if ref is approval else ref for ref in refs)
    revoked = propose(current, updated_refs, store)
    assert revoked["evidence_checks"][approval.kind.value] == "revoked"
    assert not stale["simulated_long_lived_eligible"] and not revoked["simulated_long_lived_eligible"]
    assert_no_actions(revoked)


@pytest.mark.parametrize("field,value,reason", [
    ("owner_id", "another-owner", "independent_owner_approval_mismatch"),
    ("purpose", "ongoing_channel_access", "independent_owner_approval_mismatch"),
    ("purpose", "work_lease_extension", "independent_owner_approval_mismatch"),
    ("purpose", "credential_storage", "independent_owner_approval_mismatch"),
    ("source", EvidenceSource.HOST_RECEIPT, "wrong_evidence_source"),
])
def test_independent_lifetime_approval_cannot_be_substituted_by_other_consent(field, value, reason):
    current, refs, store = setup()
    ref = next(ref for ref in refs if ref.kind is EvidenceKind.OWNER_APPROVAL)
    store.records[ref.record_id] = replace(store.records[ref.record_id], **{field: value})
    result = propose(current, refs, store)
    assert not result["simulated_long_lived_eligible"]
    assert result["evidence_checks"][ref.kind.value] == reason


@pytest.mark.parametrize("value", [True, False, {"approved": True}, "yes"])
def test_frontend_approval_values_are_not_server_evidence(value):
    current, refs, store = setup()
    with pytest.raises(ValueError):
        negotiate(current, (value,), store, now=NOW, requested_ttl_ms=None)
    ref = next(ref for ref in refs if ref.kind is EvidenceKind.OWNER_APPROVAL)
    store.records[ref.record_id] = value
    result = propose(current, refs, store)
    assert result["evidence_checks"][ref.kind.value] == "untrusted_record_shape"
    assert not result["simulated_long_lived_eligible"]


def test_approval_bool_parameter_is_not_part_of_contract():
    current, refs, store = setup()
    with pytest.raises(TypeError):
        negotiate(current, refs, store, now=NOW, approved=True)


def test_duplicate_or_retyped_reference_cannot_supply_multiple_proofs():
    current, refs, store = setup()
    result = propose(current, refs + (refs[0],), store)
    assert "duplicate_evidence_reference" in result["blockers"]
    assert not result["simulated_long_lived_eligible"]
    altered = tuple(replace(ref, record_id=refs[0].record_id) if ref.kind is EvidenceKind.OWNER_APPROVAL else ref for ref in refs)
    assert not propose(current, altered, store)["simulated_long_lived_eligible"]


def test_missing_or_wrong_latest_record_fails_closed():
    current, refs, store = setup()
    first = refs[0]
    del store.records[first.record_id]
    assert propose(current, refs, store)["evidence_checks"][first.kind.value] == "missing"
    store.records[first.record_id] = replace(store.records[refs[1].record_id], record_id=first.record_id)
    assert propose(current, refs, store)["evidence_checks"][first.kind.value] == "identity_mismatch"


@pytest.mark.parametrize("requested,expected", [
    (-1, 60_000), (0, 60_000), (1, 60_000), (60_001, 60_001),
    (FINITE_TTL_MS, FINITE_TTL_MS), (10 ** 30, FINITE_TTL_MS), (None, FINITE_TTL_MS),
])
def test_finite_runtime_ttl_is_always_retained(requested, expected):
    current, refs, store = setup()
    result = negotiate(current, refs, store, now=NOW, requested_ttl_ms=requested)
    assert result["effective_ttl_ms"] == expected
    assert_no_actions(result)


@pytest.mark.parametrize("invalid", [True, False, "60000", 1.5])
def test_ttl_does_not_coerce_frontend_values(invalid):
    current, refs, store = setup()
    with pytest.raises(ValueError):
        negotiate(current, refs, store, now=NOW, requested_ttl_ms=invalid)


@pytest.mark.parametrize("invalid", [True, float("inf"), float("nan"), -1])
def test_timestamps_are_finite_and_never_boolean(invalid):
    current, refs, store = setup()
    with pytest.raises(ValueError):
        negotiate(current, refs, store, now=invalid)


def test_model_records_are_immutable_and_repository_is_read_only():
    current, refs, store = setup()
    before = dict(store.records)
    with pytest.raises(FrozenInstanceError):
        current.revision = 8
    propose(current, refs, store)
    assert store.records == before


def test_recovery_requires_original_confirmed_checkpoint_and_keeps_finite_scope():
    current, _, _ = setup()
    result = recovery_decision(current, observation(current), now=NOW)
    assert result["action"] == "retain_original_finite_subscription"
    assert result["effective_ttl_ceiling_ms"] == FINITE_TTL_MS
    assert_no_actions(result)


@pytest.mark.parametrize("changes,action", [
    ({"receipt_state": ReceiptState.UNKNOWN}, "inspect_original_receipt"),
    ({"checkpoint_digest": None}, "inspect_original_receipt"),
    ({"subscription_id": "other"}, "review_changed_scope"),
    ({"scope_digest": "f" * 64}, "review_changed_scope"),
    ({"current_revision": 8}, "review_changed_scope"),
    ({"grant_revision": 4}, "review_changed_scope"),
    ({"credential_generation": 3}, "review_changed_scope"),
    ({"observed_at": NOW + 1}, "inspect_original_receipt"),
    ({"valid_until": NOW}, "inspect_original_receipt"),
    ({"http_status": 410}, "stop_permanent_rejection"),
    ({"http_status": 413}, "stop_permanent_rejection"),
    ({"receipt_state": ReceiptState.PERMANENT_REJECTION}, "stop_permanent_rejection"),
])
def test_recovery_mismatch_unknown_expiry_and_permanent_rejection_do_not_retry(changes, action):
    current, _, _ = setup()
    result = recovery_decision(current, observation(current, **changes), now=NOW)
    assert result["action"] == action
    assert_no_actions(result)


@pytest.mark.parametrize("field,state,action", [
    ("grant_state", State.REVOKED, "stop_revoked"),
    ("subscription_state", State.REVOKED, "stop_revoked"),
    ("grant_state", State.PAUSED, "keep_paused"),
    ("subscription_state", State.PAUSED, "keep_paused"),
    ("host_state", State.UNKNOWN, "inspect_current_authority"),
])
def test_recovery_never_reactivates_paused_or_revoked_authority(field, state, action):
    current, _, _ = setup()
    result = recovery_decision(replace(current, **{field: state}), observation(current), now=NOW)
    assert result["action"] == action
    assert_no_actions(result)


def test_finite_expiry_only_proposes_original_scope_renewal_without_acting():
    current, _, _ = setup()
    expired = replace(current, subscription_expires_at=NOW)
    result = recovery_decision(expired, observation(current), now=NOW)
    assert result["action"] == "request_original_scope_finite_renewal"
    assert_no_actions(result)
    revoked = replace(expired, grant_state=State.REVOKED)
    assert recovery_decision(revoked, observation(current), now=NOW)["action"] == "stop_revoked"


def test_missing_recovery_receipt_is_not_success_or_permission_to_replay():
    current, _, _ = setup()
    result = recovery_decision(current, None, now=NOW)
    assert result["action"] == "inspect_original_receipt"
    assert_no_actions(result)


@pytest.mark.parametrize("changes", [{"grant_state": True}, {"revision": True}, {"credential_generation": True}])
def test_server_state_cannot_be_frontend_truthy_values(changes):
    current, _, _ = setup()
    with pytest.raises(ValueError):
        replace(current, **changes)


def test_zero_generation_is_valid_but_still_exactly_bound():
    current, refs, store = setup()
    current = replace(current, credential_generation=0)
    store.records = {key: replace(value, credential_generation=0) for key, value in store.records.items()}
    assert propose(current, refs, store)["simulated_long_lived_eligible"]
    old = replace(current, credential_generation=1)
    assert not propose(old, refs, store)["simulated_long_lived_eligible"]


@pytest.mark.parametrize("status,action", [(401, "inspect_current_authority"), (403, "inspect_current_authority"),
                                         (429, "inspect_original_receipt"), (503, "inspect_original_receipt")])
def test_rejected_http_observation_cannot_be_claimed_confirmed_success(status, action):
    current, _, _ = setup()
    result = recovery_decision(current, observation(current, http_status=status), now=NOW)
    assert result["action"] == action
    assert_no_actions(result)


def test_temporarily_unavailable_evidence_repository_stays_disabled():
    current, refs, store = setup()
    def unavailable(_):
        raise OSError("synthetic inaccessible server evidence")
    store.read_latest = unavailable
    result = propose(current, refs, store)
    assert set(result["evidence_checks"].values()) == {"repository_unavailable"}
    assert not result["simulated_long_lived_eligible"]
    assert_no_actions(result)


def test_missing_recovery_scope_digest_is_not_a_checkpoint():
    current, _, _ = setup()
    with pytest.raises(ValueError):
        observation(current, scope_digest=None)


def test_evidence_source_registry_cannot_be_reconfigured_at_runtime():
    with pytest.raises(TypeError):
        REQUIRED_SOURCES[EvidenceKind.OWNER_APPROVAL] = EvidenceSource.HOST_RECEIPT
