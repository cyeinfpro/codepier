#!/usr/bin/env python3
"""Verified-package installer. Standard library only until dependencies are ready."""
from __future__ import annotations
import argparse
import base64
import csv
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

LABEL = 'com.codepier.agent'
TASK = 'CodePierAgent'


def is_legacy_launchd_name(name):
    """Recognize the historical label format without embedding an owner's name."""
    return isinstance(name, str) and re.fullmatch(
        r'com\.[A-Za-z0-9][A-Za-z0-9-]*\.remote-dev-agent', name) is not None


def _read_owned_service_file(path):
    """Read only an owner-controlled regular file, without following symlinks."""
    path = Path(path)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode)
            or hasattr(os, 'getuid') and before.st_uid != os.getuid()):
        raise ValueError('Managed service definition must be an owner-owned regular file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or hasattr(os, 'getuid') and opened.st_uid != os.getuid()):
            raise ValueError('Managed service definition changed or belongs to another owner')
        limit = 1024 * 1024
        if opened.st_size > limit:
            raise ValueError('Managed service file exceeds the size limit')
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Managed service file exceeds the size limit')
        return raw


def _launchd_service_name(base, recorded=None, home=None):
    # Keep this implementation identical in the two standalone bootstrap scripts.
    canonical = 'com.codepier.agent'
    if recorded and recorded != canonical and not is_legacy_launchd_name(recorded):
        raise ValueError('Unrecognized managed service name')
    home = Path.home() if home is None else Path(home)
    folder = home/'Library/LaunchAgents'
    if folder.is_symlink():
        raise ValueError('Managed service directory must not be a symlink')
    names = {canonical}
    names.update(path.stem for path in folder.glob('*.plist') if is_legacy_launchd_name(path.stem))
    if recorded:
        names.add(recorded)
    command = [str(base/'runtime/.venv/bin/python'), '-m', 'agent', '--config', str(base/'config.json'), 'run']
    owned = []
    for name in sorted(names):
        target = folder/(name+'.plist')
        try:
            value = plistlib.loads(_read_owned_service_file(target))
            matches = (isinstance(value, dict) and value.get('Label') == name
                       and value.get('WorkingDirectory') == str(base/'runtime')
                       and value.get('ProgramArguments') == command
                       and value.get('Program') in (None, command[0]))
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
            matches = False
        if matches:
            owned.append(name)
        elif (name == recorded or name == canonical) and (target.exists() or target.is_symlink()):
            raise ValueError('Recorded managed service ownership could not be verified')
    if len(owned) > 1:
        raise ValueError('Both legacy and CodePier services exist; inspect before updating')
    if recorded and recorded != canonical and recorded not in owned:
        raise ValueError('Recorded managed service definition is missing')
    return owned[0] if owned else canonical


def managed_service_name(base, kind, scope):
    # Bootstrap runs before shared modules are installed. Keep this discovery
    # standard-library-only, and validate ownership before any service action.
    names = {'launchd': ('com.codepier.agent', 'com.example.remote-dev-agent'),
             'systemd': ('codepier-agent.service', 'remote-dev-agent.service'),
             'schtasks': ('CodePierAgent', 'RemoteDevAgent')}.get(kind)
    if not names: return ''
    metadata_path = base/'management.json'
    metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.is_file() else {}
    name = metadata.get('service_name')
    if kind == 'launchd':
        if scope != 'user' or metadata_path.is_symlink():
            raise ValueError('Unrecognized managed launchd metadata or scope')
        return _launchd_service_name(base, name)
    if name:
        if name not in names: raise ValueError('Unrecognized managed service name')
        return name
    if kind == 'schtasks':
        target = base/'service.xml'
        if target.is_file():
            text = target.read_text(encoding='utf-16')
            return names[0] if 'CodePierAgent' in text or '.codepier-agent' in text else names[1]
        return names[0]
    folder = (Path.home()/'Library/LaunchAgents' if kind == 'launchd' else
              Path('/etc/systemd/system') if scope == 'system' else Path.home()/'.config/systemd/user')
    owned = []
    for name in names:
        target = folder/(name+'.plist' if kind == 'launchd' else name)
        if target.is_symlink(): raise ValueError('Managed service definition must not be a symlink')
        if not target.is_file(): continue
        if kind == 'launchd':
            value = plistlib.loads(target.read_bytes())
            matches = value.get('Label') == name and value.get('WorkingDirectory') == str(base/'runtime')
        else:
            matches = 'WorkingDirectory='+str(base/'runtime').replace('%','%%') in target.read_text(encoding='utf-8').splitlines()
        if matches: owned.append(name)
    if len(owned)>1: raise ValueError('Both legacy and CodePier services exist; inspect before updating')
    return owned[0] if owned else names[0]


def default_install_base():
    new, old = Path.home()/'.codepier-agent', Path.home()/'.remote-dev-agent'
    if new.exists() and old.exists() and new.resolve()!=old.resolve():
        raise ValueError('Both CodePier and legacy installations exist; no merge attempted')
    if new.is_symlink() or old.is_symlink() and old.resolve()!=new:
        raise ValueError('Unrecognized installation symlink')
    return new if new.exists() or not old.exists() else old


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def display_command(args):
    # Recovery hints are pasted into PowerShell, not into cmd.exe.
    if os.name == 'nt':
        return '& ' + ' '.join("'" + str(arg).replace("'", "''") + "'" for arg in args)
    return shlex.join(args)


def service_identity():
    if sys.platform == 'darwin':
        return 'launchd', 'user'
    if sys.platform == 'win32':
        return 'schtasks', 'user'
    if sys.platform.startswith('linux'):
        return 'systemd', 'system' if os.geteuid() == 0 else 'user'
    return '', ''


def service_definition(base):
    kind, scope = service_identity()
    name = managed_service_name(base, kind, scope)
    if kind == 'launchd':
        return Path.home()/'Library/LaunchAgents'/f'{name}.plist'
    if kind == 'systemd':
        folder = Path('/etc/systemd/system') if scope == 'system' else Path.home()/'.config/systemd/user'
        return folder/name
    if kind == 'schtasks':
        return base/'service.xml'
    return None


def runtime_version(runtime):
    try:
        text = (runtime/'shared/util.py').read_text(encoding='utf-8')
        match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)', text, re.M)
        return match.group(1) if match else ''
    except OSError:
        return ''


def atomic_json_file(target, value):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+target.name+'-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        try: os.chmod(temporary, 0o600)
        except OSError: pass
        os.replace(temporary, target)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def update_management(base, **patch):
    target = base/'management.json'
    try:
        value = json.loads(target.read_text(encoding='utf-8')) if target.exists() else {}
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.update(patch)
    kind, scope = service_identity()
    if kind and value.get('service_kind') != 'none' and 'service_name' not in value:
        value['service_name'] = managed_service_name(base, kind, scope)
    value.update({'schema': 1, 'managed': True, 'layout': 'managed-runtime', 'product': 'CodePier', 'updated_at': time.time()})
    atomic_json_file(target, value)


def unpack(archive, expected, destination):
    raw = Path(archive).read_bytes()
    if len(raw) > 8*1024*1024 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Agent package checksum mismatch; generate a new command in the panel.')
    with zipfile.ZipFile(archive) as z:
        seen, total = set(), 0
        for item in z.infolist():
            path = PurePosixPath(item.filename)
            total += item.file_size
            if (item.is_dir() or item.filename in seen or path.is_absolute() or '..' in path.parts
                or '\\' in item.filename or ':' in item.filename or len(path.parts) != 2 and item.filename != 'requirements-agent.txt'
                or (item.external_attr >> 16) & 0o170000 == 0o120000 or total > 32*1024*1024):
                raise ValueError('Unsafe Agent archive')
            allowed = (len(path.parts) == 2 and path.parts[0] in {'agent','shared'} and path.suffix == '.py')
            allowed |= item.filename in {'scripts/install_agent.py', 'scripts/agent_lifecycle.py'}
            allowed |= item.filename in {'deploy/install-from-hub.sh','deploy/install-from-hub.ps1',
                'deploy/install-agent.sh','deploy/install-agent.ps1','deploy/start-agent.sh',
                'deploy/start-agent.cmd','deploy/agent.service'}
            if not allowed and item.filename != 'requirements-agent.txt':
                raise ValueError('Unexpected Agent archive member')
            seen.add(item.filename)
        if not {'agent/__main__.py','agent/lifecycle.py','shared/util.py','shared/agent_lifecycle.py',
                'requirements-agent.txt','scripts/install_agent.py','scripts/agent_lifecycle.py'} <= seen:
            raise ValueError('Incomplete Agent archive')
        for item in z.infolist():
            target = destination.joinpath(*PurePosixPath(item.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(item))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Panel enrollment redirected; regenerate using the final panel URL.')


def enroll(hub, ticket, expected_device=''):
    parsed = urllib.parse.urlsplit(hub)
    if parsed.scheme not in {'https','http'} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Invalid panel URL')
    headers={'Authorization':'Bearer '+ticket, 'Content-Type':'application/json'}
    if expected_device:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', expected_device):
            raise ValueError('Invalid expected panel device identity.')
        headers['X-CodePier-Expected-Device']=expected_device
    request = urllib.request.Request(hub.rstrip('/')+'/agent/enroll', data=b'', method='POST', headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            raw = response.read(16385)
        if len(raw) > 16384:
            raise ValueError('Invalid pairing response')
        pairing = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise ValueError('Install ticket belongs to another device; no runtime was replaced. The ticket was not consumed by a compatible Hub.') from exc
        raise ValueError('Pairing ticket rejected or expired; generate a new command in the panel.') from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError('Pairing response unavailable; generate a new command before retrying.') from exc
    if (not isinstance(pairing,dict) or not all(isinstance(pairing.get(k),str) and pairing[k] for k in ('device_id','secret','hub_url'))
        or pairing['hub_url'].rstrip('/') != hub.rstrip('/')):
        raise ValueError('Invalid pairing response')
    check_pairing_device(pairing, expected_device)
    return {k:pairing[k] for k in ('device_id','name','secret','hub_url') if k in pairing}


def check_pairing_device(pairing, expected_device):
    if expected_device and pairing.get('device_id') != expected_device:
        raise ValueError('Install ticket belongs to another device; no runtime was replaced.')


def systemctl():
    return ['systemctl'] if os.geteuid() == 0 else ['systemctl','--user']


def service_preflight(base):
    kind, scope = service_identity()
    if kind == 'launchd':
        folder = Path.home()/'Library/LaunchAgents'
        if any(is_legacy_launchd_name(path.stem) for path in folder.glob('*.plist')):
            raise ValueError('A legacy Agent service exists; update its original installation instead.')
    elif kind == 'systemd':
        folder = Path('/etc/systemd/system') if scope == 'system' else Path.home()/'.config/systemd/user'
        old = folder/'remote-dev-agent.service'
        if old.exists() or old.is_symlink(): raise ValueError('A legacy Agent service exists; update its original installation instead.')
    elif kind == 'schtasks':
        if windows_task_registered('RemoteDevAgent'):
            raise ValueError('A legacy Agent task exists; update its original installation instead.')
    if sys.platform == 'darwin':
        if os.geteuid() == 0:
            raise ValueError('Run this command in the Mac desktop account, without sudo.')
        run(['launchctl','print',f'gui/{os.getuid()}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
        if target.exists():
            raise ValueError('An Agent launch service already exists; preserve or remove it before a new installation.')
    elif sys.platform == 'win32':
        if windows_task_registered(TASK):
            raise ValueError('An Agent scheduled task already exists; preserve or remove it before a new installation.')
    elif sys.platform.startswith('linux'):
        if not shutil.which('systemctl'):
            raise ValueError('Autostart requires systemd. Use --no-service for containers, then start the printed command.')
        run(systemctl()+['show-environment'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = (Path('/etc/systemd/system') if os.geteuid()==0 else Path.home()/'.config/systemd/user')/'codepier-agent.service'
        if target.exists():
            raise ValueError('An Agent systemd service already exists; preserve or remove it before a new installation.')
    else:
        raise ValueError('Unsupported operating system')


def systemd_quote(value):
    # systemd specifier and environment expansion are independent of shell quoting.
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'


def windows_user_sid():
    rows = subprocess.check_output(['whoami.exe', '/user', '/fo', 'csv', '/nh'], text=True)
    sid = next(csv.reader(rows.splitlines()))[1]
    if not re.fullmatch(r'S-1-\d+(?:-\d+)+', sid):
        raise ValueError('Cannot determine the Windows installation account')
    return sid


def write_windows_wrapper(runtime):
    wrapper = runtime/'run-service.py'
    if wrapper.is_symlink():
        raise ValueError('Agent service wrapper must not be a symlink')
    wrapper.write_text('from pathlib import Path\n'
                       'from agent.service_watchdog import run_service\n'
                       'run_service(Path(__file__).resolve().parent.parent)\n', encoding='utf-8')


def windows_task_xml(base, python, name, user):
    """Boot without a desktop login, retaining the installation user's identity.

    The indefinite time trigger also recovers successful exits and exhausted
    RestartOnFailure attempts. IgnoreNew and the Agent lock prevent duplicates.
    """
    import xml.etree.ElementTree as ET
    ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', ns)
    def child(parent, tag, value=None):
        element = ET.SubElement(parent, '{'+ns+'}'+tag)
        if value is not None:
            element.text = value
        return element
    runtime = base/'runtime'
    windowless = python.with_name('pythonw.exe')
    if not windowless.is_file():
        raise ValueError('pythonw.exe is missing; repair the Agent Python environment before enabling background startup')
    task = ET.Element('{'+ns+'}Task', version='1.2')
    registration = child(task, 'RegistrationInfo')
    child(registration, 'URI', '\\'+name)
    # Allow this same user to stop/restart/disable the task during maintenance
    # even though initial BootTrigger registration requires UAC elevation.
    child(registration, 'SecurityDescriptor', 'D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;'+user+')')
    triggers = child(task, 'Triggers')
    boot = child(triggers, 'BootTrigger')
    child(boot, 'Enabled', 'true')
    child(boot, 'Delay', 'PT15S')
    timer = child(triggers, 'TimeTrigger')
    child(timer, 'StartBoundary', (datetime.now().astimezone()+timedelta(minutes=1)).isoformat(timespec='seconds'))
    child(timer, 'Enabled', 'true')
    repetition = child(timer, 'Repetition')
    child(repetition, 'Interval', 'PT1M')
    child(repetition, 'StopAtDurationEnd', 'false')
    principal = child(child(task, 'Principals'), 'Principal')
    principal.set('id', 'Author')
    child(principal, 'UserId', user)
    child(principal, 'LogonType', 'S4U')
    child(principal, 'RunLevel', 'LeastPrivilege')
    settings = child(task, 'Settings')
    for tag, value in (
        ('MultipleInstancesPolicy', 'IgnoreNew'), ('DisallowStartIfOnBatteries', 'false'),
        ('StopIfGoingOnBatteries', 'false'), ('AllowStartOnDemand', 'true'),
        ('StartWhenAvailable', 'true'), ('RunOnlyIfNetworkAvailable', 'false'),
        ('ExecutionTimeLimit', 'PT0S'), ('Enabled', 'true'),
    ):
        child(settings, tag, value)
    restart = child(settings, 'RestartOnFailure')
    child(restart, 'Interval', 'PT1M')
    child(restart, 'Count', '3')
    actions = child(task, 'Actions')
    actions.set('Context', 'Author')
    action = child(actions, 'Exec')
    child(action, 'Command', str(windowless))
    child(action, 'Arguments', subprocess.list2cmdline([str(runtime/'run-service.py')]))
    child(action, 'WorkingDirectory', str(runtime))
    return ET.tostring(task, encoding='utf-16', xml_declaration=True)


def register_windows_task(name, target):
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin():
        run(['schtasks.exe', '/Create', '/TN', name, '/XML', target, '/F'])
        return
    # Elevate only registration, not the Agent or the dependency installer. The
    # principal and ACL in XML remain bound to the original user's SID.
    def quote(value):
        return "'"+str(value).replace("'", "''")+"'"
    script = ('& schtasks.exe /Create /TN '+quote(name)+' /XML '+quote(target)+' /F; exit $LASTEXITCODE')
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    launch = ("$ErrorActionPreference='Stop'; try { $p=Start-Process powershell.exe -Verb RunAs -Wait -PassThru "
              "-ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand','"+encoded+"'); exit $p.ExitCode } catch { exit 1 }")
    try:
        run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', launch], timeout=180)
    except subprocess.SubprocessError as exc:
        raise ValueError('Windows boot startup needs administrator approval. Run --start-service in a local terminal and accept UAC; the installed Agent was preserved.') from exc


def query_windows_task_xml(name):
    """Read the live task as Unicode, independent of the console/OEM code page.

    schtasks /Query /XML can emit UTF-8 bytes while declaring UTF-16. The
    Task Scheduler COM Xml property is a Unicode BSTR; explicitly transport it
    as UTF-8, then parse text without guessing or replacing path characters.
    """
    import xml.etree.ElementTree as ET
    quoted = "'" + str(name).replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop'; "
              "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
              "$s=New-Object -ComObject Schedule.Service; $s.Connect(); "
              "$t=$s.GetFolder('\\').GetTask(" + quoted + "); "
              "[Console]::Write([string]$t.Xml)")
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    raw = subprocess.check_output(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        timeout=30)
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > 1024 * 1024:
            raise ValueError('Invalid task XML response size')
        text = raw.decode('utf-8-sig', errors='strict')
        if chr(0) in text or '<!doctype' in text.lower() or '<!entity' in text.lower():
            raise ValueError('Unsupported task XML content')
        ET.fromstring(text)
        return text
    except (UnicodeError, ET.ParseError, ValueError) as exc:
        raise ValueError('Cannot verify Windows scheduled task XML; the installed service was preserved.') from exc


def windows_task_has_recovery(raw, user=None):
    import xml.etree.ElementTree as ET
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    try:
        task = ET.fromstring(raw)
    except ET.ParseError:
        return False
    # Task Scheduler exports omit these default-valued elements. Defaults
    # apply only when the containing structure exists, never to a missing task
    # principal/trigger/settings or an explicitly empty/disabled/elevated field.
    # https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema
    defaults = {'Principals/Principal/RunLevel': 'LeastPrivilege',
                'Triggers/BootTrigger/Enabled': 'true',
                'Triggers/TimeTrigger/Enabled': 'true',
                'Settings/RunOnlyIfNetworkAvailable': 'false'}
    def value(path):
        parts = path.split('/')
        element = task.find('/'.join('t:'+part for part in parts), namespaces=ns)
        if element is not None:
            return element.text
        parent = task.find('/'.join('t:'+part for part in parts[:-1]), namespaces=ns)
        return defaults.get(path) if parent is not None else None
    if user is not None and value('Principals/Principal/UserId') != user:
        return False
    return (value('Principals/Principal/LogonType') == 'S4U'
            and value('Principals/Principal/RunLevel') == 'LeastPrivilege'
            and value('Triggers/BootTrigger/Enabled') == 'true'
            and value('Triggers/TimeTrigger/Enabled') == 'true'
            and value('Triggers/TimeTrigger/Repetition/Interval') == 'PT1M'
            and value('Triggers/TimeTrigger/Repetition/Duration') is None
            and value('Triggers/TimeTrigger/EndBoundary') is None
            and value('Settings/MultipleInstancesPolicy') == 'IgnoreNew'
            and value('Settings/ExecutionTimeLimit') == 'PT0S'
            and all(value('Settings/'+key) == 'false' for key in
                    ('DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries', 'RunOnlyIfNetworkAvailable'))
            and value('Settings/StartWhenAvailable') == 'true')


def start_service(base, python):
    name = managed_service_name(base, *service_identity())
    runtime = base/'runtime'
    config = base/'config.json'
    logs = base/'logs'
    logs.mkdir(exist_ok=True)
    command = [str(python), '-m', 'agent', '--config', str(config), 'run']
    if sys.platform == 'darwin':
        target = Path.home()/'Library/LaunchAgents'/f'{name}.plist'
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and plistlib.loads(target.read_bytes()).get('ProgramArguments') != command:
            raise ValueError('Existing launch service belongs to a different Agent configuration')
        data = {'Label':name,'ProgramArguments':command,'WorkingDirectory':str(runtime),
                'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'ProcessType':'Standard',
                'EnvironmentVariables':{'PYTHONUNBUFFERED':'1','CODEPIER_SUPERVISED':'1','PATH':os.environ.get('PATH','/usr/bin:/bin')},
                'StandardOutPath':str(logs/'stdout.log'),'StandardErrorPath':str(logs/'stderr.log')}
        target.write_bytes(plistlib.dumps(data)); target.chmod(0o600)
        domain=f'gui/{os.getuid()}/{name}'
        loaded=subprocess.run(['launchctl','print',domain],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if loaded.returncode != 0:run(['launchctl','bootstrap',f'gui/{os.getuid()}',target])
        run(['launchctl','kickstart',domain])
        time.sleep(2)
        status=subprocess.check_output(['launchctl','print',domain],text=True)
        if 'state = running' not in status:raise ValueError('Agent exited during startup; inspect '+str(logs/'stderr.log'))
    elif sys.platform == 'win32':
        if not (runtime/'agent/service_watchdog.py').is_file():
            raise ValueError('Upgrade the Agent runtime with the current source installer before repairing Windows boot startup')
        # Access/COM failures are not evidence of task absence. Never re-register
        # with /F after an unverified query failure.
        existing = windows_task_registered(name)
        if existing:
            try:
                verify_service_ownership(base)
            except ValueError:
                if (base/'service.xml').exists():
                    raise
                live=verify_windows_orphaned_registration(base,name)
                with (base/'service.xml').open('xb') as stream:
                    stream.write(live.encode('utf-16'))
            require_idle(base, read_installed_config(base))
        user = windows_user_sid()
        content = windows_task_xml(base, python, name, user)
        target = base/'service.xml'
        current = existing and windows_task_has_recovery(target.read_bytes(), user)
        if current:
            live = query_windows_task_xml(name)
            current = windows_task_has_recovery(live, user)
        pending = base/'service-pending.xml'
        if target.is_symlink() or pending.is_symlink():
            raise ValueError('Agent service definition must not be a symlink')
        if not current:
            pending.write_bytes(content)
            try:
                register_windows_task(name, pending)
            finally:
                pending.unlink(missing_ok=True)
        # Registration succeeds before replacing the previous service definition.
        if not current:
            target.write_bytes(content)
        update_management(base, service_kind='schtasks', service_scope='user')
        helper = load_lifecycle_helper(runtime/'scripts/agent_lifecycle.py')
        helper.stop_service(base)
        write_windows_wrapper(runtime)
        helper.start_service(base)
        helper.verify_service(base)
        update_management(base, startup='boot', service_logon='S4U', service_user=user,
                          recovery_interval_seconds=60, watchdog_timeout_seconds=120)
    else:
        target=(Path('/etc/systemd/system') if os.geteuid()==0 else Path.home()/'.config/systemd/user')/name
        if target.parent.is_symlink():
            raise ValueError('Managed service directory must not be a symlink')
        if target.exists() or target.is_symlink():
            # Repair may replace our definition, never another installation's.
            verify_service_ownership(base)
        target.parent.mkdir(parents=True,exist_ok=True)
        content = ('[Unit]\nDescription=CodePier Agent\nAfter=network-online.target\nWants=network-online.target\n'
            '[Service]\nType=simple\nWorkingDirectory='+str(runtime).replace('%','%%')+'\nExecStart='+
            ' '.join(systemd_quote(x) for x in command)+'\nRestart=always\nRestartSec=5\nUMask=0077\nEnvironment=PYTHONUNBUFFERED=1\nEnvironment=CODEPIER_SUPERVISED=1\n'
            '[Install]\nWantedBy='+('multi-user.target' if os.geteuid()==0 else 'default.target')+'\n')
        target.write_text(content);target.chmod(0o600)
        run(systemctl()+['daemon-reload']);run(systemctl()+['enable','--now',name])
        time.sleep(2)
        run(systemctl()+['is-active','--quiet',name])
    return command



def read_installed_config(base):
    path = base/'config.json'
    if path.is_symlink() or (base/'runtime').is_symlink():
        raise ValueError('Agent configuration/runtime must not be a symlink')
    if not path.is_file() or path.stat().st_size > 1024*1024:
        raise ValueError('No valid installed Agent found; use a panel installation command first.')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k] for k in ('device_id','hub_url')):
        raise ValueError('Existing Agent config is invalid; no files were changed.')
    return value


def check_existing_panel_target(current, expected_device='', hub=''):
    """Reject identity/Hub changes before preparing or replacing an installation."""
    if expected_device and current['device_id'] != expected_device:
        identity = current['device_id']
        hint = (' Existing local device ID: '+identity+'.') if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity) else ''
        raise ValueError('This command belongs to a different device; a different panel node is already recorded on this computer. '
                         'No files were changed and the ticket was not used.'+hint+
                         ' To keep this Agent, select its original node and copy that node\'s install or upgrade command. '
                         'To intentionally change nodes, use the Windows archive recovery command with local confirmation, '
                         'then reinstall. Do not delete config.json.')
    if hub and current['hub_url'].rstrip('/') != hub.rstrip('/'):
        raise ValueError('This command belongs to a different panel (Hub); no files were changed and the ticket was not used. '
                         'Select the original node on its original panel to upgrade. '
                         'To intentionally move to another panel, archive the old installation with local confirmation first.')


def validate_install_base(base):
    # Accept only the precise migration alias, never arbitrary symlinks.
    if base.name == '.remote-dev-agent' and (base.is_symlink() or getattr(base, 'is_junction', lambda: False)()) and base.resolve() == base.with_name('.codepier-agent'):
        base = base.resolve()
    if (not base.is_absolute() or base.is_symlink() or getattr(base, 'is_junction', lambda: False)() or len(base.parts) < 3
            or any(ord(c) < 32 or ord(c) == 127 for c in str(base))):
        raise ValueError('Install directory must be a dedicated absolute directory, not a symlink or filesystem root')
    resolved = base.resolve()
    home = Path.home().resolve()
    if resolved == home or resolved in home.parents:
        raise ValueError('Refusing to use the home directory or its ancestors as the Agent installation')
    return resolved


def load_lifecycle_helper(source):
    import importlib.util
    spec = importlib.util.spec_from_file_location('codepier_installer_lifecycle', source)
    if not spec or not spec.loader:
        raise ValueError('Lifecycle helper is unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_service_ownership(base, *, allow_missing=False):
    """Never operate on the fixed service name for a different installation."""
    kind, scope = service_identity()
    name = managed_service_name(base, kind, scope)
    target = service_definition(base)
    metadata_path = base/'management.json'
    if metadata_path.is_symlink():
        raise ValueError('Management metadata must not be a symlink')
    metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.exists() else {}
    if not isinstance(metadata, dict):
        raise ValueError('Invalid Agent management metadata')
    if metadata.get('service_kind') not in (None, '', kind) or metadata.get('service_scope') not in (None, '', scope):
        raise ValueError('Run as the original installation account; service scope does not match (do not add sudo).')
    if not target or target.is_symlink():
        raise ValueError('No safe managed service definition found')
    if not target.is_file():
        # A missing file is not proof that the globally named service is absent.
        # Refuse rather than stopping an unrelated loaded service.
        raise ValueError('Existing Agent is not a managed service; no files were changed.')
    runtime = base/'runtime'
    config = base/'config.json'
    if kind == 'launchd':
        value = plistlib.loads(target.read_bytes())
        args = value.get('ProgramArguments', [])
        owned = (value.get('Label') == name and value.get('WorkingDirectory') == str(runtime)
                 and isinstance(args, list) and '--config' in args
                 and args[args.index('--config')+1:args.index('--config')+2] == [str(config)]
                 and args[:3] == [str(runtime/'.venv/bin/python'), '-m', 'agent'])
    elif kind == 'systemd':
        text = target.read_text(encoding='utf-8')
        command = [str(runtime/'.venv/bin/python'), '-m', 'agent', '--config', str(config), 'run']
        owned = ('WorkingDirectory='+str(runtime).replace('%','%%') in text.splitlines()
                 and 'ExecStart='+' '.join(systemd_quote(x) for x in command) in text.splitlines())
    elif kind == 'schtasks':
        import xml.etree.ElementTree as ET
        ns = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        value = ET.fromstring(target.read_bytes())
        action = value.find('t:Actions/t:Exec', ns)
        owned = (action is not None and action.findtext('t:WorkingDirectory', namespaces=ns) == str(runtime)
                 and action.findtext('t:Command', namespaces=ns) in
                     [str(runtime/'.venv/Scripts/python.exe'), str(runtime/'.venv/Scripts/pythonw.exe')]
                 and action.findtext('t:Arguments', namespaces=ns) == subprocess.list2cmdline([str(runtime/'run-service.py')]))
        # The live scheduled task, not just our saved XML, must also belong to us.
        live = query_windows_task_xml(name)
        live_action = ET.fromstring(live).find('t:Actions/t:Exec', ns)
        owned = owned and live_action is not None and all(
            action.findtext('t:'+field, namespaces=ns) == live_action.findtext('t:'+field, namespaces=ns)
            for field in ('Command','Arguments','WorkingDirectory'))
    else:
        owned = False
    if not owned:
        raise ValueError('Existing service belongs to a different Agent configuration; no files were changed.')


def require_idle(base, current, *, recover_stale_operations=False):
    """Never infer dead Agent == dead child. Recovery needs explicit local review."""
    import sqlite3
    state = Path(current.get('state_dir') or base/'state').expanduser()
    database = state/'agent.sqlite3'
    active = 0
    if database.exists():
        try:
            with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=3) as db:
                active = db.execute("SELECT COUNT(*) FROM calls WHERE status IN ('running','accepted')").fetchone()[0]
        except sqlite3.Error as exc:
            raise ValueError('Cannot verify Agent task state; inspect the local journal before retrying.') from exc
    native = state/'native-cli/native.sqlite3'
    if native.exists():
        try:
            with sqlite3.connect(native.resolve().as_uri()+'?mode=ro', uri=True, timeout=3) as db:
                sessions = db.execute("SELECT COUNT(*) FROM sessions WHERE status IN ('starting','running','stopping','orphaned')").fetchone()[0]
        except sqlite3.Error as exc:
            raise ValueError('Cannot verify native terminal session state; inspect sessions before maintenance.') from exc
        if sessions:
            raise ValueError('Native terminal sessions are active. Explicitly stop them before Agent maintenance; stale-operation recovery does not override them.')
    plans = state/'lifecycle'
    if plans.is_dir() and any(p.suffix in ('.json', '.claimed') for p in plans.iterdir()):
        raise ValueError('An Agent lifecycle operation is pending; inspect it before retrying.')
    if not active:
        return
    if not recover_stale_operations:
        raise ValueError('Agent has active operations. Finish them first. If the Agent is dead, locally inspect residual child processes and results, then explicitly use --recover-stale-operations; this never reruns a command.')
    helper = Path(__file__).resolve().parent.parent/'shared/instance_lock.py'
    if not helper.is_file():
        helper = base/'runtime/shared/instance_lock.py'
    locks = load_lifecycle_helper(helper)
    try:
        with locks.InstanceLock(state/'.agent.lock'):
            # The lock fences any starting Agent while the original call IDs are
            # made terminal. It is not evidence that residual commands stopped.
            with sqlite3.connect(database, timeout=3) as db:
                db.execute('BEGIN IMMEDIATE')
                rows=db.execute("SELECT id,status FROM calls WHERE status IN ('running','accepted')").fetchall()
                audit={'created':time.time(),'action':'explicit_local_stale_operation_recovery',
                       'residual_processes_reviewed_by_owner':True,
                       'commands_reexecuted':False,'side_effects_reversed':False,
                       'calls':[{'operation_id':row[0],'prior_status':row[1]} for row in rows]}
                record=state/('maintenance-recovery-'+hashlib.sha256(os.urandom(16)).hexdigest()[:16]+'.json')
                with record.open('x',encoding='utf-8') as stream:
                    os.chmod(record,0o600);json.dump(audit,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
                result=json.dumps({'ok':False,'error':{'code':'INTERRUPTED','retryable':False,
                    'message':'Owner explicitly reconciled stale journal after checking the stopped Agent and residual processes; execution was not replayed or reported successful.'}})
                db.execute("UPDATE calls SET status='interrupted',result=?,acked=0 WHERE status IN ('running','accepted')",(result,))
    except RuntimeError as exc:
        raise ValueError('Agent still owns its instance lock. No stale calls were recovered; stop and inspect the real Agent first.') from exc
    print('Stale call IDs preserved as interrupted; no command was rerun and no result was marked successful.')


def external_python(base):
    candidate = Path(getattr(sys, '_base_executable', None) or sys.executable).resolve()
    if not candidate.is_file() or candidate.is_relative_to(base/'runtime') or candidate.name.endswith('-config'):
        raise ValueError('A Python interpreter outside runtime is required for safe replacement.')
    return candidate


def find_uv(base, supplied=None):
    metadata_path = base/'management.json'
    metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.is_file() else {}
    candidates = [supplied, metadata.get('uv'), base/'tools'/('uv.exe' if os.name=='nt' else 'uv'), shutil.which('uv')]
    for raw in candidates:
        if raw:
            candidate = Path(raw).expanduser()
            if candidate.is_file():
                return candidate.resolve()
    raise ValueError('uv is unavailable; use a new installation / repair command from the panel.')


def fetch_update(base, current, temporary):
    hub = current['hub_url'].rstrip('/')
    parsed = urllib.parse.urlsplit(hub)
    if (parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('Invalid configured panel URL')
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(hub+'/agent/manifest.json', timeout=30) as response:
            raw = response.read(16385)
        if len(raw) > 16384:
            raise ValueError('Invalid update manifest size')
        manifest = json.loads(raw)
        sha = manifest.get('sha256') if isinstance(manifest, dict) else None
        size = manifest.get('bytes') if isinstance(manifest, dict) else None
        if (not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha)
                or type(size) is not int or not 1 <= size <= 8*1024*1024):
            raise ValueError('Invalid update manifest')
        # Ignore any URL supplied by the manifest: the download stays on our configured Hub.
        with opener.open(hub+'/agent/agent.zip?sha256='+sha, timeout=120) as response:
            raw = response.read(8*1024*1024+1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('Agent package size or checksum mismatch; installed runtime was preserved.')
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError('This panel does not provide command-line updates yet; update the panel or use its installation / repair command.') from exc
        raise ValueError('Agent update download failed; installed runtime was preserved.') from exc
    archive = temporary/'agent.zip'
    archive.write_bytes(raw)
    return archive, sha


def install_cli_links(base):
    if (base/'runtime/deploy/install-from-hub.sh').is_file():
        target = base/'agentctl'
        if target.is_symlink():
            raise ValueError('Refusing to overwrite a symlinked Agent command')
        target.write_text('#!/usr/bin/env bash\nset -euo pipefail\n'
                          'base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)\n'
                          'exec bash "$base/runtime/deploy/install-from-hub.sh" --install-dir "$base" "$@"\n', encoding='utf-8')
        target.chmod(0o700)
    if (base/'runtime/deploy/install-from-hub.ps1').is_file():
        target = base/'agentctl.ps1'
        if target.is_symlink():
            raise ValueError('Refusing to overwrite a symlinked Agent command')
        target.write_text("$ErrorActionPreference = 'Stop'\n"
                          "& (Join-Path $PSScriptRoot 'runtime/deploy/install-from-hub.ps1') -InstallDir $PSScriptRoot @args\n",
                          encoding='utf-8')

    # Canonical management commands; old agentctl remains an explicit alias.
    for old, new in [('agentctl', 'codepier-agent'), ('agentctl.ps1', 'codepier-agent.ps1')]:
        source, target = base / old, base / new
        if source.is_file():
            if target.is_symlink():
                raise ValueError('Refusing a symlinked CodePier command')
            target.write_bytes(source.read_bytes())
            target.chmod(0o700)


def pending_windows_install(base):
    if sys.platform != 'win32':
        return False
    path=base/'management.json'
    if not path.exists():
        return False
    value=json.loads(_read_owned_service_file(path))
    return (isinstance(value,dict) and value.get('managed') is True
            and value.get('layout') == 'managed-runtime'
            and value.get('service_kind') == 'schtasks' and value.get('service_scope') == 'user'
            and value.get('status') in ('service_pending','service_error'))


def resume_windows_service_install(base,current):
    """Repair an owned, paired first install; never initialize or replace config."""
    if not pending_windows_install(base):
        return False
    runtime=base/'runtime'
    python=runtime/'.venv/Scripts/python.exe'
    if not python.is_file() or not (runtime/'agent/service_watchdog.py').is_file():
        raise ValueError('Pending Windows installation has an incomplete runtime; use the verified archive recovery command.')
    before=(base/'config.json').read_bytes()
    require_idle(base,current)
    verify_windows_instance_is_stopped(base,current)
    try:
        start_service(base,python)
        if (base/'config.json').read_bytes() != before:
            raise ValueError('Agent configuration changed while resuming Windows startup.')
        install_cli_links(base)
        update_management(base,service=True,status='ready',last_error='')
    except Exception as exc:
        update_management(base,service=False,status='service_error',last_error=str(exc)[:500])
        raise
    return True


def local_upgrade(base, args, current, *, repair_service=True):
    try:
        verify_service_ownership(base)
    except ValueError:
        # An already-owned registration can update a damaged old runtime.
        # Only an unregistered/incomplete first service setup needs resuming;
        # never require the old executable to boot before staging its replacement.
        if not pending_windows_install(base):
            raise
        resume_windows_service_install(base, current)
        verify_service_ownership(base)
    require_idle(base, current, recover_stale_operations=getattr(args,"recover_stale_operations",False))
    before = (base/'config.json').read_bytes()
    interpreter, uv = external_python(base), find_uv(base, args.uv)
    candidate = base/('.runtime-update-cli-'+hashlib.sha256(os.urandom(16)).hexdigest()[:12])
    with tempfile.TemporaryDirectory(prefix='codepier-upgrade-') as folder:
        temporary = Path(folder)
        archive, sha = (Path(args.archive), args.sha256) if args.archive and args.sha256 else fetch_update(base, current, temporary)
        candidate.mkdir()
        try:
            unpack(archive, sha, candidate)
            python = candidate/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            run([uv,'venv','--relocatable','--python',interpreter,candidate/'.venv'], timeout=600)
            run([uv,'pip','install','--require-hashes','--python',python,'-r',candidate/'requirements-agent.txt'], timeout=900)
            run([python,'-c','import agent.config, agent.runner, agent.lifecycle'], cwd=candidate, timeout=60)
            if sys.platform == 'win32' and (candidate/'agent/service_watchdog.py').is_file():
                write_windows_wrapper(candidate)
            if (base/'config.json').read_bytes() != before:
                raise ValueError('Configuration changed during preparation; no runtime was replaced. Retry after local edits finish.')
            require_idle(base, current, recover_stale_operations=getattr(args,"recover_stale_operations",False))
            verify_service_ownership(base)
            kind, scope = service_identity()
            update_management(base, service=True, service_kind=kind, service_scope=scope,
                              helper_python=str(interpreter), uv=str(uv), status='updating', last_error='')
            helper = temporary/'agent_lifecycle.py'
            shutil.copyfile(candidate/'scripts/agent_lifecycle.py', helper)
            try:
                run([interpreter,helper,'--install-dir',base,'--wait-pid','0','--apply-update',candidate], cwd=temporary, timeout=240)
            except Exception:
                metadata=json.loads((base/'management.json').read_text(encoding='utf-8'))
                if metadata.get('status') == 'updating':
                    update_management(base,status='update_failed',last_error='Command upgrade did not complete; inspect the local installer output before retrying.')
                raise
            install_cli_links(base)
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)
    if repair_service and sys.platform == 'win32' and (base/'runtime/agent/service_watchdog.py').is_file():
        start_service(base, base/'runtime/.venv/Scripts/python.exe')
    print('Agent upgraded. Configuration, device identity, project permissions and history were preserved.')
    print('Installed runtime: '+runtime_version(base/'runtime'))


def local_uninstall(base, args, current):
    verify_service_ownership(base, allow_missing=True)
    require_idle(base, current, recover_stale_operations=getattr(args,"recover_stale_operations",False))
    for root in current.get('allowed_roots', []):
        raw = root.get('path') if isinstance(root, dict) else root
        if isinstance(raw, str) and Path(raw).expanduser().resolve().is_relative_to(base):
            raise ValueError('An authorized project directory is inside the Agent installation. Move it out before uninstalling; nothing was deleted.')
    print('This removes the local Agent service, runtime, credentials, logs and state: '+str(base))
    print('Project files outside this directory and the panel device record will be preserved.')
    if not args.yes:
        expected = current.get('name') or current['device_id']
        try:
            answer = input('Type '+expected+' to confirm uninstall: ')
        except EOFError as exc:
            raise ValueError('Uninstall requires confirmation. Use --yes only after backing up needed Agent data.') from exc
        if answer != expected:
            raise ValueError('Uninstall cancelled; confirmation did not match.')
    with tempfile.TemporaryDirectory(prefix='codepier-uninstall-') as folder:
        temporary = Path(folder)
        if args.archive and args.sha256:
            source = temporary/'package'
            unpack(args.archive,args.sha256,source)
            helper = source/'scripts/agent_lifecycle.py'
        else:
            # A new source-package command may target an older installed Agent.
            # Use this installer's helper, not the old unchecked uninstaller.
            helper = Path(__file__).resolve().with_name('agent_lifecycle.py')
        module = load_lifecycle_helper(helper)
        module.uninstall(base, 0)
    if os.name == 'nt':
        print('Service removed; Windows directory cleanup was handed off. The PowerShell launcher verifies removal after Python exits.')
    elif base.exists():
        raise ValueError('Agent directory remains; uninstall was not completed.')
    else:
        print('Agent uninstalled. Panel records and project files were preserved.')


def windows_task_registered(name):
    """Distinguish a missing task from a Task Scheduler access/error failure."""
    quoted = "'" + name.replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop'; "
              "$service=New-Object -ComObject Schedule.Service; $service.Connect(); "
              "try { $null=$service.GetFolder('\\').GetTask("+quoted+"); exit 10 } "
              "catch { $e=$_.Exception; for($i=0; $i -lt 8 -and $null -ne $e; $i++){ "
              "if ($e.HResult -eq -2147024894) { exit 0 }; $e=$e.InnerException }; exit 11 }")
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive',
                             '-EncodedCommand',encoded],
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    if result.returncode not in (0,10):
        raise ValueError('Cannot verify Windows scheduled tasks; no Agent files were changed.')
    return result.returncode == 10


def verify_windows_orphaned_registration(base, name):
    """Only rescue a task registered during a failed first boot setup.

    The installer can crash after Task Scheduler registration but before the
    local service.xml checkpoint is written. A live task must match the exact
    installation path and original account; never trust the global task name.
    """
    import xml.etree.ElementTree as ET
    management = base/'management.json'
    if not management.is_file() or management.is_symlink():
        raise ValueError('Cannot verify orphaned Agent service ownership.')
    record = json.loads(management.read_text(encoding='utf-8'))
    if (not isinstance(record,dict) or record.get('managed') is not True
        or record.get('layout') != 'managed-runtime'
        or record.get('status') not in {'service_pending','service_error'}
        or record.get('service_kind') != 'schtasks'
        or record.get('service_scope') != 'user'
        or record.get('service_name',name) != name):
        raise ValueError('Cannot verify orphaned Agent service ownership.')
    namespace = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    live_xml = query_windows_task_xml(name)
    if not windows_task_has_recovery(live_xml,windows_user_sid()):
        raise ValueError('Live task startup settings are not verified as this Agent.')
    task = ET.fromstring(live_xml)
    if len(task.findall('t:Actions/t:Exec',namespace)) != 1:
        raise ValueError('Unexpected Windows task actions; no files were changed.')
    action = task.find('t:Actions/t:Exec',namespace)
    principal = task.find('t:Principals/t:Principal',namespace)
    runtime = base/'runtime'
    if (action is None or principal is None
        or action.findtext('t:WorkingDirectory',namespaces=namespace) != str(runtime)
        or action.findtext('t:Command',namespaces=namespace) not in
            (str(runtime/'.venv/Scripts/python.exe'),str(runtime/'.venv/Scripts/pythonw.exe'))
        or action.findtext('t:Arguments',namespaces=namespace) !=
            subprocess.list2cmdline([str(runtime/'run-service.py')])
        or principal.findtext('t:UserId',namespaces=namespace) != windows_user_sid()
        or principal.findtext('t:LogonType',namespaces=namespace) != 'S4U'):
        raise ValueError('Live scheduled task is not verified as this failed Agent; no files were changed.')
    return live_xml


def verify_windows_instance_is_stopped(base, current):
    """Refuse archive if an Agent process still holds the state lock."""
    state = Path(current.get('state_dir') or base/'state').expanduser()
    lock = state/'.agent.lock'
    if lock.is_symlink():
        raise ValueError('Agent state lock is a symlink; manual review required.')
    if os.name != 'nt' or not lock.exists():
        return
    import msvcrt
    try:
        with lock.open('r+b') as stream:
            stream.seek(0)
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            try:
                pass
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
    except OSError as exc:
        raise ValueError('An Agent instance still owns the state lock; do not archive a running Agent.') from exc


def archive_fingerprints(base):
    """Bounded snapshots contain hashes only, never pairing keys in a handoff."""
    values = {}
    for name in ('config.json','management.json','service.xml'):
        path = base/name
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)():
            raise ValueError('Archive metadata cannot be a link.')
        values[name] = hashlib.sha256(_read_owned_service_file(path)).hexdigest() if path.exists() else None
    return values


def release_archive_lock(base, lock_token):
    lock = base/'.install.lock'
    owner = lock/'archive.json'
    if not lock.exists():
        return
    if lock.is_symlink() or owner.is_symlink():
        raise ValueError('Archive maintenance lock changed; manual review required.')
    if not owner.exists():
        lock.rmdir()
        return
    value = json.loads(owner.read_text(encoding='utf-8'))
    if value.get('lock_token') != lock_token:
        raise ValueError('Archive maintenance lock belongs to another invocation.')
    owner.unlink()
    lock.rmdir()


def archive_windows_install(base, args):
    """Confirmed recovery with a lock surviving deferred Windows rename."""
    if sys.platform != 'win32':
        raise ValueError('Archiving a failed installation is supported only on Windows.')
    base = validate_install_base(base)
    if not base.is_dir() or not any((base/name).exists() for name in ('config.json','runtime','management.json')):
        raise ValueError('No recognizable Agent installation to archive.')
    lock = base/'.install.lock'
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ValueError('An Agent installation is already active or needs lock review; no files were changed.') from exc
    lock_token = os.urandom(16).hex()
    destination = base
    transferred = False
    try:
        atomic_json_file(lock/'archive.json',{'schema':1,'lock_token':lock_token,'pid':os.getpid()})
        before = archive_fingerprints(base)
        destination, transferred = _archive_windows_install_locked(base,args,lock_token,before)
    finally:
        if not transferred:
            release_archive_lock(destination,lock_token)


def _archive_windows_install_locked(base,args,lock_token,before):
    config, runtime, management = base/'config.json', base/'runtime', base/'management.json'
    handoff = Path(args.archive_handoff_file).expanduser() if getattr(args,'archive_handoff_file','') else None
    if handoff:
        if (not handoff.is_absolute() or handoff.exists() or handoff.is_symlink()
            or not handoff.parent.is_dir() or handoff.parent.is_symlink()
            or handoff.resolve().is_relative_to(base)):
            raise ValueError('Invalid external Windows archive handoff path; no files were changed.')
    elif Path(sys.executable).resolve().is_relative_to(base):
        raise ValueError('The Python interpreter is inside this Agent installation. '
                         'Use the panel Windows archive recovery command so PowerShell can move it after Python exits.')
    for item in (config,runtime,management,base/'service.xml',base/'state'):
        if item.is_symlink() or getattr(item,'is_junction',lambda:False)():
            raise ValueError('Agent installation contains a symlink; preserve it for manual recovery.')
    current = read_installed_config(base) if config.exists() else None
    if current:
        if args.expected_device and current['device_id'] != args.expected_device:
            raise ValueError('This command belongs to a different device; no files were changed.')
        require_idle(base,current)
        verify_windows_instance_is_stopped(base,current)
        roots = current.get('allowed_roots') or []
        if not isinstance(roots,list):
            raise ValueError('Existing allowed_roots configuration is invalid; manual review required.')
        for root in roots:
            path = root.get('path') if isinstance(root,dict) else root
            if isinstance(path,str) and Path(path).expanduser().resolve().is_relative_to(base):
                raise ValueError('An authorized project directory is inside the Agent installation. '
                                 'Move it outside before archiving; nothing was changed.')
    elif (base/'state').exists() and any((base/'state').iterdir()):
        raise ValueError('Unpaired installation has state files; inspect these before archiving.')
    registered = [name for name in (TASK,'RemoteDevAgent') if windows_task_registered(name)]
    if registered:
        if not current or len(registered) != 1 or registered[0] != managed_service_name(base,*service_identity()):
            raise ValueError('An unverified Windows Agent task exists; nothing was changed.')
        try:
            verify_service_ownership(base)
        except ValueError:
            if (base/'service.xml').exists():
                raise
            verify_windows_orphaned_registration(base,registered[0])
        # No other live command/session may be displaced. The owned task is
        # disabled and removed only after explicit confirmation below.
    marker = current['device_id'] if current else 'ARCHIVE INCOMPLETE'
    print('Archive local Agent installation to a timestamped sibling backup: '+str(base),flush=True)
    print('The local Agent service will be removed if it belongs to this directory. '
          'Configuration, credentials, state and logs will NOT be deleted.',flush=True)
    print('The panel node record and any external project files remain unchanged.',flush=True)
    try:
        answer = input('Type '+marker+' to confirm local archive: ')
    except EOFError as exc:
        raise ValueError('Local archive needs interactive confirmation; no files were changed.') from exc
    if answer != marker:
        raise ValueError('Archive cancelled; confirmation did not match.')
    backup = base.with_name(base.name+'.recovery-'+time.strftime('%Y%m%d-%H%M%S')+'-'
                            +hashlib.sha256(os.urandom(16)).hexdigest()[:8])
    if backup.exists():
        raise ValueError('Recovery backup already exists; no files were changed.')
    if archive_fingerprints(base) != before:
        raise ValueError('Agent configuration or service definition changed during confirmation; no service was stopped.')
    if [name for name in (TASK,'RemoteDevAgent') if windows_task_registered(name)] != registered:
        raise ValueError('Windows task registration changed during confirmation; archive refused.')
    if current:
        require_idle(base,current)
        verify_windows_instance_is_stopped(base,current)
    if registered:
        try:
            verify_service_ownership(base)
        except ValueError:
            if (base/'service.xml').exists():
                raise
            verify_windows_orphaned_registration(base,registered[0])
        saved_service=(base/'service.xml').read_bytes() if (base/'service.xml').exists() else None
        service_snapshot=base/('.recovery-service-'+lock_token+'.xml')
        if saved_service is not None:
            with service_snapshot.open('xb') as stream:
                stream.write(saved_service);stream.flush();os.fsync(stream.fileno())
        # Use only the pinned package helper or a complete local source checkout.
        # A corrupt/obsolete installed runtime must not provide uninstall logic.
        if args.archive and args.sha256:
            with tempfile.TemporaryDirectory(prefix='codepier-archive-helper-') as folder:
                checked = Path(folder)/'verified'
                unpack(args.archive,args.sha256,checked)
                helper = load_lifecycle_helper(checked/'scripts/agent_lifecycle.py')
                helper.stop_service(base,remove=True)
        else:
            helper_source = Path(__file__).resolve().with_name('agent_lifecycle.py')
            if not helper_source.is_file():
                raise ValueError('Verified service removal helper unavailable; no files were changed.')
            load_lifecycle_helper(helper_source).stop_service(base,remove=True)
        if any(windows_task_registered(name) for name in (TASK,'RemoteDevAgent')):
            raise ValueError('Scheduled task remains registered or changed. Installation was preserved.')
        # stop_service(remove=True) removes its file as well as the registration.
        # Preserve the exact saved definition inside the recovery archive.
        if saved_service is not None and not (base/'service.xml').exists():
            with (base/'service.xml').open('xb') as stream:
                stream.write(saved_service)
        if saved_service is not None:
            service_snapshot.unlink()
    if archive_fingerprints(base)['config.json'] != before['config.json']:
        raise ValueError('Agent configuration changed while stopping service; original files were preserved.')
    if handoff:
        # The bootstrap owns the final rename after this interpreter exits;
        # Windows refuses to rename a directory containing a running python.exe.
        atomic_json_file(handoff,{'schema':1,'source':str(base),'backup':str(backup),
                                 'lock_token':lock_token,'files':archive_fingerprints(base)})
        print('Agent archive checks complete. Waiting for PowerShell to release and move the directory.',flush=True)
        return backup, True
    base.rename(backup)
    print('Agent installation archived: '+str(backup),flush=True)
    print('The old credentials and state are preserved inside this backup. '
          'Generate a fresh install command from the intended panel node.',flush=True)

    return backup, False


def install_source(base, source, pairing_file=None, allowed=None, *, recover_stale_operations=False, shell="full"):
    """Import a Windows checkout into the Hub installer's managed layout.

    Preserve existing source-install configuration byte for byte. Never copy
    the checkout's venv, credentials, projects or state into the runtime.
    """
    source = Path(source).expanduser().resolve()
    runtime = base/'runtime'
    current = read_installed_config(base) if (base/'config.json').exists() else None
    if not current and (not pairing_file or not allowed):
        raise ValueError('A pairing file and allowed directory are required for the first installation')
    if not current:
        if not Path(pairing_file).expanduser().is_file() or not Path(allowed).expanduser().is_dir():
            raise ValueError('Pairing file and allowed directory must exist')
    if runtime.exists():
        if not current:
            raise ValueError('Existing runtime has no configuration; preserve it before reinstalling')
        verify_service_ownership(base)
        require_idle(base, current, recover_stale_operations=recover_stale_operations)
    else:
        service_preflight(base)
        if current:
            require_idle(base, current, recover_stale_operations=recover_stale_operations)
            locks = load_lifecycle_helper(source/'shared/instance_lock.py')
            try:
                with locks.InstanceLock(Path(current.get('state_dir') or base/'state')/'.agent.lock'):
                    pass
            except RuntimeError as exc:
                raise ValueError('Close the old foreground Agent window, then run this installer again. Configuration was preserved.') from exc
    with tempfile.TemporaryDirectory(prefix='codepier-source-install-') as temporary:
        archive = Path(temporary)/'agent.zip'
        names = ['requirements-agent.txt', 'scripts/install_agent.py', 'scripts/agent_lifecycle.py',
                 'deploy/install-from-hub.sh', 'deploy/install-from-hub.ps1',
                 'deploy/install-agent.sh', 'deploy/install-agent.ps1',
                 'deploy/start-agent.sh', 'deploy/start-agent.cmd', 'deploy/agent.service']
        for directory in ('agent', 'shared'):
            if (source/directory).is_symlink():
                raise ValueError('Source package directory must not be a symlink')
            names.extend(p.relative_to(source).as_posix() for p in sorted((source/directory).glob('*.py')))
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package:
            for name in names:
                path = source/name
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Missing or unsafe source package member: '+name)
                package.write(path, name)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        unpack(archive, digest, Path(temporary)/'checked')
        interpreter = Path(getattr(sys, '_base_executable', None) or sys.executable).resolve()
        try:
            uv = find_uv(base)
        except ValueError:
            tools_env = base/'tools/installer'
            run([interpreter, '-m', 'venv', tools_env])
            tool_python = tools_env/'Scripts/python.exe'
            run([tool_python, '-m', 'pip', 'install', 'uv==0.10.8'])
            uv = tools_env/'Scripts/uv.exe'
        if runtime.exists():
            update_management(base, uv=str(uv), helper_python=str(interpreter))
            local_upgrade(base, argparse.Namespace(uv=str(uv), archive=str(archive), sha256=digest), current, repair_service=False)
        else:
            runtime.mkdir()
            prepared = False
            try:
                unpack(archive, digest, runtime)
                python = runtime/'.venv/Scripts/python.exe'
                run([uv, 'venv', '--relocatable', '--python', interpreter, runtime/'.venv'])
                run([uv, 'pip', 'install', '--require-hashes', '--python', python, '-r', runtime/'requirements-agent.txt'])
                run([python, '-c', 'import agent.config, agent.runner, agent.lifecycle'], cwd=runtime)
                if current is None:
                    run([python, '-m', 'agent', '--config', base/'config.json', 'init',
                         '--pairing-file', Path(pairing_file).expanduser().resolve(),
                         '--allow', Path(allowed).expanduser().resolve(), '--shell', shell], cwd=runtime)
                prepared = True
            finally:
                if not prepared:
                    shutil.rmtree(runtime)
            update_management(base, managed=True, service=False, service_kind='schtasks', service_scope='user',
                              uv=str(uv), helper_python=str(interpreter), installed_version=runtime_version(runtime),
                              status='service_pending', last_error='')
        install_cli_links(base)
        try:
            start_service(base, runtime/'.venv/Scripts/python.exe')
        except Exception as exc:
            update_management(base, status='service_error', last_error=str(exc)[:500])
            raise
        update_management(base, service=True, status='ready', last_error='')
    print('Agent installed as a background task. It starts at boot before login and recovers automatically.')
    print('The terminal can now be closed. Logs: '+str(base/'logs'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive');parser.add_argument('--sha256');parser.add_argument('--hub');parser.add_argument('--allow')
    parser.add_argument('--uv');parser.add_argument('--install-dir',default=None)
    parser.add_argument('--source', help='Install a local Windows source package into the managed runtime')
    parser.add_argument('--pairing-file')
    parser.add_argument('--shell', choices=('full', 'disabled'), default='full', help='Fresh installation execution mode; existing configuration is always preserved')
    parser.add_argument('--no-service',action='store_true')
    actions=parser.add_mutually_exclusive_group()
    for flag in ('--start-service','--upgrade','--uninstall','--status','--archive-install'):
        actions.add_argument(flag,action='store_true')
    parser.add_argument('--yes',action='store_true',help='Explicitly confirm destructive local uninstall')
    parser.add_argument('--recover-stale-operations',action='store_true',help='After locally reviewing residual processes, reconcile a dead Agent journal without replaying commands; refuses a live Agent or native session')
    parser.add_argument('--expected-device',default='',help='Refuse commands copied from a different panel node')
    parser.add_argument('--archive-handoff-file',default='',help='Windows bootstrap-only file for deferred archive after interpreter exit')
    args=parser.parse_args()
    if args.archive_handoff_file and not args.archive_install:
        raise ValueError('--archive-handoff-file requires --archive-install')
    if args.source and (sys.platform != 'win32' or args.no_service or args.start_service or args.upgrade or args.uninstall or args.status or args.archive_install):
        raise ValueError('--source is a Windows background installation action')
    os.umask(0o077)
    base=Path(args.install_dir).expanduser() if args.install_dir else default_install_base()
    if not base.is_absolute() or any(ord(c)<32 or ord(c)==127 for c in str(base)):
        raise ValueError('Install directory must be absolute and contain no control characters')
    base=validate_install_base(base)
    if args.no_service and (args.upgrade or args.uninstall or args.status or args.start_service or args.archive_install):
        raise ValueError('--no-service is only valid for a new installation')
    if args.yes and not args.uninstall:
        raise ValueError('--yes is only valid with --uninstall')
    if args.archive_install:
        archive_windows_install(base,args)
        return
    if args.status:
        if not (base/'config.json').exists():
            print(json.dumps({'installed':False}));return
        current=read_installed_config(base)
        print(json.dumps({'installed':True,'name':current.get('name',''),'device_id':current['device_id'],
                          'version':runtime_version(base/'runtime'),'install_dir':str(base)},ensure_ascii=False));return
    if args.uninstall and not base.exists():
        print('Agent is not installed; nothing to remove.');return
    if args.expected_device and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', args.expected_device):
        raise ValueError('Invalid expected panel device identity.')
    if args.upgrade or args.uninstall:
        current=read_installed_config(base)
    elif (args.expected_device or args.hub) and not args.source and (base/'config.json').is_file():
        # A panel install ticket is bound to a node. Check locally before
        # touching ACLs, installing dependencies, or redeeming the ticket.
        current=read_installed_config(base)
    else:
        current=None
    if current:
        check_existing_panel_target(current, args.expected_device, args.hub)
    base.mkdir(parents=True,exist_ok=True)
    if os.name == 'nt':
        # Apply ACLs before creating any pairing/config material.
        import csv
        identity=next(csv.reader([subprocess.check_output(['whoami.exe','/user','/fo','csv','/nh'],text=True).strip()]))
        sid=identity[1]
        run(['icacls.exe',base,'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],stdout=subprocess.DEVNULL)
    lock=base/'.install.lock'
    try:lock.mkdir()
    except FileExistsError:raise ValueError('Another installation is active; inspect '+str(lock)+' before retrying.')
    runtime=base/'runtime'; config=base/'config.json'
    python=runtime/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    created=False
    try:
        if current:
            # Recheck after acquiring the install lock; configuration can change
            # between the early preflight and maintenance admission.
            current = read_installed_config(base)
            check_existing_panel_target(current, args.expected_device, args.hub)
        if args.source:
            install_source(base, args.source, args.pairing_file, args.allow, recover_stale_operations=args.recover_stale_operations, shell=args.shell);return
        if args.upgrade:
            local_upgrade(base,args,current);return
        if args.uninstall:
            local_uninstall(base,args,current);return
        if args.start_service:
            if not config.is_file() or not python.is_file():raise ValueError('No installed Agent found')
            start_service(base,python)
            kind, scope = service_identity()
            update_management(base, service=True, service_kind=kind, service_scope=scope,
                              installed_version=runtime_version(runtime), status='ready', last_error='')
            install_cli_links(base)
            print('Agent service started. Check the node online state in the panel.')
            return
        if (current and args.expected_device and args.hub
                and isinstance(current.get('secret'), str) and current['secret']):
            # A command explicitly naming this paired node and Hub is an update.
            # Keep config byte-identical: roots, shell, custom settings and the
            # existing pairing are authoritative. No new install ticket is used.
            if not all((args.archive, args.sha256, args.uv)):
                raise ValueError('Incomplete installation options')
            os.environ.pop('CODEPIER_INSTALL_TOKEN', None)
            os.environ.pop('RELAY_INSTALL_TOKEN', None)
            print('Existing node detected; upgrading with its saved configuration and pairing.')
            local_upgrade(base, args, current)
            return
        if not all((args.archive,args.sha256,args.hub,args.allow,args.uv)):raise ValueError('Incomplete installation options')
        allowed=Path(args.allow).expanduser()
        if not allowed.is_absolute() or not allowed.is_dir():raise ValueError('Allowed directory must already exist and be absolute')
        ticket=os.environ.pop('CODEPIER_INSTALL_TOKEN','') or os.environ.pop('RELAY_INSTALL_TOKEN','')
        if not ticket.startswith('rdi_') or len(ticket)>200:raise ValueError('Invalid installation ticket')
        if config.exists() or runtime.exists():
            if not config.is_file() or not runtime.is_dir() or not (runtime/'agent/__main__.py').is_file():
                raise ValueError('Existing Agent layout is incomplete; no files were changed.')
            current_config=read_installed_config(base)
            resume_windows_service_install(base,current_config)
            definition = service_definition(base)
            if not definition or not definition.is_file():
                raise ValueError('Existing Agent is not a managed service; stop it and repair manually instead of overwriting it.')
            current_before = config.read_bytes()
            current_config = read_installed_config(base)
            verify_service_ownership(base)
            require_idle(base,current_config,recover_stale_operations=getattr(args,"recover_stale_operations",False))
            candidate = base/('.runtime-update-manual-'+hashlib.sha256(os.urandom(16)).hexdigest()[:12])
            candidate.mkdir()
            try:
                unpack(args.archive,args.sha256,candidate)
                candidate_python=candidate/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
                run([args.uv,'venv','--relocatable','--python',sys.executable,candidate/'.venv'])
                run([args.uv,'pip','install','--require-hashes','--python',candidate_python,'-r',candidate/'requirements-agent.txt'])
                run([candidate_python,'-c','import agent.config, agent.runner, agent.lifecycle'],cwd=candidate)
                if config.read_bytes() != current_before:
                    raise ValueError('Configuration changed during preparation; ticket was not used.')
                pairing=enroll(args.hub,ticket,current_config['device_id']);ticket=''
                check_pairing_device(pairing,current_config['device_id'])
                try: current=json.loads(config.read_text(encoding='utf-8'))
                except (OSError,ValueError) as exc: raise ValueError('Existing Agent config is invalid; no runtime was replaced.') from exc
                if not isinstance(current,dict) or current.get('device_id')!=pairing.get('device_id'):
                    raise ValueError('Install ticket belongs to another device; no runtime was replaced. '
                                     'If the existing install failed, use the Windows archive recovery command '
                                     'to preserve it and free the install directory before retrying. '
                                     'Do not delete config.json.')
                if config.read_bytes() != current_before:
                    raise ValueError('Configuration changed during preparation; no runtime was replaced.')
                verify_service_ownership(base)
                require_idle(base,current)
                current.update({k:pairing[k] for k in ('name','secret','hub_url') if k in pairing})
                atomic_json_file(config,current)
                kind,scope=service_identity()
                update_management(base,service=True,service_kind=kind,service_scope=scope,
                                  installed_version=runtime_version(runtime),helper_python=str(Path(sys.executable).resolve()),
                                  uv=str(Path(args.uv).expanduser().resolve()),status='updating',last_error='')
                try:
                    run([external_python(base),candidate/'scripts/agent_lifecycle.py','--install-dir',base,
                         '--wait-pid','0','--apply-update',candidate])
                except Exception:
                    # Restore only the exact configuration written by this invocation.
                    if json.loads(config.read_text(encoding='utf-8')) == current:
                        config.write_bytes(current_before)
                    raise
            finally:
                if candidate.exists():shutil.rmtree(candidate,ignore_errors=True)
            install_cli_links(base)
            if sys.platform == 'win32':
                start_service(base, python)
            print('Existing managed Agent updated: '+str(base))
            return
        if not args.no_service:service_preflight(base)
        runtime.mkdir();created=True
        unpack(args.archive,args.sha256,runtime)
        run([args.uv,'venv','--relocatable','--python',sys.executable,runtime/'.venv'])
        run([args.uv,'pip','install','--require-hashes','--python',python,'-r',runtime/'requirements-agent.txt'])
        run([python,'-c','import agent.config, agent.runner, agent.lifecycle'],cwd=runtime)
        print('Pairing with the panel…',flush=True)
        pairing=enroll(args.hub,ticket,args.expected_device);ticket=''
        check_pairing_device(pairing,args.expected_device)
        with tempfile.TemporaryDirectory(prefix='pairing-',dir=base) as temporary:
            pairing_file=Path(temporary)/'pairing.json'
            pairing_file.write_text(json.dumps(pairing),encoding='utf-8');pairing_file.chmod(0o600)
            run([python,'-m','agent','--config',config,'init','--pairing-file',pairing_file,'--allow',allowed.resolve(),'--shell',args.shell],cwd=runtime)
        kind, scope = service_identity()
        update_management(base, service=False, service_kind=kind if not args.no_service else 'none',
                          service_scope=scope if not args.no_service else 'none',
                          installed_version=runtime_version(runtime), helper_python=str(Path(sys.executable).resolve()),
                          uv=str(Path(args.uv).expanduser().resolve()), installed_at=time.time(),
                          status='service_pending' if not args.no_service else 'ready', last_error='')
        install_cli_links(base)
        if not args.no_service:
            try:
                start_service(base,python)
                update_management(base, service=True, status='ready', last_error='')
            except Exception as exc:
                update_management(base, service=False, status='service_error', last_error=str(exc)[:500])
                print('Agent paired; service setup needs retry. Run:',file=sys.stderr)
                print(display_command([str(python),str(runtime/'scripts/install_agent.py'),'--install-dir',str(base),'--start-service']),file=sys.stderr)
                raise ValueError('Service setup failed; pairing and runtime were preserved.') from exc
        install_cli_links(base)
        print('Agent installed: '+str(base))
        print('Manage: '+str(base/('codepier-agent.ps1' if os.name=='nt' else 'codepier-agent'))+' upgrade | uninstall | status')
        if args.no_service:
            print('Start from '+str(runtime)+': '+display_command([str(python),'-m','agent','--config',str(config),'run']))
        else:print('Autostart configured. Check the node online state in the panel.')
    finally:
        # A failed dependency download or expired ticket must remain retryable.
        if created and not config.exists():shutil.rmtree(runtime)
        try:lock.rmdir()
        except FileNotFoundError:pass


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.SubprocessError,zipfile.BadZipFile) as exc:
        print('Installation failed: '+str(exc),file=sys.stderr)
        sys.exit(1)
