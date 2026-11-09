#!/usr/bin/env python3
"""Publish the current main revision from its source and image build evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_source_bundle import source_version  # noqa: E402
from scripts.ci_source_artifact import source_build_record  # noqa: E402


def read(*command):
    return subprocess.check_output(command, cwd=ROOT, text=True).strip()


def run(*command):
    subprocess.run(command, cwd=ROOT, check=True)


def verify_job(repo, commit, run_id, workflow, name, *, publication=False):
    endpoint = f'repos/{repo}/actions/runs/{run_id}'
    record = json.loads(read('gh', 'api', endpoint))
    if (record['repository']['full_name'] != repo or record['head_repository']['full_name'] != repo
            or record['head_sha'] != commit or record['path'] != workflow
            or record['head_branch'] != 'main' or record['event'] not in {'push', 'workflow_dispatch'}):
        raise ValueError(f'Run {run_id} does not match the trusted workflow and main revision')
    attempt = record['run_attempt']
    jobs = json.loads(read('gh', 'api', f'{endpoint}/attempts/{attempt}/jobs?per_page=100'))['jobs']
    matching = [job for job in jobs if job['name'] == name]
    if len(matching) != 1 or matching[0]['conclusion'] != 'success':
        raise ValueError(f'Run {run_id} needs a successful {name} job')
    if publication and not any(
        step['name'].startswith('Record the published reference') and step['conclusion'] == 'success'
        for step in matching[0]['steps']
    ):
        raise ValueError(f'Image run {run_id} has no successful registry publication')
    return attempt, record['event']


def verify_main(repo, commit):
    if (read('git', 'branch', '--show-current') != 'main'
            or read('git', 'rev-parse', 'HEAD') != commit
            or read('git', 'status', '--porcelain')):
        raise ValueError('Publication requires the verified revision on a clean local main checkout')
    origin = read('git', 'remote', 'get-url', 'origin')
    push_origin = read('git', 'remote', 'get-url', '--push', 'origin')
    origins = {f'https://github.com/{repo}.git', f'https://github.com/{repo}', f'git@github.com:{repo}.git'}
    if origin not in origins or push_origin not in origins:
        raise ValueError('GitHub repository does not match both origin URLs')
    if read('git', 'ls-remote', 'origin', 'refs/heads/main').split() != [commit, 'refs/heads/main']:
        raise ValueError('Local and GitHub main must match the verified commit')


def publish(args):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', args.repo, re.ASCII):
        raise ValueError('Use an owner/repository GitHub name')
    commit = read('git', 'rev-parse', 'HEAD')
    verify_main(args.repo, commit)
    attempt, event = verify_job(args.repo, commit, args.source_run, '.github/workflows/ci.yml',
                                'Build verified public source')
    verify_job(args.repo, commit, args.image_run, '.github/workflows/image.yml',
               'Build, boot-test and publish', publication=True)

    version = source_version()
    tag = f'v{version}'
    prefix = f'codepier-{version}-source'
    with tempfile.TemporaryDirectory(prefix='codepier-release-') as temp:
        directory = Path(temp)
        run('gh', 'run', 'download', str(args.source_run), '-R', args.repo,
            '-n', 'public-source', '-D', temp)
        context = {'GITHUB_SHA': commit, 'GITHUB_REPOSITORY': args.repo,
                   'GITHUB_RUN_ID': str(args.source_run), 'GITHUB_RUN_ATTEMPT': str(attempt),
                   'GITHUB_EVENT_NAME': event}
        proof = source_build_record(ROOT, directory, context)
        if proof != json.loads((directory / 'source-build.json').read_text()):
            raise ValueError('Downloaded assets do not match their source build receipt')
        for suffix, options in (('-full.zip', ()), ('.zip', ('--panel-update',))):
            run(sys.executable, 'scripts/check_release.py', *options, '--bundle', str(directory / (prefix + suffix)))

        # Recheck both receipts in case a new attempt started during verification.
        if verify_job(args.repo, commit, args.source_run, '.github/workflows/ci.yml',
                      'Build verified public source') != (attempt, event):
            raise ValueError('The source run attempt changed during verification')
        verify_job(args.repo, commit, args.image_run, '.github/workflows/image.yml',
                   'Build, boot-test and publish', publication=True)
        if args.check_only:
            print(f'Publication checks passed for {tag} at {commit}')
            return

        verify_main(args.repo, commit)
        tag_ref = f'refs/tags/{tag}'
        remote_tags = dict((ref, oid) for oid, ref in
                           (line.split() for line in read('git', 'ls-remote', '--tags', 'origin',
                                                          tag_ref, tag_ref + '^{}').splitlines()))
        if remote_tags:
            if remote_tags.get(tag_ref + '^{}', remote_tags.get(tag_ref)) != commit:
                raise ValueError('Existing GitHub tag belongs to another commit')
        else:
            if not read('git', 'tag', '--list', tag):
                run('git', 'tag', '-a', tag, commit, '-m', f'CodePier {tag}')
            if read('git', 'rev-parse', tag_ref + '^{}') != commit:
                raise ValueError('Existing local tag belongs to another commit')
            run('git', 'push', 'origin', tag_ref)

        assets = [str(directory / name) for name in sorted(proof['assets'])]
        run('gh', 'release', 'create', tag, '-R', args.repo, '--verify-tag',
            '--title', f'CodePier {tag}', '--notes-file', str(args.notes_file.resolve()), *assets)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--source-run', type=int, required=True)
    parser.add_argument('--image-run', type=int, required=True)
    parser.add_argument('--notes-file', type=Path)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    if not args.check_only and (args.notes_file is None or not args.notes_file.is_file()):
        parser.error('--notes-file must name an existing file when publishing')
    try:
        publish(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
