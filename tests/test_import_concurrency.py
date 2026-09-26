"""Imports share destination locks without blocking sibling project work."""
import asyncio
import hashlib
import threading
import uuid

import pytest

from shared.tool_protocol import wire_version
from tests.test_audit_agent import eventually, local_agent


DATA = b'imported fixture'


def project(root):
    return {'root': str(root), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True}


def request(root, tool, **args):
    identifier = uuid.uuid4().hex
    return {'id': identifier, 'tool': tool, 'project': project(root),
            'tool_contract_version': wire_version(tool),
            'args': {'project': 'fixture', **args}}


def download(root):
    return request(root, 'download_artifact', path='imports/nested/file.txt',
        file={'download_url': 'https://files.oaiusercontent.com/fixture',
              'file_id': 'synthetic-file', 'size': len(DATA)},
        expected_sha256=hashlib.sha256(DATA).hexdigest(), idempotency_key=uuid.uuid4().hex)


async def read_sibling(agent, root):
    sibling = root / 'tower'
    sibling.mkdir(exist_ok=True)
    (sibling / 'design.txt').write_text('readable')
    # Cover both the current file API and tools using legacy project slots.
    for tool in ('read', 'fs_read'):
        call = request(sibling, tool, path='design.txt')
        await asyncio.wait_for(agent.handle(call), 2)
        result = agent.journal.status(call['id'])['result']
        assert result['ok'] and result['data']['content'] == 'readable'


def waiting(agent, call):
    return any(event['stage'] == 'waiting_resource'
               for event in agent.telemetry.snapshot(call['id']))


@pytest.mark.asyncio
async def test_import_and_sibling_reads_progress_during_unrelated_release(local_agent, monkeypatch):
    agent, root = local_agent
    repository = root / 'codepier'
    repository.mkdir()
    started, release = threading.Event(), threading.Event()

    def stream(*args):
        started.set()
        assert release.wait(10)
        yield DATA

    monkeypatch.setattr('agent.incoming_artifacts.download_chunks', stream)
    call = download(root)
    task = None
    try:
        async with agent.execution_slot('release-test', 'exec', project(repository),
                {'cwd': '.', 'resources': [{'kind': 'path', 'name': '.', 'mode': 'read'}]},
                repository, True, None):
            task = asyncio.create_task(agent.handle(call))
            await eventually(started.is_set)
            await read_sibling(agent, root)
            assert not task.done()
            release.set()
            await asyncio.wait_for(task, 2)
            assert agent.journal.status(call['id'])['result']['ok']
            assert (root / call['args']['path']).read_bytes() == DATA
    finally:
        release.set()
        if task:
            await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('held_path', ['imports/nested/file.txt', 'imports'])
async def test_queued_import_preserves_destination_exclusion_without_blocking_siblings(
        local_agent, monkeypatch, held_path):
    agent, root = local_agent
    monkeypatch.setattr('agent.incoming_artifacts.download_chunks', lambda *args: iter([DATA]))
    call = download(root)
    task = None
    try:
        async with agent.execution_slot('destination-reader', 'exec', project(root),
                {'cwd': '.', 'resources': [{'kind': 'path', 'name': held_path, 'mode': 'read'}]},
                root, False, None):
            task = asyncio.create_task(agent.handle(call))
            await eventually(lambda: waiting(agent, call))
            assert agent.journal.status(call['id'])['status'] == 'accepted'
            await read_sibling(agent, root)
            assert not task.done() and not (root / call['args']['path']).exists()
        await asyncio.wait_for(task, 2)
        assert agent.journal.status(call['id'])['result']['ok']
        assert (root / call['args']['path']).read_bytes() == DATA
    finally:
        if task:
            await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_duplicate_import_and_target_read_wait_for_atomic_publication(local_agent, monkeypatch):
    agent, root = local_agent
    started, release = threading.Event(), threading.Event()
    downloads = []

    def stream(*args):
        downloads.append(True)
        started.set()
        assert release.wait(10)
        yield DATA

    monkeypatch.setattr('agent.incoming_artifacts.download_chunks', stream)
    first, duplicate = download(root), download(root)
    read = request(root, 'read', path=first['args']['path'])
    tasks = [asyncio.create_task(agent.handle(first))]
    try:
        await eventually(started.is_set)
        tasks.extend(asyncio.create_task(agent.handle(call)) for call in (duplicate, read))
        await eventually(lambda: all(waiting(agent, call) for call in (duplicate, read)))
        await read_sibling(agent, root)
        assert not (root / first['args']['path']).exists()
        assert len(downloads) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert agent.journal.status(first['id'])['result']['ok']
        assert agent.journal.status(duplicate['id'])['result']['error']['code'] == 'ARTIFACT_DESTINATION_EXISTS'
        assert agent.journal.status(read['id'])['result']['data']['content'] == DATA.decode()
        assert len(downloads) == 1
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
