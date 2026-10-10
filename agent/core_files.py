"""Pi-inspired file primitives, using CodePier's path, SHA and backup guards.

Design reference: badlogic/pi-mono coding-agent tools (MIT), commit d5629e2.
All replacements match the original snapshot; no fuzzy substitution guesses.
"""
from __future__ import annotations

import base64
from shared.crypto import digest
from shared.computer_media import normalize_content
from shared.util import DevError

OUTPUT_BYTES = 50 * 1024


class CoreFiles:
    def __init__(self, engine):
        self.engine = engine

    def read_one(self, root, args, *, text_only=False):
        engine = self.engine
        path = engine.path(root, args['path'], False)
        data = engine.read_bytes(path, max_bytes=16 * 1024 * 1024)
        sha = digest(data)
        if args['expected_sha256'] and args['expected_sha256'] != sha:
            raise DevError('SHA_CONFLICT', '分页读取期间文件改变，请从头重新读取', 409)
        meta = {'path': path.relative_to(root).as_posix(), 'sha256': sha, 'bytes': len(data)}
        mime = 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else None
        if mime and text_only:
            raise DevError('BINARY_FILE', '批量读取仅支持 UTF-8 文本；图片请单独读取')
        if mime:
            if args['offset'] != 1:
                raise DevError('INVALID_OFFSET', '图片不支持文本行分页')
            return {**meta, **normalize_content({'content': [{'type': 'image', 'mimeType': mime,
                'data': base64.b64encode(data).decode('ascii')}]}), 'truncated': False, 'next_offset': None}
        lines = engine.text(data).splitlines(keepends=True)
        start = args['offset'] - 1
        if start > 0 and start >= len(lines):
            raise DevError('INVALID_OFFSET', 'offset 超出文件行数')
        selected, size = [], 0
        for line in lines[start:start + args['limit']]:
            count = len(line.encode('utf-8'))
            if size + count > OUTPUT_BYTES:
                if not selected:
                    raise DevError('LINE_TOO_LARGE', '此行超过 50 KiB，请用 exec 按字节截取；未返回半行')
                break
            selected.append(line)
            size += count
        end = start + len(selected)
        more = end < len(lines)
        return {**meta, 'content': ''.join(selected), 'offset': start + 1, 'end_line': end,
                'total_lines': len(lines), 'truncated': more, 'next_offset': end + 1 if more else None}

    def read_batch(self, root, args):
        # One admitted operation, one root, no sub-dispatch or authorization retry.
        # FileEngine retains all traversal, symlink, hardlink and secret guards.
        files = []
        for index, item in enumerate(args['options']['items']):
            try:
                value = self.read_one(root, item, text_only=True)
                files.append({'index': index, 'ok': True, **value})
            except DevError as exc:
                files.append({'index': index, 'path': item['path'], 'ok': False,
                              'error': {'code': exc.code, 'message': exc.message,
                                        'retryable': False}})
            except OSError:
                # Do not return OS exceptions containing absolute host paths.
                files.append({'index': index, 'path': item['path'], 'ok': False,
                              'error': {'code': 'FILE_READ_FAILED',
                                        'message': '无法读取此文件', 'retryable': False}})
        return {'batch': True, 'files': files,
                'truncated': any(item.get('truncated', False) for item in files)}

    def call(self, tool, project, args):
        engine = self.engine
        root, _ = engine.root(project, tool != 'read')
        if tool == 'read':
            return self.read_batch(root, args) if args.get('operation') == 'batch' else self.read_one(root, args)
        if tool == 'edit' and args.get('changes'):
            return engine.call('apply_patch', project, args)
        path = engine.path(root, args['path'], False)
        # Retain the engine's short mutation critical section for compatibility
        # with legacy writes. Async resource claims cover the complete operation.
        with engine.mutation_lock:
            if tool == 'write':
                after = engine.encode_content(args['content'])
                before = engine.read_bytes(path, True)
                if (digest(before) if before is not None else 'new') != args['expected_sha256']:
                    raise DevError('SHA_CONFLICT', '文件已改变，请重新读取后再修改', 409)
                if not path.parent.is_dir():
                    engine.call('fs_mkdir', project, {'path': path.parent.relative_to(root).as_posix()})
            else:
                before = engine.read_bytes(path)
                if digest(before) != args['expected_sha256']:
                    raise DevError('SHA_CONFLICT', '文件已改变，请重新读取后再修改', 409)
                original = engine.text(before)
                # Index normalization back into the original preserves untouched
                # CRLF/LF bytes, including mixed files and a UTF-8 BOM.
                normalized, positions = [], []
                for index, char in enumerate(original):
                    if char == '\r' and original[index:index + 2] == '\r\n':
                        continue
                    positions.append(index - 1 if char == '\n' and index and original[index - 1] == '\r' else index)
                    normalized.append(char)
                positions.append(len(original))
                text = ''.join(normalized)
                replacements = []
                for edit in args['edits']:
                    needle = edit['old_text'].replace('\r\n', '\n')
                    start = text.find(needle)
                    if start < 0:
                        raise DevError('EDIT_NOT_FOUND', 'old_text 未匹配原文件，未修改')
                    if text.find(needle, start + 1) >= 0:
                        raise DevError('EDIT_AMBIGUOUS', 'old_text 在原文件中不唯一，请补充上下文，未修改')
                    end = start + len(needle)
                    line_ending = '\r\n' if '\r\n' in original[positions[start]:positions[end]] or '\r\n' in original else '\n'
                    replacement = edit['new_text'].replace('\r\n', '\n').replace('\n', line_ending)
                    replacements.append((start, end, replacement))
                replacements.sort()
                if any(left[1] > right[0] for left, right in zip(replacements, replacements[1:])):
                    raise DevError('EDIT_OVERLAP', '多处编辑在原文件中重叠，未修改')
                updated = original
                for start, end, replacement in reversed(replacements):
                    updated = updated[:positions[start]] + replacement + updated[positions[end]:]
                after = engine.encode_content(updated)
            return engine.mutate(project, args['path'], args['expected_sha256'], after)
