#!/usr/bin/env python3
"""Install an explicitly selected extension/profile native host. Preview by default.

macOS/Linux use an owner-local executable wrapper. Windows requires the native
executable built from codepier_browser_host.py with scripts/build_browser_host.py.
No browser profile, login data, model settings or service is changed or restarted.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from agent.setup_integrations import read_config, write_config
from agent.config import validate_config
from agent.integration_config import validate_integrations
from shared.instance_lock import InstanceLock
from shared.util import atomic_json

NAME='com.codepier.browser'

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def profile_root(browser,platform=sys.platform,home=None):
    home=Path(home or Path.home())
    if platform=='darwin':return home/'Library/Application Support'/({'chrome':'Google/Chrome','chromium':'Chromium','chrome-for-testing':'Google/ChromeForTesting'}[browser])
    if platform=='win32':return Path(os.getenv('LOCALAPPDATA',str(home/'AppData/Local')))/({'chrome':'Google/Chrome','chromium':'Chromium','chrome-for-testing':'Google/ChromeForTesting'}[browser])/'User Data'
    return Path(os.getenv('XDG_CONFIG_HOME',str(home/'.config')))/{'chrome':'google-chrome','chromium':'chromium','chrome-for-testing':'google-chrome-for-testing'}[browser]

def registry_key(browser):return 'Software\\'+{'chrome':'Google\\Chrome','chromium':'Chromium','chrome-for-testing':'Google\\ChromeForTesting'}[browser]+'\\NativeMessagingHosts\\'+NAME

def registry(browser,value=False):
    if os.name!='nt':return None
    import winreg
    key=registry_key(browser)
    if value is False:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key) as handle:return winreg.QueryValueEx(handle,'')[0]
        except FileNotFoundError:return None
    if value is None:
        try:winreg.DeleteKey(winreg.HKEY_CURRENT_USER,key)
        except FileNotFoundError:pass
    else:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,key) as handle:winreg.SetValueEx(handle,'',0,winreg.REG_SZ,value)


def install(config_path,extension_id,profile_id,projects,origins,*,browser='chrome',root=None,native_executable=None,apply=False):
    config_path=Path(config_path).expanduser().absolute();before,value=read_config(config_path)
    checked=validate_config(value,config_path);state=Path(checked['state_dir']);directory=state/'browser-bridge'
    receipt_path=directory/'install-receipt.json'
    if not re.fullmatch('[a-p]{32}',extension_id) or not re.fullmatch('[a-f0-9]{32}',profile_id):raise ValueError('使用扩展窗口显示的完整扩展编号与档案编号')
    if not projects or not origins:raise ValueError('必须明确指定至少一个项目和站点')
    desired=validate_integrations({'browser':{'enabled':True,'projects':projects,'origins':origins,'extension_id':extension_id,'profile_id':profile_id}})['browser']
    root=Path(root).expanduser().absolute() if root else profile_root(browser)
    manifest=(directory if os.name=='nt' else root/'NativeMessagingHosts')/(NAME+'.json')
    descriptor=state/'integration-local.json'
    result={'changed':False,'manifest':str(manifest),'descriptor':str(descriptor),'browser':desired,'service_restarted':False,
            'extension_source':str(ROOT/'web/browser-extension') if (ROOT/'web/browser-extension').is_dir() else None,
            'extension_download':'/static/browser-extension.zip','activation':'在本机加载扩展、授权站点并准备标签页；启用本机监听后正常重启 Agent。'}
    if not apply:return result
    with InstanceLock(config_path.parent/'.integration-setup.lock'):
        if manifest.exists() or manifest.is_symlink() or receipt_path.exists() or registry(browser) is not None:raise ValueError('已有安装或注册，拒绝覆盖；先检查或卸载原安装')
        if directory.exists() and any(directory.iterdir()):raise ValueError('受管目录包含未核实文件，请检查上次安装')
        for parent in {directory,manifest.parent,*directory.parents,*manifest.parent.parents}:
            if parent.is_symlink():raise ValueError('安装目标路径不能包含链接')
        if os.name=='nt' and (not native_executable or not Path(native_executable).is_file() or Path(native_executable).suffix.lower()!='.exe'):
            raise ValueError('Windows 请先用 build_browser_host.py 构建本机 exe，并使用 --native-executable 指定')
        directory.mkdir(parents=True,mode=0o700,exist_ok=True);os.chmod(directory,0o700)
        manifest.parent.mkdir(parents=True,exist_ok=True)
        owned=[];config_written=None
        try:
            if os.name=='nt':
                launcher=directory/'codepier-browser-host.exe';shutil.copyfile(native_executable,launcher);owned.append(launcher)
                settings=directory/'codepier-browser-host.json';atomic_json(settings,{'descriptor':str(descriptor),'extension_id':extension_id});owned.append(settings)
            else:
                # The native host is stdlib-only. Install its own immutable copy:
                # moving/updating a checkout must not strand a browser registration.
                native_script=directory/'codepier_browser_host.py'
                with native_script.open('xb') as file:
                    owned.append(native_script)
                    file.write(Path(__file__).resolve().with_name('codepier_browser_host.py').read_bytes())
                native_script.chmod(0o600)
                launcher=directory/'host.sh'
                interpreter=str(Path(sys.executable).resolve())
                command=shlex.join([interpreter,str(native_script),'--descriptor',str(descriptor),'--extension-id',extension_id])
                with launcher.open('x') as file:
                    owned.append(launcher)
                    file.write('#!/bin/sh\ncd '+shlex.quote(str(directory))+' || exit 1\nexec '+command+' "$@"\n')
                launcher.chmod(0o700)
            atomic_json(manifest,{'name':NAME,'description':'CodePier explicitly authorized browser bridge','path':str(launcher),
                                 'type':'stdio','allowed_origins':['chrome-extension://'+extension_id+'/']});owned.append(manifest)
            # Save a recoverable ownership receipt before changing config/registry.
            receipt={'config':str(config_path),'manifest':str(manifest),'browser':browser,'files':{str(p):sha(p) for p in owned},
                     'browser_before':value.get('integrations',{}).get('browser'),'browser_after':desired,'state':'prepared','at':time.time()}
            atomic_json(receipt_path,receipt)
            updated={**value,'integrations':{**value.get('integrations',{}),'browser':desired}}
            backup=write_config(config_path,before,updated);config_written=config_path.read_bytes()
            registry(browser,str(manifest))
            receipt['state']='installed';atomic_json(receipt_path,receipt)
        except BaseException:
            if config_written is not None and config_path.read_bytes()==config_written:write_config(config_path,config_written,value)
            if registry(browser)==str(manifest):registry(browser,None)
            for path in owned:
                if path.is_file():path.unlink()
            receipt_path.unlink(missing_ok=True)
            raise
    return {**result,'changed':True,'receipt':str(receipt_path),'backup':backup}


def uninstall(config_path,apply=False):
    config_path=Path(config_path).expanduser().absolute();before,value=read_config(config_path)
    state=Path(validate_config(value,config_path)['state_dir']);receipt_path=state/'browser-bridge/install-receipt.json'
    if not receipt_path.exists():return {'changed':False,'state':'not_installed'}
    if receipt_path.is_symlink():raise ValueError('安装回执不能为链接')
    receipt=json.loads(receipt_path.read_text())
    if receipt['config']!=str(config_path):raise ValueError('安装回执属于另一配置')
    for name,digest in receipt['files'].items():
        path=Path(name)
        if path.is_symlink() or not path.is_file() or sha(path)!=digest:raise ValueError('受管文件已被修改，未删除：'+name)
    if value.get('integrations',{}).get('browser')!=receipt['browser_after']:raise ValueError('浏览器设置已修改，拒绝覆盖后续配置')
    if os.name=='nt' and registry(receipt['browser'])!=receipt['manifest']:raise ValueError('宿主注册已修改，拒绝删除')
    if os.name=='nt':
        from shared.brand_browser import _registry_key, _registry_get, OLD_NAME
        for old in receipt.get('legacy_registry', []):
            if old.get('key')!=_registry_key(receipt['browser'],OLD_NAME) or _registry_get(old['key'])!=old.get('value'):
                raise ValueError('旧版宿主注册已改变；没有删除任何注册')
    if not apply:return {'changed':False,'planned':'remove_owned_registration','files':list(receipt['files'])}
    with InstanceLock(config_path.parent/'.integration-setup.lock'):
        updated={**value,'integrations':dict(value.get('integrations',{}))}
        if receipt['browser_before'] is None:updated['integrations'].pop('browser',None)
        else:updated['integrations']['browser']=receipt['browser_before']
        backup=write_config(config_path,before,updated)
        registry(receipt['browser'],None)
        if os.name=='nt':
            from shared.brand_browser import _registry_write
            for old in receipt.get('legacy_registry',[]):_registry_write(old['key'],None)
        for name in receipt['files']:Path(name).unlink()
        receipt_path.unlink()
    return {'changed':True,'state':'uninstalled','backup':backup,'browser_profile_preserved':True,'service_restarted':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    parser.add_argument('--uninstall',action='store_true');parser.add_argument('--apply',action='store_true')
    parser.add_argument('--extension-id');parser.add_argument('--profile-id');parser.add_argument('--project',action='append',default=[]);parser.add_argument('--origin',action='append',default=[])
    parser.add_argument('--browser',choices=['chrome','chromium','chrome-for-testing'],default='chrome');parser.add_argument('--profile-root');parser.add_argument('--native-executable')
    args=parser.parse_args()
    try:
        if args.uninstall:result=uninstall(args.config,args.apply)
        else:result=install(args.config,args.extension_id or '',args.profile_id or '',args.project,args.origin,browser=args.browser,root=args.profile_root,native_executable=args.native_executable,apply=args.apply)
    except (ValueError,OSError,RuntimeError) as exc:print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 1
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
