"""Shared isolated collaboration fixtures, never real host subscriptions or credentials."""
import time
import uuid
from dataclasses import replace

import pytest
from hub.collaboration.config import CollaborationConfig
from hub.collaboration.schema import migrate
from hub.collaboration.service import CollaborationService
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from tests.legacy_iam_fixture import seed_owner, seed_grant
from tests.support import running_stack


def key():
    return uuid.uuid4().hex


@pytest.fixture
def collab(tmp_path):
    store = Store(tmp_path / 'hub')
    with store.transaction():
        migrate(store.db)
    seed_owner(store, 'owner', 'admin')
    seed_owner(store, 'other', 'other')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,?)", (store.encrypt('synthetic-test-device'), time.time()))
    for pid in ('proj', 'otherproj'):
        store.execute('''INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created)
            VALUES (?,?,?,'dev','/tmp/fixture','write',1,?)''', (pid, pid, pid, time.time()))
    for grant in ('worker', 'dot', 'second'):
        seed_grant(store, grant, 'owner', scopes=('read',), projects=('proj',))
    seed_grant(store, 'broad', 'owner', scopes=('read', 'write', 'execute'), projects=('proj',))
    runtime = Runtime(store)
    clock = [time.time()]
    service = CollaborationService(runtime, CollaborationConfig(enabled=True), clock=lambda: clock[0])
    owner = Principal('panel:admin', 'owner', {'read', 'write'}, ['*'], admin=True)
    worker = Principal('mcp:worker:fixture', 'owner', {'read'}, ['proj'], grant_id='worker')
    dot = replace(worker, grant_id='dot', actor='mcp:dot:fixture')
    scope = {'project': 'proj', 'environment_id': 'production'}
    room = service.room_create({**scope, 'idempotency_key': key()}, owner)['room']
    agents = [service.register_agent({**scope, 'label': 'same name', 'kind': kind,
              'grant_id': grant, 'idempotency_key': key()}, owner)
              for kind, grant in [('work_cloud', 'worker'), ('dot', 'dot')]]
    yield service, owner, worker, dot, room, agents, clock, scope
    store.close()



@pytest.fixture
def collaboration_stack(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'true')
    monkeypatch.setenv('CODEPIER_MCP_EVENTS_ENABLED', 'true')
    monkeypatch.setenv('CODEPIER_MONITOR_COLLECTOR_ENABLED', 'false')
    monkeypatch.setenv('CODEPIER_ANALYSIS_DISPATCH_ENABLED', 'false')
    with running_stack(tmp_path / 'collaboration-http') as stack:
        yield stack
