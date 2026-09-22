"""One-shot parser workers: a native crash is a failed read, not a dead Agent."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from shared.util import DevError

MAX_SOURCE_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 16384
TIMEOUT_SECONDS = 15.0
_SLOTS = threading.BoundedSemaphore(2)
_ROOT = Path(__file__).resolve().parents[1]


def _worker_command():
    python = Path(sys.executable)
    if os.name == 'nt' and python.name.lower() == 'pythonw.exe':
        python = python.with_name('python.exe')
    # -I ignores project directories, PYTHONPATH and user site packages. The
    # parent has already read/authorized the source; never import project code.
    bootstrap = ('import sys; sys.path.insert(0, ' + repr(str(_ROOT)) + '); '
                 'from agent.symbol_worker import main; main()')
    return [str(python), '-I', '-c', bootstrap]


def analyze_isolated(path, source, *, timeout=None, max_nodes=100000,
                     max_metadata_bytes=2 * 1024 * 1024):
    from agent.symbols import SUPPORTED
    if Path(path).suffix.lower() not in SUPPORTED:
        raise DevError('UNSUPPORTED_LANGUAGE', '结构检索目前支持 Python、JavaScript、TypeScript 和 TSX；其他文件使用文本搜索')
    if not isinstance(source, bytes) or len(source) > MAX_SOURCE_BYTES:
        raise DevError('CODE_ANALYSIS_LIMIT', '代码解析输入超过 1 MiB 上限，请缩小文件')
    timeout = TIMEOUT_SECONDS if timeout is None else min(float(timeout), TIMEOUT_SECONDS)
    if not math.isfinite(timeout) or timeout <= 0:
        raise DevError('PARSER_TIMEOUT', '代码解析已超过时间预算，请缩小文件后重试')
    header = json.dumps({'path': str(path), 'max_nodes': max_nodes,
                         'max_metadata_bytes': max_metadata_bytes}, ensure_ascii=True).encode() + b'\n'
    if len(header) > MAX_HEADER_BYTES:
        raise DevError('CODE_ANALYSIS_LIMIT', '代码解析路径超过上限')
    deadline = time.monotonic() + timeout
    if not _SLOTS.acquire(timeout=min(timeout, 2.0)):
        raise DevError('PARSER_BUSY', '代码解析工作进程繁忙，请稍后重试', 429)
    try:
        # An anonymous temporary file keeps even an invalid worker response out
        # of Agent memory until its size has been checked. No source files or
        # parser stderr are copied into error messages.
        with tempfile.TemporaryFile() as output:
            try:
                with subprocess.Popen(
                    _worker_command(), cwd=_ROOT, stdin=subprocess.PIPE,
                    stdout=output, stderr=subprocess.DEVNULL, close_fds=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                ) as process:
                    try:
                        process.communicate(header + source, timeout=max(.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired as exc:
                        process.kill()
                        process.communicate()
                        raise DevError('PARSER_TIMEOUT', '代码解析超时，工作进程已停止；请缩小文件后重试') from exc
                    except BaseException:
                        process.kill()
                        process.wait()
                        raise
                    if process.returncode:
                        raise DevError('PARSER_CRASHED', f'代码解析工作进程异常退出（{process.returncode}）；Agent 保持运行，当前请求未自动重试')
            except OSError as exc:
                raise DevError('PARSER_UNAVAILABLE', '代码解析工作进程无法启动或读取，请检查本机运行环境', 503) from exc
            if output.tell() > MAX_RESULT_BYTES:
                raise DevError('CODE_ANALYSIS_LIMIT', '代码解析结果超过大小上限，请缩小文件')
            output.seek(0)
            try:
                response = json.loads(output.read(MAX_RESULT_BYTES + 1))
                if not isinstance(response, dict) or type(response.get('ok')) is not bool:
                    raise ValueError('invalid envelope')
                if not response['ok']:
                    error = response['error']
                    if not isinstance(error['code'], str) or not isinstance(error['message'], str):
                        raise ValueError('invalid error')
                    raise DevError(error['code'], error['message'])
                result = response['data']
                if (not isinstance(result, dict)
                        or not all(isinstance(result.get(k), list) for k in ('symbols', 'references'))
                        or not all(isinstance(result.get(k), str) for k in ('language', 'backend', 'precision', 'column_unit'))):
                    raise ValueError('invalid result')
                return result
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                raise DevError('PARSER_FAILED', '代码解析工作进程返回无效结果；当前请求未自动重试') from exc
    finally:
        _SLOTS.release()


def _resource_limits():
    if os.name != 'posix':
        return
    import resource
    # Set limits inside the fresh interpreter, never preexec_fn in a threaded
    # Agent. Parent timeout also covers startup, I/O stalls and sleeping workers.
    for kind, limit in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_CPU, 16),
                        (resource.RLIMIT_FSIZE, MAX_RESULT_BYTES)):
        _, hard = resource.getrlimit(kind)
        limit = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        resource.setrlimit(kind, (limit, limit))
    if sys.platform.startswith('linux'):
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = 512 * 1024 * 1024
        limit = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def main():
    _resource_limits()
    from agent import symbols
    try:
        header = sys.stdin.buffer.readline(MAX_HEADER_BYTES + 1)
        if len(header) > MAX_HEADER_BYTES or not header.endswith(b'\n'):
            raise ValueError('invalid header')
        request = json.loads(header)
        for key, cap in (('max_nodes', symbols.MAX_NODES),
                         ('max_metadata_bytes', symbols.MAX_METADATA_BYTES)):
            value = request[key]
            if type(value) is not int or not 0 < value <= cap:
                raise ValueError('invalid budget')
        source = sys.stdin.buffer.read(MAX_SOURCE_BYTES + 1)
        if len(source) > MAX_SOURCE_BYTES:
            raise DevError('CODE_ANALYSIS_LIMIT', '代码解析输入超过 1 MiB 上限')
        symbols.MAX_NODES = request['max_nodes']
        symbols.MAX_METADATA_BYTES = request['max_metadata_bytes']
        result = symbols._analyze_in_process(request['path'], source)
        response = {'ok': True, 'data': result}
    except DevError as exc:
        response = {'ok': False, 'error': {'code': exc.code, 'message': exc.message}}
    except MemoryError:
        response = {'ok': False, 'error': {'code': 'CODE_ANALYSIS_LIMIT', 'message': '代码解析超过内存预算'}}
    except Exception:
        response = {'ok': False, 'error': {'code': 'PARSER_FAILED', 'message': '代码解析失败，请检查文件或本机解析依赖'}}
    encoded = json.dumps(response, ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_RESULT_BYTES:
        encoded = b'{"ok":false,"error":{"code":"CODE_ANALYSIS_LIMIT","message":"Parser result exceeds size limit"}}'
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    main()
