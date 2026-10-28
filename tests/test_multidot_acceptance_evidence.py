"""Adversarial checks for offline evidence review; no host/credentials/network."""
from copy import deepcopy
import hashlib

import pytest

from scripts.verify_multidot_host import SCENARIOS, verify

COMMIT = "a" * 40
SOURCE = "b" * 64


@pytest.fixture
def evidence(tmp_path):
    payload = b"sanitized fixture observation, not actual host evidence"
    (tmp_path / "receipt.txt").write_bytes(payload)
    proof = {"id": "proof", "path": "receipt.txt", "sha256": hashlib.sha256(payload).hexdigest()}
    manifest = {
        "schema_version": 1, "source_commit": COMMIT, "source_sha256": SOURCE,
        "versions": {"hub": "fixture", "agent": "fixture", "host": "fixture"},
        "evidence": [proof],
        "participants": [
            {"id": "dot_a", "identity_mode": "independent_grant", "evidence_ref": "proof"},
            {"id": "dot_b", "identity_mode": "delivery_ticket", "evidence_ref": "proof"},
        ],
        "scenarios": [
            {"id": name, "consumer_kind": "real_dot", "status": "passed",
             "source_commit": COMMIT, "source_sha256": SOURCE,
             "participants": ["dot_a", "dot_b"] if name == "team" else ["dot_a"],
             "started_at": "2026-10-28T10:00:00+08:00", "finished_at": "2026-10-28T10:01:00+08:00",
             "checks": {check: {"observed": True, "evidence_refs": ["proof"]} for check in checks}}
            for name, checks in SCENARIOS.items()
        ],
    }
    return manifest, tmp_path


def report(evidence):
    manifest, root = evidence
    return verify(manifest, root, expected_commit=COMMIT, expected_source_sha256=SOURCE)


def test_complete_fixture_is_reviewable_but_never_independently_verified(evidence):
    result = report(evidence)
    assert result["ready_for_review"] is True
    assert result["host_independently_verified"] is False
    assert result["grants_authority"] is False
    assert "Offline" in result["meaning"]


@pytest.mark.parametrize("kind", ["mock", "test_consumer", "unknown"])
def test_simulated_consumption_cannot_close_real_host_requirements(evidence, kind):
    evidence[0]["scenarios"][0]["consumer_kind"] = kind
    result = report(evidence)
    assert not result["ready_for_review"]
    assert "REAL_HOST_OBSERVATIONS_INCOMPLETE" in result["errors"]


def test_http_acceptance_does_not_cover_missing_consumer_receipt(evidence):
    evidence[0]["scenarios"][0]["checks"]["consumer_call"] = {"observed": False, "evidence_refs": []}
    result = report(evidence)
    assert not result["evidence_complete"]
    assert "SCENARIO_PROOF_MISSING" in result["errors"]


@pytest.mark.parametrize("field,value", [("source_commit", "c" * 40), ("source_sha256", "d" * 64)])
def test_results_from_different_candidate_cannot_be_combined(evidence, field, value):
    evidence[0]["scenarios"][2][field] = value
    assert "MIXED_CANDIDATE_EVIDENCE" in report(evidence)["errors"]


def test_same_dot_twice_is_not_two_consumers(evidence):
    team = next(row for row in evidence[0]["scenarios"] if row["id"] == "team")
    team["participants"] = ["dot_a", "dot_a"]
    assert "SCENARIO_PARTICIPANTS_INVALID" in report(evidence)["errors"]


def test_missing_digest_or_changed_evidence_is_rejected(evidence):
    (evidence[1] / "receipt.txt").write_text("changed")
    assert "EVIDENCE_DIGEST_MISMATCH" in report(evidence)["errors"]


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "nested/../receipt.txt", "nested\\receipt.txt"])
def test_evidence_cannot_escape_explicit_root(evidence, path):
    evidence[0]["evidence"][0]["path"] = path
    result = report(evidence)
    assert not result["ready_for_review"]
    assert "EVIDENCE_PATH_INVALID" in result["errors"]


def test_symlink_evidence_does_not_read_target(evidence):
    root = evidence[1]
    (root / "link.txt").symlink_to(root / "receipt.txt")
    evidence[0]["evidence"][0]["path"] = "link.txt"
    assert "EVIDENCE_FILE_UNAVAILABLE" in report(evidence)["errors"]


def test_unbounded_or_incomplete_scenarios_fail_closed(evidence):
    missing = deepcopy(evidence[0])
    missing["scenarios"].pop()
    result = verify(missing, evidence[1], expected_commit=COMMIT, expected_source_sha256=SOURCE)
    assert not result["ready_for_review"]
    assert "SCENARIOS_INCOMPLETE" in result["errors"]


@pytest.mark.parametrize("value", [None, [], {"id": "incomplete"}, True])
def test_malformed_records_do_not_throw_or_become_verified(evidence, value):
    evidence[0]["participants"][0] = value
    result = report(evidence)
    assert not result["ready_for_review"]
    assert not result["host_independently_verified"]


def test_timestamps_require_zone_and_causal_order(evidence):
    evidence[0]["scenarios"][0]["finished_at"] = "2026-10-28T09:00:00+08:00"
    assert "SCENARIO_TIME_INVALID" in report(evidence)["errors"]
    evidence[0]["scenarios"][0]["finished_at"] = "2026-10-28T10:01:00"
    assert "SCENARIO_TIME_INVALID" in report(evidence)["errors"]


@pytest.mark.parametrize("field", ["identity_mode", "evidence_ref"])
def test_unhashable_participant_fields_fail_closed(evidence, field):
    evidence[0]["participants"][0][field] = []
    assert not report(evidence)["ready_for_review"]


def test_empty_proof_file_is_not_a_reviewable_observation(evidence):
    (evidence[1] / "receipt.txt").write_bytes(b"")
    evidence[0]["evidence"][0]["sha256"] = hashlib.sha256(b"").hexdigest()
    assert "EVIDENCE_DIGEST_MISMATCH" in report(evidence)["errors"]
