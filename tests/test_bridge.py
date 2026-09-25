import json, os, subprocess, sys
from pathlib import Path
import pytest
from scripts.mcp_stdio_bridge import token_from_file
from tests.support import BASE
from tests.catalog_assertions import assert_task_catalog

@pytest.mark.parametrize('profile,count',[(None,9),('coding',9)])
def test_stdio_bridge_initialize_tools_read(stack,tmp_path,profile,count):
    f=tmp_path/'token.txt';f.write_text(stack.pat);f.chmod(0o600)
    env={**os.environ,'CODEPIER_TOKEN_FILE':str(f),'CODEPIER_HUB_URL':stack.url}
    env.pop('CODEPIER_MCP_PROFILE',None)
    if profile:env['CODEPIER_MCP_PROFILE']=profile
    requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{}}},
      {'jsonrpc':'2.0','method':'notifications/initialized'},
      {'jsonrpc':'2.0','id':2,'method':'tools/list'},
      {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'read','arguments':{'project':'Imago','path':'README.md'}}}]
    p=subprocess.run([sys.executable,'-m','scripts.mcp_stdio_bridge'],input='\n'.join(json.dumps(x) for x in requests)+'\n',text=True,capture_output=True,env=env,cwd=BASE,timeout=15)
    assert p.returncode==0 and stack.pat not in p.stdout+p.stderr
    replies=[json.loads(l) for l in p.stdout.splitlines()];assert len(replies)==3
    assert replies[0]['result']['serverInfo']['name']=='codepier-agent'
    assert_task_catalog(replies[1]['result']['tools'], count)
    names={t['name'] for t in replies[1]['result']['tools']}
    assert {'workspace','read','write','edit','exec','process','browser','computer','vps'} == names
    assert not {'integration_control','validations_accept'} & names
    assert all('securitySchemes' not in t.get('_meta',{}) for t in replies[1]['result']['tools'])
    assert '# Imago' in replies[2]['result']['structuredContent']['content']

def test_bridge_private_token_permissions(tmp_path):
    p=tmp_path/'token';p.write_text('rd_'+'x'*43);p.chmod(0o600);assert token_from_file(p).startswith('rd_')
    if os.name!='nt':
        p.chmod(0o644)
        with pytest.raises(ValueError):token_from_file(p)

def test_bridge_no_missing_token_secret_output(tmp_path):
    p=subprocess.run([sys.executable,'-m','scripts.mcp_stdio_bridge'],env={**os.environ,'CODEPIER_TOKEN_FILE':str(tmp_path/'absent')},capture_output=True,text=True,cwd=BASE)
    assert p.returncode==2 and not p.stdout
