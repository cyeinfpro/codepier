"""Managed Agent lifecycle preparation and post-ACK handoff."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from shared.agent_lifecycle import DEVICE_ACTIONS, MANAGEMENT_SCHEMA, MAX_AGENT_PACKAGE_BYTES
from shared.util import DevError, VERSION, atomic_json

LABEL = "com.liangchanghua.remote-dev-agent"
TASK = "RemoteDevAgent"
UNIT = "remote-dev-agent.service"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Agent package download redirected")


class LifecycleManager:
    def __init__(self, config_path: Path, state_dir: Path, config_getter):
        self.config_path = config_path.resolve()
        self.state_dir = state_dir.resolve()
        self.config_getter = config_getter
        self.lock = asyncio.Lock()
        self.handoff_pending = False
        self.plan_dir = self.state_dir / "lifecycle"
        self.runtime_root = Path(__file__).resolve().parent.parent
        candidate = self.runtime_root.parent
        self.base = candidate if self.runtime_root.name == "runtime" and self.config_path == candidate / "config.json" else None

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _metadata(self) -> dict:
        return self._read_json(self.base / "management.json") if self.base else {}

    def _service_kind(self) -> tuple[str, str]:
        metadata = self._metadata()
        kind = str(metadata.get("service_kind", ""))
        scope = str(metadata.get("service_scope", ""))
        if kind:
            return kind, scope
        if sys.platform == "darwin":
            return "launchd", "user"
        if sys.platform == "win32":
            return "schtasks", "user"
        if sys.platform.startswith("linux"):
            return "systemd", "system" if hasattr(os, "geteuid") and os.geteuid() == 0 else "user"
        return "", ""

    def _service_target(self) -> Path | None:
        if not self.base:
            return None
        kind, scope = self._service_kind()
        if kind == "launchd":
            return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if kind == "systemd":
            folder = Path("/etc/systemd/system") if scope == "system" else Path.home() / ".config/systemd/user"
            return folder / UNIT
        if kind == "schtasks":
            return self.base / "service.xml"
        return None

    def _external_python(self) -> Path | None:
        if not self.base:
            return None
        metadata = self._metadata()
        raw = metadata.get("helper_python")
        candidates = [Path(raw)] if isinstance(raw, str) and raw else []
        if os.name == "nt":
            candidates.extend(sorted((self.base / "python").glob("cpython-*/python.exe"), reverse=True))
        else:
            candidates.extend(sorted(
                (path for path in (self.base / "python").glob("cpython-*/bin/python3*")
                 if re.fullmatch(r"python3(?:\.\d+)*(?:t)?", path.name)),
                reverse=True,
            ))
        runtime = self.runtime_root.resolve()
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
                if (not candidate.name.endswith("-config") and not resolved.name.endswith("-config")
                        and resolved.is_file() and resolved != runtime and runtime not in resolved.parents):
                    return resolved
            except OSError:
                continue
        return None

    def _uv(self) -> Path | None:
        if not self.base:
            return None
        metadata = self._metadata()
        raw = metadata.get("uv")
        candidates = [Path(raw)] if isinstance(raw, str) and raw else []
        candidates.append(self.base / "tools" / ("uv.exe" if os.name == "nt" else "uv"))
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
                if resolved.is_file():
                    return resolved
            except OSError:
                continue
        return None

    def _handoff_ready(self) -> bool:
        kind, _ = self._service_kind()
        if kind == "systemd":
            return shutil.which("systemd-run") is not None
        if kind == "launchd":
            return shutil.which("launchctl") is not None
        if kind == "schtasks":
            return shutil.which("schtasks.exe") is not None
        return False

    def describe(self) -> dict:
        if not self.base:
            return {
                "schema": MANAGEMENT_SCHEMA,
                "managed": False,
                "service": False,
                "layout": "external",
                "installed_version": VERSION,
                "reason": "当前 Agent 不是由面板一键安装器管理",
            }
        metadata = self._metadata()
        target = self._service_target()
        service = bool(target and target.is_file())
        helper = (self.runtime_root / "scripts" / "agent_lifecycle.py").is_file()
        external_python = self._external_python() is not None
        uv = self._uv() is not None
        handoff = self._handoff_ready()
        kind, _ = self._service_kind()
        reason = ""
        if not service:
            reason = "未检测到受管自启动服务；请手动维护此 Agent"
        elif not helper or not external_python:
            reason = "当前安装缺少安全生命周期组件；请先重新执行一次安装命令"
        elif not handoff:
            reason = "系统缺少独立的服务交接工具，无法保证更新或卸载不中断"
        return {
            "schema": MANAGEMENT_SCHEMA,
            "managed": True,
            "service": service,
            "service_kind": kind,
            "layout": "managed-runtime",
            "installed_version": str(metadata.get("installed_version") or VERSION)[:80],
            "status": str(metadata.get("status", "ready"))[:40],
            "last_error": str(metadata.get("last_error", ""))[:500],
            "update_ready": bool(service and helper and external_python and uv and handoff),
            "control_ready": bool(service and helper and external_python and handoff),
            "reason": reason,
        }

    def actions(self) -> list[str]:
        status = self.describe()
        actions = []
        if status.get("control_ready"):
            actions.extend(("agent_restart", "agent_uninstall"))
        if status.get("update_ready"):
            actions.append("agent_update")
        return sorted(actions)

    def _plan_path(self, operation_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation_id):
            raise DevError("INVALID_REQUEST", "生命周期操作编号无效")
        return self.plan_dir / f"{operation_id}.json"

    def has_pending(self, operation_id: str) -> bool:
        try:
            return self._plan_path(operation_id).is_file()
        except DevError:
            return False

    @staticmethod
    def _extract(archive: Path, expected: str, destination: Path) -> None:
        raw = archive.read_bytes()
        if len(raw) > MAX_AGENT_PACKAGE_BYTES or hashlib.sha256(raw).hexdigest() != expected:
            raise DevError("AGENT_PACKAGE_CHANGED", "Agent 更新包校验失败，请重新发起更新")
        with zipfile.ZipFile(archive) as bundle:
            seen, total = set(), 0
            for item in bundle.infolist():
                path = PurePosixPath(item.filename)
                total += item.file_size
                mode = (item.external_attr >> 16) & 0o170000
                if (item.is_dir() or item.filename in seen or path.is_absolute() or ".." in path.parts
                        or "\\" in item.filename or ":" in item.filename or mode == 0o120000
                        or total > 32 * 1024 * 1024):
                    raise DevError("AGENT_PACKAGE_INVALID", "Agent 更新包结构不安全")
                allowed = len(path.parts) == 2 and path.parts[0] in {"agent", "shared"} and path.suffix == ".py"
                allowed |= item.filename in {"scripts/install_agent.py", "scripts/agent_lifecycle.py", "requirements-agent.txt"}
                allowed |= item.filename in {
                    "deploy/install-from-hub.sh", "deploy/install-from-hub.ps1", "deploy/install-agent.sh",
                    "deploy/install-agent.ps1", "deploy/start-agent.sh", "deploy/start-agent.cmd", "deploy/agent.service",
                }
                if not allowed:
                    raise DevError("AGENT_PACKAGE_INVALID", "Agent 更新包包含未知文件")
                seen.add(item.filename)
            required = {"agent/__main__.py", "shared/util.py", "requirements-agent.txt", "scripts/agent_lifecycle.py"}
            if not required <= seen:
                raise DevError("AGENT_PACKAGE_INVALID", "Agent 更新包不完整")
            for item in bundle.infolist():
                target = destination.joinpath(*PurePosixPath(item.filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(item))

    def _download(self, sha256: str, expected_bytes: int, target: Path) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise DevError("INVALID_REQUEST", "Agent 更新包校验值无效")
        if type(expected_bytes) is not int or not 1 <= expected_bytes <= MAX_AGENT_PACKAGE_BYTES:
            raise DevError("INVALID_REQUEST", "Agent 更新包大小无效")
        hub = str(self.config_getter().get("hub_url", "")).rstrip("/")
        request = urllib.request.Request(f"{hub}/agent/agent.zip?sha256={sha256}", method="GET")
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
                raw = response.read(MAX_AGENT_PACKAGE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise DevError("AGENT_DOWNLOAD_FAILED", "无法从当前面板下载 Agent 更新包") from exc
        if len(raw) != expected_bytes or len(raw) > MAX_AGENT_PACKAGE_BYTES or hashlib.sha256(raw).hexdigest() != sha256:
            raise DevError("AGENT_PACKAGE_CHANGED", "Agent 更新包大小或校验值不匹配，请重新发起更新")
        target.write_bytes(raw)

    @staticmethod
    def _run(command: list[object], *, cwd: Path | None = None, timeout: int = 600) -> None:
        try:
            result = subprocess.run([str(part) for part in command], cwd=cwd, timeout=timeout,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DevError("AGENT_UPDATE_PREPARE_FAILED", "准备 Agent 新版本失败") from exc
        if result.returncode:
            detail = (result.stdout or "")[-1200:].strip()
            raise DevError("AGENT_UPDATE_PREPARE_FAILED", "准备 Agent 新版本失败" + ("：" + detail if detail else ""))

    @staticmethod
    def _package_version(runtime: Path) -> str:
        text = (runtime / "shared" / "util.py").read_text(encoding="utf-8")
        match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)', text, re.M)
        return match.group(1) if match else ""

    def _copy_helper(self, operation_id: str) -> Path:
        source = self.runtime_root / "scripts" / "agent_lifecycle.py"
        if not source.is_file():
            raise DevError("AGENT_NOT_MANAGED", "当前安装缺少生命周期助手，请重新安装 Agent")
        folder = Path(tempfile.gettempdir()) / "remote-dev-agent-lifecycle"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        helper = folder / f"{operation_id}.py"
        helper.write_bytes(source.read_bytes())
        try:
            helper.chmod(0o700)
        except OSError:
            pass
        return helper

    def _write_plan(self, operation_id: str, plan: dict) -> None:
        self.plan_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json(self._plan_path(operation_id), plan)

    def _prepare_update(self, operation_id: str, args: dict, phase) -> dict:
        if not self.base:
            raise DevError("AGENT_NOT_MANAGED", "当前 Agent 不是受管安装")
        sha256 = args.get("package_sha256")
        package_bytes = args.get("package_bytes")
        target_version = args.get("target_version")
        if not isinstance(target_version, str) or not target_version or len(target_version) > 80:
            raise DevError("INVALID_REQUEST", "目标 Agent 版本无效")
        helper_python, uv = self._external_python(), self._uv()
        if not helper_python or not uv:
            raise DevError("AGENT_NOT_MANAGED", "当前安装缺少独立 Python 或 uv，无法安全更新")
        candidate = self.base / f".runtime-update-{operation_id}"
        archive = self.state_dir / f".agent-update-{operation_id}.zip"
        shutil.rmtree(candidate, ignore_errors=True)
        archive.unlink(missing_ok=True)
        try:
            phase("downloading_update", target_version=target_version)
            self._download(sha256, package_bytes, archive)
            candidate.mkdir(mode=0o700)
            self._extract(archive, sha256, candidate)
            if self._package_version(candidate) != target_version:
                raise DevError("AGENT_PACKAGE_CHANGED", "更新包版本与面板声明不一致")
            python = candidate / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            phase("preparing_update", target_version=target_version)
            self._run([uv, "venv", "--python", helper_python, candidate / ".venv"])
            self._run([uv, "pip", "install", "--python", python, "-r", candidate / "requirements-agent.txt"])
            self._run([python, "-c", "import agent.config, agent.runner, agent.lifecycle"], cwd=candidate, timeout=90)
            helper = self._copy_helper(operation_id)
            self._write_plan(operation_id, {
                "action": "agent_update", "helper": str(helper), "python": str(helper_python),
                "base": str(self.base), "candidate": str(candidate), "target_version": target_version,
                "created_at": time.time(),
            })
        except Exception:
            shutil.rmtree(candidate, ignore_errors=True)
            raise
        finally:
            archive.unlink(missing_ok=True)
        return {
            "action": "agent_update", "stage": "prepared", "target_version": target_version,
            "message": "新版本已完成校验，面板确认结果后将原子切换并重启；配置、状态和日志保持不变。",
        }

    def _prepare_control(self, operation_id: str, action: str) -> dict:
        if not self.base:
            raise DevError("AGENT_NOT_MANAGED", "当前 Agent 不是受管安装")
        helper_python = self._external_python()
        if not helper_python:
            raise DevError("AGENT_NOT_MANAGED", "当前安装缺少独立 Python，无法安全执行此操作")
        helper = self._copy_helper(operation_id)
        self._write_plan(operation_id, {
            "action": action, "helper": str(helper), "python": str(helper_python),
            "base": str(self.base), "created_at": time.time(),
        })
        if action == "agent_restart":
            return {"action": action, "stage": "prepared", "message": "重启已准备，将在面板确认结果后执行。"}
        return {
            "action": action, "stage": "prepared",
            "message": "卸载已准备，将在面板确认结果后移除服务、运行时、配置、状态和日志；面板节点记录会保留。",
        }

    def check_native_sessions(self):
        live = getattr(self, 'native_live', lambda: [])()
        if live:
            raise DevError('CLI_BUSY', '请先显式停止原生 CLI 会话再管理 Agent', 409,
                           sessions=[{'id': r['id'], 'project_id': r['project_id']} for r in live])

    async def prepare(self, operation_id: str, action: str, args: dict, phase=lambda *a, **k: None) -> dict:
        if action not in DEVICE_ACTIONS:
            raise DevError("UNKNOWN_TOOL", "未知 Agent 生命周期操作")
        if action not in self.actions():
            raise DevError("AGENT_NOT_MANAGED", self.describe().get("reason") or "当前 Agent 不支持一键生命周期管理")
        async with self.lock:
            self.check_native_sessions()
            if self.has_pending(operation_id):
                plan = self._read_json(self._plan_path(operation_id))
                return {"action": action, "stage": "prepared", "target_version": plan.get("target_version", ""),
                        "message": "生命周期操作已经准备完成。"}
            if action == "agent_update":
                return await asyncio.to_thread(self._prepare_update, operation_id, args, phase)
            return await asyncio.to_thread(self._prepare_control, operation_id, action)

    def _launch_helper(self, operation_id: str, command: list[str]) -> None:
        kind, scope = self._service_kind()
        unit = f"remote-dev-agent-lifecycle-{operation_id[:20]}"
        if kind == "systemd":
            runner = shutil.which("systemd-run")
            if not runner:
                raise OSError("systemd-run is unavailable")
            launch = [runner]
            if scope != "system":
                launch.append("--user")
            launch += ["--unit", unit, "--collect", "--quiet", "--property=Type=exec",
                       "--property=RuntimeMaxSec=15min", "--", *command]
            subprocess.run(launch, check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return
        if kind == "launchd":
            runner = shutil.which("launchctl")
            if not runner:
                raise OSError("launchctl is unavailable")
            label = "com.liangchanghua.remote-dev-agent.lifecycle." + operation_id[:20]
            import plistlib
            self.plan_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            definition = self.plan_dir / f"{operation_id}.plist"
            definition.write_bytes(plistlib.dumps({
                "Label": label, "ProgramArguments": [*command, "--handoff-label", label],
                "RunAtLoad": True, "KeepAlive": False,
            }))
            try:
                subprocess.run([runner, "bootstrap", f"gui/{os.getuid()}", str(definition)],
                               check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            finally:
                definition.unlink(missing_ok=True)
            return
        if kind == "schtasks":
            runner = shutil.which("schtasks.exe")
            if not runner:
                raise OSError("schtasks.exe is unavailable")
            import xml.etree.ElementTree as ET
            task_name = "RelayAgentLifecycle-" + operation_id[:20]
            command = [*command, "--handoff-task", task_name]
            ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
            ET.register_namespace("", ns)
            def child(parent, name, value=None):
                element = ET.SubElement(parent, "{" + ns + "}" + name)
                if value is not None:
                    element.text = value
                return element
            user = subprocess.check_output(["whoami.exe"], text=True).strip()
            task = ET.Element("{" + ns + "}Task", version="1.2")
            child(task, "Triggers")
            principal = child(child(task, "Principals"), "Principal")
            principal.set("id", "Author")
            child(principal, "UserId", user)
            child(principal, "LogonType", "InteractiveToken")
            child(principal, "RunLevel", "LeastPrivilege")
            settings = child(task, "Settings")
            child(settings, "MultipleInstancesPolicy", "IgnoreNew")
            child(settings, "DisallowStartIfOnBatteries", "false")
            child(settings, "StopIfGoingOnBatteries", "false")
            child(settings, "AllowStartOnDemand", "true")
            child(settings, "ExecutionTimeLimit", "PT15M")
            actions = child(task, "Actions")
            actions.set("Context", "Author")
            action = child(actions, "Exec")
            child(action, "Command", command[0])
            child(action, "Arguments", subprocess.list2cmdline(command[1:]))
            xml_path = self.plan_dir / f"{operation_id}.xml"
            ET.ElementTree(task).write(xml_path, encoding="utf-16", xml_declaration=True)
            try:
                subprocess.run([runner, "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"],
                               check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                subprocess.run([runner, "/Run", "/TN", task_name], check=True, timeout=30,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            finally:
                xml_path.unlink(missing_ok=True)
            return
        raise OSError("managed service handoff is unavailable")

    async def acknowledge(self, operation_id: str) -> bool:
        path = self._plan_path(operation_id)
        if not path.is_file():
            return False
        claimed = path.with_suffix(".claimed")
        async with self.lock:
            self.check_native_sessions()
            if not path.is_file():
                return False
            os.replace(path, claimed)
            try:
                plan = self._read_json(claimed)
                action = plan.get("action")
                command = [plan["python"], plan["helper"], "--install-dir", plan["base"],
                           "--wait-pid", str(os.getpid())]
                if action == "agent_update":
                    command += ["--apply-update", plan["candidate"]]
                elif action == "agent_restart":
                    command += ["--restart"]
                elif action == "agent_uninstall":
                    command += ["--uninstall"]
                else:
                    raise ValueError("unknown lifecycle plan")
                self.handoff_pending = True
                self._launch_helper(operation_id, [str(part) for part in command])
                claimed.unlink(missing_ok=True)
                return True
            except Exception:
                self.handoff_pending = False
                os.replace(claimed, path)
                raise
