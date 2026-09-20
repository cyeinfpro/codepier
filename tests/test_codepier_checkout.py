"""Checkout rename only changes a recognized source root and generated launchers."""
import json
from pathlib import Path
import pytest
from scripts import rename_checkout as checkout


def fixture(tmp_path):
    root=tmp_path/'remote-dev-mcp';root.mkdir()
    for name in ('install.sh','compose.yml','agent/__main__.py','hub/__main__.py','shared/brand_migration.py'):
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('CodePier')
    entry=root/'.venv/bin/example';entry.parent.mkdir(parents=True);entry.write_text('#!'+str(root/'.venv/bin/python')+'\n');entry.chmod(0o755)
    (root/'owner-note.txt').write_text(str(root))
    return root


def test_rename_preserves_owner_files_and_legacy_access(tmp_path):
    old=fixture(tmp_path);new=tmp_path/'codepier'
    assert checkout.rename(old)['state']=='planned' and not new.exists()
    result=checkout.rename(old,True)
    assert result['state']=='completed' and old.is_symlink() and old.resolve()==new
    assert (new/'owner-note.txt').read_text()==str(old)
    assert (new/'.venv/bin/example').read_text()=='#!'+str(new/'.venv/bin/python')+'\n'
    assert checkout.rename(old,True)['state']=='already-current'


def test_existing_destination_is_not_merged(tmp_path):
    old=fixture(tmp_path);(tmp_path/'codepier').mkdir()
    with pytest.raises(RuntimeError,match='already exists'):checkout.rename(old,True)
    assert not old.is_symlink() and (old/'owner-note.txt').read_text()==str(old)


def test_custom_paths_are_not_renamed(tmp_path):
    p=tmp_path/'My source';p.mkdir();assert checkout.rename(p,True)['changed'] is False


def test_interrupted_rename_recovers_without_rewriting_owner_files(tmp_path,monkeypatch):
    old=fixture(tmp_path);real=checkout.alias
    def crash(*args):raise KeyboardInterrupt('interrupted after directory move')
    monkeypatch.setattr(checkout,'alias',crash)
    with pytest.raises(KeyboardInterrupt):checkout.rename(old,True)
    new=tmp_path/'codepier';monkeypatch.setattr(checkout,'alias',real)
    assert checkout.rename(new,True)['state']=='recovered'
    assert old.is_symlink() and (new/'owner-note.txt').read_text()==str(old)


def test_recovery_does_not_overwrite_an_edited_launcher(tmp_path,monkeypatch):
    old=fixture(tmp_path)
    monkeypatch.setattr(checkout,'alias',lambda *args:(_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):checkout.rename(old,True)
    new=tmp_path/'codepier';(new/'.venv/bin/example').write_text('owner edit')
    with pytest.raises(RuntimeError,match='changed'):checkout.recover(new)
    assert (new/'.venv/bin/example').read_text()=='owner edit'
