"""Receipt-owned native-host migration, including explicitly external state.

Old host names remain compatibility aliases. No profile scan, new site grant,
browser restart or arbitrary registry change is performed.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
OLD_NAME='me.infpro.relay.browser'
NEW_NAME='com.codepier.browser'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic(path,raw,mode=0o600):
    if path.is_symlink():raise RuntimeError('Owned browser file is a symlink')
    fd,name=tempfile.mkstemp(prefix='.codepier-browser-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,mode);os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


def _create(base,journal,path,raw,api,mode=0o600):
    if path.exists() or path.is_symlink():raise RuntimeError('A CodePier browser file already exists; no merge attempted')
    path.parent.mkdir(parents=True,exist_ok=True)
    journal.setdefault('created_files',{})[str(path)]=hashlib.sha256(raw).hexdigest();api['write_json'](base/api['JOURNAL'],journal)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())


def _modify(base,journal,path,raw,api):
    if path.is_relative_to(base):return api['_modify'](base,journal,path,raw)
    if path.is_symlink() or not path.is_file():raise RuntimeError('Owned external browser file is not regular')
    entries=journal.setdefault('external_files',{});name=str(path)
    if name not in entries:
        key=hashlib.sha256(name.encode()).hexdigest();before=path.read_bytes()
        api['write_bytes'](base/journal['backup']/'external'/key,before)
        entries[name]={'backup':key,'sha256':hashlib.sha256(before).hexdigest(),'mode':path.stat().st_mode&0o777,'written':[]}
    entry=entries[name];expected=entry['written'][-1] if entry['written'] else entry['sha256']
    if sha(path)!=expected:raise RuntimeError('Owned browser file changed during migration; local edit preserved')
    entry['written'].append(hashlib.sha256(raw).hexdigest());api['write_json'](base/api['JOURNAL'],journal)
    api['write_bytes'](path,raw,entry['mode'])


def restore_external(base,journal):
    for name,entry in journal.get('external_files',{}).items():
        path=Path(name)
        if not path.is_absolute() or '..' in path.parts or path.is_symlink() or not path.is_file():raise RuntimeError('External browser recovery path is unsafe')
        if sha(path) not in [entry['sha256'],*entry['written']]:raise RuntimeError('External browser file changed; recovery preserved it')
        backup=base/journal['backup']/'external'/entry['backup']
        if sha(backup)!=entry['sha256']:raise RuntimeError('External browser backup checksum mismatch')
        _atomic(path,backup.read_bytes(),entry['mode'])


def _registry_key(browser,name):
    if name not in {OLD_NAME,NEW_NAME}:raise RuntimeError('Unknown browser host identity')
    prefix={'chrome':'Google\\Chrome','chromium':'Chromium','chrome-for-testing':'Google\\ChromeForTesting'}.get(browser)
    if not prefix:raise RuntimeError('Unknown browser registry identity')
    return 'Software\\'+prefix+'\\NativeMessagingHosts\\'+name


def _registry_get(key):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key) as handle:return winreg.QueryValueEx(handle,'')[0]
    except FileNotFoundError:return None


def _registry_write(key,value):
    import winreg
    if value is None:
        try:winreg.DeleteKey(winreg.HKEY_CURRENT_USER,key)
        except FileNotFoundError:pass
    else:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,key) as handle:winreg.SetValueEx(handle,'',0,winreg.REG_SZ,value)


def remove_created(journal):
    for name,expected in journal.get('created_files',{}).items():
        path=Path(name)
        if not path.exists() and not path.is_symlink():continue
        if path.is_symlink() or not path.is_file() or sha(path)!=expected:raise RuntimeError('A newly registered browser file changed; recovery preserved it')
        path.unlink()
    if os.name=='nt':
        for entry in reversed(journal.get('browser_registry_updates',[])):
            current=_registry_get(entry['key'])
            if current==entry['before']:continue
            if current!=entry['after']:raise RuntimeError('Browser registry changed; recovery preserved it')
            _registry_write(entry['key'],entry['before'])


def migrate(base,old,new,config,journal,api):
    relocate,read=api['relocate_path'],api['read_json']
    # Match Agent path normalization before relocating an owned installation.
    # Expanding after relocation would leave ~/.../relay-agent pointing at the
    # retired directory and silently skip its receipt.
    configured_state=Path(config.get('state_dir') or old/'state').expanduser().resolve()
    state=Path(relocate(str(configured_state),old,new));receipt_path=state/'browser-bridge/install-receipt.json'
    if receipt_path.is_symlink():raise RuntimeError('Browser receipt is a symlink')
    if not receipt_path.is_file():return
    receipt=read(receipt_path)
    if receipt.get('config') not in {str(old/'config.json'),str(new/'config.json')}:raise RuntimeError('Browser receipt belongs to another Agent')
    files=receipt.get('files')
    if not isinstance(files,dict):raise RuntimeError('Invalid browser ownership receipt')
    actual={}
    for name,expected in files.items():
        path=Path(relocate(name,old,new))
        if path.is_symlink() or not path.is_file() or sha(path)!=expected:raise RuntimeError('An owned browser file changed; migration preserved it')
        actual[name]=path
    prior_manifest=receipt.get('manifest');manifest=actual.get(prior_manifest)
    if not manifest:raise RuntimeError('Browser manifest is not owned by receipt')
    registration=read(manifest);prior_name=registration.get('name')
    if prior_name not in {OLD_NAME,NEW_NAME}:raise RuntimeError('Unknown browser host name')
    launcher=Path(relocate(registration.get('path',''),old,new))
    if launcher not in actual.values():raise RuntimeError('Browser launcher is not an owned file')
    new_manifest=manifest.with_name(NEW_NAME+'.json');registry_change=None
    if os.name=='nt':
        prior_key=_registry_key(receipt['browser'],prior_name);prior_value=_registry_get(prior_key)
        if prior_value not in {prior_manifest,str(manifest)}:raise RuntimeError('Existing browser registration no longer belongs to this receipt')
        canonical_key=_registry_key(receipt['browser'],NEW_NAME);canonical_value=_registry_get(canonical_key)
        if prior_name!=NEW_NAME and canonical_value is not None:raise RuntimeError('CodePier native-host registry is occupied')
        registry_change={'key':canonical_key,'before':canonical_value,'after':str(new_manifest)}
        if prior_name!=NEW_NAME:receipt['legacy_registry']=[{'key':prior_key,'value':prior_value}]
    if launcher.suffix=='.sh':
        text=launcher.read_text();changed=text.replace(str(old)+os.sep,str(new)+os.sep).replace('agent/relay_browser_host.py','agent/codepier_browser_host.py')
        if text!=changed:_modify(base,journal,launcher,changed.encode(),api)
    elif launcher.name=='relay-browser-host.exe':
        canonical=launcher.with_name('codepier-browser-host.exe');_create(base,journal,canonical,launcher.read_bytes(),api,0o700);launcher=canonical
    for path in actual.values():
        if path.name in {'relay-browser-host.json','codepier-browser-host.json'}:
            settings=read(path)
            if isinstance(settings.get('descriptor'),str):
                settings['descriptor']=relocate(settings['descriptor'],old,new)
                _modify(base,journal,path,(json.dumps(settings,indent=2)+'\n').encode(),api)
            canonical=path.with_name('codepier-browser-host.json')
            if canonical!=path:_create(base,journal,canonical,path.read_bytes(),api)
    registration.update(name=NEW_NAME,description='CodePier explicitly authorized browser bridge',path=str(launcher))
    raw=(json.dumps(registration,indent=2)+'\n').encode()
    if new_manifest==manifest:_modify(base,journal,manifest,raw,api)
    else:_create(base,journal,new_manifest,raw,api)
    updated={str(path):sha(path) for path in actual.values()};updated.update(journal.get('created_files',{}))
    receipt.update(config=str(new/'config.json'),manifest=str(new_manifest),files=updated,product='CodePier')
    if new_manifest!=manifest:receipt['legacy_manifests']=[str(manifest)]
    _modify(base,journal,receipt_path,(json.dumps(receipt,indent=2)+'\n').encode(),api)
    if registry_change and registry_change['before']!=registry_change['after']:
        journal.setdefault('browser_registry_updates',[]).append(registry_change);api['write_json'](base/api['JOURNAL'],journal)
        _registry_write(registry_change['key'],registry_change['after'])
    journal['browser_registration']='canonical-with-owned-legacy-alias'
