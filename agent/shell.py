"""Locally opted-in shell execution; this is intentionally not an OS sandbox."""
from __future__ import annotations

import getpass
import os
import platform
import shutil
from pathlib import Path

from shared.util import DevError
from shared.execution_policy import POLICY_VERSION, agent_blocks_codex


def default_command():
    if os.name == "nt":
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    return [os.environ.get("SHELL") or "/bin/sh", "-lc"]


def validate_shell(value):
    if not isinstance(value, dict):
        raise ValueError("shell 必须是本机执行配置对象")
    if set(value) - {"enabled", "projects", "command", "inherit_env", "env", "max_timeout_seconds"}:
        raise ValueError("shell 包含未知配置字段")
    result = {"enabled": False, "projects": [], "command": default_command(),
              "inherit_env": True, "env": {}, "max_timeout_seconds": 86400, **value}
    for field in ("enabled", "inherit_env"):
        if type(result[field]) is not bool:
            raise ValueError(f"shell.{field} 必须是 true/false")
    projects = result["projects"]
    if not isinstance(projects, list) or any(not isinstance(p, str) or not p.strip() for p in projects):
        raise ValueError("shell.projects 必须是项目别名/ID 数组；所有项目使用 [\"*\"]")
    command = result["command"]
    if (not isinstance(command, list) or not command or
            any(not isinstance(x, str) or "\x00" in x for x in command) or not command[0].strip()):
        raise ValueError("shell.command 必须是 Shell 程序及选项的非空数组，远程命令作为最后一个参数传入")
    timeout = result["max_timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError("shell.max_timeout_seconds 必须是 1–86400 秒的整数")
    env = result["env"]
    if not isinstance(env, dict) or any(not isinstance(k, str) or not k or "=" in k or "\x00" in k or not isinstance(v, str) or "\x00" in v for k, v in env.items()):
        raise ValueError("shell.env 必须是有效的环境变量文字键值对象")
    return result


def shell_denial(config, project, spec):
    shell = config.get("shell", {})
    if project.get("mode") != "write" or not spec.get("writable", True):
        return "READ_ONLY", "项目或本机授权目录是只读的"
    if not project.get("allow_tasks") or not spec.get("allow_tasks"):
        return "TASKS_DISABLED", "面板项目与本机目录必须同时允许执行任务"
    if not shell.get("enabled", False):
        return "SHELL_DISABLED", "请在本机 Agent 配置中开启 shell.enabled 完整执行权限"
    allowed = shell.get("projects", [])
    if not ("*" in allowed or project.get("alias") in allowed or project.get("id") in allowed):
        return "SHELL_NOT_ALLOWED", "该项目未在本机 shell.projects 中授权"
    return None


def prepare_shell(config, project, spec, root, args):
    denial = shell_denial(config, project, spec)
    if denial:
        raise DevError(*denial, 403)
    shell = config["shell"]
    if args["timeout_seconds"] > shell["max_timeout_seconds"]:
        raise DevError("SHELL_TIMEOUT_LIMIT", f"请求超时超过本机上限 {shell['max_timeout_seconds']} 秒")
    try:
        cwd = Path(args["cwd"]).expanduser()
        cwd = (cwd if cwd.is_absolute() else root / cwd).resolve(strict=True)
        if not cwd.is_dir():
            raise DevError("INVALID_CWD", "Shell 工作目录必须是已存在的目录")
    except (OSError, RuntimeError, ValueError) as exc:
        raise DevError("INVALID_CWD", "无法访问 Shell 工作目录，请核对本机路径") from exc
    return shell["command"] + [args["command"]], cwd, {**shell["env"], **args["env"]}


def execution_info(config, project, spec, root):
    shell = config.get("shell") or validate_shell({})
    environment = dict(os.environ) if shell["inherit_env"] else {"PATH": os.environ.get("PATH", os.defpath)}
    environment.update(shell["env"])
    denial = shell_denial(config, project, spec)
    command = shell["command"]
    try:
        username = getpass.getuser()
        if hasattr(os, "geteuid"):
            import pwd
            username = pwd.getpwuid(os.geteuid()).pw_name
    except (OSError, KeyError):
        username = "unknown"
    return {
        "platform": platform.system(), "user": username,
        "uid": os.geteuid() if hasattr(os, "geteuid") else None,
        "project_root": str(root),
        "execution_policy": {"version": POLICY_VERSION,
                             "local_block_codex": config.get("mcp_policy", {}).get("block_local_codex", False),
                             "effective_block_codex": agent_blocks_codex(config, project),
                             "enforcement": "static_invocation_guard",
                             "os_sandbox": False},
        "shell": {"enabled": denial is None, "configured_enabled": shell["enabled"],
                  "denial": {"code": denial[0], "message": denial[1]} if denial else None,
                  "command": command, "executable_available": bool(shutil.which(command[0], path=environment.get("PATH"))),
                  "inherit_env": shell["inherit_env"], "environment_keys": sorted(shell["env"]),
                  "max_timeout_seconds": shell["max_timeout_seconds"], "interactive": False,
                  "filesystem_scope": "Agent OS user; absolute cwd and paths outside project allowed",
                  "next": "shell_exec" if denial is None else None},
        "tools": {name: shutil.which(name, path=environment.get("PATH")) for name in
                  ("git", "ssh", "gh", "docker", "python3", "uv", "node", "npm", "pnpm")},
        "tool_lookup": "Agent process PATH plus local shell.env; shell startup files may extend it",
        "credential_environment_present": {name: bool(environment.get(name)) for name in
                                           ("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "DOCKER_HOST")},
        "note": "Shell commands use the Agent user's OS permissions and existing local credentials. No automatic backup/rollback or interactive input. Project file tools retain their own path rules.",
    }
