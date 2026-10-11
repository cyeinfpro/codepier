"""Owner-scoped, expiring iLink QR enrollment; no account until final consent.

Provider reference: Tencent/openclaw-weixin c6643f6f0ac03574eb126f2d69569ea4267cc3c6.
No background login, credential disclosure, arbitrary redirect or automatic bind.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import re
import uuid

from hub.collaboration.common import canonical
from hub.collaboration import weixin_schema as models
from hub.collaboration.weixin_protocol import API_BASE, approved_url, text
from hub.room_scope import snapshot as project_snapshot
from shared.util import DevError

TTL = 300
POLL_LEASE = 50
ACTIVE = {'starting', 'wait', 'scaned', 'need_verifycode', 'confirmed'}
TERMINAL = {'accepted', 'expired', 'cancelled', 'attention', 'verify_code_blocked', 'binded_redirect'}


class WeixinEnrollment:
    def __init__(self, weixin):
        self.w, self.c, self.store = weixin, weixin.c, weixin.store
        self.expire()

    def expire(self):
        # Keep a small, secret-free receipt after expiry. The encrypted provider
        # token and QR challenge never outlive the enrollment window.
        self.store.execute("""UPDATE weixin_enrollments SET state='expired',payload='',
            version=version+1,updated=? WHERE expires_at<=? AND state IN
            ('starting','wait','scaned','need_verifycode','confirmed')""",
            (self.c.clock(), self.c.clock()))

    def row(self, identifier, principal, room, conversation):
        row = self.store.one("""SELECT * FROM weixin_enrollments
            WHERE id=? AND space_id=? AND owner_user_id=? AND room_id=? AND conversation_id=?""",
            (identifier, principal.space_id, principal.user_id, room['id'], conversation))
        if not row:
            raise DevError('WEIXIN_ENROLLMENT_NOT_FOUND', '当前房主与话题没有此扫码请求', 404)
        return row

    def payload(self, row):
        return json.loads(self.store.decrypt(row['payload'])) if row['payload'] else {}

    def view(self, row):
        body = self.payload(row)
        result = {'enrollment_id': row['id'], 'state': row['state'], 'version': row['version'],
                  'expires_at': row['expires_at'], 'reason_code': body.get('reason_code', ''),
                  'credential_stored': row['state'] == 'accepted', 'room_bound': False}
        if row['state'] in {'wait', 'scaned', 'need_verifycode'} and body.get('image'):
            result['qrcode_image'] = body['image']
        if row['state'] == 'confirmed':
            result.update(scanner_id=body['scanner_id'], bot_id=body['bot_id'])
        if row['state'] == 'accepted':
            result = {**body.get('account', {}), **result}
        return result

    def update(self, row, state, body):
        self.store.execute("""UPDATE weixin_enrollments SET state=?,payload=?,version=version+1,
            updated=? WHERE id=? AND version=?""",
            (state, self.store.encrypt(canonical(body)) if body else '', self.c.clock(),
             row['id'], row['version']))
        return self.store.one('SELECT * FROM weixin_enrollments WHERE id=?', (row['id'],))

    def check_mapping(self, body, room, principal):
        project = self.c.runtime.project(room['project_id'], principal)
        if body.get('project_snapshot') != project_snapshot(project):
            raise DevError('WEIXIN_MAPPING_CHANGED', '扫码期间项目范围变化，请取消后重新开始', 409)

    def prepare_start(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.w.owner(raw, principal, models.QRStart)
            self.expire()

            def create():
                count = self.store.one("""SELECT COUNT(*) AS n FROM weixin_enrollments
                    WHERE space_id=? AND owner_user_id=? AND created>?""",
                    (principal.space_id, principal.user_id, self.c.clock() - 3600))['n']
                if count >= 20:
                    raise DevError('WEIXIN_QR_RATE_LIMIT', '扫码请求过于频繁，请稍后再试', 429)
                active = self.store.one("""SELECT id FROM weixin_enrollments
                    WHERE space_id=? AND owner_user_id=? AND room_id=? AND conversation_id=?
                    AND state IN ('starting','wait','scaned','need_verifycode','confirmed')""",
                    (principal.space_id, principal.user_id, room['id'], args['conversation_id']))
                if active:
                    return {'enrollment_id': active['id']}
                identifier, now = uuid.uuid4().hex, self.c.clock()
                body = {'project_snapshot': project_snapshot(self.c.runtime.project(room['project_id'], principal)),
                        'api_base': API_BASE, 'attempted': False}
                self.store.execute("""INSERT INTO weixin_enrollments
                    (id,space_id,owner_user_id,room_id,conversation_id,state,payload,expires_at,created,updated)
                    VALUES (?,?,?,?,?,'starting',?,?,?,?)""",
                    (identifier, principal.space_id, principal.user_id, room['id'], args['conversation_id'],
                     self.store.encrypt(canonical(body)), now + TTL, now, now))
                return {'enrollment_id': identifier}

            saved = self.c.mutation(principal, room, 'weixin_qr_start', args, create)
            row = self.row(saved['enrollment_id'], principal, room, args['conversation_id'])
            body = self.payload(row)
            claim = None
            if row['state'] == 'starting' and not body.get('attempted'):
                self.check_mapping(body, room, principal)
                body['attempted'] = True
                body['start_fence'] = uuid.uuid4().hex
                self.store.execute('UPDATE weixin_enrollments SET payload=? WHERE id=?',
                                   (self.store.encrypt(canonical(body)), row['id']))
                claim = {**body, 'id': row['id'], 'version': row['version']}
            return args, principal, self.view(row), claim

    def finish_start(self, args, principal, claim, response=None, error=None):
        with self.store.transaction():
            args, principal, room = self.w.owner(args, principal, models.QRStart)
            self.expire()
            row = self.row(claim['id'], principal, room, args['conversation_id'])
            body = self.payload(row)
            if row['state'] != 'starting' or body.get('start_fence') != claim['start_fence']:
                return self.view(row)
            self.check_mapping(body, room, principal)
            if error is not None:
                return self.view(self.update(row, 'attention', {'reason_code': 'WEIXIN_QR_START_UNCONFIRMED'}))
            try:
                self.provider_ok(response)
                qrcode = text(response.get('qrcode'), 8192)
                content = text(response.get('qrcode_img_content'), 4096)
                from hub.collaboration.qr_image import qr_svg
                image = 'data:image/svg+xml;base64,' + base64.b64encode(qr_svg(content).encode()).decode()
            except (DevError, ValueError, TypeError):
                return self.view(self.update(row, 'attention', {'reason_code': 'WEIXIN_QR_PAYLOAD_INVALID'}))
            body.update(qrcode=qrcode, image=image, poll_fence=0, poll_until=0)
            body.pop('start_fence', None)
            return self.view(self.update(row, 'wait', body))

    async def start(self, raw, principal):
        args, principal, view, claim = await self.store.run(self.prepare_start, raw, principal)
        if not claim:
            return view
        try:
            response = await self.w.client_factory(API_BASE, '').qr_start()
        except asyncio.CancelledError:
            # The durable attempted marker prevents another caller from issuing
            # a second external start if this response was lost.
            raise
        except Exception:
            return await self.store.run(self.finish_start, args, principal, claim, None, True)
        return await self.store.run(self.finish_start, args, principal, claim, response)

    @staticmethod
    def provider_ok(response):
        if not isinstance(response, dict) or any(
                type(response.get(k, 0)) is not int or response.get(k, 0) != 0 for k in ('ret', 'errcode')):
            raise DevError('WEIXIN_QR_REJECTED', '微信未确认此扫码操作，请核对后重试', 502)

    def prepare_status(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.w.owner(raw, principal, models.QRStatus)
            self.expire()
            row = self.row(args['enrollment_id'], principal, room, args['conversation_id'])
            if args['expected_version'] > row['version']:
                raise DevError('STALE_VERSION', '扫码版本无效，请重新核对状态', 409)
            body = self.payload(row)
            if (row['state'] not in {'wait', 'scaned', 'need_verifycode'}
                    or args['expected_version'] < row['version']
                    or body.get('poll_until', 0) > self.c.clock()):
                return args, principal, self.view(row), None
            self.check_mapping(body, room, principal)
            code = args['verify_code'].strip()
            if code and (row['state'] != 'need_verifycode' or not re.fullmatch(r'[0-9]{1,32}', code)):
                raise DevError('WEIXIN_VERIFY_CODE_INVALID', '仅在微信要求时输入手机显示的数字', 422)
            # Do not re-submit the same verification after response loss.
            # Only a bounded digest is retained, never the verification digits.
            if code:
                import hashlib
                fingerprint = hashlib.sha256((row['id'] + code).encode()).hexdigest()
                if body.get('verify_digest') == fingerprint:
                    code = ''
                else:
                    body['verify_digest'] = fingerprint
            body['poll_fence'] = body.get('poll_fence', 0) + 1
            body['poll_until'] = self.c.clock() + POLL_LEASE
            self.store.execute('UPDATE weixin_enrollments SET payload=? WHERE id=?',
                               (self.store.encrypt(canonical(body)), row['id']))
            return args, principal, self.view(row), {
                'id': row['id'], 'version': row['version'], 'poll_fence': body['poll_fence'],
                'base': body['api_base'], 'qrcode': body['qrcode'], 'verify_code': code}

    def finish_status(self, args, principal, claim, response=None, error=None):
        with self.store.transaction():
            args, principal, room = self.w.owner(args, principal, models.QRStatus)
            self.expire()
            row = self.row(claim['id'], principal, room, args['conversation_id'])
            body = self.payload(row)
            if (row['state'] not in {'wait', 'scaned', 'need_verifycode'}
                    or row['version'] != claim['version'] or body.get('poll_fence') != claim['poll_fence']):
                return self.view(row)
            self.check_mapping(body, room, principal)
            body['poll_until'] = 0
            if error:
                if isinstance(error, dict) and error.get('status') in {401, 403}:
                    return self.view(self.update(row, 'attention', {'reason_code': 'WEIXIN_QR_AUTH_REJECTED'}))
                body['reason_code'] = 'WEIXIN_QR_STATUS_UNAVAILABLE'
                delay = error.get('retry_after_seconds', 2) if isinstance(error, dict) else 2
                body['poll_until'] = self.c.clock() + min(3600, max(2, delay))
                return self.view(self.update(row, row['state'], body))
            try:
                self.provider_ok(response)
                state = response.get('status')
                body.pop('reason_code', None)
                if state == 'confirmed':
                    token = text(response.get('bot_token'), 16384)
                    if any(ord(c) < 33 or ord(c) > 126 for c in token):
                        raise ValueError()
                    body = {'token': token, 'bot_id': text(response.get('ilink_bot_id'), 256),
                            'scanner_id': text(response.get('ilink_user_id'), 256),
                            'api_base': approved_url(response.get('baseurl') or body['api_base'],
                                bases=self.w.allowed_api_bases, exact_base=True),
                            'project_snapshot': body['project_snapshot']}
                elif state == 'scaned_but_redirect':
                    host = text(response.get('redirect_host'), 2048)
                    if '/' in host or '?' in host or '#' in host or '@' in host:
                        raise ValueError()
                    body['api_base'] = approved_url('https://' + host, bases=self.w.allowed_api_bases, exact_base=True)
                    body['reason_code'] = 'WEIXIN_QR_APPROVED_NODE_CHANGED'
                    state = 'scaned'
                elif state in {'expired', 'verify_code_blocked', 'binded_redirect'}:
                    body = {}
                elif state not in {'wait', 'scaned', 'need_verifycode'}:
                    raise ValueError()
                # A scan acknowledgment cannot regress after a late long poll.
                if row['state'] == 'scaned' and state == 'wait':
                    state = 'scaned'
            except (DevError, ValueError, TypeError):
                state, body = 'attention', {'reason_code': 'WEIXIN_QR_RESPONSE_UNSUPPORTED'}
            return self.view(self.update(row, state, body))

    async def status(self, raw, principal):
        args, principal, view, claim = await self.store.run(self.prepare_status, raw, principal)
        if not claim:
            return view
        try:
            response = await self.w.client_factory(claim['base'], '').qr_status(claim['qrcode'], claim['verify_code'])
        except asyncio.CancelledError:
            raise
        except DevError as exc:
            error = {'status': exc.status, 'retry_after_seconds': exc.details.get('retry_after_seconds', 2)}
            return await self.store.run(self.finish_status, args, principal, claim, None, error)
        except Exception:
            return await self.store.run(self.finish_status, args, principal, claim, None, True)
        return await self.store.run(self.finish_status, args, principal, claim, response)

    def cancel(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.w.owner(raw, principal, models.QRCancel, enabled=False)
            self.expire()
            row = self.row(args['enrollment_id'], principal, room, args['conversation_id'])

            def save():
                # Close/Cancel wins even when a preceding status response was
                # lost. Cancellation never touches an already accepted account.
                if row['state'] == 'accepted':
                    return self.view(row)
                if args['expected_version'] > row['version']:
                    raise DevError('STALE_VERSION', '扫码版本无效', 409)
                return self.view(self.update(row, 'cancelled', {}))
            return self.c.mutation(principal, room, 'weixin_qr_cancel', args, save)

    def accept(self, raw, principal):
        with self.store.transaction():
            args, principal, room = self.w.owner(raw, principal, models.QRAccept)
            self.expire()
            row = self.row(args['enrollment_id'], principal, room, args['conversation_id'])

            def save():
                if row['state'] != 'confirmed' or row['version'] != args['expected_version']:
                    raise DevError('WEIXIN_QR_NOT_CONFIRMED', '扫码尚未确认或已过期，请重新核对', 409)
                body = self.payload(row)
                self.check_mapping(body, room, principal)
                if not hmac.compare_digest(body['scanner_id'], args['confirm_scanner_id']):
                    raise DevError('WEIXIN_SCANNER_MISMATCH', '请确认本次扫码实际返回的微信身份', 409)
                # Existing accounts require their explicit renewal flow; a QR
                # scan cannot silently revoke an existing room's credentials.
                if self.store.one('SELECT id FROM weixin_accounts WHERE bot_id=?', (body['bot_id'],)):
                    raise DevError('WEIXIN_ACCOUNT_EXISTS', '此机器人已有登记，请核对原账号；本次扫码未覆盖凭据', 409)
                result = self.w.import_account({
                    'project': args['project'], 'environment_id': args['environment_id'],
                    'conversation_id': args['conversation_id'], 'bot_id': body['bot_id'],
                    'token': body['token'], 'label': args['label'], 'api_base': body['api_base'],
                    'expected_version': 0, 'confirm_credential_storage': True,
                    'confirm_provider_terms': True, 'idempotency_key': 'qr-account-' + row['id']}, principal)
                self.update(row, 'accepted', {'account': result})
                self.c.audit(room, principal.actor, 'weixin.qr_accepted', row['id'],
                             {'account_id': result['account_id'], 'room_bound': False})
                return {**result, 'enrollment_id': row['id'], 'state': 'accepted', 'version': row['version'] + 1}
            return self.c.mutation(principal, room, 'weixin_qr_accept', args, save)
