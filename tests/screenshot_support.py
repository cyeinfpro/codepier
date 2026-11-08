"""Single-attempt screenshots with synthetic layout context on failure."""
from pathlib import Path


_PAINTED_PAGE_READY = '''async expected => {
  await document.fonts.ready;
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return document.visibilityState === 'visible' &&
    innerWidth === expected.width && innerHeight === expected.height;
}'''

_SCREENSHOT_CONTEXT = '''() => {
  const d = document.documentElement, b = document.body;
  return JSON.stringify({
    route: (document.querySelector('#page')?.dataset.page || '').slice(0, 80),
    viewport: [innerWidth, innerHeight], visibility: document.visibilityState,
    document: d && b ? [
      Math.max(d.scrollWidth, d.offsetWidth, d.clientWidth, b.scrollWidth, b.offsetWidth, b.clientWidth),
      Math.max(d.scrollHeight, d.offsetHeight, d.clientHeight, b.scrollHeight, b.offsetHeight, b.clientHeight)
    ] : null
  });
}'''


def _screenshot_failure_context(page):
    # A hidden/stalled renderer must not turn error reporting into an unbounded wait.
    # Return a primitive string: json_value reads its cached value, not page JavaScript.
    try:
        handle = page.wait_for_function(_SCREENSHOT_CONTEXT, polling=100, timeout=1000)
        try:
            return str(handle.json_value())[:700]
        finally:
            handle.dispose()
    except Exception:
        return 'unavailable'


def capture_painted_page(page, path):
    """Wait for visible frames at the requested size, then capture exactly once."""
    viewport = page.viewport_size
    try:
        # DOM visibility and loaded fonts alone do not establish a capture frame.
        # Keep the original bounded readiness check and complete screenshot contract.
        page.wait_for_function(_PAINTED_PAGE_READY, arg=viewport, timeout=5000)
        return page.screenshot(path=str(path), full_page=True, animations='disabled')
    except Exception as error:
        BaseException.add_note(error,
            f'Painted screenshot context: path={Path(path).name[:120]!r}; '
            f'viewport={viewport}; page={_screenshot_failure_context(page)}')
        raise


def capture_route_screenshot(page, path, *, route, scheme, viewport, document_size):
    """Keep full-page capture and its original error; never retry or downgrade."""
    try:
        return page.screenshot(path=str(path), full_page=True)
    except Exception as error:
        BaseException.add_note(error,
            f'Route screenshot context: route={route}; scheme={scheme}; '
            f'viewport={viewport[0]}x{viewport[1]}; '
            f'document={document_size[0]}x{document_size[1]}')
        raise
