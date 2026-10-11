"""Process bootstrap gates; owner channel preferences persist separately."""
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

    @classmethod
    def from_env(cls):
        enabled = env_bool('CODEPIER_COLLABORATION_ENABLED', False)
        weixin_enabled = env_bool('CODEPIER_WEIXIN_ENABLED', True)
        result = cls(enabled,
                     env_bool('CODEPIER_MCP_EVENTS_ENABLED', False),
                     env_bool('CODEPIER_MONITOR_COLLECTOR_ENABLED', False),
                     env_bool('CODEPIER_ANALYSIS_DISPATCH_ENABLED', False),
                     enabled and weixin_enabled,
                     env_bool('CODEPIER_TELEGRAM_ENABLED', False))
        if not result.enabled and any((result.events_enabled, result.collector_enabled, result.analysis_dispatch_enabled, result.weixin_enabled, result.telegram_enabled)):
            raise ConfigurationError('Collaboration must be enabled before its events, collector or analysis dispatcher')
        return result
