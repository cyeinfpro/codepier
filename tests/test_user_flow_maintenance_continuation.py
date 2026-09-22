"""F02: maintenance admission and acknowledgement races with disposable plans."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from agent.runner import Agent
from shared.util import DevError
from tests.test_agent_lifecycle import managed_manager


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_prepare',[False,True])
async def test_download_fences_new_work_before_yield_and_cancellation_does_not_release_it(tmp_path,monkeypatch,cancel_prepare):
    manager,base=managed_manager(tmp_path,monkeypatch)
    entered,release=threading.Event(),threading.Event()
    monkeypatch.setattr(manager,'actions',lambda:{'agent_update'})
    def download(operation,args,phase):
        entered.set()
        assert release.wait(5)
        manager._write_plan(operation,{'action':'agent_update','base':str(base),
            'helper':str(base/'helper.py'),'python':str(base/'python'),'candidate':str(base/'candidate')})
        return {'stage':'prepared'}
    monkeypatch.setattr(manager,'_prepare_update',download)
    task=asyncio.create_task(manager.prepare('original-maintenance','agent_update',{}))
    try:
        assert await asyncio.to_thread(entered.wait,2)
        assert manager.handoff_pending and manager.maintenance_operation=='original-maintenance'
        with pytest.raises(DevError) as error:
            await manager.prepare('second-maintenance','agent_update',{})
        assert error.value.code=='AGENT_MAINTENANCE'
        if cancel_prepare:
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done() and manager.handoff_pending
    finally:
        release.set()
        result=await asyncio.wait_for(task,3)
    assert result['stage']=='prepared'
    assert manager.handoff_pending and manager.has_pending('original-maintenance')


@pytest.mark.asyncio
@pytest.mark.parametrize('activity',['job','process','transfer','cleanup','desktop'])
async def test_ack_rechecks_activity_and_retains_the_plan_without_launching(tmp_path,monkeypatch,activity):
    manager,base=managed_manager(tmp_path,monkeypatch)
    operation='maintenance'
    manager._write_plan(operation,{'action':'agent_restart','base':str(base),
        'helper':str(base/'helper.py'),'python':str(base/'python')})
    busy=asyncio.get_running_loop().create_future()
    owner=SimpleNamespace(computer=SimpleNamespace(session=object() if activity=='desktop' else None),
        jobs={operation:busy,**({'other':busy} if activity=='job' else {})},
        processes={'other':object()} if activity=='process' else {},
        transfer_tasks={busy} if activity=='transfer' else set(),
        kill_tasks={busy} if activity=='cleanup' else set())
    manager.activity_check=lambda identifier:Agent.check_maintenance_idle(owner,identifier)
    launched=[]
    monkeypatch.setattr(manager,'_launch_helper',lambda *_args:launched.append(True))
    try:
        with pytest.raises(DevError) as error:await manager.acknowledge(operation)
        assert error.value.code==('COMPUTER_BUSY' if activity=='desktop' else 'AGENT_BUSY')
        assert manager.has_pending(operation) and manager.handoff_pending and not launched
        owner.jobs.pop('other',None);owner.processes.clear();owner.transfer_tasks.clear();owner.kill_tasks.clear()
        owner.computer.session=None
        assert await manager.acknowledge(operation)
        assert launched==[True] and not manager.has_pending(operation)
    finally:busy.cancel()
