"""Regression tests for source-version-aware isolated Docker acceptance."""
import io
import json
import urllib.request
import pytest
from scripts import check_codepier_migration_docker as check


@pytest.mark.parametrize('source_version',['1.18.0','2.0.1'])
def test_docker_probe_uses_current_source_version(monkeypatch,capsys,source_version):
    monkeypatch.setattr(check,'VERSION',source_version)
    monkeypatch.setattr(urllib.request,'urlopen',lambda url:io.BytesIO(json.dumps({'version':source_version,'bytes':123}).encode()))
    exec(check.health_probe(),{})
    assert json.loads(capsys.readouterr().out)=={'version':source_version,'agent_package_bytes':123}


@pytest.mark.parametrize('stale',['health','manifest'])
def test_docker_probe_rejects_stale_hub_or_agent(monkeypatch,stale):
    def response(url):
        wrong=('healthz' in url)==(stale=='health')
        return io.BytesIO(json.dumps({'version':'1.9.0' if wrong else check.VERSION,'bytes':123}).encode())
    monkeypatch.setattr(urllib.request,'urlopen',response)
    with pytest.raises(AssertionError):
        exec(check.health_probe(),{})
