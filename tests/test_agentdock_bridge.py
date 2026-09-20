import copy
import json

import httpx
import pytest
from scripts.mcp_stdio_bridge import IDEMPOTENT_TOOLS, REMOTE_TOOLS, forward, prepare_request
from shared.contracts import TOOLS


def test_bridge_catalog_is_derived_not_duplicated():
    assert REMOTE_TOOLS == {n for n,t in TOOLS.items() if not t.local}
    assert IDEMPOTENT_TOOLS == {n for n,t in TOOLS.items() if 'idempotency_key' in t.model.model_fields}


@pytest.mark.parametrize('name',['project_context','workflows_create','workflows_update'])
def test_new_tool_transport_retries_keep_same_key_and_original_request(name):
    original={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{}}}
    saved=copy.deepcopy(original)
    prepared=prepare_request(original)
    assert original == saved
    assert prepared['params']['arguments']['idempotency_key'].startswith('bridge-')
    attempts=[]
    def handler(request):
        attempts.append(json.loads(request.content))
        if len(attempts)==1:raise httpx.ReadTimeout('Reply lost',request=request)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':1,'result':{'isError':False}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        forward(client,'http://fixture',prepared,{},sleep=lambda _:None)
    assert attempts[0]==attempts[1]
    assert prepare_request(prepared)==prepared


@pytest.mark.parametrize('name',['workflows_get','workflows_list','operations_wait','operations_get','projects_resolve'])
def test_keyless_tools_remain_keyless(name):
    req={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{}}}
    assert prepare_request(req)==req


def test_minimal_bridge_requirements_cover_shared_registry():
    from pathlib import Path
    requirements=(Path(__file__).resolve().parents[1]/'requirements-bridge.txt').read_text()
    assert 'httpx==' in requirements and 'pydantic==' in requirements
