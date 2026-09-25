"""Behavioral contracts for release, coverage and private evidence retention."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile
import pytest
from scripts.archive_evidence import archive, inventory
from scripts.diff_coverage import changed_lines, report
from scripts.release_policy import check_public_links, check_readme_version, check_supported_branch

ROOT = Path(__file__).resolve().parents[1]


def evidence(tmp_path):
    root = tmp_path / 'evidence';root.mkdir()
    case = root / 'old-case';case.mkdir()
    path = case / 'result.txt';path.write_bytes(b'fixture evidence')
    old = time.time() - 100 * 86400
    os.utime(path, (old, old));os.utime(case, (old, old))
    return root, path


def test_evidence_dry_run_never_deletes_or_writes(tmp_path):
    root, path = evidence(tmp_path)
    reply = subprocess.run([sys.executable, str(ROOT/'scripts/archive_evidence.py'), '--directory', str(root)], capture_output=True, text=True, timeout=15)
    assert reply.returncode == 0
    result = json.loads(reply.stdout)
    assert result['dry_run'] and not result['source_deleted']
    assert result['candidates'] == [{'path':'old-case/result.txt','bytes':16}]
    assert path.read_bytes() == b'fixture evidence' and len(list(tmp_path.iterdir())) == 1


def test_archive_is_verified_private_and_non_destructive(tmp_path):
    root, path = evidence(tmp_path)
    selected = inventory(root, time.time() - 90 * 86400)
    target = tmp_path / 'private' / 'evidence.zip'
    result = archive(root, target, selected)
    assert result['files'] == 1 and not result['source_deleted']
    with zipfile.ZipFile(target) as z:
        manifest = json.loads(z.read('EVIDENCE-MANIFEST.json'))
        assert manifest['old-case/result.txt']['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert z.read('old-case/result.txt') == path.read_bytes()
    if os.name != 'nt': assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError): archive(root, target, selected)
    assert path.exists()


@pytest.mark.parametrize('change', ['content', 'symlink', 'database', 'manifest'])
def test_archive_refuses_changed_or_unsafe_sources_without_partial_publication(tmp_path, change):
    root, path = evidence(tmp_path)
    selected = inventory(root, time.time() - 90 * 86400)
    target = tmp_path / 'evidence.zip'
    if change == 'content': path.write_bytes(b'changed')
    elif change == 'symlink':
        path.unlink();path.symlink_to(tmp_path/'outside')
    else:
        renamed = path.with_name('hub.sqlite3' if change == 'database' else 'EVIDENCE-MANIFEST.json')
        path.rename(renamed)
        old = time.time() - 100 * 86400
        os.utime(path.parent, (old, old))
        with pytest.raises(ValueError): inventory(root, time.time() - 90 * 86400)
        return
    with pytest.raises(ValueError): archive(root, target, selected)
    assert not target.exists()


def test_active_evidence_group_is_not_selected(tmp_path):
    root, path = evidence(tmp_path)
    path.write_bytes(b'active fixture')
    assert inventory(root, time.time() - 90 * 86400) == []


def test_diff_coverage_counts_statements_not_blank_lines_or_deleted_hunks():
    diff = '+++ b/hub/example.py\n@@ -1,2 +4,3 @@\n+x\n+y\n+z\n@@ -9,2 +12,0 @@\n+++ b/tests/test_example.py\n@@ -0,0 +1,4 @@\n'
    changed = changed_lines(diff)
    assert changed == {'hub/example.py': {4, 5, 6}}
    result = report(changed, {'files': {'hub/example.py': {'executed_lines':[4], 'missing_lines':[6, 12], 'excluded_lines':[5]}}})
    assert result['statements'] == 2 and result['covered'] == 1 and result['percent'] == 50
    assert result['files']['hub/example.py']['missing_lines'] == [6]
    absent = report(changed, {'files': {}})
    assert not absent['measurement_complete'] and absent['percent'] is None
    assert absent['unmeasured_files'] == ['hub/example.py']


@pytest.mark.parametrize('target', ['missing.md','../private.md','../../escape.md','file:///private','%2e%2e/private.md'])
def test_public_links_reject_missing_private_or_external_file_targets(tmp_path, target):
    (tmp_path/'docs').mkdir();(tmp_path/'private.md').write_text('not public')
    (tmp_path/'docs/guide.md').write_text('[bad]('+target+')')
    with pytest.raises(ValueError): check_public_links(tmp_path, {'docs/guide.md'})


def test_public_links_accept_cross_directory_source_and_skip_code_examples(tmp_path):
    (tmp_path/'docs').mkdir();(tmp_path/'README.md').write_text('read me')
    (tmp_path/'docs/guide.md').write_text('[Read](../README.md#usage)\n`[sample](not-a-link.md)`\n```md\n[sample](also-not.md)\n```\n')
    check_public_links(tmp_path, {'README.md','docs/guide.md'})


def test_release_version_guards_reject_stale_current_claims_not_history(tmp_path):
    for name in ['README.md','SECURITY.md']:
        (tmp_path/name).write_bytes((ROOT/name).read_bytes())
    from scripts.build_source_bundle import source_version
    version = source_version()
    check_readme_version(tmp_path, version);check_supported_branch(tmp_path, version)
    with pytest.raises(ValueError): check_readme_version(tmp_path, '0.0.0')
    with pytest.raises(ValueError): check_supported_branch(tmp_path, '0.0.0')
    readme = tmp_path/'README.md';readme.write_text(readme.read_text().replace('version-'+version+'-', 'version-0.0.0-'))
    with pytest.raises(ValueError): check_readme_version(tmp_path, version)


def test_complete_coverage_combines_explicit_files_and_keeps_originals(tmp_path):
    from coverage import CoverageData
    from scripts.coverage_reports import combine
    inputs=[]
    for i,line in enumerate([1,2]):
        path=tmp_path/str(i)/'.coverage';path.parent.mkdir()
        data=CoverageData(basename=str(path));data.add_lines({'scripts/release_policy.py':{line}});data.write();inputs.append(path)
    combined=combine(inputs,tmp_path/'complete')
    assert combined.get_data().lines('scripts/release_policy.py') == [1,2]
    assert all(path.exists() for path in inputs)
    with pytest.raises(ValueError): combine([inputs[0],inputs[0]],tmp_path/'duplicate')
    with pytest.raises(ValueError): combine([tmp_path/'missing'],tmp_path/'missing-output')
