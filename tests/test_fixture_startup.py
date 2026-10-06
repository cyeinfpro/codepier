"""The fixture startup observer is bounded and never hides a dead child."""
from types import SimpleNamespace

import httpx
import pytest

from tests import support


@pytest.fixture
def clock(monkeypatch):
    state = {'now': 0.0, 'sleeps': 0}

    def sleep(delay):
        state['now'] += delay
        state['sleeps'] += 1

    monkeypatch.setattr(support, 'time',
                        SimpleNamespace(monotonic=lambda: state['now'], sleep=sleep))
    return state


def test_agent_observer_accepts_the_same_live_child(clock):
    process = SimpleNamespace(poll=lambda: None)
    calls = []

    def online():
        calls.append(True)
        return len(calls) == 4

    support.wait_for_agent(process, online, timeout=1)
    assert len(calls) == 4 and clock['sleeps'] == 3


@pytest.mark.parametrize('code', [0, 7, -15])
def test_agent_observer_reports_early_exit_without_wait_or_restart(clock, code):
    def forbidden():
        raise AssertionError('online probe should not run for a dead child')

    with pytest.raises(AssertionError, match=f'Fixture Agent exited with code {code}'):
        support.wait_for_agent(SimpleNamespace(poll=lambda: code), forbidden)
    assert clock['sleeps'] == 0


def test_agent_observer_deadline_is_finite(clock):
    with pytest.raises(AssertionError, match='did not become online within 0.3s'):
        support.wait_for_agent(SimpleNamespace(poll=lambda: None), lambda: False, timeout=.3)
    assert clock['now'] < .5 and clock['sleeps'] == 3


def test_agent_observer_can_recover_a_transient_probe_error_without_restarting(clock):
    attempts = []

    def online():
        attempts.append(True)
        if len(attempts) == 1:
            raise httpx.ConnectError('local fixture connection not ready')
        return True

    support.wait_for_agent(SimpleNamespace(poll=lambda: None), online, timeout=1)
    assert len(attempts) == 2 and clock['sleeps'] == 1


@pytest.mark.parametrize('source,expected', [
    ('from tests.support import running_stack', (False, True)),
    ('from tests.support import Stack as LocalStack', (False, True)),
    ('from tests.support import wait_for', (False, False)),
    ('from pathlib import Path', (False, False)),
])
def test_shared_real_stack_uses_integration_pool_without_marking_plain_helpers(tmp_path, source, expected):
    from tests.classification import module_tiers
    module = tmp_path / 'classification_fixture.py'
    module.write_text(source + '\n')
    assert module_tiers(str(module)) == expected
