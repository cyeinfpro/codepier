"""Killable isolation for bounded, read-only project filesystem operations.

This is process isolation from blocked filesystem calls, not an OS sandbox.
Authorization still uses FileEngine and the parent's current guards/queues.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from shared.util import DevError

PROBE_TIMEOUT = 15
READ_TOOLS = frozenset({'open_workspace', 'project_context', 'read',
                        'fs_read', 'fs_read_many', 'fs_tree', 'fs_search'})


def eligible(tool, args):
    return tool in READ_TOOLS and not args.get('capture_baseline')


async def probe(engine, tool, project, args, *, phase='execute', expected_root=None):
    base = str(Path(__file__).resolve().parents[1])
    # Ignore user site packages and PYTHONPATH; never import project code.
    command = [sys.executable, '-I', '-c',
               'import sys; sys.path.insert(0, ' + repr(base) + '); '
               'from agent.workspace_probe import main; main()']
    payload = {'config': engine.config, 'config_path': str(engine.config_path),
               'state_dir': str(engine.journal.directory),
               'tool': tool, 'project': project, 'args': args, 'phase': phase,
               'expected_root': str(expected_root) if expected_root is not None else None}
    result = await run_worker(command, json.dumps(payload).encode(), cwd=base)
    if not result['ok']:
        error = result['error']
        raise DevError(error['code'], error['message'], error.get('status', 400),
                       **error.get('details', {}))
    return result['data']


async def root(engine, tool, project, args):
    result = await probe(engine, tool, project, args, phase='root')
    return Path(result['root']), result['spec']


async def _finish(task):
    # Cleanup owns the child until it has been reaped, even if shutdown sends
    # another cancellation. This waits for one cleanup, never retries the read.
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _reap(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def run_worker(command, payload, *, cwd, timeout=None):
    process = None
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        *command, cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL))
    try:
        # Cancellation during creation must still recover and reap that child.
        process = await asyncio.shield(spawning)
        output, _ = await asyncio.wait_for(process.communicate(payload),
                                         PROBE_TIMEOUT if timeout is None else timeout)
        if process.returncode:
            raise DevError('PROJECT_DISCOVERY_FAILED', '项目概览读取进程异常退出')
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise ValueError('invalid response')
            if result['ok']:
                if 'data' not in result:
                    raise ValueError('missing data')
            else:
                error = result.get('error')
                if (not isinstance(error, dict) or not isinstance(error.get('code'), str)
                        or not isinstance(error.get('message'), str)
                        or type(error.get('status', 400)) is not int
                        or not isinstance(error.get('details', {}), dict)):
                    raise ValueError('invalid error')
            return result
        except (ValueError, UnicodeError) as exc:
            raise DevError('PROJECT_DISCOVERY_FAILED', '项目概览读取进程返回无效结果') from exc
    except TimeoutError as exc:
        raise DevError('PROJECT_DISCOVERY_TIMEOUT',
                       '项目概览读取超时；已停止读取并释放队列，请检查本机目录访问权限或磁盘状态') from exc
    finally:
        cancelled = False
        if process is None:
            process, cancelled = await _finish(spawning)
        _, reap_cancelled = await _finish(asyncio.create_task(_reap(process)))
        if cancelled or reap_cancelled:
            raise asyncio.CancelledError


def dispatch(request):
    from agent.filesystem import FileEngine
    tool, args = request['tool'], request['args']
    if not eligible(tool, args):
        raise DevError('INVALID_REQUEST', '独立读取进程仅接受无快照的项目概览')
    engine = FileEngine(request['config'],
                        SimpleNamespace(directory=Path(request['state_dir'])),
                        Path(request['config_path']))
    phase = request.get('phase', 'execute')
    if phase in {'root', 'claims'}:
        actual_root, spec = engine.root(request['project'])
        if phase == 'root':
            return {'root': str(actual_root), 'spec': spec}
        if str(actual_root) != request.get('expected_root'):
            raise DevError('ROOT_CHANGED', '项目根目录发生变化，请确认映射后重新提交')
        from agent.resource_queue import Claim, claims_for, canonical_path, PATH_READ_TOOLS
        claims = (claims_for(engine, tool, request['project'], args, actual_root)
                  if tool == 'read' or tool in PATH_READ_TOOLS
                  else [Claim('agent', 'path', canonical_path(actual_root), False)])
        return {'claims': [asdict(claim) for claim in claims],
                'project_key': request['project'].get('id') or canonical_path(
                    request['project'].get('_original_root', actual_root))}
    if phase != 'execute':
        raise DevError('INVALID_REQUEST', '未知的独立读取阶段')
    if tool == 'read':
        from agent.core_files import CoreFiles
        return CoreFiles(engine).call(tool, request['project'], args)
    return engine.call(tool, request['project'], args)


def main():
    request = json.load(sys.stdin)
    try:
        result = {'ok': True, 'data': dispatch(request)}
    except DevError as exc:
        result = {'ok': False, 'error': {'code': exc.code, 'message': exc.message,
                                        'status': exc.status, 'details': exc.details}}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
