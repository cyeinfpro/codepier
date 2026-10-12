"""Synthetic evidence only: no network, installed services or GitHub mutation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import urllib.error
import zipfile

import pytest
import yaml

from scripts import check_ci_promotion as promotion


SHA = "a" * 40
TREE = "b" * 40
WORKFLOW_SHA = promotion.digest(b"name: CodePier CI\n")
NOW = datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)
START = "2026-10-12T01:00:00Z"
END = "2026-10-12T02:00:00Z"


def encode(value):
    return json.dumps(value, sort_keys=True).encode()


def zip_bytes(files):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            entry = zipfile.ZipInfo(name, date_time=(2026, 9, 14, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, raw)
    return target.getvalue()


def replace_artifact(case, name, filename, change):
    snapshot, payloads, _ = case
    item = next(item for item in snapshot["artifacts"] if item["name"] == name)
    files = {key: value[0] for key, value in promotion.unpack(payloads[item["id"]]).items()}
    value = json.loads(files[filename])
    change(value)
    files[filename] = encode(value)
    raw = zip_bytes(files)
    payloads[item["id"]] = raw
    item["digest"] = "sha256:" + promotion.digest(raw)
    item["size_in_bytes"] = len(raw)


@pytest.fixture
def evidence():
    source = {
        "README.md": b"CodePier synthetic source\n",
        "shared/util.py": b"VERSION = '1.2.3'\n",
        ".github/workflows/ci.yml": b"name: CodePier CI\n",
        "pyproject.toml": b"[tool.pytest.ini_options]\n",
    }
    files = {name: {"sha256": promotion.digest(raw), "bytes": len(raw), "mode": 0o644}
             for name, raw in source.items()}
    full_inventory = promotion.source_inventory_sha256(files)
    assets = {}
    for panel, suffix in ((False, "-source-full"), (True, "-source")):
        selected = {name: raw for name, raw in source.items()
                    if not panel or name not in promotion.PANEL_UPDATE_EXCLUDES}
        selected_files = {name: files[name] for name in selected}
        inner = "".join(files[name]["sha256"] + "  " + name + "\n" for name in sorted(selected)).encode()
        raw = zip_bytes({**selected, "MANIFEST.sha256": inner})
        name = "codepier-1.2.3" + suffix
        assets[name + ".zip"] = raw
        assets[name + ".manifest.json"] = encode({
            "inventory": {key: {**item, "mode": oct(item["mode"])} for key, item in selected_files.items()},
            "sha256": promotion.digest(raw), "bytes": len(raw), "verified": True,
            "public_profile": True, "source_only": True, "panel_update_profile": panel,
        })
    build = {"schema": 1, "repository": promotion.REPOSITORY, "commit": SHA, "tree": TREE,
             "run_id": 100, "run_attempt": 1, "event": "workflow_dispatch",
             "workflow_path": promotion.WORKFLOW_PATH, "workflow_sha256": WORKFLOW_SHA,
             "source_inventory_sha256": full_inventory,
             "assets": {name: {"sha256": promotion.digest(raw), "bytes": len(raw)}
                        for name, raw in assets.items()}}
    source_artifact = {**assets, "source-build.json": encode(build)}
    for panel, suffix in ((False, "-source-full"), (True, "-source")):
        raw = assets["codepier-1.2.3" + suffix + ".zip"]
        source_artifact["panel-update-check.json" if panel else "public-check.json"] = encode({
            "passed": True, "version": "1.2.3", "inventory_sha256": full_inventory,
            "verified_against_current_source": True, "sha256": promotion.digest(raw), "bytes": len(raw),
            **({"panel_updaters_verified": ["1.13.0", "current"]} if panel else {}),
        })
    summary = {"verified": True, "scope": "complete", "layout": promotion.RESOURCE_LAYOUT,
               "shards": 6, "coverage_merged": True, "collected": 99,
               "counts": {"passed": 99, "failed": 0, "skipped": 0, "missing": 0},
               "collection_sha256": "d" * 64,
               "source_inventory_sha256": promotion.regression_inventory_sha256(files)}
    proof = {"verified": True, "commit": SHA,
             "archive_sha256": promotion.digest(assets["codepier-1.2.3-source-full.zip"]),
             "source_inventory_sha256": full_inventory, "public_files": len(files), "shards": 6}
    complete_artifact = {"summary.json": encode(summary), "archive-proof.json": encode(proof),
                         "coverage.json": encode({"files": {"shared/util.py": {}},
                                                  "totals": {"num_statements": 1}})}
    payloads, artifacts = {}, []
    for index, (name, contents) in enumerate([
        ("public-source", source_artifact), ("complete-ubuntu-24.04", complete_artifact),
        ("complete-macos-15", complete_artifact),
    ], 200):
        raw = zip_bytes(contents)
        payloads[index] = raw
        artifacts.append({"id": index, "name": name, "expired": False,
                          "created_at": END, "updated_at": END, "expires_at": "2026-10-20T00:00:00Z",
                          "digest": "sha256:" + promotion.digest(raw), "size_in_bytes": len(raw),
                          "workflow_run": {"id": 100, "head_sha": SHA,
                                           "repository_id": 123, "head_repository_id": 123}})
    repository = {"id": 123, "full_name": promotion.REPOSITORY}
    workflow = {"id": 321, "path": promotion.WORKFLOW_PATH, "name": promotion.WORKFLOW_NAME,
                "state": "active"}
    run = {"id": 100, "run_attempt": 1, "repository": repository, "head_repository": repository,
           "event": "workflow_dispatch", "head_sha": SHA, "head_commit": {"id": SHA},
           "workflow_id": 321, "path": promotion.WORKFLOW_PATH, "name": promotion.WORKFLOW_NAME,
           "status": "completed", "conclusion": "success", "check_suite_id": 400,
           "html_url": "https://github.com/cyeinfpro/codepier/actions/runs/100",
           "created_at": START, "run_started_at": START, "updated_at": END}
    jobs = [{"id": index, "name": name, "run_id": 100, "run_attempt": 1, "head_sha": SHA,
             "status": "completed", "conclusion": "success", "started_at": START, "completed_at": END}
            for index, name in enumerate(sorted(promotion.EXPECTED_JOBS), 1000)]
    snapshot = {"repository": repository, "workflow": workflow, "run": run, "jobs": jobs,
                "workflow_sha256": WORKFLOW_SHA, "tree": TREE, "runs": [deepcopy(run)],
                "all_jobs": deepcopy(jobs),
                "check_runs": [], "statuses": [], "artifacts": artifacts}
    kwargs = {"expected_sha": SHA, "expected_workflow_sha256": WORKFLOW_SHA,
              "expected_tree": TREE, "source_files": files, "version": "1.2.3", "now": NOW}
    return snapshot, payloads, kwargs


def verify(case):
    snapshot, payloads, kwargs = case
    return promotion.verify_evidence(snapshot, payloads, **kwargs)


def test_complete_candidate_reuses_identical_four_assets(evidence):
    result, assets = verify(evidence)
    assert result["verified"] is True
    assert result["mode"] == "reuse-complete-evidence"
    assert result["candidate_run_id"] == 100
    assert result["candidate_run_attempt"] == 1
    assert len(result["jobs"]) == 19
    assert len(result["artifacts"]) == 3
    assert len(assets) == 4
    assert result["assets"] == {name: {"sha256": promotion.digest(raw), "bytes": len(raw)}
                                for name, raw in assets.items()}


@pytest.mark.parametrize(("field", "value"), [
    ("event", "push"), ("event", "pull_request"), ("head_sha", "f" * 40),
    ("workflow_id", 999), ("path", ".github/workflows/other.yml"), ("name", "Other CI"),
    ("status", "in_progress"), ("conclusion", "failure"), ("conclusion", "cancelled"),
    ("conclusion", "skipped"), ("run_attempt", 2), ("run_attempt", True),
    ("created_at", "2026-09-01T00:00:00Z"), ("updated_at", "2027-01-01T00:00:00Z"),
    ("html_url", "https://example.com/actions/runs/100"), ("check_suite_id", None),
])
def test_candidate_identity_and_latest_attempt_fail_closed(evidence, field, value):
    evidence[0]["run"][field] = value
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


@pytest.mark.parametrize(("group", "field", "value"), [
    ("repository", "full_name", "attacker/codepier"), ("repository", "id", False),
    ("workflow", "path", ".github/workflows/fake.yml"), ("workflow", "state", "disabled_manually"),
    ("workflow", "id", 999),
])
def test_wrong_repository_or_workflow(evidence, group, field, value):
    evidence[0][group][field] = value
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


@pytest.mark.parametrize("field", ["workflow_sha256", "tree"])
def test_source_revision_bound(evidence, field):
    evidence[0][field] = "f" * len(evidence[0][field])
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


@pytest.mark.parametrize(("field", "value"), [
    ("name", "pretend success"), ("run_id", 99), ("run_attempt", 2), ("head_sha", "f" * 40),
    ("status", "queued"), ("conclusion", "failure"), ("conclusion", "skipped"),
    ("completed_at", None),
])
def test_each_required_job_must_really_pass_latest_attempt(evidence, field, value):
    evidence[0]["jobs"][0][field] = value
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_missing_duplicate_or_extra_job_is_not_full_ci(evidence):
    for jobs in (evidence[0]["jobs"][:-1], evidence[0]["jobs"] + [evidence[0]["jobs"][0]],
                 evidence[0]["jobs"][:-1] + [evidence[0]["jobs"][0]]):
        changed = deepcopy(evidence)
        changed[0]["jobs"] = jobs
        with pytest.raises(promotion.EvidenceError):
            verify(changed)


@pytest.mark.parametrize(("field", "value"), [
    ("expired", True), ("expires_at", START), ("created_at", "2026-10-11T00:00:00Z"),
    ("digest", "sha256:" + "0" * 64), ("size_in_bytes", 1), ("id", True),
])
def test_artifact_identity_expiry_and_content_bound(evidence, field, value):
    evidence[0]["artifacts"][0][field] = value
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


@pytest.mark.parametrize(("field", "value"), [
    ("id", 99), ("head_sha", "f" * 40), ("head_repository_id", 666), ("repository_id", 666),
])
def test_artifact_provenance_bound(evidence, field, value):
    evidence[0]["artifacts"][0]["workflow_run"][field] = value
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_missing_or_duplicate_artifact(evidence):
    changed = deepcopy(evidence)
    changed[0]["artifacts"].pop()
    with pytest.raises(promotion.EvidenceError):
        verify(changed)
    evidence[0]["artifacts"].append(deepcopy(evidence[0]["artifacts"][0]))
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


@pytest.mark.parametrize(("field", "value"), [
    ("verified", False), ("scope", "shard"), ("shards", 5), ("coverage_merged", False),
    ("layout", "four-plus-resource-v1"), ("collection_sha256", "f" * 64),
    ("source_inventory_sha256", "f" * 64), ("source_changed_during_run", True),
    ("duplicate_phases", ["test"]), ("coverage_error", "missing"),
    ("counts", {"passed": 98, "failed": 0, "skipped": 1, "missing": 0}),
    ("collected", True),
])
def test_incomplete_or_cross_platform_conflicting_summary(evidence, field, value):
    replace_artifact(evidence, "complete-macos-15", "summary.json", lambda report: report.update({field: value}))
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


@pytest.mark.parametrize(("field", "value"), [
    ("verified", False), ("commit", "f" * 40), ("archive_sha256", "f" * 64),
    ("source_inventory_sha256", "f" * 64), ("shards", 5), ("public_files", 999),
])
def test_archive_proof_must_match_reused_zip(evidence, field, value):
    replace_artifact(evidence, "complete-ubuntu-24.04", "archive-proof.json",
                     lambda report: report.update({field: value}))
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


@pytest.mark.parametrize(("field", "value"), [
    ("commit", "f" * 40), ("tree", "f" * 40), ("run_id", 999), ("run_attempt", 2),
    ("event", "push"), ("workflow_sha256", "f" * 64), ("assets", {}),
])
def test_build_provenance_must_be_exact(evidence, field, value):
    replace_artifact(evidence, "public-source", "source-build.json", lambda report: report.update({field: value}))
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_missing_coverage_and_updater_proof(evidence):
    for artifact, filename, change in [
        ("complete-macos-15", "coverage.json", lambda report: report.update(files={})),
        ("public-source", "panel-update-check.json", lambda report: report.pop("panel_updaters_verified")),
    ]:
        changed = deepcopy(evidence)
        replace_artifact(changed, artifact, filename, change)
        with pytest.raises(promotion.EvidenceError):
            verify(changed)


@pytest.mark.parametrize("state", ["in_progress", "queued", "failure", "cancelled", "skipped"])
def test_conflicting_newer_run_blocks_promotion(evidence, state):
    later = deepcopy(evidence[0]["run"])
    later.update(id=101, created_at=END, updated_at="2026-10-12T02:30:00Z",
                 status=state if state in {"in_progress", "queued"} else "completed",
                 conclusion=None if state in {"in_progress", "queued"} else state)
    evidence[0]["runs"].append(later)
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_external_checks_and_statuses_do_not_define_ci_reuse(evidence):
    evidence[0]["check_runs"] = [{"head_sha": SHA, "check_suite": {"id": 999},
                                  "started_at": END, "status": "in_progress", "conclusion": None}]
    evidence[0]["statuses"] = [{"updated_at": END, "state": "failure"}]
    assert verify(evidence)[0]["verified"]
    evidence[0].pop("check_runs")
    evidence[0].pop("statuses")
    assert verify(evidence)[0]["verified"]


def test_current_promotion_run_is_exempt_but_cannot_promote_itself(evidence):
    current = deepcopy(evidence[0]["run"])
    current.update(id=101, status="in_progress", conclusion=None, check_suite_id=401,
                   event="push", head_branch="main", run_started_at=END)
    evidence[0]["current_run"] = current
    evidence[0]["main_sha"] = SHA
    evidence[0]["runs"].append(current)
    evidence[0]["check_runs"].append({"head_sha": SHA, "check_suite": {"id": 401},
                                     "started_at": END, "status": "in_progress", "conclusion": None})
    evidence[2]["current_run_id"] = 101
    assert verify(evidence)[0]["verified"]
    evidence[2]["current_run_id"] = 100
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


@pytest.mark.parametrize("raw", [b'{"verified":true,"verified":false}', b'{"x":NaN}', b"not JSON"])
def test_ambiguous_json_is_rejected(raw):
    with pytest.raises(promotion.EvidenceError):
        promotion.load_json(raw)


@pytest.mark.parametrize("name", ["../secret", "/absolute", "a/../../escape", "a\\b", "a//b"])
def test_artifact_paths_never_escape(name):
    with pytest.raises(promotion.EvidenceError):
        promotion.unpack(zip_bytes({name: b"x"}))


def test_duplicate_and_symlink_zip_members():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        entry = zipfile.ZipInfo("link")
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, b"/etc/passwd")
    with pytest.raises(promotion.EvidenceError):
        promotion.unpack(stream.getvalue())


def test_corrupt_or_changed_archive_bytes(evidence):
    item = evidence[0]["artifacts"][0]
    evidence[1][item["id"]] = evidence[1][item["id"]][:-5]
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_source_zip_checks_file_modes_and_bytes(evidence):
    _, assets = verify(evidence)
    manifest = json.loads(assets["codepier-1.2.3-source-full.manifest.json"])
    files = deepcopy(evidence[2]["source_files"])
    files["README.md"]["mode"] = 0o755
    with pytest.raises(promotion.EvidenceError):
        promotion.verify_bundle(assets["codepier-1.2.3-source-full.zip"], manifest, files, panel=False)


def test_stale_evidence_time(evidence):
    evidence[2]["now"] = NOW + timedelta(days=8)
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_paginated_evidence_never_accepts_missing_items():
    reader = promotion.GitHubReader()
    reader.get = lambda path: {"total_count": 2, "jobs": [{"id": 1}]}
    with pytest.raises(promotion.EvidenceError):
        reader.collection("/jobs", "jobs")


def test_reader_refuses_unpinned_url():
    reader = promotion.GitHubReader()
    for value in ("https://evil.example/x", "//evil.example/x"):
        with pytest.raises(promotion.EvidenceError):
            reader.get(value)


def test_download_redirect_never_carries_token():
    reader = promotion.GitHubReader("synthetic-read-token")
    calls = []

    class Response(io.BytesIO):
        pass

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 302, "Found",
                                             {"Location": "https://example.blob.core.windows.net/file?signature=synthetic"},
                                             None)
            return Response(b"ZIP bytes")
    reader.opener = Opener()
    assert reader.get("/actions/artifacts/1/zip", archive=True) == b"ZIP bytes"
    assert calls[0].get_header("Authorization") == "Bearer synthetic-read-token"
    assert calls[1].get_header("Authorization") is None


def test_denial_does_not_retry_with_another_identity():
    reader = promotion.GitHubReader("synthetic-read-token")
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 403, "Denied", {}, None)
    reader.opener = Opener()
    with pytest.raises(promotion.EvidenceError):
        reader.get("/actions/artifacts/1/zip", archive=True)
    assert len(calls) == 1


def test_cli_failure_writes_full_ci_decision_without_network(tmp_path, monkeypatch):
    from scripts import check_release
    monkeypatch.setattr(check_release, "check_source", lambda root: (_ for _ in ()).throw(ValueError("invalid source")))
    output = tmp_path / "decision.json"
    assert promotion.main(["--sha", SHA, "--output", str(output)]) == 1
    assert json.loads(output.read_text()) == {
        "schema": 1, "verified": False, "eligible": False, "mode": "full-ci", "reason": "ValueError",
    }


class SnapshotReader:
    """Minimal GET fixture with list/get response-shape differences."""
    def __init__(self, case):
        self.snapshot, self.payloads, _ = case
        self.calls = []
        self.run_reads = 0
        self.changed = None

    def get(self, path, *, archive=False):
        import base64
        self.calls.append(path)
        if path == "":
            return deepcopy(self.snapshot["repository"])
        if path == "/actions/workflows/ci.yml":
            return deepcopy(self.snapshot["workflow"])
        if path == "/actions/runs/100":
            self.run_reads += 1
            result = deepcopy(self.snapshot["run"])
            result["detail_only_field"] = "unrelated"
            if self.changed and self.run_reads > 1:
                result.update(self.changed)
            return result
        if path == "/git/commits/" + SHA:
            return {"sha": SHA, "tree": {"sha": TREE}}
        if path == "/contents/.github/workflows/ci.yml?ref=" + SHA:
            return {"encoding": "base64", "content": base64.b64encode(b"name: CodePier CI\n").decode()}
        if path.startswith("/actions/artifacts/") and archive:
            return self.payloads[int(path.split("/")[-2])]
        raise AssertionError("Unexpected endpoint: " + path)

    def collection(self, path, key=None):
        self.calls.append(path)
        if path.startswith("/actions/workflows/321/runs?"):
            return deepcopy(self.snapshot["runs"])
        if path == "/actions/runs/100/jobs?filter=latest":
            return deepcopy(self.snapshot["jobs"])
        if path == "/actions/runs/100/jobs?filter=all":
            return deepcopy(self.snapshot["all_jobs"])
        if path == "/actions/runs/100/artifacts":
            return deepcopy(self.snapshot["artifacts"])
        if path == "/commits/" + SHA + "/check-runs?filter=latest":
            return deepcopy(self.snapshot["check_runs"])
        if path == "/commits/" + SHA + "/statuses":
            return deepcopy(self.snapshot["statuses"])
        raise AssertionError("Unexpected endpoint: " + path)


@pytest.mark.parametrize("candidate", [None, 100])
def test_live_reader_matches_explicit_or_bounded_latest_candidate(evidence, candidate):
    reader = SnapshotReader(evidence)
    snapshot, payloads = promotion.live_snapshot(reader, expected_sha=SHA,
                                                candidate_run_id=candidate, current_run_id=0)
    receipt, assets = promotion.verify_evidence(snapshot, payloads, **evidence[2])
    assert receipt["verified"]
    assert len(assets) == 4
    assert reader.run_reads == 2
    assert not any("/check-runs" in path or "/statuses" in path for path in reader.calls)
    assert "/actions/runs/100/jobs?filter=latest" in reader.calls
    assert "/actions/runs/100/jobs?filter=all" in reader.calls


@pytest.mark.parametrize("change", [{"run_attempt": 2}, {"status": "in_progress"},
                                   {"conclusion": "failure"}, {"head_sha": "f" * 40}])
def test_live_reader_rereads_latest_candidate_revision(evidence, change):
    reader = SnapshotReader(evidence)
    reader.changed = change
    with pytest.raises(promotion.EvidenceError):
        promotion.live_snapshot(reader, expected_sha=SHA, candidate_run_id=100, current_run_id=0)


def test_external_check_metadata_is_not_required(evidence):
    evidence[0]["check_runs"] = [{"head_sha": SHA, "status": "in_progress", "started_at": END}]
    assert verify(evidence)[0]["verified"]


@pytest.mark.parametrize(("field", "value"), [
    ("event", "workflow_dispatch"), ("head_branch", "other"),
    ("status", "completed"), ("conclusion", "failure"), ("run_started_at", START),
])
def test_only_current_main_push_can_be_exempt(evidence, field, value):
    current = deepcopy(evidence[0]["run"])
    current.update(id=101, status="in_progress", conclusion=None, check_suite_id=401,
                   event="push", head_branch="main", run_started_at=END)
    current[field] = value
    evidence[0]["current_run"] = current
    evidence[0]["main_sha"] = SHA
    evidence[2]["current_run_id"] = 101
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


@pytest.mark.parametrize("url", ["http://example.blob.core.windows.net/file",
                                "https://evil.example/zip",
                                "https://example.blob.core.windows.net.evil.example/zip",
                                "https://user:password@example.blob.core.windows.net/zip"])
def test_download_redirect_is_pinned_to_https_artifact_hosts(url):
    reader = promotion.GitHubReader("synthetic-read-token")

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {"Location": url}, None)
    reader.opener = Opener()
    with pytest.raises(promotion.EvidenceError):
        reader.get("/actions/artifacts/1/zip", archive=True)


def test_pagination_checks_every_page_and_total():
    reader = promotion.GitHubReader()
    calls = []

    def get(path):
        calls.append(path)
        return {"total_count": 101, "jobs": list(range(100)) if len(calls) == 1 else [100]}
    reader.get = get
    assert reader.collection("/jobs", "jobs") == list(range(101))
    assert len(calls) == 2


def inherited_rerun(case):
    snapshot = case[0]
    snapshot["run"].update(run_attempt=2, run_started_at="2026-10-12T02:10:00Z",
                           updated_at="2026-10-12T02:30:00Z")
    snapshot["runs"] = [deepcopy(snapshot["run"])]
    target = next(job for job in snapshot["jobs"] if job["name"] == "Release gate")
    old = next(job for job in snapshot["all_jobs"] if job["id"] == target["id"])
    old["conclusion"] = "failure"
    target.update(id=9999, run_attempt=2, started_at="2026-10-12T02:10:00Z",
                  completed_at="2026-10-12T02:30:00Z")
    snapshot["all_jobs"].append(deepcopy(target))
    return case


def test_latest_effective_rerun_preserves_original_successful_source(evidence):
    result, assets = verify(inherited_rerun(evidence))
    assert result["candidate_run_attempt"] == 2
    assert result["source_build_attempt"] == 1
    assert len(assets) == 4
    origins = {job["name"]: job["origin_attempt"] for job in result["jobs"]}
    assert origins["Release gate"] == 2
    assert origins["Build verified public source"] == 1


def test_older_success_cannot_hide_later_failed_attempt(evidence):
    inherited_rerun(evidence)
    hidden = deepcopy(evidence[0]["jobs"][0])
    hidden.update(id=9998, run_attempt=2, conclusion="failure")
    evidence[0]["all_jobs"].append(hidden)
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_original_source_must_match_its_producer_attempt(evidence):
    inherited_rerun(evidence)
    replace_artifact(evidence, "public-source", "source-build.json",
                     lambda report: report.update(run_attempt=2))
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_missing_history_or_duplicate_attempt_fails_closed(evidence):
    changed = deepcopy(evidence)
    changed[0]["all_jobs"].pop()
    with pytest.raises(promotion.EvidenceError):
        verify(changed)
    duplicate = deepcopy(evidence[0]["all_jobs"][0])
    duplicate["id"] = 9997
    evidence[0]["all_jobs"].append(duplicate)
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_unrelated_container_check_does_not_block_same_commit_ci(evidence):
    evidence[0]["check_runs"] = [{"head_sha": SHA, "check_suite": {"id": 999},
                                  "started_at": END, "status": "in_progress", "conclusion": None}]
    assert verify(evidence)[0]["verified"]


def test_github_copied_successful_jobs_keep_original_artifact_attempt(evidence):
    inherited_rerun(evidence)
    snapshot = evidence[0]
    for index, job in enumerate(snapshot["jobs"], 20000):
        if job["run_attempt"] == 1:
            job.update(id=index, run_attempt=2)
            snapshot["all_jobs"].append(deepcopy(job))
    result, _ = verify(evidence)
    source = next(job for job in result["jobs"] if job["name"] == "Build verified public source")
    assert source["effective_attempt"] == 2
    assert source["origin_attempt"] == result["source_build_attempt"] == 1


def test_copied_job_with_different_steps_cannot_claim_old_source(evidence):
    inherited_rerun(evidence)
    snapshot = evidence[0]
    job = next(job for job in snapshot["jobs"] if job["name"] == "Build verified public source")
    job.update(id=20000, run_attempt=2, steps=[{"name": "different", "conclusion": "success"}])
    snapshot["all_jobs"].append(deepcopy(job))
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_copied_job_requires_original_successful_build_record(evidence):
    inherited_rerun(evidence)
    snapshot = evidence[0]
    job = next(job for job in snapshot["jobs"] if job["name"] == "Build verified public source")
    original = next(item for item in snapshot["all_jobs"] if item["id"] == job["id"])
    original["conclusion"] = "failure"
    job.update(id=20000, run_attempt=2)
    snapshot["all_jobs"].append(deepcopy(job))
    with pytest.raises(promotion.EvidenceError):
        verify(evidence)


def test_current_main_must_still_point_to_expected_commit(evidence):
    current = deepcopy(evidence[0]["run"])
    current.update(id=101, status="in_progress", conclusion=None, check_suite_id=401,
                   event="push", head_branch="main", run_started_at=END)
    evidence[0]["current_run"] = current
    evidence[0]["main_sha"] = "f" * 40
    evidence[2]["current_run_id"] = 101
    with pytest.raises(promotion.EvidenceError, match="Main has moved"):
        verify(evidence)

def test_ci_actions_read_is_limited_to_main_promotion_validator():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        expected = {"contents": "read", "actions": "read"} if name == "promotion" else {"contents": "read"}
        assert job.get("permissions", workflow["permissions"]) == expected
    job = workflow["jobs"]["promotion"]
    proof = next(step for step in job["steps"] if step.get("id") == "proof")
    assert proof["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert proof["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "--token-env GH_TOKEN" in proof["run"]
    assert not proof.get("continue-on-error")
    for step in job["steps"]:
        if step is not proof:
            assert "GH_TOKEN" not in step.get("env", {})


@pytest.mark.parametrize("event,ref,exit_code,reused", [
    ("push", "refs/heads/main", 0, "true"),
    ("push", "refs/heads/main", 1, "false"),
    ("push", "refs/heads/other", 0, "false"),
    ("workflow_dispatch", "refs/heads/main", 0, "false"),
    ("pull_request", "refs/pull/1/merge", 0, "false"),
])
def test_ci_promotion_shell_falls_back_without_success(tmp_path, event, ref, exit_code, reused):
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    proof = next(step for step in workflow["jobs"]["promotion"]["steps"] if step.get("id") == "proof")
    binary = tmp_path / "bin"
    binary.mkdir()
    call_file = tmp_path / "called.json"
    stub = binary / "python3"
    stub.write_text(
        f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
        f"Path({str(call_file)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "assert os.environ['GH_TOKEN'] == 'synthetic-read-only-token'\n"
        f"raise SystemExit({exit_code})\n"
    )
    stub.chmod(0o755)
    output = tmp_path / "github-output"
    environment = {
        **os.environ, "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
        "EVENT_NAME": event, "REF_NAME": ref, "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": promotion.REPOSITORY, "GITHUB_SHA": SHA, "GITHUB_RUN_ID": "101",
        "GH_TOKEN": "synthetic-read-only-token",
    }
    environment.pop("BASH_ENV", None)
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", proof["run"]],
                            cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines()[-1] == f"reused={reused}"
    assert call_file.exists() == (event == "push" and ref == "refs/heads/main")
    if call_file.exists():
        args = json.loads(call_file.read_text())
        assert args == ["scripts/check_ci_promotion.py", "--repository", promotion.REPOSITORY,
                        "--sha", SHA, "--current-run-id", "101", "--token-env", "GH_TOKEN",
                        "--output", "dist/promotion/proof.json"]
    assert "synthetic-read-only-token" not in result.stdout + result.stderr
