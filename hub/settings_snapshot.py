"""Read-only, allowlisted projections of existing settings and node readiness."""
from __future__ import annotations

import os
import time

from hub.access import access_defaults
from hub.file_import_settings import snapshot as file_import_snapshot
from shared.settings_catalog import settings_catalog
from shared.util import DevError


def selected_project(runtime, principal, identifier):
    if not identifier:
        return None
    # Equal response for a missing or inaccessible selection; never expose paths.
    visible = {row["id"]: row for row in runtime.list_projects(principal)}
    if identifier not in visible:
        raise DevError("SETTINGS_SCOPE_NOT_FOUND", "所选项目不存在或当前账号无权查看", 404)
    return runtime.project(identifier, principal)


def snapshot(context, principal, project_id=""):
    store, runtime, config = context.store, context.runtime, context.config
    project = selected_project(runtime, principal, project_id)
    catalog = settings_catalog()
    rows = {row["id"]: row for row in catalog["items"]}
    for row in rows.values():
        row.update(effective_value=None, configured_value=None, source=row["source_kind"],
                   state="entry" if row["entry"] else "unknown", can_edit=False,
                   editable_here=False, checked_at=time.time())
        if row["source_kind"] == "agent_config":
            row["state"] = "not_checked" if project else "select_project"
        row["edit_mode"] = ("local" if row["source_kind"] == "agent_config"
                            else "deployment" if row["source_kind"] == "environment"
                            else "security_entry" if row["risk"] == "security" else "entry")
        if row["entry"] == "identity-admin" and not principal.instance_admin:
            row["entry"] = ""
            row["state"] = "restricted"
        if row["entry"] == "members" and not principal.admin:
            row["entry"] = "identity"

    def value(identifier, effective, source, *, configured=None, state="known"):
        rows[identifier].update(effective_value=effective, configured_value=configured,
                                source=source, state=state)

    row = store.one("SELECT value FROM meta WHERE key='public_url'")
    value("public_url", context.public_url(), "database" if row else
          "environment" if os.getenv("MCP_PUBLIC_URL") or os.getenv("HUB_PUBLIC_URL") else "default",
          configured=row["value"] if row else None)
    defaults_row = store.one("SELECT value FROM meta WHERE key=?", ("mcp_access_defaults:" + principal.user_id,))
    defaults = access_defaults(store, principal.user_id)
    value("access_defaults", defaults, "database" if defaults_row else "default",
          configured=defaults if defaults_row else None)
    for key in ("appearance", "access_defaults", "public_url"):
        editable = key != "public_url" or principal.instance_admin
        rows[key].update(can_edit=editable, editable_here=editable,
                         edit_mode="inline" if editable else "restricted")
    value("appearance", None, "browser_storage", state="browser")
    for key in ("hub_ingress", "native_relay", "hub_file_sources"):
        rows[key]["owner"] = "实例管理员（显式覆盖）/ 部署管理员（继承值）"
    ingress = file_import_snapshot(store)
    for key, field in (("hub_ingress", "streaming_enabled"), ("native_relay", "native_relay_enabled")):
        setting = ingress["settings"][field]
        value(key, setting["effective_value"], setting["source"],
              configured=setting["configured_value"], state=setting["state"])
        rows[key].update({name: setting[name] for name in
                          ("activation", "inherited_value", "inherited_source", "inherited_state")})
        if principal.instance_admin:
            rows[key].update(editor="file_import", editable_here=True, can_edit=True, edit_mode="inline")
    if principal.instance_admin:
        hosts = ingress["settings"]["native_file_hosts"]
        providers = ingress["settings"]["native_file_providers"]
        value("hub_file_sources", {"allowed_hosts": hosts["effective_value"],
              "file_source_providers": providers["effective_value"]},
              hosts["source"] if hosts["source"] == providers["source"] else "mixed",
              configured={"hosts": hosts["configured_value"], "providers": providers["configured_value"]},
              state="invalid" if hosts["state"] == "invalid" or providers["state"] == "invalid" else "known")
        rows["hub_file_sources"].update(editor="file_import", editable_here=True, can_edit=True,
                                         edit_mode="inline", activation="new_call")
        value("hub_process", {"port": config.port, "timezone": str(config.timezone),
              "data_dir": str(store.directory), "oidc_public_url": config.oidc_public_url},
              "running_process")
        value("reliability", {"queue_ttl_seconds": runtime.queue_seconds,
              "call_wait_seconds": runtime.wait_seconds,
              "delivery_retry_seconds": runtime.retry_seconds}, "running_process")
    else:
        for key in ("hub_file_sources", "hub_process", "reliability"):
            rows[key]["state"] = "restricted"
    features = getattr(getattr(runtime, "collaboration", None), "config", None)
    if features is not None:
        value("collaboration_features", {key: getattr(features, key) for key in
              ("enabled", "events_enabled", "collector_enabled", "analysis_dispatch_enabled")
              if type(getattr(features, key, None)) is bool}, "running_process")
    gateway = getattr(runtime, "gateway", None)
    if gateway is not None:
        value("gateway", {"enabled": gateway.enabled}, "running_process")
    if project:
        value("project_mapping", {key: project.get(key) for key in ("alias", "mode", "allow_tasks")}, "database")
    choices = [{"id": row["id"], "alias": row["alias"], "online": bool(row.get("online"))}
               for row in runtime.list_projects(principal)]
    return {**catalog, "schema_version": 1, "space_id": principal.space_id,
            "instance_admin": principal.instance_admin, "space_admin": principal.admin,
            "selected_project": project["id"] if project else "",
            "projects": choices, "checked_at": time.time(),
            "node": {"state": "not_checked" if project and runtime.online(project["device_id"])
                     else "offline" if project else "select_project",
                     "effective_value": None},
            "note": "只聚合现有配置来源；部署、本机权限和凭据通过各自入口管理。"}


def node_snapshot(result):
    """Strictly bounded projection, even if a node returns an unexpected payload."""
    from shared.file_sources import normalize_file_hosts, normalize_file_source_providers
    import re
    if not isinstance(result, dict):
        return {"state": "unknown", "values": {}}
    operation_id = result.get("operation_id")
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id):
        operation_id = None
    if result.get("pending") is True:
        return {"state": "pending", "operation_id": operation_id, "values": {},
                "checked_at": time.time()}
    if "result" in result:
        receipt = result.get("result")
        if not isinstance(receipt, dict) or receipt.get("ok") is not True:
            return {"state": "failed", "operation_id": operation_id, "values": {},
                    "checked_at": time.time()}
        result = receipt.get("data", {})
    if not isinstance(result, dict) or not isinstance(result.get("file_import"), dict):
        return {"state": "unknown", "operation_id": operation_id, "values": {}}
    def object_value(parent, key):
        value = parent.get(key)
        return value if isinstance(value, dict) else {}
    def booleans(parent, keys):
        return {key: parent[key] for key in keys if type(parent.get(key)) is bool}
    files = result["file_import"]
    shell = object_value(object_value(result, "execution"), "shell")
    browser = object_value(result, "browser")
    policy = {}
    for key in ("allowed_hosts", "extra_hosts"):
        if key in files:
            try:
                policy[key] = normalize_file_hosts(files[key], key)
            except (ValueError, TypeError):
                pass
    if "file_source_providers" in files:
        try:
            policy["file_source_providers"] = normalize_file_source_providers(files["file_source_providers"])
        except (ValueError, TypeError):
            pass
    if type(files.get("max_bytes")) is int and 1 <= files["max_bytes"] <= 512 * 1024 * 1024:
        policy["max_bytes"] = files["max_bytes"]
    if files.get("policy_mode") in ("default", "explicit"):
        policy["policy_mode"] = files["policy_mode"]
    version = files.get("source_policy_version")
    if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version):
        policy["source_policy_version"] = version
    shell_value = booleans(shell, ("enabled", "configured_enabled", "effective_ready", "inherit_env"))
    if type(shell.get("max_timeout_seconds")) is int and 1 <= shell["max_timeout_seconds"] <= 86400:
        shell_value["max_timeout_seconds"] = shell["max_timeout_seconds"]
    write = "unknown"
    checks = result.get("checks")
    if isinstance(checks, list):
        for check in checks[:32]:
            if isinstance(check, dict) and check.get("name") == "project_write" and check.get("state") in ("ready", "denied", "paused", "unknown"):
                write = check["state"]
    values = {
        "node_ingress": files.get("resumable_upload_enabled") if
            type(files.get("resumable_upload_enabled")) is bool else None,
        "node_file_sources": policy,
        "shell": shell_value,
        "browser": booleans(browser, ("enabled", "connected", "profile_bound")),
        "local_control": result.get("local_control") if type(result.get("local_control")) is bool else None,
        "project_write": write,
    }
    return {"state": "checked", "operation_id": operation_id, "values": values,
            "checked_at": time.time(), "source": "agent_runtime",
            "configured_source": "unknown",
            "note": "节点只报告运行中的有效值；未报告的本机显式配置和待重启差异仍未知。"}
