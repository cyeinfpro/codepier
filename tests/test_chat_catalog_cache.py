"""Bounded native discovery, cache freshness, and concurrent read regression tests."""
import json
import os
import sys
import threading

import pytest

from agent.chat_catalog import CatalogCache, probe
from tests.test_native_cli import native


def test_cache_outlives_five_seconds_expires_and_returns_independent_values(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('agent.chat_catalog.time.monotonic', lambda: now[0])
    cache = CatalogCache()
    calls = []

    def loader():
        calls.append(1)
        return {'models': [{'id': 'native'}]}

    first = cache.get('catalog', loader)
    first['models'].clear()
    now[0] += 60
    assert cache.get('catalog', loader)['models'] == [{'id': 'native'}]
    assert len(calls) == 1
    now[0] += 241
    cache.get('catalog', loader)
    assert len(calls) == 2
    cache.get('catalog', loader, refresh=True)
    assert len(calls) == 3


@pytest.mark.parametrize('refresh', [False, True])
def test_concurrent_refresh_joins_probe_instead_of_starting_another(refresh):
    cache = CatalogCache()
    if refresh:
        cache.get('catalog', lambda: {'models': ['old']})
    entered, joined, release = (threading.Event() for _ in range(3))
    calls, results, failures = [], [], []

    def loader():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {'models': ['new']}

    def load():
        try:
            results.append(cache.get('catalog', loader, refresh=refresh))
        except Exception as exc:
            failures.append(exc)

    owner = threading.Thread(target=load)
    waiter = threading.Thread(target=load)
    owner.start()
    try:
        assert entered.wait(1)
        pending = cache.pending['catalog']
        wait = pending.wait

        def observed_wait(timeout):
            joined.set()
            return wait(timeout)

        pending.wait = observed_wait
        waiter.start()
        assert joined.wait(1)
    finally:
        release.set()
        owner.join(3)
        if waiter.ident is not None:
            waiter.join(3)
    assert not failures and not owner.is_alive() and not waiter.is_alive()
    assert calls == [1] and results == [{'models': ['new']}, {'models': ['new']}]
    assert not cache.pending


def test_failed_refresh_preserves_successful_cache_and_can_retry():
    cache = CatalogCache()
    cache.get('catalog', lambda: {'models': ['old']})

    def fail():
        raise ValueError('native offline')

    with pytest.raises(ValueError, match='offline'):
        cache.get('catalog', fail, refresh=True)
    assert not cache.pending
    assert cache.get('catalog', fail) == {'models': ['old']}
    assert cache.get('catalog', lambda: {'models': ['new']}, refresh=True) == {'models': ['new']}


@pytest.mark.parametrize('cli', ['pi', 'codex'])
def test_fast_catalog_skips_skills_but_keeps_native_model_and_effort(tmp_path, cli):
    script = tmp_path / 'native'
    script.write_text('#!' + sys.executable + '\n' + '''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 with open('wire','a') as f:f.write(line)
 if 'id' not in m:continue
 method=m.get('type',m.get('method'))
 assert method not in ('prompt','turn/start','thread/start')
 data={
  'get_state':{'model':{'id':'m','provider':'p'},'thinkingLevel':'low'},
  'get_available_models':{'models':[{'id':'m','provider':'p'}]},
  'get_available_thinking_levels':{'levels':['low','high']},
  'get_commands':{'commands':[{'name':'local-skill'}]},
  'model/list':{'data':[{'id':'m','model':'m','supportedReasoningEfforts':[{'reasoningEffort':'low'}]}]},
  'config/read':{'config':{'model':'m','model_reasoning_effort':'low','api_key':'secret'}},
  'skills/list':{'data':[{'skills':[{'name':'local-skill'}]}]},
 }.get(method,{})
 print(json.dumps({'id':m['id'],**({'type':'response','success':True,'data':data} if 'type' in m else {'result':data})}),flush=True)
''')
    script.chmod(0o700)
    result = probe(cli, str(script), tmp_path, dict(os.environ), include_commands=False)
    wire = [json.loads(line) for line in (tmp_path / 'wire').read_text().splitlines()]
    methods = [m.get('type', m.get('method')) for m in wire]
    assert result['models'] and result['thinking_levels']
    assert 'commands' not in result and 'secret' not in json.dumps(result)
    assert 'get_commands' not in methods and 'skills/list' not in methods
    result = probe(cli, str(script), tmp_path, dict(os.environ))
    assert result['commands'][0]['name'] == 'local-skill'
    if cli == 'pi':
        methods = [json.loads(line).get('type') for line in (tmp_path / 'wire').read_text().splitlines()]
        assert methods.index('get_available_thinking_levels') < methods.index('get_commands')


def test_agent_cache_separates_commands_defaults_models_and_environment(native, monkeypatch):
    obj, project = native
    calls = []

    def fake(cli, executable, cwd, env, model='', **kwargs):
        calls.append((str(cwd), model, kwargs['include_commands']))
        data = {'cli': cli, 'model': model or str(cwd), 'models': []}
        if kwargs['include_commands']:
            data['commands'] = [{'name': str(cwd)}]
        return data

    monkeypatch.setattr('agent.native_cli.probe', fake)
    args = {'cli': 'pi', 'include_commands': False}
    root = obj.action('chat_catalog', project, args)
    assert 'commands' not in root
    assert obj.action('chat_catalog', project, args) == root
    assert len(calls) == 1
    commands = obj.action('chat_catalog', project, {**args, 'include_commands': True})
    assert commands['commands'] and len(calls) == 2
    sub = obj.action('chat_catalog', project, {**args, 'cwd': 'sub'})
    assert sub['model'] != root['model'] and len(calls) == 3
    selected = obj.action('chat_catalog', project, {**args, 'model': 'p/other'})
    assert selected['model'] == 'p/other' and len(calls) == 4
    obj.agent.config['shell']['env']['CATALOG_TEST_CONFIG'] = 'changed'
    obj.action('chat_catalog', project, args)
    assert len(calls) == 5
    with pytest.raises(ValueError, match='boolean'):
        obj.action('chat_catalog', project, {**args, 'include_commands': 'false'})


def test_agent_rechecks_authorization_even_when_catalog_is_cached(native, monkeypatch):
    obj, project = native
    calls = []

    def fake(*args, **kwargs):
        calls.append(1)
        return {'models': []}

    monkeypatch.setattr('agent.native_cli.probe', fake)
    obj.action('chat_catalog', project, {'cli': 'pi', 'include_commands': False})
    project['allow_tasks'] = False
    with pytest.raises(Exception):
        obj.action('chat_catalog', project, {'cli': 'pi', 'include_commands': False})
    assert len(calls) == 1
