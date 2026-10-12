#!/usr/bin/env python3
"""Bind image promotion to successful, exact-source build and boot evidence.

Only the read-only discover job reads GitHub Actions. Its bounded job output
is revalidated by consume without an Actions token; registry writes are confined
to promote. Missing evidence always means a full build.
Requires only Python's standard library and the runner's existing gh / Docker.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
import zipfile

WORKFLOW = ".github/workflows/image.yml"
SCHEMA = 1
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_ARTIFACT = 1024 * 1024
MAX_HANDOFF = 32 * 1024
PLATFORMS = ("linux/amd64", "linux/arm64")
RECIPE = {
    "context": "git-full-sha",
    "dockerfile": "Dockerfile",
    "platforms": list(PLATFORMS),
    "provenance": "mode=max",
    "sbom": True,
    "boot": {
        "platform": "linux/amd64", "read_only": True, "tmpfs": "/tmp:size=64m,mode=1777",
        "cap_drop": "ALL", "no_new_privileges": True, "uid": 10001,
        "health": "ok", "agent_manifest": True, "maintenance": False,
    },
}


class EvidenceError(ValueError):
    """Missing, stale, conflicting, or untrusted evidence."""


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def command(*args, timeout=90):
    result = subprocess.run(args, check=True, capture_output=True, timeout=timeout)
    return result.stdout


def text_command(*args):
    return command(*args).decode().strip()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def output(**values):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            for key, value in values.items():
                require("\n" not in str(value), "Invalid multiline output")
                stream.write(f"{key}={value}\n")


def identity(version):
    sha = os.environ["GITHUB_SHA"]
    repository = os.environ["GITHUB_REPOSITORY"]
    require(SHA.fullmatch(sha), "A full 40-character source SHA is required")
    require(re.fullmatch(r"[\w.-]+/[\w.-]+", repository), "Invalid repository")
    require(re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version), "Invalid source version")
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        origin = run_origin()
        ref = origin["ref"]
        branch = ref.removeprefix("refs/heads/")
        require(trusted_ref(origin["event"], branch, version) == ref
                and origin["forced"] is False and origin["inputs"] == {},
                "Only an empty-input version-matching release candidate dispatch may publish")
    require(text_command("git", "rev-parse", "HEAD") == sha, "Checkout is not the event SHA")
    require(not command("git", "diff", "--name-only", "HEAD"), "Tracked source changed before build")
    tree = text_command("git", "rev-parse", "HEAD^{tree}")
    require(SHA.fullmatch(tree), "Invalid source tree")
    return {
        "schema": SCHEMA, "repository": repository, "source_sha": sha, "source_tree": tree,
        "version": version, "workflow": WORKFLOW,
        "workflow_sha256": hashlib.sha256(Path(WORKFLOW).read_bytes()).hexdigest(),
        "recipe": RECIPE,
        "context": f"https://github.com/{repository}.git#{sha}",
        "image": f"ghcr.io/{repository.lower()}",
    }


def candidate_branch(branch, version):
    """Only a version-matching release branch, never a PR/topic or arbitrary ref."""
    return isinstance(branch, str) and len(branch) <= 128 and re.fullmatch(
        r"release/v" + re.escape(version) + r"-[a-z0-9]+(?:-[a-z0-9]+)*", branch) is not None


def trusted_ref(event, branch, version):
    if event == "push":
        if branch == "main":
            return "refs/heads/main"
        if branch == "v" + version:
            return "refs/tags/" + branch
    elif event == "workflow_dispatch" and candidate_branch(branch, version):
        return "refs/heads/" + branch
    return None


def run_origin():
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    inputs = payload.get("inputs")
    return {
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "event": os.environ["GITHUB_EVENT_NAME"],
        "ref": os.environ["GITHUB_REF"],
        "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
        "forced": payload.get("forced", False),
        "inputs": {} if inputs is None else inputs,
    }


def eligible_run(run, expected, current_run_id):
    repository = expected["repository"]
    return (
        type(run.get("id")) is int and run["id"] != current_run_id
        and type(run.get("run_attempt")) is int and run["run_attempt"] >= 1
        and run.get("status") == "completed" and run.get("conclusion") == "success"
        and run.get("head_sha") == expected["source_sha"] and run.get("path") == WORKFLOW
        and trusted_ref(run.get("event"), run.get("head_branch"), expected["version"]) is not None
        and run.get("repository", {}).get("full_name") == repository
        and run.get("head_repository", {}).get("full_name") == repository
        and type(run.get("repository", {}).get("id")) is int
        and run["repository"]["id"] > 0
        and run["repository"]["id"] == run.get("head_repository", {}).get("id")
    )


def validate_proof(proof, expected, run=None):
    require(isinstance(proof, dict), "Proof is not an object")
    require(proof.get("identity") == expected, "Source, workflow, version or build inputs differ")
    require(DIGEST.fullmatch(str(proof.get("digest", ""))), "Invalid image digest")
    require(DIGEST.fullmatch(str(proof.get("tested_amd64_config", ""))), "Missing boot-tested config")
    require(proof.get("boot_verified") is True, "Real hardened amd64 boot did not pass")
    require(proof.get("registry", {}).get("digest") == proof["digest"], "Index digest differs")
    require(proof["registry"].get("amd64_config") == proof["tested_amd64_config"],
            "Published amd64 is not the boot-tested image")
    require(set(proof["registry"].get("platforms", {})) == set(PLATFORMS), "Missing target platform")
    require(proof["registry"].get("attestations_verified") is True, "Missing SBOM / provenance")
    platforms = proof["registry"]["platforms"]
    attestations = proof["registry"].get("attestations")
    require(all(DIGEST.fullmatch(str(value)) for value in platforms.values())
            and len(set(platforms.values())) == len(PLATFORMS)
            and isinstance(attestations, dict) and set(attestations) == set(platforms.values())
            and all(DIGEST.fullmatch(str(value)) for value in attestations.values()),
            "Attestation descriptors do not bind both runtime manifests")
    origin = proof.get("origin", {})
    event, ref = origin.get("event"), origin.get("ref")
    require(origin.get("forced") is False, "Forced or ambiguous origin cannot supply reusable proof")
    require(isinstance(ref, str), "Missing origin ref")
    if event == "workflow_dispatch":
        require(origin.get("inputs") == {}, "Candidate dispatch inputs must be explicitly empty")
        branch = ref.removeprefix("refs/heads/")
    else:
        branch = "main" if ref == "refs/heads/main" else ref.removeprefix("refs/tags/")
    require(trusted_ref(event, branch, expected["version"]) == ref, "Untrusted build ref")
    require(origin.get("workflow_sha") == expected["source_sha"], "Workflow source SHA differs")
    if run is not None:
        require(origin.get("run_id") == run["id"] and origin.get("run_attempt") == run["run_attempt"],
                "Proof belongs to a different run or attempt")
        expected_ref = trusted_ref(run.get("event"), run.get("head_branch"), expected["version"])
        require(origin["event"] == run.get("event") and expected_ref is not None
                and origin["ref"] == expected_ref, "Proof ref/event does not match successful run")
    return proof


def api(path):
    require(path.startswith("repos/"), "Only repository API requests are supported")
    return command("gh", "api", "--method", "GET", path, timeout=60)


def api_json(path):
    return json.loads(api(path))


def proof_from_archive(data):
    require(len(data) <= MAX_ARTIFACT, "Image proof archive too large")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        require(len(entries) == 1 and entries[0].filename == "proof.json", "Unexpected artifact members")
        entry = entries[0]
        require(entry.file_size <= MAX_ARTIFACT and not entry.flag_bits & 1, "Invalid proof member")
        return json.loads(archive.read(entry))


def inspect(reference, raw=False):
    options = ("--raw",) if raw else ("--format", "{{json .Manifest}}")
    return json.loads(command("docker", "buildx", "imagetools", "inspect", reference, *options))


def registry_evidence(image, digest):
    require(DIGEST.fullmatch(digest), "Invalid registry digest")
    index = inspect(f"{image}@{digest}")
    require(index.get("digest") == digest, "Registry returned a different index")
    require(index.get("mediaType") in (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ), "Image is not a multi-platform index")
    platforms, attestations = {}, {}
    for descriptor in index.get("manifests", []):
        require(DIGEST.fullmatch(str(descriptor.get("digest", ""))), "Invalid child digest")
        platform = descriptor.get("platform", {})
        key = platform.get("os", "") + "/" + platform.get("architecture", "")
        annotations = descriptor.get("annotations", {})
        if key in PLATFORMS:
            require(key not in platforms, "Duplicate target platform")
            platforms[key] = descriptor["digest"]
        elif (key == "unknown/unknown"
              and annotations.get("vnd.docker.reference.type") == "attestation-manifest"):
            target = annotations.get("vnd.docker.reference.digest")
            require(target not in attestations, "Duplicate attestation")
            attestations[target] = descriptor["digest"]
        else:
            raise EvidenceError("Unexpected platform or unrecognized attestation")
    require(set(platforms) == set(PLATFORMS), "Both linux/amd64 and linux/arm64 are required")
    require(set(attestations) == set(platforms.values()), "Missing architecture attestation")
    for runtime_digest in platforms.values():
        manifest = inspect(f"{image}@{attestations[runtime_digest]}", raw=True)
        layers = manifest.get("layers", [])
        require(layers and all(DIGEST.fullmatch(str(layer.get("digest", ""))) for layer in layers),
                "Invalid attestation layer")
        predicates = {layer.get("annotations", {}).get("in-toto.io/predicate-type") for layer in layers}
        require("https://spdx.dev/Document" in predicates, "Missing SPDX SBOM")
        require(bool(predicates & {"https://slsa.dev/provenance/v0.2", "https://slsa.dev/provenance/v1"}),
                "Missing SLSA provenance")
    amd64 = inspect(f"{image}@{platforms['linux/amd64']}", raw=True)
    config = amd64.get("config", {}).get("digest", "")
    require(DIGEST.fullmatch(config), "Missing amd64 image config")
    return {
        "digest": digest, "platforms": platforms, "amd64_config": config,
        "attestations": attestations, "attestations_verified": True,
    }


def boot_published(image, digest, version, config):
    """If exporters changed image configuration, boot the exact published image."""
    reference = f"{image}@{digest}"
    name = "candidate-published"
    command("docker", "pull", "--platform", "linux/amd64", reference, timeout=180)
    actual_config = text_command("docker", "image", "inspect", "--format", "{{.Id}}", reference)
    require(actual_config == config, "Pulled amd64 config does not match published manifest")
    try:
        command("docker", "run", "--detach", "--name", name, "--platform", "linux/amd64",
                "--read-only", "--tmpfs", "/tmp:size=64m,mode=1777", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--publish", "127.0.0.1:8765:8765",
                "--env", "HUB_PUBLIC_URL=http://127.0.0.1:8765", reference)
        deadline = time.monotonic() + 60
        health = None
        while time.monotonic() < deadline:
            try:
                with urlopen("http://127.0.0.1:8765/healthz", timeout=3) as response:
                    health = json.load(response)
                break
            except (URLError, OSError):
                time.sleep(2)
        require(isinstance(health, dict) and health.get("status") == "ok"
                and health.get("version") == version, "Published image failed version/health boot gate")
        require(health.get("panel_update") == {"maintenance": False, "ready": False},
                "Published image has unexpected maintenance state")
        with urlopen("http://127.0.0.1:8765/agent/manifest.json", timeout=5) as response:
            require(response.status == 200, "Published agent manifest unavailable")
        require(text_command("docker", "exec", name, "id", "-u") == "10001",
                "Published image is not running as UID 10001")
    finally:
        # Cleanup never changes the boot outcome, matching the original gate.
        try:
            logs = command("docker", "logs", name).decode(errors="replace")
            print("\n".join(logs.splitlines()[-100:]))
        except subprocess.SubprocessError:
            pass
        try:
            command("docker", "rm", "--force", "--volumes", name)
        except subprocess.SubprocessError:
            pass


FULL_BUILD_STEPS = frozenset({
    "Release inputs must agree with source VERSION (host Python, standard library only)",
    "Bind build context to the complete immutable source commit",
    "Build the linux/amd64 candidate into the local daemon",
    "Candidate must boot healthy from an empty data directory under the Compose hardening",
    "Build linux/amd64 and linux/arm64 once; publish a run-specific candidate",
    "Verify both manifests and attestations bind to the real boot-tested amd64 image",
    "Promote only the tested digest; never repoint VERSION or full-SHA tags",
    "Retain immutable build and hardened-boot proof",
})


def verify_producer_jobs(payload, run, expected):
    jobs = payload.get("jobs", [])
    require(payload.get("total_count") == len(jobs) == 2, "Incomplete image job evidence")
    require({job.get("name") for job in jobs} == {
        "Verify reusable image evidence (read-only)", "Build, boot-test and publish"},
        "Image workflow jobs differ")
    for job in jobs:
        require(job.get("run_id") == run["id"] and job.get("head_sha") == expected["source_sha"]
                and type(job.get("run_attempt")) is int
                and 1 <= job["run_attempt"] <= run["run_attempt"]
                and job.get("status") == "completed" and job.get("conclusion") == "success",
                "Image producer job was skipped, failed or belongs to another source")
    producer = next(job for job in jobs if job["name"] == "Build, boot-test and publish")
    require(producer["run_attempt"] == run["run_attempt"], "Image proof is not from latest producer attempt")
    for name in FULL_BUILD_STEPS:
        steps = [step for step in producer.get("steps", []) if step.get("name") == name]
        require(len(steps) == 1 and steps[0].get("status") == "completed"
                and steps[0].get("conclusion") == "success", "Required image build/boot/proof step did not pass")


def discover(expected, current_run_id):
    """A failed lookup never authorizes skipping the normal build."""
    base = f"repos/{expected['repository']}"
    query = f"head_sha={expected['source_sha']}&status=success&per_page=100"
    result = api_json(f"{base}/actions/workflows/image.yml/runs?{query}")
    require(result.get("total_count", 0) <= 100, "More candidate runs than bounded lookup supports")
    for run in result.get("workflow_runs", []):
        if not eligible_run(run, expected, current_run_id):
            continue
        try:
            name = f"image-proof-{expected['source_sha']}-{run['id']}-{run['run_attempt']}"
            artifacts = api_json(f"{base}/actions/runs/{run['id']}/artifacts?per_page=100")
            require(artifacts.get("total_count", 0) <= 100, "Artifact list is incomplete")
            matches = [item for item in artifacts.get("artifacts", []) if item.get("name") == name]
            require(len(matches) == 1, "No unique immutable proof artifact")
            artifact = matches[0]
            require(artifact.get("expired") is False, "Image proof expired")
            require(0 < artifact.get("size_in_bytes", 0) <= MAX_ARTIFACT, "Invalid proof size")
            artifact_run = artifact.get("workflow_run", {})
            require(artifact_run.get("id") == run["id"]
                    and artifact_run.get("head_sha") == expected["source_sha"]
                    and artifact_run.get("repository_id") == run["repository"]["id"]
                    and artifact_run.get("head_repository_id") == run["repository"]["id"],
                    "Artifact source run does not match")
            archive = api(f"{base}/actions/artifacts/{int(artifact['id'])}/zip")
            checksum = artifact.get("digest")
            require(checksum == "sha256:" + hashlib.sha256(archive).hexdigest()
                    and len(archive) == artifact["size_in_bytes"],
                    "Artifact archive digest or size missing or mismatched")
            proof = validate_proof(proof_from_archive(archive), expected, run)
            jobs = api_json(f"{base}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
            verify_producer_jobs(jobs, run, expected)
            current = api_json(f"{base}/actions/runs/{run['id']}")
            revision = ("id", "run_attempt", "status", "conclusion", "event", "head_sha",
                        "path", "head_branch", "workflow_id", "updated_at", "repository", "head_repository")
            require(all(current.get(key) == run.get(key) for key in revision),
                    "Image source run changed during verification")
            return proof
        except (EvidenceError, AttributeError, KeyError, TypeError, ValueError, RecursionError, OSError,
                subprocess.SubprocessError, zipfile.BadZipFile) as error:
            print(f"Run {run['id']} cannot supply reusable image proof: {type(error).__name__}")
    return None


def handoff_target(expected):
    """Bind the job output to this workflow run/attempt as well as its source."""
    return {
        "repository": expected["repository"], "source_sha": expected["source_sha"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
    }


def encode_handoff(proof, expected):
    validate_proof(proof, expected)
    value = json.dumps({"schema": SCHEMA, "verified_for": handoff_target(expected), "proof": proof},
                       separators=(",", ":"), sort_keys=True)
    require(len(value.encode("utf-8")) <= MAX_HANDOFF, "Proof job output is too large")
    return value


def consume_handoff(expected, value, result):
    """Consume only a successful dependency job's output; never call Actions."""
    require(result == "success", "Read-only verification job did not succeed")
    require(value and len(value.encode("utf-8")) <= MAX_HANDOFF, "Proof job output missing or too large")
    envelope = json.loads(value)
    require(envelope.get("schema") == SCHEMA, "Unsupported proof handoff schema")
    require(envelope.get("verified_for") == handoff_target(expected), "Proof handoff run/attempt differs")
    proof = validate_proof(envelope.get("proof"), expected)
    require(registry_evidence(expected["image"], proof["digest"]) == proof["registry"],
            "Published manifest no longer matches verified proof")
    return proof


def resolve(reference):
    try:
        result = inspect(reference)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode(errors="replace").lower()
        # Authentication, transport, rate limiting, and ambiguous failures are
        # NOT evidence that a tag is absent.
        require(not re.search(r"unauthorized|denied|forbidden|401|403|429|timeout|connection", detail),
                "Cannot establish registry tag state")
        absent = "manifest unknown" in detail or "manifest_unknown" in detail
        absent = absent or bool(re.search(re.escape(reference.lower()) + r'["\x27]?: not found', detail))
        require(absent, "Registry tag lookup failed ambiguously")
        return None
    digest = result.get("digest", "")
    require(DIGEST.fullmatch(digest), "Registry tag has no valid digest")
    return digest


def promotion_tags(expected, tags):
    image = expected["image"]
    full_sha_tag = f"{image}:sha-{expected['source_sha']}"
    values = list(dict.fromkeys(tags))
    require(full_sha_tag in values, "Full-SHA tag is required")
    for tag in values:
        require(tag.startswith(image + ":") and re.fullmatch(r"[\w][\w.-]{0,127}", tag[len(image) + 1:]),
                "Promotion tag is outside the expected image")
    immutable = {full_sha_tag, f"{image}:{expected['version']}"}
    return values, immutable


def promote(expected, proof, tags):
    require(proof.get("boot_verified") is True
            and proof.get("tested_amd64_config") == proof.get("registry", {}).get("amd64_config"),
            "Missing successful boot-to-published-image binding")
    require(proof.get("identity") == expected, "Promotion source identity differs")
    digest = proof["digest"]
    require(registry_evidence(expected["image"], digest) == proof["registry"], "Promotion proof differs")
    tags, immutable = promotion_tags(expected, tags)
    states = {tag: resolve(tag) for tag in tags}
    for tag in immutable.intersection(tags):
        require(states[tag] in (None, digest), f"Refusing to repoint existing immutable tag {tag}")
    # One existing index, no new annotations/platform filters: carbon copy keeps
    # both runtime manifests and their original SBOM/provenance descriptors.
    missing = [tag for tag in tags if states[tag] != digest]
    if missing:
        args = ["docker", "buildx", "imagetools", "create", "--prefer-index=false"]
        for tag in missing:
            args.extend(("--tag", tag))
        args.append(f"{expected['image']}@{digest}")
        command(*args, timeout=180)
    for tag in tags:
        require(resolve(tag) == digest, f"Promoted tag does not resolve to tested digest: {tag}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("identity", "discover", "consume", "record", "guard-build", "promote"))
    parser.add_argument("--identity", default="dist/image/identity.json")
    parser.add_argument("--proof", default="dist/image/proof.json")
    parser.add_argument("--version")
    parser.add_argument("--digest")
    parser.add_argument("--tested-config")
    args = parser.parse_args(argv)
    if args.action == "identity":
        expected = identity(args.version)
        write_json(args.identity, expected)
        build_tag = (f"{expected['image']}:build-{expected['source_sha']}-"
                     f"{int(os.environ['GITHUB_RUN_ID'])}-{int(os.environ['GITHUB_RUN_ATTEMPT'])}")
        output(context=expected["context"], image=expected["image"], build_tag=build_tag)
        return 0
    expected = read_json(args.identity)
    if args.action in ("discover", "consume"):
        proof, encoded = None, ""
        try:
            if args.action == "discover":
                proof = discover(expected, int(os.environ["GITHUB_RUN_ID"]))
                if proof:
                    encoded = encode_handoff(proof, expected)
            else:
                proof = consume_handoff(expected, os.environ.get("PROMOTION_PROOF", ""),
                                        os.environ.get("PROMOTION_RESULT", ""))
        except (EvidenceError, AttributeError, KeyError, TypeError, ValueError, RecursionError, OSError,
                subprocess.SubprocessError, zipfile.BadZipFile) as error:
            print(f"Image proof unavailable ({type(error).__name__}); full build required")
            proof, encoded = None, ""
        if args.action == "discover":
            # No transfer artifact or token is needed by the publishing job.
            # GitHub suppressing/truncating this output safely causes a rebuild.
            output(proof=encoded)
        else:
            output(reused=str(proof is not None).lower(), digest=proof["digest"] if proof else "")
        if proof:
            write_json(args.proof, proof)
            print(f"Verified successful run {proof['origin']['run_id']}, digest {proof['digest']}")
        else:
            print("No trusted image proof found; building and boot-testing both target manifests")
    elif args.action == "guard-build":
        tag = (f"{expected['image']}:build-{expected['source_sha']}-"
               f"{int(os.environ['GITHUB_RUN_ID'])}-{int(os.environ['GITHUB_RUN_ATTEMPT'])}")
        require(resolve(tag) is None, "This run attempt already published; do not replay an uncertain build")
    elif args.action == "record":
        registry = registry_evidence(expected["image"], args.digest)
        require(DIGEST.fullmatch(str(args.tested_config)), "Missing original successful boot config")
        boot_mode = "candidate-config-match"
        if registry["amd64_config"] != args.tested_config:
            print("Published config differs; running the same hardening gate on its exact digest")
            boot_published(expected["image"], args.digest, expected["version"], registry["amd64_config"])
            boot_mode = "published-digest"
        proof = {"identity": expected, "origin": run_origin(), "digest": args.digest,
                 "registry": registry, "tested_amd64_config": registry["amd64_config"],
                 "boot_verified": True, "boot_mode": boot_mode}
        # Only version-matching release dispatches with empty inputs and fully
        # successful producer jobs may later become main/tag promotion evidence.
        write_json(args.proof, proof)
    elif args.action == "promote":
        proof = read_json(args.proof)
        require(proof.get("boot_verified") is True
                and proof.get("tested_amd64_config") == proof.get("registry", {}).get("amd64_config"),
                "Missing successful boot-to-published-image binding")
        promote(expected, proof, os.environ["IMAGE_TAGS"].split())
        output(digest=proof["digest"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvidenceError, AttributeError, KeyError, TypeError, ValueError, RecursionError, OSError, subprocess.SubprocessError) as exc:
        print(f"Image promotion failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
