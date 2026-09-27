"""Live IAM/Role checks for one immutable binding, never actions x resources."""
from __future__ import annotations

import json

from hub import iam
from hub.access_profiles import refresh_profile_principal
from hub.roles import role_binding
from hub.gateway.catalog import fingerprint
from shared.util import DevError


def grant_context(store, principal):
    principal = refresh_profile_principal(store, principal)
    if not principal.grant_id or principal.authorization_mode != 'role':
        raise DevError('GATEWAY_ROLE_REQUIRED', '外部 MCP 仅接受明确同意的动态 Role 凭据', 403)
    grant = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
    consent = store.one('SELECT * FROM gateway_consents WHERE grant_id=?', (principal.grant_id,))
    if not consent or consent['consent_version'] != 1 or any(consent[key] != grant[key] for key in ('user_id', 'space_id', 'profile_id', 'role_id')):
        raise DevError('GATEWAY_CONSENT_REQUIRED', '请由此凭据的账号在面板明确同意外部 MCP 委派', 403)
    role, policy, _ = role_binding(store, grant)
    if not role['enabled']:
        raise DevError('GATEWAY_POLICY_DENIED', '当前角色已暂停', 403)
    return principal, policy


def accessible(account, principal):
    return account['space_id'] == principal.space_id and (account['sharing'] == 'space' or account['owner_user_id'] == principal.user_id)


def binding_rows(store, binding_id, space_id):
    binding = store.one('SELECT * FROM gateway_bindings WHERE id=? AND space_id=?', (binding_id, space_id))
    account = store.one('SELECT * FROM gateway_accounts WHERE id=? AND space_id=?', (binding['account_id'], space_id)) if binding else None
    connector = store.one('SELECT * FROM gateway_connectors WHERE id=? AND space_id=?', (account['connector_id'], space_id)) if account else None
    if not binding or not account or not connector:
        raise DevError('GATEWAY_TOOL_NOT_FOUND', '工具或连接不存在于当前授权范围', 404)
    return binding, account, connector


def require_tool(store, principal, binding_id, tool_name):
    principal, policy = grant_context(store, principal)
    if not any(rule.binding_id == binding_id and tool_name in rule.tools for rule in policy.connector_rules):
        raise DevError('GATEWAY_POLICY_DENIED', '当前角色未允许此工具与连接组合', 403)
    binding, account, connector = binding_rows(store, binding_id, principal.space_id)
    if not accessible(account, principal) or not all(row['enabled'] for row in (binding, account, connector)):
        raise DevError('GATEWAY_POLICY_DENIED', '连接已暂停、移除或不属于此账号', 403)
    tool = next((tool for tool in json.loads(binding['tools']) if tool['name'] == tool_name), None)
    if tool is None:
        raise DevError('GATEWAY_TOOL_NOT_FOUND', '工具未获批准发布', 404)
    current = next((item for item in json.loads(account['catalog']) if item['name'] == tool_name), None)
    if current is None or fingerprint(current) != fingerprint(tool):
        raise DevError('GATEWAY_SCHEMA_REVIEW_REQUIRED', '发现的工具定义已改变；请审核并重新发布', 409)
    return principal, binding, account, connector, tool


def visible_tools(store, principal):
    try:
        principal, policy = grant_context(store, principal)
    except DevError:
        return []
    visible, seen = [], set()
    # Bound by RolePolicy (32 rules) and gateway catalog limits. No cross-await
    # permission cache or backend discovery request is performed by tools/list.
    for rule in policy.connector_rules:
        try:
            binding, account, connector = binding_rows(store, rule.binding_id, principal.space_id)
        except DevError:
            continue
        if not accessible(account, principal) or not all(row['enabled'] for row in (binding, account, connector)):
            continue
        discovered = {tool['name']: fingerprint(tool) for tool in json.loads(account['catalog'])}
        for tool in json.loads(binding['tools']):
            key = (binding['id'], tool['name'])
            if tool['name'] in rule.tools and key not in seen and discovered.get(tool['name']) == fingerprint(tool):
                seen.add(key); visible.append((binding, tool))
    return visible


def consent_grant(store, principal, grant_id, expected_role_version):
    """Explicit downstream delegation, in the same transaction as issuance."""
    import time
    store.require_transaction()
    grant = store.one('SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?',
                      (grant_id, principal.user_id, principal.space_id))
    if not grant or grant['authorization_mode'] != 'role':
        raise DevError('GATEWAY_ROLE_REQUIRED', '外部 MCP 委派要求动态 Role 授权', 403)
    iam.validate_grant(store, grant)
    role, _, _ = role_binding(store, grant)
    if not role['enabled'] or role['version'] != expected_role_version:
        raise DevError('ROLE_CHANGED', '角色政策已改变，请重新阅读再同意', 409)
    store.db.execute("""INSERT INTO gateway_consents(grant_id,user_id,space_id,profile_id,role_id,consent_version,created)
        VALUES(?,?,?,?,?,1,?) ON CONFLICT(grant_id) DO UPDATE SET consent_version=1,created=excluded.created""",
        (grant_id, principal.user_id, principal.space_id, grant['profile_id'], grant['role_id'], time.time()))
    store.audit(principal.actor, 'gateway.delegation_consented', grant_id,
                detail={'role_id': role['id'], 'role_version': role['version'], 'future_reviewed_connector_rules': True}, commit=False)
