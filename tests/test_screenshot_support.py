"""Screenshot diagnostics preserve the original failure and capture contract."""
import subprocess
import traceback

import pytest

from playwright.sync_api import Error, TimeoutError

from tests.screenshot_support import _PAINTED_PAGE_READY, capture_painted_page, capture_route_screenshot


@pytest.mark.parametrize('failure', [False, True])
def test_route_screenshot_keeps_single_full_page_capture_and_context(tmp_path, failure):
    calls = []
    error = RuntimeError('Protocol error (Page.captureScreenshot): Unable to capture screenshot')
    error.add_note('existing browser detail')
    image = b'synthetic PNG result'

    class Page:
        def screenshot(self, **options):
            calls.append(options)
            if failure:
                raise error
            return image

    path = tmp_path / 'identity-390-light.png'

    def capture():
        return capture_route_screenshot(
            Page(), path, route='identity', scheme='light',
            viewport=(390, 844), document_size=(390, 1720))

    if failure:
        with pytest.raises(RuntimeError) as caught:
            capture()
        assert caught.value is error
        assert str(caught.value) == 'Protocol error (Page.captureScreenshot): Unable to capture screenshot'
        assert error.__notes__ == [
            'existing browser detail',
            'Route screenshot context: route=identity; scheme=light; viewport=390x844; document=390x1720',
        ]
        assert traceback.extract_tb(error.__traceback__)[-1].name == 'screenshot'
    else:
        assert capture() is image
    assert calls == [{'path': str(path), 'full_page': True}]


@pytest.mark.parametrize('failure', [None, 'readiness', 'screenshot'])
def test_painted_capture_preserves_order_single_capture_and_original_error(tmp_path, failure):
    calls = []
    image = b'synthetic complete PNG'
    error = (TimeoutError('paint readiness timed out') if failure == 'readiness' else
             Error('Protocol error (Page.captureScreenshot): Unable to capture screenshot'))
    error.add_note('existing browser detail')
    context = '{"route":"overview","viewport":[320,568],"document":[320,1607],"visibility":"visible"}'

    class Handle:
        def json_value(self):
            return context

        def dispose(self):
            calls.append(('dispose',))

    class Page:
        viewport_size = {'width': 320, 'height': 568}

        def wait_for_function(self, expression, **options):
            calls.append(('wait', options))
            if options['timeout'] == 1000:
                return Handle()
            assert expression == _PAINTED_PAGE_READY
            if failure == 'readiness':
                raise error

        def screenshot(self, **options):
            calls.append(('screenshot', options))
            if failure == 'screenshot':
                raise error
            return image

    path = tmp_path / 'light-320-overview.png'
    expected = [('wait', {'arg': Page.viewport_size, 'timeout': 5000})]
    if failure != 'readiness':
        expected.append(('screenshot', {
            'path': str(path), 'full_page': True, 'animations': 'disabled'}))
    if failure:
        with pytest.raises(type(error)) as caught:
            capture_painted_page(Page(), path)
        assert caught.value is error
        assert error.__notes__ == [
            'existing browser detail',
            f"Painted screenshot context: path='light-320-overview.png'; "
            f"viewport={Page.viewport_size}; page={context}",
        ]
        origin = 'wait_for_function' if failure == 'readiness' else 'screenshot'
        assert traceback.extract_tb(error.__traceback__)[-1].name == origin
        expected.extend([('wait', {'polling': 100, 'timeout': 1000}), ('dispose',)])
    else:
        assert capture_painted_page(Page(), path) is image
    assert calls == expected


@pytest.mark.parametrize('failure', ['wait', 'json_value', 'dispose'])
def test_painted_capture_diagnostic_failure_cannot_replace_original_error(tmp_path, failure):
    error = Error('original screenshot protocol failure')
    calls = []

    class Handle:
        def json_value(self):
            if failure == 'json_value':
                raise RuntimeError('diagnostic value failed')
            return '{}'

        def dispose(self):
            if failure == 'dispose':
                raise RuntimeError('diagnostic disposal failed')

    class Page:
        viewport_size = {'width': 390, 'height': 844}

        def wait_for_function(self, expression, **options):
            calls.append(('wait', options))
            if options['timeout'] == 1000:
                if failure == 'wait':
                    raise TimeoutError('diagnostic timed out')
                return Handle()

        def screenshot(self, **options):
            calls.append(('screenshot', options))
            raise error

    with pytest.raises(Error) as caught:
        capture_painted_page(Page(), tmp_path / 'route.png')
    assert caught.value is error
    assert str(error) == 'original screenshot protocol failure'
    assert error.__notes__[0].endswith('page=unavailable')
    assert [row[0] for row in calls] == ['wait', 'screenshot', 'wait']
    assert calls[-1][1] == {'polling': 100, 'timeout': 1000}


def test_painted_readiness_requires_fonts_two_frames_and_actual_visible_viewport():
    script = """const assert = require('node:assert/strict');
      const ready = eval('(' + process.argv[1] + ')');
      const tick = () => new Promise(setImmediate);
      async function check(visibility, width, height, expected) {
        let fontsReady, settled = false;
        const frames = [];
        global.document = {visibilityState: visibility,
          fonts: {ready: new Promise(resolve => {fontsReady = resolve;})}};
        global.innerWidth = width; global.innerHeight = height;
        global.requestAnimationFrame = callback => frames.push(callback);
        const promise = ready({width: 320, height: 568}).then(value => {
          settled = true; return value;
        });
        await tick();
        assert.equal(settled, false); assert.equal(frames.length, 0);
        fontsReady(); await tick();
        assert.equal(settled, false); assert.equal(frames.length, 1);
        frames.shift()(); await tick();
        assert.equal(settled, false); assert.equal(frames.length, 1);
        frames.shift()();
        assert.equal(await promise, expected);
        assert.equal(frames.length, 0);
      }
      (async () => {
        await check('visible', 320, 568, true);
        await check('hidden', 320, 568, false);
        await check('visible', 390, 568, false);
        await check('visible', 320, 844, false);
      })().catch(error => {console.error(error); process.exitCode = 1;});
    """
    result = subprocess.run(
        ['node', '-e', script, _PAINTED_PAGE_READY],
        capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


def test_old_full_page_capture_import_uses_shared_helper():
    from tests.test_ui_redesign import capture_painted_page as legacy_capture
    assert legacy_capture is capture_painted_page
