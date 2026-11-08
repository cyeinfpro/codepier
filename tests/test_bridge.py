import json, os, subprocess, sys
from pathlib import Path
import httpx
import pytest
from scripts.mcp_stdio_bridge import forward, token_from_file
from tests.support import BASE
from tests.catalog_assertions import assert_task_catalog
from shared.knowledge_contracts import KNOWLEDGE_TOOLS
from shared.core_contracts import CORE_TOOLS
from shared.query_contracts import QUERY_TOOLS

@pytest.mark.parametrize('profile,count',[(None,15),('core',15),('full',15),('coding',11)])
def test_stdio_bridge_initialize_tools_read(stack,tmp_path,profile,count):
    f=tmp_path/'token.txt';f.write_text(stack.pat);f.chmod(0o600)
    env={**os.environ,'CODEPIER_TOKEN_FILE':str(f),'CODEPIER_HUB_URL':stack.url}
    env.pop('CODEPIER_MCP_PROFILE',None)
    env.pop('REMOTE_DEV_MCP_PROFILE',None)
    if profile:env['CODEPIER_MCP_PROFILE']=profile
    requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{}}},
      {'jsonrpc':'2.0','method':'notifications/initialized'},
      {'jsonrpc':'2.0','id':2,'method':'tools/list'},
      {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'read','arguments':{'project':'ProjectAlpha','path':'README.md'}}}]
    p=subprocess.run([sys.executable,'-m','scripts.mcp_stdio_bridge'],input='\n'.join(json.dumps(x) for x in requests)+'\n',text=True,capture_output=True,env=env,cwd=BASE,timeout=15)
    assert p.returncode==0 and stack.pat not in p.stdout+p.stderr
    replies=[json.loads(l) for l in p.stdout.splitlines()];assert len(replies)==3
    assert replies[0]['result']['serverInfo']['name']=='codepier-agent'
    expected = ({'project_query', 'task_query', 'workspace', 'read', 'write',
                 'edit', 'exec', 'process', 'get_profile'} | KNOWLEDGE_TOOLS if profile == 'coding'
                else CORE_TOOLS | QUERY_TOOLS | KNOWLEDGE_TOOLS | {'get_profile', 'get_access_context'})
    assert_task_catalog(replies[1]['result']['tools'], count, expected_names=expected)
    names={t['name'] for t in replies[1]['result']['tools']}
    assert expected == names
    assert not {'integration_control','validations_accept'} & names
    assert all('securitySchemes' not in t.get('_meta',{}) for t in replies[1]['result']['tools'])
    assert '# ProjectAlpha' in replies[2]['result']['structuredContent']['content']

@pytest.mark.parametrize('profile', [None, 'core', 'full', 'coding'])
@pytest.mark.parametrize('method', ['initialize', 'tools/list', 'tools/call', 'notifications/initialized'])
def test_bridge_routes_every_request_to_selected_profile(profile, method):
    sent = []
    request = {'jsonrpc': '2.0', 'method': method}
    if method != 'notifications/initialized':
        request['id'] = 7
    result = ({'protocolVersion': '2025-11-25'} if method == 'initialize'
              else {'tools': []} if method == 'tools/list' else {})

    def handler(http_request):
        sent.append(http_request)
        if len(sent) == 1:
            return httpx.Response(503)
        if 'id' not in request:
            return httpx.Response(202)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 7, 'result': result})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        options = {} if profile is None else {'profile': profile}
        response = forward(client, 'http://fixture', request, {}, sleep=lambda _: None, **options)
    assert len(sent) == 2
    assert all(str(item.url) == 'http://fixture/mcp?profile=' + (profile or 'core') for item in sent)
    assert all(json.loads(item.content) == request for item in sent)
    assert response == (None if 'id' not in request else {'jsonrpc': '2.0', 'id': 7, 'result': result})


def test_bridge_private_token_permissions(tmp_path):
    p=tmp_path/'token';p.write_text('rd_'+'x'*43);p.chmod(0o600);assert token_from_file(p).startswith('rd_')
    if os.name!='nt':
        p.chmod(0o644)
        with pytest.raises(ValueError):token_from_file(p)

def test_bridge_no_missing_token_secret_output(tmp_path):
    p=subprocess.run([sys.executable,'-m','scripts.mcp_stdio_bridge'],env={**os.environ,'CODEPIER_TOKEN_FILE':str(tmp_path/'absent')},capture_output=True,text=True,cwd=BASE)
    assert p.returncode==2 and not p.stdout
