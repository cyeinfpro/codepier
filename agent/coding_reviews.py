"""Private, bounded source snapshots and immutable interval reviews.

Works without Git. A snapshot is sequential, not an OS transaction; shared
workspace changes cannot be attributed exclusively to a particular agent.
"""
from __future__ import annotations
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import PurePosixPath
import re
import stat
import time
import uuid
from agent.context import project_context
from agent.filesystem import MAX_FILE, CHECKPOINT_ARTIFACTS
from shared.util import DevError, atomic_json, fsync_directory

MAX_FILES = 2000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_STORAGE_BYTES = 256 * 1024 * 1024
MAX_RECORDS = 256
TTL = 7 * 24 * 3600
EXCLUSIONS = 'Protected paths, links, dependencies, .venv-* / venv-* environments, dist/build, caches and docs/evidence are excluded.'
SCOPE = '共享目录在两个采样时点之间的变化；不证明本会话独占修改。快照按文件读取，不是原子文件系统快照。'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def binding(engine, project):
    root, _ = engine.root(project)
    owner = project.get('_coding_owner')
    if not isinstance(owner, str) or not owner:
        raise DevError('REVIEW_OWNER_REQUIRED', '审阅快照必须绑定调用方', 403)
    return {'project_id': project.get('id'), 'root': str(root),
            'device': project.get('_coding_device', project.get('device_id', '')), 'owner': owner}


def excluded(path):
    parts = PurePosixPath(path).parts
    return any(p in CHECKPOINT_ARTIFACTS | {'dist', 'build', '.venv-compat'} or p.startswith(('.venv-', 'venv-')) for p in parts) or path == 'docs/evidence' or path.startswith('docs/evidence/')


class ReviewStore:
    def __init__(self, engine):
        self.engine = engine
        self.directory = engine.journal.directory / 'coding-reviews'
        if self.directory.is_symlink():
            raise DevError('REVIEW_STORAGE_INVALID', '快照存储路径不安全', 403)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    @contextmanager
    def lock(self):
        # Shared by native workers and the Agent; limits are enforced across processes.
        fd = os.open(self.directory / '.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise DevError('REVIEW_STORAGE_INVALID', '无效存储锁', 403)
            if os.name == 'nt':
                import msvcrt
                if not os.fstat(fd).st_size:
                    os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def save(self, document):
        ref = uuid.uuid4().hex
        document = {**document, 'id': ref, 'created': time.time(), 'expires_at': time.time()+TTL}
        wrapped = {'sha256': fingerprint(document), 'payload': document}
        size = len(json.dumps(wrapped, ensure_ascii=False, indent=2).encode())
        if size > MAX_DOCUMENT_BYTES:
            raise DevError('REVIEW_TOO_LARGE', '快照序列化超过 32 MiB；没有发布不完整快照', 413)
        with self.lock():
            used, count = 0, 0
            for path in self.directory.glob('*.json'):
                if path.is_symlink():
                    raise DevError('REVIEW_STORAGE_INVALID', '快照存储存在符号链接', 403)
                st = path.stat()
                if time.time()-st.st_mtime > TTL:
                    path.unlink()
                else:
                    used += st.st_size
                    count += 1
            if count >= MAX_RECORDS or used+size > MAX_STORAGE_BYTES:
                raise DevError('REVIEW_QUOTA', '审阅快照达到 256 条或 256 MiB 配额；旧快照保留七天，不自动删除未过期结果', 409)
            atomic_json(self.directory / (ref+'.json'), wrapped)
        return document

    def purge_owner(self, project):
        expected = binding(self.engine, project)
        removed = 0
        with self.lock():
            for path in self.directory.glob('*.json'):
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                with os.fdopen(fd, 'rb') as source:
                    st = os.fstat(source.fileno())
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_DOCUMENT_BYTES:
                        raise DevError('REVIEW_CLEANUP_UNCONFIRMED', '快照缓存异常，清理未完成；请在节点核查', 409)
                    wrapped = json.loads(source.read(MAX_DOCUMENT_BYTES+1))
                document = wrapped.get('payload', {})
                if wrapped.get('sha256') != fingerprint(document):
                    raise DevError('REVIEW_CLEANUP_UNCONFIRMED', '快照缓存校验失败，清理未完成；请在节点核查', 409)
                if document.get('binding') == expected:
                    path.unlink()
                    removed += 1
            fsync_directory(self.directory)
        return removed

    def release_baseline(self, ref, project):
        self.load(ref, project, 'baseline')
        with self.lock():
            (self.directory / (ref+'.json')).unlink(missing_ok=True)

    def load(self, ref, project, kind):
        expected_binding = binding(self.engine, project)  # Reauthorize on EVERY read.
        if not isinstance(ref, str) or not re.fullmatch('[a-f0-9]{32}', ref):
            raise DevError('REVIEW_INVALID_REF', '快照编号无效')
        try:
            path = self.directory / (ref+'.json')
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as source:
                st = os.fstat(source.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_DOCUMENT_BYTES:
                    raise DevError('REVIEW_STORAGE_INVALID', '快照文件无效', 409)
                raw = source.read(MAX_DOCUMENT_BYTES+1)
            wrapped = json.loads(raw)
            document = wrapped['payload']
            if wrapped['sha256'] != fingerprint(document) or document['id'] != ref:
                raise DevError('REVIEW_INTEGRITY', '快照校验失败，未返回内容', 409)
        except FileNotFoundError as exc:
            raise DevError('REVIEW_NOT_FOUND', '快照不存在或已过期；不会用当前目录差异替代历史结果', 404) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise DevError('REVIEW_INTEGRITY', '快照格式损坏', 409) from exc
        if document.get('binding') != expected_binding or document.get('kind') != kind:
            raise DevError('REVIEW_NOT_FOUND', '快照不属于当前项目映射和授权', 404)
        if document['expires_at'] <= time.time():
            raise DevError('REVIEW_EXPIRED', '快照已过期；不会用当前目录差异替代', 410)
        return document


def capture(engine, project):
    bound = binding(engine, project)
    root, _ = engine.root(project)
    started = time.time()
    files, skipped = {}, []
    size, incomplete = 0, False
    with engine.mutation_lock:
        try:
            for path, relative, st in engine.walk(root, root, exclude=excluded, deadline=time.monotonic()+2):
                if not stat.S_ISREG(st.st_mode):
                    continue
                if len(files)+len(skipped) >= MAX_FILES or size+min(st.st_size, MAX_FILE) > MAX_SOURCE_BYTES:
                    incomplete = True
                    break
                try:
                    data = engine.read_bytes(engine.path(root, relative, False))
                    if size+len(data) > MAX_SOURCE_BYTES:
                        incomplete = True
                        break
                    size += len(data)
                    try:
                        text = engine.text(data)
                    except DevError as exc:
                        if exc.code not in {'BINARY_FILE', 'ENCODING'}:
                            raise
                        text = None
                    files[relative] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                                       'mode': stat.S_IMODE(st.st_mode), 'text': text}
                except (DevError, OSError) as exc:
                    skipped.append({'path': relative, 'code': getattr(exc, 'code', 'READ_FAILED')})
        except (DevError, OSError) as exc:
            incomplete = True
            skipped.append({'path': '.', 'code': getattr(exc, 'code', 'SCAN_FAILED')})
    return {'kind': 'baseline', 'binding': bound, 'started': started, 'finished': time.time(), 'files': files,
            'coverage': {'complete': not incomplete and not skipped, 'captured_files': len(files),
                         'captured_bytes': size, 'skipped': skipped, 'truncated': incomplete,
                         'max_files': MAX_FILES, 'max_bytes': MAX_SOURCE_BYTES, 'exclusions': EXCLUSIONS}}


def public_summary(review):
    return {k: review[k] for k in ('baseline_ref', 'started', 'finished', 'expires_at', 'summary', 'coverage', 'scope')} | {'review_ref': review['id'], 'immutable': True}


def freeze_review(engine, project, baseline_ref):
    store = ReviewStore(engine)
    baseline = store.load(baseline_ref, project, 'baseline')
    after = capture(engine, project)
    files, added, removed, approximate = [], 0, 0, False
    for path in sorted(baseline['files'].keys() | after['files'].keys()):
        before, current = baseline['files'].get(path), after['files'].get(path)
        if before and current and (before['sha256'], before['mode']) == (current['sha256'], current['mode']):
            continue
        unknown = (before is None and not baseline['coverage']['complete'] or current is None and not after['coverage']['complete'])
        binary = any(v is not None and v['text'] is None for v in (before, current))
        kind = 'unverified' if unknown else 'added' if before is None else 'deleted' if current is None else 'modified'
        item = {'path': path, 'status': kind, 'before_sha256': before['sha256'] if before else 'new',
                'sha256': current['sha256'] if current else 'new', 'before': before, 'after': current,
                'text_diff_available': not binary and not unknown, 'added_lines': 0, 'removed_lines': 0}
        if item['text_diff_available']:
            diff = engine.diff(path, before['text'].encode() if before else None, current['text'].encode() if current else None)
            item.update({k: diff.get(k, False) for k in ('added_lines', 'removed_lines', 'diff_truncated', 'line_counts_approximate')})
            added += item['added_lines']
            removed += item['removed_lines']
            approximate |= item['line_counts_approximate']
        files.append(item)
    document = store.save({'kind': 'review', 'binding': baseline['binding'], 'baseline_ref': baseline_ref,
                           'started': baseline['started'], 'finished': after['finished'], 'files': files,
                           'summary': {'files': len(files), 'added_lines': added, 'removed_lines': removed,
                                       'line_counts_approximate': approximate,
                                       'unverified_files': sum(x['status'] == 'unverified' for x in files)},
                           'coverage': {'before': baseline['coverage'], 'after': after['coverage'],
                                        'complete': baseline['coverage']['complete'] and after['coverage']['complete']}, 'scope': SCOPE})
    return public_summary(document)


def read_review(engine, project, args):
    review = ReviewStore(engine).load(args['review_ref'], project, 'review')
    result = public_summary(review)
    offset = args.get('offset', 0)
    path = args.get('path', '')
    if path:
        root, _ = engine.root(project)
        path = engine.path(root, path, False).relative_to(root).as_posix()
        item = next((x for x in review['files'] if x['path'] == path), None)
        if not item:
            raise DevError('REVIEW_FILE_NOT_FOUND', '此文件不在该固定快照中', 404)
        if not item['text_diff_available']:
            text = '二进制或未完整捕获的文件变化，没有可验证的文本差异。'
            details = {'diff_truncated': True}
        else:
            before, after = item['before'], item['after']
            details = engine.diff(path, before['text'].encode() if before else None, after['text'].encode() if after else None)
            text = details.pop('diff')
        end = min(len(text), offset+args.get('max_chars', 16000))
        result.update(path=path, diff=text[offset:end], offset=offset,
                      next_offset=end if end < len(text) else None, **details)
        return result
    end = min(len(review['files']), offset+args.get('limit', 40))
    result.update(files=[{k:v for k,v in item.items() if k not in {'before', 'after'}} for item in review['files'][offset:end]],
                  next_offset=end if end < len(review['files']) else None)
    return result


def open_workspace(engine, project, args):
    context = project_context(engine, project, args)
    manifest = {'binding': binding(engine, project),
                'documents': [{k: d[k] for k in ('path', 'sha256')} for d in context['documents']],
                'skills': context['skills'], 'codex_skills': context['codex_skills'],
                'skills_catalog': context['skills_catalog'], 'root_entries': context['root_entries'],
                'remaining_documents': context['remaining_documents'], 'warnings': context['warnings'],
                'tasks': context['tasks'], 'execution': context['execution'],
                'limits': {k: args[k] for k in ('max_files', 'max_chars', 'include_skills')},
                'permissions': {'mode': project['mode'], 'allow_tasks': project.get('allow_tasks'), 'granted_scopes': project.get('_coding_scopes'),
                                'local_roots': engine.config.get('allowed_roots', [])}}
    context_id = fingerprint(manifest)
    unchanged = context_id == args.get('context_id')
    result = {'context_id': context_id, 'context_unchanged': unchanged,
              'workspace': {'project': project.get('alias'), 'project_id': project.get('id'),
                            'root': context['project_root'], 'mode': project['mode'], 'allow_tasks': project.get('allow_tasks'), 'granted_scopes': project.get('_coding_scopes')},
              'context': None if unchanged else context, 'truncated': context['truncated'],
              'context_scope': 'Bounded bootstrap only. Revalidated on every open; context_id is not an access token.',
              'next': 'fs_read / skills_read on demand; capture baseline BEFORE edits; show_changes to freeze results.'}
    if args.get('capture_baseline'):
        baseline = ReviewStore(engine).save(capture(engine, project))
        result.update(baseline_ref=baseline['id'], baseline_coverage=baseline['coverage'], baseline_expires_at=baseline['expires_at'])
    return result


def show_changes(engine, project, args):
    if args.get('baseline_ref'):
        summary = freeze_review(engine, project, args['baseline_ref'])
        args = {**args, 'review_ref': summary['review_ref'], 'baseline_ref': ''}
    return read_review(engine, project, args)
