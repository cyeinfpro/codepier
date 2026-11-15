"""Quality tools distinguish read-only evidence from claims of full UI acceptance."""

import importlib.util
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_integration_check_never_writes(tmp_path, monkeypatch):
    mod = load("build_integration_assets")
    web = tmp_path / "web"
    source = web / "browser-extension"
    source.mkdir(parents=True)
    for name in mod.EXTENSION_FILES:
        (source / name).write_text("{}" if name == "manifest.json" else "// fixture")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/INTEGRATIONS-20260917.md").write_text("guide")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    missing = mod.build(check=True)
    assert not missing["matches"]
    assert not (web / "browser-extension.zip").exists()
    mod.build()
    outputs = [
        web / name
        for name in ("browser-extension.zip", "integration-guide.md", "integration-assets.json")
    ]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in outputs]
    assert mod.build(check=True)["matches"]
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in outputs]
    (source / "popup.js").write_text("// changed")
    assert "browser-extension.zip" in mod.build(check=True)["drift"]
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in outputs]
    a, _ = mod.render_assets()
    b, _ = mod.render_assets()
    assert a == b


def test_comfort_summary_keeps_unverified_distinct():
    mod = load("ui_comfort_audit")
    case = {
        "documentWidth": 390,
        "viewport": {"width": 390},
        "failures": [],
        "complexBackgroundSkipped": [{"text": "gradient"}],
    }
    result = mod.audit_summary([case])
    assert result["measured_checks_passed"]
    assert result["complex_background_text_unchecked"] == 1
    assert not result["full_accessibility_verified"]
    assert not mod.audit_summary([])["measured_checks_passed"]
    assert not mod.audit_summary([{**case, "documentWidth": 500}])["measured_checks_passed"]
    assert not mod.audit_summary([{**case, "failures": [{"ratio": 1}]}])["measured_checks_passed"]


def test_comfort_refuses_overwriting_evidence(tmp_path):
    import pytest

    mod = load("ui_comfort_audit")
    evidence = tmp_path / "captures"
    evidence.mkdir()
    original = evidence / "report.json"
    original.write_text("previous evidence")
    with pytest.raises(ValueError, match="preserved"):
        mod.capture(evidence)
    assert original.read_text() == "previous evidence"


def test_copy_sources_cover_hosts_and_source_template():
    out = subprocess.check_output(
        ["node", "scripts/check-ui-copy.mjs", "--coverage"], cwd=ROOT, text=True
    )
    report, _ = json.JSONDecoder().raw_decode(out)
    sources = report["sources"]
    assert "web/index.source.html" in sources
    assert "web/browser-extension/popup.js" in sources
    assert "web/mcp-apps/ui.js" in sources
    assert "deploy/install-agent.sh" in sources
    assert "deploy/install-agent.ps1" in sources
    assert "web/index.html" not in sources
    assert all("/vendor/" not in path and "/node_modules/" not in path for path in sources)
