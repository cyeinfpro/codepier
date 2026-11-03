"""Only actual waiting transitions produce durable diagnostic writes."""
import asyncio
import sys

import pytest

from agent.journal import Journal
from agent.telemetry import AgentTelemetry
from tests.test_audit_agent import local_agent


def test_repeated_wait_preserves_first_timestamp_and_only_writes_transitions(tmp_path):
    journal = Journal(tmp_path / 'journal')
    try:
        telemetry = AgentTelemetry(journal)
        assert telemetry.record('op', 'waiting_resource', blocked_by=['writer'],
                                queue_reason='resource_conflict', lane='read', queue_age_ms=0)
        before = journal.db.total_changes
        first = telemetry.snapshot('op')[0]
        for age in range(1, 100):
            assert not telemetry.record('op', 'waiting_resource', blocked_by=['writer'],
                                        queue_reason='resource_conflict', lane='read', queue_age_ms=age)
        assert journal.db.total_changes == before
        assert telemetry.snapshot('op') == [first]
        assert telemetry.record('op', 'waiting_resource', blocked_by=['another-writer'],
                                queue_reason='resource_conflict', lane='read', queue_age_ms=100)
        assert telemetry.record('op', 'waiting_worker', queue_reason='lane_capacity', lane='read')
        assert telemetry.record('op', 'waiting_resource', blocked_by=['another-writer'],
                                queue_reason='resource_conflict', lane='read', queue_age_ms=200)
        assert [item['seq'] for item in telemetry.snapshot('op')] == [1, 2, 3, 4]
        assert telemetry.record('op', 'result_ready')
        assert not telemetry.starts and not telemetry._last
    finally:
        journal.db.close()


def test_diagnostic_write_failure_is_observable_and_can_retry_same_transition(tmp_path):
    journal = Journal(tmp_path / 'journal')
    try:
        telemetry = AgentTelemetry(journal)
        journal.db.execute("CREATE TRIGGER reject_trace BEFORE INSERT ON execution_stages "
                           "BEGIN SELECT RAISE(ABORT, 'disk fixture'); END")
        assert not telemetry.record('op', 'waiting_worker', queue_reason='lane_capacity', lane='read')
        assert telemetry.errors == 1
        journal.db.execute('DROP TRIGGER reject_trace')
        assert telemetry.record('op', 'waiting_worker', queue_reason='lane_capacity', lane='read')
        assert len(telemetry.snapshot('op')) == 1
    finally:
        journal.db.close()


@pytest.mark.asyncio
async def test_unchanged_output_tail_is_not_written_again_and_final_output_is_durable(local_agent, monkeypatch):
    agent, root = local_agent
    agent.journal.start('chatty', {'tool': 'tasks_run'})
    snapshots = []
    update = agent.journal.update_output
    def counted(identifier, text, sequence):
        snapshots.append((sequence, text))
        update(identifier, text, sequence)
    monkeypatch.setattr(agent.journal, 'update_output', counted)
    command = 'import sys,time;\nfor _ in range(3):\n sys.stdout.write("x"*200000); sys.stdout.flush(); time.sleep(.35)'
    result = await asyncio.wait_for(agent.run_process('chatty', [sys.executable, '-u', '-c', command],
                                                     root, 5, stream=True), 7)
    assert result['exit_code'] == 0 and not result['timed_out']
    assert result['output'] == 'x' * 131072 and result['output_truncated']
    assert snapshots and all(left[1] != right[1] for left, right in zip(snapshots, snapshots[1:]))
    assert [sequence for sequence, _ in snapshots] == list(range(1, len(snapshots)+1))
    saved = agent.journal.status('chatty')
    assert saved['output'] == result['output'] and saved['output_seq'] == snapshots[-1][0]
    assert not agent.processes
