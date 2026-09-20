"""Allowlisted desktop diagnostics; never retain provider text or input."""
import math
import re

COMPUTER_STAGES = {
    'native_startup': '正在连接原生桌面接口',
    'native_observe': '正在读取应用画面',
    'native_preflight': '正在核对输入前的画面',
    'native_action': '正在执行原生输入',
    'approval_wait': '等待本人批准应用访问',
    'approval_decided': '应用授权等待已结束',
}
OUTCOMES = {'started', 'completed', 'failed', 'cancelled', 'accept', 'decline', 'cancel'}


def safe_detail(detail):
    result = {}
    for key in ('duration_ms', 'approval_wait_ms', 'native_call_ms', 'provider_exit_code', 'native_error_code'):
        value = detail.get(key)
        if type(value) is int and -(2**31) <= value < 2**31 and (not key.endswith('_ms') or value >= 0):
            result[key] = value
    value = detail.get('approval_timeout_seconds')
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 90:
        result['approval_timeout_seconds'] = value
    value = detail.get('outcome')
    if isinstance(value, str) and value in OUTCOMES:
        result['outcome'] = value
    value = detail.get('error_code')
    if isinstance(value, str) and re.fullmatch(r'(?:COMPUTER_[A-Z_]+|QUEUE_EXPIRED|CANCELLED)', value) and len(value) <= 80:
        result['error_code'] = value
    return result


def native_error(error):
    """Classify a provider error locally without exporting its arbitrary message."""
    from shared.util import DevError
    error = error if isinstance(error, dict) else {}
    message = error.get('message')
    message = message[:2000].lower() if isinstance(message, str) else ''
    code, hint = 'COMPUTER_NATIVE_ERROR', '原生接口拒绝请求，请检查本机权限和接口版本'
    if 'ambiguous' in message or 'multiple apps' in message or 'multiple applications' in message:
        code, hint = 'COMPUTER_APP_AMBIGUOUS', '应用匹配多个安装路径；请用 computer_apps 核对并选择准确的应用绝对路径'
    elif 'permission' in message or 'denied' in message or 'not authorized' in message:
        code, hint = 'COMPUTER_PERMISSION_REQUIRED', '原生接口拒绝访问；请由本人检查本机系统权限和应用授权'
    detail = safe_detail({'native_error_code': error.get('code')})
    return DevError(code, hint, **detail)
