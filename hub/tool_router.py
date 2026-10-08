"""One explicit public catalog for native and reviewed remote backends.

Unknown/internal names are never used as a gateway fallback. Catalog visibility
is not authorization: both backends revalidate at execution boundaries.
"""
from __future__ import annotations
from dataclasses import dataclass
from hub import iam
from hub.principal import refresh_principal
from shared.contracts import tool_definitions
from shared.core_contracts import REPLACED_MCP_TOOLS
from shared.integration_contracts import ADMIN_TOOLS
from shared.util import DevError

@dataclass(frozen=True)
class ToolRoute:
    backend: str
    name: str

class ToolRouter:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway

    @iam.read_decision
    def definitions(self, principal):
        principal = refresh_principal(self.store, principal)
        native = tool_definitions(authorization=principal.authorization_mode)
        collaboration = getattr(self.store, "collaboration", None)
        if collaboration is not None and collaboration.config.enabled:
            from shared.collaboration_contracts import tool_definitions as collaboration_tools
            native += collaboration_tools(authorization=principal.authorization_mode)
        external = self.gateway.tools(principal)
        from shared.public_collaboration import LEGACY_TOOLS
        if collaboration is not None and collaboration.config.enabled and any(
                item['name'] in LEGACY_TOOLS for item in external):
            raise DevError('TOOL_NAME_COLLISION', '远端工具不能覆盖兼容协作调用', 409)
        names = [item['name'] for item in native + external]
        if len(names) != len(set(names)):
            raise DevError('TOOL_NAME_COLLISION', '工具发布名称冲突；未选择任意后端', 409)
        return principal, native, external

    def list_tools(self, principal, cursor=None):
        principal, native, external = self.definitions(principal)
        from hub.gateway.catalog import page
        return page(native + external, cursor,
                    [principal.space_id, principal.user_id, principal.grant_id, principal.profile_id],
                    self.gateway.secret)

    def resolve(self, principal, name):
        if name in REPLACED_MCP_TOOLS:
            raise DevError('TOOL_REMOVED', '旧工具已移除，请使用 ' + REPLACED_MCP_TOOLS[name], 404)
        if name in ADMIN_TOOLS:
            raise DevError('OWNER_REQUIRED', '此操作只接受已授权的管理入口', 403)
        principal, native, external = self.definitions(principal)
        from shared.public_collaboration import LEGACY_TOOLS
        collaboration = getattr(self.store, "collaboration", None)
        if name in LEGACY_TOOLS and collaboration is not None and collaboration.config.enabled:
            # Compatibility is a route, not an advertised tool. The same business
            # handler still rechecks current scope, feature flags, grant and lease.
            return ToolRoute('native', name)
        if any(item['name'] == name for item in native):
            return ToolRoute('native', name)
        if any(item['name'] == name for item in external):
            return ToolRoute('remote', name)
        raise DevError('UNKNOWN_TOOL', '工具不存在或不在当前授权目录', 404)
