"""Use the Agent's OpenSSH trust files without disabling host verification.

New keys are persisted only after SSH proves possession, immediately before
authentication releases a password. A second concurrent first connection must
agree with the key committed by the first. No private key is read here.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import glob
import hmac
import os
from pathlib import Path
import shlex
import stat
import threading

from shared.ssh_host_pin import CONFIRM_CHANGE, validate_optional_host_pin


class HostTrustError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


_LOCK = threading.RLock()
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_CONFIG_FILES = 32
_TRUST_DIRECTIVES = frozenset(
    {
        "userknownhostsfile",
        "globalknownhostsfile",
        "hostkeyalias",
        "knownhostscommand",
        "verifyhostkeydns",
        "revokedhostkeys",
        "checkhostip",
    }
)


def default_sources(home: Path | None = None) -> tuple[Path, ...]:
    home = home or Path.home()
    if os.name == "nt":
        system = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "ssh"
    else:
        system = Path("/etc/ssh")
    return (
        home / ".ssh/known_hosts",
        home / ".ssh/known_hosts2",
        system / "ssh_known_hosts",
        system / "ssh_known_hosts2",
    )


def _trust_config_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise HostTrustError("HOST_TRUST_CONFIGURATION", "无法读取 Agent 的 SSH 信任配置") from exc


def _include_arguments(words):
    arguments = words[1:]
    if "=" in words[0]:
        value = words[0].split("=", 1)[1]
        arguments = ([value] if value else []) + arguments
    if arguments and arguments[0].startswith("="):
        value = arguments[0][1:]
        arguments = ([value] if value else []) + arguments[1:]
    if not arguments:
        raise HostTrustError("HOST_TRUST_CONFIGURATION", "SSH Include 配置缺少路径")
    return arguments


def _include_paths(words, base):
    for pattern in _include_arguments(words):
        if not pattern or "%" in pattern or "$" in pattern:
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "SSH Include 路径需要单独核验")
        expanded = Path(pattern).expanduser()
        if not expanded.is_absolute():
            expanded = base / expanded
        yield from ((Path(item), base) for item in glob.glob(str(expanded)))


def _trust_config_includes(lines, base):
    for line in lines:
        try:
            words = shlex.split(line, comments=True)
        except ValueError as exc:
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "SSH 配置语法无法安全解析") from exc
        if not words:
            continue
        directive = words[0].split("=", 1)[0].lower()
        if directive in _TRUST_DIRECTIVES:
            raise HostTrustError(
                "HOST_TRUST_CONFIGURATION",
                "Agent 使用自定义 SSH 信任设置；请先配置并核验终端的共享信任来源",
            )
        if directive == "include":
            yield from _include_paths(words, base)


def check_native_trust_configuration(home: Path | None = None) -> None:
    """Fail closed when default files cannot represent the native SSH policy.

    Includes are inspected as data, never evaluated using ``ssh -G`` (which can
    run Match exec). Explicit Agent-owned sources can bypass this default-source
    check after their configuration has been reviewed by the operator.
    """
    home = home or Path.home()
    system = (
        Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "ssh"
        if os.name == "nt"
        else Path("/etc/ssh")
    )
    pending = [(home / ".ssh/config", home / ".ssh"), (system / "ssh_config", system)]
    seen: set[Path] = set()
    while pending:
        path, base = pending.pop()
        if path in seen or not path.exists():
            continue
        seen.add(path)
        if len(seen) > _MAX_CONFIG_FILES or path.stat().st_size > _MAX_FILE_BYTES:
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "SSH 信任配置超出可核验范围")
        pending.extend(_trust_config_includes(_trust_config_lines(path), base))


def _read(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise HostTrustError("HOST_TRUST_UNAVAILABLE", "无法读取 Agent 的可信主机记录") from exc
    if len(data) > _MAX_FILE_BYTES:
        raise HostTrustError("HOST_TRUST_CONFIGURATION", "可信主机记录超过大小上限")
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise HostTrustError("HOST_TRUST_CONFIGURATION", "可信主机记录编码无效") from exc


def _without_bare_ed25519_keys(text: str, ssh) -> str:
    """Exclude only canonical public-key rows which have no host field.

    A standalone Ed25519 public key cannot express a host pin, a CA, or a
    revocation. Do not give it a host or relax validation for any other row:
    a malformed host-scoped record could otherwise become eligible for TOFU.
    """
    kept = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "ssh-ed25519" and len(parts[1]) == 68:
            try:
                key = ssh.import_public_key(" ".join(parts))
                if key.export_public_key("openssh").decode("ascii").split() == parts:
                    continue
            except (ValueError, TypeError):
                pass  # The strict validator below rejects invalid bare rows.
        kept.append(line)
    return "\n".join(kept)


def _validate_lines(text: str) -> None:
    # AsyncSSH ignores keys of algorithms it cannot import. Do not turn an
    # existing but unsupported host entry into an unknown host eligible for TOFU.
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0].startswith("@"):
            if parts.pop(0) not in {"@cert-authority", "@revoked"}:
                raise HostTrustError("HOST_TRUST_CONFIGURATION", "可信主机记录含不支持的标记")
        if len(parts) < 3:
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "可信主机记录格式无效")
        try:
            base64.b64decode(parts[2], validate=True)
        except (ValueError, TypeError) as exc:
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "可信主机公钥格式无效") from exc


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "信任锁文件类型无效")
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise HostTrustError("HOST_TRUST_BUSY", "主机信任记录正在更新，请重试") from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise HostTrustError("HOST_TRUST_BUSY", "主机信任记录正在更新，请重试") from exc
        yield
    finally:
        os.close(fd)


class HostKeyPolicy:
    """A connection-local validator backed by shared OpenSSH known_hosts."""

    def __init__(
        self, ssh, policy: str, *, sources=None, write_path=None, home=None, expected_fingerprint=""
    ):
        if policy not in {"strict", "accept-new", CONFIRM_CHANGE}:
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "未知主机密钥政策")
        try:
            self.expected_fingerprint = validate_optional_host_pin(policy, expected_fingerprint)
        except ValueError as exc:
            raise HostTrustError("HOST_TRUST_CONFIGURATION", str(exc)) from None
        self.ssh, self.policy = ssh, policy
        if sources is None:
            check_native_trust_configuration(home)
            sources = default_sources(home)
        self.sources = tuple(Path(p).expanduser() for p in sources)
        if not self.sources or any(not path.is_absolute() for path in self.sources):
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "信任来源必须为 Agent 本机绝对路径")
        self.write_path = Path(write_path).expanduser() if write_path else self.sources[0]
        if self.write_path not in self.sources:
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "首次信任写入路径必须属于共享信任来源")
        self.pending = None
        self.error: HostTrustError | None = None
        self.fingerprint = None
        self._loaded = self._load()

    def _load(self):
        text = _without_bare_ed25519_keys("\n".join(_read(path) for path in self.sources), self.ssh)
        _validate_lines(text)
        # Validate every key, rather than silently dropping unsupported keys.
        for line in text.splitlines():
            parts = line.strip().split()
            if not parts or parts[0].startswith("#"):
                continue
            if parts[0].startswith("@"):
                parts = parts[1:]
            try:
                self.ssh.import_public_key(" ".join(parts[1:3]))
            except Exception as exc:
                raise HostTrustError(
                    "HOST_TRUST_CONFIGURATION", "可信主机记录含当前 SSH 引擎不支持的公钥"
                ) from exc
        try:
            return self.ssh.import_known_hosts(text)
        except Exception as exc:
            raise HostTrustError("HOST_TRUST_CONFIGURATION", "可信主机记录无法解析") from exc

    def matched(self, host: str, address: str, port: int, *, reload=False):
        port = port or 22
        if reload:
            self._loaded = self._load()
        # AsyncSSH's default non-22-port fallback is broader than native
        # OpenSSH. Match explicit bracketed names without that fallback.
        host_key = host if port == 22 else f"[{host}]:{port}"
        # Native OpenSSH's default CheckHostIP=no matches the requested host,
        # not an arbitrary DNS result. Custom CheckHostIP directives were
        # rejected during preflight. Empty addr also prevents AsyncSSH from
        # trying to parse a bracketed [IP]:port as an IP address.
        return self._loaded.match(host_key, "", None)

    def known_hosts(self, host, address, port):
        matches = self.matched(host, address, port)
        if self.expected_fingerprint:
            # No known key, certificate authority, or X.509 trust may bypass
            # validate_candidate; retain both public-key and certificate revocations.
            return [], [], matches[2], [], [], [], matches[6]
        return matches

    def validate_candidate(self, host, address, port, key) -> bool:
        port = port or 22
        try:
            matches = self.matched(host, address, port, reload=True)
            if key in matches[2]:
                raise HostTrustError("HOST_KEY_REVOKED", "主机密钥已撤销")
            if self.expected_fingerprint:
                presented = key.get_fingerprint()
                if not hmac.compare_digest(self.expected_fingerprint, presented):
                    raise HostTrustError(
                        "HOST_KEY_CHANGED",
                        "主机指纹不匹配；请在 VPS 云控制台核验，并更新保存的指纹",
                    )
                self.fingerprint = presented
                return True
            if key in matches[0]:
                self.fingerprint = key.get_fingerprint()
                return True
            if any(matches):
                raise HostTrustError("HOST_KEY_CHANGED", "主机密钥与已有可信记录不一致")
            if self.policy == "strict":
                raise HostTrustError("HOST_KEY_UNKNOWN", "主机尚无可信记录，请先核验指纹")
            self.pending = (host, address, port, key)
            self.fingerprint = key.get_fingerprint()
            return True
        except HostTrustError as exc:
            self.error = exc
            return False

    def commit_before_authentication(self) -> None:
        if self.expected_fingerprint and not self.fingerprint:
            raise HostTrustError("HOST_KEY_CHANGED", "尚未确认已保存的主机指纹")
        if self.pending is None:
            return
        host, address, port, key = self.pending
        with _LOCK, _file_lock(self.write_path.with_name(self.write_path.name + ".codepier.lock")):
            matches = self.matched(host, address, port, reload=True)
            if key in matches[2] or (any(matches) and key not in matches[0]):
                raise HostTrustError("HOST_KEY_CHANGED", "首次连接期间主机信任记录已变化")
            if key not in matches[0]:
                exported = key.export_public_key("openssh").decode("ascii").strip()
                label = host if port == 22 else f"[{host}]:{port}"
                old = _read(self.write_path)
                line = (
                    ("\n" if old and not old.endswith("\n") else "")
                    + f"{label} {exported} CodePier-Web-SSH\n"
                ).encode("utf-8")
                if len(old.encode("utf-8")) + len(line) > _MAX_FILE_BYTES:
                    raise HostTrustError("HOST_TRUST_CONFIGURATION", "可信主机记录已达到大小上限")
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(self.write_path, flags, 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise HostTrustError("HOST_TRUST_CONFIGURATION", "可信主机记录不是普通文件")
                    # Append one complete record. Never replace the file and
                    # lose another OpenSSH process's unrelated appended keys.
                    if os.write(fd, line) != len(line):
                        raise HostTrustError("HOST_TRUST_UNAVAILABLE", "主机信任记录未完整写入")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self.pending = None
            self._loaded = self._load()
