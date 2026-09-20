"""Generated release text must remain Git-safe without changing string values."""
from pathlib import Path
import json
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('name', ['web/app.js', 'web/mcp-apps/workspace-v1.html',
                                'web/mcp-apps/changes-v1.html',
                                'web/mcp-apps/THIRD_PARTY_NOTICES.txt'])
def test_generated_release_text_has_no_trailing_whitespace(name):
    lines = (ROOT / name).read_text(encoding='utf-8').splitlines()
    bad = [number for number, line in enumerate(lines, 1) if line.rstrip(' \t') != line]
    assert not bad, f'{name}: trailing whitespace at {bad}'


def test_mcp_apps_generated_metadata_matches_declared_dependency():
    directory = ROOT / 'web/mcp-apps'
    package = json.loads((directory / 'package.json').read_text())
    manifest = json.loads((directory / 'manifest.json').read_text())
    version = package['dependencies']['@modelcontextprotocol/ext-apps']
    assert manifest['sdk'] == f'@modelcontextprotocol/ext-apps@{version}'
    notices = (directory / 'THIRD_PARTY_NOTICES.txt').read_text()
    assert notices.endswith('\n') and not notices.endswith('\n\n')


def test_template_lowering_preserves_runtime_string_bytes():
    directory = ROOT / 'web/mcp-apps'
    assert "supported:{'template-literal':false}" in (directory / 'build.mjs').read_text()
    script = r"""
import {transform} from 'esbuild';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const value='first\n        \nlast ${literal} ` \\';
const output=await transform('globalThis.probe='+JSON.stringify(value), {
  minify:true,target:'es2022',supported:{'template-literal':false}
});
const scope={};vm.runInNewContext(output.code,scope);
assert.equal(scope.probe,value);
assert(!/[ \t]+$/m.test(output.code));
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=directory,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
