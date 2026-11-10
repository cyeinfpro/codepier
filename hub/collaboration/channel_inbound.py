"""Durable per-message quarantine and bounded, scope-fenced backpressure replay."""
import json

from cryptography.fernet import InvalidToken

from hub.collaboration.common import canonical, digest
from shared.util import DevError

BATCH_ERRORS = {
    'TELEGRAM_POLL_FENCE_LOST', 'TELEGRAM_BINDING_CHANGED', 'TELEGRAM_BATCH_LIMIT',
    'TELEGRAM_UPDATE_INVALID', 'TELEGRAM_UPDATE_CONFLICT', 'TELEGRAM_INBOX_QUOTA',
    'WEIXIN_POLL_FENCE_LOST', 'WEIXIN_BINDING_CHANGED', 'WEIXIN_BATCH_LIMIT',
    'WEIXIN_DAILY_QUOTA', 'WEIXIN_DUPLICATE_CONFLICT', 'WEIXIN_RECEIVER_MISMATCH',
    'CHANNEL_QUARANTINE_QUOTA',
}
DEFERRED_ERRORS = {'TELEGRAM_DOT_QUOTA', 'WEIXIN_INBOX_QUOTA'}


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS channel_inbound_failures(
        id TEXT PRIMARY KEY,channel TEXT NOT NULL,account_id TEXT NOT NULL,
        scope TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,reason_code TEXT NOT NULL,
        created REAL NOT NULL,updated REAL NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS channel_inbound_failure_ready
        ON channel_inbound_failures(channel,account_id,state,created,id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS channel_inbound_failure_created
        ON channel_inbound_failures(created)""")


def identity(channel, account, scope, source):
    return digest([channel, account['id'], account['credential_version'],
                   account['generation'], scope, source])


def isolate(service, channel, account, scope, source, ingest, *, replay=False, deferred_evidence=None):
    """Only known message-level failures are isolated; batch/storage faults abort."""
    store, now = service.store, service.c.clock()
    identifier = identity(channel, account, scope, source)
    previous = store.one('SELECT * FROM channel_inbound_failures WHERE id=?', (identifier,))
    if previous and not replay:
        # Older versions could quarantine an account-identity error. Reobserving
        # that receipt must not turn a batch-fatal error into cursor advancement.
        if previous['reason_code'] in BATCH_ERRORS:
            raise DevError(previous['reason_code'], '历史隔离记录包含批次身份或一致性错误；保留原游标供核对', 409)
        return None, previous['state'] + ':' + previous['reason_code']
    try:
        with store.savepoint():
            result = ingest()
    except DevError as exc:
        if exc.code in BATCH_ERRORS or exc.status >= 500:
            raise
        state = 'deferred' if exc.code in DEFERRED_ERRORS else 'quarantined'
        payload = {'source': source, 'scope': scope, 'generation': account['generation'],
                   'credential_version': account['credential_version']}
        if state == 'deferred' and not previous and deferred_evidence:
            payload['authority'] = deferred_evidence()
        body = store.encrypt(canonical(payload))
        if not previous:
            usage = store.one("""SELECT COUNT(*) AS n,COALESCE(SUM(length(body)),0) AS bytes
                FROM channel_inbound_failures WHERE channel=? AND account_id=? AND state!='committed'""", (channel, account['id']))
            if usage['n'] >= 5000 or usage['bytes'] + len(body) > 32 * 1024 * 1024:
                raise DevError('CHANNEL_QUARANTINE_QUOTA', '渠道隔离收件预算已满；保留原游标等待处理', 429) from exc
        store.execute("""INSERT INTO channel_inbound_failures
            (id,channel,account_id,scope,body,state,reason_code,created,updated) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state,reason_code=excluded.reason_code,updated=excluded.updated""",
            (identifier, channel, account['id'], canonical(scope), body, state, exc.code, now, now))
        return None, state + ':' + exc.code
    if previous:
        store.execute("UPDATE channel_inbound_failures SET state='committed',body='',reason_code='',updated=? WHERE id=?",
                      (now, identifier))
    return result, ''


def pending(service, channel, account_id):
    return service.store.all("""SELECT * FROM channel_inbound_failures
        WHERE channel=? AND account_id=? AND state='deferred' ORDER BY created,rowid LIMIT 20""",
        (channel, account_id))


def stale(service, row):
    service.store.execute("""UPDATE channel_inbound_failures SET state='quarantined',
        reason_code='binding_changed_before_replay',updated=? WHERE id=?""", (service.c.clock(), row['id']))


def summary(store, channel, account_id):
    return store.all("""SELECT state,reason_code,COUNT(*) AS count FROM channel_inbound_failures
        WHERE channel=? AND account_id=? AND state!='committed' GROUP BY state,reason_code""",
        (channel, account_id))


def unpack(service, row):
    try:
        value = json.loads(service.store.decrypt(row['body']))
        if (not isinstance(value, dict) or not isinstance(value.get('scope'), dict)
                or not {'source', 'generation', 'credential_version'} <= value.keys()):
            raise ValueError('invalid saved scope')
        return value
    except (ValueError, TypeError, InvalidToken):
        service.store.execute("""UPDATE channel_inbound_failures SET state='quarantined',
            reason_code='saved_payload_invalid',updated=? WHERE id=?""", (service.c.clock(), row['id']))
        return None


def summaries(store, channel, account_ids):
    result = {}
    if not account_ids:
        return result
    for row in store.all("""SELECT account_id,state,reason_code,COUNT(*) AS count
            FROM channel_inbound_failures WHERE channel=?
            AND account_id IN (SELECT value FROM json_each(?)) AND state!='committed'
            GROUP BY account_id,state,reason_code""", (channel, canonical(account_ids))):
        result.setdefault(row.pop('account_id'), []).append(row)
    return result
