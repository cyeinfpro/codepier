"""Generated release text must remain Git-safe without changing string values."""
from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_mcp_apps_generated_metadata_matches_declared_dependency():
    directory = ROOT / 'web/mcp-apps'
    package = json.loads((directory / 'package.json').read_text())
    manifest = json.loads((directory / 'manifest.json').read_text())
    # The Apps SDK is the test host harness only; production pages bundle the
    # local host bridge, so it must not be a production dependency.
    assert '@modelcontextprotocol/ext-apps' not in package.get('dependencies', {})
    version = package['devDependencies']['@modelcontextprotocol/ext-apps']
    assert manifest['test_sdk'] == f'@modelcontextprotocol/ext-apps@{version}'
    notices = (directory / 'THIRD_PARTY_NOTICES.txt').read_text()
    assert notices.endswith('\n') and not notices.endswith('\n\n')
    lock = json.loads((directory / 'package-lock.json').read_text())
    declared = {line for line in notices.splitlines() if '@' in line[1:] and ' ' not in line}
    expected = set()
    for name in [*manifest['bundled_packages'], 'esbuild']:
        selected = lock['packages']['node_modules/' + name]['version']
        expected.add(name + '@' + selected)
    assert declared == expected, 'Rebuild Apps after changing the bundled packages or lock'


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
