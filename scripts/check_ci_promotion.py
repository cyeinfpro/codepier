#!/usr/bin/env python3
"""Read-only, fail-closed promotion of a complete same-commit CI candidate.

A successful decision reuses immutable evidence; it does not mark skipped jobs
as executed. The CLI reads the pinned GitHub repository, never posts a check,
changes a release, or rebuilds source assets. Missing evidence means full CI.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.build_source_bundle import PANEL_UPDATE_EXCLUDES, PUBLIC_DOCS
from scripts.regression_plan import RESOURCE_LAYOUT, fingerprint

REPOSITORY = "cyeinfpro/codepier"
WORKFLOW_PATH = ".github/workflows/ci.yml"
WORKFLOW_NAME = "CodePier CI"
PLATFORMS = ("ubuntu-24.04", "macos-15")
SHARDS = 6
MAX_BYTES = 256 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024
MAX_AGE = timedelta(days=7)
EXPECTED_JOBS = frozenset({
    "Validate same-commit promotion", "Build verified public source",
    "Windows core, bootstrap and background recovery",
    "Real Authentik OIDC acceptance", "Release gate",
    *("Complete evidence " + platform for platform in PLATFORMS),
    *(f"{'Resource admission' if shard == 4 else 'Long wall-clock' if shard == 5 else 'Regression'} "
      f"{platform} shard {shard} of {SHARDS}"
      for platform in PLATFORMS for shard in range(SHARDS)),
})
ARTIFACT_NAMES = frozenset({"public-source", *("complete-" + os_name for os_name in PLATFORMS)})


class EvidenceError(ValueError):
    """Unusable evidence. Callers must fall back to full CI."""


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def exact_int(value, minimum=1):
    return type(value) is int and value >= minimum


def sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def timestamp(value):
    require(isinstance(value, str), "Missing timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError("Invalid timestamp") from error
    require(result.tzinfo is not None, "Timestamp needs a timezone")
    return result.astimezone(timezone.utc)


def load_json(raw):
    require(len(raw) <= MAX_JSON, "JSON exceeds size limit")

    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_):
        raise EvidenceError("Non-finite JSON number")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError("Invalid JSON evidence") from error


def unpack(raw, *, source=False):
    """Read bounded ZIP bytes without writing artifact-controlled paths."""
    require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES, "Artifact exceeds size limit")
    result = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            require(len(entries) <= 20000, "Too many archive members")
            require(sum(entry.file_size for entry in entries) <= MAX_BYTES, "Expanded archive too large")
            for entry in entries:
                path = PurePosixPath(entry.filename)
                require(entry.filename and not path.is_absolute() and
                        ".." not in path.parts and "\\" not in entry.filename and
                        str(path) == entry.filename and entry.filename not in result,
                        "Unsafe or duplicate archive path")
                mode = entry.external_attr >> 16
                require(not entry.is_dir() and not stat.S_ISLNK(mode) and
                        (stat.S_ISREG(mode) if source else not stat.S_IFMT(mode) or stat.S_ISREG(mode)),
                        "Non-regular archive member")
                require(not entry.flag_bits & ~0x80e and
                        entry.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
                        "Unsupported archive encoding")
                limit = 8 * 1024 * 1024 if source else MAX_BYTES
                require(0 <= entry.file_size <= limit, "Archive member too large")
                with archive.open(entry) as stream:
                    value = stream.read(entry.file_size + 1)
                require(len(value) == entry.file_size, "Archive member size differs")
                result[entry.filename] = (value, stat.S_IMODE(mode), entry.date_time)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as error:
        raise EvidenceError("Invalid artifact ZIP") from error
    return result


def source_inventory_sha256(files):
    return digest(json.dumps(files, sort_keys=True).encode())


def regression_inventory_sha256(files):
    directories = {"agent", "hub", "shared", "scripts", "tests", "web", "deploy",
                   "skills", ".github", "requirements"}
    top = {"Dockerfile", "compose.yml", "codepier", "codepier.ps1", "install.sh",
           ".dockerignore", ".gitignore", ".gitattributes", ".env.example", "ruff.toml",
           "pyproject.toml", ".prettierrc.json", "README.md", "CHANGELOG.md",
           "SECURITY.md", "CONTRIBUTING.md", "RELEASE.json"}
    selected = {name: item["sha256"] for name, item in files.items()
                if name.split("/")[0] in directories or name in top or name in PUBLIC_DOCS or
                ("/" not in name and name.startswith("requirements") and
                 Path(name).suffix in {".txt", ".in"})}
    return fingerprint(selected)


def verify_bundle(raw, manifest, files, *, panel):
    entries = unpack(raw, source=True)
    require(set(entries) == set(files) | {"MANIFEST.sha256"}, "Source ZIP inventory differs")
    require(len({item[2] for item in entries.values()}) == 1, "Source ZIP timestamps differ")
    expected_manifest = "".join(files[name]["sha256"] + "  " + name + "\n"
                                for name in sorted(files)).encode()
    require(entries["MANIFEST.sha256"][0] == expected_manifest and
            entries["MANIFEST.sha256"][1] == 0o644, "Internal source manifest differs")
    for name, item in files.items():
        content, mode, _ = entries[name]
        require(digest(content) == item["sha256"] and len(content) == item["bytes"] and
                mode == item["mode"], "Source ZIP differs from committed source")
    expected = {name: {**item, "mode": oct(item["mode"])} for name, item in files.items()}
    require(manifest.get("inventory") == expected and manifest.get("sha256") == digest(raw) and
            manifest.get("bytes") == len(raw) and manifest.get("verified") is True and
            manifest.get("public_profile") is True and manifest.get("source_only") is True and
            manifest.get("panel_update_profile") is panel, "External source manifest differs")


def run_revision(run):
    """Compare security-relevant fields shared by run-list and run-detail APIs."""
    fields = ("id", "run_attempt", "head_sha", "workflow_id", "path", "status", "conclusion",
              "created_at", "run_started_at", "updated_at", "check_suite_id")
    return {key: run.get(key) for key in fields}


def execution_revision(job):
    """GitHub can copy a successful job into a newer attempt with a new ID."""
    fields = ("name", "head_sha", "status", "conclusion", "started_at", "completed_at", "steps")
    return {key: job.get(key) for key in fields}


def origin_attempt(job, history):
    matches = [item["run_attempt"] for item in history
               if execution_revision(item) == execution_revision(job)]
    require(bool(matches), "Missing original job execution")
    return min(matches)


def verify_run(run, workflow, expected_sha, repository_id, now):
    require(exact_int(run.get("id")) and exact_int(run.get("run_attempt")), "Invalid run identity")
    require(run.get("repository", {}).get("full_name") == REPOSITORY and
            run.get("repository", {}).get("id") == repository_id and
            run.get("head_repository", {}).get("full_name") == REPOSITORY and
            run.get("head_repository", {}).get("id") == repository_id, "Candidate repository differs")
    require(run.get("event") == "workflow_dispatch" and run.get("head_sha") == expected_sha and
            run.get("head_commit", {}).get("id") == expected_sha, "Candidate event or commit differs")
    require(run.get("workflow_id") == workflow["id"] and run.get("path") == WORKFLOW_PATH and
            run.get("name") == WORKFLOW_NAME, "Candidate workflow differs")
    require(run.get("status") == "completed" and run.get("conclusion") == "success",
            "Candidate is not successfully completed")
    require(run.get("html_url") == f"https://github.com/{REPOSITORY}/actions/runs/{run['id']}",
            "Candidate URL differs")
    created, started, updated = (timestamp(run.get(key)) for key in
                                 ("created_at", "run_started_at", "updated_at"))
    require(now - MAX_AGE <= created <= started <= updated <= now, "Candidate is stale or inconsistent")
    require(exact_int(run.get("check_suite_id")), "Missing candidate check suite")


def verify_evidence(snapshot, artifact_payloads, *, expected_sha, expected_workflow_sha256,
                    expected_tree, source_files, version, now, current_run_id=0):
    """Pure validation of an authenticated/read-only GitHub snapshot and ZIP bytes.

    The snapshot is data, not a signed attestation. Only the CLI's pinned live
    reader establishes its provenance. Synthetic/offline snapshots are for tests.
    Returns a bounded provenance receipt and the four unchanged release assets.
    """
    require(re.fullmatch(r"[0-9a-f]{40}", expected_sha) is not None and
            sha256(expected_workflow_sha256), "Invalid expected source identity")
    require(re.fullmatch(r"[0-9a-f]{40}", expected_tree) is not None, "Invalid expected Git tree")
    require(re.fullmatch(r"\d+\.\d+\.\d+", version) is not None, "Invalid release version")
    require(now.tzinfo is not None, "Verification time needs a timezone")
    require(source_files.get(WORKFLOW_PATH, {}).get("sha256") == expected_workflow_sha256,
            "Source inventory does not bind the expected workflow")
    repository, workflow, run = snapshot["repository"], snapshot["workflow"], snapshot["run"]
    require(repository.get("full_name") == REPOSITORY and exact_int(repository.get("id")),
            "Repository endpoint identity differs")
    require(workflow.get("path") == WORKFLOW_PATH and workflow.get("name") == WORKFLOW_NAME and
            workflow.get("state") == "active" and exact_int(workflow.get("id")),
            "Workflow endpoint identity differs")
    verify_run(run, workflow, expected_sha, repository["id"], now)
    require(run["id"] != current_run_id, "A run cannot promote itself")
    require(snapshot.get("workflow_sha256") == expected_workflow_sha256 and
            snapshot.get("tree") == expected_tree, "Remote source or workflow differs")
    jobs = snapshot["jobs"]
    require(isinstance(jobs, list) and len(jobs) == len(EXPECTED_JOBS) and
            {job.get("name") for job in jobs} == EXPECTED_JOBS, "Required job set differs")
    require(len({job.get("id") for job in jobs}) == len(jobs), "Duplicate job identity")
    history = snapshot["all_jobs"]
    require(isinstance(history, list) and history and
            len({job.get("id") for job in history}) == len(history), "Missing or duplicate job history")
    by_name = {}
    for job in history:
        require(job.get("name") in EXPECTED_JOBS and exact_int(job.get("id")) and
                job.get("run_id") == run["id"] and job.get("head_sha") == expected_sha and
                exact_int(job.get("run_attempt")) and job["run_attempt"] <= run["run_attempt"],
                "Job history belongs to another commit or attempt")
        attempts = by_name.setdefault(job["name"], {})
        require(job["run_attempt"] not in attempts, "Ambiguous job attempt history")
        attempts[job["run_attempt"]] = job
    require(set(by_name) == EXPECTED_JOBS and
            max(job["run_attempt"] for job in history) == run["run_attempt"],
            "Latest attempt is absent from job history")
    for job in jobs:
        require(exact_int(job.get("id")) and job.get("run_id") == run["id"] and
                exact_int(job.get("run_attempt")) and 1 <= job["run_attempt"] <= run["run_attempt"] and
                job.get("head_sha") == expected_sha and
                job["id"] == by_name[job["name"]][max(by_name[job["name"]])]["id"],
                "Job belongs to another commit or is not the latest effective attempt")
        require(job == by_name[job["name"]][job["run_attempt"]], "Job history revision differs")
        require(job.get("status") == "completed" and job.get("conclusion") == "success",
                "Required job failed, skipped or incomplete")
        require(timestamp(run["created_at"]) <= timestamp(job.get("started_at")) <=
                timestamp(job.get("completed_at")) <= timestamp(run["updated_at"]),
                "Job timestamps differ from the complete attempt")
    if current_run_id:
        current = snapshot["current_run"]
        require(snapshot.get("main_sha") == expected_sha, "Main has moved beyond the expected revision")
        require(current.get("id") == current_run_id and current.get("head_sha") == expected_sha and
                current.get("workflow_id") == workflow["id"] and
                current.get("repository", {}).get("id") == repository["id"] and
                current.get("path") == WORKFLOW_PATH and current.get("event") == "push" and
                current.get("head_branch") == "main" and current.get("status") == "in_progress" and
                current.get("conclusion") is None and
                timestamp(current["run_started_at"]) >= timestamp(run["updated_at"]) and
                exact_int(current.get("check_suite_id")), "Current run identity differs")
    for later in snapshot["runs"]:
        require(later.get("head_sha") == expected_sha and later.get("workflow_id") == workflow["id"],
                "Workflow run listing is inconsistent")
        if later["id"] == run["id"]:
            require(run_revision(later) == run_revision(run), "Candidate changed during verification")
        elif later["id"] != current_run_id and (
                timestamp(later["created_at"]) >= timestamp(run["created_at"]) or
                timestamp(later["updated_at"]) > timestamp(run["updated_at"])):
            require(later.get("status") == "completed" and later.get("conclusion") == "success",
                    "A later same-commit CI run failed or is incomplete")
    require(sum(item.get("id") == run["id"] for item in snapshot["runs"]) == 1,
            "Candidate absent or duplicated in latest workflow runs")
    # Scope is this CI workflow only. Branch protection and unrelated external
    # checks remain platform gates; this verifier needs no checks/statuses scope.

    metadata = snapshot["artifacts"]
    selected = {}
    for name in ARTIFACT_NAMES:
        matches = [item for item in metadata if item.get("name") == name]
        require(len(matches) == 1, "Missing or ambiguous required artifact")
        item = matches[0]
        producer = next(job for job in jobs if job["name"] ==
                        ("Build verified public source" if name == "public-source" else
                         "Complete evidence " + name.removeprefix("complete-")))
        require(exact_int(item.get("id")) and item.get("expired") is False and
                timestamp(item["expires_at"]) > now and
                timestamp(producer["started_at"]) <= timestamp(item["created_at"]) <=
                timestamp(item["updated_at"]) <= timestamp(producer["completed_at"]),
                "Expired, stale or invalid artifact")
        provenance = item.get("workflow_run", {})
        require(provenance.get("id") == run["id"] and provenance.get("head_sha") == expected_sha and
                provenance.get("repository_id") == repository["id"] and
                provenance.get("head_repository_id") == repository["id"], "Artifact provenance differs")
        raw = artifact_payloads[item["id"]]
        require(item.get("digest") == "sha256:" + digest(raw) and item.get("size_in_bytes") == len(raw),
                "Artifact ZIP digest or size differs")
        selected[name] = (item, {key: value[0] for key, value in unpack(raw).items()})
    require(len({item[0]["id"] for item in selected.values()}) == len(selected),
            "Duplicate artifact identity")

    source = selected["public-source"][1]
    prefix = "codepier-" + version
    asset_names = {prefix + suffix for suffix in
                   ("-source-full.zip", "-source-full.manifest.json", "-source.zip", "-source.manifest.json")}
    require(set(source) == asset_names | {"public-check.json", "panel-update-check.json", "source-build.json", "runtime-acceptance.json"},
            "Public-source artifact file set differs")
    assets = {name: source[name] for name in sorted(asset_names)}
    assets_receipt = {name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in assets.items()}
    source_identity = source_inventory_sha256(source_files)
    binding = load_json(source["source-build.json"])
    source_job = next(job for job in jobs if job["name"] == "Build verified public source")
    source_attempt = origin_attempt(source_job, history)
    require(exact_int(binding.get("run_attempt")) and binding["run_attempt"] == source_attempt,
            "Source artifact origin is not the original successful build execution")
    expected_binding = {"schema": 1, "repository": REPOSITORY, "commit": expected_sha,
                        "tree": expected_tree, "run_id": run["id"], "run_attempt": source_attempt,
                        "event": "workflow_dispatch", "workflow_path": WORKFLOW_PATH,
                        "workflow_sha256": expected_workflow_sha256,
                        "source_inventory_sha256": source_identity, "assets": assets_receipt}
    require(binding == expected_binding, "Source-build immutable provenance differs")
    for panel, suffix in ((False, "-source-full"), (True, "-source")):
        name = prefix + suffix
        files = {key: value for key, value in source_files.items()
                 if not panel or key not in PANEL_UPDATE_EXCLUDES}
        verify_bundle(source[name + ".zip"], load_json(source[name + ".manifest.json"]), files, panel=panel)
        checked = load_json(source["panel-update-check.json" if panel else "public-check.json"])
        require(checked.get("passed") is True and checked.get("version") == version and
                checked.get("inventory_sha256") == source_identity and
                checked.get("verified_against_current_source") is True and
                checked.get("sha256") == digest(source[name + ".zip"]) and
                checked.get("bytes") == len(source[name + ".zip"]), "Source archive check differs")
        if panel:
            require(checked.get("panel_updaters_verified") == ["1.13.0", "1.30.5", "current"],
                    "Panel updater compatibility evidence is missing")
    summaries = []
    full_digest = digest(source[prefix + "-source-full.zip"])
    from shared.runtime_qualification import verified_receipt
    require(verified_receipt(load_json(source['runtime-acceptance.json']), commit=expected_sha,
            archive_sha256=full_digest, source_inventory_sha256=source_identity),
            'Mandatory real runtime qualification failed or belongs to different source bytes')
    regression_identity = regression_inventory_sha256(source_files)
    for platform in PLATFORMS:
        platform_files = selected["complete-" + platform][1]
        summary = load_json(platform_files["summary.json"])
        proof = load_json(platform_files["archive-proof.json"])
        coverage = load_json(platform_files["coverage.json"])
        require(summary.get("verified") is True and summary.get("scope") == "complete" and
                summary.get("layout") == RESOURCE_LAYOUT and summary.get("shards") == SHARDS and
                summary.get("coverage_merged") is True, "Platform evidence is not exhaustive with coverage")
        count = summary.get("collected")
        require(exact_int(count) and all(exact_int(value, 0) for value in summary.get("counts", {}).values()) and
                summary.get("counts") ==
                {"passed": count, "failed": 0, "skipped": 0, "missing": 0},
                "Platform contains failed, skipped or missing tests")
        require(sha256(summary.get("collection_sha256")) and
                summary.get("source_inventory_sha256") == regression_identity and
                not any(summary.get(key) for key in ("source_changed_during_run", "coverage_error",
                                                    "duplicate_phases", "unexpected_tests", "module_failures")),
                "Platform source, collection or execution evidence differs")
        require(proof.get("verified") is True and proof.get("commit") == expected_sha and
                proof.get("archive_sha256") == full_digest and
                proof.get("source_inventory_sha256") == source_identity and
                proof.get("public_files") == len(source_files) and proof.get("shards") == SHARDS,
                "Platform public-archive proof differs")
        require(isinstance(coverage.get("files"), dict) and coverage["files"] and
                exact_int(coverage.get("totals", {}).get("num_statements")),
                "Platform coverage measurements are missing")
        summaries.append(summary)
    require(summaries[0] == summaries[1], "Platforms tested different source or collections")
    return {
        "schema": 1, "verified": True, "eligible": True, "mode": "reuse-complete-evidence",
        "repository": REPOSITORY, "repository_id": repository["id"], "commit": expected_sha,
        "tree": expected_tree, "workflow_id": workflow["id"], "workflow_path": WORKFLOW_PATH,
        "workflow_sha256": expected_workflow_sha256, "candidate_run_id": run["id"],
        "candidate_run_attempt": run["run_attempt"], "source_build_attempt": source_attempt,
        "candidate_url": run["html_url"],
        "verified_at": now.isoformat(), "source_inventory_sha256": source_identity,
        "collection_sha256": summaries[0]["collection_sha256"],
        "jobs": [{"id": job["id"], "name": job["name"], "effective_attempt": job["run_attempt"],
                  "origin_attempt": origin_attempt(job, history)}
                 for job in sorted(jobs, key=lambda x: x["name"])],
        "artifacts": {name: {"id": item["id"], "digest": item["digest"], "expires_at": item["expires_at"]}
                      for name, (item, _) in sorted(selected.items())},
        "assets": assets_receipt,
    }, assets


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        return None


class GitHubReader:
    """GET-only reader. Optional existing token stays on the pinned API host."""

    base = "https://api.github.com/repos/" + REPOSITORY

    def __init__(self, token=None):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path, *, archive=False):
        require((path == "" or path.startswith("/")) and not path.startswith("//") and "://" not in path,
                "Invalid GitHub endpoint")
        url = self.base + path
        headers = {
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "CodePier-CI-promotion-verifier",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            if not archive or error.code != 302:
                raise EvidenceError("GitHub evidence unavailable") from error
            location = error.headers.get("Location", "")
            target = urllib.parse.urlsplit(location)
            require(target.scheme == "https" and not target.username and not target.password and
                    target.port in {None, 443} and target.hostname and
                    (target.hostname.endswith(".blob.core.windows.net") or
                     target.hostname.endswith(".actions.githubusercontent.com")),
                    "Artifact redirect host is not trusted")
            response = self.opener.open(urllib.request.Request(location, method="GET"), timeout=30)
        with response:
            limit = MAX_BYTES if archive else MAX_JSON
            raw = response.read(limit + 1)
        require(len(raw) <= limit, "GitHub response exceeds limit")
        return raw if archive else load_json(raw)

    def collection(self, path, key=None):
        items = []
        separator = "&" if "?" in path else "?"
        expected_total = None
        for page in range(1, 21):
            payload = self.get(f"{path}{separator}per_page=100&page={page}")
            batch = payload if key is None else payload[key]
            require(isinstance(batch, list), "Invalid paginated response")
            if key is not None:
                total = payload.get("total_count")
                require(exact_int(total, 0), "Missing pagination count")
                if expected_total is None:
                    expected_total = total
                require(total == expected_total, "Pagination changed during verification")
            items.extend(batch)
            if len(batch) < 100:
                require(expected_total is None or len(items) == expected_total, "Incomplete GitHub listing")
                return items
        raise EvidenceError("GitHub evidence listing exceeds pagination budget")


def live_snapshot(reader, *, expected_sha, candidate_run_id, current_run_id):
    repository = reader.get("")
    workflow = reader.get("/actions/workflows/ci.yml")
    require(workflow.get("path") == WORKFLOW_PATH and exact_int(workflow.get("id")),
            "Pinned workflow endpoint differs")
    run_path = f"/actions/workflows/{workflow['id']}/runs?head_sha={expected_sha}"
    runs = reader.collection(run_path, "workflow_runs")
    if candidate_run_id is None:
        candidates = [run for run in runs if run.get("event") == "workflow_dispatch" and
                      run.get("status") == "completed" and run.get("conclusion") == "success"]
        require(bool(candidates), "No eligible explicit candidate CI run")
        candidate_run_id = max(candidates, key=lambda item: timestamp(item["created_at"]))["id"]
    run = reader.get(f"/actions/runs/{candidate_run_id}")
    require(exact_int(run.get("run_attempt")), "Missing latest candidate attempt")
    jobs = reader.collection(f"/actions/runs/{candidate_run_id}/jobs?filter=latest", "jobs")
    all_jobs = reader.collection(f"/actions/runs/{candidate_run_id}/jobs?filter=all", "jobs")
    artifacts = reader.collection(f"/actions/runs/{candidate_run_id}/artifacts", "artifacts")
    commit = reader.get("/git/commits/" + expected_sha)
    require(commit.get("sha") == expected_sha, "Commit endpoint identity differs")
    workflow_source = reader.get("/contents/" + WORKFLOW_PATH + "?ref=" + expected_sha)
    require(workflow_source.get("encoding") == "base64", "Missing immutable workflow source")
    try:
        raw_workflow = base64.b64decode(workflow_source["content"].replace("\n", ""), validate=True)
    except (ValueError, KeyError) as error:
        raise EvidenceError("Invalid workflow source response") from error
    payloads = {}
    for item in artifacts:
        if item.get("name") in ARTIFACT_NAMES:
            require(exact_int(item.get("id")), "Invalid artifact identity")
            payloads[item["id"]] = reader.get(f"/actions/artifacts/{item['id']}/zip", archive=True)
    snapshot = {
        "repository": repository, "workflow": workflow, "run": run, "jobs": jobs, "all_jobs": all_jobs,
        "artifacts": artifacts, "workflow_sha256": digest(raw_workflow),
        "tree": commit["tree"]["sha"], "runs": runs,
    }
    if current_run_id:
        snapshot["current_run"] = reader.get(f"/actions/runs/{current_run_id}")
        reference = reader.get("/git/ref/heads/main")
        require(reference.get("ref") == "refs/heads/main" and
                reference.get("object", {}).get("type") == "commit", "Main reference is not a commit")
        snapshot["main_sha"] = reference["object"]["sha"]
    # Re-read mutable metadata after all downloads. A new attempt/run invalidates
    # this decision instead of accepting a mixed-time evidence snapshot.
    require(run_revision(reader.get(f"/actions/runs/{candidate_run_id}")) == run_revision(run),
            "Candidate changed while reading evidence")
    require(reader.collection(f"/actions/runs/{candidate_run_id}/artifacts", "artifacts") == artifacts,
            "Artifacts changed while reading evidence")
    require([run_revision(item) for item in reader.collection(run_path, "workflow_runs")
             if item["id"] != current_run_id] ==
            [run_revision(item) for item in runs if item["id"] != current_run_id],
            "Workflow runs changed while reading evidence")
    return snapshot, payloads


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=REPOSITORY, choices=[REPOSITORY])
    parser.add_argument("--sha", required=True)
    parser.add_argument("--candidate-run-id", type=int)
    parser.add_argument("--current-run-id", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assets-output", type=Path)
    parser.add_argument("--token-env", help="Explicit existing read-only token environment variable; default anonymous")
    args = parser.parse_args(argv)
    try:
        from scripts.check_release import check_source
        from scripts.release_acceptance import bind_commit
        report, files = check_source(ROOT)
        bind_commit(ROOT, args.sha, files)
        workflow_hash = digest((ROOT / WORKFLOW_PATH).read_bytes())
        tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()
        token = os.environ[args.token_env] if args.token_env else None
        snapshot, payloads = live_snapshot(GitHubReader(token), expected_sha=args.sha,
                                          candidate_run_id=args.candidate_run_id,
                                          current_run_id=args.current_run_id)
        result, assets = verify_evidence(snapshot, payloads, expected_sha=args.sha,
                                        expected_workflow_sha256=workflow_hash, expected_tree=tree,
                                        source_files=files, version=report["version"],
                                        now=datetime.now(timezone.utc), current_run_id=args.current_run_id)
        bind_commit(ROOT, args.sha, files)
        require(check_source(ROOT)[1] == files, "Source changed during evidence verification")
        if args.assets_output:
            args.assets_output.mkdir(parents=True, exist_ok=False)
            for name, raw in assets.items():
                with (args.assets_output / name).open("xb") as stream:
                    stream.write(raw)
    except Exception as error:
        reason = str(error) if isinstance(error, EvidenceError) else type(error).__name__
        result = {"schema": 1, "verified": False, "eligible": False, "mode": "full-ci", "reason": reason}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
