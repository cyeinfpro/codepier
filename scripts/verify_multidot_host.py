"""Validate sanitized host acceptance evidence without claiming to verify a live host.

This is an offline review aid, not a deployment gate or a source of execution
authority. The operator supplies the expected candidate identity independently.
No credentials, callbacks, model calls or network access are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

SCENARIOS = {
    "duplex": ("native_subscription", "callback_verified", "consumer_call", "original_thread_reply"),
    "authorized_work": ("owner_scope_matched", "operation_receipt", "original_operation_reused", "original_thread_result"),
    "team": ("two_distinct_consumers", "same_goal", "handoff_receipt", "artifact_version_verified", "aggregate_publication"),
    "recovery": ("interruption_observed", "original_operation_reused", "no_duplicate_effect", "no_duplicate_publication"),
    "subscription": ("native_refresh_observed", "expiry_stops_delivery", "revocation_blocks_admission", "original_inbox_restored"),
}
ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
COMMIT = re.compile(r"^[a-f0-9]{40}$")
MAX_MANIFEST_BYTES = 262144
MAX_EVIDENCE_BYTES = 2097152
MAX_TOTAL_BYTES = 16777216


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("timezone required")
    return result


def verify(manifest, evidence_root, *, expected_commit, expected_source_sha256):
    """Check completeness and file digests; never attest that observations are true."""
    errors = []

    def fail(code):
        if code not in errors:
            errors.append(code)

    result = {
        "schema_version": 1,
        "evidence_complete": False,
        "ready_for_review": False,
        "host_independently_verified": False,
        "grants_authority": False,
        "errors": errors,
        "meaning": "Offline completeness and digest checks only; review original host evidence separately.",
    }
    if not isinstance(manifest, dict):
        fail("MANIFEST_OBJECT_REQUIRED")
        return result
    allowed = {"schema_version", "source_commit", "source_sha256", "versions", "evidence", "participants", "scenarios"}
    if set(manifest) != allowed or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        fail("MANIFEST_SCHEMA_INVALID")
    if (not isinstance(expected_commit, str) or not COMMIT.fullmatch(expected_commit)
            or manifest.get("source_commit") != expected_commit):
        fail("CANDIDATE_COMMIT_MISMATCH")
    if (not isinstance(expected_source_sha256, str) or not SHA.fullmatch(expected_source_sha256)
            or manifest.get("source_sha256") != expected_source_sha256):
        fail("CANDIDATE_SOURCE_MISMATCH")
    versions = manifest.get("versions")
    if (not isinstance(versions, dict) or set(versions) != {"hub", "agent", "host"}
            or any(not isinstance(v, str) or not v.strip() or len(v) > 120 for v in versions.values())):
        fail("VERSIONS_REQUIRED")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 64:
        fail("EVIDENCE_LIST_REQUIRED")
        evidence = []
    refs, total = set(), 0
    root = Path(evidence_root).resolve()
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"id", "path", "sha256"}:
            fail("EVIDENCE_SCHEMA_INVALID")
            continue
        identifier, name, expected = item["id"], item["path"], item["sha256"]
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in refs:
            fail("EVIDENCE_ID_INVALID")
            continue
        refs.add(identifier)
        if not isinstance(name, str) or not name or len(name) > 240 or "\\" in name:
            fail("EVIDENCE_PATH_INVALID")
            continue
        relative = Path(name)
        if relative.is_absolute() or any(part in {"..", "."} for part in name.split("/")):
            fail("EVIDENCE_PATH_INVALID")
            continue
        if not isinstance(expected, str) or not SHA.fullmatch(expected):
            fail("EVIDENCE_DIGEST_INVALID")
            continue
        file = root / relative
        try:
            current = root
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise ValueError("symlink")
            if not file.resolve().is_relative_to(root) or not file.is_file():
                raise ValueError("file")
            size = file.stat().st_size
            total += size
            if size > MAX_EVIDENCE_BYTES or total > MAX_TOTAL_BYTES:
                fail("EVIDENCE_BYTE_BUDGET")
                continue
            with file.open("rb") as stream:
                data = stream.read(MAX_EVIDENCE_BYTES + 1)
            if not data or len(data) != size or hashlib.sha256(data).hexdigest() != expected:
                fail("EVIDENCE_DIGEST_MISMATCH")
        except (OSError, ValueError):
            fail("EVIDENCE_FILE_UNAVAILABLE")
    participants = manifest.get("participants")
    member_ids = set()
    if not isinstance(participants, list) or not 2 <= len(participants) <= 16:
        fail("TWO_CONSUMERS_REQUIRED")
        participants = []
    for participant in participants:
        if not isinstance(participant, dict) or set(participant) != {"id", "identity_mode", "evidence_ref"}:
            fail("PARTICIPANT_SCHEMA_INVALID")
            continue
        identifier = participant["id"]
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in member_ids:
            fail("PARTICIPANT_ID_INVALID")
            continue
        member_ids.add(identifier)
        if (not isinstance(participant["identity_mode"], str)
                or participant["identity_mode"] not in {"independent_grant", "delivery_ticket"}):
            fail("PARTICIPANT_IDENTITY_UNVERIFIED")
        if not isinstance(participant["evidence_ref"], str) or participant["evidence_ref"] not in refs:
            fail("PARTICIPANT_EVIDENCE_MISSING")
    scenarios = manifest.get("scenarios")
    observed = set()
    if not isinstance(scenarios, list) or len(scenarios) != len(SCENARIOS):
        fail("SCENARIOS_INCOMPLETE")
        scenarios = scenarios if isinstance(scenarios, list) else []
    for scenario in scenarios:
        fields = {"id", "consumer_kind", "status", "source_commit", "source_sha256", "participants",
                  "started_at", "finished_at", "checks"}
        if not isinstance(scenario, dict) or set(scenario) != fields:
            fail("SCENARIO_SCHEMA_INVALID")
            continue
        identifier = scenario["id"]
        if not isinstance(identifier, str) or identifier not in SCENARIOS or identifier in observed:
            fail("SCENARIO_ID_INVALID")
            continue
        observed.add(identifier)
        if scenario["consumer_kind"] != "real_dot" or scenario["status"] != "passed":
            fail("REAL_HOST_OBSERVATIONS_INCOMPLETE")
        if scenario["source_commit"] != expected_commit or scenario["source_sha256"] != expected_source_sha256:
            fail("MIXED_CANDIDATE_EVIDENCE")
        try:
            if timestamp(scenario["finished_at"]) < timestamp(scenario["started_at"]):
                raise ValueError("time order")
        except (TypeError, ValueError):
            fail("SCENARIO_TIME_INVALID")
        members = scenario["participants"]
        if (not isinstance(members, list) or not members or any(not isinstance(p, str) for p in members)
                or len(set(members)) != len(members) or not set(members) <= member_ids
                or (identifier == "team" and len(members) < 2)):
            fail("SCENARIO_PARTICIPANTS_INVALID")
        checks = scenario["checks"]
        if not isinstance(checks, dict) or set(checks) != set(SCENARIOS[identifier]):
            fail("SCENARIO_CHECKS_INCOMPLETE")
            continue
        for check in checks.values():
            if (not isinstance(check, dict) or set(check) != {"observed", "evidence_refs"}
                    or check.get("observed") is not True
                    or not isinstance(check.get("evidence_refs"), list) or not check["evidence_refs"]
                    or any(not isinstance(ref, str) or ref not in refs for ref in check["evidence_refs"])):
                fail("SCENARIO_PROOF_MISSING")
    if observed != set(SCENARIOS):
        fail("SCENARIOS_INCOMPLETE")
    result["evidence_complete"] = result["ready_for_review"] = not errors
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("manifest byte budget")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = verify(manifest, args.evidence_root, expected_commit=args.expected_commit,
                        expected_source_sha256=args.expected_source_sha256)
    except (OSError, ValueError, TypeError):
        result = {"schema_version": 1, "evidence_complete": False, "ready_for_review": False,
                  "host_independently_verified": False, "grants_authority": False,
                  "errors": ["MANIFEST_UNREADABLE"]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ready_for_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
