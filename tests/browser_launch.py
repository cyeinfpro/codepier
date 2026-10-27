"""Explicit launch policy for synthetic browser acceptance, never application runtime."""
import sys


def launch_options(engine, options=None, *, platform=None):
    """Keep caller options; select supported full Chromium headless on macOS only.

    Default macOS headless-shell can stop animation frames. The official Chromium
    channel retains real animation/actionability; no flags or assertions change.
    Explicit browser, channel and headed choices always win.
    """
    result = dict(options or {})
    if ((sys.platform if platform is None else platform) == 'darwin'
            and engine == 'chromium' and result.get('headless') is not False
            and 'channel' not in result and 'executable_path' not in result):
        result['channel'] = 'chromium'
    return result


def launch_browser(browser_type, **options):
    return browser_type.launch(**launch_options(browser_type.name, options))


def launch_persistent_browser(browser_type, user_data_dir, **options):
    return browser_type.launch_persistent_context(
        user_data_dir, **launch_options(browser_type.name, options))
