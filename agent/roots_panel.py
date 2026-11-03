"""Device-owner panel directory grants with CAS, backup and reload evidence."""
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import time

from agent.config import validate_config
from agent.runtime_config import read_json, _replace_config
from shared.agent_roots_contracts import RootsGet, RootsCommand
from shared.util import DevError


def _authorized(agent, project, device_id):
    if (project.get('_roots_admin') is not True
            or project.get('_integration_admin') is not True
            or (project.get('_execution_policy') or {}).get('origin') != 'panel'
            or project.get('_coding_device') != agent.config['device_id']
            or device_id != agent.config['device_id']
            or project.get('id') is not None
            or project.get('_original_root') or project.get('_coding_workspace')):
        raise DevError('OWNER_REQUIRED', '目录授权仅允许已登录面板的设备管理员操作', 403)


def _load(agent):
    try:
        original, raw, info = read_json(agent.config_path)
        config = validate_config(original, agent.config_path)
    except (ValueError, OSError) as exc:
        raise DevError('ROOTS_CONFIG_INVALID', 'Agent 配置当前无法安全读取；未修改任何授权', 409) from exc
    if any(config.get(key) != agent.config.get(key) for key in ('device_id', 'secret', 'state_dir', 'hub_url')):
        raise DevError('ROOTS_RELOAD_REQUIRED', 'Agent 连接设置已改变，请等待原节点重新加载后再保存', 409)
    return original, raw, info, config


def _rows(config):
    # Do not stat every saved directory here: one disconnected NAS must not
    # prevent inspecting or revoking its grant. Only preview probes its target.
    return [{'path': row['path'], 'writable': bool(row.get('writable', True)),
             'allow_tasks': bool(row.get('allow_tasks', False))}
            for row in config.get('allowed_roots', [])]


def _snapshot(agent, config, raw):
    applied = config == agent.config and not getattr(agent, 'runtime_reload_pending', False)
    return {'schema_version': 1, 'device_id': agent.config['device_id'],
            'config_sha256': hashlib.sha256(raw).hexdigest(),
            'state': 'applied' if applied else 'pending_reload', 'roots': _rows(config),
            'can_save': True, 'saved': False, 'observed_at': time.time(),
            'shell_opt_in_changed': False, 'project_permissions_changed': False}


def _path(value):
    try:
        path = Path(value).expanduser()
    except (ValueError, RuntimeError, OSError) as exc:
        raise DevError('ROOTS_PATH_INVALID', '请输入执行节点上的绝对目录路径', 422) from exc
    if not path.is_absolute():
        raise DevError('ROOTS_PATH_INVALID', '请输入执行节点上的绝对目录路径', 422)
    return path


def _candidate(original, config, model):
    candidate = deepcopy(original)
    rows = candidate.setdefault('allowed_roots', [])
    requested = _path(model.path)
    # Revocation uses the saved spelling and never resolves a disappeared NAS
    # or changed symlink. A revoke must remain possible while storage is down.
    lexical = os.path.normcase(os.path.normpath(str(requested)))
    matched = [i for i, row in enumerate(rows)
               if os.path.normcase(os.path.normpath(str(_path(row['path'])))) == lexical]
    overlaps = []
    if model.action == 'remove':
        if not matched:
            raise DevError('ROOTS_NOT_FOUND', '这条目录授权已不存在，请刷新目录列表', 409)
        canonical = str(requested)
        for i in reversed(matched):
            rows.pop(i)
        exists = None
    else:
        try:
            target = requested.resolve(strict=True)
            if not target.is_dir():
                raise DevError('ROOTS_NOT_DIRECTORY', '授权路径必须是已存在的目录', 422)
            if not os.access(target, os.R_OK | os.X_OK):
                raise DevError('ROOTS_PATH_DENIED', 'Agent 进程没有读取这个目录的系统权限', 403)
            if model.writable and not os.access(target, os.W_OK | os.X_OK):
                raise DevError('ROOTS_PATH_DENIED', 'Agent 进程没有写入这个目录的系统权限', 403)
        except FileNotFoundError as exc:
            raise DevError('ROOTS_PATH_MISSING', '目录不存在或共享尚未挂载；请连接后再授权', 409) from exc
        except (OSError, RuntimeError) as exc:
            raise DevError('ROOTS_PATH_UNAVAILABLE', '目录当前无法访问；请核对挂载和系统权限', 409) from exc
        canonical = str(target)
        canonical_key = os.path.normcase(os.path.normpath(canonical))
        matched = sorted(set(matched + [i for i, row in enumerate(rows)
            if os.path.normcase(os.path.normpath(str(_path(row['path'])))) == canonical_key]))
        entry = {'path': canonical, 'writable': model.writable, 'allow_tasks': model.allow_tasks}
        if matched:
            # Preserve unknown future/local metadata on the edited entry.
            rows[matched[0]].update(entry)
            for i in reversed(matched[1:]):
                rows.pop(i)
        else:
            if len(rows) >= 256:
                raise DevError('ROOTS_LIMIT', '目录授权最多支持 256 条，请合并重复授权后再新增', 409)
            rows.append(entry)
        exists = True
    for row in rows:
        other = _path(row['path'])
        canonical_path = Path(canonical)
        if str(other) != canonical and (canonical_path.is_relative_to(other) or other.is_relative_to(canonical_path)):
            overlaps.append({'path': row['path'], 'writable': bool(row.get('writable', True)),
                             'allow_tasks': bool(row.get('allow_tasks', False))})
    return candidate, {'action': model.action, 'path': canonical, 'requested_path': model.path,
                       'exists': exists, 'overlapping_roots': overlaps,
                       'canonicalized': canonical != model.path}


def call(agent, tool, project, arguments):
    model = (RootsGet if tool == 'agent_roots_get' else RootsCommand).model_validate(arguments)
    _authorized(agent, project, model.device_id)
    original, raw, info, config = _load(agent)
    if tool == 'agent_roots_get':
        return _snapshot(agent, config, raw)
    current_sha = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(model.expected_sha256, current_sha):
        raise DevError('SETTINGS_CHANGED', '授权配置已在其他窗口修改；请读取最新配置后再保存', 409)
    candidate, preview = _candidate(original, config, model)
    try:
        checked = validate_config(candidate, agent.config_path)
    except (ValueError, TypeError) as exc:
        raise DevError('ROOTS_CONFIG_INVALID', '候选目录授权未通过完整配置校验，未保存', 422) from exc
    encoded = (json.dumps(candidate, ensure_ascii=False, indent=2) + '\n').encode()
    if tool == 'agent_roots_preview':
        return {**_snapshot(agent, config, raw), 'preview': preview,
                'candidate_roots': _rows(checked), 'valid': True, 'saved': False,
                'unchanged': candidate == original}
    if candidate == original:
        return {**_snapshot(agent, config, raw), 'preview': preview, 'saved': True, 'unchanged': True}
    prepared = _snapshot(agent, checked, encoded)
    try:
        backup = _replace_config(agent.config_path, raw, info, encoded, current_sha)
    except FileExistsError as exc:
        raise DevError('ROOTS_CONFIG_BUSY', '另一个配置保存正在进行；请核对最新配置后再保存', 409) from exc
    except DevError as exc:
        if exc.code == 'SHA_CONFLICT':
            raise DevError('SETTINGS_CHANGED', '保存期间配置发生变化，未覆盖另一份修改', 409) from exc
        if exc.code == 'DURABILITY_UNCONFIRMED':
            return {**prepared, 'state': 'needs_review', 'saved': True, 'outcome_confirmed': False,
                    'code': 'ROOTS_SAVE_UNCERTAIN', 'preview': preview,
                    'message': '配置已替换，但持久落盘尚未确认；请核对原保存和当前配置，不要重复授权'}
        raise
    except OSError:
        # A network filesystem can perform replace and lose its reply. The
        # exception alone cannot prove that the original config is still live.
        try:
            _, observed, _ = read_json(agent.config_path)
            saved = observed == encoded
        except (OSError, ValueError, DevError):
            saved = None
        return {**prepared, 'state': 'needs_review', 'saved': saved, 'outcome_confirmed': False,
                'code': 'ROOTS_SAVE_UNCERTAIN', 'preview': preview,
                'message': '配置保存遇到存储错误，结果尚未确认；请核对原保存和当前配置，不要重复授权'}
    except ValueError as exc:
        raise DevError('ROOTS_CONFIG_INVALID', '配置大小或文件归属不符合安全保存要求，未保存', 422) from exc
    return {**prepared, 'saved': True, 'unchanged': False, 'backup_ref': backup,
            'preview': preview, 'outcome_confirmed': True,
            'activation': 'existing_agent_config_reload', 'running_operations_restarted': False}
