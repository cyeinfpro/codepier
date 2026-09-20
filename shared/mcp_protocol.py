"""MCP 2026 request metadata/header rules shared with the stdio bridge.

JSON-RPC IDs are transport correlation, never business idempotency keys.
"""
from __future__ import annotations
import base64,binascii,re
from shared.util import VERSION

MODERN='2026-07-28'
LEGACY=frozenset({'2025-03-26','2025-06-18','2025-11-25'})
SUPPORTED=[MODERN,'2025-11-25','2025-06-18','2025-03-26']
PREFIX='io.modelcontextprotocol/'
SERVER_INFO={'name':'codepier-agent','version':VERSION,'title':'CodePier'}
MIME='text/html;profile=mcp-app'

class ProtocolError(Exception):
    def __init__(self,code,message,data=None):self.code,self.message,self.data=code,message,data


def encode_header(value):
    if not isinstance(value,str):raise ValueError('Header source must be a string')
    safe=value==value.strip() and all(c=='\t' or 32<=ord(c)<=126 for c in value)
    if safe and not (value.startswith('=?base64?') and value.endswith('?=')):return value
    return '=?base64?'+base64.b64encode(value.encode('utf-8')).decode('ascii')+'?='


def decode_header(value):
    if not isinstance(value,str) or len(value)>16384 or value!=value.strip() or any(c!='\t' and not 32<=ord(c)<=126 for c in value):
        raise ProtocolError(-32020,'Invalid request metadata header')
    if value.startswith('=?base64?') and value.endswith('?='):
        try:return base64.b64decode(value[9:-2],validate=True).decode('utf-8')
        except (ValueError,UnicodeError,binascii.Error) as exc:raise ProtocolError(-32020,'Malformed encoded request header') from exc
    return value


def is_modern(body,headers=None):
    params=body.get('params') if isinstance(body,dict) else None
    meta=params.get('_meta') if isinstance(params,dict) else None
    if isinstance(meta,dict) and PREFIX+'protocolVersion' in meta:return True
    version=headers.get('mcp-protocol-version') if headers is not None else None
    return version is not None and version not in LEGACY or isinstance(body,dict) and body.get('method')=='server/discover'


def validate_modern(body,headers):
    params=body.get('params',{});meta=params.get('_meta') if isinstance(params,dict) else None
    if not isinstance(meta,dict):raise ProtocolError(-32602,'Per-request _meta is required')
    version=meta.get(PREFIX+'protocolVersion');caps=meta.get(PREFIX+'clientCapabilities')
    if not isinstance(version,str) or not version or not isinstance(caps,dict):
        raise ProtocolError(-32602,'protocolVersion and clientCapabilities are required on every request')
    if version!=MODERN:
        raise ProtocolError(-32022,'Unsupported protocol version for per-request metadata',{'supported':SUPPORTED,'requested':version})
    info=meta.get(PREFIX+'clientInfo')
    if info is not None and (not isinstance(info,dict) or not isinstance(info.get('name'),str) or not isinstance(info.get('version'),str)):
        raise ProtocolError(-32602,'Invalid clientInfo')
    level=meta.get(PREFIX+'logLevel')
    if level is not None and level not in {'debug','info','notice','warning','error','critical','alert','emergency'}:
        raise ProtocolError(-32602,'Invalid logLevel')
    if not isinstance(caps.get('extensions',{}),dict):raise ProtocolError(-32602,'Invalid extension capabilities')
    for name,value in caps.get('extensions',{}).items():
        if not isinstance(value,dict) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9.-]*/[A-Za-z0-9_.-]+',name):
            raise ProtocolError(-32602,'Invalid extension declaration')
    if headers.get('mcp-protocol-version')!=version or headers.get('mcp-method')!=body['method']:
        raise ProtocolError(-32020,'MCP-Protocol-Version and Mcp-Method must match the request body')
    field='uri' if body['method']=='resources/read' else 'name' if body['method'] in {'tools/call','prompts/get'} else None
    if field:
        value=params.get(field)
        if not isinstance(value,str):raise ProtocolError(-32602,'Required request name/uri must be a string')
        if decode_header(headers.get('mcp-name'))!=value:raise ProtocolError(-32020,'Mcp-Name must match the request body')
    # No MRTR capability is advertised. Never accept a purported approval continuation.
    if 'requestState' in params or 'inputResponses' in params:
        raise ProtocolError(-32602,'This server does not issue MRTR input requests')
    return meta


def request_headers(body):
    params=body.get('params',{});meta=params.get('_meta',{})
    version=meta.get(PREFIX+'protocolVersion')
    result={'MCP-Protocol-Version':version,'Mcp-Method':body['method']}
    field='uri' if body['method']=='resources/read' else 'name' if body['method'] in {'tools/call','prompts/get'} else None
    if field and isinstance(params.get(field),str):result['Mcp-Name']=encode_header(params[field])
    return result


def complete(result):
    return {**result,'resultType':'complete','_meta':{**result.get('_meta',{}),PREFIX+'serverInfo':SERVER_INFO}}


def capabilities():
    return {'tools':{},'resources':{},'prompts':{},'extensions':{'io.modelcontextprotocol/ui':{'mimeTypes':[MIME]}}}
