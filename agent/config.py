"""Validate a complete config before swapping the live connection settings.

A half-written/invalid configuration must not stop the reconnection watcher.
Named tasks and full-access shell are explicit local-owner capabilities.
"""
from __future__ import annotations
import copy
from pathlib import Path
from shared.util import DevError, normalize_url, valid_json_value
from agent.shell import validate_shell
from agent.skills import validate_skills
from agent.computer import validate_computer
from shared.execution_policy import validate_policy
from agent.integration_config import validate_integrations


def validate_config(value: object, config_path: Path) -> dict:
    if not isinstance(value, dict) or not valid_json_value(value):
        raise ValueError("Agent 配置必须是 JSON 对象")
    c = copy.deepcopy(value)
    checkpoint_max_mib = c.get("checkpoint_max_mib", 100)
    if type(checkpoint_max_mib) is not int or not 1 <= checkpoint_max_mib <= 4096:
        raise ValueError("checkpoint_max_mib 必须是 1–4096 的整数")
    if not isinstance(c.get("hub_url"), str) or not c["hub_url"].strip():
        raise ValueError("hub_url 必须是面板 IP:端口或 HTTP(S) 根地址")
    c["hub_url"] = normalize_url(c["hub_url"])
    if not isinstance(c.get("device_id"), str) or not 1 <= len(c["device_id"]) <= 100 or any(not (x.isascii() and (x.isalnum() or x in "-_.")) for x in c["device_id"]):
        raise ValueError("device_id 无效；请保留配对文件中的设备身份")
    if not isinstance(c.get("secret"), str) or not 40 <= len(c["secret"]) <= 512:
        raise ValueError("Agent 配置缺少有效设备密钥，请先运行 init")
    if "name" in c and (not isinstance(c["name"], str) or len(c["name"]) > 200):
        raise ValueError("name 必须是长度不超过 200 的文字")
    state = c.get("state_dir", str(config_path.parent / "state"))
    if not isinstance(state, str) or not state.strip() or "\x00" in state:
        raise ValueError("state_dir 必须是本机目录")
    if not Path(state).expanduser().is_absolute():
        raise ValueError("state_dir 必须是绝对路径，避免启动目录改变日志与设备身份")
    c["state_dir"] = str(Path(state).expanduser().resolve())
    roots = c.get("allowed_roots", [])
    if not isinstance(roots, list):
        raise ValueError("allowed_roots 应是目录授权对象数组")
    for root in roots:
        if not isinstance(root, dict) or not isinstance(root.get("path"), str) or not root["path"].strip() or "\x00" in root["path"]:
            raise ValueError("每个 allowed_roots 条目必须包含有效 path")
        if not Path(root["path"]).expanduser().is_absolute():
            raise ValueError("本机目录授权 path 必须是绝对路径")
        for field in ("writable", "allow_tasks"):
            if field in root and type(root[field]) is not bool:
                raise ValueError(f"{field} 必须是 true/false，不能是字符串")
    c["allowed_roots"] = roots
    tasks = c.get("tasks", {})
    if not isinstance(tasks, dict):
        raise ValueError("tasks 应为具名任务对象")
    for name, task in tasks.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 100 or not isinstance(task, dict):
            raise ValueError("任务名称或任务配置无效")
        command = task.get("command")
        if not isinstance(command, list) or not command or not isinstance(command[0], str) or not command[0].strip() or any(not isinstance(x, str) or "\x00" in x for x in command):
            raise ValueError(f"任务 {name}: command 必须是非空命令参数数组")
        projects = task.get("projects", [])
        if not isinstance(projects, list) or any(not isinstance(p, str) or not p for p in projects):
            raise ValueError(f"任务 {name}: projects 必须是项目名称数组")
        timeout = task.get("timeout", 300)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError(f"任务 {name}: timeout 必须是 1–3600 秒的整数")
        if "allow_read_concurrency" in task and type(task["allow_read_concurrency"]) is not bool:
            raise ValueError(f"任务 {name}: allow_read_concurrency 必须是 true/false")
        if not isinstance(task.get("cwd", "."), str):
            raise ValueError(f"任务 {name}: cwd 必须是项目相对路径")
        from agent.filesystem import relative_path
        try:
            relative_path(task.get("cwd", "."))
        except DevError as exc:
            raise ValueError(f"任务 {name}: cwd 必须是允许访问的项目相对路径") from exc
        if not isinstance(task.get("description", ""), str):
            raise ValueError(f"任务 {name}: description 必须是文字")
        if "env" in task:
            if not isinstance(task["env"], dict) or any(not isinstance(k, str) or not k or "=" in k or "\x00" in k or not isinstance(v, str) or "\x00" in v for k, v in task["env"].items()):
                raise ValueError(f"任务 {name}: env 必须是文字键值对象")
    c["tasks"] = tasks
    c["shell"] = validate_shell(c.get("shell", {}))
    c["mcp_policy"] = validate_policy(c.get("mcp_policy", {}))
    c["skills"] = validate_skills(c.get("skills", {}))
    c["computer"] = validate_computer(c.get("computer", {}))
    c["integrations"] = validate_integrations(c.get("integrations", {}))
    return c
