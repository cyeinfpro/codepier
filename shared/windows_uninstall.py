"""Owner-verified, local Windows Agent cleanup. No enrollment or identity changes.

This standard-library-only module builds one bounded plan and keeps maintenance
locks until the PowerShell launcher finishes deleting the approved directories.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

TASKS = ('CodePierAgent', 'RemoteDevAgent')
EVIDENCE = ('config.json', 'management.json', 'service.xml')
ENTRIES = {
    *EVIDENCE, 'runtime', 'tools', 'python', 'logs', 'state',
    'agentctl', 'agentctl.ps1', 'codepier-agent', 'codepier-agent.ps1',
    'service-pending.xml', '.runtime-previous', '.runtime-failed',
    '.codepier-migration.json', '.uninstall.json',
}
NS = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}


def linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def safe_directory(path, api):
    path = Path(path)
    for ancestor in (path, *path.parents):
        if linked(ancestor):
            raise ValueError('Installation path contains a link or junction: '+str(path))
    return api.validate_install_base(path)


def read_record(path, api):
    if not path.exists():
        return {}
    value = json.loads(api._read_owned_service_file(path))
    if not isinstance(value, dict):
        raise ValueError('Invalid Agent metadata: '+str(path))
    return value


def fingerprint(base, api):
    return {name: hashlib.sha256(api._read_owned_service_file(base/name)).hexdigest()
            if (base/name).exists() else None for name in EVIDENCE}


def owned_task_root(xml, sid, api):
    """The known name is discovery only; command, sole action and SID prove scope."""
    task = ET.fromstring(xml)
    actions = task.find('t:Actions', NS)
    principals = task.find('t:Principals', NS)
    if (actions is None or len(actions) != 1 or actions[0].tag != '{'+NS['t']+'}Exec'
            or principals is None or len(principals) != 1
            or principals[0].tag != '{'+NS['t']+'}Principal'
            or principals[0].findtext('t:UserId', namespaces=NS) != sid):
        raise ValueError('Windows Agent task has unverified actions or account; nothing was removed.')
    action = actions[0]
    raw = action.findtext('t:WorkingDirectory', namespaces=NS) or ''
    runtime = Path(raw)
    if not runtime.is_absolute() or runtime.name != 'runtime':
        raise ValueError('Windows Agent task has an unverified runtime path.')
    base = safe_directory(runtime.parent, api)
    if (action.findtext('t:Command', namespaces=NS) not in
            (str(base/'runtime/.venv/Scripts/python.exe'), str(base/'runtime/.venv/Scripts/pythonw.exe'))
            or action.findtext('t:Arguments', namespaces=NS) !=
            subprocess.list2cmdline([str(base/'runtime/run-service.py')])):
        raise ValueError('Windows Agent task points to another program; nothing was removed.')
    return base


def task_inventory(api):
    result = {}
    sid = api.windows_user_sid()
    for name in TASKS:
        if api.windows_task_registered(name):
            xml = api.query_windows_task_xml(name)
            result[name] = {'base': owned_task_root(xml, sid, api), 'xml': xml}
    return result


def known_entry(name):
    return name in ENTRIES or bool(re.fullmatch(
        r'\.runtime-update-(?:cli|manual)-[a-f0-9]{12}|\.recovery-service-[a-f0-9]{32}\.xml', name))


def verify_directory_owner(base, api):
    """Read Windows ownership; elevation alone is not another user's permission."""
    quoted = "'" + str(base).replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop'; $p="+quoted+"; "
              "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; "
              "$acl=[IO.Directory]::GetAccessControl($p); "
              "$owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value; "
              "if($owner -eq $sid.Value){exit 0}; "
              "$full=[Security.AccessControl.FileSystemRights]::FullControl; "
              "$mine=@($acl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]) | "
              "Where-Object {$_.IdentityReference.Value -eq $sid.Value -and "
              "$_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and "
              "($_.FileSystemRights -band $full) -eq $full}); "
              "if($owner -eq 'S-1-5-32-544' -and $acl.AreAccessRulesProtected -and $mine.Count -gt 0){exit 0}; exit 13")
    encoded = api.base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = api.subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                 '-EncodedCommand', encoded],
                                stdout=api.subprocess.DEVNULL, stderr=api.subprocess.DEVNULL, timeout=30)
    if result.returncode != 0:
        raise ValueError('Cannot verify current-account ownership; directory was preserved: '+str(base))


def inspect_root(base, api, *, locked=False):
    """Refuse unknown contents instead of recursively deleting a guessed directory."""
    base = safe_directory(base, api)
    if not base.is_dir():
        raise ValueError('Agent installation is not a directory: '+str(base))
    verify_directory_owner(base, api)
    entries = sorted(p.name for p in base.iterdir())
    unknown = [name for name in entries if not known_entry(name) and not (locked and name == '.install.lock')]
    if unknown:
        raise ValueError('Unrecognized contents were preserved in '+str(base)+': '+', '.join(unknown))
    inspected = 1
    for folder, directories, files in os.walk(base, followlinks=False):
        inspected += len(directories)+len(files)
        if inspected > 200000:
            raise ValueError('Cleanup tree exceeds the inspection limit; nothing was removed.')
        for name in directories+files:
            item = Path(folder)/name
            if linked(item):
                raise ValueError('Linked contents were preserved: '+str(item))
            if name in {'.git', '.hg', '.svn'}:
                raise ValueError('A project checkout exists inside the installation: '+str(item))
    metadata = read_record(base/'management.json', api)
    config = read_record(base/'config.json', api)
    resume = read_record(base/'.uninstall.json', api)
    trusted_resume = (resume.get('schema') == 1 and resume.get('source') == str(base)
                      and resume.get('owner_sid') == api.windows_user_sid()
                      and resume.get('kind') == 'codepier-local-uninstall')
    managed = metadata.get('managed') is True and metadata.get('layout') == 'managed-runtime'
    valid_config = (isinstance(config.get('device_id'), str)
                    and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', config['device_id'])
                    and isinstance(config.get('hub_url'), str) and config['hub_url'])
    if config and not valid_config:
        raise ValueError('Invalid Agent configuration was preserved: '+str(base/'config.json'))
    if not (trusted_resume or managed or valid_config and (base/'runtime/agent/__main__.py').is_file()):
        raise ValueError('Cannot prove this is a managed CodePier installation: '+str(base))
    no_service = metadata.get('service_kind') == 'none' and metadata.get('service_scope') == 'none'
    if metadata and not no_service and (metadata.get('service_kind') not in (None, '', 'schtasks')
                                       or metadata.get('service_scope') not in (None, '', 'user')):
        raise ValueError('Different service scope was preserved: '+str(base))
    roots = config.get('allowed_roots', [])
    if not isinstance(roots, list):
        raise ValueError('Invalid authorized project roots; installation was preserved.')
    for root in roots:
        raw = root.get('path') if isinstance(root, dict) else root
        if not isinstance(raw, str):
            raise ValueError('Invalid authorized project root; installation was preserved.')
        if Path(raw).expanduser().resolve().is_relative_to(base):
            raise ValueError('An authorized project is inside the installation; move it out before uninstalling.')
    state = Path(config.get('state_dir') or base/'state').expanduser()
    if not state.is_absolute() or not state.resolve().is_relative_to(base):
        raise ValueError('External Agent state was preserved; its ownership needs review: '+str(state))
    if (state/'browser-bridge/install-receipt.json').exists():
        raise ValueError('A browser bridge is installed. Remove its registered host with its verified uninstaller before Agent cleanup.')
    api.require_idle(base, config or {'state_dir': str(state)})
    return {'base': base, 'config': config, 'files': fingerprint(base, api),
            'entries': [name for name in entries if name != '.install.lock']}


def inventory(args, api):
    tasks = task_inventory(api)
    home = Path.home()
    candidates = {home/'.codepier-agent', home/'.remote-dev-agent'}
    if args.install_dir:
        candidates.add(Path(args.install_dir).expanduser())
    candidates.update(task['base'] for task in tasks.values())
    aliases = []
    for base in list(candidates):
        if base == home/'.remote-dev-agent' and linked(base) and base.resolve() == home/'.codepier-agent':
            aliases.append(str(base))
            candidates.remove(base)
            candidates.add(base.resolve())
    # Only exact installer-created backup names next to known roots are candidates.
    for base in list(candidates):
        safe_directory(base, api)
        if base.parent.is_dir():
            pattern = re.compile(re.escape(base.name)+r'\.recovery-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}')
            candidates.update(item for item in base.parent.iterdir() if pattern.fullmatch(item.name))
    roots = [inspect_root(base, api) for base in sorted(candidates) if base.exists() or linked(base)]
    if len(roots) > 16:
        raise ValueError('Too many Agent installation roots; review the cleanup scope before retrying.')
    present = {item['base'] for item in roots}
    missing = [name for name, item in tasks.items() if item['base'] not in present]
    if missing:
        raise ValueError('Agent task remains but its installation evidence is missing: '+', '.join(missing))
    for left in present:
        if any(right != left and right.is_relative_to(left) for right in present):
            raise ValueError('Nested Agent installation roots require review; nothing was removed.')
    return roots, tasks, aliases


def release_lock(base, token, api):
    lock = base/'.install.lock'
    if not lock.exists():
        return
    owner = read_record(lock/'uninstall.json', api)
    if linked(lock) or owner.get('lock_token') != token:
        raise ValueError('Uninstall lock changed; preserve it for review: '+str(lock))
    (lock/'uninstall.json').unlink()
    lock.rmdir()


def run(args, api):
    handoff = Path(args.uninstall_handoff_file).expanduser() if args.uninstall_handoff_file else None
    preview = getattr(args, 'uninstall_preview', False)
    if not preview and (handoff is None or not handoff.is_absolute() or handoff.exists() or linked(handoff)
            or not handoff.parent.is_dir() or any(linked(p) for p in (handoff.parent, *handoff.parents))):
        raise ValueError('Use the current PowerShell uninstall launcher for verified cleanup after Python exits.')
    roots, tasks, aliases = inventory(args, api)
    if handoff and any(handoff.resolve().is_relative_to(item['base']) for item in roots):
        raise ValueError('The cleanup handoff must be outside every Agent installation.')
    print('Local CodePier Agent cleanup, for the current Windows account:', flush=True)
    for item in roots:
        print('  Remove runtime, private tools, configuration, credentials, logs and state: '+str(item['base']), flush=True)
    for name in tasks:
        print('  Stop and remove verified scheduled task: '+name, flush=True)
    for alias in aliases:
        print('  Remove migration alias only: '+alias, flush=True)
    print('Panel records, project files and other software are preserved. Other Windows accounts are not scanned.', flush=True)
    if preview:
        print('Preview only; no service or files changed.', flush=True)
        return
    if roots or aliases:
        if not args.yes:
            try:
                answer = input('Type UNINSTALL CODEPIER to remove all listed local Agent data: ')
            except EOFError as exc:
                raise ValueError('Local uninstall needs confirmation; no files were changed.') from exc
            if answer != 'UNINSTALL CODEPIER':
                raise ValueError('Uninstall cancelled; no files were changed.')
    locks = []
    transferred = False
    try:
        for item in roots:
            base = item['base']
            lock = base/'.install.lock'
            lock.mkdir()
            token = os.urandom(16).hex()
            try:
                api.atomic_json_file(lock/'uninstall.json', {'schema': 1, 'lock_token': token, 'pid': os.getpid()})
            except Exception:
                # The directory was created by this call. Remove it only while
                # empty; never sweep unknown lock contents after a failed write.
                try:
                    lock.rmdir()
                except OSError:
                    pass
                raise
            locks.append((base, token))
        if task_inventory(api) != tasks:
            raise ValueError('Scheduled tasks changed during confirmation; cleanup was cancelled.')
        for item in roots:
            current = inspect_root(item['base'], api, locked=True)
            if current['files'] != item['files'] or current['entries'] != item['entries']:
                raise ValueError('Agent files changed during confirmation; cleanup was cancelled.')
        helper = api.load_lifecycle_helper(Path(api.__file__).resolve().with_name('agent_lifecycle.py'))
        # Exact task ownership is rechecked immediately before each service action.
        for name, task in tasks.items():
            if api.query_windows_task_xml(name) != task['xml']:
                raise ValueError('Scheduled task changed before removal: '+name)
            helper.set_windows_task_enabled(name, False)
            api.subprocess.run(['schtasks.exe', '/End', '/TN', name], check=False, timeout=30)
            # Poll actual instances even when Enabled is false.
            for _ in range(50):
                script = helper.windows_task_script(name)+'if($t.GetInstances(0).Count -gt 0){exit 10}; exit 0'
                code = helper._run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script])
                if code == 0:
                    break
                if code != 10:
                    raise ValueError('Cannot verify stopped scheduled task: '+name)
                api.time.sleep(0.2)
            else:
                raise ValueError('Agent process did not stop; files were preserved: '+name)
            if owned_task_root(api.query_windows_task_xml(name), api.windows_user_sid(), api) != task['base']:
                raise ValueError('Scheduled task ownership changed before deletion: '+name)
            api.run(['schtasks.exe', '/Delete', '/TN', name, '/F'])
            if api.windows_task_registered(name):
                raise ValueError('Scheduled task remains; files were preserved: '+name)
        if task_inventory(api):
            raise ValueError('An Agent task appeared during cleanup; files were preserved.')
        plan = []
        for item, (base, token) in zip(roots, locks):
            current = item['config'] or {'state_dir': str(base/'state')}
            api.require_idle(base, current)
            api.verify_windows_instance_is_stopped(base, current)
            if fingerprint(base, api) != item['files']:
                raise ValueError('Agent configuration changed during cleanup; files were preserved.')
            api.atomic_json_file(base/'.uninstall.json', {
                'schema': 1, 'kind': 'codepier-local-uninstall', 'source': str(base),
                'owner_sid': api.windows_user_sid(), 'stage': 'files_pending'})
            plan.append({'source': str(base), 'lock_token': token, 'files': fingerprint(base, api),
                         'entries': sorted(set(item['entries']) | {'.uninstall.json'})})
        api.atomic_json_file(handoff, {'schema': 1, 'roots': plan, 'aliases': aliases})
        transferred = True
        print('Services checked. PowerShell will now finish and verify local file cleanup.', flush=True)
    finally:
        if not transferred:
            for base, token in reversed(locks):
                release_lock(base, token, api)
