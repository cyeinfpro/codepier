"""Typed Hub file-ingress policy stored in the existing meta database.

Owner overrides > explicit deployment environment > reviewed source defaults.
An absent override inherits; False and [] are never interpreted as absence.
No credentials, node configuration or project permissions live here.
"""
from __future__ import annotations

import hashlib
import json
import os

from hub.api.models import Model
from pydantic import Field, field_validator, model_validator
from shared.file_sources import (
    DEFAULT_FILE_HOSTS, FILE_SOURCE_PROFILE_NAMES, file_source_policy,
    normalize_file_hosts, normalize_file_source_providers,
)
from shared.util import DevError

META_KEY = "hub_file_import_settings"
ENVIRONMENT = {
    "streaming_enabled": "CODEPIER_FILE_IMPORT_STREAMING",
    "native_relay_enabled": "CODEPIER_NATIVE_FILE_RELAY",
    "native_file_hosts": "CODEPIER_NATIVE_FILE_HOSTS",
    "native_file_providers": "CODEPIER_NATIVE_FILE_PROVIDERS",
}
DEFAULTS = {
    "streaming_enabled": True,
    "native_relay_enabled": True,
    "native_file_hosts": list(DEFAULT_FILE_HOSTS),
    "native_file_providers": [],
}
NOTE = ("Changes apply to new admission checks immediately. A finish already admitted "
        "to the Agent may publish before the next check. A source download keeps its "
        "original per-hop policy snapshot; project, grant and node permissions still apply.")


class FileImportPatch(Model):
    streaming_enabled: bool | None = None
    native_relay_enabled: bool | None = None
    native_file_hosts: list[str] | None = Field(default=None, max_length=20)
    native_file_providers: list[str] | None = Field(default=None, max_length=20)

    @field_validator("native_file_hosts")
    @classmethod
    def hosts(cls, value):
        return None if value is None else normalize_file_hosts(value, "native_file_hosts")

    @field_validator("native_file_providers")
    @classmethod
    def providers(cls, value):
        return None if value is None else normalize_file_source_providers(value)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set:
            raise ValueError("Specify at least one setting; null explicitly resets it to inherit")
        return self


class FileImportPreview(Model):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    patch: FileImportPatch


class FileImportUpdate(FileImportPreview):
    confirmation: str = Field(pattern=r"^[a-f0-9]{64}$")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def initialize(store):
    """Additive startup migration, including old databases; never overwrite policy."""
    with store.transaction():
        store.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
                      (META_KEY, _json({"version": 1, "revision": 0, "values": {}})))


def _normalize(key, value):
    if key in ("streaming_enabled", "native_relay_enabled"):
        if type(value) is not bool:
            raise ValueError("Invalid boolean")
        return value
    if type(value) is not list:
        raise ValueError("Invalid list")
    if key == "native_file_hosts":
        return normalize_file_hosts(value, key)
    if key == "native_file_providers":
        return normalize_file_source_providers(value)
    raise ValueError("Unknown setting")


def _record(store):
    row = store.one("SELECT value FROM meta WHERE key=?", (META_KEY,)) if store is not None else None
    if row is None:
        return {"version": 1, "revision": 0, "values": {}}, None
    try:
        record = json.loads(row["value"])
        if (type(record) is not dict or set(record) != {"version", "revision", "values"}
                or type(record["version"]) is not int or record["version"] != 1
                or type(record["revision"]) is not int or record["revision"] < 0
                or type(record["values"]) is not dict
                or set(record["values"]) - ENVIRONMENT.keys()):
            raise ValueError("Invalid record")
        record["values"] = {key: _normalize(key, value) for key, value in record["values"].items()}
        return record, None
    except (ValueError, TypeError, KeyError):
        # Do not expose malformed database contents or fall back to a wider policy.
        return None, _digest(row["value"])


def _inherited(key, environment):
    env = ENVIRONMENT[key]
    if env not in environment:
        value = DEFAULTS[key]
        return list(value) if isinstance(value, list) else value, "default", "known"
    try:
        raw = environment[env]
        value = ({"true": True, "false": False}[raw] if key.endswith("_enabled")
                 else json.loads(raw))
        return _normalize(key, value), "environment", "known"
    except (ValueError, TypeError, KeyError):
        return False if key.endswith("_enabled") else [], "environment", "invalid"


def _snapshot(record, invalid, environment):
    settings = {}
    for key in ENVIRONMENT:
        inherited, inherited_source, inherited_state = _inherited(key, environment)
        configured = record["values"].get(key) if record is not None else None
        explicit = record is not None and key in record["values"]
        effective = configured if explicit else inherited
        state = "known" if explicit else inherited_state
        source = "database" if explicit else inherited_source
        if invalid:
            effective, state, source = (False if key.endswith("_enabled") else []), "invalid", "database"
        settings[key] = dict(configured_value=configured, effective_value=effective,
                             source=source, state=state, activation="new_call",
                             inherited_value=inherited, inherited_source=inherited_source,
                             inherited_state=inherited_state)
    if not settings["streaming_enabled"]["effective_value"]:
        settings["native_relay_enabled"]["effective_value"] = False
    policy = None
    if all(settings[key]["state"] == "known" for key in ("native_file_hosts", "native_file_providers")):
        config = {"file_source_providers": settings["native_file_providers"]["effective_value"]}
        if settings["native_file_hosts"]["source"] != "default":
            config["file_hosts"] = settings["native_file_hosts"]["effective_value"]
        policy = {**file_source_policy(config), "policy_scope": "hub"}
    # Raw values are only hashed for concurrency. They never leave this module.
    revision = _digest({"record": record, "invalid": invalid, "environment": environment, "defaults": DEFAULTS})
    return {"schema_version": 1, "revision": revision, "settings": settings,
            "source_policy": policy, "provider_options": sorted(FILE_SOURCE_PROFILE_NAMES),
            "activation": "new_call", "note": NOTE}


def snapshot(store=None):
    record, invalid = _record(store)
    environment = {name: os.environ[name] for name in ENVIRONMENT.values() if name in os.environ}
    return _snapshot(record, invalid, environment)


def source_policy(store=None):
    policy = snapshot(store)["source_policy"]
    if policy is None:
        raise DevError("FILE_IMPORT_POLICY_INVALID", "Hub native-file source policy is invalid", 503)
    return policy


def enabled(key, store=None):
    return snapshot(store)["settings"][key]["effective_value"] is True


def preview(store, expected_revision, patch):
    record, invalid = _record(store)
    environment = {name: os.environ[name] for name in ENVIRONMENT.values() if name in os.environ}
    before = _snapshot(record, invalid, environment)
    if before["revision"] != expected_revision:
        raise DevError("SETTINGS_CHANGED", "Settings changed; reread and review your draft", 409)
    if invalid:
        raise DevError("FILE_IMPORT_POLICY_INVALID", "Stored Hub policy requires administrator repair", 503)
    values = dict(record["values"])
    normalized = patch.model_dump(exclude_unset=True)
    for key, value in normalized.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    next_record = {"version": 1, "revision": record["revision"] + 1, "values": values}
    after = _snapshot(next_record, None, environment)
    # Include dependent effective changes, such as native relay becoming enabled.
    changes = [{"key": key, "before": before["settings"][key], "after": after["settings"][key],
                "reset_to_inherit": key in normalized and normalized[key] is None}
               for key in ENVIRONMENT if key in normalized or before["settings"][key] != after["settings"][key]]
    confirmation = _digest({"expected_revision": expected_revision, "patch": normalized,
                            "changes": changes, "resulting_policy": after["source_policy"]})
    return {"changes": changes, "confirmation": confirmation, "snapshot": after}, next_record


def apply(store, principal, body):
    result, record = preview(store, body.expected_revision, body.patch)
    if body.confirmation != result["confirmation"]:
        raise DevError("SETTINGS_CONFIRMATION_REQUIRED", "Review the exact setting changes before saving", 409)
    store.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (META_KEY, _json(record)))
    # Only allowlisted typed values, no URLs, file IDs, credentials or request payloads.
    store.audit(principal.actor, "settings.file_import.updated", detail={
        "settings": body.patch.model_dump(exclude_unset=True),
        "revision": record["revision"], "activation": "new_call"})
    return result["snapshot"]
