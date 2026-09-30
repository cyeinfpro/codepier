"""Disposable Agent/Hub fixtures for scheduler and durability regression tests."""
import asyncio
import hashlib
import time
import uuid

import pytest

from agent.runner import Agent
from hub.runtime import Principal, Runtime
from hub.store import Store
from shared.crypto import token
from shared.tool_protocol import wire_version
from shared.util import atomic_json


DATA = b'imported fixture'


@pytest.fixture
def local_agent(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    config = tmp_path / 'config.json'
    atomic_json(config, {'hub_url': 'http://127.0.0.1:9', 'device_id': 'fixture',
        'secret': token(), 'state_dir': str(tmp_path / 'state'),
        'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}], 'tasks': {}})
    instance = Agent(config)
    yield instance, root
    instance.journal.db.close()
    instance.instance_lock.close()


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / 'hub')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','home',?,?)",
                  (store.encrypt(token()), time.time()))
    store.execute("INSERT INTO projects VALUES ('proj','ProjectAlpha','projectalpha','dev','/tmp/fixture','','write',1,?)",
                  (time.time(),))
    instance = Runtime(store)
    instance.wait_seconds = 0
    principal = Principal('panel:admin', 'owner', {'read', 'write', 'execute'}, ['*'], admin=True)
    yield instance, principal
    store.close()


class Peer:
    def __init__(self, cancel_pending_protocol=0):
        self.last_seen = time.time()
        self.journal_id = 'journal-one'
        self.unusable = False
        self.packets = []
        self.cancel_pending_protocol = cancel_pending_protocol

    async def send(self, packet):
        self.packets.append(packet)


async def eventually(predicate, seconds=2):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


def project(root):
    return {'root': str(root), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True}


def request(root, tool, **args):
    return {'id': uuid.uuid4().hex, 'tool': tool, 'project': project(root),
            'tool_contract_version': wire_version(tool), 'args': {'project': 'fixture', **args}}


def download(root):
    return request(root, 'download_artifact', path='imports/nested/file.txt',
        file={'download_url': 'https://files.oaiusercontent.com/fixture',
              'file_id': 'synthetic-file', 'size': len(DATA)},
        expected_sha256=hashlib.sha256(DATA).hexdigest(), idempotency_key=uuid.uuid4().hex)


def waiting(agent, call):
    return any(event['stage'] == 'waiting_resource' for event in agent.telemetry.snapshot(call['id']))
