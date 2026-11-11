"""Safe recovery descriptions, independent of task execution and authorization."""

from __future__ import annotations


def error_recovery(code, operation_id=None):
    code = str(code or "UNKNOWN")
    denied = code in {
        "INVALID_TOKEN",
        "INSUFFICIENT_SCOPE",
        "LOGIN_REQUIRED",
        "READ_ONLY",
        "EXECUTION_POLICY_BLOCKED",
        "ROOT_NOT_ALLOWED",
        "PROTECTED_PATH",
        "OWNER_REQUIRED",
        "AUTHORIZATION_CHANGED",
        "ACCOUNT_LOCKED",
        "TOKEN_EXPIRED",
        "GOAL_APPROVAL_CHANGED",
        "WORK_APPROVAL_CHANGED",
        "DELEGATION_APPROVAL_CHANGED",
    } or any(part in code for part in ("DENIED", "REVOKED", "DISABLED"))
    if denied:
        return {
            "category": "authorization",
            "next_action": "stop_and_review_permission",
            "retryable": False,
            "operation_id": operation_id,
        }
    if operation_id:
        return {
            "category": "operation_outcome",
            "next_action": "query_original_operation",
            "retryable": False,
            "operation_id": operation_id,
        }
    if code in {"INVALID_ARGUMENTS", "INVALID_ARGUMENT", "UNKNOWN_OPERATION", "INVALID_CURSOR"}:
        return {
            "category": "argument_rejected",
            "next_action": "correct_arguments",
            "execution_started": False,
            "retryable": False,
            "operation_id": None,
        }
    if code == "COMMAND_MISSING":
        return {
            "category": "command_missing",
            "next_action": "inspect_execution_environment",
            "retryable": False,
            "operation_id": None,
        }
    if code == "SHA_CONFLICT":
        return {
            "category": "file_changed",
            "next_action": "read_current_file_before_new_edit",
            "retryable": False,
            "operation_id": None,
        }
    return {
        "category": "unconfirmed",
        "next_action": "inspect_without_replay",
        "retryable": False,
        "operation_id": None,
    }


def operation_recovery(operation, *, online, after_output_seq=None):
    state = operation.get("state")
    pending = bool(operation.get("pending"))
    error = (operation.get("result") or {}).get("error") or {}
    code = error.get("code") if isinstance(error, dict) else None
    denied = error_recovery(code)["category"] == "authorization" if code else False
    if denied:
        action, message = (
            "stop_and_review_permission",
            "权限或明确拒绝已阻止操作；停止，不自动重试。",
        )
    elif pending:
        action = "query_original_operation"
        message = (
            "取消请求等待本机确认，尚未证明任务已停止。"
            if operation.get("cancel_requested")
            else "连接中断或状态未确认，任务可能仍在运行。查询原操作，不要另建请求。"
            if not online or state in {"reconnecting", "unknown"}
            else "原任务仍在进行，继续按原操作编号和输出游标读取。"
        )
    elif state in {"needs_review", "interrupted", "unknown"}:
        action, message = (
            "query_original_operation",
            "结果尚未确认；按原操作编号读取诊断，不要重放。",
        )
    else:
        action, message = (
            "inspect_terminal_result",
            "原操作已到终态；核对结果与退出码，不重放请求。",
        )
    if denied:
        category = "authorization"
    elif pending and (not online or state == "reconnecting"):
        category = "transport_interrupted"
    elif state in {"unknown", "needs_review", "interrupted"}:
        category = "outcome_unconfirmed"
    else:
        category = "pending" if pending else "terminal"
    return {
        "category": category,
        "connection_state": "connected" if online else "unavailable",
        "task_state": state,
        "task_outcome_confirmed": state in {"succeeded", "failed", "cancelled"},
        "next_action": action,
        "operation_id": operation.get("operation_id") or operation.get("id"),
        "after_output_seq": after_output_seq,
        "automatic_replay": False,
        "new_operation_recommended": False,
        "message": message,
    }
