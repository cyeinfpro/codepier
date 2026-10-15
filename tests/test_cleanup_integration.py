"""Real subprocess regressions for disposable run data and retained evidence."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import pytest
from scripts.regression_plan import pytest_command
from scripts.preview_worktree_cleanup import inventory, size


def test_separate_scratch_from_evidence():
    command = pytest_command(['python'], Path('evidence'), ['tests/test_a.py'],
                             scratch=Path('scratch'))
    assert '--basetemp=scratch/pytest' in command
    assert 'cache_dir=scratch/cache' in command
    assert '--junitxml=evidence/results.xml' in command


@pytest.mark.parametrize('fails', [False, True, 'forced_descendant'])
@pytest.mark.parametrize('archive_proof', [False, True])
def test_full_runner_retains_reports_but_removes_owned_scratch(tmp_path, monkeypatch, fails, archive_proof):
    from scripts import check_full_regression as runner
    root = tmp_path/'source'
    (root/'tests').mkdir(parents=True)
    (root/'pytest.ini').write_text('[pytest]\n')
    (root/'tests'/'test_example.py').write_text(
        "import os, tempfile, pytest\n"
        "from pathlib import Path\n"
        "@pytest.fixture(scope='session')\n"
        "def before_tmp():\n"
        "    with tempfile.TemporaryDirectory() as d:\n"
        "        p=Path(d)/'live-browser-sentinel'; p.write_text('keep')\n"
        "        yield p\n"
        "        assert p.read_text()=='keep'\n"
        "def test_example(before_tmp, tmp_path):\n"
        "    assert before_tmp.read_text()=='keep'\n"
        "    assert Path(os.environ['TMPDIR']).resolve() not in tmp_path.parents\n"
        "    (tmp_path/'payload').write_bytes(b'x' * 100000)\n"
        f"    assert {fails is not True}\n")
    output = tmp_path/'evidence'
    if archive_proof:
        output.mkdir()
        (output/'archive-proof.json').write_text('{"archive": "immutable fixture"}')
    monkeypatch.setattr(runner, 'ROOT', root)
    monkeypatch.setattr(runner, 'snapshot', lambda: {'fixture': 'unchanged'})
    monkeypatch.setattr(sys, 'argv', ['regression', '--output', str(output)])
    if fails == 'forced_descendant':
        original_run = runner.ManagedTestRun.run
        def with_forced_descendant(self, command, **options):
            record = original_run(self, command, **options)
            if '--collect-only' not in command:
                record.update(status='failed', forced_termination=True)
            return record
        monkeypatch.setattr(runner.ManagedTestRun, 'run', with_forced_descendant)
    result = runner.main()
    assert result == int(bool(fails))
    summary = json.loads((output/'summary.json').read_text())
    assert summary['verified'] is (not fails)
    assert (output/'test_example'/'results.xml').is_file()
    assert (output/'test_example'/'output.log').is_file()
    assert not list(output.rglob('.scratch-*'))
    manifests = list(output.rglob('managed-run.json'))
    assert len(manifests) == 2
    if archive_proof:
        assert (output/'archive-proof.json').read_text() == '{"archive": "immutable fixture"}'


def test_full_runner_never_adopts_existing_directory(tmp_path, monkeypatch):
    from scripts import check_full_regression as runner
    sentinel = tmp_path/'keep'
    sentinel.write_text('user data')
    monkeypatch.setattr(sys, 'argv', ['regression', '--output', str(tmp_path)])
    with pytest.raises(SystemExit):
        runner.main()
    assert sentinel.read_text() == 'user data'


def test_preview_preserves_dirty_ignored_and_unique_commit(tmp_path):
    root = tmp_path/'repo'
    root.mkdir()
    def git(*args, cwd=root):
        return subprocess.check_output(['git', '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', '-c', 'core.hooksPath=/dev/null',
            '-C', str(cwd), *args], stderr=subprocess.DEVNULL)
    git('init')
    (root/'file').write_text('base')
    (root/'.gitignore').write_text('cache/\n')
    git('add', '.')
    git('commit', '-m', 'fixture')
    clean = tmp_path/'clean'
    unique = tmp_path/'unique'
    dirty = tmp_path/'dirty'
    for path in (clean, unique, dirty):
        git('worktree', 'add', '--detach', str(path), 'HEAD')
    (unique/'file').write_text('unique')
    git('add', '.', cwd=unique)
    git('commit', '-m', 'unique fixture', cwd=unique)
    (dirty/'user-data').write_text('keep')
    (clean/'cache').mkdir()
    (clean/'cache'/'payload').write_text('ignored')
    report = inventory(root)
    rows = {Path(row['path']).name: row for row in report['worktrees']}
    assert report['deletion_supported'] is False
    assert rows['repo']['candidate_for_review'] is False
    assert rows['dirty']['dirty'] is True
    assert rows['unique']['head_retained_by_ref'] is False
    assert rows['clean']['ignored_count'] == 1
    assert all(row['delete_authorized'] is False for row in rows.values())
    assert (dirty/'user-data').read_text() == 'keep'
    assert (clean/'cache'/'payload').exists()


def test_inventory_size_is_bounded_and_never_follows_symlink(tmp_path):
    source = tmp_path/'source'; source.mkdir()
    outside = tmp_path/'outside'; outside.mkdir()
    (outside/'payload').write_bytes(b'x'*10000)
    try:
        (source/'link').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    assert size(source)[0] == 0
    for index in range(3):
        (source/str(index)).write_bytes(b'x')
    assert size(source, limit=1) == (1, False)
