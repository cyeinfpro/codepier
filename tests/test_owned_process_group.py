"""An unavailable process survey is not proof of either cleanup or an orphan."""
from types import SimpleNamespace
import signal
import subprocess

import pytest

from agent import owned_process_group as groups


@pytest.fixture
def owned_child(monkeypatch):
    now = [0.0]
    signals, reaps = [], []
    monkeypatch.setattr(groups, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    monkeypatch.setattr(groups, 'child_exited', lambda pid: True)
    monkeypatch.setattr(groups.os, 'killpg', lambda pid, number: signals.append((pid, number)))
    monkeypatch.setattr(groups.os, 'waitpid', lambda pid, flags: (reaps.append((pid, flags)) or (pid, 0)))
    return signals, reaps, now


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired('ps', 1), ProcessLookupError(), PermissionError()])
def test_transient_survey_failure_keeps_identity_until_confirmed_empty(owned_child, monkeypatch, error):
    signals, reaps, _ = owned_child
    calls = []

    def survey(pid, **kwargs):
        assert not reaps, 'Do not release process identity before checking descendants'
        calls.append(pid)
        if len(calls) == 1:
            raise error
        return []

    monkeypatch.setattr(groups, 'live_group_members', survey)
    assert groups.stop_owned_group(4242, grouped=True) == (True, 0)
    assert len(calls) == 2
    assert reaps == [(4242, 0)]
    assert len(signals) <= 2, 'Only observations may repeat, not the stop command'
    assert all(number in {signal.SIGTERM, signal.SIGKILL} for _, number in signals)


def test_failed_surveys_remain_unconfirmed_and_bounded(owned_child, monkeypatch):
    signals, _, now = owned_child

    def unavailable(pid, **kwargs):
        raise subprocess.TimeoutExpired('ps', 1)

    monkeypatch.setattr(groups, 'live_group_members', unavailable)
    clean, _ = groups.stop_owned_group(4242, grouped=True, grace=0, kill_timeout=.1)
    assert not clean
    assert now[0] <= .2
    assert len(signals) <= 2


def test_live_descendant_after_zombie_eperm_never_counts_as_cleanup(owned_child, monkeypatch):
    signals, _, _ = owned_child

    def denied(pid, number):
        signals.append((pid, number))
        raise PermissionError()

    monkeypatch.setattr(groups.os, 'killpg', denied)
    monkeypatch.setattr(groups, 'live_group_members', lambda pid, **kwargs: [4243])
    clean, _ = groups.stop_owned_group(4242, grouped=True, grace=0, kill_timeout=.1)
    assert not clean


def test_darwin_zombie_eperm_is_confirmed_by_survey(owned_child, monkeypatch):
    def denied(pid, number):
        raise PermissionError()

    monkeypatch.setattr(groups.os, 'killpg', denied)
    monkeypatch.setattr(groups, 'live_group_members', lambda pid, **kwargs: [])
    assert groups.stop_owned_group(4242, grouped=True) == (True, 0)


def test_reaped_or_foreign_pid_never_receives_signal(owned_child, monkeypatch):
    signals, _, _ = owned_child

    def not_our_child(*args):
        raise ChildProcessError()

    monkeypatch.setattr(groups, 'child_exited', not_our_child)
    monkeypatch.setattr(groups.os, 'waitpid', not_our_child)
    assert groups.stop_owned_group(4242, grouped=True) == (False, None)
    assert signals == []
