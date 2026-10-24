"""Read-only, fail-closed retention planning over a trusted host inventory.

No discovery, Docker commands, deletion, scheduler or deployment side effects.
The caller must collect a complete, same-deployment snapshot (including stopped
containers) and supply JSON with:
 deployment: stable deployment ID; current_version: verified running version;
 complete: true only after all collectors succeeded;
 current_mount_refs, env_refs, current_json_refs, rollback_refs, pins: resource IDs;
 deploy_lock: false only when observed absent; containers: [{id, refs, state}];
 jobs: native apply/check records plus deployment and refs (resource IDs);
 objects: [{kind, name, version, allocated_bytes, labels, refs}].
Resource IDs are "kind:name". Kinds are volume/image/source/backup. Labels must
include com.codepier.deployment and com.codepier.role (state/code/backup/assets).
The collector must resolve aliases to exact IDs, include every reference, and
mark incomplete/ambiguous metadata rather than infer ownership from names.
Job refs supplement native old/new_volume, old_image, image, source and backup.
Old unlabelled objects are intentionally manual-only. Job/key records are never
candidates. A plan is not permission to delete: any future executor needs separate
approval and fresh state-hash comparison under the deployment lock.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
KINDS = {"volume", "image", "source", "backup"}
ROLES = {"state", "code", "backup", "assets"}
TERMINAL = {"succeeded", "failed", "rolled_back", "cancelled"}
REF_FIELDS = ("current_mount_refs", "env_refs", "current_json_refs", "rollback_refs", "pins")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class RetentionManifest:
    """Immutable bytes-equivalent manifest; decoded views cannot mutate approval."""
    canonical_json: str
    sha256: str

    def as_dict(self):
        return json.loads(self.canonical_json)


def _refs(value):
    if not isinstance(value, list) or any(not isinstance(v, str) or ":" not in v for v in value):
        raise ValueError("references must be lists of exact kind:name identifiers")
    return value


def _job_refs(job):
    refs = list(_refs(job.get("refs", [])))
    for field, kind in (("old_volume", "volume"), ("new_volume", "volume"),
                        ("old_image", "image"), ("image", "image"), ("source", "source")):
        if job.get(field):
            refs.append(kind + ":" + str(job[field]))
    backup = job.get("backup", {})
    if isinstance(backup, dict) and backup.get("volume"):
        refs.append("volume:" + str(backup["volume"]))
    return refs


def plan_retention(inventory):
    """Keep current + two distinct successful historical versions; never delete.

    Historical ordering uses successful apply completion time, not semver or job
    count. Failed candidates form their own review group, never consume slots.
    Every object of a retained version is kept (no duplicate-volume guessing).
    """
    # Take a detached JSON snapshot; reject non-JSON/NaN metadata before planning.
    data = json.loads(canonical(inventory))
    if not isinstance(data, dict):
        raise ValueError("inventory must be an object")
    objects, jobs, containers = (data.get(k, []) for k in ("objects", "jobs", "containers"))
    if any(not isinstance(v, list) for v in (objects, jobs, containers)):
        raise ValueError("objects, jobs and containers must be lists")
    if any(not isinstance(v, dict) for v in objects + jobs + containers):
        raise ValueError("inventory entries must be objects")
    deployment = data.get("deployment")
    current = data.get("current_version")
    global_reasons = []
    if (data.get("complete") is not True or not isinstance(deployment, str) or not deployment
            or not isinstance(current, str) or not VERSION.fullmatch(current)):
        global_reasons.append("incomplete_inventory")
    if data.get("deploy_lock") is not False:
        global_reasons.append("deployment_lock_or_unknown")
    for field in ("objects", "jobs", "containers", *REF_FIELDS):
        if field not in data:
            global_reasons.append("missing_" + field)
    protected = {}
    def pin(ref, reason):
        protected.setdefault(ref, set()).add(reason)
    for field in REF_FIELDS:
        for ref in _refs(data.get(field, [])):
            pin(ref, field)
    if any(not data.get(field) for field in REF_FIELDS[:3]):
        global_reasons.append("missing_current_reference")
    for container in containers:
        if not container.get("id") or "refs" not in container or not container.get("state"):
            global_reasons.append("incomplete_container_metadata")
        for ref in _refs(container.get("refs", [])):
            pin(ref, "container:" + str(container.get("id", "unknown")))

    historical = {}
    failed_refs = set()
    for job in jobs:
        refs = _job_refs(job)
        if not job.get("id") or not job.get("kind") or not job.get("state") or not job.get("deployment"):
            global_reasons.append("incomplete_job_metadata")
        if job.get("deployment") != deployment:
            # Cross-deployment records cannot establish successful history.
            for ref in refs:
                pin(ref, "foreign_or_unknown_job_deployment")
            continue
        state = job.get("state")
        if state not in TERMINAL:
            for ref in refs:
                pin(ref, "job:" + str(job.get("id", "unknown")) + ":" + str(state))
            if not refs:
                global_reasons.append("unresolved_active_job")
        if job.get("kind") != "apply":
            continue
        if state in {"failed", "rolled_back", "cancelled"}:
            candidate_refs = {
                kind + ":" + str(job[field])
                for field, kind in (("new_volume", "volume"), ("image", "image"), ("source", "source"))
                if job.get(field)
            }
            failed_refs.update(candidate_refs)
            # Candidate provenance is job-local: another job may need that same
            # object as its rollback state. Explicit old/backup roles always win,
            # even if malformed metadata aliases old and new within this job.
            rollback_refs = {
                kind + ":" + str(job[field])
                for field, kind in (("old_volume", "volume"), ("old_image", "image"))
                if job.get(field)
            }
            backup = job.get("backup", {})
            if isinstance(backup, dict) and backup.get("volume"):
                rollback_refs.add("volume:" + str(backup["volume"]))
            for ref in (set(refs) - candidate_refs) | rollback_refs:
                pin(ref, "failed_job_rollback_reference")
        if state == "succeeded":
            timestamp = job.get("updated")
            if (job.get("commit_decided") is not True or not isinstance(timestamp, (int, float))
                    or isinstance(timestamp, bool) or not math.isfinite(timestamp)):
                global_reasons.append("ambiguous_successful_job")
                continue
            for version in (job.get("from_version"), job.get("target_version")):
                if not isinstance(version, str) or not VERSION.fullmatch(version):
                    global_reasons.append("unknown_successful_version")
                elif version != current:
                    historical[version] = max(historical.get(version, -1), timestamp)
    # A successful transition's target is newer than its from_version on ties.
    retained = sorted(historical, key=lambda v: (historical[v], tuple(map(int, v.split(".")))), reverse=True)[:2]
    ids = [str(o.get("kind", "")) + ":" + str(o.get("name", "")) for o in objects]
    duplicates = Counter(ids)
    entries = []
    for obj, identity in zip(objects, ids):
        reasons = set(global_reasons) | protected.get(identity, set())
        labels = obj.get("labels")
        version = obj.get("version")
        refs = _refs(obj.get("refs", []))
        reasons.update("object_reference:" + ref for ref in refs)
        if duplicates[identity] > 1:
            reasons.add("ambiguous_duplicate_identity")
        if obj.get("kind") not in KINDS or not isinstance(obj.get("name"), str) or not obj.get("name"):
            reasons.add("unknown_object_kind_or_name")
        if not isinstance(labels, dict):
            reasons.add("missing_labels")
            labels = {}
        if labels.get("com.codepier.deployment") != deployment:
            reasons.add("foreign_or_unknown_deployment")
        if labels.get("com.codepier.role") not in ROLES:
            reasons.add("unknown_role")
        if labels.get("com.codepier.role") == "assets":
            reasons.add("stable_assets")
        if obj.get("ambiguous") is True or "refs" not in obj:
            reasons.add("ambiguous_or_missing_metadata")
        size = obj.get("allocated_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            reasons.add("unknown_allocated_bytes")
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            reasons.add("unknown_version")
        elif version == current:
            reasons.add("current_version")
        elif version in retained:
            reasons.add("retained_historical_version")
        known_version = isinstance(version, str) and bool(VERSION.fullmatch(version))
        group = "failed_candidate" if identity in failed_refs and (not known_version or version not in historical) else "historical"
        if group == "historical" and version != current and (not known_version or version not in historical):
            reasons.add("no_successful_version_evidence")
        entries.append({
            "id": identity, "kind": obj.get("kind"), "name": obj.get("name"),
            "labels": labels, "version": version, "allocated_bytes": size,
            "references": sorted(set(refs) | protected.get(identity, set())),
            "protection_reasons": sorted(reasons), "group": group,
            "disposition": "protected" if reasons else "review_candidate",
            "expected_object_sha256": digest(obj),
        })
    body = {
        "schema_version": 1, "mode": "read_only", "deletion_authorized": False,
        "deployment": deployment, "current_version": current,
        "historical_versions_to_keep": retained, "history_limit": 2,
        "expected_state_sha256": digest(data),
        "expected_reference_sha256": digest({k: data.get(k) for k in (*REF_FIELDS, "containers", "jobs", "deploy_lock")}),
        "objects": sorted(entries, key=lambda entry: (entry["id"], entry["expected_object_sha256"])),
        "warnings": sorted(set(global_reasons)),
    }
    encoded = canonical(body)
    return RetentionManifest(encoded, hashlib.sha256(encoded.encode()).hexdigest())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only retention manifest from trusted inventory JSON; never deletes")
    parser.add_argument("inventory", type=Path)
    args = parser.parse_args(argv)
    manifest = plan_retention(json.loads(args.inventory.read_text(encoding="utf-8")))
    print(canonical({"manifest": manifest.as_dict(), "manifest_sha256": manifest.sha256}))


if __name__ == "__main__":
    main()
