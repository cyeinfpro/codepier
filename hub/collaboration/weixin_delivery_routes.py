"""Owner-approved recovery of unsent Weixin parts after same-peer re-pairing."""

import json

from cryptography.fernet import InvalidToken

from hub.collaboration.common import canonical, digest
from shared.util import DevError

QUARANTINED = {'credential_changed', 'credential_generation_changed'}
SUCCESSFUL = {'accepted', 'owner_confirmed'}


def unsent_quarantined(part):
    # Provider attempts, including a frozen rate-limited request, belong to
    # their original credential generation. Never replay them on a new route.
    return part['state'] == 'cancelled' and part['reason_code'] in QUARANTINED and part['attempts'] == 0


def _scope_changed():
    return DevError('WEIXIN_RECOVERY_SCOPE_CHANGED', '原微信回复的内容、收件人或当前回传授权已变化', 409)


def route_snapshot(service, delivery):
    store, collaboration = service.store, service.c
    native = collaboration.room_channels.native
    binding = store.one('SELECT * FROM weixin_bindings WHERE id=?', (delivery['binding_id'],))
    if (delivery['kind'] != 'reply' or not binding or not binding.get('native_member_id')
            or binding['generation'] <= delivery['generation'] or binding['peer_id'] != delivery['peer_id']
            or not binding['allow_replies']):
        raise _scope_changed()
    binding, account, owner, room, _ = native.valid(binding)
    message = store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                        (delivery['message_id'], room['id']))
    source = store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                       (delivery['source_message_id'], room['id']))
    if not message or not source or source['origin'] != 'external_channel' or source['state'] != 'saved':
        raise _scope_changed()
    body = json.loads(message['body'])
    source_body = json.loads(source['body'])
    origin = source_body.get('external_origin', {})
    if (message['origin'] != 'bound_connector' or message['version'] != delivery['message_version']
            or digest(body) != delivery['message_digest'] or origin.get('provider_deleted')
            or not origin.get('native_room') or origin.get('channel') != 'weixin'
            or any(origin.get(key) != expected for key, expected in (
                ('binding_id', delivery['binding_id']), ('generation', delivery['generation']),
                ('account_id', account['id']), ('peer_id', delivery['peer_id'])))):
        raise _scope_changed()
    collaboration.chatroom.require_message_visible(owner, message)
    collaboration.chatroom.require_message_visible(owner, source)
    history = store.one('''SELECT * FROM weixin_inbound WHERE account_id=? AND generation=? AND peer_id=? AND external_id=?''',
                        (account['id'], delivery['generation'], delivery['peer_id'], origin.get('external_message_id')))
    if (not history or history['message_id'] != source['id']
            or history['provider_version'] != origin.get('provider_version')):
        raise _scope_changed()
    current = store.one('''SELECT provider_version,source_digest FROM weixin_inbound
        WHERE account_id=? AND generation=? AND peer_id=? AND external_id=?''',
        (account['id'], binding['generation'], delivery['peer_id'], origin.get('external_message_id')))
    if current and any(current[key] != history[key] for key in ('provider_version', 'source_digest')):
        raise _scope_changed()

    member = collaboration.rooms.member(body.get('sender_member_id', ''), room['id'])
    if member['grant_id'] != message['author']:
        raise _scope_changed()
    actor = collaboration.rooms.principal(member)
    collaboration.rooms.member_scope(actor, room['id'], member['id'], member['binding_generation'])
    projects = set(body.get('provenance_project_ids', []))
    scope_digest = ''
    if message.get('goal_id') or projects or body.get('dot_reply_complete') is False:
        auth = store.one("SELECT * FROM room_channel_owner_authorizations WHERE binding_id=? AND state='active'",
                         (binding['id'],))
        approved = native.approved_scope(auth, binding, owner) if auth else None
        if not approved:
            raise _scope_changed()
        current_scopes = collaboration.rooms.scopes(member, actor)
        authorized_scopes = {(row['project_id'], row['environment_id']) for row in approved['projects']}
        authorized_scopes &= {(row['project_id'], row['environment_id']) for row in current_scopes if row['capabilities']}
        provenance = {(row['project_id'], row['environment_id']) for row in body.get('provenance_scopes', [])}
        if not provenance <= authorized_scopes or not projects <= {project for project, _ in authorized_scopes}:
            raise _scope_changed()
        scope_digest = digest([auth['id'], auth['version'], approved])
    evidence = {'binding_id': binding['id'], 'binding_version': binding['version'],
                'channel_generation': binding['generation'], 'credential_version': account['credential_version'],
                'account_id': account['id'], 'peer_id': binding['peer_id'], 'conversation_id': room['id'],
                'source_id': source['id'], 'source_version': source['version'], 'source_digest': digest(source_body),
                'sender_member_id': member['id'], 'member_generation': member['binding_generation'],
                'current_scope_digest': scope_digest}
    return (binding, account, owner, message), evidence


def attachment(service, delivery, part, scope):
    binding, _, owner, message = scope
    if not binding['allow_media_outbound']:
        raise _scope_changed()
    identifier = json.loads(service.store.decrypt(part['body']))['attachment_id']
    row = service.store.one('''SELECT a.* FROM channel_attachments a JOIN channel_attachment_transfers t
        ON t.attachment_id=a.id AND t.part_id=? AND t.channel='weixin'
        WHERE a.id=? AND a.owner_user_id=? AND a.space_id=?''', (part['id'], identifier, owner.user_id, owner.space_id))
    if (not row or row['direction'] != 'outbound' or row['state'] != 'ready'
            or row['expires_at'] <= service.c.clock()
            or (row['binding_id'], row['generation'], row['peer_id'], row['conversation_id'], row['message_id'])
            != (delivery['binding_id'], delivery['generation'], delivery['peer_id'], binding['conversation_id'], message['id'])):
        raise _scope_changed()
    service.c.runtime.project(row['project_id'], owner)
    return row


def proof(service, delivery, part, scope, evidence):
    try:
        payload = json.loads(service.store.decrypt(part['body']))
    except (InvalidToken, ValueError) as exc:
        raise _scope_changed() from exc
    if part['body_digest'] != digest(payload):
        raise _scope_changed()
    saved = {**evidence, 'delivery_id': delivery['id'], 'original_generation': delivery['generation'],
             'message_digest': delivery['message_digest'], 'client_id': part['client_id'],
             'body_digest': part['body_digest']}
    if part['kind'] == 'attachment':
        row = attachment(service, delivery, part, scope)
        saved.update(attachment_id=row['id'], attachment_digest=row['sha256'])
    return canonical(saved)


def authorize_parts(service, delivery, part_ids, principal):
    scope, evidence = route_snapshot(service, delivery)
    selected = set(part_ids)
    parts = service.store.all('SELECT * FROM weixin_parts WHERE delivery_id=? ORDER BY ordinal', (delivery['id'],))
    authorized = {}
    settled_before = True
    for part in parts:
        if part['id'] in selected:
            if not settled_before or not unsent_quarantined(part):
                raise DevError('WEIXIN_RECOVERY_PART_UNAVAILABLE', '仅能恢复已证实未发出的连续分项，请先核对前面的分项', 409)
            authorized[part['id']] = proof(service, delivery, part, scope, evidence)
        elif part['state'] not in SUCCESSFUL:
            settled_before = False
    if set(authorized) != selected or not authorized:
        raise DevError('WEIXIN_RECOVERY_PART_UNAVAILABLE', '没有选择属于此原回复且可安全恢复的分项', 409)
    now = service.c.clock()
    for identifier, saved in authorized.items():
        service.store.execute('''INSERT INTO weixin_part_route_recoveries
            (part_id,channel_generation,evidence,approved_by,approved_at) VALUES (?,?,?,?,?)''',
            (identifier, evidence['channel_generation'], saved, principal.user_id, now))
        service.store.execute("""UPDATE weixin_parts SET state='queued',reason_code='route_recovery_approved',
            next_attempt=?,lease_fence=lease_fence+1,lease_until=0,wire_body=NULL,context_version=NULL,updated=?
            WHERE id=?""", (now, now, identifier))
    return len(authorized)


def validate_part(service, delivery, part):
    if not part:
        return None
    saved = service.store.one('''SELECT * FROM weixin_part_route_recoveries
        WHERE part_id=? ORDER BY channel_generation DESC LIMIT 1''', (part['id'],))
    if not saved:
        return None
    try:
        scope, evidence = route_snapshot(service, delivery)
    except DevError as exc:
        if exc.code in {'ROOM_PAUSED', 'WEIXIN_BINDING_PAUSED'}:
            raise
        raise _scope_changed() from exc
    if (part['delivery_id'] != delivery['id'] or saved['approved_by'] != scope[2].user_id
            or saved['channel_generation'] != evidence['channel_generation']
            or saved['evidence'] != proof(service, delivery, part, scope, evidence)):
        raise _scope_changed()
    return scope


def available_part(service, delivery_id):
    # Show only the next admissible part. A batch can explicitly include a
    # contiguous suffix, but can never jump over an unknown/rejected part.
    part = service.store.one("""SELECT * FROM weixin_parts WHERE delivery_id=?
        AND state NOT IN ('accepted','owner_confirmed') ORDER BY ordinal LIMIT 1""", (delivery_id,))
    if not part or not unsent_quarantined(part):
        return None
    delivery = service.store.one('SELECT * FROM weixin_deliveries WHERE id=?', (delivery_id,))
    try:
        scope, evidence = route_snapshot(service, delivery)
        proof(service, delivery, part, scope, evidence)
    except DevError:
        return None
    return part['id']
