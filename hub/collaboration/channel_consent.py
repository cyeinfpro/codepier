"""Purpose-specific consent evidence; no migration creates an approval."""
from typing import Literal

from pydantic import Field, model_validator

from hub.access import AccessModel

CONSENT_VERSION = 'channel-consent-v1'
OWNER_PURPOSES = ('owner_identity', 'future_tasks', 'result_delivery', 'until_revoked', 'pairing_notices')


class OwnerPreapproval(AccessModel):
    text_version: Literal['channel-consent-v1']
    expected_scope_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_peer_id: str = Field(default='', max_length=256)
    confirm_owner_identity: Literal[True]
    confirm_future_tasks: Literal[True]
    confirm_result_delivery: Literal[True]
    confirm_until_revoked: Literal[True]
    confirm_pairing_notices: Literal[True]

    @model_validator(mode='before')
    @classmethod
    def explicit_purposes(cls, value):
        if not isinstance(value, dict) or any(value.get('confirm_' + purpose) is not True for purpose in OWNER_PURPOSES):
            raise ValueError('Every displayed purpose needs its own explicit evidence; storage or terms are not task consent')
        return value


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS channel_consent_evidence(
        receipt_id TEXT NOT NULL,purpose TEXT NOT NULL,channel TEXT NOT NULL,
        owner_user_id TEXT NOT NULL,space_id TEXT NOT NULL,room_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,subject_id TEXT NOT NULL,text_version TEXT NOT NULL,
        approved INTEGER NOT NULL CHECK(approved=1),detail TEXT NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(receipt_id,purpose))''')
    db.execute('''CREATE TABLE IF NOT EXISTS channel_enrollment_authorizations(
        binding_id TEXT PRIMARY KEY,channel TEXT NOT NULL,receipt_id TEXT NOT NULL,
        state TEXT NOT NULL,scope TEXT NOT NULL,scope_digest TEXT NOT NULL,
        expected_peer_id TEXT NOT NULL,expires_at REAL NOT NULL,created REAL NOT NULL,
        completed_at REAL,reason_code TEXT NOT NULL DEFAULT '')''')
    # Original private-message identity is independent of delivery envelopes.
    # Keep only metadata, not message content; no migration grants authority.
    db.execute('''CREATE TABLE IF NOT EXISTS channel_enrollment_inputs(
        channel TEXT NOT NULL,account_id TEXT NOT NULL,generation INTEGER NOT NULL,
        peer_id TEXT NOT NULL,external_id TEXT NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(channel,account_id,generation,peer_id,external_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS channel_onboarding_notices(
        id TEXT PRIMARY KEY,channel TEXT NOT NULL,binding_id TEXT NOT NULL,account_id TEXT NOT NULL,
        generation INTEGER NOT NULL,credential_version INTEGER NOT NULL,peer_id TEXT NOT NULL,
        source_external_id TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,
        binding_version INTEGER NOT NULL,authorization_version INTEGER NOT NULL DEFAULT 0,
        lease_fence INTEGER NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,
        not_before REAL NOT NULL DEFAULT 0,expires_at REAL NOT NULL,reason_code TEXT NOT NULL DEFAULT '',
        provider_receipt TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL,
        UNIQUE(channel,account_id,generation,peer_id,source_external_id,kind))''')
    db.execute('CREATE INDEX IF NOT EXISTS channel_onboarding_ready ON channel_onboarding_notices(state,not_before,created)')
    columns = {r[1] for r in db.execute('PRAGMA table_info(telegram_bindings)')}
    if 'lifetime_mode' not in columns:
        db.execute("ALTER TABLE telegram_bindings ADD COLUMN lifetime_mode TEXT NOT NULL DEFAULT 'timed'")
