"""Default-off Telegram transport and immutable per-target delivery records."""

from typing import Literal

from hub.collaboration.channel_consent import OwnerPreapproval
from pydantic import Field, SecretStr
from hub.collaboration.weixin_schema import Scope, Mutation

SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_accounts(
 id TEXT PRIMARY KEY,space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,bot_id TEXT NOT NULL UNIQUE,
 label TEXT NOT NULL,secret TEXT NOT NULL,state TEXT NOT NULL,credential_version INTEGER NOT NULL,
 generation INTEGER NOT NULL DEFAULT 1,offset INTEGER NOT NULL DEFAULT 0,
 poll_fence INTEGER NOT NULL DEFAULT 0,poll_until REAL NOT NULL DEFAULT 0,next_poll REAL NOT NULL DEFAULT 0,
 last_sync_at REAL,reason_code TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_bindings(
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL,space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,
 room_id TEXT NOT NULL,conversation_id TEXT NOT NULL,project_id TEXT NOT NULL,environment_id TEXT NOT NULL,
 dot_id TEXT NOT NULL,owner_principal TEXT NOT NULL,dot_principal TEXT NOT NULL,project_snapshot TEXT NOT NULL,
 credential_version INTEGER NOT NULL,generation INTEGER NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL,user_id TEXT NOT NULL DEFAULT '',chat_id TEXT NOT NULL DEFAULT '',
 pending_user_id TEXT NOT NULL DEFAULT '',pending_chat_id TEXT NOT NULL DEFAULT '',
 pairing_hash TEXT NOT NULL,pairing_secret TEXT NOT NULL,pairing_until REAL NOT NULL,
 mirror_enabled INTEGER NOT NULL DEFAULT 0,disable_notification INTEGER NOT NULL DEFAULT 0,
 allow_media_outbound INTEGER NOT NULL DEFAULT 0,expires_at REAL NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS telegram_one_conversation ON telegram_bindings(account_id,conversation_id) WHERE state!='revoked';
CREATE UNIQUE INDEX IF NOT EXISTS telegram_one_peer_room ON telegram_bindings(account_id,user_id,chat_id) WHERE state='active';
CREATE TABLE IF NOT EXISTS telegram_inbox(
 account_id TEXT NOT NULL,update_id INTEGER NOT NULL,credential_version INTEGER NOT NULL,generation INTEGER NOT NULL,
 source_digest TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,message_id TEXT,reason_code TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL,PRIMARY KEY(account_id,update_id));
CREATE TABLE IF NOT EXISTS channel_mirrors(
 id TEXT PRIMARY KEY,channel TEXT NOT NULL,binding_id TEXT NOT NULL,binding_version INTEGER NOT NULL,
 generation INTEGER NOT NULL,credential_version INTEGER NOT NULL,message_id TEXT NOT NULL,
 message_version INTEGER NOT NULL,message_digest TEXT NOT NULL,room_id TEXT NOT NULL,conversation_id TEXT NOT NULL,
 state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
 UNIQUE(channel,binding_id,generation,message_id,message_version));
CREATE TABLE IF NOT EXISTS channel_mirror_parts(
 id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL,ordinal INTEGER NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,
 state TEXT NOT NULL,external_id TEXT NOT NULL DEFAULT '',reason_code TEXT NOT NULL DEFAULT '',
 not_before REAL NOT NULL DEFAULT 0,lease_fence INTEGER NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,
 created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(delivery_id,ordinal));
CREATE INDEX IF NOT EXISTS channel_mirror_ready ON channel_mirror_parts(state,not_before,updated);
CREATE TABLE IF NOT EXISTS channel_message_map(
 channel TEXT NOT NULL,binding_id TEXT NOT NULL,generation INTEGER NOT NULL,external_id TEXT NOT NULL,
 message_id TEXT NOT NULL,PRIMARY KEY(channel,binding_id,generation,external_id));
CREATE TABLE IF NOT EXISTS channel_mirror_consent(
 channel TEXT NOT NULL,binding_id TEXT NOT NULL,binding_version INTEGER NOT NULL,
 generation INTEGER NOT NULL,created REAL NOT NULL,PRIMARY KEY(channel,binding_id));
"""


def migrate(db):
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    media_columns = {r[1] for r in db.execute("PRAGMA table_info(channel_attachments)")}
    if "binding_version" not in media_columns:
        db.execute(
            "ALTER TABLE channel_attachments ADD COLUMN binding_version INTEGER NOT NULL DEFAULT 0"
        )
    accounts = {r[1] for r in db.execute("PRAGMA table_info(telegram_accounts)")}
    if "next_send" not in accounts:
        db.execute(
            "ALTER TABLE telegram_accounts ADD COLUMN next_send REAL NOT NULL DEFAULT 0"
        )
    columns = {r[1] for r in db.execute("PRAGMA table_info(channel_attachments)")}
    if "channel" not in columns:
        db.execute(
            "ALTER TABLE channel_attachments ADD COLUMN channel TEXT NOT NULL DEFAULT 'weixin'"
        )


class AccountImport(Mutation):
    bot_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    token: SecretStr = Field(min_length=20, max_length=256)
    label: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(default=0, ge=0)
    confirm_credential_storage: Literal[True]
    confirm_provider_terms: Literal[True]


class Bind(Mutation):
    owner_preapproval: OwnerPreapproval | None = Field(default=None, exclude_if=lambda value: value is None)
    account_id: str = Field(min_length=1, max_length=100)
    dot_id: str = Field(min_length=1, max_length=100)
    confirm_external_data_ingress: Literal[True]
    confirm_full_mirror: Literal[True]
    disable_notification: bool = False
    expires_seconds: int = Field(default=86400, ge=300, le=2592000)


class Control(Mutation):
    binding_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    action: Literal["activate", "pause", "resume", "revoke"]
    confirm_user_id: str = Field(default="", max_length=32)
    confirm_chat_id: str = Field(default="", max_length=32)
    confirm_same_owner: bool = False


class MirrorConsent(Mutation):
    confirm_same_owner: Literal[True]
    binding_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    confirm_full_mirror: Literal[True]


class DeliveryControl(Mutation):
    delivery_id: str = Field(min_length=1, max_length=100)
    action: Literal["cancel_unsent", "confirm_received"]
    confirmation_note: str = Field(default="", max_length=1000)


class FileSelection(Mutation):
    binding_id: str = Field(min_length=1, max_length=100)
    expected_binding_version: int = Field(ge=1)
    message_id: str = Field(min_length=1, max_length=100)
    expected_message_version: int = Field(ge=1)
    artifact_ids: list[str] = Field(min_length=1, max_length=16)


class FileApprove(FileSelection):
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm_file_egress: Literal[True]
