#!/usr/bin/env python3
"""Out-of-process service helper for managed Agent restart/update/uninstall.

The Agent copies this standard-library-only file to the OS temporary directory
before acknowledging a lifecycle operation. It therefore keeps running while the
installed runtime is replaced, and never imports code from that runtime.
"""
from __future__ import annotations

import argparse
import base64
import json
import plistlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

LABEL = "com.codepier.agent"
TASK = "CodePierAgent"
UNIT = "codepier-agent.service"

def managed_service_name(base, kind, scope):
    # Bootstrap runs before shared modules are installed. Keep this discovery
    # standard-library-only, and validate ownership before any service action.
    names = {'launchd': ('com.codepier.agent', 'com.liangchanghua.remote-dev-agent'),
             'systemd': ('codepier-agent.service', 'remote-dev-agent.service'),
             'schtasks': ('CodePierAgent', 'RemoteDevAgent')}.get(kind)
    if not names: return ''
    metadata_path = base/'management.json'
    metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.is_file() else {}
    name = metadata.get('service_name')
    if name:
        if name not in names: raise ValueError('Unrecognized managed service name')
        return name
    if kind == 'schtasks':
        target = base/'service.xml'
        if target.is_file():
            text = target.read_text(encoding='utf-16')
            return names[0] if 'CodePierAgent' in text or '.codepier-agent' in text else names[1]
        return names[0]
    folder = (Path.home()/'Library/LaunchAgents' if kind == 'launchd' else
              Path('/etc/systemd/system') if scope == 'system' else Path.home()/'.config/systemd/user')
    owned = []
    for name in names:
        target = folder/(name+'.plist' if kind == 'launchd' else name)
        if target.is_symlink(): raise ValueError('Managed service definition must not be a symlink')
        if not target.is_file(): continue
        if kind == 'launchd':
            value = plistlib.loads(target.read_bytes())
            matches = value.get('Label') == name and value.get('WorkingDirectory') == str(base/'runtime')
        else:
            matches = 'WorkingDirectory='+str(base/'runtime').replace('%','%%') in target.read_text(encoding='utf-8').splitlines()
        if matches: owned.append(name)
    if len(owned)>1: raise ValueError('Both legacy and CodePier services exist; inspect before updating')
    return owned[0] if owned else names[0]



def _service_name(base):
    return managed_service_name(base, *_service(base))


def _run(command, *, check=False, timeout=45):
    result = subprocess.run(
        [str(part) for part in command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError(f"service command failed ({result.returncode}): {command[0]}")
    return result.returncode


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".management-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(name, 0o600)
        except OSError:
            pass
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _management(base: Path) -> tuple[Path, dict]:
    path = base / "management.json"
    return path, _read_json(path)


def _update_management(base: Path, **patch) -> None:
    path, value = _management(base)
    value.update(patch)
    value["schema"] = 1
    value["updated_at"] = time.time()
    _atomic_json(path, value)


def _service(base: Path) -> tuple[str, str]:
    _, value = _management(base)
    kind = str(value.get("service_kind", ""))
    scope = str(value.get("service_scope", ""))
    if kind:
        return kind, scope
    if sys.platform == "darwin":
        return "launchd", "user"
    if sys.platform == "win32":
        return "schtasks", "user"
    if sys.platform.startswith("linux"):
        return "systemd", "system" if hasattr(os, "geteuid") and os.geteuid() == 0 else "user"
    return "", ""


def _systemctl(scope: str) -> list[str]:
    return ["systemctl"] if scope == "system" else ["systemctl", "--user"]


def _service_target(base: Path, kind: str, scope: str) -> Path | None:
    name = _service_name(base)
    if kind == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{name}.plist"
    if kind == "systemd":
        folder = Path("/etc/systemd/system") if scope == "system" else Path.home() / ".config/systemd/user"
        return folder / name
    if kind == "schtasks":
        return base / "service.xml"
    return None


def windows_task_script(name: str) -> str:
    escaped = name.replace("'", "''")
    return ("$ErrorActionPreference='Stop'; $s=New-Object -ComObject Schedule.Service; "
            "$s.Connect(); $t=$s.GetFolder('\\').GetTask('"+escaped+"'); ")


def set_windows_task_enabled(name: str, enabled: bool) -> None:
    # Modify the registered task's Enabled property without re-registering its
    # BootTrigger (which would require elevation in a noninteractive session).
    script = windows_task_script(name)+"$t.Enabled="+("$true" if enabled else "$false")
    _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], check=True)


def service_is_running(base: Path) -> bool:
    kind, scope = _service(base)
    name = _service_name(base)
    if kind == "launchd":
        # A missing service is harmless only if this user's launchd domain is accessible.
        _run(["launchctl", "print", f"gui/{os.getuid()}"], check=True)
        result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{name}"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
        return result.returncode == 0 and bool(re.search(r"\bpid = [1-9][0-9]*", result.stdout))
    if kind == "systemd":
        # is-active also returns false for 'deactivating'; inspect ActiveState instead.
        result = subprocess.run(_systemctl(scope) + ["show", "--property=ActiveState", "--value", name],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("cannot verify systemd service state; refusing to remove files")
        state = result.stdout.strip()
        if state not in {"inactive", "failed", "active", "activating", "deactivating", "reloading", "maintenance"}:
            raise RuntimeError("unknown systemd service state; refusing to remove files")
        return state not in {"inactive", "failed"}
    if kind == "schtasks":
        # State can be Disabled while a previously started instance still runs.
        # Inspect actual instances so maintenance waits for process termination.
        script = windows_task_script(name)+"if($t.GetInstances(0).Count -gt 0){exit 10}; exit 0"
        code = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
        if code not in (0, 10):
            raise RuntimeError("cannot verify scheduled task state; refusing to remove files")
        return code == 10
    raise RuntimeError("managed service metadata is unavailable")


def require_stopped(base: Path) -> None:
    for _ in range(50):
        if not service_is_running(base):
            return
        time.sleep(0.2)
    raise RuntimeError("Agent service did not stop; installed files were preserved")


def stop_service(base: Path, *, remove: bool = False) -> None:
    kind, scope = _service(base)
    name = _service_name(base)
    if kind == "launchd":
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{name}"])
        require_stopped(base)
        if remove:
            registered = subprocess.run(["launchctl", "print", f"{domain}/{name}"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if registered.returncode == 0:
                raise RuntimeError("Agent launchd service is still registered; installed files were preserved")
            target = _service_target(base, kind, scope)
            if target:
                target.unlink(missing_ok=True)
    elif kind == "systemd":
        command = _systemctl(scope)
        _run(command + (["disable", "--now", name] if remove else ["stop", name]), check=remove)
        require_stopped(base)
        if remove:
            target = _service_target(base, kind, scope)
            if target:
                target.unlink(missing_ok=True)
            _run(command + ["daemon-reload"])
    elif kind == "schtasks":
        # Prevent the periodic recovery trigger from racing an update/uninstall.
        set_windows_task_enabled(name, False)
        _run(["schtasks.exe", "/End", "/TN", name])
        require_stopped(base)
        if remove:
            _run(["schtasks.exe", "/Delete", "/TN", name, "/F"], check=True)
            target = _service_target(base, kind, scope)
            if target:
                target.unlink(missing_ok=True)
    else:
        raise RuntimeError("managed service metadata is unavailable")


def start_service(base: Path) -> None:
    kind, scope = _service(base)
    name = _service_name(base)
    if kind == "launchd":
        target = _service_target(base, kind, scope)
        if not target or not target.is_file():
            raise RuntimeError("launchd service definition is missing")
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootstrap", domain, target], check=True)
        _run(["launchctl", "kickstart", f"{domain}/{name}"], check=True)
    elif kind == "systemd":
        command = _systemctl(scope)
        _run(command + ["daemon-reload"], check=True)
        _run(command + ["start", name], check=True)
    elif kind == "schtasks":
        set_windows_task_enabled(name, True)
        _run(["schtasks.exe", "/Run", "/TN", name], check=True)
    else:
        raise RuntimeError("managed service metadata is unavailable")


def verify_service(base: Path) -> None:
    kind, scope = _service(base)
    time.sleep(2)
    name = _service_name(base)
    if kind == "launchd":
        result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{name}"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
        if result.returncode or "state = running" not in result.stdout:
            raise RuntimeError("Agent launchd service did not stay running")
    elif kind == "systemd":
        _run(_systemctl(scope) + ["is-active", "--quiet", name], check=True)
    elif kind == "schtasks":
        script = "if ((Get-ScheduledTask -TaskName '" + name + "').State -ne 'Running') { exit 1 }"
        _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], check=True)
    else:
        raise RuntimeError("managed service metadata is unavailable")


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _wait_for_exit(pid: int, seconds: float = 45) -> None:
    deadline = time.monotonic() + seconds
    while _process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _process_alive(pid):
        raise RuntimeError("Agent process did not stop before lifecycle change")


def _remove(path: Path) -> None:
    if not path.exists():
        return
    for attempt in range(30):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt == 29:
                raise
            time.sleep(0.25)


def _move(source: Path, destination: Path) -> None:
    for attempt in range(60):
        try:
            os.replace(source, destination)
            return
        except OSError:
            if attempt == 59:
                raise
            time.sleep(0.25)


def _version(runtime: Path) -> str:
    try:
        match = re.search(r'^VERSION\s*=\s*["\']([^"\']+)', (runtime / "shared" / "util.py").read_text(encoding="utf-8"), re.M)
        return match.group(1) if match else ""
    except OSError:
        return ""


def refresh_cli_commands(base: Path) -> None:
    # Remote panel updates must install the same local commands as CLI updates.
    # Only load the already verified and installed package, never a remote URL.
    import importlib.util
    source = base / "runtime/scripts/install_agent.py"
    if not source.is_file():
        return
    spec = importlib.util.spec_from_file_location("codepier_installed_commands", source)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load installed command generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    install = getattr(module, "install_cli_links", None)
    if callable(install):
        install(base)


def apply_update(base: Path, candidate: Path, pid: int) -> None:
    base = base.resolve()
    candidate = candidate.resolve()
    if candidate.parent != base or not candidate.name.startswith(".runtime-update-"):
        raise RuntimeError("invalid staged runtime location")
    for required in (candidate / "agent" / "__main__.py", candidate / "shared" / "util.py",
                     candidate / "scripts" / "agent_lifecycle.py"):
        if not required.is_file():
            raise RuntimeError("staged runtime is incomplete")
    runtime = base / "runtime"
    backup = base / ".runtime-previous"
    failed = base / ".runtime-failed"
    # Regenerate the wrapper from the verified candidate so old Windows installs
    # acquire the watchdog too. Older packages retain their original wrapper.
    wrapper = runtime / "run-service.py"
    if wrapper.is_file() and not (candidate / "run-service.py").exists():
        if (candidate / "agent/service_watchdog.py").is_file():
            import importlib.util
            spec = importlib.util.spec_from_file_location("codepier_candidate_installer", candidate / "scripts/install_agent.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.write_windows_wrapper(candidate)
        else:
            shutil.copyfile(wrapper, candidate / "run-service.py")
    previous_version = _version(runtime)
    old_stopped = False
    runtime_moved = False
    try:
        # Every action after requesting a stop belongs to the recovery boundary,
        # including backup cleanup and the first rename (not just candidate boot).
        stop_service(base)
        _wait_for_exit(pid)
        old_stopped = True
        _remove(backup)
        _remove(failed)
        _move(runtime, backup)
        runtime_moved = True
        _move(candidate, runtime)
        _update_management(base, managed=True, service=True, installed_version=_version(runtime),
                           previous_version=previous_version, status="starting", last_error="", brand_migration="", brand_migration_error="")
        start_service(base)
        verify_service(base)
        refresh_cli_commands(base)
        _update_management(base, status="ready", last_error="")
    except Exception as exc:
        detail = str(exc).strip()[:300]
        try:
            if runtime_moved:
                try:
                    stop_service(base)
                except Exception:
                    pass
                if runtime.exists():
                    _move(runtime, failed)
                if not backup.is_dir():
                    raise RuntimeError("previous Agent runtime is missing")
                _move(backup, runtime)
                start_service(base)
                verify_service(base)
            elif old_stopped:
                # The original runtime never moved, but its service is stopped.
                start_service(base)
                verify_service(base)
            else:
                # Stop/wait itself failed. An already-running launchd service
                # must not be bootstrapped a second time merely to recover it.
                if _service(base)[0] == "schtasks":
                    # A failed stop may have disabled recovery even while the
                    # old process is still alive. Restore its trigger as well.
                    start_service(base)
                try:
                    verify_service(base)
                except Exception:
                    start_service(base)
                    verify_service(base)
            # Only advertise rollback after the original service was verified.
            _update_management(base, installed_version=previous_version, status="rollback",
                               last_error="更新失败，已恢复上一版本" + ("：" + detail if detail else ""))
        except Exception as recovery_error:
            recovery_detail = str(recovery_error).strip()[:200]
            message = "更新失败，自动恢复未完成：" + detail + "；" + recovery_detail
            try:
                _update_management(base, status="error", last_error=message[:500])
            except Exception:
                pass
            raise RuntimeError(message) from exc
        raise


def restart(base: Path, pid: int) -> None:
    try:
        stop_service(base)
        _wait_for_exit(pid)
    except Exception:
        if _service(base)[0] == "schtasks":
            start_service(base)
        raise
    _update_management(base, status="restarting", last_error="")
    start_service(base)
    verify_service(base)
    _update_management(base, status="ready", last_error="")


def _schedule_windows_removal(base: Path) -> None:
    escaped = str(base).replace("'", "''")
    script = (
        "$p='" + escaped + "'; Start-Sleep -Seconds 2; "
        "for($i=0;$i -lt 60;$i++){Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue; "
        "if(-not (Test-Path -LiteralPath $p)){exit 0}; Start-Sleep -Milliseconds 500}; exit 1"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def uninstall(base: Path, pid: int) -> None:
    # The helper is also called after remote ACK: never permit arbitrary recursive deletion.
    home = Path.home().resolve()
    if (not base.is_absolute() or base.is_symlink() or len(base.parts) < 3
            or base.resolve() == home or base.resolve() in home.parents
            or (base / "config.json").is_symlink() or (base / "runtime").is_symlink()):
        raise RuntimeError("unsafe Agent uninstall directory")
    base = base.resolve()
    config = _read_json(base / "config.json")
    if not config.get("device_id") or not (base / "runtime/agent/__main__.py").is_file():
        raise RuntimeError("unrecognized Agent installation; refusing to remove files")
    for root in config.get("allowed_roots", []):
        raw = root.get("path") if isinstance(root, dict) else root
        if isinstance(raw, str) and Path(raw).expanduser().resolve().is_relative_to(base):
            raise RuntimeError("authorized project directory is inside installation; refusing to remove files")
    state=Path(config.get('state_dir') or base/'state').expanduser()
    receipt=state/'browser-bridge/install-receipt.json'
    if receipt.is_file():
        python=base/'runtime/.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        # Preflight receipt ownership before removing the service or credentials.
        subprocess.run([str(python),'-m','agent.install_browser_bridge','--config',str(base/'config.json'),'--uninstall'],
                       cwd=base/'runtime',check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=60)
    stop_service(base, remove=True)
    _wait_for_exit(pid)
    if receipt.is_file():
        subprocess.run([str(python),'-m','agent.install_browser_bridge','--config',str(base/'config.json'),'--uninstall','--apply'],
                       cwd=base/'runtime',check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=60)
    if base.name=='.codepier-agent':
        old=base.with_name('.remote-dev-agent')
        if (old.is_symlink() or getattr(old,'is_junction',lambda:False)()) and old.resolve()==base:
            if getattr(old,'is_junction',lambda:False)():old.rmdir()
            else:old.unlink()
    if os.name == "nt":
        _schedule_windows_removal(base)
    else:
        _remove(base)


def record_failure(base: Path, exc: Exception) -> None:
    current = _read_json(base / "management.json")
    brand_record = _read_json(base / '.codepier-migration.json')
    if current.get('brand_migration') == 'rollback' and brand_record.get('stage') == 'rolled_back':
        _update_management(base, status='rollback', last_error='CodePier 命名迁移失败，已验证恢复原安装')
        return
    if current.get("status") == "rollback":
        # apply_update already restored and verified the previous service. Keep
        # that stronger state instead of obscuring it with a generic error.
        message = str(current.get("last_error") or "更新失败，已恢复上一版本")[:500]
        _update_management(base, status="rollback", last_error=message)
        return
    _update_management(base, status="error", last_error=str(exc)[:500])


def migrate_brand(base, pid):
    # Load verified source into memory before moving the installation.
    import importlib.util
    from types import SimpleNamespace
    loaded = {}
    for name in ('brand_migration', 'brand_browser'):
        source = base / 'runtime/shared' / (name + '.py')
        spec = importlib.util.spec_from_file_location('codepier_' + name, source)
        if not spec or not spec.loader:
            raise RuntimeError('Missing brand migration component')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded[name] = module
    backend = SimpleNamespace(**globals())
    backend.brand_browser = loaded['brand_browser']
    return loaded['brand_migration'].migrate_agent(base, pid, backend)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--wait-pid", type=int, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply-update")
    group.add_argument("--restart", action="store_true")
    group.add_argument("--migrate-brand", action="store_true")
    group.add_argument("--uninstall", action="store_true")
    parser.add_argument("--handoff-task", default="")
    parser.add_argument("--handoff-label", default="")
    args = parser.parse_args()
    base = Path(args.install_dir).expanduser()
    if not args.uninstall:
        base = base.resolve()
    time.sleep(0.5)
    try:
        if args.migrate_brand:
            print(json.dumps(migrate_brand(base, args.wait_pid), ensure_ascii=False))
        elif args.apply_update:
            apply_update(base, Path(args.apply_update), args.wait_pid)
        elif args.restart:
            restart(base, args.wait_pid)
        else:
            uninstall(base, args.wait_pid)
    except Exception as exc:
        if base.exists() and not args.uninstall and not args.migrate_brand:
            try:
                record_failure(base, exc)
            except Exception:
                pass
        raise
    finally:
        if args.handoff_task:
            _run(["schtasks.exe", "/Delete", "/TN", args.handoff_task, "/F"])
        # One-shot launchd jobs have KeepAlive disabled and do not rerun on exit;
        # an explicit self-removal can terminate the helper before final writes flush.


if __name__ == "__main__":
    main()
