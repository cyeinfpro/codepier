"""SHA-checked batch edits with all-path preflight and observable rollback.

This is deliberately NOT a filesystem transaction. The existing file engine
supplies per-file backups and compare-and-swap; local editors remain concurrent.
"""
from __future__ import annotations
import stat
import sys
import uuid
from shared.crypto import digest
from shared.util import DevError, atomic_json

MAX_BATCH_BYTES = 2 * 1024 * 1024


def apply_patch(engine, project, args):
    with engine.mutation_lock:
        root, _ = engine.root(project, True)
        plans, touched, budget = [], set(), 0

        def prepare(relative, expected, after, mode=None):
            nonlocal budget
            path = engine.path(root, relative, False)
            relative = path.relative_to(root).as_posix()
            # real path keys also reject alias/case collisions on macOS/Windows.
            identity = str(path.resolve()).casefold() if sys.platform in ('darwin', 'win32') else str(path.resolve())
            if identity in touched:
                raise DevError('PATCH_DUPLICATE_PATH', '同一批补丁不能重复触碰同一路径', 409)
            touched.add(identity)
            engine.check_local_write(path)
            before = engine.read_bytes(path, True)
            if before is not None:
                engine.text(before)
            actual = digest(before) if before is not None else 'new'
            if actual != expected:
                raise DevError('SHA_CONFLICT', '整批预检冲突：'+relative+'；没有写入任何文件', 409)
            if not path.parent.is_dir():
                raise DevError('PARENT_MISSING', '先创建父目录：'+relative, 409)
            before_mode = stat.S_IMODE(path.stat().st_mode) if before is not None else None
            budget += len(before or b'') + len(after or b'')
            if budget > MAX_BATCH_BYTES:
                raise DevError('PATCH_TOO_LARGE', '整批修改前后内容合计上限为 2 MiB；没有写入', 413)
            plans.append(dict(path=relative, before=before, after=after, expected=actual,
                              after_sha=digest(after) if after is not None else 'new',
                              before_mode=before_mode, mode=mode))

        for change in args['changes']:
            action, path, expected = change['action'], change['path'], change['expected_sha256']
            if action == 'write':
                prepare(path, expected, engine.encode_content(change['content']))
            elif action == 'delete':
                prepare(path, expected, None)
            else:
                source = engine.path(root, path, False)
                data = engine.read_bytes(source)
                engine.text(data)
                if digest(data) != expected:
                    raise DevError('SHA_CONFLICT', '移动源文件已改变；整批未写入', 409)
                prepare(change['destination'], 'new', data, stat.S_IMODE(source.stat().st_mode))
                prepare(path, expected, None)

        previews = []
        remaining = 64000
        for plan in plans:
            diff = engine.diff(plan['path'], plan['before'], plan['after'])
            text = diff['diff'][:remaining]
            remaining -= len(text)
            previews.append({'path': plan['path'], 'sha256': plan['after_sha'], **diff,
                             'diff': text, 'diff_truncated': diff['diff_truncated'] or len(text) != len(diff['diff'])})
        if args['dry_run']:
            return {'success': True, 'outcome': 'preview', 'atomic': False, 'files': previews,
                    'added_lines': sum(x['added_lines'] for x in previews),
                    'removed_lines': sum(x['removed_lines'] for x in previews)}

        patch_id = uuid.uuid4().hex
        manifest_path = engine.journal.directory / 'coding-patches' / (patch_id+'.json')
        manifest = {'patch_id': patch_id, 'project_root': str(root), 'outcome': 'prepared', 'files': []}
        # Back up the WHOLE batch before publishing its first filesystem change.
        for plan in plans:
            backup = uuid.uuid4().hex
            engine.journal.add_backup(backup, str(root), plan['path'], plan['before'], plan['after_sha'], before_mode=plan['before_mode'])
            plan['backup_id'] = backup
            manifest['files'].append({'path': plan['path'], 'before_sha256': plan['expected'],
                                      'after_sha256': plan['after_sha'], 'backup_id': backup, 'state': 'pending'})
        atomic_json(manifest_path, manifest)
        attempted = []
        try:
            for plan in plans:
                attempted.append(plan)
                row = manifest['files'][len(attempted)-1]
                row['state'] = 'applying'
                atomic_json(manifest_path, manifest)
                engine.mutate(project, plan['path'], plan['expected'], plan['after'], restore_mode=plan['mode'])
                row['state'] = 'completed'
            manifest['outcome'] = 'completed'
            atomic_json(manifest_path, manifest)
        except (DevError, OSError) as exc:
            rollback_errors, restored = [], []
            for plan in reversed(attempted):
                try:
                    path = engine.path(root, plan['path'], False)
                    current = engine.read_bytes(path, True)
                    actual = digest(current) if current is not None else 'new'
                    mode = stat.S_IMODE(path.stat().st_mode) if current is not None else None
                    if actual == plan['expected'] and mode == plan['before_mode']:
                        continue
                    if actual != plan['after_sha']:
                        raise DevError('ROLLBACK_CONFLICT', '本机出现其他修改，未覆盖', 409)
                    engine.mutate(project, plan['path'], actual, plan['before'], restore_mode=plan['before_mode'])
                    restored.append(plan['path'])
                except (DevError, OSError) as rollback_error:
                    rollback_errors.append({'path': plan['path'], 'code': getattr(rollback_error, 'code', 'ROLLBACK_IO'),
                                            'message': str(rollback_error)[:240], 'backup_id': plan['backup_id']})
            failed_paths = {x['path'] for x in rollback_errors}
            attempted_paths = {p['path'] for p in attempted}
            for row in manifest['files']:
                row['state'] = 'needs_review' if row['path'] in failed_paths else 'original_restored' if row['path'] in attempted_paths else 'not_applied'
            outcome = 'partial' if rollback_errors else 'rolled_back'
            manifest.update(outcome=outcome, restored=restored, rollback_errors=rollback_errors)
            try:
                atomic_json(manifest_path, manifest)
            except OSError:
                manifest['manifest_durability_unconfirmed'] = True
            return {'success': False, 'atomic': False, 'patch_id': patch_id, 'outcome': outcome,
                    'files': manifest['files'], 'attempted': [p['path'] for p in attempted], 'restored': restored, 'rollback_errors': rollback_errors,
                    'error': {'code': 'PATCH_PARTIAL' if rollback_errors else 'PATCH_ROLLED_BACK',
                              'message': '批量写入未完成；已保留逐文件备份。'+str(exc)[:500]},
                    'next': 'Inspect files and backups; do not blindly repeat the mutation.'}
        return {'success': True, 'outcome': 'completed', 'atomic': False, 'patch_id': patch_id,
                'files': [{**preview, 'backup_id': plan['backup_id']} for preview, plan in zip(previews, plans)],
                'added_lines': sum(x['added_lines'] for x in previews),
                'removed_lines': sum(x['removed_lines'] for x in previews)}
