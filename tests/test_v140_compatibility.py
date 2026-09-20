import json
import os
import subprocess
from pathlib import Path
import pytest
from shared.contracts import tool_definitions


@pytest.mark.parametrize('mode',['legacy','auto'])
def test_independent_official_sdk_matches_real_http_contracts(stack,tmp_path,mode):
    python=os.environ.get('MCP_COMPAT_PYTHON')
    if not python:
        pytest.skip('Set MCP_COMPAT_PYTHON to isolated environment installed from requirements-compat.txt')
    token=tmp_path/'client-token.txt';token.write_text(stack.pat);token.chmod(0o600)
    script=Path(__file__).resolve().parents[1]/'scripts/check_mcp_compat.py'
    result=subprocess.run([python,str(script),'--url',stack.url+'/mcp','--token-file',str(token),'--project','Imago','--mode',mode],
        text=True,capture_output=True,timeout=90)
    assert stack.pat not in result.stdout+result.stderr
    assert result.returncode==0,result.stderr[-7000:]
    data=json.loads(result.stdout.strip().splitlines()[-1])
    assert data['tool_count']==len(tool_definitions())
    assert data['protocol_version']==('2025-11-25' if mode=='legacy' else '2026-07-28')
    assert any(c['is_error'] for c in data['checks'])
    assert all(c['schema_valid'] for c in data['checks'])
    evidence=os.environ.get('MCP_COMPAT_EVIDENCE')
    if evidence:
        path=Path(evidence);path.mkdir(parents=True,exist_ok=True)
        (path/('official-sdk-'+mode+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2))
