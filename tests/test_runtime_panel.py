"""Owner-only project runtime settings; real config CAS and clean default."""
import json
import uuid

import pytest
from pydantic import ValidationError

from agent.config import validate_config
from agent.project_runtime import default_profile, selected
from agent.runtime_panel import call
from shared.runtime_panel_contracts import RuntimeSave
from shared.tool_protocol import PeerContract, require_compatible, validate_call_epoch, wire_version
from shared.util import DevError, atomic_json
from tests.test_shell import shell_agent  # noqa: F401


def project(agent, root):
    return {'id': 'fixture-id', 'alias': 'fixture', 'root': str(root), 'mode': 'write',
        'allow_tasks': True, '_integration_admin': True,
        '_execution_policy': {'origin': 'panel'}, '_coding_device': agent.config['device_id']}


def get(agent, value):
    return call(agent, 'project_runtime_get', value, {'project': value['id']})


def save(agent, value, snapshot, profile):
    return call(agent, 'project_runtime_set', value, {'project': value['id'],
        'expected_revision': snapshot['revision'], 'profile': profile, 'idempotency_key': uuid.uuid4().hex})


def reload(agent):
    agent.config = validate_config(json.loads(agent.config_path.read_text()), agent.config_path)
    agent.engine.config = agent.config


def test_unspecified_projects_default_to_real_clean_runtime_not_just_ui():
    default = selected({}, {'id': 'p', 'alias': 'P'})
    assert default == default_profile() and default['mode'] == 'project_clean_v1'
    assert default['pass_env'] == [] and default['env'] == {}
    assert 'credentials' not in default
    assert 'node_modules/.bin' in default['path']
    explicit = {'project_runtimes': {'p': {'schema_version': 1, 'mode': 'native_compat'}}}
    assert selected(explicit, {'id': 'p', 'alias': 'P'})['mode'] == 'native_compat'


def test_get_does_not_change_config_or_create_home(shell_agent):
    agent, root = shell_agent
    value = project(agent, root)
    before = agent.config_path.read_bytes()
    result = get(agent, value)
    assert result['state'] == 'applied' and result['can_save']
    assert result['default_mode'] == 'project_clean_v1'
    assert agent.config_path.read_bytes() == before
    assert agent.config['secret'] not in json.dumps(result)
    assert str(root) not in json.dumps(result)
    assert not (agent.state_dir / 'project-runtimes').exists()


def test_switch_retains_other_config_and_reports_saved_separately_from_loaded(shell_agent):
    agent, root = shell_agent
    value = project(agent, root)
    # Exercise a genuine unconfigured project irrespective of legacy fixture defaults.
    raw = json.loads(agent.config_path.read_text()); raw.pop('project_runtimes', None)
    atomic_json(agent.config_path, raw); reload(agent)
    original = agent.config_path.read_bytes()
    snapshot = get(agent, value)
    assert snapshot['profile']['mode'] == 'project_clean_v1'
    result = save(agent, value, snapshot, {'mode': 'native_compat'})
    assert result['saved'] and result['state'] == 'pending_reload'
    assert result['runtime']['mode'] == 'project_clean_v1'
    assert result['saved_runtime']['mode'] == 'native_compat'
    changed = json.loads(agent.config_path.read_text())
    assert changed.pop('project_runtimes') == {'fixture-id': {'schema_version': 1, 'mode': 'native_compat'}}
    assert changed == json.loads(original)
    assert (agent.config_path.parent / result['backup_ref']).read_bytes() == original
    reload(agent)
    ready = get(agent, value)
    assert ready['state'] == 'applied' and ready['runtime']['mode'] == 'native_compat'
    assert not ready['grants_changed'] and not ready['shell_opt_in_changed']
    clean = save(agent, value, ready, {'mode': 'project_clean_v1'})
    assert clean['profile']['mode'] == 'project_clean_v1'
    assert clean['profile']['path'] == default_profile()['path']


def test_stale_config_revision_does_not_overwrite_new_settings(shell_agent):
    agent, root = shell_agent; value = project(agent, root)
    first = get(agent, value)
    save(agent, value, first, {'mode': 'project_clean_v1', 'path': ['tools']})
    after = agent.config_path.read_bytes()
    with pytest.raises(DevError) as error:
        save(agent, value, first, {'mode': 'native_compat'})
    assert error.value.code == 'SETTINGS_CHANGED'
    assert agent.config_path.read_bytes() == after


@pytest.mark.parametrize('field,wrong', [('_integration_admin', False), ('_coding_device', 'another'),
    ('_execution_policy', {'origin': 'mcp'}), ('_coding_workspace', 'other'), ('_original_root', '/outside')])
def test_caller_cannot_replace_authenticated_management_identity(shell_agent, field, wrong):
    agent, root = shell_agent; value = project(agent, root); before = agent.config_path.read_bytes()
    with pytest.raises(DevError): get(agent, {**value, field: wrong})
    assert agent.config_path.read_bytes() == before


def test_revision_is_bound_to_mapping_not_just_config_file(shell_agent):
    agent, root = shell_agent; value = project(agent, root)
    first = get(agent, value)
    other = {**value, 'id': 'other-project'}
    with pytest.raises(DevError) as error: save(agent, other, first, {'mode': 'native_compat'})
    assert error.value.code == 'SETTINGS_CHANGED'


def test_secret_environment_values_never_roundtrip_through_panel(shell_agent):
    agent, root = shell_agent; value = project(agent, root)
    raw = json.loads(agent.config_path.read_text())
    raw['project_runtimes'] = {'fixture': {'mode': 'project_clean_v1', 'env': {'PROJECT_TOKEN': 'synthetic-secret'}}}
    atomic_json(agent.config_path, raw); reload(agent)
    snapshot = get(agent, value)
    assert snapshot['environment_keys'] == ['PROJECT_TOKEN']
    assert 'synthetic-secret' not in json.dumps(snapshot)
    save(agent, value, snapshot, {'mode': 'project_clean_v1', 'path': ['tools']})
    updated = json.loads(agent.config_path.read_text())['project_runtimes']
    assert 'fixture' not in updated
    assert updated['fixture-id']['env'] == {'PROJECT_TOKEN': 'synthetic-secret'}


def test_read_only_project_configuration_does_not_enable_file_or_command_permissions(shell_agent):
    agent, root = shell_agent; value = {**project(agent, root), 'mode': 'read', 'allow_tasks': False}
    original = json.loads(agent.config_path.read_text())
    result = save(agent, value, get(agent, value), {'mode': 'project_clean_v1'})
    assert result['saved']
    data = json.loads(agent.config_path.read_text())
    assert data['allowed_roots'] == original['allowed_roots']
    assert data['shell'] == original['shell']


@pytest.mark.parametrize('extra', [{'env': {'TOKEN': 'secret'}}, {'enabled': True}, {'allow_tasks': True}])
def test_panel_cannot_supply_secrets_or_expand_authority(extra):
    with pytest.raises(ValidationError):
        RuntimeSave(expected_revision='a'*64, idempotency_key='fixture-key',
            profile={'mode': 'project_clean_v1', **extra})


def test_container_missing_requirements_is_not_native_fallback(shell_agent):
    agent, root = shell_agent; value = project(agent, root); before = agent.config_path.read_bytes()
    with pytest.raises(DevError) as error:
        save(agent, value, get(agent, value), {'mode': 'container_v1', 'shell': 'sh',
            'container': {'engine': '/not-installed/docker', 'endpoint': 'unix:///not-installed/docker.sock', 'image': 'sha256:' + 'a'*64}})
    assert error.value.code in {'RUNTIME_ENGINE_MISSING', 'RUNTIME_ENGINE_UNAVAILABLE'}
    assert agent.config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_default_clean_refuses_native_ssh_before_transport_without_fallback(shell_agent, monkeypatch):
    agent, root = shell_agent
    agent.config.pop('project_runtimes', None)
    assert selected(agent.config, {'id': 'fixture-id', 'alias': 'fixture'})['mode'] == 'project_clean_v1'
    def unexpected(*args, **kwargs):
        pytest.fail('Default clean mode must not invoke the native SSH transport')
    monkeypatch.setattr('agent.ssh.prepare_ssh', unexpected)
    value = {'id': 'fixture-id', 'alias': 'fixture', 'root': str(root), 'mode': 'write', 'allow_tasks': True}
    with pytest.raises(DevError) as error:
        await agent.execute(uuid.uuid4().hex, 'ssh_exec', value, {})
    assert error.value.code == 'RUNTIME_REMOTE_TARGET_UNSUPPORTED'
    assert not agent.processes


@pytest.mark.parametrize('credentials', [
    {'github_cli': 1}, {'git_config': 'true'}, {'ssh_agent': None},
    {'github_cli': True, 'token': 'synthetic-secret'}, None,
])
def test_credentials_contract_rejects_secrets_coercion_and_null(credentials):
    with pytest.raises(ValidationError):
        RuntimeSave(expected_revision='a'*64, idempotency_key='fixture-key',
            profile={'mode': 'project_clean_v1', 'credentials': credentials})


@pytest.mark.parametrize('mode', ['native_compat', 'container_v1'])
@pytest.mark.parametrize('credentials', [{}, {'github_cli': False}, None])
def test_credentials_contract_only_accepts_clean_mode(mode, credentials):
    profile = {'mode': mode, 'credentials': credentials}
    if mode == 'container_v1':
        profile['container'] = {'engine': '/not-installed/docker',
            'endpoint': 'unix:///not-installed/docker.sock', 'image': 'sha256:' + 'a'*64}
    with pytest.raises(ValidationError):
        RuntimeSave(expected_revision='a'*64, idempotency_key='fixture-key', profile=profile)


def test_credentials_contract_preserves_omission_through_hub_agent_roundtrip():
    omitted = RuntimeSave(expected_revision='a'*64, idempotency_key='fixture-key',
        profile={'mode': 'project_clean_v1'})
    assert 'credentials' not in omitted.profile.model_dump(exclude_none=True)
    assert 'credentials' not in omitted.model_dump()['profile']
    assert RuntimeSave.model_validate(omitted.model_dump()) == omitted


@pytest.mark.parametrize('credentials,expected', [
    ({}, {'github_cli': True, 'git_config': True, 'ssh_agent': True}),
    ({'github_cli': False}, {'github_cli': False, 'git_config': True, 'ssh_agent': True}),
    ({'git_config': False, 'ssh_agent': False}, {'github_cli': True, 'git_config': False, 'ssh_agent': False}),
    ({'github_cli': False, 'git_config': False, 'ssh_agent': False},
     {'github_cli': False, 'git_config': False, 'ssh_agent': False}),
])
def test_credentials_contract_defaults_only_missing_choices(credentials, expected):
    explicit = RuntimeSave(expected_revision='a'*64, idempotency_key='fixture-key',
        profile={'mode': 'project_clean_v1', 'credentials': credentials})
    assert explicit.profile.credentials.model_dump() == expected
    assert RuntimeSave.model_validate(explicit.model_dump()).profile.credentials.model_dump() == expected


@pytest.mark.parametrize('profile,expected', [
    (None, {'github_cli': True, 'git_config': True, 'ssh_agent': True}),
    ({'mode': 'project_clean_v1'}, {'github_cli': True, 'git_config': True, 'ssh_agent': True}),
    ({'mode': 'project_clean_v1', 'credentials': {}},
     {'github_cli': True, 'git_config': True, 'ssh_agent': True}),
    ({'mode': 'project_clean_v1', 'credentials': {'github_cli': False}},
     {'github_cli': False, 'git_config': True, 'ssh_agent': True}),
])
def test_credentials_snapshot_reports_effective_defaults_without_rewriting_config(shell_agent, monkeypatch, profile, expected):
    agent, root = shell_agent
    value = project(agent, root)
    monkeypatch.setattr('agent.runtime_panel.runtime_readiness', lambda *args: {'state': 'ready'})
    raw = json.loads(agent.config_path.read_text())
    raw.pop('project_runtimes', None)
    if profile is not None:
        raw['project_runtimes'] = {value['id']: profile}
    atomic_json(agent.config_path, raw); reload(agent)
    before = agent.config_path.read_bytes()
    result = get(agent, value)
    assert result['profile']['credentials'] == expected
    assert result['clean_default_profile']['credentials'] == {
        'github_cli': True, 'git_config': True, 'ssh_agent': True}
    assert agent.config_path.read_bytes() == before
    assert not (agent.state_dir / 'project-runtimes').exists()


def test_credentials_preserve_omitted_same_mode_revoke_explicitly_and_reset_on_switch(shell_agent, monkeypatch):
    agent, root = shell_agent
    value = project(agent, root)
    # This test covers panel persistence only. Never inspect a real login source.
    monkeypatch.setattr('agent.runtime_panel.runtime_readiness', lambda *args: {'state': 'ready'})
    choices = {'github_cli': True, 'git_config': False, 'ssh_agent': False}
    first = save(agent, value, get(agent, value), {'mode': 'project_clean_v1', 'credentials': choices})
    assert first['profile']['credentials'] == choices
    reload(agent)
    # Older callers know only the existing toolchain fields.
    same = save(agent, value, get(agent, value), {'mode': 'project_clean_v1', 'path': ['tools']})
    assert same['profile']['credentials'] == choices
    raw = json.loads(agent.config_path.read_text())
    assert raw['project_runtimes'][value['id']]['credentials'] == choices
    assert 'credentials' not in selected(agent.config, {'id': 'other-project', 'alias': 'other'})
    reload(agent)
    off = dict.fromkeys(choices, False)
    revoked = save(agent, value, get(agent, value), {'mode': 'project_clean_v1', 'credentials': off})
    assert revoked['profile']['credentials'] == off
    reload(agent)
    still_off = save(agent, value, get(agent, value), {'mode': 'project_clean_v1', 'path': ['other-tools']})
    assert still_off['profile']['credentials'] == off
    reload(agent)
    switched = save(agent, value, get(agent, value), {'mode': 'native_compat'})
    assert 'credentials' not in switched['profile']
    reload(agent)
    returned = save(agent, value, get(agent, value), {'mode': 'project_clean_v1'})
    assert returned['profile']['credentials'] == dict.fromkeys(choices, True)


@pytest.mark.parametrize('tool', ['project_runtime_get', 'project_runtime_set'])
def test_credential_runtime_contract_refuses_old_agent(tool):
    assert wire_version(tool) == 3
    old = PeerContract((1, 30, 1), False, 'a'*64, {tool: 2})
    with pytest.raises(DevError) as error:
        require_compatible(old, tool)
    assert error.value.code == 'AGENT_UPGRADE_REQUIRED'
    with pytest.raises(DevError) as error:
        validate_call_epoch(tool, 2)
    assert error.value.code == 'TOOL_PROTOCOL_MISMATCH'
    require_compatible(PeerContract((1, 30, 1), False, 'b'*64, {tool: 3}), tool)
    validate_call_epoch(tool, 3)
