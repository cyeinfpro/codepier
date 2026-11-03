"""Failure-only browser startup context without retries or caller-data disclosure."""
import traceback

import pytest

from tests import browser_launch


@pytest.mark.parametrize('persistent', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'failure', 'interrupt'])
def test_launch_diagnostics_preserve_call_and_exception(monkeypatch, persistent, outcome):
    monkeypatch.setattr(browser_launch.sys, 'platform', 'darwin')
    ticks = iter([10.0, 190.125])
    monkeypatch.setattr(browser_launch, 'monotonic', lambda: next(ticks))
    failure = RuntimeError('original failure')
    failure.add_note('existing note')
    interrupt = KeyboardInterrupt('stop')
    result = object()
    calls = []

    class Browser:
        name = 'chromium'

        def launch(self, *args, **kwargs):
            calls.append((args, kwargs))
            if outcome == 'failure':
                raise failure
            if outcome == 'interrupt':
                raise interrupt
            return result

        launch_persistent_context = launch

    options = {'headless': False, 'timeout': 456, 'args': ['--synthetic'],
               'env': {'SYNTHETIC_SECRET': 'private'}}
    before = dict(options)

    def invoke():
        if persistent:
            return browser_launch.launch_persistent_browser(Browser(), '/private/profile', **options)
        return browser_launch.launch_browser(Browser(), **options)

    if outcome == 'success':
        assert invoke() is result
    elif outcome == 'interrupt':
        with pytest.raises(KeyboardInterrupt) as caught:
            invoke()
        assert caught.value is interrupt
        assert not hasattr(interrupt, '__notes__')
    else:
        with pytest.raises(RuntimeError) as caught:
            invoke()
        assert caught.value is failure
        assert str(failure) == 'original failure'
        assert failure.__notes__[0] == 'existing note'
        assert len(failure.__notes__) == 2
        note = failure.__notes__[1]
        assert 'elapsed_seconds=180.125' in note
        assert 'engine=chromium; platform=darwin' in note
        assert 'mode=' + ('persistent' if persistent else 'browser') in note
        assert 'headless=false; channel=default; executable=default; timeout=explicit' in note
        assert 'private' not in note
        assert traceback.extract_tb(failure.__traceback__)[-1].name == 'launch'
    assert calls == [(('/private/profile',) if persistent else (), before)]
    assert options == before


@pytest.mark.parametrize('persistent', [False, True])
def test_launch_diagnostics_are_bounded_and_redact_untrusted_options(monkeypatch, persistent):
    monkeypatch.setattr(browser_launch.sys, 'platform', 'darwin')
    ticks = iter([0.0, 1.25])
    monkeypatch.setattr(browser_launch, 'monotonic', lambda: next(ticks))
    secret = 'DO_NOT_REPORT' * 1000
    error = RuntimeError('failure')

    class Browser:
        name = secret

        def launch(self, *args, **kwargs):
            raise error

        launch_persistent_context = launch

    options = dict(channel=secret, executable_path=secret, args=[secret],
                   env={'secret': secret}, proxy={'server': secret},
                   timeout=123, headless=True)
    with pytest.raises(RuntimeError) as caught:
        if persistent:
            browser_launch.launch_persistent_browser(Browser(), secret, **options)
        else:
            browser_launch.launch_browser(Browser(), **options)
    assert caught.value is error
    note, = error.__notes__
    assert len(note) < 300
    assert 'DO_NOT_REPORT' not in note
    assert 'engine=other' in note
    assert 'headless=true; channel=explicit; executable=explicit; timeout=explicit' in note


def test_launch_diagnostics_show_effective_macos_policy(monkeypatch):
    monkeypatch.setattr(browser_launch.sys, 'platform', 'darwin')
    ticks = iter([0.0, 0.5])
    monkeypatch.setattr(browser_launch, 'monotonic', lambda: next(ticks))
    calls = []

    class Browser:
        name = 'chromium'

        def launch(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError('failed')

    with pytest.raises(RuntimeError) as caught:
        browser_launch.launch_browser(Browser())
    assert calls == [{'channel': 'chromium'}]
    assert 'headless=default; channel=chromium' in caught.value.__notes__[0]
