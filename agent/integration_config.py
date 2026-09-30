"""Validate owner-controlled optional integrations without changing existing grants."""
from __future__ import annotations
import copy,re
from pathlib import Path
from urllib.parse import urlsplit
from shared.util import valid_json_value
from shared.file_sources import DEFAULT_MAX_IMPORT_BYTES, MAX_IMPORT_BYTES, normalize_file_hosts


def origin(value):
    if not isinstance(value,str) or not value or len(value)>2048 or any(c.isspace() or ord(c)<32 or c=='\\' for c in value):
        raise ValueError('站点应为完整 HTTP(S) origin')
    parsed=urlsplit(value)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment:
        raise ValueError('站点白名单只接受精确 origin，不接受通配符、凭据、路径或查询参数')
    port=parsed.port
    if port is not None and not 1<=port<=65535:raise ValueError('站点端口无效')
    host=parsed.hostname.encode('idna').decode('ascii').lower()
    if ':' in host:
        import ipaddress
        ipaddress.IPv6Address(host)
    elif not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host) or '..' in host:
        raise ValueError('站点主机名无效，不接受通配符')
    authority='['+host+']' if ':' in host else host
    if port is not None and port!=({'http':80,'https':443}[parsed.scheme]):authority+=':'+str(port)
    return parsed.scheme+'://'+authority


def strings(value,label,limit=100):
    if not isinstance(value,list) or len(value)>limit or any(not isinstance(v,str) or not v or len(v)>512 or '\x00' in v for v in value):
        raise ValueError(label+' 必须是有界非空字符串数组')
    return value


def validate_integrations(raw):
    if raw is None:raw={}
    if not isinstance(raw,dict) or not valid_json_value(raw):raise ValueError('integrations 必须是 JSON 对象')
    c=copy.deepcopy(raw)
    if set(c)-{'max_import_bytes','file_hosts','extra_file_hosts','worktree_directory','language_servers','browser','local_control'}:
        raise ValueError('integrations 包含未知配置项')
    limit=c.setdefault('max_import_bytes',DEFAULT_MAX_IMPORT_BYTES)
    if type(limit) is not int or not 1<=limit<=MAX_IMPORT_BYTES:raise ValueError('max_import_bytes 必须为 1–512 MiB 内的字节数')
    # Do not persist today's defaults as an explicit owner restriction.
    # Absent lists must continue to follow the versioned registry on upgrades.
    for key in ('file_hosts','extra_file_hosts'):
        if key in c:c[key]=normalize_file_hosts(c[key],key)
    directory=c.setdefault('worktree_directory','')
    if not isinstance(directory,str) or '\x00' in directory or directory and not Path(directory).expanduser().is_absolute():
        raise ValueError('worktree_directory 必须为空或绝对路径')
    servers=c.setdefault('language_servers',{})
    if not isinstance(servers,dict) or len(servers)>32:raise ValueError('language_servers 必须是至多 32 个语言配置对象')
    for language,spec in servers.items():
        if not isinstance(language,str) or not re.fullmatch('[a-z][a-z0-9_-]{0,39}',language) or not isinstance(spec,dict):raise ValueError('语言名称或配置无效')
        if set(spec)-{'command','projects','enabled','env','settings','initialization_options','timeout_seconds'}:raise ValueError('语言服务包含未知配置项')
        command=strings(spec.get('command',[]),'language server command',32)
        if not command:raise ValueError('语言服务 command 不能为空')
        strings(spec.setdefault('projects',[]),'language server projects')
        if type(spec.setdefault('enabled',True)) is not bool:raise ValueError('语言服务 enabled 应为布尔值')
        timeout=spec.setdefault('timeout_seconds',20)
        if type(timeout) is not int or not 1<=timeout<=60:raise ValueError('语言服务 timeout_seconds 必须为 1–60')
        env=spec.setdefault('env',{})
        if not isinstance(env,dict) or len(env)>100 or any(not isinstance(k,str) or not k or '=' in k or '\x00' in k or not isinstance(v,str) or '\x00' in v for k,v in env.items()):raise ValueError('语言服务 env 无效')
        if sum(len(k)+len(v) for k,v in env.items())>65536:raise ValueError('语言服务 env 超出大小限制')
        for key in ('settings','initialization_options'):
            val=spec.setdefault(key,{})
            if not isinstance(val,dict) or len(__import__('json').dumps(val).encode())>65536:raise ValueError(key+' 应为至多 64 KiB 的对象')
    browser=c.setdefault('browser',{})
    if not isinstance(browser,dict) or set(browser)-{'enabled','projects','origins','extension_id','profile_id','lease_seconds','pool_size'}:raise ValueError('browser 配置无效')
    if type(browser.setdefault('enabled',False)) is not bool:raise ValueError('browser.enabled 应为布尔值')
    strings(browser.setdefault('projects',[]),'browser.projects')
    sites=strings(browser.setdefault('origins',[]),'browser.origins',100)
    browser['origins']=sorted(set(origin(s) for s in sites))
    extension=browser.setdefault('extension_id','')
    if not isinstance(extension,str) or extension and not re.fullmatch('[a-p]{32}',extension):raise ValueError('browser.extension_id 应为 Chrome 扩展 ID')
    if browser['enabled'] and not extension:raise ValueError('启用 browser 前必须绑定明确的 extension_id')
    profile=browser.setdefault('profile_id','')
    if not isinstance(profile,str) or profile and not re.fullmatch('[a-f0-9]{32}',profile):raise ValueError('browser.profile_id 应为扩展显示的档案编号')
    if browser['enabled'] and not profile:raise ValueError('启用 browser 前必须绑定明确的 profile_id')
    for key,default,minimum,maximum in [('lease_seconds',900,30,3600),('pool_size',4,1,16)]:
        value=browser.setdefault(key,default)
        if type(value) is not int or not minimum<=value<=maximum:raise ValueError('browser.'+key+' 超出允许范围')
    if type(c.setdefault('local_control',False)) is not bool:raise ValueError('local_control 应为布尔值')
    return c
