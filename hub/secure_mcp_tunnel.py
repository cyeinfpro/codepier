"""Pure previews for the official Secure MCP Tunnel and the existing PAT bridge.

No filesystem, network, credential store, process or application state is inspected
or changed here. Passing validation proves only the supported input structure.
Official YAML schema: openai/tunnel-client v0.0.16 docs/configuration.md, checked
2026-10-10. Only documented fields are emitted; credentials remain references.
"""
from __future__ import annotations

import ipaddress
import json
from pathlib import PurePosixPath
import re
import shlex
from urllib.parse import urlsplit

from shared.util import normalize_url, valid_json_value

GUIDE_URL = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
CONFIG_URL = "https://github.com/openai/tunnel-client/blob/v0.0.16/docs/configuration.md"
RELEASE_URL = "https://github.com/openai/tunnel-client/releases/latest"
CHECKED_AT = "2026-10-10"
_ALLOWED = {"tunnel_id", "profile", "install_dir", "hub_url"}
_SECRET = re.compile(r"(?:\bsk-[A-Za-z0-9_-]{12,}|\brd_[A-Za-z0-9_-]{32,}|\bBearer\s+\S+)", re.I)

_WARNINGS = (
    ("PREVIEW_ONLY", "这里只校验输入并生成预览；没有读取文件、创建凭据、保存配置、联网或运行 Tunnel。"),
    ("PAT_SERVICE_IDENTITY", "stdio 桥接器对所有请求使用同一个已有 PAT，不会按 ChatGPT 用户自动切换 OAuth 身份；请核对 PAT 范围与 Tunnel 关联受众。"),
    ("PERMISSIONS_SEPARATE", "Tunnel 的平台权限、工作区关联、ChatGPT 使用权限、PAT 范围与 Agent 本机政策仍需分别核对。"),
    ("PRIVATE_DISTRIBUTION", "私有 Tunnel 不支持公开插件提交或分发；公开插件继续使用稳定 HTTPS 与既有 OAuth 路线。"),
    ("STDIO_SINGLE_INSTANCE", "同一个 tunnel_id 只能有一个活动 stdio 客户端，升级时也不能重叠运行；replicas=1 不等于避免滚动重叠。"),
    ("OAUTH_REACHABILITY", "Tunnel 不会自动穿透 OAuth 授权服务器；使用 OAuth 的其他接入路线仍需单独核对其可达性。"),
    ("RUNTIME_CREDENTIALS", "CONTROL_PLANE_API_KEY 是官方 Tunnel runtime key，与 CodePier PAT、模型 API 凭据、管理员密钥和 Agent 密钥分别管理；示例仅对子进程排除通用 OPENAI_API_KEY 回退。"),
    ("HOST_BOUNDARY", "安装目录属于运行 Tunnel 且能访问 Hub 的主机，不会从当前 Agent 项目或设备自动推断；此模板仅支持 POSIX Bash 桥接入口。"),
    ("CONFIG_PRECEDENCE", "tunnel-client 的环境变量和命令行可覆盖 YAML；实际生效配置需人工核对，预览不能证明运行配置一致。"),
)


def _issue(field: str, code: str, message: str) -> dict:
    return {"field": field, "code": code, "message": message}


def _text(value, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and valid_json_value(value)
        and all(char.isprintable() for char in value)
    )


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _command(identifier: str, title: str, args: list[str]) -> dict:
    return {
        "id": identifier, "title": title, "argv": args,
        "shell": shlex.join(args), "requires_manual_action": True,
    }


def _yaml(tunnel_id: str, bridge_command: str) -> str:
    # JSON string literals are YAML-compatible quoted scalars. No user input is
    # interpolated as YAML syntax, a mapping key, a tag or a shell fragment.
    tunnel_literal = json.dumps(tunnel_id, ensure_ascii=False)
    command_literal = json.dumps(bridge_command, ensure_ascii=False)
    return (
        "# Preview only. Review and save manually; contains no credential values.\n"
        "config_version: 1\n"
        "control_plane:\n"
        '  base_url: "https://api.openai.com"\n'
        f"  tunnel_id: {tunnel_literal}\n"
        '  api_key: "env:CONTROL_PLANE_API_KEY"\n'
        "health:\n"
        '  listen_addr: "127.0.0.1:0"\n'
        "admin_ui:\n"
        "  open_browser: false\n"
        "mcp:\n"
        "  commands:\n"
        '    - channel: "main"\n'
        f"      command: {command_literal}\n"
    )


def build_preview(payload) -> dict:
    """Return a deterministic, non-mutating preview; never accept secret values.

    Expected JSON object: tunnel_id, install_dir, optional profile and hub_url.
    'profile' is a safe label for the suggested file, not a profile creation.
    Paths refer to the operator's chosen host and are not read or resolved here.
    """
    result = {
        "schema_version": 1,
        "route": "tunnel_stdio",
        "mode": "preview_only",
        "configuration_valid": False,
        "verification_status": "not_run",
        "connection_state": "unknown",
        "auth_mode": "existing_pat",
        "effective_scope": "unverified",
        "errors": [],
        "warnings": [{"code": code, "message": message} for code, message in _WARNINGS],
        "artifacts": [],
        "commands": [],
        "manual_steps": [],
        "sources": [
            {"title": "官方 Secure MCP Tunnel 指南", "url": GUIDE_URL, "checked_at": CHECKED_AT},
            {"title": "官方配置 schema v0.0.16", "url": CONFIG_URL, "checked_at": CHECKED_AT},
            {"title": "官方最新客户端下载", "url": RELEASE_URL},
        ],
    }
    errors = result["errors"]
    if not isinstance(payload, dict):
        errors.append(_issue("", "OBJECT_REQUIRED", "请提交配置对象。"))
        return result
    if any(key not in _ALLOWED for key in payload):
        # Do not repeat unknown field names or values: they may be credentials.
        errors.append(_issue("", "UNSUPPORTED_FIELDS", "只接受 Tunnel 编号、配置名称、安装目录和 Hub 根地址；不要提交任何凭据。"))
    if any(isinstance(value, str) and _SECRET.search(value) for value in payload.values()):
        errors.append(_issue("", "SECRET_NOT_ACCEPTED", "此接口不接收凭据值。请仅在目标主机的安全流程中配置已有凭据。"))
        return result

    tunnel_id = payload.get("tunnel_id")
    profile = payload.get("profile", "codepier")
    install_dir = payload.get("install_dir")
    hub_url = payload.get("hub_url", "http://127.0.0.1:8765")
    if not _text(tunnel_id, 39) or re.fullmatch(r"tunnel_[a-f0-9]{32}", tunnel_id) is None:
        errors.append(_issue("tunnel_id", "INVALID_TUNNEL_ID", "Tunnel 编号应为 tunnel_ 加 32 位小写十六进制字符；语法通过不代表编号存在或已获授权。"))
    if not _text(profile, 64) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", profile) is None:
        errors.append(_issue("profile", "INVALID_PROFILE", "配置名称应以字母或数字开头，最多 64 字符，只含字母、数字、点、下划线和短横线。"))
    if (
        not _text(install_dir, 1024)
        or not install_dir.startswith("/")
        or install_dir.startswith("//")
        or "\\" in install_dir
        or any(part in {".", ".."} for part in install_dir.split("/"))
        or str(PurePosixPath(install_dir)) == "/"
    ):
        errors.append(_issue("install_dir", "INVALID_INSTALL_DIR", "请填写目标主机的 POSIX 绝对安装目录，不使用根目录、相对路径、反斜杠或点路径。"))
    else:
        install_dir = str(PurePosixPath(install_dir))

    normalized_url = None
    try:
        if not _text(hub_url, 2048) or not hub_url.startswith(("http://", "https://")):
            raise ValueError("explicit HTTP(S) scheme required")
        normalized_url = normalize_url(hub_url)
        parts = urlsplit(normalized_url)
        if parts.scheme == "http" and not _loopback(parts.hostname or ""):
            errors.append(_issue("hub_url", "HTTPS_REQUIRED", "明文 HTTP 仅允许明确的回环地址；其他 Hub 地址必须使用 HTTPS。"))
    except (ValueError, TypeError):
        errors.append(_issue("hub_url", "INVALID_HUB_URL", "请填写明确的 HTTP(S) Hub 根地址，不含账号、密码、路径、查询参数或片段。"))
    if errors:
        return result

    root = PurePosixPath(install_dir)
    bridge_path = str(root / "deploy" / "mcp-stdio.sh")
    pat_path = str(root / "private" / "token.txt")
    env_path = str(root / "private" / "bridge.env")
    config_name = f"codepier-tunnel-{profile}.yaml"
    config_path = str(root / "private" / config_name)
    bridge_command = shlex.join([bridge_path])
    bridge_env = (
        "# Preview only. Review manually; never put a PAT value in this file.\n"
        f"CODEPIER_HUB_URL={shlex.quote(normalized_url)}\n"
        f"CODEPIER_TOKEN_FILE={shlex.quote(pat_path)}\n"
    )
    result["configuration_valid"] = True
    result["artifacts"] = [
        {"name": "bridge.env", "media_type": "text/plain", "suggested_path": env_path,
         "content": bridge_env},
        {"name": config_name, "media_type": "application/yaml", "suggested_path": config_path,
         "content": _yaml(tunnel_id, bridge_command)},
    ]
    result["commands"] = [
        _command("help", "先核对已安装客户端的官方帮助", ["tunnel-client", "help", "quickstart"]),
        _command("sample", "核对官方 stdio 配置样例", ["tunnel-client", "profiles", "samples", "show", "sample_mcp_stdio_local"]),
        _command("doctor", "人工配置凭据和文件后检查；此处不会执行", ["env", "-u", "OPENAI_API_KEY", "tunnel-client", "doctor", "--config", config_path, "--explain"]),
        _command("run", "核对权限并确认后由操作者启动", ["env", "-u", "OPENAI_API_KEY", "tunnel-client", "run", "--config", config_path]),
    ]
    result["manual_steps"] = [
        {"id": "host", "title": "核对目标主机与已有桥接器",
         "detail": "检查安装路径、Bash、桥接器 Python 环境和到 Hub 的可达性；预览没有检查文件或安装依赖。"},
        {"id": "credentials", "title": "分别核对已有 PAT 与 Tunnel runtime key",
         "detail": "PAT 只由桥接器从私有 token.txt 读取；CONTROL_PLANE_API_KEY 只用于 Tunnel。不要粘贴到面板、命令参数、仓库或日志；此向导不创建或保存凭据。"},
        {"id": "authorization", "title": "核对平台、工作区与 PAT 授权",
         "detail": "检查 Tunnel 平台权限、目标工作区关联及 ChatGPT 使用权限；同时检查 PAT 受众和实际范围。语法校验不能证明任何一项已授权。"},
        {"id": "save", "title": "审阅配置后在目标主机手工保存",
         "detail": "先比较已有配置，不要覆盖未审阅文件。按 suggested_path 保存预览，核对私有目录与文件访问权限；此向导不会写入或改变权限。"},
        {"id": "validate", "title": "由操作者检查并启动单个实例",
         "detail": "使用已安装客户端的帮助核对参数；凭据和网络准备完成后人工执行 doctor，再确认 run。doctor 可能联网，不是本向导的离线校验。"},
        {"id": "chatgpt", "title": "在 ChatGPT 中完成真实连接验收",
         "detail": "在插件连接方式选择 Tunnel 并核对编号、认证与风险提示。先发现工具再做已授权只读调用；仅有配置或客户端健康状态不能证明 ChatGPT 已连接。"},
    ]
    return result
