"""Private, bounded source snapshots and immutable interval reviews.

Works without Git. A snapshot is sequential, not an OS transaction; shared
workspace changes cannot be attributed exclusively to a particular agent.
"""
from __future__ import annotations
import asyncio
import errno
import hashlib
import json
import logging
import math
import os
from contextlib import contextmanager
from functools import lru_cache
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
BASELINE_TTL = 24 * 3600
CLEANUP_INTERVAL = 3600
EXCLUSIONS = 'Protected paths, links, dependencies, .venv-* / venv-* environments, dist/build, caches, .work, .codepier-test-runs and docs/evidence are excluded. Direct files precede subdirectories within each directory.'
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


def review_excluded(path):
    # Review capture has a smaller budget than validation/source fingerprints.
    # Internal scratch histories must not crowd actual project source out of it.
    return excluded(path) or bool({'.work', '.codepier-test-runs'} & set(PurePosixPath(path).parts))


class ReviewStore:
    def __init__(self, engine):
        self.engine = engine
        self.directory = engine.journal.directory / 'coding-reviews'
        if self.directory.is_symlink():
            raise DevError('REVIEW_STORAGE_INVALID', '快照存储路径不安全', 403)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    @contextmanager
    def lock(self, *, blocking=True):
        # Shared by native workers and the Agent; limits are enforced across processes.
        fd = os.open(self.directory / '.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise DevError('REVIEW_STORAGE_INVALID', '无效存储锁', 403)
            try:
                if os.name == 'nt':
                    import msvcrt
                    if not os.fstat(fd).st_size:
                        os.write(fd, b'0')
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except OSError as exc:
                if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    yield False
                    return
                raise
            yield True
        finally:
            os.close(fd)

    @staticmethod
    def _read(path):
        """Validate before using metadata for reclamation or quota accounting."""
        if path.is_symlink():
            raise DevError('REVIEW_STORAGE_INVALID', '快照存储存在符号链接', 403)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as source:
            st = os.fstat(source.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_DOCUMENT_BYTES:
                raise DevError('REVIEW_STORAGE_INVALID', '快照文件无效', 409)
            raw = source.read(MAX_DOCUMENT_BYTES+1)
        try:
            wrapped = json.loads(raw)
            document = wrapped['payload']
            expires = document['expires_at']
            if (wrapped['sha256'] != fingerprint(document) or document['id'] != path.stem
                    or type(expires) not in (int, float) or not math.isfinite(expires)):
                raise ValueError('Invalid snapshot metadata')
        except (ValueError, KeyError, TypeError) as exc:
            raise DevError('REVIEW_INTEGRITY', '快照格式或校验损坏', 409) from exc
        return document, st.st_size

    @staticmethod
    @lru_cache(maxsize=512)
    def _metadata(path, signature):
        # Cache only metadata, never source text. Atomic writes and even in-place
        # edits invalidate via inode/ctime/mtime/size; permissions and links count too.
        document, size = ReviewStore._read(path)
        return {'expires_at': document['expires_at'], 'review_key': ReviewStore._review_key(document)}, size

    def _records(self, *, now=None):
        # Caller holds the cross-process lock. Never infer expiry from mtime:
        # copying/restoring a file must not extend or shorten its published lease.
        now = time.time() if now is None else now
        removed = False
        try:
            for path in self.directory.glob('*.json'):
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise DevError('REVIEW_STORAGE_INVALID', '快照文件无效', 409)
                signature = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_nlink)
                # Windows ctime is creation time, not a reliable in-place-change
                # indicator. Revalidate there instead of trusting this cache.
                metadata = self._metadata.__wrapped__ if os.name == 'nt' else self._metadata
                document, size = metadata(path, signature)
                if document['expires_at'] <= now:
                    path.unlink()
                    removed = True
                else:
                    yield path, document, size
        finally:
            if removed:
                fsync_directory(self.directory)

    def cleanup(self):
        # Maintenance must not leave a thread waiting behind a native worker
        # while the Agent is trying to stop or replace its runtime.
        with self.lock(blocking=False) as acquired:
            if not acquired:
                return
            for _ in self._records():
                pass

    @staticmethod
    def _review_key(document):
        if document.get('kind') != 'review' or not document.get('coverage', {}).get('complete'):
            return None
        # Reuse the original immutable interval, including its original expiry.
        return fingerprint({k: document[k] for k in
                            ('binding', 'baseline_ref', 'started', 'files', 'summary', 'coverage', 'scope')})

    def save(self, document, *, finalize_project=None):
        finalizing = finalize_project is not None
        ref = document['baseline_ref'] if finalizing else uuid.uuid4().hex
        now = time.time()
        ttl = min(TTL, BASELINE_TTL) if document['kind'] == 'baseline' else TTL
        document = {**document, 'id': ref, 'created': now, 'expires_at': now+ttl}
        if finalizing:
            document['baseline_released'] = True
        wrapped = {'sha256': fingerprint(document), 'payload': document}
        size = len(json.dumps(wrapped, ensure_ascii=False, indent=2).encode())
        if size > MAX_DOCUMENT_BYTES:
            raise DevError('REVIEW_TOO_LARGE', '快照序列化超过 32 MiB；没有发布不完整快照', 413)
        with self.lock():
            sweep_time = time.time()
            replaced_size = 0
            if finalizing:
                previous = self.load(ref, finalize_project, ('baseline', 'review'))
                if previous['binding'] != document['binding']:
                    raise DevError('REVIEW_NOT_FOUND', '快照不属于当前项目映射和授权', 404)
                if previous['kind'] == 'review':
                    if previous.get('baseline_released') and previous.get('baseline_ref') == ref:
                        return previous
                    raise DevError('REVIEW_NOT_FOUND', '此编号不是可完成的基线', 404)
                replaced_size = (self.directory / (ref+'.json')).stat().st_size
            used, count = 0, 0
            key = self._review_key(document) if not finalizing else None
            reused = None
            for path, existing, stored_size in self._records(now=sweep_time):
                used += stored_size
                count += 1
                if key is not None and existing['review_key'] == key:
                    reused = path
            if reused is not None:
                return self._read(reused)[0]
            # New baselines leave room for completing reviews. Finalization
            # atomically replaces the baseline at the same reference, so neither
            # record count nor bytes are charged twice and crashes cannot lose both.
            reserve = MAX_DOCUMENT_BYTES if document['kind'] == 'baseline' else 0
            if count + (0 if finalizing else 1) > MAX_RECORDS or used-replaced_size+size > MAX_STORAGE_BYTES-reserve:
                raise DevError('REVIEW_QUOTA', '审阅快照配额不足；新基线保留 24 小时，审阅结果保留七天。请完成已有基线或等待到期回收', 409,
                               used_bytes=used, limit_bytes=MAX_STORAGE_BYTES, records=count,
                               limit_records=MAX_RECORDS, completion_reserve_bytes=reserve)
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
        with self.lock():
            self.load(ref, project, 'baseline')
            (self.directory / (ref+'.json')).unlink(missing_ok=True)
            fsync_directory(self.directory)

    def load(self, ref, project, kind):
        expected_binding = binding(self.engine, project)  # Reauthorize on EVERY read.
        if not isinstance(ref, str) or not re.fullmatch('[a-f0-9]{32}', ref):
            raise DevError('REVIEW_INVALID_REF', '快照编号无效')
        try:
            path = self.directory / (ref+'.json')
            document, _ = self._read(path)
        except FileNotFoundError as exc:
            raise DevError('REVIEW_NOT_FOUND', '快照不存在或已过期；不会用当前目录差异替代历史结果', 404) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise DevError('REVIEW_INTEGRITY', '快照格式损坏', 409) from exc
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if document.get('binding') != expected_binding or document.get('kind') not in kinds:
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
            for path, relative, st in engine.walk(root, root, exclude=review_excluded, deadline=time.monotonic()+2, files_first=True):
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
    return {k: review[k] for k in ('baseline_ref', 'started', 'finished', 'expires_at', 'summary', 'coverage', 'scope')} | {'review_ref': review['id'], 'immutable': True, 'baseline_released': review.get('baseline_released', False)}


def freeze_review(engine, project, baseline_ref, *, finalize=False):
    store = ReviewStore(engine)
    baseline = store.load(baseline_ref, project, ('baseline', 'review') if finalize else 'baseline')
    if baseline['kind'] == 'review':
        if baseline.get('baseline_released') and baseline.get('baseline_ref') == baseline_ref:
            return public_summary(baseline)
        raise DevError('REVIEW_NOT_FOUND', '此编号不是可完成的基线', 404)
    after = capture(engine, project)
    files, added, removed, approximate = [], 0, 0, False
    for path in sorted(baseline['files'].keys() | after['files'].keys()):
        if review_excluded(path):
            # Legacy baselines can contain newly excluded scratch files. A
            # capture-policy change is not evidence that those files were deleted.
            continue
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
                                        'complete': baseline['coverage']['complete'] and after['coverage']['complete']}, 'scope': SCOPE},
                          finalize_project=project if finalize else None)
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
    from agent.git_freshness import freshness
    git = freshness(engine.root(project)[0])
    from agent.project_runtime import descriptor
    from shared.runtime_results import capability_index
    runtime = descriptor(engine.config, project, engine.root(project)[0])
    manifest = {'binding': binding(engine, project),
                'documents': [{k: d[k] for k in ('path', 'sha256')} for d in context['documents']],
                'skills': context['skills'], 'codex_skills': context['codex_skills'],
                'skills_catalog': context['skills_catalog'], 'root_entries': context['root_entries'],
                'remaining_documents': context['remaining_documents'], 'warnings': context['warnings'],
                'tasks': context.get('tasks'), 'execution': context.get('execution'), 'runtime': runtime,
                'view_version': 1, 'context_detail': args.get('context_detail', 'minimal'),
                'workspace_id': args.get('workspace_id', ''),
                'authority_revision': project.get('_authority_revision'),
                'instruction_index': context.get('instruction_index'),
                'project_knowledge': context.get('project_knowledge'),
                'limits': {k: args[k] for k in ('max_files', 'max_chars', 'include_skills')},
                'permissions': {'mode': project['mode'], 'allow_tasks': project.get('allow_tasks'), 'granted_scopes': project.get('_coding_scopes'),
                                'local_roots': engine.config.get('allowed_roots', [])}}
    context_id = fingerprint(manifest)
    unchanged = context_id == args.get('context_id')
    result = {'view_version': 1, 'runtime': runtime, 'capability_index': capability_index(project.get('alias')), 'context_detail': args.get('context_detail', 'minimal'),
              'observed_at': time.time(), 'git': git,
              'context_id': context_id, 'context_unchanged': unchanged,
              'workspace': {'project': project.get('alias'), 'project_id': project.get('id'),
                            'root': context['project_root'], 'mode': project['mode'], 'allow_tasks': project.get('allow_tasks'), 'granted_scopes': project.get('_coding_scopes')},
              'context': None if unchanged else context, 'truncated': context['truncated'],
              'context_scope': 'Bounded bootstrap only. Revalidated on every open; context_id is not an access token.',
              'next': 'fs_read / skills_read on demand; capture baseline BEFORE edits; show_changes to freeze results.'}
    result['_project_view_binding'] = {
        'project_id': project.get('id'), 'root': project.get('_original_root', project.get('root')),
        'device_id': project.get('_coding_device', project.get('device_id')),
        'workspace_id': args.get('workspace_id', ''), 'authority_revision': project.get('_authority_revision'),
        'mode': project.get('mode'), 'allow_tasks': bool(project.get('allow_tasks'))}
    if args.get('capture_baseline'):
        baseline = ReviewStore(engine).save(capture(engine, project))
        result.update(baseline_ref=baseline['id'], baseline_coverage=baseline['coverage'], baseline_expires_at=baseline['expires_at'])
    return result


def show_changes(engine, project, args):
    if args.get('baseline_ref'):
        summary = freeze_review(engine, project, args['baseline_ref'], finalize=args.get('finalize', False))
        args = {**args, 'review_ref': summary['review_ref'], 'baseline_ref': ''}
    return read_review(engine, project, args)


async def watch_cleanup(engine, stop_event):
    """Expiry is independent of new writes and of the Hub connection."""
    while not stop_event.is_set():
        work = asyncio.create_task(asyncio.to_thread(lambda: ReviewStore(engine).cleanup()))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            # Cancelling to_thread alone leaves its filesystem writer alive.
            # Finish it before Agent.run releases the single-instance lock.
            settled = asyncio.gather(work, return_exceptions=True)
            while not settled.done():
                try:
                    await asyncio.shield(settled)
                except asyncio.CancelledError:
                    # Repeated stop/cancel requests must not cancel the wrapper
                    # while its underlying thread still owns filesystem writes.
                    continue
            raise
        except Exception as exc:
            logging.getLogger(__name__).warning('Review cleanup failed (%s)', type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), CLEANUP_INTERVAL)
        except asyncio.TimeoutError:
            pass
