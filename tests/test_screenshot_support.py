"""Screenshot diagnostics preserve the original failure and capture contract."""
import traceback

import pytest

from tests.screenshot_support import capture_route_screenshot


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
