"""Durable owner-scoped channel switches; never credentials or execution grants."""
from typing import Literal

from pydantic import Field, StrictBool

from hub.collaboration.common import validate
from hub.collaboration.weixin_schema import Mutation
from shared.util import DevError


class ChannelSettingsControl(Mutation):
    channel: Literal['weixin', 'telegram']
    enabled: StrictBool
    expected_version: int = Field(ge=0)


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS collaboration_channel_settings(
        space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,channel TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),version INTEGER NOT NULL,
        updated REAL NOT NULL,PRIMARY KEY(space_id,owner_user_id,channel))""")


class ChannelSettings:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    @staticmethod
    def identity(owner):
        if isinstance(owner, dict):
            return owner['space_id'], owner['owner_user_id']
        return owner.space_id, owner.user_id

    def default(self, channel):
        if channel not in {'weixin', 'telegram'}:
            raise DevError('CHANNEL_UNSUPPORTED', '不支持此通道', 422)
        return bool(getattr(self.c.config, channel + '_enabled'))

    def setting(self, channel, owner):
        default = self.default(channel)
        row = self.store.one("""SELECT * FROM collaboration_channel_settings
            WHERE space_id=? AND owner_user_id=? AND channel=?""",
            (*self.identity(owner), channel))
        # Environment values are bootstrap preferences. Only an explicit panel
        # action can override one; reading status never persists or starts login.
        import os
        return {'channel': channel, 'enabled': bool(row['enabled']) if row else default,
                'version': row['version'] if row else 0,
                'source': 'panel' if row else 'environment' if
                    'CODEPIER_' + channel.upper() + '_ENABLED' in os.environ else 'default',
                'scope': 'owner', 'default_enabled': channel == 'weixin'}

    def enabled(self, channel, owner):
        return bool(self.c.config.enabled and self.setting(channel, owner)['enabled'])

    def require_enabled(self, channel, owner):
        if not self.enabled(channel, owner):
            raise DevError(channel.upper() + '_DISABLED',
                           '通道已关闭；请由空间管理员在面板开启', 409)

    def sql_enabled(self, channel, alias):
        # All inputs are fixed internal SQL identifiers, never user strings.
        default = int(self.default(channel) and self.c.config.enabled)
        if channel not in {'weixin', 'telegram'} or not alias.isidentifier():
            raise ValueError('Invalid internal channel SQL scope')
        if not self.c.config.enabled:
            return '0'
        return ("COALESCE((SELECT enabled FROM collaboration_channel_settings cs "
                f"WHERE cs.space_id={alias}.space_id AND cs.owner_user_id={alias}.owner_user_id "
                f"AND cs.channel='{channel}'),{default})=1")

    def control(self, raw, principal):
        args = validate(ChannelSettingsControl, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            self.c.conversations.resolve(principal, room, args)

            def save():
                current = self.setting(args['channel'], principal)
                if current['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '通道开关已变化，请刷新后重试', 409)
                now = self.c.clock()
                self.store.execute("""INSERT INTO collaboration_channel_settings
                    (space_id,owner_user_id,channel,enabled,version,updated) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(space_id,owner_user_id,channel) DO UPDATE SET
                    enabled=excluded.enabled,version=excluded.version,updated=excluded.updated""",
                    (principal.space_id, principal.user_id, args['channel'], int(args['enabled']),
                     current['version'] + 1, now))
                channel = args['channel']
                # Invalidate any in-flight poll before returning the new state.
                # Credentials, bindings, provider cursors and grants stay intact.
                self.store.execute(f"""UPDATE {channel}_accounts
                    SET poll_fence=poll_fence+1,poll_until=0,next_poll=0
                    WHERE space_id=? AND owner_user_id=?""",
                    (principal.space_id, principal.user_id))
                if not args['enabled']:
                    self.pause(channel, principal)
                else:
                    # Resume only downloads paused by this switch. Fetch workers
                    # still revalidate the original binding, grant and expiry.
                    # Outbound work must never be revived by a channel toggle.
                    self.store.execute("""UPDATE channel_attachments SET state='pending',
                        reason_code='',next_attempt=0,lease_fence=lease_fence+1,lease_until=0,updated=?
                        WHERE channel=? AND space_id=? AND owner_user_id=?
                        AND direction='inbound' AND state='blocked' AND reason_code='channel_disabled'""",
                        (now, channel, principal.space_id, principal.user_id))
                result = self.setting(channel, principal)
                self.c.audit(room, principal.actor, 'channel.setting_changed', channel,
                    {'channel': channel, 'enabled': args['enabled'], 'version': result['version'],
                     'credentials_retained': True, 'bindings_retained': True,
                     'creates_mcp_grant': False, 'automatic_resend': False})
                return {**result, 'setting': result, 'credentials_retained': True,
                        'bindings_retained': True, 'automatic_resend': False}
            return self.c.mutation(principal, room, 'channel_settings', args, save)

    def pause(self, channel, principal):
        now = self.c.clock()
        bindings = self.store.all(f"""SELECT id FROM {channel}_bindings
            WHERE space_id=? AND owner_user_id=?""", (principal.space_id, principal.user_id))
        for binding in bindings:
            if channel == 'weixin':
                pending = self.store.all("""SELECT p.id,p.delivery_id FROM weixin_parts p
                    JOIN weixin_deliveries d ON d.id=p.delivery_id WHERE d.binding_id=?
                    AND p.state IN ('queued','waiting_context','waiting_media','preparing','sending')""",
                    (binding['id'],))
                for part in pending:
                    self.store.execute("""UPDATE weixin_parts SET
                        state=CASE WHEN state='sending' THEN 'unknown' ELSE 'cancelled' END,
                        reason_code='channel_disabled',lease_fence=lease_fence+1,lease_until=0,updated=?
                        WHERE id=?""", (now, part['id']))
                    self.c.weixin.delivery.refresh(part['delivery_id'])
            self.c.channels.quarantine(channel, binding['id'], 'channel_disabled')
            # A send already submitted to a provider cannot be recalled or safely
            # retried. Invalidate its local lease and retain an unknown receipt.
            rows = self.store.all("""SELECT d.id FROM channel_mirrors d
                WHERE d.channel=? AND d.binding_id=?""", (channel, binding['id']))
            for row in rows:
                self.store.execute("""UPDATE channel_mirror_parts SET state='unknown',
                    reason_code='channel_disabled_during_send',lease_fence=lease_fence+1,lease_until=0
                    WHERE delivery_id=? AND state='sending'""", (row['id'],))
                self.c.channels.refresh(row['id'])
        self.store.execute("""UPDATE channel_attachments SET state='blocked',
            reason_code='channel_disabled',lease_fence=lease_fence+1,lease_until=0,updated=?
            WHERE channel=? AND space_id=? AND owner_user_id=? AND state IN ('pending','fetching')""",
            (now, channel, principal.space_id, principal.user_id))
        if channel == 'weixin':
            self.store.execute("""UPDATE weixin_enrollments SET state='cancelled',payload='',
                version=version+1,updated=? WHERE space_id=? AND owner_user_id=?
                AND state NOT IN ('accepted','cancelled','expired','attention','binded_redirect','verify_code_blocked')""",
                (now, principal.space_id, principal.user_id))
