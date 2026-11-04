"""Bounded file-error evidence, never arbitrary Agent exception details."""
import ntpath
from pathlib import PurePosixPath
import re


FILESYSTEM_ERROR_TOOLS = frozenset({
    'read', 'write', 'edit', 'fs_tree', 'fs_read', 'fs_read_many', 'fs_search',
    'fs_preview', 'fs_write', 'fs_edit', 'fs_delete', 'fs_move', 'fs_mkdir',
    'history_restore', 'project_checkpoint', 'apply_patch', 'download_artifact',
    'incoming_upload_finish',
})
FILESYSTEM_ERROR_CODES = frozenset({
    'ATOMIC_CREATE_UNSUPPORTED', 'WRITE_OUTCOME_UNCONFIRMED',
    'DURABILITY_UNCONFIRMED', 'STAGING_CLEANUP_UNCONFIRMED', 'FILESYSTEM_UNAVAILABLE',
})


def safe_filesystem_error_detail(tool, code, details):
    if (not isinstance(tool, str) or not isinstance(code, str)
            or tool not in FILESYSTEM_ERROR_TOOLS or code not in FILESYSTEM_ERROR_CODES
            or not isinstance(details, dict)):
        return {}
    result = {}
    for key in ('retryable', 'mutation_started', 'destination_overwritten', 'atomic', 'partial_content_visible'):
        if type(details.get(key)) is bool:
            result[key] = details[key]
    for key in ('publication', 'durability', 'staging_cleanup'):
        if details.get(key) == 'unconfirmed':
            result[key] = 'unconfirmed'
    number = details.get('filesystem_errno')
    if type(number) is int and 0 <= number <= 65535:
        result['filesystem_errno'] = number
    for key in ('path', 'destination'):
        value = details.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 4096:
            continue
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            continue
        normalized = value.replace('\\', '/')
        if (ntpath.splitdrive(value)[0] or normalized.startswith('/')
                or '..' in PurePosixPath(normalized).parts
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            continue
        result[key] = value
    for key, length in (('backup_id', 32), ('expected_sha256', 64)):
        value = details.get(key)
        if isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(length) + '}', value):
            result[key] = value
    return result
