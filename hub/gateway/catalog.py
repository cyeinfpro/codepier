"""Reviewed, bounded tool descriptors; no backend-controlled Hub identity/meta."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

from jsonschema import Draft202012Validator
from shared.util import DevError, valid_json_value
from shared.role_contracts import ROLE_SCOPE

TOOL_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,128}$')
ALIAS = re.compile(r'^[a-z][a-z0-9_]{0,19}$')
MAX_TOOLS = 128
MAX_SCHEMA_BYTES = 65536
PAGE_SIZE = 100
STATUS_TOOL = 'gateway_call_get'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def check_schema(schema):
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        raise DevError('GATEWAY_SCHEMA_INVALID', '工具 schema 必须是 object', 502)
    if len(encoded(schema).encode()) > MAX_SCHEMA_BYTES or not valid_json_value(schema):
        raise DevError('GATEWAY_SCHEMA_LIMIT', '工具 schema 超过安全大小或嵌套限制', 502)
    stack = [schema]
    count = 0
    while stack:
        value = stack.pop(); count += 1
        if count > 4096:
            raise DevError('GATEWAY_SCHEMA_LIMIT', '工具 schema 结构过大', 502)
        if isinstance(value, dict):
            for key in ('$ref', '$dynamicRef'):
                if key in value and (not isinstance(value[key], str) or not value[key].startswith('#')):
                    raise DevError('GATEWAY_SCHEMA_REFERENCE', '不允许从外部 URL 加载 schema', 502)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise DevError('GATEWAY_SCHEMA_INVALID', '后端工具 schema 无效', 502) from exc
    return schema


def review_tools(raw):
    if not isinstance(raw, list) or len(raw) > MAX_TOOLS:
        raise DevError('GATEWAY_CATALOG_LIMIT', '后端工具数量超过限制', 502)
    result, names = [], set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not TOOL_NAME.fullmatch(item['name']):
            raise DevError('GATEWAY_TOOL_INVALID', '后端工具名称无效', 502)
        if item['name'] in names:
            raise DevError('GATEWAY_TOOL_COLLISION', '后端返回重复工具名称', 502)
        names.add(item['name'])
        description = item.get('description', '')
        if not isinstance(description, str) or len(description) > 8192:
            raise DevError('GATEWAY_TOOL_INVALID', '后端工具描述无效', 502)
        tool = {'name': item['name'], 'description': description,
                'inputSchema': check_schema(item.get('inputSchema'))}
        if 'outputSchema' in item:
            tool['outputSchema'] = check_schema(item['outputSchema'])
        # Deliberately conservative. A backend's self-declared read-only hint is
        # not reviewed permission and cannot suppress client confirmation.
        tool['annotations'] = {'readOnlyHint': False, 'destructiveHint': True,
                               'idempotentHint': False, 'openWorldHint': True}
        result.append(tool)
    return sorted(result, key=lambda tool: tool['name'])


def public_name(alias, name):
    full = alias + '__' + name
    if len(full) > 64:
        full = full[:51] + '_' + hashlib.sha256(full.encode()).hexdigest()[:12]
    return full


def definition(alias, tool):
    value = dict(tool)
    value['name'] = public_name(alias, tool['name'])
    value['description'] = f'[{alias}] ' + tool['description']
    schemes = [{'type': 'oauth2', 'scopes': [ROLE_SCOPE]}]
    value['securitySchemes'] = schemes
    value['_meta'] = {'securitySchemes': schemes}
    return value


def status_definition():
    return {'name': STATUS_TOOL, 'description': 'Read the receipt of a gateway call using its original call_id. Never executes or retries the backend operation. Results remain bound to the original grant and current permission.',
            'inputSchema': {'type': 'object', 'properties': {'call_id': {'type': 'string', 'maxLength': 64}},
                            'required': ['call_id'], 'additionalProperties': False},
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False},
            '_meta': {'securitySchemes': [{'type': 'oauth2', 'scopes': [ROLE_SCOPE]}]}}


def validate_arguments(tool, arguments, *, schema_check=True):
    if not isinstance(arguments, dict) or not valid_json_value(arguments) or len(encoded(arguments).encode()) > 262144:
        raise DevError('GATEWAY_ARGUMENTS_INVALID', '工具参数必须是有界 JSON object')
    if not schema_check:
        return
    try:
        Draft202012Validator(tool['inputSchema']).validate(arguments)
    except Exception as exc:
        # ValidationError.message can contain passwords or literal arguments.
        raise DevError('GATEWAY_ARGUMENTS_INVALID', '参数不符合已发布的工具 schema') from exc


def page(tools, cursor, identity, secret):
    tools = sorted(tools, key=lambda item: item['name'])
    names = [tool['name'] for tool in tools]
    if len(names) != len(set(names)):
        raise DevError('GATEWAY_TOOL_COLLISION', '工具名称冲突；未公开混淆的目录', 409)
    revision = fingerprint([identity, tools])
    offset = 0
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 512:
                raise ValueError()
            body, signature = cursor.split('.')
            expected = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError()
            data = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
            if data['revision'] != revision or data['expires'] < time.time():
                raise ValueError()
            offset = data['offset']
            if type(offset) is not int or offset < 0 or offset >= len(tools):
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise DevError('GATEWAY_CATALOG_CHANGED', '目录或授权已变化；请从第一页重新发现工具', 409) from exc
    result = {'tools': tools[offset:offset + PAGE_SIZE]}
    if offset + PAGE_SIZE < len(tools):
        body = base64.urlsafe_b64encode(encoded({'revision': revision, 'offset': offset + PAGE_SIZE,
                                               'expires': int(time.time()) + 600}).encode()).decode().rstrip('=')
        result['nextCursor'] = body + '.' + hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return result
