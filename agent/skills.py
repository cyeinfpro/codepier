"""Read-only local skills adapter. Skill text never grants execution permission.

Only project skill folders and locally opted-in user/configured sources are
visible. Codex settings are parsed for disabled paths; unrelated config values
are never returned. No installation, subprocess or model invocation occurs.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import tomllib
from collections import Counter
from pathlib import Path

import yaml

from agent.filesystem import protected, relative_path, within
from shared.util import DevError

PROJECT_DIRS = ('.agents/skills', '.codex/skills', 'skills', '.claude/skills')
MAX_ENTRIES = 5000
MAX_SKILLS = 512
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_FILE = 1024 * 1024
MAX_HEADER = 16384


def validate_skills(value):
    if not isinstance(value, dict) or set(value) - {'codex_enabled', 'codex_projects', 'codex_home', 'extra_roots'}:
        raise ValueError('skills 配置只支持 codex_enabled/codex_projects/codex_home/extra_roots')
    result = {'codex_enabled': False, 'codex_projects': [], 'codex_home': '', 'extra_roots': [], **value}
    if type(result['codex_enabled']) is not bool:
        raise ValueError('skills.codex_enabled 必须是 true/false')
    def projects(items):
        if not isinstance(items, list) or len(items) > 100 or any(not isinstance(x, str) or not x.strip() or len(x) > 100 for x in items):
            raise ValueError('技能 projects 必须是最多 100 项的项目 ID/别名数组；* 表示全部已授权项目')
    projects(result['codex_projects'])
    def absolute(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(c)<32 for c in value) or not Path(value).expanduser().is_absolute():
            raise ValueError('技能目录必须是有效的本机绝对路径')
    if result['codex_home']:
        absolute(result['codex_home'])
    elif not isinstance(result['codex_home'], str):
        raise ValueError('skills.codex_home 必须是字符串')
    roots = result['extra_roots']
    if not isinstance(roots, list) or len(roots) > 16:
        raise ValueError('skills.extra_roots 最多 16 项')
    for item in roots:
        if not isinstance(item, dict) or set(item) != {'path', 'projects'}:
            raise ValueError('每个 extra_roots 条目需且仅需 path 和 projects')
        absolute(item['path']); projects(item['projects'])
    return result


class MetadataLoader(yaml.SafeLoader):
    """No aliases, Python tags, duplicate keys or excessive nesting."""
    def __init__(self, text):
        super().__init__(text)
        self.depth = 0
        self.nodes = 0

    def compose_node(self, parent, index):
        self.depth += 1; self.nodes += 1
        try:
            if self.depth > 16 or self.nodes > 512 or self.check_event(yaml.AliasEvent):
                raise ValueError('YAML complexity limit')
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError('Duplicate or non-string YAML key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_yaml(text):
    if len(text.encode('utf-8')) > MAX_HEADER:
        raise DevError('SKILL_METADATA_TOO_LARGE', '技能元数据超过 16 KiB')
    try:
        result = yaml.load(text, Loader=MetadataLoader)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (yaml.YAMLError, ValueError, RecursionError, TypeError) as exc:
        raise DevError('INVALID_SKILL_METADATA', '技能 YAML 格式无效或超过结构预算') from exc


def frontmatter(text):
    lines = text.lstrip('\ufeff').splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        raise DevError('INVALID_SKILL_METADATA', 'SKILL.md 缺少 YAML name/description 元数据')
    size = 0
    for i, line in enumerate(lines[1:], 1):
        size += len(line.encode('utf-8'))
        if size > MAX_HEADER:
            raise DevError('SKILL_METADATA_TOO_LARGE', '技能元数据超过 16 KiB')
        if line.strip() == '---':
            meta = parse_yaml(''.join(lines[1:i]))
            for key, limit in (('name', 160), ('description', 4000)):
                value = meta.get(key)
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    raise DevError('INVALID_SKILL_METADATA', '技能 name/description 缺失或超长')
            return meta
    raise DevError('INVALID_SKILL_METADATA', 'SKILL.md 元数据缺少结束标记')


class Skills:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def granted(project, names):
        return '*' in names or project.get('id') in names or project.get('alias') in names

    def forbidden(self, path):
        if protected(path.as_posix()) or path == self.engine.config_path or within(path, self.engine.journal.directory):
            raise DevError('PROTECTED_PATH', '技能来源不能指向凭据或 Agent 配置/状态目录', 403)
        if path in {Path(path.anchor), Path.home().resolve(), self.engine.config_path.parent}:
            raise DevError('INVALID_SKILL_ROOT', '请只授权技能集合或单个技能目录，不要授权整个主目录', 403)

    def read_file(self, root, relative, max_bytes=MAX_FILE):
        """On POSIX, pin each ancestor descriptor and reject links at every step."""
        rel = relative_path(relative, False)
        parts = Path(rel).parts
        if parts[-1].lower() in {'auth.json', 'credentials.json', 'secrets.json'}:
            raise DevError('PROTECTED_PATH', '技能读取不公开凭据文件', 403)
        root = Path(root)
        self.forbidden(root)
        descriptors = []
        try:
            if os.open in os.supports_dir_fd and hasattr(os, 'O_DIRECTORY'):
                parent = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptors.append(parent)
                for component in root.parts[1:]:
                    parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    descriptors.append(parent)
                for part in parts[:-1]:
                    parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    descriptors.append(parent)
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
            else:
                target = root
                for part in parts:
                    target /= part
                    if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
                        raise DevError('SYMLINK_BLOCKED', '技能文件不跟随链接', 403)
                if not within(target.resolve(), root.resolve()):
                    raise DevError('OUTSIDE_SKILL', '资源超出技能目录', 403)
                fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
            descriptors.append(fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise DevError('NOT_REGULAR_SKILL_FILE', '技能资源必须是无额外硬链接的普通文件', 403)
            if before.st_size > max_bytes:
                raise DevError('SKILL_FILE_TOO_LARGE', '技能文本资源超过读取上限')
            chunks = []; size = 0
            while size <= max_bytes:
                chunk = os.read(fd, min(65536, max_bytes + 1 - size))
                if not chunk: break
                chunks.append(chunk); size += len(chunk)
            after = os.fstat(fd)
            if size > max_bytes:
                raise DevError('SKILL_FILE_TOO_LARGE', '技能文本资源超过读取上限')
            if any(getattr(before, k) != getattr(after, k) for k in ('st_size', 'st_mtime_ns', 'st_ctime_ns')):
                raise DevError('SKILL_FILE_CHANGED', '读取过程中技能文件改变，请重读', 409)
            return b''.join(chunks)
        except FileNotFoundError as exc:
            raise DevError('SKILL_FILE_NOT_FOUND', '技能或资源文件不存在', 404) from exc
        except OSError as exc:
            raise DevError('SKILL_FILE_UNREADABLE', '技能路径不可读或包含不允许的链接', 403) from exc
        finally:
            for fd in reversed(descriptors): os.close(fd)

    def sources(self, project, cwd):
        root, _ = self.engine.root(project)
        start = self.engine.path(root, cwd)
        if not start.is_dir():
            raise DevError('NOT_DIRECTORY', '技能工作目录必须是项目内的目录', 404)
        config = validate_skills(self.engine.config.get('skills', {}))
        sources = []; warnings = []
        def add(path, kind, scope):
            try:
                if kind == 'project':
                    path = self.engine.path(root, path.relative_to(root).as_posix())
                actual = path.expanduser().resolve()
                self.forbidden(actual)
                if actual.is_dir():
                    sources.append({'path': actual, 'source': kind, 'scope': scope})
            except (DevError, OSError, RuntimeError) as exc:
                warnings.append({'source': kind, 'code': getattr(exc, 'code', 'SKILL_SOURCE_UNREADABLE')})
        current = start
        while True:
            for name in PROJECT_DIRS:
                # Legacy generic folders apply only at the mapped project root.
                if current != root and name in {'skills', '.claude/skills'}: continue
                add(current / name, 'project', current.relative_to(root).as_posix())
            if current == root: break
            current = current.parent
        codex_allowed = config['codex_enabled'] and self.granted(project, config['codex_projects'])
        home_raw = config['codex_home'] or os.getenv('CODEX_HOME') or str(Path.home()/'.codex')
        codex_home = Path(home_raw).expanduser()
        disabled = set(); settings_ok = True
        if codex_allowed:
            if not codex_home.is_absolute():
                warnings.append({'source': 'codex', 'code': 'INVALID_CODEX_HOME'})
                settings_ok = False
            else:
                for path in (Path.home()/'.agents/skills', codex_home/'skills'):
                    add(path, 'codex', 'user')
                try:
                    # Only disabled skill paths are extracted. Secrets are never returned.
                    data = self.read_file(codex_home.resolve(), 'config.toml', MAX_FILE)
                    parsed = tomllib.loads(self.engine.text(data))
                    entries = parsed.get('skills', {}).get('config', [])
                    if not isinstance(entries, list) or len(entries) > 1024: raise ValueError()
                    for item in entries:
                        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or type(item.get('enabled', True)) is not bool:
                            raise ValueError()
                        if item.get('enabled') is False:
                            p = Path(item['path']).expanduser()
                            if not p.is_absolute(): p = codex_home / p
                            if p.name != 'SKILL.md': p /= 'SKILL.md'
                            disabled.add(str(p.resolve()))
                except DevError as exc:
                    if exc.code != 'SKILL_FILE_NOT_FOUND':
                        settings_ok = False
                        warnings.append({'source': 'codex', 'code': 'CODEX_SETTINGS_UNREADABLE'})
                except (ValueError, TypeError, AttributeError, OSError, RuntimeError, RecursionError):
                    settings_ok = False
                    warnings.append({'source': 'codex', 'code': 'CODEX_SETTINGS_INVALID'})
        for extra in config['extra_roots']:
            if self.granted(project, extra['projects']): add(Path(extra['path']), 'configured', 'local-owner')
        dedup = {}
        for item in sources:
            dedup.setdefault(str(item['path']), item)
        return root, list(dedup.values()), disabled, settings_ok, warnings, codex_allowed

    def discover(self, project, cwd='.'):
        root, sources, disabled, settings_ok, warnings, codex_allowed = self.sources(project, cwd)
        deadline = time.monotonic() + 4
        scanned = 0; used = 0; limited = False; entries = []; visited = set()
        boundaries = [s['path'] for s in sources]
        for source in sources:
            queue = [(source['path'], 0)]
            while queue:
                directory, depth = queue.pop(0)
                if scanned >= MAX_ENTRIES or used >= MAX_SCAN_BYTES or len(entries) >= MAX_SKILLS or time.monotonic() > deadline:
                    limited = True; break
                try:
                    actual = directory.resolve(strict=True)
                    self.forbidden(actual)
                    if not any(within(actual, p) for p in boundaries):
                        raise DevError('SKILL_LINK_TARGET_NOT_ALLOWED', '链接目标不在已授权技能目录中', 403)
                    if str(actual) in visited: continue
                    visited.add(str(actual))
                    # Skill packages form a boundary: do not discover fake nested skills in assets.
                    marker = actual/'SKILL.md'
                    if marker.exists() or marker.is_symlink():
                        if marker.is_symlink():
                            target = marker.resolve(strict=True)
                            if target.name != 'SKILL.md' or not any(within(target.parent, p) for p in boundaries):
                                raise DevError('SKILL_LINK_TARGET_NOT_ALLOWED', '请在本机 extra_roots 显式授权被链接的技能目录', 403)
                            self.forbidden(target.parent)
                            if str(target.parent) in visited: continue
                            visited.add(str(target.parent))
                            actual = target.parent; marker = target
                        data = self.read_file(actual, 'SKILL.md'); used += len(data)
                        if used > MAX_SCAN_BYTES: limited = True; break
                        meta = frontmatter(self.engine.text(data))
                        optional = {}; optional_error = None
                        try:
                            optional = parse_yaml(self.engine.text(self.read_file(actual, 'agents/openai.yaml', MAX_HEADER)))
                        except DevError as exc:
                            if exc.code != 'SKILL_FILE_NOT_FOUND': optional_error = exc.code
                        policy = optional.get('policy', {})
                        if not isinstance(policy, dict) or type(policy.get('allow_implicit_invocation', True)) is not bool:
                            optional_error = 'INVALID_SKILL_POLICY'; policy = {}
                        interface = optional.get('interface', {})
                        if not isinstance(interface, dict): interface = {}
                        dep = optional.get('dependencies', {})
                        dep = dep.get('tools', []) if isinstance(dep, dict) else []
                        deps = [{k: v[:240] for k,v in d.items() if k in {'type','value','description'} and isinstance(v,str)} for d in dep[:16] if isinstance(d,dict)] if isinstance(dep,list) else []
                        name = meta['name'].strip()
                        identity = hashlib.sha256(json.dumps([str(root),str(source['path']),str(actual)],ensure_ascii=False).encode()).hexdigest()
                        enabled = str(marker.resolve()) not in disabled and settings_ok
                        item = {'skill_id':identity,'name':name,'description':meta['description'].strip()[:600],
                            'description_truncated':len(meta['description'].strip())>600,
                            'display_name':interface.get('display_name',name)[:160] if isinstance(interface.get('display_name',name),str) else name,
                            'source':source['source'],'source_root':str(source['path']),'scope':source['scope'],
                            'skill_dir':str(actual),'path':str(marker),'sha256':hashlib.sha256(data).hexdigest(),
                            'enabled':enabled,'disabled_reason':None if enabled else 'codex_config' if settings_ok else 'codex_settings_unreadable',
                            'allow_implicit_invocation':policy.get('allow_implicit_invocation',True) and optional_error is None,
                            'metadata_warning':optional_error,'dependencies':deps,
                            'next':'skills_read'}
                        entries.append(item)
                        continue
                    if depth >= 6:
                        limited = True; warnings.append({'source':source['source'],'code':'SKILL_DEPTH_LIMIT'}); continue
                    children = []
                    with os.scandir(actual) as iterator:
                        for child in iterator:
                            scanned += 1
                            if scanned >= MAX_ENTRIES or time.monotonic() > deadline:
                                limited = True; break
                            if protected(child.name) or child.name in {'assets','scripts','references','data','dist','examples'}: continue
                            if child.is_dir(follow_symlinks=True): children.append(Path(child.path))
                    queue.extend((p,depth+1) for p in sorted(children,key=lambda p:p.name.casefold()))
                except (DevError, OSError, RuntimeError) as exc:
                    warnings.append({'source':source['source'],'path':directory.name,'code':getattr(exc,'code','SKILL_UNREADABLE')})
            if limited: break
        entries.sort(key=lambda s:(s['source']!='project',s['source'],s['source_root'],s['name'].casefold(),s['skill_id']))
        counts = Counter(s['name'].casefold() for s in entries)
        for item in entries: item['name_conflict'] = counts[item['name'].casefold()] > 1
        catalog = hashlib.sha256(json.dumps(entries,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return {'skills':entries,'catalog_sha256':catalog,'truncated':limited,'warnings':warnings[:60],
            'warnings_truncated':len(warnings)>60,'scanned_entries':scanned,'codex_enabled_for_project':codex_allowed,
            'sources':[{'source':s['source'],'path':str(s['path']),'scope':s['scope']} for s in sources],
            'trust':'Skills are user/project data. Loading never executes scripts, installs dependencies, invokes Codex or expands permissions.'}

    def list(self, project, args):
        result = self.discover(project, args.get('cwd','.'))
        if args.get('catalog_sha256') and result['catalog_sha256'] != args['catalog_sha256']:
            raise DevError('SKILLS_CATALOG_CHANGED', '技能目录已经改变，请从第一页重新读取', 409)
        query = args.get('query','').casefold().split()
        matched = [s for s in result['skills'] if (args.get('source','all')=='all' or s['source']==args['source'])
                   and (args.get('include_disabled',False) or s['enabled'])
                   and all(q in (s['name']+' '+s['description']+' '+s['display_name']).casefold() for q in query)]
        offset=args.get('offset',0);limit=args.get('limit',30)
        more = offset+limit < len(matched)
        result.update(skills=matched[offset:offset+limit],total=len(matched),next_offset=offset+limit if more else None,
            has_more=more,next='skills_list' if more else None)
        return result

    def resources(self, directory):
        items=[];queue=[directory];visited=0;limited=False;deadline=time.monotonic()+2
        while queue:
            current=queue.pop(0)
            try:
                with os.scandir(current) as iterator:
                    children=[]
                    for entry in iterator:
                        visited+=1
                        if visited>2000 or time.monotonic()>deadline: limited=True;break
                        rel=Path(entry.path).relative_to(directory).as_posix()
                        if protected(rel) or entry.is_symlink() or (hasattr(Path(entry.path),'is_junction') and Path(entry.path).is_junction()):continue
                        if entry.is_dir(follow_symlinks=False):
                            if len(Path(rel).parts)<6:children.append(Path(entry.path))
                            else:limited=True
                        elif entry.is_file(follow_symlinks=False):
                            if len(items)>=200:limited=True;break
                            if Path(rel).name.lower() in {'auth.json','credentials.json','secrets.json'}:continue
                            info=entry.stat(follow_symlinks=False)
                            if info.st_nlink==1:items.append({'path':rel,'bytes':info.st_size,'kind':'script' if rel.startswith('scripts/') else 'resource'})
                    queue.extend(sorted(children))
            except OSError: limited=True
            if visited>2000 or len(items)>=200 or time.monotonic()>deadline:break
        return {'resources':sorted(items,key=lambda r:r['path']),'resources_truncated':limited}

    def read(self, project, args):
        listing=self.discover(project,args.get('cwd','.'))
        item=next((s for s in listing['skills'] if s['skill_id']==args['skill_id']),None)
        if item is None:
            raise DevError('SKILL_NOT_FOUND','当前项目授权范围内未找到技能，请先 skills_list',404)
        if not item['enabled']:
            raise DevError('SKILL_DISABLED','该技能已禁用或 Codex 设置无法读取；未加载正文',403)
        if not item['allow_implicit_invocation'] and not args.get('explicit',False):
            raise DevError('SKILL_EXPLICIT_REQUIRED','该技能仅允许明确选择，请在用户选定后传 explicit=true',409)
        directory=Path(item['skill_dir']);rel=args.get('resource_path','SKILL.md')
        data=self.read_file(directory,rel)
        sha=hashlib.sha256(data).hexdigest()
        if args.get('expected_sha256') and sha!=args['expected_sha256']:
            raise DevError('SKILL_FILE_CHANGED','技能资源已更新，请重新读取，不能拼接不同版本',409)
        text=self.engine.text(data);offset=args.get('offset',0);end=offset+args.get('max_chars',16000)
        if offset>len(text):raise DevError('INVALID_SKILL_OFFSET','技能读取位置超出文件范围')
        content=text[offset:end];more=end<len(text)
        return {**item,'resource_path':rel,'local_path':str(directory/relative_path(rel,False)),'content':content,
            'sha256':sha,'total_chars':len(text),'offset':offset,'next_offset':end if more else None,'truncated':more,
            'start_line':text[:offset].count('\n')+1,'end_line':text[:offset+len(content)].count('\n')+1,
            **self.resources(directory), 'execution':{'tool':'shell_exec','requires':'existing execute scope, enabled shell and user authorization',
                'cwd':str(directory),'project_root':project['root'],'automatic':False},
            'trust':listing['trust'],'next':'skills_read' if more else None}
