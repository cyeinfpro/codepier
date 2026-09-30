"""Current-release browser cache and verification infrastructure contracts."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_release
from shared.util import VERSION


@pytest.fixture
def assets(tmp_path):
    directory = tmp_path / 'web'
    directory.mkdir()
    for name in ('app.js', 'styles.css'):
        (directory / name).write_text('/* fixture */\n')
    (directory / 'index.html').write_text(
        f'<link rel="stylesheet" href="/static/styles.css?v=codepier-{VERSION}">'
        f'<script src="/static/app.js?v=codepier-{VERSION}"></script>')
    return tmp_path


def test_all_first_party_assets_have_the_current_cache_version(assets):
    selected = check_release.check_web_assets(assets, VERSION)
    assert set(selected) == {'app.js', 'styles.css'}


@pytest.mark.parametrize('query', ['', '?v=old-release', '?v=codepier-0.0.0',
                                   f'?v=codepier-{VERSION}&v=old-release', f'?v=codepier-{VERSION}&v='])
def test_web_cache_guard_rejects_missing_stale_or_ambiguous_version(assets, query):
    (assets / 'web/index.html').write_text(f'<script src="/static/app.js{query}"></script>')
    with pytest.raises(ValueError, match='cache version'):
        check_release.check_web_assets(assets, VERSION)


def test_web_cache_guard_rejects_missing_asset(assets):
    (assets / 'web/app.js').unlink()
    with pytest.raises(ValueError, match='asset'):
        check_release.check_web_assets(assets, VERSION)


def test_web_cache_guard_rejects_empty_or_duplicate_manifest(assets):
    index = assets / 'web/index.html'
    html = index.read_text()
    index.write_text(html + html)
    with pytest.raises(ValueError, match='Duplicate'):
        check_release.check_web_assets(assets, VERSION)
    index.write_text('<html>No application assets</html>')
    with pytest.raises(ValueError, match='No'):
        check_release.check_web_assets(assets, VERSION)


def test_vendor_assets_must_have_explicit_package_version_in_path(assets):
    vendor = assets / 'web/vendor/package-1.2.3'
    vendor.mkdir(parents=True)
    (vendor / 'index.js').write_text('/* vendor fixture */')
    with (assets / 'web/index.html').open('a') as stream:
        stream.write('<script src="/static/vendor/package-1.2.3/index.js"></script>')
    assert 'vendor/package-1.2.3/index.js' in check_release.check_web_assets(assets, VERSION)


def test_owned_hub_exit_is_not_retried(monkeypatch):
    from tests import support
    calls = []
    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            calls.append(url)
            raise AssertionError('An exited child must never be probed')
    monkeypatch.setattr(support.httpx, 'Client', lambda **kwargs: Client())
    with pytest.raises(AssertionError, match='exited with code 7'):
        support.wait_for_hub(SimpleNamespace(poll=lambda: 7), 'http://127.0.0.1:1')
    assert calls == []


def test_regression_processes_preserve_per_module_fixture_evidence():
    from scripts import check_full_regression
    command = check_full_regression.pytest_command(['python', '-m', 'pytest'], Path('evidence/module'), ['tests/test_example.py'])
    assert '--basetemp=' + str(Path('evidence/module')/'tmp') in command
    assert '--junitxml=' + str(Path('evidence/module')/'results.xml') in command
    assert command[-1] == 'tests/test_example.py'


def test_license_is_in_the_container_distribution():
    root = Path(__file__).resolve().parents[1]
    assert 'COPY --chown=codepier:codepier LICENSE ./' in (root / 'Dockerfile').read_text()


@pytest.mark.parametrize('image', ['codepier:{v}', '"${CODEPIER_HUB_IMAGE:-codepier:{v}}"'])
def test_compose_default_version_accepts_managed_updates(tmp_path,image):
    (tmp_path/'compose.yml').write_text('services:\n  hub:\n    image: '+image.replace('{v}',VERSION)+'\n')
    check_release.check_compose_image(tmp_path,VERSION)


@pytest.mark.parametrize('image', ['codepier:0.0.0', '"${CODEPIER_HUB_IMAGE:-codepier:0.0.0}"',
    '"${CODEPIER_HUB_IMAGE}"', '"${OTHER_IMAGE:-codepier:{v}}"'])
def test_compose_default_version_rejects_unpinned_or_stale_defaults(tmp_path,image):
    (tmp_path/'compose.yml').write_text('services:\n  hub:\n    image: '+image.replace('{v}',VERSION)+'\n')
    with pytest.raises(ValueError,match='Compose image version'):
        check_release.check_compose_image(tmp_path,VERSION)
