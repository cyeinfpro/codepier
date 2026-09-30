"""Validate Hub process settings before acquiring or migrating persistent state."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from shared.config import DEFAULT_HUB_PORT, DEFAULT_PUBLIC_URL, ConfigurationError, RuntimeConfig, env_bool, env_csv, env_int, env_timezone
from shared.util import normalize_url

# The one provider a deployment may declare through CODEPIER_OIDC_*.
OIDC_SEED_PROVIDER_ID = "idp_env"


@dataclass(frozen=True)
class OIDCSeed:
    """A provider declared by the deployment environment and reconciled at every start.

    The environment stays authoritative for this provider: the panel cannot edit it,
    and changing a setting takes effect on the next Hub start.
    """
    label: str
    issuer: str
    client_id: str
    client_secret: str
    discovery_url: str
    scopes: str
    group_claim: str
    required_group: str
    endpoint_origins: tuple[str, ...]
    freshness_seconds: int
    enabled: bool

    @classmethod
    def from_env(cls) -> OIDCSeed | None:
        issuer = os.getenv("CODEPIER_OIDC_ISSUER", "").strip()
        if not issuer:
            return None
        client_id = os.getenv("CODEPIER_OIDC_CLIENT_ID", "").strip()
        client_secret = os.getenv("CODEPIER_OIDC_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise ConfigurationError("CODEPIER_OIDC_CLIENT_ID and CODEPIER_OIDC_CLIENT_SECRET are required with CODEPIER_OIDC_ISSUER")
        seed = cls(
            label=os.getenv("CODEPIER_OIDC_LABEL", "").strip() or "SSO",
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            discovery_url=os.getenv("CODEPIER_OIDC_DISCOVERY_URL", "").strip(),
            scopes=os.getenv("CODEPIER_OIDC_SCOPES", "").strip() or "openid profile email",
            group_claim=os.getenv("CODEPIER_OIDC_GROUP_CLAIM", "").strip() or "groups",
            required_group=os.getenv("CODEPIER_OIDC_REQUIRED_GROUP", "").strip(),
            endpoint_origins=env_csv("CODEPIER_OIDC_ENDPOINT_ORIGINS"),
            freshness_seconds=env_int("CODEPIER_OIDC_FRESHNESS_SECONDS", 900, 60, 86400),
            enabled=env_bool("CODEPIER_OIDC_ENABLED", True),
        )
        seed.provider_input()
        return seed

    def provider_input(self):
        """Apply exactly the administration API's validation; errors name settings, never values."""
        from pydantic import ValidationError

        from hub.oidc import ProviderInput, endpoint

        def invalid(setting: str, message: str) -> ConfigurationError:
            return ConfigurationError(f"Invalid OIDC seed setting: {setting}: {message}")

        # The model validates every URL through one code path; check the optional
        # ones first so a failure names the setting that actually needs fixing.
        for setting, values in (("CODEPIER_OIDC_DISCOVERY_URL", [self.discovery_url] if self.discovery_url else []),
                                ("CODEPIER_OIDC_ENDPOINT_ORIGINS", list(self.endpoint_origins))):
            for value in values:
                try:
                    endpoint(value)
                except ValueError as exc:
                    raise invalid(setting, str(exc)) from None
        try:
            return ProviderInput(label=self.label, issuer=self.issuer, client_id=self.client_id, client_secret=self.client_secret,
                                 discovery_url=self.discovery_url, enabled=self.enabled, admission="jit", group_claim=self.group_claim,
                                 required_group=self.required_group, scopes=self.scopes, endpoint_origins=list(self.endpoint_origins),
                                 freshness_seconds=self.freshness_seconds)
        except ValidationError as exc:
            issues = []
            for error in exc.errors():
                message = str(error["msg"])
                if error["loc"]:
                    setting = "CODEPIER_OIDC_" + str(error["loc"][0]).upper()
                elif "scope" in message.lower():
                    setting = "CODEPIER_OIDC_SCOPES"
                elif "discovery" in message.lower():
                    setting = "CODEPIER_OIDC_DISCOVERY_URL"
                elif "origin" in message.lower():
                    setting = "CODEPIER_OIDC_ENDPOINT_ORIGINS"
                else:
                    setting = "CODEPIER_OIDC_ISSUER"
                issues.append(f"{setting}: {message}")
            raise ConfigurationError("Invalid OIDC seed setting: " + "; ".join(sorted(set(issues)))) from None


@dataclass(frozen=True)
class HubConfig:
    port: int
    timezone: ZoneInfo
    public_url: str
    runtime: RuntimeConfig
    oidc_seed: OIDCSeed | None = None
    # first-login: while no active instance administrator exists, the first admitted
    # OIDC identity receives instance authority. The window closes by itself.
    oidc_bootstrap_admin: bool = False

    @classmethod
    def from_env(cls) -> HubConfig:
        port = env_int("HUB_PORT", DEFAULT_HUB_PORT, 1, 65535)
        timezone = env_timezone()
        setting = "MCP_PUBLIC_URL" if os.getenv("MCP_PUBLIC_URL") else "HUB_PUBLIC_URL"
        try:
            public_url = normalize_url(os.getenv(setting) or DEFAULT_PUBLIC_URL)
        except ValueError:
            raise ConfigurationError(f"{setting} must be an HTTP(S) base URL without credentials, a query or a fragment") from None
        bootstrap = os.getenv("CODEPIER_OIDC_BOOTSTRAP_ADMIN", "off").strip().lower()
        if bootstrap not in {"off", "first-login"}:
            raise ConfigurationError("CODEPIER_OIDC_BOOTSTRAP_ADMIN must be off or first-login")
        return cls(port, timezone, public_url, RuntimeConfig.from_env(), OIDCSeed.from_env(), bootstrap == "first-login")
