"""Bounded, inference-free native discovery. Never return native connection config."""

from collections.abc import Callable, Mapping, Sequence, Set
import copy
import contextlib
import hashlib
import json
import os
import selectors
import signal
import subprocess
import threading
import time
from typing import Any

from shared.native_contracts import ActionAvailability, FeatureState, ProviderCapabilities

CAPABILITIES = dict(
    steer=True, compact=True, commands=True, stats=True, auto_compaction=False, auto_retry=False
)
COMMANDS = {
    "pi": {"refresh", "compact", "abort_retry", "set_auto_compaction", "set_auto_retry"},
    "codex": {"refresh", "compact"},
    "claude": {"refresh", "compact"},
}


def public_model(model):
    if not isinstance(model, dict):
        return model if isinstance(model, str) else None
    strings = {"id", "name", "displayName", "model", "provider", "defaultReasoningEffort"}
    out = {k: v[:2000] for k, v in model.items() if k in strings and isinstance(v, str)}
    for k in ("reasoning", "contextWindow", "maxTokens", "configured", "isDefault"):
        if type(model.get(k)) in (bool, int):
            out[k] = model[k]
    if isinstance(model.get("input"), list):
        out["input"] = [v for v in model["input"] if v in ("text", "image")]
    if isinstance(model.get("supportedReasoningEfforts"), list):
        out["supportedReasoningEfforts"] = [
            {
                k: v[:1000]
                for k, v in x.items()
                if k in ("reasoningEffort", "description") and isinstance(v, str)
            }
            for x in model["supportedReasoningEfforts"][:20]
            if isinstance(x, dict)
        ]
    return out


def commands(data, cli):
    rows = (
        data.get("commands", [])
        if cli != "codex"
        else [s for group in data.get("data", []) for s in group.get("skills", [])]
    )
    return [
        dict(
            name=str(x["name"])[:200],
            description=str(x.get("description", ""))[:2000],
            source=str(x.get("source", "native"))[:80] if cli != "codex" else "skill",
            **({"invocation": "$" + str(x["name"])[:200]} if cli == "codex" else {}),
        )
        for x in rows[:500]
        if isinstance(x, dict) and isinstance(x.get("name"), str)
    ]


def validate_settings(payload, cli):
    out = {}
    for key in ("model", "effort"):
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid native setting")
        if (
            key == "effort"
            and value
            and value
            not in (
                {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
                if cli == "pi"
                else {"low", "medium", "high", "xhigh", "max"}
                if cli == "claude"
                else {"none", "minimal", "low", "medium", "high", "xhigh"}
            )
        ):
            raise ValueError("Unsupported native effort")
        if (
            key == "model"
            and value
            and cli == "pi"
            and ("/" not in value or not all(value.split("/", 1)))
        ):
            raise ValueError("Pi model must be provider/id")
        out[key] = value
    return out


class Probe:
    def __init__(self, argv, cwd, env, timeout=8):
        self.deadline = time.monotonic() + timeout
        self.child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=0,
        )
        self.selector = selectors.DefaultSelector()
        os.set_blocking(self.child.stdout.fileno(), False)
        os.set_blocking(self.child.stdin.fileno(), False)
        self.selector.register(self.child.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.serial = 0
        self.total = 0

    def send(self, value):
        raw = (json.dumps(value) + "\n").encode()
        while raw:
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Native catalog probe timed out")
            try:
                raw = raw[os.write(self.child.stdin.fileno(), raw) :]
            except BlockingIOError:
                time.sleep(0.005)

    def call(self, method, params=None, pi=False, timeout=None, claude=False):
        deadline = (
            min(self.deadline, time.monotonic() + timeout) if timeout is not None else self.deadline
        )
        self.serial += 1
        key = str(self.serial)
        if claude:
            self.send(
                {
                    "type": "control_request",
                    "request_id": key,
                    "request": {"subtype": method, **(params or {})},
                }
            )
        else:
            self.send(
                {
                    "id": key,
                    **(
                        {"type": method, **(params or {})}
                        if pi
                        else {"method": method, "params": params or {}}
                    ),
                }
            )
        while time.monotonic() < deadline:
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer[:] = rest
                m = json.loads(line)
                if claude:
                    response = m.get("response") or {}
                    if m.get("type") != "control_response" or response.get("request_id") != key:
                        continue
                    if response.get("subtype") != "success":
                        raise ValueError(method + " unavailable in installed Claude version")
                    return response.get("response") or {}
                if m.get("id") != key:
                    continue
                if m.get("error") or pi and not m.get("success"):
                    raise ValueError(method + " unavailable in installed native version")
                return m.get("data" if pi else "result") or {}
            if not self.selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                continue
            raw = os.read(self.child.stdout.fileno(), 65536)
            if not raw:
                raise ValueError(
                    "Native CLI exited before catalog response; check local installation/login"
                )
            self.buffer.extend(raw)
            self.total += len(raw)
            if len(self.buffer) > 2 * 1024 * 1024 or self.total > 8 * 1024 * 1024:
                raise ValueError("Native catalog exceeds safe limit")
        raise TimeoutError("Native catalog probe timed out")

    def close(self):
        if self.child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.child.pid, signal.SIGTERM)
            try:
                self.child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=1)
        self.selector.close()
        self.child.stdin.close()
        self.child.stdout.close()


def probe(cli, executable, cwd, env, model="", timeout=8, include_commands=True):
    validate_settings({"model": model}, cli)
    if cli == "claude":
        from agent.claude_cli import probe as claude_probe

        return claude_probe(executable, cwd, env, model, timeout, include_commands)
    result = dict(
        cli=cli,
        cwd=str(cwd),
        models=[],
        model=None,
        thinking_levels=[],
        commands=[],
        warnings=[],
        capabilities=dict(CAPABILITIES),
    )
    if not include_commands:
        result.pop("commands")
    p = Probe(
        [executable, "--mode", "rpc", "--no-session"]
        if cli == "pi"
        else [executable, "app-server"],
        cwd,
        env,
        timeout,
    )

    def call(method, params=None, optional=False):
        try:
            return p.call(method, params, cli == "pi", timeout=1.5 if optional else None)
        except (ValueError, TimeoutError) as exc:
            if not optional:
                raise
            result["warnings"].append(str(exc))
            return {}

    try:
        if cli == "pi":
            result["warnings"].append(
                "Installed Pi auto-compaction/retry toggles persist globally; session-only toggles unavailable"
            )
            state = call("get_state")
            result.update({k: state[k] for k in ("thinkingLevel",) if k in state})
            result["model"] = public_model(state.get("model"))
            result["models"] = [
                public_model(x) for x in call("get_available_models").get("models", [])
            ]
            if model:
                provider, mid = model.split("/", 1)
                result["model"] = public_model(
                    call("set_model", {"provider": provider, "modelId": mid})
                )
                selected_state = call("get_state")
                if isinstance(selected_state.get("thinkingLevel"), str):
                    result["thinkingLevel"] = selected_state["thinkingLevel"]
            result["thinking_levels"] = [
                x
                for x in call("get_available_thinking_levels", optional=True).get("levels", [])
                if isinstance(x, str)
            ][:20]
            if include_commands:
                result["commands"] = commands(call("get_commands", optional=True), cli)
        else:
            call("initialize", {"clientInfo": {"name": "codepier-catalog", "version": "1.0.0"}})
            p.send({"method": "initialized", "params": {}})
            cursor = None
            for _ in range(20):
                page = call("model/list", {"cursor": cursor} if cursor else {})
                result["models"].extend(public_model(x) for x in page.get("data", []))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            else:
                result["warnings"].append("Native model catalog pagination limit reached")
            config = call(
                "config/read", {"cwd": str(cwd), "includeLayers": False}, optional=True
            ).get("config", {})
            current = config.get("model")
            if isinstance(current, str) and not any(
                current in (x.get("id"), x.get("model")) for x in result["models"]
            ):
                result["models"].append(
                    dict(
                        id=current,
                        model=current,
                        displayName=current + " (configured)",
                        configured=True,
                    )
                )
            current = current or next(
                (x.get("model") or x.get("id") for x in result["models"] if x.get("isDefault")),
                None,
            )
            selected = model or current
            result["model"] = next(
                (x for x in result["models"] if selected in (x.get("id"), x.get("model"))), selected
            )
            effort = config.get("model_reasoning_effort")
            result["reasoningEffort"] = effort if isinstance(effort, str) else None
            if isinstance(result["model"], dict):
                result["thinking_levels"] = [
                    x["reasoningEffort"]
                    for x in result["model"].get("supportedReasoningEfforts", [])
                    if "reasoningEffort" in x
                ]
            if include_commands:
                result["commands"] = commands(
                    call("skills/list", {"cwds": [str(cwd)]}, optional=True), cli
                )
        return result
    finally:
        p.close()


class CatalogCache:
    def __init__(self, ttl=86400, path=None):
        self.ttl = ttl
        self.path = path
        self.lock = threading.Lock()
        self.entries = {}
        self.pending = {}
        if path is not None:
            try:
                saved = json.loads(path.read_text())
                for key, row in list(saved.items())[:64]:
                    expires, value = row
                    if isinstance(value, dict) and isinstance(value.get("models"), list):
                        self.entries[key] = (
                            time.monotonic() + min(ttl, expires - time.time()),
                            value,
                        )
            except (OSError, ValueError, TypeError, AttributeError):
                self.entries.clear()

    @staticmethod
    def make_key(
        *,
        project_id: str,
        device_id: str,
        root: Any,
        cli: str,
        cwd: Any,
        model: str = "",
        include_commands: bool = True,
        executable: str = "",
        environment_key: str = "",
        provider_instance_id: str = "",
        instance_revision: int = 1,
    ) -> tuple[Any, ...]:
        """Build an instance- and directory-isolated catalog cache key."""
        return (
            project_id,
            device_id,
            str(root),
            cli,
            str(cwd),
            model,
            include_commands,
            executable,
            environment_key,
            provider_instance_id,
            instance_revision,
        )

    @staticmethod
    def _check_auth(authorize: Callable[[], None] | None) -> None:
        if authorize is not None:
            authorize()

    def _scope_key(
        self,
        key: Any,
        provider_instance_id: str | None,
        instance_revision: int | None,
    ) -> Any:
        if provider_instance_id is None and instance_revision is None:
            return key
        normalized = (
            json.dumps(key, sort_keys=True)
            if self.path is None and isinstance(key, Mapping)
            else key
        )
        return ("scoped", normalized, provider_instance_id, instance_revision)

    def get(
        self,
        key,
        loader,
        refresh=False,
        allow_stale=False,
        *,
        provider_instance_id: str | None = None,
        instance_revision: int | None = None,
        authorize: Callable[[], None] | None = None,
    ):
        self._check_auth(authorize)
        key = self._scope_key(key, provider_instance_id, instance_revision)
        if self.path is not None:
            key = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        with self.lock:
            entry = self.entries.get(key)
            if not refresh and entry and entry[0] > time.monotonic():
                return copy.deepcopy(entry[1])
            # Refresh bypasses a completed entry, but joins an existing probe.
            event = self.pending.get(key)
            if event is None:
                event = threading.Event()
                self.pending[key] = event
                owner = True
            else:
                owner = False
        if not owner:
            if not event.wait(12):
                raise TimeoutError("Native catalog coalesced probe timed out")
            if hasattr(event, "error"):
                raise ValueError(event.error)
            return copy.deepcopy(event.result)
        try:
            value = loader()
            event.result = value
            with self.lock:
                if len(self.entries) >= 64:
                    self.entries.pop(next(iter(self.entries)))
                self.entries[key] = (time.monotonic() + self.ttl, value)
                if self.path is not None:
                    from shared.util import atomic_json

                    # Cache persistence is best effort; a read-only/full disk must
                    # not turn a successful native discovery into a failed one.
                    with contextlib.suppress(OSError):
                        atomic_json(
                            self.path,
                            {
                                k: (time.time() + expires - time.monotonic(), data)
                                for k, (expires, data) in self.entries.items()
                            },
                        )
            return copy.deepcopy(value)
        except Exception as exc:
            if allow_stale and entry and entry[1].get("models"):
                value = copy.deepcopy(entry[1])
                value["catalog_stale"] = True
                value.setdefault("warnings", []).append(
                    "模型目录刷新失败，正在使用上次成功读取的缓存；可稍后手动刷新。"
                )
                event.result = value
                return copy.deepcopy(value)
            event.error = str(exc)
            raise
        finally:
            with self.lock:
                if self.pending.get(key) is event:
                    self.pending.pop(key)
                event.set()


_PROVIDER_OPTIONS_SCHEMAS: dict[str, dict[str, Any]] = {
    "pi": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "model": {"type": "string", "maxLength": 200},
            "effort": {
                "type": "string",
                "enum": ["off", "minimal", "low", "medium", "high", "xhigh", "max"],
            },
        },
    },
    "codex": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "model": {"type": "string", "maxLength": 200},
            "effort": {
                "type": "string",
                "enum": ["none", "minimal", "low", "medium", "high", "xhigh"],
            },
        },
    },
    "claude": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "model": {"type": "string", "maxLength": 200},
            "effort": {
                "type": "string",
                "enum": ["low", "medium", "high", "xhigh", "max"],
            },
        },
    },
}

_PROVIDER_CHANGE_MODES: dict[str, tuple[str, str, bool]] = {
    "pi": ("in_place", "in_place", True),
    "codex": ("next_turn", "next_turn", True),
    "claude": ("in_place", "unsupported", False),
}

_AVAILABILITY_ACTIONS: tuple[str, ...] = (
    "start_session",
    "resume_session",
    "send",
    "steer",
    "interrupt",
    "answer_approval",
    "change_model",
    "change_options",
    "fork_session",
    "rollback_session",
    "stop_session",
)

_EXECUTION_ACTIONS = frozenset(
    {
        "start_session",
        "resume_session",
        "send",
        "steer",
        "change_model",
        "change_options",
        "fork_session",
        "rollback_session",
    }
)


def describe_provider_capabilities(
    provider_kind: str,
    *,
    provider_instance_id: str,
    instance_revision: int = 1,
    provider_version: str = "unknown",
    platform_supported: bool = True,
    platform_reason: str | None = None,
) -> ProviderCapabilities:
    """Build normalized ProviderCapabilities for a verified provider instance."""
    if provider_kind not in _PROVIDER_CHANGE_MODES:
        raise ValueError(f"Unsupported provider_kind: {provider_kind}")
    model_change, effort_change, steer_supported = _PROVIDER_CHANGE_MODES[provider_kind]
    unavailable_reason = None if platform_supported else (platform_reason or "CLI_CHAT_PLATFORM")
    chat_feature = FeatureState(
        supported=True, available=platform_supported, reason_code=unavailable_reason
    )
    steer_feature = (
        FeatureState(supported=True, available=platform_supported, reason_code=unavailable_reason)
        if steer_supported
        else FeatureState(supported=False, available=False, reason_code="UNSUPPORTED_BY_PROVIDER")
    )
    unsupported = FeatureState(
        supported=False, available=False, reason_code="UNSUPPORTED_BY_PROVIDER"
    )
    return ProviderCapabilities(
        provider_instance_id=provider_instance_id,
        revision=instance_revision,
        provider_kind=provider_kind,
        provider_version=provider_version or "unknown",
        structured_chat=chat_feature,
        resume=chat_feature,
        steer=steer_feature,
        conversation_rollback=unsupported,
        conversation_fork=unsupported,
        context_import=chat_feature,
        model_change=model_change,
        effort_change=effort_change,
        options_schema=copy.deepcopy(_PROVIDER_OPTIONS_SCHEMAS[provider_kind]),
    )


def _action_supported(
    action: str, capabilities: ProviderCapabilities
) -> tuple[bool, str | None, bool, str | None]:
    """Return (supported, unsupported_reason, platform_available, platform_reason)."""
    feature_map = {
        "start_session": capabilities.structured_chat,
        "resume_session": capabilities.resume,
        "send": capabilities.structured_chat,
        "steer": capabilities.steer,
        "interrupt": capabilities.structured_chat,
        "answer_approval": capabilities.structured_chat,
        "fork_session": capabilities.conversation_fork,
        "rollback_session": capabilities.conversation_rollback,
    }
    feature = feature_map.get(action)
    if feature is not None:
        plat_ok = feature.available if feature.supported else capabilities.structured_chat.available
        plat_reason = (
            feature.reason_code if feature.supported else capabilities.structured_chat.reason_code
        )
        return (
            feature.supported,
            None if feature.supported else (feature.reason_code or "UNSUPPORTED_BY_PROVIDER"),
            plat_ok,
            None if plat_ok else (plat_reason or "CLI_CHAT_PLATFORM"),
        )
    if action == "change_model":
        ok = capabilities.model_change != "unsupported"
        return (
            ok,
            None if ok else "UNSUPPORTED_BY_PROVIDER",
            capabilities.structured_chat.available,
            capabilities.structured_chat.reason_code,
        )
    if action == "change_options":
        ok = capabilities.effort_change != "unsupported"
        return (
            ok,
            None if ok else "UNSUPPORTED_BY_PROVIDER",
            capabilities.structured_chat.available,
            capabilities.structured_chat.reason_code,
        )
    return True, None, True, None


def _scope_grants_action(
    authorized_scopes: Sequence[str] | Set[str] | Mapping[str, bool], action: str
) -> bool:
    if isinstance(authorized_scopes, Mapping):
        return bool(authorized_scopes.get(action, authorized_scopes.get("*", False)))
    scopes = set(authorized_scopes)
    if action in scopes or "*" in scopes:
        return True
    general = {"chat", "native_chat", "execute", "command_exec", "tasks"}
    if action in {"stop_session", "interrupt", "answer_approval"}:
        general = general | {"control", "session_control"}
    return bool(scopes & general)


def _action_authorized(
    action: str,
    authorized_scopes: Sequence[str] | Set[str] | Mapping[str, bool],
    allow_tasks: bool,
    shell_enabled: bool,
) -> tuple[bool, str | None]:
    if not _scope_grants_action(authorized_scopes, action):
        return False, "INSUFFICIENT_SCOPE"
    if action in _EXECUTION_ACTIONS:
        if not allow_tasks:
            return False, "TASKS_DISABLED"
        if not shell_enabled:
            return False, "SHELL_DISABLED"
    return True, None


def _action_ready(
    action: str,
    *,
    platform_available: bool,
    platform_reason: str | None,
    provider_executable_present: bool,
    device_enabled: bool,
    mode: str,
    session_status: str | None,
    has_active_run: bool,
    pending_approvals: int,
) -> tuple[bool, str | None, str]:
    if not device_enabled:
        return False, "DEVICE_DISABLED", "select_node"
    if not platform_available:
        reason = platform_reason or "CLI_CHAT_PLATFORM"
        return False, reason, "select_node" if reason == "CLI_CHAT_PLATFORM" else "update_agent"
    if mode != "chat":
        return False, "INVALID_SESSION_MODE", "choose_supported_option"
    state = _session_state_ready(action, session_status, has_active_run, pending_approvals)
    if (
        state[0]
        and action in {"start_session", "resume_session"}
        and not provider_executable_present
    ):
        return False, "CLI_MISSING", "select_node"
    return state


def _session_state_ready(
    action: str,
    session_status: str | None,
    has_active_run: bool,
    pending_approvals: int,
) -> tuple[bool, str | None, str]:
    live_statuses = {"starting", "running", "stopping", "orphaned"}
    if action == "start_session":
        if session_status in live_statuses:
            return False, "SESSION_ACTIVE", "resolve_current_run"
        return True, None, "none"
    if action == "resume_session":
        if session_status in live_statuses:
            return False, "SESSION_ACTIVE", "resolve_current_run"
        if session_status is None:
            return False, "SESSION_NOT_FOUND", "choose_supported_option"
        return True, None, "none"
    if action == "stop_session":
        return (
            (True, None, "none")
            if session_status in live_statuses
            else (False, "SESSION_NOT_RUNNING", "none")
        )
    if session_status not in {None, "starting", "running"}:
        return False, "SESSION_NOT_RUNNING", "reconnect"
    return _run_state_ready(action, has_active_run, pending_approvals)


def _run_state_ready(
    action: str, has_active_run: bool, pending_approvals: int
) -> tuple[bool, str | None, str]:
    if action in {"steer", "interrupt"}:
        return (True, None, "none") if has_active_run else (False, "NO_ACTIVE_RUN", "none")
    if action == "answer_approval":
        return (
            (True, None, "none")
            if pending_approvals > 0
            else (False, "NO_PENDING_APPROVAL", "none")
        )
    if pending_approvals > 0:
        return False, "PENDING_APPROVAL", "resolve_current_run"
    if action in {"change_model", "change_options"} and has_active_run:
        return False, "ACTIVE_RUN_IN_PROGRESS", "resolve_current_run"
    return True, None, "none"


def compute_action_availabilities(
    *,
    capabilities: ProviderCapabilities,
    authorized_scopes: Sequence[str] | Set[str] | Mapping[str, bool],
    device_enabled: bool,
    mode: str,
    allow_tasks: bool,
    shell_enabled: bool,
    session_status: str | None = None,
    has_active_run: bool = False,
    pending_approvals: int = 0,
    observed_revision: str = "1",
    provider_executable_present: bool = True,
) -> list[ActionAvailability]:
    """Compute independent supported, authorized, and ready states for native actions."""
    results: list[ActionAvailability] = []
    for action in _AVAILABILITY_ACTIONS:
        supported, unsupported_reason, plat_ok, plat_reason = _action_supported(
            action, capabilities
        )
        authorized, auth_reason = _action_authorized(
            action, authorized_scopes, allow_tasks, shell_enabled
        )
        ready, ready_reason, ready_remedy = _action_ready(
            action,
            platform_available=plat_ok,
            platform_reason=plat_reason,
            provider_executable_present=provider_executable_present,
            device_enabled=device_enabled,
            mode=mode,
            session_status=session_status,
            has_active_run=has_active_run,
            pending_approvals=pending_approvals,
        )
        reason_code, remedy = _pick_reason(
            supported,
            unsupported_reason,
            authorized,
            auth_reason,
            ready,
            ready_reason,
            ready_remedy,
        )
        results.append(
            ActionAvailability(
                action=action,
                supported=supported,
                authorized=authorized,
                ready=ready,
                reason_code=reason_code,
                remedy=remedy,
                observed_revision=observed_revision,
            )
        )
    return results


def _pick_reason(
    supported: bool,
    unsupported_reason: str | None,
    authorized: bool,
    auth_reason: str | None,
    ready: bool,
    ready_reason: str | None,
    ready_remedy: str,
) -> tuple[str | None, str]:
    if not supported:
        return unsupported_reason or "UNSUPPORTED_BY_PROVIDER", "choose_supported_option"
    if not authorized:
        return auth_reason or "INSUFFICIENT_SCOPE", "review_access"
    if not ready:
        return ready_reason or "NOT_READY", ready_remedy
    return None, "none"
