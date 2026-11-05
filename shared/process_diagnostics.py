"""Allowlisted observations of an operation's already-owned child only."""
PROCESS_STAGES = {
    'process_spawning': '正在创建本操作子进程',
    'process_spawned': '本操作子进程已创建',
    'process_leader_exited': '本操作子进程已退出',
    'process_stdout_eof': '本操作标准输出已关闭',
    'process_cleanup_started': '正在清理本操作子进程',
    'process_cleanup_finished': '本操作子进程清理已返回',
}


def safe_process_detail(detail):
    if not isinstance(detail, dict):
        return {}
    result = {}
    for key in ('pid', 'returncode'):
        value = detail.get(key)
        if type(value) is int and -(2**32) <= value <= 2**32:
            if key != 'pid' or value > 0:
                result[key] = value
        elif key == 'returncode' and value is None and key in detail:
            result[key] = None
    if type(detail.get('reader_done')) is bool:
        result['reader_done'] = detail['reader_done']
    return result
