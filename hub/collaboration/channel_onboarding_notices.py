"""Bounded, durable onboarding receipts, not collaboration messages or tasks.

An accepted provider call is not proof of delivery or reading. An interrupted
send becomes unknown and is never automatically replayed. Only definite rate
limit rejections can be retried. No address/context is invented from QR identity.
"""
from __future__ import annotations

import asyncio
import json
import uuid

from hub.collaboration.common import canonical
from shared.util import DevError


class OnboardingNotices:
    def __init__(self, enrollment):
        self.e, self.c, self.store = enrollment, enrollment.c, enrollment.store

    def queue(self, channel, binding, peer, external_id, kind, value, *, authorization_version=0):
        if not peer or not external_id or kind not in ('connected', 'pairing_required'):
            raise DevError('CHANNEL_NOTICE_SCOPE', '接入回执必须有真实原私聊地址与入站消息', 422)
        # Successful activation must not send a stale "not connected" reminder.
        if kind == 'connected':
            self.store.execute("UPDATE channel_onboarding_notices SET state='cancelled',reason_code='pairing_completed' WHERE binding_id=? AND kind='pairing_required' AND state IN ('queued','waiting_context')", (binding['id'],))
        now = self.c.clock()
        self.store.execute('''INSERT OR IGNORE INTO channel_onboarding_notices
            (id,channel,binding_id,account_id,generation,credential_version,peer_id,source_external_id,kind,body,
             state,binding_version,authorization_version,expires_at,created,updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?)''',
            (uuid.uuid4().hex, channel, binding['id'], binding['account_id'], binding['generation'],
             binding['credential_version'], peer, str(external_id), kind, self.store.encrypt(value),
             binding['version'], authorization_version, now + (86400 if kind == 'connected' else 900), now, now))

    def validate(self, row):
        binding, account, owner, room, dot = self.e.transport(row['channel']).valid(
            row['binding_id'], pairing=row['kind'] == 'pairing_required')
        if (row['generation'] != binding['generation'] or row['credential_version'] != account['credential_version']
                or row['binding_version'] != binding['version'] or row['account_id'] != account['id']
                or row['expires_at'] <= self.c.clock()):
            raise DevError('CHANNEL_NOTICE_STALE', '原接入回执的身份、世代或有效期已变化', 403)
        if row['kind'] == 'pairing_required':
            self.e.validate_pending(row['channel'], binding, owner, room, dot, row['peer_id'])
        else:
            auth = self.c.channel_owner.current(binding['id'])
            if not auth or auth['state'] != 'active' or auth['version'] != row['authorization_version']:
                raise DevError('CHANNEL_OWNER_REVOKED', '原本人任务授权已变化，停止旧成功回执', 403)
            self.c.channel_owner.live(auth)
            if row['peer_id'] != binding.get('peer_id', binding.get('chat_id')):
                raise DevError('CHANNEL_NOTICE_PEER_CHANGED', '回执不能改投其他私聊', 403)
        return binding, account

    def claim(self, identifier):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM channel_onboarding_notices WHERE id=?', (identifier,))
            now = self.c.clock()
            if not row or row['state'] not in ('queued', 'waiting_context') or row['not_before'] > now:
                return None
            try:
                binding, account = self.validate(row)
            except DevError as exc:
                self.store.execute("UPDATE channel_onboarding_notices SET state='cancelled',reason_code=?,updated=? WHERE id=?",
                                   (exc.code, now, identifier))
                return None
            context = ''
            if row['channel'] == 'weixin':
                saved = self.store.one('SELECT token FROM weixin_contexts WHERE account_id=? AND generation=? AND peer_id=?',
                                       (row['account_id'], row['generation'], row['peer_id']))
                if not saved:
                    self.store.execute("UPDATE channel_onboarding_notices SET state='waiting_context',not_before=?,updated=? WHERE id=?", (now + 5, now, identifier))
                    return None
                context = self.store.decrypt(saved['token'])
            if row['channel'] == 'telegram':
                if account['next_send'] > now:
                    self.store.execute('UPDATE channel_onboarding_notices SET not_before=? WHERE id=?', (account['next_send'], identifier))
                    return None
                self.store.execute('UPDATE telegram_accounts SET next_send=? WHERE id=?', (now + 1.1, account['id']))
            # No asynchronous I/O between this last check and sending state.
            fence = row['lease_fence'] + 1
            self.store.execute("UPDATE channel_onboarding_notices SET state='sending',lease_fence=?,lease_until=?,updated=? WHERE id=?",
                               (fence, now + 60, now, identifier))
            return {**row, 'lease_fence': fence, 'account': account, 'binding': binding, 'context': context}

    def finish(self, claim, receipt='', error=None):
        with self.store.transaction():
            row = self.store.one('SELECT * FROM channel_onboarding_notices WHERE id=?', (claim['id'],))
            if not row or row['state'] != 'sending' or row['lease_fence'] != claim['lease_fence']:
                return
            state, reason, delay = 'accepted', '', 0
            if error:
                reason = error.code if isinstance(error, DevError) else 'transport_unknown'
                if isinstance(error, DevError) and error.status == 429:
                    state, delay = 'queued', min(86400, max(1, error.details.get('retry_after_seconds', 60)))
                    if claim['channel'] == 'telegram':
                        self.store.execute('UPDATE telegram_accounts SET next_send=MAX(next_send,?) WHERE id=?',
                                           (self.c.clock() + delay, claim['account_id']))
                elif isinstance(error, DevError) and error.code in ('TELEGRAM_PROVIDER_REJECTED', 'WEIXIN_AUTH_REJECTED'):
                    state = 'rejected'
                else:
                    state = 'unknown'
            self.store.execute('''UPDATE channel_onboarding_notices SET state=?,reason_code=?,provider_receipt=?,
                not_before=?,lease_until=0,updated=? WHERE id=?''',
                (state, reason, canonical(receipt) if receipt else '', self.c.clock() + delay, self.c.clock(), row['id']))

    async def send_once(self, identifier):
        claim = await self.store.run(self.claim, identifier)
        if not claim:
            return
        try:
            # Recheck current authority after returning from the store worker;
            # a revoked/changed record is not a license to send its old summary.
            await self.store.run(self.validate, claim)
        except DevError as exc:
            await self.store.run(
                self.store.execute,
                "UPDATE channel_onboarding_notices SET state='cancelled',reason_code=?,lease_until=0 WHERE id=? AND state='sending' AND lease_fence=?",
                (exc.code, claim['id'], claim['lease_fence']))
            return
        try:
            value = self.store.decrypt(claim['body'])
            if claim['channel'] == 'telegram':
                client = self.c.telegram.client_factory(self.store.decrypt(claim['account']['secret']))
                receipt = await client.send(claim['peer_id'], value,
                    silent=claim['binding']['disable_notification'], reply_to=claim['source_external_id'])
            else:
                client = self.c.weixin.client_factory(claim['account']['api_base'], self.store.decrypt(claim['account']['secret']))
                receipt = await client.send(claim['peer_id'], claim['context'], 'cpo-' + claim['id'],
                                             [{'type': 1, 'text_item': {'text': value}}])
            await self.store.run(self.finish, claim, receipt)
        except asyncio.CancelledError:
            await self.store.run(self.finish, claim, '', DevError('CHANNEL_NOTICE_CANCELLED', '发送中断，结果未知', 503))
            raise
        except Exception as exc:
            await self.store.run(self.finish, claim, '', exc)

    async def tick(self):
        def pending():
            with self.store.transaction():
                self.store.execute("UPDATE channel_onboarding_notices SET state='unknown',reason_code='sender_lease_expired',updated=? WHERE state='sending' AND lease_until<=?", (self.c.clock(), self.c.clock()))
                return self.store.all('''SELECT id FROM (
                    SELECT id,created,ROW_NUMBER() OVER (PARTITION BY channel,account_id ORDER BY created,id) AS lane
                    FROM channel_onboarding_notices WHERE state IN ('queued','waiting_context') AND not_before<=?)
                    ORDER BY lane,created,id LIMIT 16''', (self.c.clock(),))
        for row in await self.store.run(pending):
            await self.send_once(row['id'])
