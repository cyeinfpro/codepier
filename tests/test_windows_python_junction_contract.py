"""Portable source contracts; native PowerShell cases run in check_windows_bootstrap."""
from pathlib import Path
import pytest

pytestmark = pytest.mark.windows_core
ROOT = Path(__file__).resolve().parents[1]


def function(source, name):
    anchor = 'function '+name+' {'
    assert source.count(anchor) == 1
    start = source.index(anchor)
    end = source.find('\nfunction ', start+len(anchor))
    return source[start:end if end >= 0 else len(source)]


def test_powershell_only_unlinks_revalidated_python_junction_objects():
    source = (ROOT/'deploy/install-from-hub.ps1').read_text()
    validator = function(source, 'Assert-CodePierPythonJunction')
    for required in ("$Item.LinkType -cne 'Junction'", "$Item.Target).Count -ne 1",
                     "Assert-CodePierPlainPath -Path $Junction.target",
                     "Assert-CodePierPlainPath -Path (Join-Path $Target 'python.exe')",
                     "[IO.Path]::GetDirectoryName($Target) -ine $Python",
                     "[IO.Path]::GetFullPath($RawTarget) -ine $Target"):
        assert required in validator
    remover = function(source, 'Remove-CodePierPythonJunctions')
    assert remover.index('Assert-CodePierPythonJunction -Junction') < remover.index('[IO.Directory]::Delete')
    assert '[IO.Directory]::Delete($Verified.path, $false)' in remover
    assert 'Remove-Item' not in remover
    assert 'Assert-CodePierUninstallTree -Path $Source' in remover


def test_tree_skips_only_validated_aliases_then_cleanup_requires_plain_tree():
    source = (ROOT/'deploy/install-from-hub.ps1').read_text()
    tree = function(source, 'Assert-CodePierUninstallTree')
    assert '$AllowedPythonJunctions -notcontains $Item.FullName' in tree
    assert tree.index('continue') < tree.index('Get-ChildItem')
    root = function(source, 'Assert-CodePierUninstallRoot')
    assert root.index('Get-CodePierPythonJunctions') < root.index('-AllowedPythonJunctions')
    complete = function(source, 'Complete-CodePierUninstall')
    assert complete.index('Assert-CodePierUninstallTasksAbsent') < complete.index('Remove-CodePierPythonJunctions')
    assert complete.index('Remove-CodePierPythonJunctions') < complete.index('Remove-Item -LiteralPath $Item -Recurse')
    assert 'Assert-CodePierUninstallTree -Path $Item\n' in complete
    assert source.count('Remove-CodePierPythonJunctions -Root $Root -Source $Source') == 1


def test_bootstrap_preserves_installed_python_until_helper_exits():
    source = (ROOT/'deploy/install-from-hub.ps1').read_text()
    assert "& $Python @CodePierArgs\n      if ($LASTEXITCODE -ne 0)" in source
    assert "& $CodePierPython @CodePierArgs\n  if ($LASTEXITCODE -ne 0)" in source
    assert source.index('if ($LASTEXITCODE -ne 0)', source.index('& $CodePierPython @CodePierArgs')) < source.rindex('Complete-CodePierUninstall -PlanPath')
    assert 'pyvenv.cfg' not in source
