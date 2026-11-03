"""Platform-scoped browser policy without changing actionability or caller options."""
import pytest
from tests.browser_launch import launch_options, launch_browser, launch_persistent_browser


@pytest.mark.parametrize('platform', ['darwin', 'linux', 'win32'])
@pytest.mark.parametrize('engine', ['chromium', 'webkit', 'firefox'])
@pytest.mark.parametrize('options', [{}, {'headless': True}, {'headless': False},
    {'channel': 'chrome'}, {'channel': None}, {'executable_path': '/synthetic/browser'},
    {'executable_path': None}, {'args': ['--synthetic'], 'timeout': 123}])
def test_launch_policy_preserves_explicit_choices(platform, engine, options):
    before = dict(options)
    result = launch_options(engine, options, platform=platform)
    expected = dict(options)
    if platform == 'darwin' and engine == 'chromium' and options.get('headless') is not False and 'channel' not in options and 'executable_path' not in options:
        expected['channel'] = 'chromium'
    assert result == expected
    assert options == before
    assert result is not options


def test_launch_wrappers_keep_parameters(monkeypatch):
    monkeypatch.setattr('tests.browser_launch.sys.platform', 'darwin')
    class Browser:
        name = 'chromium'
        def launch(self, **kwargs):
            return kwargs
        def launch_persistent_context(self, user_data_dir, **kwargs):
            return user_data_dir, kwargs
    assert launch_browser(Browser(), timeout=4) == {'timeout': 4, 'channel': 'chromium'}
    assert launch_persistent_browser(Browser(), '/synthetic/profile', headless=False) == ('/synthetic/profile', {'headless': False})
