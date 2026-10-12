"""Read-only, compile-time channel capabilities; declarations never grant access.

An effective capability needs four independent positive facts. Unknown remains
None in the evidence and never enables a feature. No external adapters load here.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from hub.collaboration.telegram_protocol import DOWNLOAD_LIMIT, UPLOAD_LIMIT
from hub.collaboration.weixin_protocol import MAX_MEDIA
from shared.util import DevError

Fact = bool | None
Feature = Literal["text", "image", "file", "video", "voice", "edit", "typing",
                  "native_reply", "receipt_lookup", "read_receipt", "preview", "transcription"]
FEATURES: tuple[Feature, ...] = (
    "text", "image", "file", "video", "voice", "edit", "typing", "native_reply",
    "receipt_lookup", "read_receipt", "preview", "transcription",
)
MEDIA = frozenset({"image", "file", "video", "voice"})


@dataclass(frozen=True)
class ChannelDescriptor:
    channel: str
    implemented: frozenset[str]
    text_unit: str
    text_limit: int | None
    download_limit: int | None
    upload_limit: int | None
    outbound_types: tuple[str, ...]
    rate_limit_dimensions: tuple[str, ...]
    acceptance_proof: str


DESCRIPTORS: Mapping[str, ChannelDescriptor] = MappingProxyType({
    # Image/video bytes are delivered as documents in Telegram, not native cards.
    "telegram": ChannelDescriptor("telegram", frozenset({"text", "image", "file", "video", "native_reply"}),
        "utf16_code_units", 3500, DOWNLOAD_LIMIT, UPLOAD_LIMIT, ("document",),
        ("account", "peer", "provider_retry_after", "daily_mirror_quota"), "provider_accepted"),
    "weixin": ChannelDescriptor("weixin", frozenset({"text", "image", "file", "video"}),
        "utf8_bytes_source_reply_or_utf16_mirror", 3000, MAX_MEDIA, MAX_MEDIA, ("image", "file", "video"),
        ("account", "peer", "daily_mirror_quota"), "provider_accepted"),
})


def intersection(*facts: Fact) -> Fact:
    """False dominates; a missing proof is not a positive capability."""
    if any(value is False for value in facts):
        return False
    return True if all(value is True for value in facts) else None


def describe(channel: str, *, verified: Mapping[str, Fact] | None = None,
             authorized: Mapping[str, Fact] | None = None,
             available: Mapping[str, Fact] | None = None) -> dict:
    descriptor = DESCRIPTORS.get(channel)
    verified, authorized, available = verified or {}, authorized or {}, available or {}
    features = {}
    for name in FEATURES:
        layers = {
            "implemented": bool(descriptor and name in descriptor.implemented),
            "provider_account_verified": verified.get(name),
            "authorized": authorized.get(name),
            "session_available": available.get(name),
        }
        effective = intersection(*layers.values())
        features[name] = {
            **layers, "effective": effective, "enabled": effective is True,
            "unknown_layers": [key for key, value in layers.items() if value is None],
            "evidence_source": "current_binding_receipt" if verified.get(name) is True else "unverified",
        }
    return {
        "schema_version": 1, "channel": descriptor.channel if descriptor else "unsupported",
        "supported": descriptor is not None, "read_only": True, "grants_authority": False,
        "features": features,
        "text": {"unit": descriptor.text_unit if descriptor else "unknown",
                 "limit": descriptor.text_limit if descriptor else None,
                 "mirror_limit": 700 if channel == "weixin" else 3500 if descriptor else None,
                 "mirror_unit": "utf16_code_units" if descriptor else "unknown"},
        "media": {"download_limit": descriptor.download_limit if descriptor else None,
                  "upload_limit": descriptor.upload_limit if descriptor else None,
                  "outbound_types": list(descriptor.outbound_types) if descriptor else [],
                  "approval": "each_file_digest_and_recipient_version",
                  "upload_is_delivery": False, "transcription_available": False,
                  "preview": "download_only"},
        "rate_limit_dimensions": list(descriptor.rate_limit_dimensions) if descriptor else [],
        "error_types": ["authorization", "expired", "rate_limited", "unavailable", "send_unknown"],
        "acceptance_proof": descriptor.acceptance_proof if descriptor else "unknown",
        "read_proof": "unavailable",
        "progress_policy": "coalesce_unsent_important_stages",
        "reference_fallback": "text_source_identifier",
    }


def diagnostic_summary(capabilities: dict) -> dict:
    """Allow-listed export: no identity, content, raw errors, paths or secrets."""
    features = capabilities.get("features", {})
    return {
        "schema_version": 1,
        "channel": capabilities.get("channel") if capabilities.get("channel") in DESCRIPTORS else "unsupported",
        "capabilities": {
            name: {"enabled": features.get(name, {}).get("enabled") is True,
                   "effective": True if features.get(name, {}).get("effective") is True else False if features.get(name, {}).get("effective") is False else None,
                   "evidence_source": "current_binding_receipt" if features.get(name, {}).get("evidence_source") == "current_binding_receipt" else "unverified",
                   "unknowns": [key for key in ("implemented", "provider_account_verified", "authorized", "session_available")
                                if key in features.get(name, {}).get("unknown_layers", [])]}
            for name in FEATURES
        },
    }


class ChannelCapabilities:
    def __init__(self, router):
        self.router, self.c, self.store = router, router.c, router.store

    def for_binding(self, channel, identifier):
        """Called only after the normal authenticated listing scope check."""
        if channel not in DESCRIPTORS:
            return describe(channel)
        try:
            binding, account, *_ = self.router.bindings.validate(channel, identifier)
        except DevError:
            return describe(channel, authorized={name: False for name in FEATURES},
                            available={name: False for name in FEATURES})
        verified: dict[str, Fact] = {}
        # Receipts prove only platform acceptance for this current recipient and
        # credential generation. They never prove recipient reading or all media.
        rows = self.store.all(
            """SELECT DISTINCT p.kind FROM channel_mirror_parts p
            JOIN channel_mirrors d ON d.id=p.delivery_id
            WHERE d.channel=? AND d.binding_id=? AND d.binding_version=? AND d.generation=?
              AND d.credential_version=? AND p.state='accepted'""",
            (channel, identifier, binding["version"], binding["generation"], account["credential_version"]),
        )
        if channel == "weixin":
            rows += self.store.all(
                """SELECT DISTINCT p.kind FROM weixin_parts p JOIN weixin_deliveries d ON d.id=p.delivery_id
                WHERE d.binding_id=? AND d.generation=? AND d.peer_id=? AND p.state='accepted'""",
                (identifier, binding["generation"], binding["peer_id"]),
            )
        if any(row["kind"] == "text" for row in rows):
            verified["text"] = True
        # Generic attachment success cannot prove a particular media encoding.
        # Per-file receipts are shown in the manifest instead.
        approved_text = bool(binding.get("allow_replies") if channel == "weixin" else binding.get("mirror_enabled"))
        authorized = {name: approved_text for name in FEATURES}
        for name in MEDIA:
            authorized[name] = None  # Each actual file needs its own current review.
        available = {name: True for name in FEATURES}
        if channel == "weixin":
            context = self.store.one(
                "SELECT 1 FROM weixin_contexts WHERE account_id=? AND generation=? AND peer_id=?",
                (account["id"], binding["generation"], binding["peer_id"]),
            )
            available = {name: bool(context) for name in FEATURES}
        value = describe(channel, verified=verified, authorized=authorized, available=available)
        value["authorization_scope"] = "current_binding_only"
        return value


def subscription_proposal(*, requested_ttl_ms: int | None = 86_400_000,
                          host_verified: Fact = None, account_verified: Fact = None,
                          independent_approval: Fact = None) -> dict:
    """P2 proposal only. No lifecycle mutation or implicit subscription approval.

    The protocol may allow null, but this implementation deliberately retains the
    existing finite policy until lifetime, revocation and recovery are validated.
    """
    requested_long_lived = requested_ttl_ms is None
    return {
        "schema_version": 1, "read_only": True, "proposal_only": True,
        "local_decision_model": {"available": True, "simulation_only": True,
                                 "runtime_evidence_repository_connected": False},
        "protocol_allows_unbounded_request": True,
        "requested_ttl_ms": requested_ttl_ms,
        "effective_ttl_ms": 86_400_000 if requested_ttl_ms is None else min(max(60_000, requested_ttl_ms), 86_400_000),
        "long_lived": {"implemented": False, "host_verified": host_verified,
                       "account_verified": account_verified, "independent_approval": independent_approval,
                       "enabled": False},
        "downgraded": requested_long_lived,
        "reason": "finite_policy_retained",
        "requires_independent_approval": True,
        "changes_subscription": False, "changes_grant": False, "changes_work_lease": False,
        "resumes_paused_subscription": False,
    }
