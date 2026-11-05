"""Complete path claims for Git worktree creation and private run outputs."""
import hashlib
from types import SimpleNamespace

import pytest

from agent.resource_queue import Claim, canonical_path, claims_for, overlaps
from shared.util import DevError


def engine(**integrations):
    return SimpleNamespace(config={'integrations': integrations})


def test_worktree_claims_metadata_and_exact_default_target(tmp_path):
    root = tmp_path.resolve() / 'source'
    root.mkdir()
    (root / '.git').mkdir()
    claims = claims_for(engine(), 'worktrees_create', {}, {}, root, operation_id='operation')
    target = root.parent / 'CodePier-Worktrees' / hashlib.sha256(str(root).encode()).hexdigest()[:16] / 'operation'
    assert claims == [
        Claim('agent', 'path', canonical_path(root), False, 'process', True),
        Claim('agent', 'path', canonical_path(root / '.git'), True, 'process'),
        Claim('agent', 'path', canonical_path(target), True, 'process'),
    ]
    assert not any(claim.write and claim.name == canonical_path(root) for claim in claims)
    assert not overlaps(claims, [Claim('agent', 'path', canonical_path(root / 'src'), False, 'process')])
    assert overlaps(claims, [Claim('agent', 'path', canonical_path(root / '.git' / 'refs'), True, 'process')])
    assert overlaps(claims, [Claim('agent', 'path', canonical_path(target / 'src'), True)])
    assert not target.exists()


def test_configured_target_and_legacy_conservative_parent(tmp_path):
    root = tmp_path.resolve() / 'source'
    root.mkdir()
    configured = tmp_path.resolve() / 'managed'
    parent = configured / hashlib.sha256(str(root).encode()).hexdigest()[:16]
    exact = claims_for(engine(worktree_directory=str(configured)), 'worktrees_create', {}, {}, root, 'op')
    legacy = claims_for(engine(worktree_directory=str(configured)), 'worktrees_create', {}, {}, root)
    assert exact[-1].name == canonical_path(parent / 'op')
    assert legacy[-1].name == canonical_path(parent)
    assert not configured.exists()


def test_gitfile_and_commondir_cover_shared_metadata(tmp_path):
    root = tmp_path.resolve() / 'checkout'
    root.mkdir()
    common = tmp_path.resolve() / 'repository.git'
    private = common / 'worktrees' / 'checkout'
    private.mkdir(parents=True)
    (private / 'commondir').write_text('../..\n')
    (root / '.git').write_text('gitdir: ../repository.git/worktrees/checkout\n')
    claims = claims_for(engine(), 'worktrees_create', {}, {}, root, 'op')
    names = {claim.name for claim in claims if claim.write}
    assert {canonical_path(root / '.git'), canonical_path(private), canonical_path(common)} <= names
    assert overlaps(claims, [Claim('agent', 'path', canonical_path(common / 'packed-refs'), True, 'process')])


def test_metadata_symlink_is_canonicalized_only_as_conflict_key(tmp_path):
    root = tmp_path.resolve() / 'checkout'
    root.mkdir()
    actual = tmp_path.resolve() / 'metadata'
    actual.mkdir()
    (root / '.git').symlink_to(actual, target_is_directory=True)
    claims = claims_for(engine(), 'worktrees_create', {}, {}, root, 'op')
    assert claims[1].name == canonical_path(actual)
    # Claim generation is observational: no new directories or Git mutations.
    assert list(actual.iterdir()) == []


@pytest.mark.parametrize('pointer', ['malformed', 'gitdir: ', 'gitdir: foo\nbar', 'gitdir: ' + 'x' * 5000])
def test_ambiguous_git_metadata_fails_before_execution(tmp_path, pointer):
    root = tmp_path.resolve() / 'checkout'
    root.mkdir()
    (root / '.git').write_text(pointer)
    with pytest.raises(DevError) as caught:
        claims_for(engine(), 'worktrees_create', {}, {}, root, 'op')
    assert caught.value.code == 'WORKTREE_METADATA_UNAVAILABLE'


def test_checkpoint_only_freezes_source(tmp_path):
    root = tmp_path.resolve()
    assert claims_for(engine(), 'project_checkpoint', {}, {}, root, 'op') == [
        Claim('agent', 'path', canonical_path(root), False, 'process', True)]


def test_run_directories_are_pairwise_independent_but_root_claims_still_apply(tmp_path):
    root = tmp_path.resolve()
    first = Claim('agent', 'path', canonical_path(root / '.codepier/runs/first'), True, 'process')
    second = Claim('agent', 'path', canonical_path(root / '.codepier/runs/second'), True, 'process')
    assert not first.conflicts(second)
    assert first.conflicts(Claim('agent', 'path', canonical_path(root), False, 'process'))
    assert first.conflicts(Claim('agent', 'path', canonical_path(root / '.codepier/runs/first/log'), False, 'process'))
    assert not first.conflicts(Claim('agent', 'path', canonical_path(root / 'src'), False, 'process'))


@pytest.mark.parametrize('operation_id', ['../escape', '/outside', ''])
def test_invalid_operation_identity_never_creates_target_claim(tmp_path, operation_id):
    with pytest.raises(DevError):
        claims_for(engine(), 'worktrees_create', {}, {}, tmp_path.resolve(), operation_id)


def test_relative_configured_worktree_root_is_rejected(tmp_path):
    with pytest.raises(DevError) as caught:
        claims_for(engine(worktree_directory='relative'), 'worktrees_create', {}, {}, tmp_path.resolve(), 'op')
    assert caught.value.code == 'WORKTREE_DIRECTORY'
