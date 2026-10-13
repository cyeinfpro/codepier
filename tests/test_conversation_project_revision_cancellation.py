"""Cancellation admission tests use only synthetic queued/running operation rows."""
import pytest

from shared.util import DevError

from tests.collaboration_support import collab as collab
from tests.test_conversation_project_revision import (
    fixture, select, task, goal, initial_work, work, claim, admit,
)


def running_operation(c, task_value, item):
    lease = claim(c, task_value, item)
    admitted = admit(c, task_value, lease)
    identifier = admitted['operation_id']
    # Simulate an already-admitted transport attempt; no Agent/process is started.
    c.s.store.execute("UPDATE operations SET state='running',attempts=1 WHERE id=?", (identifier,))
    return identifier, lease


def read_op(c, identifier):
    return c.s.store.one('SELECT * FROM operations WHERE id=?', (identifier,))


def test_same_goal_removal_fences_all_steps_but_cancels_only_removed_project_operation(collab):
    c = fixture(collab)
    select(c, ['proj', 'otherproj'])
    current = task(c)
    removed_op, removed_lease = running_operation(c, current, work(c, current, 'otherproj'))
    retained_op, retained_lease = running_operation(c, current, work(c, current, 'proj'))
    retained_before = read_op(c, retained_op)
    select(c, ['proj'])
    assert goal(c, current)['state'] == 'paused'
    removed = read_op(c, removed_op)
    assert removed['cancel_requested'] == 1 and removed['state'] == 'cancelling'
    assert read_op(c, retained_op) == retained_before
    for lease in (removed_lease, retained_lease):
        row = c.s.store.one('SELECT * FROM coordination_work WHERE id=?', (lease['id'],))
        assert row['fencing_token'] > lease['fencing_token']
        assert row['lease_until'] is None
        with pytest.raises(DevError):
            admit(c, current, lease)
    assert len(c.s.store.all('SELECT * FROM operations')) == 2


def test_removing_other_project_leaves_separate_existing_goal_lease_and_operation_unchanged(collab):
    c = fixture(collab)
    retained = task(c)  # Original immutable project-only goal.
    retained_op, retained_lease = running_operation(c, retained, initial_work(c, retained))
    retained_goal = goal(c, retained)
    retained_work = c.s.store.one('SELECT * FROM coordination_work WHERE id=?', (retained_lease['id'],))
    retained_operation = read_op(c, retained_op)
    select(c, ['proj', 'otherproj'])
    affected = task(c)
    removed_op, _ = running_operation(c, affected, work(c, affected, 'otherproj'))
    select(c, ['proj'])
    assert goal(c, retained) == retained_goal
    assert c.s.store.one('SELECT * FROM coordination_work WHERE id=?', (retained_lease['id'],)) == retained_work
    assert read_op(c, retained_op) == retained_operation
    assert goal(c, affected)['state'] == 'paused'
    assert read_op(c, removed_op)['cancel_requested'] == 1
