"""Process bootstrap gates; owner channel preferences persist separately."""
import os
from dataclasses import dataclass
from shared.config import ConfigurationError, env_bool


@dataclass(frozen=True)
class CollaborationConfig:
    enabled: bool = False
    events_enabled: bool = False
    collector_enabled: bool = False
    analysis_dispatch_enabled: bool = False
    weixin_enabled: bool = True
    telegram_enabled: bool = False
    experience_projection_enabled: bool = True
    relay_batch_enabled: bool = True
    relay_batch_shadow: bool = False
    host_connections_mode: str = 'off'
    weixin_delivery_order_v2: bool = True

    @classmethod
    def from_env(cls):
        enabled = env_bool('CODEPIER_COLLABORATION_ENABLED', False)
        weixin_enabled = env_bool('CODEPIER_WEIXIN_ENABLED', True)
        mode = os.getenv('CODEPIER_ROOM_HOST_CONNECTIONS_MODE', 'off')
        if mode not in {'off', 'shadow', 'on'}:
            raise ConfigurationError('CODEPIER_ROOM_HOST_CONNECTIONS_MODE must be off, shadow or on')
        result = cls(enabled,
                     env_bool('CODEPIER_MCP_EVENTS_ENABLED', False),
                     env_bool('CODEPIER_MONITOR_COLLECTOR_ENABLED', False),
                     env_bool('CODEPIER_ANALYSIS_DISPATCH_ENABLED', False),
                     enabled and weixin_enabled,
                     env_bool('CODEPIER_TELEGRAM_ENABLED', False),
                     env_bool('CODEPIER_EXPERIENCE_PROJECTION_ENABLED', True),
                     env_bool('CODEPIER_RELAY_BATCH_ENABLED', True),
                     env_bool('CODEPIER_RELAY_BATCH_SHADOW', False), mode,
                     env_bool('CODEPIER_WEIXIN_DELIVERY_ORDER_V2', True))
        if not result.enabled and any((result.events_enabled, result.collector_enabled, result.analysis_dispatch_enabled, result.weixin_enabled, result.telegram_enabled)):
            raise ConfigurationError('Collaboration must be enabled before its events, collector or analysis dispatcher')
        return result
