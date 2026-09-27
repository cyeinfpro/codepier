import pytest
from tests.support import running_stack

@pytest.fixture(scope='module')
def stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('real-hub-agent')) as s:
        yield s


def pytest_addoption(parser):
    parser.addoption('--chat-browser-reuse', action='store_true', default=False,
                     help='Reuse engines in a synchronous UI-only batch; contexts remain isolated.')


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    from tests.classification import REGRESSION_EXCLUSIVE, WINDOWS_CORE, module_tiers
    for item in items:
        browser, integration = module_tiers(str(item.path))
        browser = browser or 'chat_browser_pool' in item.fixturenames
        integration = integration or 'stack' in item.fixturenames
        if browser:
            item.add_marker(pytest.mark.browser)
        if integration:
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.slow)
        if item.path.name in WINDOWS_CORE:
            item.add_marker(pytest.mark.windows_core)
        if item.path.name in REGRESSION_EXCLUSIVE:
            item.add_marker(pytest.mark.serial_regression)
    if config.getoption('--chat-browser-reuse'):
        import inspect
        if any(item.get_closest_marker('asyncio') or item.get_closest_marker('anyio')
               or inspect.iscoroutinefunction(getattr(item, 'obj', None)) for item in items):
            raise pytest.UsageError('--chat-browser-reuse is only for synchronous UI batches; run async tests separately.')


def _chat_browser_scope(fixture_name, config):
    return 'session' if config.getoption('--chat-browser-reuse') else 'function'


@pytest.fixture(scope=_chat_browser_scope)
def chat_browser_pool():
    """Fresh contexts per test; engine reuse is explicit and synchronous-only.

    Default runs close the sync driver after each test so mixed asyncio suites
    keep working. The opt-in batch rejects async items before starting a driver.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browsers = {}
        def get_browser(kind):
            if kind not in browsers or not browsers[kind].is_connected():
                browsers[kind] = getattr(pw, kind).launch()
            return browsers[kind]
        try:
            yield get_browser
        finally:
            for browser in browsers.values():
                browser.close()
