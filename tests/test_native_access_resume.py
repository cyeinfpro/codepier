"""The UI resume probe delegates to the unchanged native authority boundary."""
from tests.test_iam_integration import team, shared_role  # noqa: F401
from tests.test_roles import update_role, must


def test_native_resume_probe_requires_live_read_write_execute(team):
    app,browsers=team
    role=shared_role(app,browsers,['read','write','execute'])
    result=browsers['alice'].get('/api/native/access?project=project-team')
    assert result.status_code==200
    assert result.json()=={'allowed':True,'project_id':'project-team'}
    assert not app.state.runtime.native.pending
    must(update_role(browsers['owner'],role,project_rules=[{'actions':['read'],'all_projects':True}]))
    assert browsers['alice'].get('/api/projects').json()['projects']
    assert browsers['alice'].get('/api/native/access?project=project-team').status_code==403
    assert not app.state.runtime.native.pending


def test_native_resume_probe_rejects_other_space_and_disabled_mapping(team):
    app,browsers=team
    shared_role(app,browsers,['read','write','execute'])
    denied=browsers['alice'].get('/api/native/access?project=project-legacy')
    assert denied.status_code in (403,404)
    assert 'allowed' not in denied.json()
    app.state.store.execute("UPDATE projects SET allow_tasks=0 WHERE id='project-team'")
    assert browsers['alice'].get('/api/native/access?project=project-team').status_code==403
    assert not app.state.runtime.native.pending
