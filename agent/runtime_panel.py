"""Panel-only, project-bound Agent configuration edits with existing CAS/backup.

No caller-selected config path, roots, secrets, shell opt-ins or authority fields.
Saved configuration and loaded runtime are reported independently. A configuration
write never starts project code, installs a tool, pulls an image or restarts work.
"""
from copy import deepcopy
import hashlib
import hmac
import json
import time

from agent.config import validate_config
from agent.project_runtime import (default_profile, descriptor, selected,
    runtime_readiness, validate_runtimes)
from agent.runtime_config import read_json, _replace_config
from shared.runtime_panel_contracts import DEFAULT_MODE, RuntimeGet, RuntimeSet
from shared.util import DevError

EDITABLE = frozenset({'mode', 'shell', 'path', 'pass_env', 'container'})


def _authorized(agent, project):
    if (project.get('_integration_admin') is not True
            or (project.get('_execution_policy') or {}).get('origin') != 'panel'
            or project.get('_coding_device') != agent.config['device_id']
            or not isinstance(project.get('id'), str) or not project['id']
            or project.get('_original_root') or project.get('_coding_workspace')):
        raise DevError('OWNER_REQUIRED', 'Execution settings require the authenticated panel administrator and original project mapping', 403)
    return agent.engine.root(project)[0]


def _revision(config, raw, project):
    binding = [project['id'], project['alias'], project['root'], config['device_id'],
               hashlib.sha256(raw).hexdigest()]
    return hmac.new(config['secret'].encode(), json.dumps(binding, ensure_ascii=False).encode(),
                    hashlib.sha256).hexdigest()


def _load(agent, project):
    raw_config, raw, info = read_json(agent.config_path)
    config = validate_config(raw_config, agent.config_path)
    if any(config.get(key) != agent.config.get(key) for key in ('device_id', 'secret', 'state_dir', 'hub_url')):
        raise DevError('RUNTIME_CONFIG_RELOAD_REQUIRED', 'Agent connection settings changed; wait for the original Agent to reload', 409)
    return raw_config, raw, info, config


def _editable(profile):
    return {key: deepcopy(value) for key, value in profile.items() if key in EDITABLE}


def _snapshot(agent, project, root, config, raw):
    profiles = config.get('project_runtimes', {})
    source = 'project_id' if project['id'] in profiles else 'alias' if project['alias'] in profiles else 'default'
    try:
        saved = selected(config, project)
    except DevError as exc:
        if exc.code != 'RUNTIME_BINDING_AMBIGUOUS':
            raise
        return {'schema_version': 1, 'project_id': project['id'], 'revision': _revision(config, raw, project),
            'profile': _editable(default_profile()), 'default_mode': DEFAULT_MODE, 'clean_default_profile': _editable(default_profile()),
            'selection_source': 'conflict', 'state': 'configuration_conflict',
            'code': exc.code, 'runtime': None, 'can_save': True,
            'environment_keys': [], 'grants_changed': False, 'observed_at': time.time()}
    try:
        loaded = descriptor(agent.config, project, root)
    except DevError:
        loaded = None
    saved_runtime = descriptor(config, project, root)
    readiness = runtime_readiness(config, project, root)
    # Matching mode/revision alone is insufficient: saving an explicit
    # equivalent default still schedules a connection reload. Until the exact
    # validated config is loaded and the channel reauthenticated, report pending.
    applied = bool(loaded and loaded['revision'] == saved_runtime['revision']
                   and config == agent.config
                   and not getattr(agent, 'runtime_reload_pending', False))
    return {'schema_version': 1, 'project_id': project['id'], 'revision': _revision(config, raw, project),
        'profile': _editable(saved), 'default_mode': DEFAULT_MODE, 'clean_default_profile': _editable(default_profile()), 'selection_source': source,
        'state': 'applied' if applied else 'pending_reload', 'runtime': loaded,
        'saved_runtime': saved_runtime, 'readiness': readiness,
        'can_save': True, 'environment_keys': sorted(saved.get('env', {})),
        'grants_changed': False, 'shell_opt_in_changed': False, 'observed_at': time.time()}


def call(agent, tool, project, arguments):
    root = _authorized(agent, project)
    model = (RuntimeGet if tool == 'project_runtime_get' else RuntimeSet).model_validate(arguments)
    if model.project != project['id']:
        raise DevError('RUNTIME_PROJECT_MISMATCH', 'Runtime settings must target the exact original project ID', 409)
    raw_config, raw, info, config = _load(agent, project)
    if tool == 'project_runtime_get':
        return _snapshot(agent, project, root, config, raw)
    expected = _revision(config, raw, project)
    if not hmac.compare_digest(model.expected_revision, expected):
        raise DevError('SETTINGS_CHANGED', '执行环境已在其他窗口或节点修改；草稿已保留，请重新读取后再保存', 409)
    editable = model.profile.model_dump(exclude_none=True)
    try:
        old = selected(config, project)
    except DevError as exc:
        if exc.code != 'RUNTIME_BINDING_AMBIGUOUS':
            raise
        old = {}
    if editable['mode'] == DEFAULT_MODE:
        desired = {**default_profile(), **editable}
    elif editable['mode'] == 'native_compat':
        desired = {'mode': 'native_compat', 'schema_version': 1}
    else:
        desired = editable
    # Existing explicit environment values are not returned or round-tripped
    # through the panel. Preserve them only for an edit of the same mode.
    if old.get('mode') == desired['mode'] and desired['mode'] != 'native_compat':
        desired['env'] = deepcopy(old.get('env', {}))
    try:
        desired = validate_runtimes({project['id']: desired})[project['id']]
    except (ValueError, TypeError) as exc:
        raise DevError('RUNTIME_SETTINGS_INVALID', str(exc), 422) from exc
    candidate = deepcopy(raw_config)
    bindings = candidate.setdefault('project_runtimes', {})
    if project['alias'] != project['id']:
        bindings.pop(project['alias'], None)  # Explicit save resolves ID/alias disagreement.
    bindings[project['id']] = desired
    checked = validate_config(candidate, agent.config_path)
    readiness = runtime_readiness(checked, project, root)
    if readiness['state'] == 'unavailable':
        raise DevError(readiness.get('code') or 'RUNTIME_UNAVAILABLE', '所选执行环境缺少必要程序或容器服务；未保存，也不会回退到其他模式', 409)
    encoded = (json.dumps(candidate, ensure_ascii=False, indent=2) + '\n').encode()
    if candidate == raw_config:
        return {**_snapshot(agent, project, root, config, raw), 'saved': True, 'unchanged': True}
    # Prepare all fallible diagnostics before the write. A later filesystem or
    # toolchain observation must not turn a committed save into 'not saved'.
    prepared = _snapshot(agent, project, root, checked, encoded)
    try:
        backup = _replace_config(agent.config_path, raw, info, encoded, hashlib.sha256(raw).hexdigest())
    except FileExistsError as exc:
        raise DevError('RUNTIME_CONFIG_BUSY', '另一个配置保存正在进行；原草稿保留，请核对最新配置', 409) from exc
    except DevError as exc:
        if exc.code == 'SHA_CONFLICT':
            raise DevError('SETTINGS_CHANGED', '保存期间配置发生变化，未覆盖另一份修改', 409) from exc
        raise
    return {**prepared, 'saved': True,
        'backup_ref': backup, 'unchanged': False,
        'environment_values_preserved': old.get('mode') == desired['mode'],
        'activation': 'existing_agent_config_reload', 'running_operations_restarted': False}
