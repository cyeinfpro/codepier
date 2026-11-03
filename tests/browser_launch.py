"""Explicit launch policy for synthetic browser acceptance, never application runtime."""
import sys
from time import monotonic


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


def _launch_with_diagnostics(browser_type, method, args, options):
    """Annotate failures without probing processes or exposing caller data."""
    effective = launch_options(browser_type.name, options)
    started = monotonic()
    try:
        return method(*args, **effective)
    except Exception as error:
        # Public Playwright launch APIs expose no owned PID on failure. Do not
        # infer ownership from error text or inspect unrelated host processes.
        elapsed = monotonic() - started
        engine = browser_type.name
        engine = engine if type(engine) is str and engine in ('chromium', 'webkit', 'firefox') else 'other'
        platform = sys.platform if sys.platform in ('darwin', 'linux', 'win32') else 'other'
        channel = effective.get('channel')
        channel = channel if type(channel) is str and channel in ('chromium', 'chrome', 'msedge') else (
            'explicit' if 'channel' in effective else 'default')
        headless = 'false' if effective.get('headless') is False else (
            'true' if effective.get('headless') is True else 'default')
        # Values outside these small allowlists (paths, environment, proxy,
        # arguments, arbitrary channel names) are deliberately never formatted.
        BaseException.add_note(error,
            f'Browser launch diagnostics: engine={engine}; platform={platform}; '
            f'mode={"persistent" if args else "browser"}; elapsed_seconds={elapsed:.3f}; '
            f'headless={headless}; channel={channel}; '
            f'executable={"explicit" if "executable_path" in effective else "default"}; '
            f'timeout={"explicit" if "timeout" in effective else "default"}')
        raise


def launch_browser(browser_type, **options):
    return _launch_with_diagnostics(browser_type, browser_type.launch, (), options)


def launch_persistent_browser(browser_type, user_data_dir, **options):
    return _launch_with_diagnostics(
        browser_type, browser_type.launch_persistent_context, (user_data_dir,), options)
