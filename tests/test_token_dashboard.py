"""Scoped retained summaries: privacy, missing history, timezone, and wire cost."""
from datetime import datetime
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from hub.token_dashboard import token_dashboard
from shared.util import DevError
from tests.test_audit_api import api  # noqa: F401
from tests.test_iam_integration import team  # noqa: F401
from tests.test_mcp_usage import call, materialize, principal


def read(app, token, **filters):
    return token_dashboard(app.state.runtime, principal(app, token), ZoneInfo("Asia/Shanghai"), **filters)


def test_overview_and_typed_api_expose_estimates_not_model_billing(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    dashboard = client.get('/api/overview').json()['token_usage']
    direct = client.get('/api/token-usage')
    assert direct.status_code == 200
    assert dashboard['summary'] == direct.json()['summary']
    assert dashboard['summary']['wire_attempts'] == 1
    assert dashboard['summary']['actual_usage'] is None
    assert dashboard['period']['timezone'] == client.get('/api/overview').json()['timezone']
    assert dashboard['coverage']['complete_history'] is False
    assert client.get('/api/token-usage?period=year').status_code == 422
    assert client.get('/api/token-usage?project=not-authorized').status_code == 404
    client.cookies.clear()
    assert client.get('/api/token-usage').status_code in {401, 403}


def test_connections_scoped_before_summary_and_options(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    p = principal(app, token)
    other = app.state.auth.issue_grant(replace(p, actor='panel:admin', admin=True, token_hash=''),
                                       'unrelated connection', ['read'], ['project'])['token']
    call(client, other, path='other.txt')
    mine = read(app, token)
    theirs = read(app, other)
    assert mine['summary']['wire_attempts'] == theirs['summary']['wire_attempts'] == 1
    assert mine['options']['connections'][0]['id'] == p.grant_id
    assert 'unrelated connection' not in str(mine)
    assert read(app, token, connection=principal(app, other).grant_id)['summary']['wire_attempts'] == 0
    app.state.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (p.grant_id,))
    with pytest.raises(DevError):
        token_dashboard(app.state.runtime, p, ZoneInfo('Asia/Shanghai'))


def test_project_remapping_and_other_space_are_excluded(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    assert read(app, token)['summary']['wire_attempts'] == 1
    app.state.store.execute("UPDATE projects SET root='/other' WHERE id='project'")
    assert read(app, token)['summary']['wire_attempts'] == 0
    app.state.store.execute("UPDATE projects SET root='/tmp/fixture' WHERE id='project'")
    with pytest.raises(DevError):
        token_dashboard(app.state.runtime, replace(principal(app, token), space_id='elsewhere'), ZoneInfo('UTC'))


def test_missing_history_not_zero_and_no_synthetic_time_buckets(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    call(client, token)
    ids = [row['id'] for row in app.state.store.all('SELECT id FROM mcp_activity ORDER BY id')]
    now = datetime(2026, 10, 9, 12, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    app.state.store.execute('UPDATE mcp_activity SET started=? WHERE id=?', (now - 3 * 3600, ids[0]))
    app.state.store.execute('UPDATE mcp_activity SET started=? WHERE id=?', (now - 3600, ids[1]))
    app.state.store.execute('DELETE FROM mcp_usage WHERE activity_id=?', (ids[0],))
    result = read(app, token, now=now)
    assert len(result['trend']) == 2  # No invented 10:00 zero bucket.
    assert result['trend'][0]['input']['estimated_tokens'] is None
    assert result['trend'][1]['input']['estimated_tokens'] > 0
    assert result['summary']['input']['unavailable_attempts'] == 1
    assert result['summary']['wire_attempts'] == 2
    assert result['summary']['distinct_server_operation_ids'] == 1  # Retried wire text still counts twice.
    assert result['period']['start'] == datetime(2026, 10, 9, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def test_window_filters_are_anonymous_read_only_and_retention_enforced(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    row = app.state.store.one('SELECT id FROM mcp_activity')
    app.state.store.execute('UPDATE mcp_activity SET window_key=? WHERE id=?', ('opaque-window', row['id']))
    with app.state.store.lock:
        changes = app.state.store.db.total_changes
        result = read(app, token, session='opaque-window')
        assert app.state.store.db.total_changes == changes
    assert result['summary']['wire_attempts'] == 1
    assert result['options']['sessions'][0] == {'id': 'opaque-window', 'label': '匿名窗口 1'}
    assert read(app, token, session='missing')['summary']['input']['estimated_tokens'] is None
    app.state.store.execute('UPDATE mcp_usage SET created=1')
    assert read(app, token)['summary']['input']['estimated_tokens'] is None
    assert app.state.store.one('SELECT COUNT(*) AS n FROM mcp_usage')['n'] == 1  # A GET never deletes data.


def test_empty_and_estimator_unavailable_are_explicit(api, monkeypatch):
    app, client, token = api
    assert read(app, token)['summary']['input']['estimated_tokens'] is None
    materialize(monkeypatch, app)
    call(client, token)
    monkeypatch.setattr(app.state.runtime.integrations, 'usage_metrics', None)
    result = read(app, token)
    assert result['coverage']['collection_unavailable'] is True
    assert result['summary']['input']['estimated_tokens'] is None


def test_panel_user_and_space_isolation_before_options_and_aggregate(team):
    from tests.test_iam_integration import credential, must, profile, shared_role
    import time
    app, browsers = team
    role = shared_role(app, browsers)
    project = app.state.store.one("SELECT * FROM projects WHERE id='project-team'")
    grants = {}
    for user in ('alice', 'bob'):
        selected = profile(browsers[user], role, user + ' profile')
        grant = must(credential(browsers[user], role, selected))
        grants[user] = grant['grant_id']
        app.state.store.execute('UPDATE grants SET label=? WHERE id=?', (user + ' private connection', grant['grant_id']))
        app.state.store.execute(
            'INSERT INTO mcp_activity(project,root,device,grant_id,actor,window_key,tool,started,status,transition,meaningful) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (project['id'], project['root'], project['device_id'], grant['grant_id'],
             'mcp:' + grant['grant_id'], user + '-window', 'read', time.time(), 'ok', '', 1))
    alice = must(browsers['alice'].get('/api/token-usage'))
    bob = must(browsers['bob'].get('/api/token-usage'))
    assert alice['summary']['wire_attempts'] == bob['summary']['wire_attempts'] == 1
    assert alice['authority_key'] != bob['authority_key']
    assert 'bob private connection' not in str(alice) and 'alice private connection' not in str(bob)
    assert grants['alice'] not in str(bob)
    other = must(browsers['bob'].get('/api/token-usage?connection=' + grants['alice']))
    assert other['summary']['wire_attempts'] == 0
    assert must(browsers['legacy'].get('/api/token-usage'))['summary']['wire_attempts'] == 0



def test_reference_cost_api_default_adjustable_and_authority_scoped(api, monkeypatch):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    original = client.get('/api/token-usage').json()
    summary = original['summary']
    cost = summary['reference_cost']
    assert original['schema_version'] == 3
    assert summary['total']['estimated_tokens'] == summary['input']['estimated_tokens'] + summary['output']['estimated_tokens']
    assert cost['amount_nano_usd'] == summary['input']['estimated_tokens'] * 50000 + summary['output']['estimated_tokens'] * 1900
    assert cost['pricing']['cache_read_percent'] == 90
    assert cost['actual_usage'] is None
    no_cache = client.get('/api/token-usage?cache_read_percent=0').json()
    assert no_cache['summary']['total'] == summary['total']
    assert no_cache['summary']['reference_cost']['amount_nano_usd'] == summary['input']['estimated_tokens'] * 50000 + summary['output']['estimated_tokens'] * 10000
    assert no_cache['trend'][0]['reference_cost']['pricing']['cache_read_percent'] == 0
    for value in ('-1', '101', '90.5', 'nan', 'bad'):
        assert client.get('/api/token-usage?cache_read_percent=' + value).status_code == 422
    excluded = client.get('/api/token-usage?connection=not-visible').json()
    assert excluded['summary']['reference_cost']['amount_nano_usd'] is None
    assert excluded['summary']['total']['estimated_tokens'] is None
    for invalid in (-1, 101, .5, True):
        with pytest.raises(DevError):
            read(app, token, cache_read_percent=invalid)


@pytest.mark.parametrize('model,input_rate,output_rate', [('gpt-6-astra', 1900, 50000), ('gpt-6.1-sol', 290, 10000), ('gpt-6-luna', 19, 500)])
def test_reference_model_api_filters_trends_and_no_store_mutation(api, monkeypatch, model, input_rate, output_rate):
    app, client, token = api
    materialize(monkeypatch, app)
    call(client, token)
    original = read(app, token)
    with app.state.store.lock:
        before = app.state.store.db.total_changes
        selected = read(app, token, reference_model=model)
        assert app.state.store.db.total_changes == before
    response = client.get('/api/token-usage', params={'reference_model': model})
    assert response.status_code == 200
    summary = selected['summary']
    assert summary['total'] == original['summary']['total']
    assert summary['reference_cost']['pricing']['model'] == model
    assert summary['reference_cost']['amount_nano_usd'] == (
        summary['input']['estimated_tokens'] * output_rate + summary['output']['estimated_tokens'] * input_rate)
    assert selected['trend'][0]['reference_cost']['pricing']['model'] == model
    assert selected['authority_key'] == original['authority_key']
    assert summary['reference_cost']['reasoning_tokens'] is None
    assert summary['reference_cost']['cache_write_tokens'] is None
    assert client.get('/api/token-usage?reference_model=unknown').status_code == 400
    assert client.get('/api/token-usage?reference_model=' + 'x' * 65).status_code == 422
