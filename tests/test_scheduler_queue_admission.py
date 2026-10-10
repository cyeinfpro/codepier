"""Hub admission reserves cross-project queue headroom before Agent fairness."""
import time

import pytest

from shared.util import DevError
from tests.test_reliability import runtime


def project(store, identifier):
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) VALUES (?,?,?,'dev',?,'','write',1,?)",
                  (identifier, identifier, identifier, "/tmp/" + identifier, time.time()))


async def submit(r, principal, alias, index):
    return await r.invoke("fs_read", {"project": alias, "path": str(index) + ".txt",
                                    "idempotency_key": "queue-fair-" + alias + "-" + str(index)}, principal)


@pytest.mark.asyncio
async def test_busy_project_cannot_fill_node_and_block_other_project(runtime):
    r, principal = runtime
    project(r.store, "other")
    receipts = [await submit(r, principal, "proj", i) for i in range(32)]
    with pytest.raises(DevError) as error:
        await submit(r, principal, "proj", 32)
    assert error.value.code == "PROJECT_BUSY"
    assert (await submit(r, principal, "other", 0))["pending"]
    # Even while a project is full, same-key recovery never creates a new job.
    assert (await submit(r, principal, "proj", 0))["operation_id"] == receipts[0]["operation_id"]
    assert r.store.one("SELECT count(*) AS n FROM operations")["n"] == 33


@pytest.mark.asyncio
async def test_new_project_headroom_and_absolute_device_bound(runtime):
    r, principal = runtime
    project(r.store, "second")
    for i in range(32):
        await submit(r, principal, "proj", i)
    for i in range(24):
        await submit(r, principal, "second", i)
    with pytest.raises(DevError) as error:
        await submit(r, principal, "second", 24)
    assert error.value.code == "DEVICE_BUSY"
    for i in range(8):
        alias = "fresh" + str(i)
        project(r.store, alias)
        assert (await submit(r, principal, alias, 0))["pending"]
    project(r.store, "last")
    with pytest.raises(DevError) as error:
        await submit(r, principal, "last", 0)
    assert error.value.code == "DEVICE_BUSY"
    assert r.store.one("SELECT count(*) AS n FROM operations")["n"] == 64
