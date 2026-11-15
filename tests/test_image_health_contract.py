"""Execute the real image workflow health gate against an isolated empty Hub."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

from fastapi.testclient import TestClient
import pytest

from shared.util import VERSION

ROOT = Path(__file__).resolve().parents[1]


def workflow_health_check(health):
    workflow = (ROOT / ".github/workflows/image.yml").read_text()
    step = workflow.split(
        "- name: Candidate must boot healthy from an empty data directory under the Compose hardening",
        1,
    )[1].split("- name: Candidate logs and cleanup", 1)[0]
    code = textwrap.dedent(
        step.split("python3 - \"$body\" <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    )
    return subprocess.run(
        [sys.executable, "-", json.dumps(health)],
        input=code,
        text=True,
        capture_output=True,
        env={**os.environ, "VERSION": VERSION},
        timeout=10,
    )


@pytest.fixture
def empty_hub_health(tmp_path, monkeypatch):
    from hub.app import create_app

    monkeypatch.delenv("HUB_PANEL_UPDATE_SOCKET", raising=False)
    monkeypatch.setenv("HUB_PUBLIC_URL", "http://127.0.0.1:8765")
    app = create_app(str(tmp_path / "empty-hub"))
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        yield response.json()


def test_image_workflow_accepts_real_empty_hub_health(empty_hub_health):
    result = workflow_health_check(empty_hub_health)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == empty_hub_health


@pytest.mark.parametrize(
    "field,value",
    [
        ("maintenance", True),
        ("ready", True),
        ("drain_ready", True),
        ("phase", "draining"),
        ("ready_semantics", "application_readiness"),
        ("missing", None),
        ("extra", False),
    ],
)
def test_image_workflow_keeps_strict_maintenance_contract(empty_hub_health, field, value):
    health = copy.deepcopy(empty_hub_health)
    if field == "missing":
        del health["panel_update"]["ready"]
    else:
        health["panel_update"][field] = value
    result = workflow_health_check(health)
    assert result.returncode != 0
    assert "Unexpected maintenance state" in result.stderr


@pytest.mark.parametrize("field,value", [("status", "unavailable"), ("version", "0.0.0")])
def test_image_workflow_keeps_health_and_version_gate(empty_hub_health, field, value):
    health = copy.deepcopy(empty_hub_health)
    health[field] = value
    result = workflow_health_check(health)
    assert result.returncode != 0
    assert "healthz reported" in result.stderr
