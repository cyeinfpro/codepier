"""Offline image promotion contracts; never contact GitHub or a registry."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile
import pytest
from scripts import image_promotion as m

SHA = "a" * 40
IMAGE = "ghcr.io/example/codepier"

def digest(char):
    return "sha256:" + char * 64

@pytest.fixture
def expected():
    return {"schema": 1, "repository": "example/codepier", "source_sha": SHA,
            "source_tree": "b" * 40, "version": "1.27.3", "workflow": m.WORKFLOW,
            "workflow_sha256": "c" * 64, "recipe": deepcopy(m.RECIPE),
            "context": "https://github.com/example/codepier.git#" + SHA, "image": IMAGE}

@pytest.fixture
def run(expected):
    return {"id": 17, "run_attempt": 2, "status": "completed", "conclusion": "success",
            "event": "push", "head_sha": SHA, "path": m.WORKFLOW, "head_branch": "main",
            "repository": {"full_name": expected["repository"]},
            "head_repository": {"full_name": expected["repository"]}}

@pytest.fixture
def proof(expected):
    registry = {"digest": digest("1"), "platforms": {"linux/amd64": digest("2"), "linux/arm64": digest("3")},
                "amd64_config": digest("4"), "attestations": {digest("2"): digest("5"), digest("3"): digest("6")},
                "attestations_verified": True}
    return {"identity": deepcopy(expected), "origin": {"run_id": 17, "run_attempt": 2,
            "event": "push", "ref": "refs/heads/main", "workflow_sha": SHA, "forced": False},
            "digest": digest("1"), "registry": registry, "tested_amd64_config": digest("4"),
            "boot_verified": True}

def archive(proof, names=("proof.json",)):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        for name in names:
            output.writestr(name, json.dumps(proof))
    return stream.getvalue()

def setup_discovery(monkeypatch, expected, run, proof):
    data = archive(proof)
    artifact = {"id": 88, "name": f"image-proof-{SHA}-17-2", "expired": False,
                "size_in_bytes": len(data), "workflow_run": {"id": 17, "head_sha": SHA},
                "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
    responses = [{"total_count": 1, "workflow_runs": [run]},
                 {"total_count": 1, "artifacts": [artifact]}]
    calls = []
    def api_json(path):
        calls.append(path)
        return responses[len(calls) - 1]
    monkeypatch.setattr(m, "api_json", api_json)
    monkeypatch.setattr(m, "api", lambda path: data)
    monkeypatch.setattr(m, "registry_evidence", lambda image, value: proof["registry"])
    return artifact, responses, calls

def test_discovers_successful_exact_source(monkeypatch, expected, run, proof):
    _, _, calls = setup_discovery(monkeypatch, expected, run, proof)
    assert m.discover(expected, 18) == proof
    assert calls[0].endswith(f"head_sha={SHA}&event=push&status=success&per_page=100")
    assert m.validate_proof(proof, expected, run) == proof

@pytest.mark.parametrize(("key", "value"), [
    ("status", "in_progress"), ("conclusion", "failure"), ("conclusion", "cancelled"),
    ("event", "pull_request"), ("event", "workflow_dispatch"), ("head_sha", "a" * 7),
    ("head_sha", "d" * 40), ("path", ".github/workflows/untrusted.yml"), ("head_branch", "topic"),
    ("repository", {"full_name": "other/codepier"}), ("head_repository", {"full_name": "fork/codepier"}),
    ("run_attempt", 0), ("id", 18),
])
def test_ineligible_runs_never_supply_proof(expected, run, key, value):
    run[key] = value
    assert not m.eligible_run(run, expected, 18)

@pytest.mark.parametrize("field", ["source_sha", "source_tree", "version", "workflow_sha256", "context", "recipe"])
def test_source_input_mismatch_rejected(expected, run, proof, field):
    proof["identity"][field] = "changed"
    with pytest.raises(m.EvidenceError, match="inputs differ"):
        m.validate_proof(proof, expected, run)

@pytest.mark.parametrize(("section", "field", "value"), [
    ("origin", "run_attempt", 1), ("origin", "run_id", 16),
    ("origin", "event", "workflow_dispatch"), ("origin", "forced", True),
    ("origin", "ref", "refs/heads/topic"), ("origin", "workflow_sha", "d" * 40),
    ("registry", "digest", digest("9")), ("registry", "amd64_config", digest("9")),
    ("registry", "attestations_verified", False), ("registry", "platforms", {"linux/amd64": digest("2")}),
])
def test_invalid_proof_never_reuses(expected, run, proof, section, field, value):
    proof[section][field] = value
    with pytest.raises(m.EvidenceError):
        m.validate_proof(proof, expected, run)

def test_boot_must_really_pass(expected, run, proof):
    proof["boot_verified"] = False
    with pytest.raises(m.EvidenceError, match="boot"):
        m.validate_proof(proof, expected, run)

@pytest.mark.parametrize(("field", "value"), [
    ("expired", True), ("digest", None), ("digest", digest("9")), ("size_in_bytes", 2 * m.MAX_ARTIFACT),
    ("workflow_run", {"id": 16, "head_sha": SHA}),
    ("workflow_run", {"id": 17, "head_sha": "d" * 40}), ("name", "image-proof-stale"),
])
def test_bad_artifact_falls_back(monkeypatch, expected, run, proof, field, value):
    item, _, _ = setup_discovery(monkeypatch, expected, run, proof)
    item[field] = value
    assert m.discover(expected, 18) is None

def test_duplicate_artifact_falls_back(monkeypatch, expected, run, proof):
    artifact, responses, _ = setup_discovery(monkeypatch, expected, run, proof)
    responses[1]["artifacts"].append(artifact)
    assert m.discover(expected, 18) is None

def test_discovery_job_does_not_need_registry_permissions(monkeypatch, expected, run, proof):
    setup_discovery(monkeypatch, expected, run, proof)
    monkeypatch.setattr(m, "registry_evidence", lambda *args: pytest.fail("Read-only Actions job used registry"))
    assert m.discover(expected, 18) == proof

@pytest.mark.parametrize("names", [("../proof.json",), ("proof.json", "extra.json"), ("other.json",)])
def test_archive_never_extracts_unexpected_paths(proof, names):
    with pytest.raises(m.EvidenceError):
        m.proof_from_archive(archive(proof, names))

def test_current_run_cannot_reuse_itself(monkeypatch, expected, run, proof):
    setup_discovery(monkeypatch, expected, run, proof)
    assert m.discover(expected, 17) is None

def test_tag_first_main_second_can_reuse(expected, run, proof):
    run["head_branch"] = "v1.27.3"
    proof["origin"]["ref"] = "refs/tags/v1.27.3"
    assert m.eligible_run(run, expected, 18)
    assert m.validate_proof(proof, expected, run) == proof

def test_registry_requires_both_platforms_and_attestations(monkeypatch):
    descriptors = []
    for platform, runtime, attest in (("amd64", "2", "5"), ("arm64", "3", "6")):
        descriptors.extend([
            {"digest": digest(runtime), "platform": {"os": "linux", "architecture": platform}},
            {"digest": digest(attest), "platform": {"os": "unknown", "architecture": "unknown"},
             "annotations": {"vnd.docker.reference.type": "attestation-manifest",
                             "vnd.docker.reference.digest": digest(runtime)}},
        ])
    index = {"mediaType": "application/vnd.oci.image.index.v1+json", "digest": digest("1"),
             "manifests": descriptors}
    attestation = {"layers": [
        {"digest": digest("7"), "annotations": {"in-toto.io/predicate-type": "https://spdx.dev/Document"}},
        {"digest": digest("8"), "annotations": {"in-toto.io/predicate-type": "https://slsa.dev/provenance/v1"}},
    ]}
    def inspect(reference, raw=False):
        if reference.endswith(digest("1")):
            return index
        if reference.endswith((digest("5"), digest("6"))):
            return attestation
        return {"config": {"digest": digest("4")}}
    monkeypatch.setattr(m, "inspect", inspect)
    assert m.registry_evidence(IMAGE, digest("1"))["amd64_config"] == digest("4")
    attestation["layers"].pop()
    with pytest.raises(m.EvidenceError, match="SLSA"):
        m.registry_evidence(IMAGE, digest("1"))
    descriptors.pop()
    with pytest.raises(m.EvidenceError, match="attestation"):
        m.registry_evidence(IMAGE, digest("1"))
    descriptors.pop()
    with pytest.raises(m.EvidenceError, match="Both"):
        m.registry_evidence(IMAGE, digest("1"))

@pytest.mark.parametrize("detail", [
    b"401 unauthorized: manifest unknown", b"403 forbidden", b"connection reset",
    b"lookup ghcr.io: no such host", b"unexpected EOF", b"429 too many requests",
])
def test_unknown_registry_state_is_not_absence(monkeypatch, detail):
    def inspect(*args):
        raise subprocess.CalledProcessError(1, "docker", stderr=detail)
    monkeypatch.setattr(m, "inspect", inspect)
    with pytest.raises(m.EvidenceError):
        m.resolve(IMAGE + ":1.27.3")

def test_explicit_missing_registry_manifest_is_absence(monkeypatch):
    def inspect(*args):
        raise subprocess.CalledProcessError(1, "docker", stderr=(IMAGE + ":1.27.3: not found").encode())
    monkeypatch.setattr(m, "inspect", inspect)
    assert m.resolve(IMAGE + ":1.27.3") is None

@pytest.mark.parametrize("conflict", ["version", "sha"])
def test_immutable_tag_conflict_prevents_every_write(monkeypatch, expected, proof, conflict):
    sha_tag = IMAGE + ":sha-" + SHA
    version_tag = IMAGE + ":1.27.3"
    conflicting = version_tag if conflict == "version" else sha_tag
    monkeypatch.setattr(m, "registry_evidence", lambda *args: proof["registry"])
    monkeypatch.setattr(m, "resolve", lambda tag: digest("9") if tag == conflicting else None)
    writes = []
    monkeypatch.setattr(m, "command", lambda *args, **kwargs: writes.append(args))
    with pytest.raises(m.EvidenceError, match="Refusing to repoint"):
        m.promote(expected, proof, [IMAGE + ":latest", version_tag, sha_tag])
    assert not writes

def test_main_does_not_check_unwritten_release_version(monkeypatch, expected, proof):
    tags = [IMAGE + ":main", IMAGE + ":sha-" + SHA]
    monkeypatch.setattr(m, "registry_evidence", lambda *args: proof["registry"])
    reads = []
    def resolve(tag):
        reads.append(tag)
        assert tag != IMAGE + ":1.27.3"
        return proof["digest"]
    monkeypatch.setattr(m, "resolve", resolve)
    monkeypatch.setattr(m, "command", lambda *args, **kwargs: pytest.fail("No-op promotion wrote registry"))
    m.promote(expected, proof, tags)
    assert reads == tags + tags

def test_promote_carbon_copies_one_index_without_rebuilding(monkeypatch, expected, proof):
    tags = [IMAGE + ":1.27.3", IMAGE + ":sha-" + SHA, IMAGE + ":latest"]
    monkeypatch.setattr(m, "registry_evidence", lambda *args: proof["registry"])
    states = {tag: None for tag in tags}
    monkeypatch.setattr(m, "resolve", lambda tag: states[tag])
    writes = []
    def command(*args, **kwargs):
        writes.append(args)
        states.update(dict.fromkeys(tags, proof["digest"]))
        return b""
    monkeypatch.setattr(m, "command", command)
    m.promote(expected, proof, tags)
    assert len(writes) == 1
    assert writes[0][:5] == ("docker", "buildx", "imagetools", "create", "--prefer-index=false")
    assert writes[0][-1] == IMAGE + "@" + proof["digest"]
    assert "--annotation" not in writes[0] and "--platform" not in writes[0]

def test_discovery_api_failure_explicitly_requires_full_build(monkeypatch, tmp_path, expected, capsys):
    identity = tmp_path / "identity.json"
    m.write_json(identity, expected)
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_RUN_ID", "18")
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    def discover(*args):
        raise subprocess.CalledProcessError(1, "gh")
    monkeypatch.setattr(m, "discover", discover)
    assert m.main(["discover", "--identity", str(identity)]) == 0
    assert outputs.read_text() == "proof=\n"
    assert "full build required" in capsys.readouterr().out

def test_registry_binding_fails_before_recording_success(monkeypatch, tmp_path, expected, proof):
    identity = tmp_path / "identity.json"
    destination = tmp_path / "proof.json"
    m.write_json(identity, expected)
    monkeypatch.setattr(m, "registry_evidence", lambda *args: proof["registry"])
    def boot(*args):
        raise m.EvidenceError("Published image failed boot-tested gate")
    monkeypatch.setattr(m, "boot_published", boot)
    with pytest.raises(m.EvidenceError, match="boot-tested"):
        m.main(["record", "--identity", str(identity), "--proof", str(destination),
                "--digest", digest("1"), "--tested-config", digest("9")])
    assert not destination.exists()

def test_workflow_preserves_real_boot_and_exact_source_fallback():
    workflow = Path(m.WORKFLOW).read_text()
    assert "group: image-" + "$" + "{{ github.workflow }}-" + "$" + "{{ github.sha }}" in workflow
    assert "cancel-in-progress: true" not in workflow
    assert "actions: write" not in workflow
    assert "packages: write" in workflow
    assert "type=sha,prefix=sha-,format=long" in workflow
    assert workflow.count("context: " + "$" + "{{ steps.identity.outputs.context }}") == 2
    assert "provenance: mode=max" in workflow and "sbom: true" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "--read-only --tmpfs /tmp:size=64m,mode=1777" in workflow
    assert "--cap-drop ALL --security-opt no-new-privileges:true" in workflow
    assert '"$(docker exec candidate id -u)" == 10001' in workflow
    assert "/agent/manifest.json" in workflow
    assert "image-proof-" + "$" + "{{ github.sha }}-" + "$" + "{{ github.run_id }}-" + "$" + "{{ github.run_attempt }}" in workflow
    assert "steps.reuse.outputs.reused != 'true'" in workflow
    assert workflow.index("id: boot") < workflow.index("id: publish") < workflow.index("id: promote")


@pytest.mark.parametrize("changed", [False, True])
def test_record_config_change_requires_exact_digest_boot(monkeypatch, tmp_path, expected, proof, changed):
    identity = tmp_path / "identity.json"
    destination = tmp_path / "proof.json"
    m.write_json(identity, expected)
    monkeypatch.setattr(m, "registry_evidence", lambda *args: proof["registry"])
    monkeypatch.setattr(m, "run_origin", lambda: proof["origin"])
    boots = []
    monkeypatch.setattr(m, "boot_published", lambda *args: boots.append(args))
    config = digest("9") if changed else digest("4")
    m.main(["record", "--identity", str(identity), "--proof", str(destination),
            "--digest", digest("1"), "--tested-config", config])
    stored = m.read_json(destination)
    assert stored["tested_amd64_config"] == digest("4")
    assert stored["boot_verified"] is True
    assert stored["boot_mode"] == ("published-digest" if changed else "candidate-config-match")
    assert boots == ([(IMAGE, digest("1"), "1.27.3", digest("4"))] if changed else [])

@pytest.mark.parametrize("bad", [None, "health", "version", "maintenance", "uid", "config"])
def test_exact_published_boot_keeps_hardening_and_failure_cleanup(monkeypatch, bad):
    calls = []
    reference = IMAGE + "@" + digest("1")
    def command(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("docker", "image", "inspect"):
            return (digest("9") if bad == "config" else digest("4")).encode()
        if args[:3] == ("docker", "exec", "candidate-published"):
            return b"0" if bad == "uid" else b"10001"
        return b""
    monkeypatch.setattr(m, "command", command)
    health = {"status": "bad" if bad == "health" else "ok",
              "version": "9.9.9" if bad == "version" else "1.27.3",
              "panel_update": {"maintenance": bad == "maintenance", "ready": False}}
    class Response(io.BytesIO):
        status = 200
    monkeypatch.setattr(m, "urlopen", lambda *args, **kwargs: Response(json.dumps(health).encode()))
    if bad:
        with pytest.raises(m.EvidenceError):
            m.boot_published(IMAGE, digest("1"), "1.27.3", digest("4"))
    else:
        m.boot_published(IMAGE, digest("1"), "1.27.3", digest("4"))
    assert calls[0] == ("docker", "pull", "--platform", "linux/amd64", reference)
    if bad == "config":
        assert not any(call[:2] == ("docker", "run") for call in calls)
        return
    run = next(call for call in calls if call[:2] == ("docker", "run"))
    assert run[-1] == reference
    for flag in ("--read-only", "--tmpfs", "--cap-drop", "--security-opt", "--platform"):
        assert flag in run
    assert "/tmp:size=64m,mode=1777" in run and "ALL" in run and "no-new-privileges:true" in run
    assert calls[-1] == ("docker", "rm", "--force", "--volumes", "candidate-published")

def test_workflow_locks_are_distinct_and_qemu_precedes_builder():
    import yaml
    workflow = yaml.safe_load(Path(m.WORKFLOW).read_text())
    workflow_lock = workflow["concurrency"]
    job_lock = workflow["jobs"]["image"]["concurrency"]
    assert workflow_lock["group"] != job_lock["group"]
    assert workflow_lock["cancel-in-progress"] is False
    assert job_lock["cancel-in-progress"] is False
    steps = workflow["jobs"]["image"]["steps"]
    qemu = next(i for i, item in enumerate(steps) if "docker/setup-qemu-action@" in item.get("uses", ""))
    builder = next(i for i, item in enumerate(steps) if "docker/setup-buildx-action@" in item.get("uses", ""))
    assert qemu < builder


@pytest.mark.parametrize("field,value", [("boot_verified", False), ("tested_amd64_config", digest("9"))])
def test_promote_cannot_bypass_boot_binding(monkeypatch, expected, proof, field, value):
    proof[field] = value
    monkeypatch.setattr(m, "registry_evidence", lambda *args: pytest.fail("Must fail before registry access"))
    with pytest.raises(m.EvidenceError, match="boot-to-published-image"):
        m.promote(expected, proof, [IMAGE + ":sha-" + SHA])

@pytest.mark.parametrize("change", [None, "short-sha", "wrong-head", "dirty-tree"])
def test_identity_binds_complete_source_and_clean_workflow(monkeypatch, tmp_path, change):
    monkeypatch.chdir(tmp_path)
    workflow = tmp_path / m.WORKFLOW
    workflow.parent.mkdir(parents=True)
    workflow.write_text("synthetic workflow")
    monkeypatch.setenv("GITHUB_SHA", "a" * 7 if change == "short-sha" else SHA)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/codepier")
    def command(*args, **kwargs):
        if args == ("git", "rev-parse", "HEAD"):
            return ("d" * 40 if change == "wrong-head" else SHA).encode()
        if args == ("git", "diff", "--name-only", "HEAD"):
            return b"Dockerfile" if change == "dirty-tree" else b""
        if args == ("git", "rev-parse", "HEAD^{tree}"):
            return b"b" * 40
        pytest.fail("Unexpected command")
    monkeypatch.setattr(m, "command", command)
    if change:
        with pytest.raises(m.EvidenceError):
            m.identity("1.27.3")
    else:
        result = m.identity("1.27.3")
        assert result["source_tree"] == "b" * 40
        assert result["workflow_sha256"] == hashlib.sha256(workflow.read_bytes()).hexdigest()
        assert result["context"].endswith("#" + SHA)


@pytest.fixture
def handoff_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "18")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", SHA)

def test_read_only_job_handoff_preserves_proof_without_actions_access(
        monkeypatch, expected, proof, handoff_environment):
    value = m.encode_handoff(proof, expected)
    assert len(value.encode()) < m.MAX_HANDOFF and "\n" not in value
    monkeypatch.setattr(m, "api", lambda *args: pytest.fail("Publishing job called Actions"))
    monkeypatch.setattr(m, "api_json", lambda *args: pytest.fail("Publishing job called Actions"))
    monkeypatch.setattr(m, "registry_evidence", lambda *args: proof["registry"])
    assert m.consume_handoff(expected, value, "success") == proof

@pytest.mark.parametrize("result", ["", "failure", "cancelled", "skipped", "unknown"])
def test_non_successful_verifier_cannot_skip_build(
        monkeypatch, expected, proof, handoff_environment, result):
    value = m.encode_handoff(proof, expected)
    monkeypatch.setattr(m, "registry_evidence", lambda *args: pytest.fail("Untrusted handoff used registry"))
    with pytest.raises(m.EvidenceError, match="did not succeed"):
        m.consume_handoff(expected, value, result)

@pytest.mark.parametrize("field,value", [
    ("run_id", 19), ("run_attempt", 2), ("repository", "fork/codepier"),
    ("source_sha", "d" * 40), ("workflow_sha", "d" * 40),
])
def test_handoff_is_bound_to_current_run_and_source(
        monkeypatch, expected, proof, handoff_environment, field, value):
    envelope = json.loads(m.encode_handoff(proof, expected))
    envelope["verified_for"][field] = value
    monkeypatch.setattr(m, "registry_evidence", lambda *args: pytest.fail("Foreign handoff used registry"))
    with pytest.raises(m.EvidenceError, match="run/attempt differs"):
        m.consume_handoff(expected, json.dumps(envelope), "success")

@pytest.mark.parametrize("value", ["", "{", "{}", "[]", "null", "[" * 2000 + "]" * 2000, " " * (m.MAX_HANDOFF + 1)])
def test_absent_truncated_or_malformed_handoff_requires_full_build(
        monkeypatch, tmp_path, expected, handoff_environment, value, capsys):
    identity = tmp_path / "identity.json"
    destination = tmp_path / "proof.json"
    outputs = tmp_path / "outputs"
    m.write_json(identity, expected)
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("PROMOTION_RESULT", "success")
    monkeypatch.setenv("PROMOTION_PROOF", value)
    monkeypatch.setattr(m, "api", lambda *args: pytest.fail("Fallback used Actions"))
    assert m.main(["consume", "--identity", str(identity), "--proof", str(destination)]) == 0
    assert "reused=false" in outputs.read_text()
    assert not destination.exists()
    assert "full build required" in capsys.readouterr().out

@pytest.mark.parametrize("change", ["source", "boot", "registry"])
def test_handoff_rechecks_identity_boot_and_live_registry(
        monkeypatch, tmp_path, expected, proof, handoff_environment, change, capsys):
    envelope = json.loads(m.encode_handoff(proof, expected))
    if change == "source":
        envelope["proof"]["identity"]["source_tree"] = "d" * 40
    elif change == "boot":
        envelope["proof"]["boot_verified"] = False
    identity, destination, outputs = [tmp_path / name for name in ("identity.json", "proof.json", "outputs")]
    m.write_json(identity, expected)
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("PROMOTION_RESULT", "success")
    monkeypatch.setenv("PROMOTION_PROOF", json.dumps(envelope))
    monkeypatch.setattr(m, "api_json", lambda *args: pytest.fail("Consumer used Actions"))
    monkeypatch.setattr(m, "registry_evidence", lambda *args: {})
    assert m.main(["consume", "--identity", str(identity), "--proof", str(destination)]) == 0
    assert "reused=false" in outputs.read_text() and not destination.exists()
    assert "full build required" in capsys.readouterr().out

def test_successful_consumer_writes_proof_and_only_then_allows_reuse(
        monkeypatch, tmp_path, expected, proof, handoff_environment):
    identity, destination, outputs = [tmp_path / name for name in ("identity.json", "proof.json", "outputs")]
    m.write_json(identity, expected)
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("PROMOTION_RESULT", "success")
    monkeypatch.setenv("PROMOTION_PROOF", m.encode_handoff(proof, expected))
    monkeypatch.setattr(m, "api", lambda *args: pytest.fail("Consumer used Actions"))
    monkeypatch.setattr(m, "registry_evidence", lambda *args: proof["registry"])
    assert m.main(["consume", "--identity", str(identity), "--proof", str(destination)]) == 0
    assert m.read_json(destination) == proof
    assert "reused=true" in outputs.read_text()

def test_image_actions_permission_is_isolated_to_read_only_verifier():
    import yaml
    workflow = yaml.safe_load(Path(m.WORKFLOW).read_text())
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert set(jobs) == {"promotion", "image"}
    verifier, publisher = jobs["promotion"], jobs["image"]
    assert verifier["permissions"] == {"contents": "read", "actions": "read"}
    assert publisher["permissions"] == {"contents": "read", "packages": "write"}
    assert publisher["needs"] == ["promotion"]
    assert publisher["if"] == "$" + "{{ !cancelled() }}"
    assert set(verifier["outputs"]) == {"proof"}
    verify_steps = json.dumps(verifier["steps"])
    publish_steps = json.dumps(publisher["steps"])
    assert "image_promotion.py discover" in verify_steps
    assert "GH_TOKEN" in verify_steps
    assert "image_promotion.py discover" not in publish_steps and "GH_TOKEN" not in publish_steps
    assert "image_promotion.py consume" in publish_steps
    assert "needs.promotion.result" in publish_steps and "needs.promotion.outputs.proof" in publish_steps
    assert "download-artifact" not in publish_steps
    for forbidden in ("docker/", "upload-artifact", "packages: write", "actions: write"):
        assert forbidden not in verify_steps

def test_read_only_discovery_exports_bounded_current_run_output(
        monkeypatch, tmp_path, expected, proof, handoff_environment):
    identity, destination, outputs = [tmp_path / name for name in ("identity.json", "proof.json", "outputs")]
    m.write_json(identity, expected)
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setattr(m, "discover", lambda *args: proof)
    monkeypatch.setattr(m, "registry_evidence", lambda *args: pytest.fail("Verifier used registry"))
    assert m.main(["discover", "--identity", str(identity), "--proof", str(destination)]) == 0
    value = outputs.read_text().removeprefix("proof=").rstrip("\n")
    assert json.loads(value)["verified_for"]["run_id"] == 18
    assert json.loads(value)["proof"] == proof


@pytest.mark.parametrize("result", ["failure", "skipped", ""])
def test_failed_or_missing_verifier_job_explicitly_falls_back(
        monkeypatch, tmp_path, expected, proof, handoff_environment, result):
    identity, destination, outputs = [tmp_path / name for name in ("identity.json", "proof.json", "outputs")]
    m.write_json(identity, expected)
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("PROMOTION_RESULT", result)
    monkeypatch.setenv("PROMOTION_PROOF", m.encode_handoff(proof, expected))
    monkeypatch.setattr(m, "api", lambda *args: pytest.fail("Build fallback used Actions"))
    monkeypatch.setattr(m, "registry_evidence", lambda *args: pytest.fail("Failed verifier evidence used"))
    assert m.main(["consume", "--identity", str(identity), "--proof", str(destination)]) == 0
    assert "reused=false" in outputs.read_text() and not destination.exists()

def test_oversize_verifier_output_falls_back_without_partial_proof(
        monkeypatch, tmp_path, expected, proof, handoff_environment, capsys):
    identity, destination, outputs = [tmp_path / name for name in ("identity.json", "proof.json", "outputs")]
    m.write_json(identity, expected)
    proof["extra"] = "x" * m.MAX_HANDOFF
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setattr(m, "discover", lambda *args: proof)
    assert m.main(["discover", "--identity", str(identity), "--proof", str(destination)]) == 0
    assert outputs.read_text() == "proof=\n" and not destination.exists()
    assert "full build required" in capsys.readouterr().out
