"""Owned, recoverable CodePier migration; standard library only.

Legacy names are compatibility identities, not current branding. This module
never re-enrolls a device or changes its URL, credentials or project permissions.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

PRODUCT = 'CodePier'
AGENT_DIRECTORY = '.codepier-agent'
LEGACY_DIRECTORY = '.remote-dev-agent'
SERVICE_NAMES = {
    'launchd': ('com.codepier.agent', 'com.liangchanghua.remote-dev-agent'),
    'systemd': ('codepier-agent.service', 'remote-dev-agent.service'),
    'schtasks': ('CodePierAgent', 'RemoteDevAgent'),
}
JOURNAL = '.codepier-migration.json'


def read_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError('Expected an object in migration metadata')
    return value


def write_bytes(path, raw, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Refusing to overwrite a symlink during migration')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.codepier-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, mode); os.replace(name, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, value):
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode())


def relocate_path(value, old, new):
    source, target = str(old), str(new)
    if value == source: return target
    separator = '\\' if '\\' in source and '/' not in source else '/'
    if value.startswith(source+separator): return target+value[len(source):]
    return value


def canonical_base(base):
    return base.with_name(AGENT_DIRECTORY) if base.name == LEGACY_DIRECTORY else base


def _link_is_owned(old, new):
    try:
        return (old.is_symlink() or getattr(old, 'is_junction', lambda: False)()) and old.resolve() == new.resolve()
    except OSError:
        return False


def default_base(home=None):
    home = Path.home() if home is None else Path(home)
    current, legacy = home/AGENT_DIRECTORY, home/LEGACY_DIRECTORY
    if current.exists() and legacy.exists() and current.resolve() != legacy.resolve():
        raise RuntimeError('Both CodePier and legacy Agent directories exist; no files were merged')
    if current.is_symlink():
        raise RuntimeError('The CodePier installation directory must not be a symlink')
    if legacy.is_symlink() and not _link_is_owned(legacy, current):
        raise RuntimeError('The legacy installation alias is not owned by CodePier')
    return current if current.exists() or not legacy.exists() else legacy


def systemd_quote(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'


def definition_owned(raw, base, kind, name):
    runtime, config = base/'runtime', base/'config.json'
    command = [str(runtime/'.venv/bin/python'), '-m', 'agent', '--config', str(config), 'run']
    try:
        if kind == 'launchd':
            value = plistlib.loads(raw)
            return (value.get('Label') == name and value.get('WorkingDirectory') == str(runtime)
                    and value.get('ProgramArguments') == command)
        if kind == 'systemd':
            lines = raw.decode().splitlines()
            return ('WorkingDirectory='+str(runtime).replace('%','%%') in lines
                    and 'ExecStart='+' '.join(systemd_quote(x) for x in command) in lines)
        if kind == 'schtasks':
            ns = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            action = ET.fromstring(raw).find('t:Actions/t:Exec', ns)
            return (action is not None and action.findtext('t:WorkingDirectory', namespaces=ns) == str(runtime)
                    and action.findtext('t:Command', namespaces=ns) in
                        [str(runtime/'.venv/Scripts/python.exe'), str(runtime/'.venv/Scripts/pythonw.exe')]
                    and action.findtext('t:Arguments', namespaces=ns) == subprocess.list2cmdline([str(runtime/'run-service.py')]))
    except (ValueError, TypeError, UnicodeError, ET.ParseError, plistlib.InvalidFileException):
        pass
    return False


def service_target(base, kind, scope, name, home=None):
    home = Path.home() if home is None else Path(home)
    if kind == 'launchd': return home/'Library/LaunchAgents'/(name+'.plist')
    if kind == 'systemd':
        return (Path('/etc/systemd/system') if scope == 'system' else home/'.config/systemd/user')/name
    if kind == 'schtasks': return base/'service.xml'
    return None


def service_name(base, kind, scope, home=None):
    names = SERVICE_NAMES.get(kind)
    if not names: raise RuntimeError('Unsupported managed Agent service')
    metadata = read_json(base/'management.json') if (base/'management.json').is_file() else {}
    recorded = metadata.get('service_name')
    if recorded:
        if recorded not in names: raise RuntimeError('Unknown managed service identity')
        return recorded
    if kind == 'schtasks':
        target = base/'service.xml'
        if target.is_file():
            text = target.read_text(encoding='utf-16')
            return names[0] if 'CodePierAgent' in text or AGENT_DIRECTORY in text else names[1]
        return names[0]
    owned = []
    for name in names:
        target = service_target(base, kind, scope, name, home)
        if target.is_symlink(): raise RuntimeError('A managed service definition is a symlink')
        if target.is_file() and definition_owned(target.read_bytes(), base, kind, name): owned.append(name)
    if len(owned)>1: raise RuntimeError('Both old and new services own this installation; inspect the duplicate')
    return owned[0] if owned else names[0]


def new_definition(raw, old, new, kind):
    if kind == 'launchd':
        value = plistlib.loads(raw); value['Label'] = SERVICE_NAMES[kind][0]
        value['ProgramArguments'] = [relocate_path(x, old, new) for x in value['ProgramArguments']]
        for key in ('WorkingDirectory','StandardOutPath','StandardErrorPath'):
            if key in value: value[key] = relocate_path(value[key], old, new)
        return plistlib.dumps(value)
    if kind == 'systemd':
        text = raw.decode().replace(str(old).replace('%','%%'), str(new).replace('%','%%'))
        text = text.replace(systemd_quote(old)[:-1], systemd_quote(new)[:-1])
        return re.sub(r'(?m)^Description=.*$', 'Description=CodePier Agent', text).encode()
    if kind == 'schtasks':
        ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'; ET.register_namespace('', ns)
        tree = ET.fromstring(raw); action = tree.find('{'+ns+'}Actions/{'+ns+'}Exec')
        for key in ('Command','Arguments','WorkingDirectory'):
            item = action.find('{'+ns+'}'+key)
            if item is not None and item.text:
                item.text = (subprocess.list2cmdline([str(new/'runtime/run-service.py')]) if key == 'Arguments'
                             else relocate_path(item.text, old, new))
        info = tree.find('{'+ns+'}RegistrationInfo')
        if info is None: info = ET.SubElement(tree, '{'+ns+'}RegistrationInfo')
        uri = info.find('{'+ns+'}URI')
        if uri is None: uri = ET.SubElement(info, '{'+ns+'}URI')
        uri.text = '\\CodePierAgent'
        return ET.tostring(tree, encoding='utf-16', xml_declaration=True)
    raise RuntimeError('Unsupported service definition')


def _safe_base(base):
    home = Path.home().resolve()
    if (not base.is_absolute() or base.is_symlink() or len(base.parts)<3 or base.resolve() == home
            or base.resolve() in home.parents or (base/'config.json').is_symlink()
            or (base/'runtime').is_symlink() or not (base/'runtime/agent/__main__.py').is_file()):
        raise RuntimeError('Unrecognized or unsafe Agent migration directory')
    if hasattr(os,'getuid') and base.stat().st_uid != os.getuid():
        raise RuntimeError('Run migration as the original Agent owner; do not add sudo')


def _make_alias(old, new):
    if old == new: return
    if old.exists() or old.is_symlink():
        if _link_is_owned(old,new): return
        raise RuntimeError('Legacy compatibility path became occupied')
    if os.name == 'nt':
        import base64
        literal=lambda value:"'"+str(value).replace("'","''")+"'"
        script="$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "+literal(old)+' -Value '+literal(new)+' | Out-Null'
        subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',
                        base64.b64encode(script.encode('utf-16le')).decode()],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
    else: old.symlink_to(new.name, target_is_directory=True)


def _remove_alias(old, new):
    if old == new: return
    if not _link_is_owned(old,new):
        if old.exists() or old.is_symlink(): raise RuntimeError('Unrecognized legacy path; refusing removal')
        return
    if getattr(old,'is_junction',lambda:False)(): old.rmdir()
    else: old.unlink()


def _snapshot(base, journal, path):
    relative = str(path.relative_to(base))
    if relative in journal['files']: return relative
    entry = {'mode':stat.S_IMODE(path.lstat().st_mode), 'written':[]}
    if path.is_symlink(): entry.update(kind='symlink', target=os.readlink(path))
    else:
        raw = path.read_bytes(); key = hashlib.sha256(relative.encode()).hexdigest()
        write_bytes(base/journal['backup']/'files'/key, raw)
        entry.update(kind='file', backup=key, sha256=hashlib.sha256(raw).hexdigest())
    journal['files'][relative] = entry; write_json(base/JOURNAL, journal)
    return relative


def _modify(base, journal, path, raw, mode=None):
    entry = journal['files'][_snapshot(base,journal,path)]
    if entry['kind'] != 'file' or path.is_symlink(): raise RuntimeError('Expected an owned regular file')
    expected = entry['written'][-1] if entry['written'] else entry['sha256']
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError('An owned file changed during migration; local edits were preserved')
    entry['written'].append(hashlib.sha256(raw).hexdigest()); write_json(base/JOURNAL,journal)
    write_bytes(path,raw,entry['mode'] if mode is None else mode)


def _modify_json(base,journal,path,value):
    _modify(base,journal,path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())


def _restore_files(base,journal):
    for relative,entry in journal['files'].items():
        if not entry.get('written'): continue
        if Path(relative).is_absolute() or '..' in Path(relative).parts: raise RuntimeError('Unsafe recovery path')
        path=base/relative
        if entry['kind']=='symlink':
            if not path.is_symlink() or os.readlink(path) not in [entry['target'],*entry['written']]:
                raise RuntimeError('A migrated symlink changed; recovery preserved the owner edit')
            path.unlink(); path.symlink_to(entry['target'])
        else:
            if path.is_symlink() or not path.is_file(): raise RuntimeError('An owned migration file was replaced')
            if hashlib.sha256(path.read_bytes()).hexdigest() not in [entry['sha256'],*entry['written']]:
                raise RuntimeError('A migrated file changed; recovery preserved the owner edit')
            raw=(base/journal['backup']/'files'/entry['backup']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=entry['sha256']: raise RuntimeError('Backup checksum mismatch')
            write_bytes(path,raw,entry['mode'])


def _relocate_owned_files(base,old,new,journal):
    path=base/'config.json'; config=read_json(path); state=config.get('state_dir')
    if isinstance(state,str) and relocate_path(state,old,new)!=state:
        config['state_dir']=relocate_path(state,old,new); _modify_json(base,journal,path,config)
    path=base/'management.json'; metadata=read_json(path)
    for key in ('helper_python','uv','install_dir'):
        if isinstance(metadata.get(key),str): metadata[key]=relocate_path(metadata[key],old,new)
    metadata.update(product=PRODUCT,install_dir=str(new),service_name=SERVICE_NAMES[journal['kind']][0],
                    brand_schema=1,brand_migration='migrating',status='migrating')
    _modify_json(base,journal,path,metadata)
    if old==new: return
    for runtime in (base/'runtime',base/'.runtime-previous'):
        if runtime.is_symlink(): continue
        venv=runtime/'.venv'
        if not venv.is_dir() or venv.is_symlink(): continue
        paths=[venv/'pyvenv.cfg']
        for folder in (venv/'bin',venv/'Scripts'):
            if folder.is_dir() and not folder.is_symlink(): paths.extend(folder.iterdir())
        for path in paths:
            if path.is_symlink():
                before=os.readlink(path); after=relocate_path(before,old,new)
                if before!=after:
                    relative=_snapshot(base,journal,path); journal['files'][relative]['written'].append(after)
                    write_json(base/JOURNAL,journal); path.unlink(); path.symlink_to(after)
                continue
            if not path.is_file() or path.stat().st_size>128*1024: continue
            raw=path.read_bytes()
            if b'\x00' in raw: continue
            try: text=raw.decode()
            except UnicodeError: continue
            if path.name!='pyvenv.cfg' and not (text.startswith('#!') or path.name.lower().startswith('activate')): continue
            changed=text.replace(str(old)+os.sep,str(new)+os.sep)
            staged=re.escape(str(new)+os.sep)+r'\.runtime-update-[A-Za-z0-9_-]+'+re.escape(os.sep+'.venv')
            changed=re.sub(staged,lambda _:str(runtime/'.venv'),changed)
            if changed!=text: _modify(base,journal,path,changed.encode())


def _verify_databases(base,config,backup):
    state=Path(config.get('state_dir') or base/'state').expanduser(); checks=[]
    for relative in ('agent.sqlite3','native-cli/native.sqlite3'):
        database=state/relative
        if not database.is_file(): continue
        if database.is_symlink(): raise RuntimeError('Agent database is a symlink')
        snapshot=backup/'databases'/relative.replace('/','-'); snapshot.parent.mkdir(parents=True,exist_ok=True)
        source=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
        try:
            target=sqlite3.connect(snapshot)
            try:
                source.backup(target); integrity=target.execute('PRAGMA integrity_check').fetchone()[0]
            finally: target.close()
        finally: source.close()
        os.chmod(snapshot,0o600)
        if integrity!='ok': raise RuntimeError('Agent database integrity check failed')
        checks.append({'database':relative,'integrity':integrity})
    return checks


def _register_service(base,journal,backend,old=False):
    name=journal['old_name'] if old else SERVICE_NAMES[journal['kind']][0]
    if journal['kind']=='systemd':
        backend._run(backend._systemctl(journal['scope'])+['daemon-reload'],check=True)
        backend._run(backend._systemctl(journal['scope'])+['enable',name],check=True)
    elif journal['kind']=='schtasks':
        backend._run(['schtasks.exe','/Create','/TN',name,'/XML',base/'service.xml','/F'],check=True)


def _retire_service(journal,backend,old=True):
    name=journal['old_name'] if old else SERVICE_NAMES[journal['kind']][0]
    other=SERVICE_NAMES[journal['kind']][0] if old else journal['old_name']
    if name==other: return
    target=Path(journal['old_target'] if old else journal['new_target'])
    if journal['kind']!='schtasks' and target.exists():
        expected=journal['service_sha256'] if old else journal.get('new_definition_sha256')
        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Service definition changed; refusing to remove it')
    if journal['kind']=='systemd': backend._run(backend._systemctl(journal['scope'])+['disable',name],check=True)
    elif journal['kind']=='schtasks': backend._run(['schtasks.exe','/Delete','/TN',name,'/F'],check=True)
    if journal['kind']!='schtasks':
        target.unlink(missing_ok=True)
        if journal['kind']=='systemd': backend._run(backend._systemctl(journal['scope'])+['daemon-reload'],check=True)


def _rollback(base,journal,backend):
    old,new=Path(journal['old_base']),Path(journal['new_base']); journal['stage']='rolling_back'
    write_json(base/JOURNAL,journal)
    if journal.get('new_registered') or journal.get('stop_requested'): backend.stop_service(base)
    if journal.get('new_definition_written'): _retire_service(journal,backend,old=False)
    browser=getattr(backend,'brand_browser',None)
    if browser:
        browser.remove_created(journal)
        browser.restore_external(base,journal)
    _restore_files(base,journal)
    if old!=new and base==new:
        _remove_alias(old,new)
        if old.exists(): raise RuntimeError('Original Agent path became occupied')
        os.replace(new,old); base=old
    raw=(base/journal['backup']/'service.before').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=journal['service_sha256']: raise RuntimeError('Service backup checksum mismatch')
    target=Path(journal['old_target'])
    if target.exists() and target.read_bytes()!=raw and hashlib.sha256(target.read_bytes()).hexdigest()!=journal.get('new_definition_sha256'):
        raise RuntimeError('Original service changed; recovery preserved the owner edit')
    write_bytes(target,raw,journal['service_mode'])
    metadata=read_json(base/'management.json')
    metadata.update(service_name=journal['old_name'],brand_migration='rollback',status='ready',brand_migration_error=journal.get('error','')[:500])
    write_json(base/'management.json',metadata); _register_service(base,journal,backend,old=True)
    backend.start_service(base); backend.verify_service(base)
    journal.update(stage='rolled_back',finished_at=time.time()); write_json(base/JOURNAL,journal)
    return base


def recover_agent(base,backend):
    for location in dict.fromkeys([base,canonical_base(base)]):
        path=location/JOURNAL
        if not path.is_file(): continue
        journal=read_json(path)
        if journal.get('stage') in {'completed','rolled_back'}: continue
        old,new=Path(journal.get('old_base','')),Path(journal.get('new_base',''))
        if (old!=base and new!=base or canonical_base(old)!=new or location.resolve() not in {old,new}
            or journal.get('kind') not in SERVICE_NAMES or journal.get('old_name') not in SERVICE_NAMES[journal['kind']]):
            raise RuntimeError('Unrecognized interrupted migration; no files were changed')
        if backend._process_alive(int(journal.get('pid',0))): raise RuntimeError('Another migration is still running')
        if read_json(location/'config.json').get('device_id')!=journal.get('device_id'):
            raise RuntimeError('Migration journal belongs to another device')
        journal['error']='An interrupted migration was recovered; retry the update after reviewing this record.'
        restored=_rollback(location.resolve(),journal,backend)
        lock=old.parent/'.codepier-agent-migration.lock'
        owner=read_json(lock/'owner.json') if (lock/'owner.json').is_file() else {}
        if owner.get('old_base')==str(old) and owner.get('pid')==journal.get('pid'):
            (lock/'owner.json').unlink(); lock.rmdir()
        return restored
    return None


def require_idle(base, config):
    state = Path(config.get('state_dir') or base/'state').expanduser()
    for relative, table, active in (
        ('agent.sqlite3', 'calls', ('accepted', 'running')),
        ('native-cli/native.sqlite3', 'sessions', ('starting', 'running', 'stopping', 'orphaned')),
    ):
        database = state/relative
        if not database.exists(): continue
        try:
            db = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
            try:
                count = db.execute('SELECT COUNT(*) FROM '+table+' WHERE status IN ('+','.join('?' for _ in active)+')', active).fetchone()[0]
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise RuntimeError('Cannot verify idle Agent state; migration was not started') from exc
        if count: raise RuntimeError('Agent has active operations or native sessions; wait before migration')


def migrate_agent(base,pid,backend):
    base=Path(base).expanduser()
    if base.is_symlink():
        resolved=base.resolve()
        if base.name!=LEGACY_DIRECTORY or resolved!=base.with_name(AGENT_DIRECTORY): raise RuntimeError('Unrecognized installation alias')
        base=resolved
    recovered=recover_agent(base,backend)
    if recovered is not None: return {'status':'recovered','install_dir':str(recovered),'retry_required':True}
    _safe_base(base); old,new=base,canonical_base(base); kind,scope=backend._service(base)
    if kind not in SERVICE_NAMES: raise RuntimeError('Unrecognized managed service type')
    name=backend._service_name(base); canonical=SERVICE_NAMES[kind][0]; metadata=read_json(base/'management.json')
    if old==new and name==canonical:
        metadata.update(product=PRODUCT,brand_schema=1,brand_migration='completed',install_dir=str(base),service_name=name)
        write_json(base/'management.json',metadata); return {'status':'already_current','install_dir':str(base)}
    if old!=new and (new.exists() or new.is_symlink()): raise RuntimeError('CodePier destination already exists; no files/services were merged')
    target=backend._service_target(base,kind,scope)
    if not target or target.is_symlink() or not target.is_file(): raise RuntimeError('Owned service definition is missing')
    raw=target.read_bytes()
    if not definition_owned(raw,base,kind,name): raise RuntimeError('Service ownership check failed; no service was stopped')
    new_target=service_target(new,kind,scope,canonical)
    if new_target!=target and (new_target.exists() or new_target.is_symlink()): raise RuntimeError('CodePier service name is occupied')
    if kind=='schtasks':
        live=subprocess.check_output(['schtasks.exe','/Query','/TN',name,'/XML'],timeout=30)
        if not definition_owned(live,base,kind,name): raise RuntimeError('Live task belongs to a different installation')
        if canonical!=name and backend._run(['schtasks.exe','/Query','/TN',canonical])==0: raise RuntimeError('CodePier task name is occupied')
    before=(base/'config.json').read_bytes(); config=read_json(base/'config.json')
    if not config.get('device_id') or not config.get('secret'): raise RuntimeError('Agent identity is incomplete')
    require_idle(base, config)
    lock=base.parent/'.codepier-agent-migration.lock'
    try: lock.mkdir(mode=0o700)
    except FileExistsError: raise RuntimeError('Another migration lock exists; inspect/recover it before retrying')
    stamp=time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]; backup=Path('backups')/'codepier-migration'/stamp
    journal={'schema':1,'pid':os.getpid(),'device_id':config['device_id'],'stage':'preflight','old_base':str(old),'new_base':str(new),
             'old_name':name,'kind':kind,'scope':scope,'old_target':str(target),'new_target':str(new_target),'backup':str(backup),
             'files':{},'service_sha256':hashlib.sha256(raw).hexdigest(),'service_mode':stat.S_IMODE(target.stat().st_mode),
             'new_registered':False,'started_at':time.time()}
    stopped=False
    try:
        write_json(lock/'owner.json',{'pid':os.getpid(),'old_base':str(old),'new_base':str(new)})
        write_bytes(base/backup/'service.before',raw); _snapshot(base,journal,base/'config.json'); _snapshot(base,journal,base/'management.json')
        pinned=read_json(base/'management.json'); pinned['service_name']=name; _modify_json(base,journal,base/'management.json',pinned)
        journal['stop_requested']=True; write_json(base/JOURNAL,journal); stopped=True
        backend.stop_service(base); backend._wait_for_exit(pid)
        if (base/'config.json').read_bytes()!=before or target.read_bytes()!=raw:
            raise RuntimeError('Configuration or service changed during migration; owner edits will be preserved')
        journal['databases']=_verify_databases(base,config,base/backup); journal['stage']='stopped'; write_json(base/JOURNAL,journal)
        if old!=new: backend._move(old,new); base=new; _make_alias(old,new)
        journal['stage']='moved'; write_json(base/JOURNAL,journal)
        _relocate_owned_files(base,old,new,journal)
        browser=getattr(backend,'brand_browser',None)
        if browser: browser.migrate(base,old,new,config,journal,globals())
        definition=new_definition(raw,old,new,kind)
        journal.update(new_definition_written=True,new_definition_sha256=hashlib.sha256(definition).hexdigest()); write_json(base/JOURNAL,journal)
        if kind=='schtasks': _modify(base,journal,base/'service.xml',definition,journal['service_mode'])
        else: write_bytes(new_target,definition,journal['service_mode'])
        journal.update(stage='starting',new_registered=True); write_json(base/JOURNAL,journal); _register_service(base,journal,backend)
        backend.start_service(base); backend.verify_service(base); backend.refresh_cli_commands(base)
        _retire_service(journal,backend)
        current=read_json(base/'config.json')
        if any(current.get(k)!=config.get(k) for k in ('device_id','secret','hub_url','allowed_roots','tasks')):
            raise RuntimeError('Identity or authorization changed unexpectedly')
        metadata=read_json(base/'management.json'); metadata.update(product=PRODUCT,brand_schema=1,brand_migration='completed',status='ready',
            legacy_install_dir=str(old) if old!=new else '',install_dir=str(new),service_name=canonical,brand_migration_error='',brand_migrated_at=time.time())
        _modify_json(base,journal,base/'management.json',metadata); journal.update(stage='completed',finished_at=time.time()); write_json(base/JOURNAL,journal)
        return {'status':'completed','install_dir':str(new),'service_name':canonical,'legacy_alias':str(old) if old!=new else None,'backup':str(base/backup)}
    except Exception as exc:
        journal['error']=str(exc)[:500]
        if stopped:
            try: _rollback(base,journal,backend)
            except Exception as recovery:
                journal.update(stage='recovery_required',recovery_error=str(recovery)[:500])
                # Recovery may already have moved the directory back. Never
                # recreate an empty canonical directory while recording a failure.
                for location in dict.fromkeys([Path(journal['old_base']),Path(journal['new_base'])]):
                    try:
                        if not location.is_dir() or location.is_symlink() or not (location/'config.json').is_file(): continue
                        if read_json(location/'config.json').get('device_id')!=journal['device_id']: continue
                        write_json(location/JOURNAL,journal)
                        metadata=read_json(location/'management.json')
                        metadata.update(status='error',brand_migration='recovery_required',brand_migration_error=type(recovery).__name__)
                        write_json(location/'management.json',metadata)
                    except (OSError,ValueError): pass
                raise RuntimeError('CodePier migration failed; recovery needs attention: '+str(recovery)) from exc
        else:
            _restore_files(base,journal); journal.update(stage='rolled_back',finished_at=time.time()); write_json(base/JOURNAL,journal)
        raise
    finally:
        (lock/'owner.json').unlink(missing_ok=True)
        try: lock.rmdir()
        except OSError: pass
