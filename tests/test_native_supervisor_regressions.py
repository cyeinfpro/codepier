"""Independent regressions for native replay ordering and offline history safety."""
from contextlib import closing
from types import SimpleNamespace
import time
import uuid
import base64

import pytest
from hub.native_cli import NativeService
from shared.native_cli import database


class NoAck:
    async def send(self, message):
        self.message = message


def cache(tmp_path):
    project = {'id': uuid.uuid4().hex, 'device_id': uuid.uuid4().hex,
               'root': '/a/project', 'mode': 'write', 'allow_tasks': True}
    store = SimpleNamespace(directory=tmp_path, one=lambda *args: project)
    service = NativeService(SimpleNamespace(store=store))
    row = {'id': uuid.uuid4().hex, 'project_id': project['id'], 'device_id': project['device_id'],
           'root': project['root'], 'cwd': project['root'], 'provider': 'codex', 'title': 'Owned session',
           'status': 'running', 'created': time.time()-50, 'updated': time.time()-20,
           'size': 5, 'exit_code': None, 'error': ''}
    return service, project, row


@pytest.mark.asyncio
@pytest.mark.parametrize('cleared', ['cleared', 'deleted'])
async def test_old_sync_never_resurrects_cleared_history(tmp_path, cleared):
    service, project, old = cache(tmp_path)
    con = NoAck()
    packet = {'type': 'native_sync', 'sessions': [old], 'chunks': [
        {'id': old['id'], 'offset': 0, 'data': base64.b64encode(b'hello').decode()}]}
    await service.receive(project['device_id'], con, packet)
    tombstone = {**old, 'status': cleared, 'size': 0, 'updated': time.time()}
    await service.receive(project['device_id'], con, {'type': 'native_sync', 'sessions': [tombstone], 'chunks': []})
    await service.receive(project['device_id'], con, packet)
    with closing(database(service.directory)) as db:
        row = db.execute('SELECT * FROM sessions WHERE id=?', (old['id'],)).fetchone()
        assert row['status'] == cleared
        assert db.execute('SELECT count(*) FROM output WHERE session=?', (old['id'],)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_late_start_reply_never_revives_exited_session(tmp_path):
    service, project, row = cache(tmp_path)
    con = NoAck()
    await service.receive(project['device_id'], con, {'type': 'native_sync', 'sessions': [
        {**row, 'status': 'exited', 'exit_code': 0, 'updated': time.time()}], 'chunks': []})
    await service.receive(project['device_id'], con, {'type': 'native_sync', 'sessions': [
        {**row, 'status': 'starting'}], 'chunks': []})
    with closing(database(service.directory)) as db:
        result = db.execute('SELECT status,exit_code FROM sessions WHERE id=?', (row['id'],)).fetchone()
        assert result['status'] == 'exited'
        assert result['exit_code'] == 0
