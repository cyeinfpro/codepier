"""Forward-only channel ledgers; no old room, grant or subscription changes."""
from typing import Literal

from pydantic import Field, SecretStr, model_validator

from hub.access import AccessModel
from hub.collaboration.weixin_protocol import API_BASE

SCHEMA = '''
CREATE TABLE IF NOT EXISTS weixin_accounts(
 id TEXT PRIMARY KEY,space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,bot_id TEXT NOT NULL UNIQUE,
 label TEXT NOT NULL,api_base TEXT NOT NULL,secret TEXT NOT NULL,state TEXT NOT NULL,
 credential_version INTEGER NOT NULL DEFAULT 1,generation INTEGER NOT NULL DEFAULT 0,
 cursor TEXT NOT NULL,poll_owner TEXT,poll_fence INTEGER NOT NULL DEFAULT 0,poll_until REAL,
 next_poll REAL NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,reason_code TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS weixin_bindings(
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES weixin_accounts(id),space_id TEXT NOT NULL,
 owner_user_id TEXT NOT NULL,room_id TEXT NOT NULL,conversation_id TEXT NOT NULL,project_id TEXT NOT NULL,
 environment_id TEXT NOT NULL,dot_id TEXT NOT NULL,owner_principal TEXT NOT NULL,dot_principal TEXT NOT NULL,
 project_snapshot TEXT NOT NULL,credential_version INTEGER NOT NULL,generation INTEGER NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL,peer_id TEXT NOT NULL DEFAULT '',
 pairing_hash TEXT NOT NULL,pairing_until REAL NOT NULL,pending_peer_id TEXT NOT NULL DEFAULT '',
 allow_replies INTEGER NOT NULL DEFAULT 0,allow_media_outbound INTEGER NOT NULL DEFAULT 0,
 expires_at REAL NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS weixin_one_room ON weixin_bindings(conversation_id) WHERE state!='revoked';
CREATE UNIQUE INDEX IF NOT EXISTS weixin_one_account ON weixin_bindings(account_id) WHERE state!='revoked';
CREATE INDEX IF NOT EXISTS weixin_binding_scope ON weixin_bindings(space_id,owner_user_id,room_id,created,id);
CREATE TABLE IF NOT EXISTS weixin_enrollments(
 id TEXT PRIMARY KEY,space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,room_id TEXT NOT NULL,
 state TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,payload TEXT NOT NULL,expires_at REAL NOT NULL,
 created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS weixin_inbound(
 account_id TEXT NOT NULL,generation INTEGER NOT NULL,peer_id TEXT NOT NULL,external_id TEXT NOT NULL,
 source_digest TEXT NOT NULL,message_id TEXT,disposition TEXT NOT NULL,created REAL NOT NULL,
 PRIMARY KEY(account_id,generation,peer_id,external_id));
CREATE TABLE IF NOT EXISTS weixin_contexts(
 account_id TEXT NOT NULL,generation INTEGER NOT NULL,peer_id TEXT NOT NULL,token TEXT NOT NULL,
 observed_at REAL NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(account_id,generation,peer_id));
CREATE TABLE IF NOT EXISTS weixin_deliveries(
 id TEXT PRIMARY KEY,binding_id TEXT NOT NULL,generation INTEGER NOT NULL,message_id TEXT NOT NULL,
 message_version INTEGER NOT NULL,message_digest TEXT NOT NULL,source_message_id TEXT NOT NULL,
 peer_id TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(binding_id,generation,message_id,message_version));
CREATE TABLE IF NOT EXISTS weixin_parts(
 id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL REFERENCES weixin_deliveries(id),ordinal INTEGER NOT NULL,
 client_id TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,body TEXT NOT NULL,body_digest TEXT NOT NULL,
 state TEXT NOT NULL,next_attempt REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT,lease_fence INTEGER NOT NULL DEFAULT 0,lease_until REAL,
 context_version INTEGER,wire_body TEXT,provider_receipt TEXT,reason_code TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(delivery_id,ordinal));
CREATE INDEX IF NOT EXISTS weixin_pending_parts ON weixin_parts(state,next_attempt,created,id);
CREATE TABLE IF NOT EXISTS channel_attachments(
 id TEXT PRIMARY KEY,schema_version INTEGER NOT NULL DEFAULT 1,space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,
 room_id TEXT NOT NULL,conversation_id TEXT NOT NULL,project_id TEXT NOT NULL,message_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL,direction TEXT NOT NULL,binding_id TEXT NOT NULL,generation INTEGER NOT NULL,
 peer_id TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER,
 sha256 TEXT,artifact_id TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,private_source TEXT NOT NULL,
 content TEXT,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
 lease_owner TEXT,lease_fence INTEGER NOT NULL DEFAULT 0,lease_until REAL,reason_code TEXT NOT NULL DEFAULT '',
 expires_at REAL NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
 UNIQUE(message_id,direction,ordinal));
CREATE INDEX IF NOT EXISTS channel_attachment_pending ON channel_attachments(state,next_attempt,created,id);
CREATE INDEX IF NOT EXISTS channel_attachment_room ON channel_attachments(room_id,message_id,ordinal);
'''


def migrate(db):
    revisions_existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='weixin_inbound_versions'").fetchone() is not None
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    enrollments = {r[1] for r in db.execute('PRAGMA table_info(weixin_enrollments)')}
    if 'conversation_id' not in enrollments:
        db.execute("ALTER TABLE weixin_enrollments ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''")
    columns = {r[1] for r in db.execute('PRAGMA table_info(weixin_bindings)')}
    if 'pairing_secret' not in columns:
        db.execute("ALTER TABLE weixin_bindings ADD COLUMN pairing_secret TEXT NOT NULL DEFAULT ''")
    if 'revalidation_required' not in columns:
        db.execute('ALTER TABLE weixin_bindings ADD COLUMN revalidation_required INTEGER NOT NULL DEFAULT 0')
    accounts = {r[1] for r in db.execute('PRAGMA table_info(weixin_accounts)')}
    if 'last_sync_at' not in accounts:
        # No historical successful poll proof is invented from account.updated.
        db.execute('ALTER TABLE weixin_accounts ADD COLUMN last_sync_at REAL')
    inbound = {r[1] for r in db.execute('PRAGMA table_info(weixin_inbound)')}
    for name in ('provider_version', 'provider_created'):
        if name not in inbound:
            db.execute(f"ALTER TABLE weixin_inbound ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    db.execute('''CREATE TABLE IF NOT EXISTS weixin_inbound_versions(
        account_id TEXT NOT NULL,generation INTEGER NOT NULL,peer_id TEXT NOT NULL,external_id TEXT NOT NULL,
        provider_version TEXT NOT NULL,source_digest TEXT NOT NULL,message_id TEXT,created REAL NOT NULL,
        PRIMARY KEY(account_id,generation,peer_id,external_id,provider_version))''')
    db.execute('CREATE INDEX IF NOT EXISTS weixin_versions_received ON weixin_inbound_versions(account_id,created)')
    db.execute('CREATE INDEX IF NOT EXISTS weixin_versions_message ON weixin_inbound_versions(message_id)')
    if not revisions_existed:
        # Copy only already-recorded facts; an empty version stays unverified.
        db.execute('''INSERT INTO weixin_inbound_versions
            SELECT account_id,generation,peer_id,external_id,provider_version,source_digest,message_id,created
            FROM weixin_inbound''')


class Scope(AccessModel):
    project: str = Field(min_length=1, max_length=100)
    environment_id: str = Field(default='production', min_length=1, max_length=100)
    conversation_id: str = Field(default='', max_length=100)


class Mutation(Scope):
    idempotency_key: str = Field(min_length=8, max_length=128)


class AccountImport(Mutation):
    bot_id: str = Field(min_length=1, max_length=256)
    token: SecretStr = Field(min_length=1, max_length=16384)
    label: str = Field(min_length=1, max_length=80)
    api_base: str = Field(default=API_BASE, max_length=2048)
    expected_version: int = Field(default=0, ge=0)
    confirm_credential_storage: Literal[True]
    confirm_provider_terms: Literal[True]


class Bind(Mutation):
    account_id: str = Field(min_length=1, max_length=100)
    dot_id: str = Field(min_length=1, max_length=100)
    confirm_external_data_ingress: Literal[True]
    allow_replies: bool = False
    allow_media_outbound: bool = False
    expires_seconds: int = Field(default=86400, ge=300, le=30 * 86400)
    lifetime_mode: Literal['timed', 'until_revoked'] = Field(default='timed', exclude_if=lambda value: value == 'timed')
    confirm_until_revoked: bool = Field(default=False, exclude_if=lambda value: not value)

    @model_validator(mode='after')
    def explicit_lifetime(self):
        if self.lifetime_mode == 'until_revoked' and not self.confirm_until_revoked:
            raise ValueError('Until-revoked channel consent must be explicitly confirmed')
        return self


class Control(Mutation):
    binding_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    action: Literal['activate', 'pause', 'resume', 'revoke', 'refresh_pairing']
    confirm_peer_id: str = Field(default='', max_length=256)


class QRStart(Mutation):
    renew_account_id: str = Field(default='', max_length=100, exclude_if=lambda value: not value)
    expected_credential_version: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    confirm_credential_storage: Literal[True]
    confirm_provider_terms: Literal[True]


class QRStatus(Scope):
    enrollment_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    verify_code: str = Field(default='', max_length=32)


class QRCancel(QRStatus):
    idempotency_key: str = Field(min_length=8, max_length=128)


class QRAccept(QRStatus):
    confirm_replace_existing: bool = Field(default=False, exclude_if=lambda value: not value)
    idempotency_key: str = Field(min_length=8, max_length=128)
    label: str = Field(min_length=1, max_length=80)
    confirm_scanner_id: str = Field(min_length=1, max_length=256)
    confirm_credential_storage: Literal[True]
    confirm_provider_terms: Literal[True]


class AttachmentAdd(Mutation):
    message_id: str = Field(min_length=1, max_length=100)
    expected_message_version: int = Field(ge=1)
    artifact_ids: list[str] = Field(min_length=1, max_length=16)
    confirm_file_egress: Literal[True]


class DeliveryControl(Mutation):
    delivery_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    action: Literal['cancel_unsent', 'confirm_received']
    confirmation_note: str = Field(default='', max_length=1000)


class OwnerRequest(Mutation):
    message_id: str = Field(min_length=1, max_length=100)
    expected_message_version: int = Field(ge=1)
    request_text: str = Field(min_length=1, max_length=8000)
    confirm_execution_request: Literal[True]
