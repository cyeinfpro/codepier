"""Only temporary databases and synthetic dependency metadata."""
import pytest

from hub.operation_history import retention_plan
from hub.store import Store
from scripts.check_dependencies import differences, expected_versions
from scripts.maintain_history import run
from tests.test_history_retention_execution import insert, NOW


def test_dependency_check_applies_markers_and_reports_missing_or_drift():
    pins = expected_versions("""Example_Package==2.0 \\
    --hash=sha256:fixture
missing==1.0
foreign==3.0 ; sys_platform == 'nonexistent-os'
""")
    assert pins == {'example-package': '2.0', 'missing': '1.0'}
    assert differences(pins, {'example.package': '1.0'}) == [
        'example-package: expected 2.0, installed 1.0',
        'missing: expected 1.0, installed missing',
    ]


@pytest.mark.parametrize('text', ['package>=2', 'package==2.*', '# empty'])
def test_dependency_check_rejects_nonlocks(text):
    with pytest.raises(ValueError):
        expected_versions(text)


def test_missing_database_preview_never_creates_state(tmp_path):
    missing = tmp_path / 'absent'
    with pytest.raises(ValueError):
        run(missing)
    assert not missing.exists()


def test_history_preview_and_execution_share_selection_and_protection(tmp_path):
    directory = tmp_path / 'hub'
    store = Store(directory)
    try:
        insert(store, 'free')
        insert(store, 'protected')
        insert(store, 'active', state='running')
        store.execute("""INSERT INTO artifacts
            (id,project_id,device_id,actor,root,name,bytes,sha256,created,expires,source_operation_id)
            VALUES ('fixture','project','device','fixture','/fixture','file',1,'hash',1,2,'protected')""")
        before = store.all('SELECT * FROM operations ORDER BY id')
        preview = run(directory, now=NOW)
        plan = retention_plan(store, now=NOW)
        assert preview == {key: value for key, value in plan.items()
                           if key not in {'operations', 'automatic_delete_allowed', 'approval_required'}}
        assert preview['operation_bodies'] == 1
        assert preview['candidate_body_bytes'] > 0
        assert preview['protected_by_reference'] == 1
        assert preview['protected_by_reason']['artifact'] == 1
        assert store.all('SELECT * FROM operations ORDER BY id') == before
        result = run(directory, apply=True, now=NOW)
        assert result['operation_records'] == preview['operation_records']
        assert result['protected_by_reason'] == preview['protected_by_reason']
        assert store.one("SELECT history_pruned_at FROM operations WHERE id='protected'")['history_pruned_at'] is None
        assert run(directory, apply=True, now=NOW)['operation_bodies'] == 0
    finally:
        store.close()
