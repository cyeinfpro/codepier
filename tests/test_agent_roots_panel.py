"""Directory grants: real config writes with isolated node paths and failures."""
import errno
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError

from agent.config import validate_config
from agent.roots_panel import call
from shared.agent_roots_contracts import RootsChange, PANEL_ROOTS_TOOLS
from shared.contracts import tool_definitions
from shared.tool_protocol import PeerContract, require_compatible, validate_call_epoch
from shared.util import DevError


@pytest.fixture
def roots_agent(tmp_path):
    root = tmp_path / 'original-project'; root.mkdir()
    config_dir = tmp_path / 'node'; config_dir.mkdir()
    path = config_dir / 'config.json'
    raw = {'hub_url': 'http://127.0.0.1:8000', 'device_id': 'roots-fixture',
           'secret': 'synthetic-only-' + 'a' * 40, 'name': 'fixture',
           'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}],
           'shell': {'enabled': False}, 'custom_preserved': {'value': 'keep'}}
    path.write_text(json.dumps(raw))
    return SimpleNamespace(config_path=path, config=validate_config(raw, path), runtime_reload_pending=False)


def identity(agent, **overrides):
    return {'id': None, 'alias': '', 'root': '', '_roots_admin': True,
            '_integration_admin': True, '_execution_policy': {'origin': 'panel'},
            '_coding_device': agent.config['device_id'], **overrides}


def get(agent):
    return call(agent, 'agent_roots_get', identity(agent), {'device_id': agent.config['device_id']})


def arguments(agent, path, action='upsert', **changes):
    return {'device_id': agent.config['device_id'], 'action': action, 'path': str(path),
            'writable': True, 'allow_tasks': True,
            'expected_sha256': get(agent)['config_sha256'], 'idempotency_key': uuid.uuid4().hex, **changes}


def reload(agent):
    agent.config = validate_config(json.loads(agent.config_path.read_bytes()), agent.config_path)


def test_preview_save_backup_reload_and_single_entry_update(roots_agent, tmp_path):
    agent = roots_agent
    target = tmp_path / 'network-share'; target.mkdir()
    original = agent.config_path.read_bytes()
    before = json.loads(original)
    body = arguments(agent, target)
    preview = call(agent, 'agent_roots_preview', identity(agent), body)
    assert preview['valid'] and not preview['saved'] and preview['preview']['exists']
    assert agent.config_path.read_bytes() == original
    result = call(agent, 'agent_roots_set', identity(agent), body)
    assert result['saved'] and result['state'] == 'pending_reload'
    assert (agent.config_path.parent / result['backup_ref']).read_bytes() == original
    assert result['config_sha256'] == hashlib.sha256(agent.config_path.read_bytes()).hexdigest()
    data = json.loads(agent.config_path.read_bytes())
    expected = before['allowed_roots'] + [{'path': str(target.resolve()), 'writable': True, 'allow_tasks': True}]
    assert data.pop('allowed_roots') == expected
    before.pop('allowed_roots'); assert data == before
    assert agent.config['secret'] not in json.dumps(result)
    reload(agent); assert get(agent)['state'] == 'applied'
    downgraded = arguments(agent, target, writable=False, allow_tasks=False)
    call(agent, 'agent_roots_set', identity(agent), downgraded)
    latest = json.loads(agent.config_path.read_bytes())['allowed_roots']
    assert len(latest) == 2 and latest[0] == expected[0]
    assert latest[1]['writable'] is False and latest[1]['allow_tasks'] is False


def test_stale_preview_and_concurrent_configuration_changes_do_not_overwrite(roots_agent, tmp_path):
    agent = roots_agent; target = tmp_path / 'new'; target.mkdir()
    stale = arguments(agent, target)
    raw = json.loads(agent.config_path.read_bytes()); raw['name'] = 'changed elsewhere'
    agent.config_path.write_text(json.dumps(raw)); before = agent.config_path.read_bytes()
    for tool in ('agent_roots_preview', 'agent_roots_set'):
        with pytest.raises(DevError) as error: call(agent, tool, identity(agent), stale)
        assert error.value.code == 'SETTINGS_CHANGED'
        assert agent.config_path.read_bytes() == before


def test_revoke_unmounted_directory_never_probes_target(roots_agent, tmp_path, monkeypatch):
    agent = roots_agent; target = tmp_path / 'unmounted-share'
    raw = json.loads(agent.config_path.read_bytes())
    raw['allowed_roots'].append({'path': str(target), 'writable': True, 'allow_tasks': True})
    agent.config_path.write_text(json.dumps(raw)); reload(agent)
    original_resolve = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == target: pytest.fail('Revoking an unavailable share must not resolve or stat it')
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    body = arguments(agent, target, 'remove')
    result = call(agent, 'agent_roots_set', identity(agent), body)
    assert result['saved'] and all(row['path'] != str(target) for row in result['roots'])


@pytest.mark.parametrize('field,value', [('_roots_admin', False), ('_integration_admin', False),
    ('_execution_policy', {'origin': 'mcp'}), ('_coding_device', 'other'),
    ('id', 'existing-project'), ('_coding_workspace', 'other'), ('_original_root', '/elsewhere')])
def test_agent_requires_authenticated_device_panel_context(roots_agent, field, value):
    agent = roots_agent; before = agent.config_path.read_bytes()
    with pytest.raises(DevError) as error:
        call(agent, 'agent_roots_get', identity(agent, **{field: value}), {'device_id': agent.config['device_id']})
    assert error.value.code == 'OWNER_REQUIRED'
    assert agent.config_path.read_bytes() == before


@pytest.mark.parametrize('override', [{'path': '../escape'}, {'path': '/missing/codepier-test-root'},
    {'path': 'relative/path'}])
def test_invalid_path_does_not_write(roots_agent, override):
    agent = roots_agent; before = agent.config_path.read_bytes()
    body = {**arguments(agent, Path('/unused')), **override}
    with pytest.raises(DevError): call(agent, 'agent_roots_set', identity(agent), body)
    assert agent.config_path.read_bytes() == before


@pytest.mark.parametrize('override', [{'writable': False, 'allow_tasks': True}, {'writable': 'true'},
    {'path': '\x00'}, {'config_path': '/another/config.json'}, {'secret': 'forbidden'}])
def test_contract_does_not_coerce_permissions_or_accept_other_settings(override):
    fields = {'action': 'upsert', 'path': '/fixture', 'expected_sha256': 'a'*64,
              'idempotency_key': 'roots-fixture', **override}
    with pytest.raises(ValidationError): RootsChange.model_validate(fields)


def test_symlink_is_pinned_to_resolved_directory(roots_agent, tmp_path):
    agent = roots_agent; target = tmp_path / 'actual'; target.mkdir()
    link = tmp_path / 'alias'
    try: link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError): pytest.skip('symlink creation unavailable on this platform')
    result = call(agent, 'agent_roots_set', identity(agent), arguments(agent, link))
    assert result['preview']['canonicalized']
    assert result['roots'][-1]['path'] == str(target.resolve())


@pytest.mark.parametrize('failure', ['sync', 'replace_reply_lost', 'unreadable'])
def test_uncertain_save_keeps_review_state_and_never_repeats_write(roots_agent, tmp_path, monkeypatch, failure):
    agent = roots_agent; target = tmp_path / 'new'; target.mkdir()
    count = []
    def replace(path, original, info, replacement, expected):
        count.append(1)
        if failure != 'unreadable': path.write_bytes(replacement)
        else: path.unlink()
        if failure == 'sync': raise DevError('DURABILITY_UNCONFIRMED', 'fixture', 409)
        raise OSError(errno.EIO, 'fixture storage error')
    monkeypatch.setattr('agent.roots_panel._replace_config', replace)
    result = call(agent, 'agent_roots_set', identity(agent), arguments(agent, target))
    assert result['state'] == 'needs_review' and result['code'] == 'ROOTS_SAVE_UNCERTAIN'
    assert result['outcome_confirmed'] is False and count == [1]


def test_pending_channel_reload_is_not_reported_applied(roots_agent):
    roots_agent.runtime_reload_pending = True
    assert get(roots_agent)['state'] == 'pending_reload'


def test_legacy_default_write_permission_is_displayed_truthfully(roots_agent):
    agent = roots_agent; raw = json.loads(agent.config_path.read_bytes())
    raw['allowed_roots'][0].pop('writable'); agent.config_path.write_text(json.dumps(raw)); reload(agent)
    assert get(agent)['roots'][0]['writable'] is True


def test_private_tools_never_appear_in_mcp_catalog_and_old_peers_refuse():
    for profile in ('core', 'full', 'coding'):
        assert not PANEL_ROOTS_TOOLS.intersection(row['name'] for row in tool_definitions(profile))
    for name in PANEL_ROOTS_TOOLS:
        with pytest.raises(DevError): require_compatible(PeerContract((1, 30, 7), True, None, None), name)
        with pytest.raises(DevError): validate_call_epoch(name, 1)
        validate_call_epoch(name, 2)
