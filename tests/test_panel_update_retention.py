import copy
from dataclasses import FrozenInstanceError

import pytest

from scripts.panel_update_retention import main, plan_retention


def inventory(count=4):
    current = "1.0.9"
    versions = [f"1.0.{i}" for i in range(count)] + [current]
    objects = [obj(v) for v in versions]
    return {
        "deployment": "test", "complete": True, "current_version": current,
        "current_mount_refs": ["volume:" + current], "env_refs": ["volume:" + current],
        "current_json_refs": ["volume:" + current], "rollback_refs": [], "pins": [],
        "deploy_lock": False, "containers": [], "objects": objects,
        "jobs": [{"id": str(i), "deployment": "test", "kind": "apply", "state": "succeeded",
                  "from_version": v, "target_version": current, "updated": i,
                  "commit_decided": True, "refs": ["volume:" + v]}
                 for i, v in enumerate(versions[:-1])],
    }


def obj(version, name=None):
    return {"kind": "volume", "name": name or version, "version": version, "allocated_bytes": 4096,
            "labels": {"com.codepier.deployment": "test", "com.codepier.role": "state"}, "refs": []}


def rows(data):
    return {row["id"]: row for row in plan_retention(data).as_dict()["objects"]}


@pytest.mark.parametrize("count", [0, 1, 2, 3, 6])
def test_two_distinct_history_plus_current(count):
    data = inventory(count)
    manifest = plan_retention(data).as_dict()
    assert len(manifest["historical_versions_to_keep"]) == min(count, 2)
    assert "1.0.9" not in manifest["historical_versions_to_keep"]
    assert rows(data)["volume:1.0.9"]["disposition"] == "protected"
    candidates = [r for r in manifest["objects"] if r["disposition"] == "review_candidate"]
    assert len(candidates) == max(0, count - 2)
    assert manifest["deletion_authorized"] is False


def test_duplicate_jobs_and_volumes_do_not_consume_history_slots():
    data = inventory()
    data["jobs"].extend([copy.deepcopy(data["jobs"][-1])] * 3)
    data["objects"].append(obj("1.0.3", "second-volume"))
    manifest = plan_retention(data).as_dict()
    assert manifest["historical_versions_to_keep"] == ["1.0.3", "1.0.2"]
    assert "retained_historical_version" in rows(data)["volume:second-volume"]["protection_reasons"]


@pytest.mark.parametrize("state", ["running", "queued", "recovery_required", "unknown"])
def test_active_and_recovery_jobs_pin_all_native_references(state):
    data = inventory()
    data["jobs"].append({"id": "active", "deployment": "test", "kind": "apply", "state": state,
                         "old_volume": "1.0.0", "new_volume": "1.0.1",
                         "backup": {"volume": "1.0.0", "retained": True}})
    assert rows(data)["volume:1.0.0"]["disposition"] == "protected"
    assert rows(data)["volume:1.0.1"]["disposition"] == "protected"


def test_failed_candidates_are_separate_and_not_successful_history():
    data = inventory()
    data["objects"].append(obj("1.0.8"))
    data["jobs"].append({"id": "failure", "deployment": "test", "kind": "apply",
                         "state": "failed", "target_version": "1.0.8", "new_volume": "1.0.8"})
    manifest = plan_retention(data).as_dict()
    assert manifest["historical_versions_to_keep"] == ["1.0.3", "1.0.2"]
    row = rows(data)["volume:1.0.8"]
    assert row["group"] == "failed_candidate"
    assert row["disposition"] == "review_candidate"


@pytest.mark.parametrize("field", ["current_mount_refs", "env_refs", "current_json_refs", "rollback_refs", "pins"])
def test_all_explicit_references_pin(field):
    data = inventory()
    data[field].append("volume:1.0.0")
    assert field in rows(data)["volume:1.0.0"]["protection_reasons"]


def test_stopped_containers_and_stable_assets_are_protected():
    data = inventory()
    data["containers"] = [{"id": "stopped", "state": "exited", "refs": ["volume:1.0.0"]}]
    data["objects"][1]["labels"]["com.codepier.role"] = "assets"
    assert "container:stopped" in rows(data)["volume:1.0.0"]["protection_reasons"]
    assert "stable_assets" in rows(data)["volume:1.0.1"]["protection_reasons"]


@pytest.mark.parametrize("field,value", [
    ("labels", {}), ("labels", {"com.codepier.deployment": "other", "com.codepier.role": "state"}),
    ("version", None), ("allocated_bytes", None), ("allocated_bytes", True), ("ambiguous", True),
])
def test_unknown_and_cross_deployment_objects_are_manual_only(field, value):
    data = inventory()
    data["objects"][0][field] = value
    assert rows(data)["volume:1.0.0"]["disposition"] == "protected"


@pytest.mark.parametrize("field", ["complete", "jobs", "containers", "pins", "deploy_lock"])
def test_missing_inventory_or_lock_blocks_all_candidates(field):
    data = inventory()
    data.pop(field)
    assert all(row["disposition"] == "protected" for row in rows(data).values())


def test_foreign_job_does_not_establish_successful_history():
    data = inventory(1)
    data["jobs"][0]["deployment"] = "other"
    manifest = plan_retention(data).as_dict()
    assert manifest["historical_versions_to_keep"] == []
    assert rows(data)["volume:1.0.0"]["disposition"] == "protected"


def test_duplicate_identity_is_ambiguous():
    data = inventory()
    data["objects"].append(copy.deepcopy(data["objects"][0]))
    assert "ambiguous_duplicate_identity" in rows(data)["volume:1.0.0"]["protection_reasons"]


def test_manifest_is_immutable_and_all_references_are_hashed():
    data = inventory()
    original = copy.deepcopy(data)
    first = plan_retention(data)
    assert data == original
    assert first == plan_retention(data)
    decoded = first.as_dict()
    decoded["objects"].clear()
    assert first.as_dict()["objects"]
    with pytest.raises(FrozenInstanceError):
        first.sha256 = "changed"
    data["containers"].append({"id": "late", "state": "exited", "refs": ["volume:1.0.0"]})
    second = plan_retention(data)
    assert first.sha256 != second.sha256
    for key in ("expected_state_sha256", "expected_reference_sha256"):
        assert first.as_dict()[key] != second.as_dict()[key]


def test_cli_only_reads_input_and_prints_manifest(tmp_path, capsys):
    import json
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory()))
    before = path.read_bytes()
    main([str(path)])
    result = json.loads(capsys.readouterr().out)
    assert result["manifest"]["mode"] == "read_only"
    assert len(result["manifest_sha256"]) == 64
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_malformed_references_abort_without_candidates():
    data = inventory()
    data["pins"] = ["not-an-exact-object-id"]
    with pytest.raises(ValueError):
        plan_retention(data)


def test_failed_job_old_state_and_backup_remain_pinned():
    data = inventory()
    data["objects"].append(obj("1.0.8"))
    data["jobs"].append({"id": "failed", "deployment": "test", "kind": "apply", "state": "rolled_back",
                         "new_volume": "1.0.8", "old_volume": "1.0.0",
                         "backup": {"volume": "1.0.1", "retained": True}})
    result = rows(data)
    assert result["volume:1.0.8"]["group"] == "failed_candidate"
    for name in ("1.0.0", "1.0.1"):
        assert "failed_job_rollback_reference" in result["volume:" + name]["protection_reasons"]


def test_check_success_does_not_count_as_deployed_version():
    data = inventory(0)
    data["objects"].append(obj("1.0.7"))
    data["jobs"].append({"id": "checked", "deployment": "test", "kind": "check",
                         "state": "succeeded", "target_version": "1.0.7", "updated": 999})
    assert plan_retention(data).as_dict()["historical_versions_to_keep"] == []
    assert rows(data)["volume:1.0.7"]["disposition"] == "protected"


@pytest.mark.parametrize("change", ["size", "label", "version", "lock", "pin"])
def test_state_changes_invalidate_manifest(change):
    data = inventory()
    before = plan_retention(data)
    if change == "size":
        data["objects"][0]["allocated_bytes"] += 4096
    elif change == "label":
        data["objects"][0]["labels"]["extra"] = "changed"
    elif change == "version":
        data["current_version"] = "1.0.10"
    elif change == "lock":
        data["deploy_lock"] = True
    else:
        data["pins"].append("volume:1.0.0")
    after = plan_retention(data)
    assert before.sha256 != after.sha256
    assert before.as_dict()["expected_state_sha256"] != after.as_dict()["expected_state_sha256"]


def test_missing_job_identity_and_unresolved_active_job_block_candidates():
    data = inventory()
    data["jobs"].append({"deployment": "test", "kind": "apply", "state": "recovery_required"})
    result = plan_retention(data).as_dict()
    assert "incomplete_job_metadata" in result["warnings"]
    assert "unresolved_active_job" in result["warnings"]
    assert all(r["disposition"] == "protected" for r in result["objects"])


def test_invalid_version_metadata_is_protected_not_inferred():
    data = inventory()
    data["objects"][0]["version"] = {"unexpected": "version"}
    assert "unknown_version" in rows(data)["volume:1.0.0"]["protection_reasons"]


@pytest.mark.parametrize("reverse", [False, True])
def test_failed_candidate_in_one_job_is_pinned_as_another_jobs_rollback(reverse):
    data = inventory()
    data["objects"].extend([obj("1.0.7"), obj("1.0.8")])
    failures = [
        {"id": "first", "deployment": "test", "kind": "apply", "state": "failed",
         "new_volume": "1.0.7"},
        {"id": "second", "deployment": "test", "kind": "apply", "state": "rolled_back",
         "new_volume": "1.0.8", "old_volume": "1.0.7", "backup": {"volume": "1.0.7"}},
    ]
    data["jobs"].extend(reversed(failures) if reverse else failures)
    row = rows(data)["volume:1.0.7"]
    assert row["group"] == "failed_candidate"
    assert row["disposition"] == "protected"
    assert "failed_job_rollback_reference" in row["protection_reasons"]


@pytest.mark.parametrize("rollback_field", ["old_volume", "backup"])
def test_same_job_candidate_and_rollback_alias_remains_pinned(rollback_field):
    data = inventory()
    data["objects"].append(obj("1.0.8"))
    job = {"id": "ambiguous", "deployment": "test", "kind": "apply", "state": "failed",
           "new_volume": "1.0.8"}
    job[rollback_field] = {"volume": "1.0.8"} if rollback_field == "backup" else "1.0.8"
    data["jobs"].append(job)
    assert rows(data)["volume:1.0.8"]["disposition"] == "protected"
