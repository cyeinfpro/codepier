"""Portable, dependency-free contract for mandatory real runtime qualification."""
CHECKS = frozenset({'non_root', 'workspace_mapping', 'shared_read_write', 'network_denied',
    'immutable_image', 'inspection_before_start', 'original_operation_replay_safe',
    'workload_exit_code', 'timeout_enforced', 'cleanup_confirmed'})


def verified_receipt(value, *, commit, archive_sha256, source_inventory_sha256):
    return (isinstance(value, dict) and type(value.get('schema_version')) is int
        and value['schema_version'] == 1 and value.get('backend') == 'docker_restricted_v1'
        and value.get('verified') is True and value.get('commit') == commit
        and value.get('archive_sha256') == archive_sha256
        and value.get('source_inventory_sha256') == source_inventory_sha256
        and value.get('cleanup_pending') is False and value.get('source_files_unchanged') is True
        and isinstance(value.get('checks'), dict) and set(value['checks']) == CHECKS
        and all(item is True for item in value['checks'].values()))



def pinned_base_image(dockerfile):
    """Select the first real FROM instruction, not the first physical line."""
    import re
    if not isinstance(dockerfile, str) or len(dockerfile) > 1024 * 1024:
        raise ValueError('Dockerfile must be bounded source text')
    for line in dockerfile.splitlines():
        words = line.strip().split()
        if not words or words[0].casefold() != 'from':
            continue
        position = 2 if len(words) > 1 and words[1].startswith('--platform=') else 1
        image = words[position] if len(words) > position else ''
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}', image):
            raise ValueError('First Docker base image must have an immutable sha256 digest')
        return image
    raise ValueError('Dockerfile contains no base image instruction')
