import json
import os
import subprocess
from pathlib import Path
import pytest
from shared.contracts import tool_definitions
from tests.support import running_stack


@pytest.mark.parametrize('profile',[None,'core','full','coding'])
@pytest.mark.parametrize('mode',['legacy','auto'])
def test_independent_official_sdk_matches_real_http_contracts(stack,tmp_path,mode,profile):
    python=os.environ.get('MCP_COMPAT_PYTHON')
    if not python:
        pytest.skip('Set MCP_COMPAT_PYTHON to isolated environment installed from requirements-compat.txt')
    token=tmp_path/'client-token.txt';token.write_text(stack.pat);token.chmod(0o600)
    script=Path(__file__).resolve().parents[1]/'scripts/check_mcp_compat.py'
    url=stack.url+'/mcp'+('' if profile is None else '?profile='+profile)
    result=subprocess.run([python,str(script),'--url',url,'--token-file',str(token),'--project','ProjectAlpha','--mode',mode],
        text=True,capture_output=True,timeout=90)
    assert stack.pat not in result.stdout+result.stderr
    assert result.returncode==0,result.stderr[-7000:]
    data=json.loads(result.stdout.strip().splitlines()[-1])
    expected={'query','act','get_profile'} if profile is None else {tool['name'] for tool in tool_definitions(profile)}
    assert data['tool_count']==len(expected)
    assert set(data['tool_names'])==expected
    assert data['catalog']==('unified' if profile is None else 'legacy')
    assert data['protocol_version']==('2025-11-25' if mode=='legacy' else '2026-07-28')
    assert any(c['is_error'] for c in data['checks'])
    assert all(c['schema_valid'] in (True,None) for c in data['checks'])
    if profile is None:
        assert any(c['tool']=='query' and c['action']=='file' for c in data['checks'])
        assert any(c['tool']=='query' and c['action']=='job' for c in data['checks'])
        assert any(c['tool']=='read' and not c['advertised'] for c in data['checks'])
        assert all(c['schema_valid'] is None for c in data['checks'])
    else:
        assert data['output_schema_count']==len(expected)
        assert all(c['advertised'] and c['schema_valid'] is True for c in data['checks'])
    evidence=os.environ.get('MCP_COMPAT_EVIDENCE')
    if evidence:
        path=Path(evidence);path.mkdir(parents=True,exist_ok=True)
        (path/('official-sdk-'+mode+'-'+(profile or 'default-unified')+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2))


@pytest.mark.parametrize('mode',['legacy','auto'])
def test_independent_official_sdk_read_only_unified_catalog(tmp_path,monkeypatch,mode):
    python=os.environ.get('MCP_COMPAT_PYTHON')
    if not python:
        pytest.skip('Set MCP_COMPAT_PYTHON to isolated environment installed from requirements-compat.txt')
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED','false')
    with running_stack(tmp_path/'read-only-stack') as stack:
        grant=stack.must(stack.client.post('/api/grants',json={
            'label':'SDK read-only fixture','scopes':['read'],'projects':[stack.project['id']],'days':1}))
        credential=grant['token']
        catalog=stack.must(stack.rpc('tools/list',token_value=credential))['result']['tools']
        assert {tool['name'] for tool in catalog}=={'query','get_profile'}
        token=tmp_path/'read-only-client-token.txt';token.write_text(credential);token.chmod(0o600)
        script=Path(__file__).resolve().parents[1]/'scripts/check_mcp_compat.py'
        result=subprocess.run([python,str(script),'--url',stack.url+'/mcp','--token-file',str(token),
            '--project','ProjectAlpha','--mode',mode],text=True,capture_output=True,timeout=90)
        assert credential not in result.stdout+result.stderr
        assert stack.pat not in result.stdout+result.stderr
        assert result.returncode==0,result.stderr[-7000:]
        data=json.loads(result.stdout.strip().splitlines()[-1])
        assert data['catalog']=='unified' and data['tool_count']==2
        assert set(data['tool_names'])=={'query','get_profile'} and 'act' not in data['tool_names']
        assert data['protocol_version']==('2025-11-25' if mode=='legacy' else '2026-07-28')
        assert any(c['tool']=='query' and c['action']=='file' and c['is_error'] for c in data['checks'])
        assert any(c['tool']=='read' and not c['advertised'] for c in data['checks'])
        assert all(c['schema_valid'] is None for c in data['checks'])
        denied=stack.mcp('write',{'project':'ProjectAlpha','path':'sdk-forbidden.txt','content':'not allowed',
            'expected_sha256':'new','idempotency_key':'sdk-read-only-denied'},token_value=credential)
        assert denied['isError'] and denied['structuredContent']['error']['code']=='INSUFFICIENT_SCOPE'
        assert not (stack.projectalpha/'sdk-forbidden.txt').exists()
        outside=stack.mcp('query',{'action':'file','project':'ProjectBeta','path':'README.md'},token_value=credential)
        assert outside['isError']
        protected=stack.mcp('query',{'action':'file','project':'ProjectAlpha','path':'.env'},token_value=credential)
        assert protected['isError'] and protected['structuredContent']['error']['code']=='PROTECTED_PATH'
        evidence=os.environ.get('MCP_COMPAT_EVIDENCE')
        if evidence:
            path=Path(evidence);path.mkdir(parents=True,exist_ok=True)
            (path/('official-sdk-'+mode+'-read-only-unified.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2))
