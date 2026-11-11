"""Bind encrypted ready to this Agent connection, and pin support durably."""

import hmac
import logging
import secrets


def new_binding():
    return {"version": 1, "agent_nonce": secrets.token_hex(32)}


def verify_binding(journal, sent, ready):
    received = ready.get("channel_binding")
    pinned = journal.meta("hub_channel_binding") == "1"
    if received is None:
        # Upgrade Hub before Agent. Silent first-contact legacy fallback would
        # accept a recording captured before the upgrade and bypass freshness.
        logging.getLogger("codepier.agent").warning(
            "Hub 缺少安全通道绑定；请先升级 Hub 再升级 Agent，已拒绝旧握手或重放。"
        )
        raise ValueError(
            "Hub channel binding missing; replay or downgrade rejected. Upgrade Hub before Agent."
        )
    if (
        not isinstance(received, dict)
        or type(received.get("version")) is not int
        or received["version"] != 1
        or not isinstance(received.get("agent_nonce"), str)
        or not received["agent_nonce"].isascii()
        or not hmac.compare_digest(received["agent_nonce"], sent["agent_nonce"])
    ):
        raise ValueError("Hub channel binding mismatch; replay rejected")
    if not pinned:
        journal.set_meta("hub_channel_binding", "1")
    return True
