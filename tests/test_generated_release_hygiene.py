"""Generated release text must remain Git-safe without changing string values."""
from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).resolve().parents[1]


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
    script = r"""
import {transform} from 'esbuild';
import {codeOptions} from './build-options.mjs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const value='first\n        \nlast ${literal} ` \\';
const output=await transform('globalThis.probe='+JSON.stringify(value), codeOptions);
const scope={};vm.runInNewContext(output.code,scope);
assert.equal(scope.probe,value);
assert(!/[ \t]+$/m.test(output.code));
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=directory,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
