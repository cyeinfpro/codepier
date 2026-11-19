"""One explicit public catalog for native and reviewed remote backends.

Unknown/internal names are never used as a gateway fallback. Catalog visibility
is not authorization: both backends revalidate at execution boundaries.
"""

from __future__ import annotations
from collections import OrderedDict
from dataclasses import dataclass
from hub import iam
from hub.principal import refresh_principal
from hub.tool_discovery import visible_definitions
from shared.contracts import tool_definitions, project_output_schemas, CODING_TOOLS
from shared.core_contracts import REPLACED_MCP_TOOLS
from shared.integration_contracts import ADMIN_TOOLS, INCOMING_UPLOAD_TOOLS
from shared.util import DevError


@dataclass(frozen=True)
class ToolRoute:
    backend: str
    name: str


class ToolRouter:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway
        self._collision_cache = OrderedDict()
        self._native_names_cache = {}

    def _native_tool_names(self, collaboration_enabled, authorization_mode):
        key = (collaboration_enabled, authorization_mode)
        cached = self._native_names_cache.get(key)
        if cached is None:
            names = {item["name"] for item in tool_definitions(authorization=authorization_mode)}
            if collaboration_enabled:
                from shared.collaboration_contracts import tool_definitions as collaboration_tools

                names.update(
                    item["name"] for item in collaboration_tools(authorization=authorization_mode)
                )
            cached = frozenset(names)
            self._native_names_cache[key] = cached
        return cached

    def _check_unified_collision(self, principal, collaboration_enabled):
        epoch = (
            self.store.epoch_stamp()
            if hasattr(self.store, "epoch_stamp")
            else getattr(self.store, "session_revision", 0)
        )
        gw_rev = (
            bool(getattr(self.gateway, "enabled", True)),
            getattr(self.gateway, "catalog_revision", None),
            id(getattr(self.gateway, "__dict__", {}).get("tools")),
        )
        cache_key = (
            principal.space_id,
            principal.user_id,
            principal.grant_id,
            principal.role_id,
            principal.authorization_mode,
            epoch,
            gw_rev,
            collaboration_enabled,
        )
        if cache_key in self._collision_cache:
            self._collision_cache.move_to_end(cache_key)
            return
        external = self.gateway.tools(principal)
        from shared.public_collaboration import LEGACY_TOOLS
        from shared.unified_contracts import UNIFIED_TOOLS

        ext_names = [item["name"] for item in external]
        if len(ext_names) != len(set(ext_names)):
            raise DevError("TOOL_NAME_COLLISION", "工具发布名称冲突；未选择任意后端", 409)
        ext_set = set(ext_names)
        if collaboration_enabled and (ext_set & LEGACY_TOOLS):
            raise DevError("TOOL_NAME_COLLISION", "远端工具不能覆盖兼容协作调用", 409)
        if ext_set & UNIFIED_TOOLS:
            raise DevError("TOOL_NAME_COLLISION", "远端工具不能覆盖统一工具 query/act", 409)
        if ext_set & self._native_tool_names(collaboration_enabled, principal.authorization_mode):
            raise DevError("TOOL_NAME_COLLISION", "工具发布名称冲突；未选择任意后端", 409)
        self._collision_cache[cache_key] = True
        while len(self._collision_cache) > 1024:
            self._collision_cache.popitem(last=False)

    @iam.read_decision
    def definitions(self, principal, *, discovery=True, profile="unified", schemas="full"):
        principal = refresh_principal(self.store, principal)
        native = tool_definitions(authorization=principal.authorization_mode)
        collaboration = getattr(self.store, "collaboration", None)
        if collaboration is not None and collaboration.config.enabled:
            from shared.collaboration_contracts import tool_definitions as collaboration_tools

            native += collaboration_tools(authorization=principal.authorization_mode)
        external = self.gateway.tools(principal)
        from shared.public_collaboration import LEGACY_TOOLS

        if (
            collaboration is not None
            and collaboration.config.enabled
            and any(item["name"] in LEGACY_TOOLS for item in external)
        ):
            raise DevError("TOOL_NAME_COLLISION", "远端工具不能覆盖兼容协作调用", 409)
        names = [item["name"] for item in native + external]
        if len(names) != len(set(names)):
            raise DevError("TOOL_NAME_COLLISION", "工具发布名称冲突；未选择任意后端", 409)
        # Resolve against the full reserved namespace, before discovery projection.
        # Hiding a name is neither authorization nor permission for a gateway to claim it.
        if discovery:
            if profile == "unified":
                from hub.unified_tools import tool_definitions as unified_definitions
                from shared.unified_contracts import UNIFIED_TOOLS

                if any(item["name"] in UNIFIED_TOOLS for item in external):
                    raise DevError("TOOL_NAME_COLLISION", "远端工具不能覆盖统一工具 query/act", 409)
                enabled = collaboration is not None and collaboration.config.enabled
                # get_profile stays published: hosts use its openai/profile marker
                # as the identity protocol, which a merged tool cannot carry.
                identity = project_output_schemas(
                    visible_definitions(
                        [item for item in native if item["name"] == "get_profile"], principal.scopes
                    ),
                    schemas,
                )
                native = (
                    unified_definitions(
                        principal.scopes,
                        collaboration=enabled,
                        authorization=principal.authorization_mode,
                    )
                    + identity
                )
                return principal, native, external
            if profile == "coding":
                native = [item for item in native if item["name"] in CODING_TOOLS]
            elif profile not in {"core", "full"}:
                raise ValueError("Unknown MCP tool profile")
            native = project_output_schemas(visible_definitions(native, principal.scopes), schemas)
            if schemas == "lean":
                from shared.public_collaboration import PUBLIC_TOOLS, lean_input_schema

                native = [
                    {**item, "inputSchema": lean_input_schema(item["inputSchema"])}
                    if item["name"] in PUBLIC_TOOLS
                    else item
                    for item in native
                ]
        return principal, native, external

    def list_tools(self, principal, cursor=None, profile="unified", schemas="full"):
        principal, native, external = self.definitions(principal, profile=profile, schemas=schemas)
        from hub.gateway.catalog import page, fingerprint

        result = page(
            native + external,
            cursor,
            [principal.space_id, principal.user_id, principal.grant_id, principal.profile_id],
            self.gateway.secret,
        )
        result["_meta"] = {
            "com.codepier/catalogSha256": fingerprint(
                sorted(native + external, key=lambda item: item["name"])
            )
        }
        return result

    @iam.read_decision
    def resolve(self, principal, name):
        if name in INCOMING_UPLOAD_TOOLS:
            raise DevError(
                "UNKNOWN_TOOL", "文件字节通道仅接受认证上传入口，不能作为模型工具调用", 404
            )
        if name in REPLACED_MCP_TOOLS:
            raise DevError("TOOL_REMOVED", "旧工具已移除，请使用 " + REPLACED_MCP_TOOLS[name], 404)
        if name in ADMIN_TOOLS:
            raise DevError("OWNER_REQUIRED", "此操作只接受已授权的管理入口", 403)
        from shared.unified_contracts import UNIFIED_TOOLS

        if name in UNIFIED_TOOLS:
            principal = refresh_principal(self.store, principal)
            collaboration = getattr(self.store, "collaboration", None)
            collab_enabled = bool(collaboration is not None and collaboration.config.enabled)
            self._check_unified_collision(principal, collab_enabled)
            # A call-shape adapter over the native facades; each translated call
            # is re-resolved and re-authorized by the same native handlers.
            return ToolRoute("native", name)
        principal, native, external = self.definitions(principal, discovery=False)
        from shared.public_collaboration import LEGACY_TOOLS

        collaboration = getattr(self.store, "collaboration", None)
        if name in LEGACY_TOOLS and collaboration is not None and collaboration.config.enabled:
            # Compatibility is a route, not an advertised tool. The same business
            # handler still rechecks current scope, feature flags, grant and lease.
            return ToolRoute("native", name)
        if any(item["name"] == name for item in native):
            return ToolRoute("native", name)
        if any(item["name"] == name for item in external):
            return ToolRoute("remote", name)
        raise DevError("UNKNOWN_TOOL", "工具不存在或不在当前授权目录", 404)
