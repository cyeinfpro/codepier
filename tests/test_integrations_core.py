"""Integration security and immutable evidence tests. All data is temporary."""
from __future__ import annotations
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from jsonschema import Draft202012Validator, ValidationError as SchemaError
from pydantic import ValidationError
from agent.incoming_artifacts import import_artifact, validate_url, DEFAULT_HOSTS, PublicTLSConnection
from agent.integration_state import Records
from agent.lsp_navigation import Projector
from agent.source_versions import source_version, Validations
from shared.contracts import TOOLS, OUTPUT_SCHEMAS, tool_definitions
from shared.integration_contracts import REMOTE_TOOLS, ADMIN_TOOLS
from shared.computer_media import mcp_result, scrub_expired
from shared.util import DevError, safe_summary
from tests.test_agentdock_context import workspace


def bound(project, **kwargs):
    return {**project, '_integration_owner':'grant:test', '_coding_owner':'grant:test',
            '_coding_device':'device-test', '_coding_scopes':['read','write','execute','computer'], **kwargs}


def incoming(path='assets/reference.bin', data=b'\x00\xffimage', **kwargs):
    return {'project':'P','workspace_id':'','idempotency_key':uuid.uuid4().hex,
            'file':{'download_url':'https://files.oaiusercontent.com/private?token=PRIVATE_FILE_TICKET','file_id':'PRIVATE_ID','size':len(data)},
            'path':path,'expected_sha256':hashlib.sha256(data).hexdigest(),**kwargs}


@pytest.mark.parametrize('name', sorted(REMOTE_TOOLS))
def test_remote_contracts_accept_pending_and_errors_without_weakening_success(name):
    schema=OUTPUT_SCHEMAS[name];Draft202012Validator.check_schema(schema)
    validate=Draft202012Validator(schema).validate
    validate({'operation_id':'op','pending':True,'state':'queued','next':'operations_wait'})
    validate({'error':{'code':'EXPECTED','message':'Explicit error'}})
    with pytest.raises(SchemaError):validate({'operation_id':'op','pending':False,'state':'queued'})


def test_public_catalogue_has_apps_and_native_files_but_no_owner_controls():
    definitions={d['name']:d for d in tool_definitions()}
    assert not set(definitions)&ADMIN_TOOLS
    assert definitions['download_artifact']['_meta']['openai/fileParams']==['file']
    assert definitions['show_changes']['_meta']['ui']['resourceUri']=='ui://codepier/changes-v1.html'
    assert definitions['open_workspace']['_meta']['ui']['resourceUri']=='ui://codepier/workspace-v1.html'
    assert definitions['lsp_query']['annotations']['readOnlyHint']
    assert set(definitions['lsp_query']['securitySchemes'][0]['scopes'])=={'read','execute'}
    for name in ('fs_read','shell_exec','lsp_query'):
        assert 'ui' not in definitions[name]['_meta']


def test_native_import_publishes_exact_binary_creates_parents_and_never_overwrites(workspace):
    engine,project,root=workspace;data=bytes(range(256))*203
    args=incoming(data=data)
    result=import_artifact(engine,project,args,stream=[data[:117],data[117:]])
    assert (root/args['path']).read_bytes()==data
    assert result['sha256']==hashlib.sha256(data).hexdigest()
    assert result['created'] and not result['overwritten'] and not result['executed'] and not result['extracted']
    if os.name!='nt':assert (root/args['path']).stat().st_mode&0o777==0o600
    with pytest.raises(DevError,match='存在'):import_artifact(engine,project,args,stream=[data])
    assert not list(root.rglob('*.part'))
    assert 'PRIVATE_FILE_TICKET' not in json.dumps(result)
    summary=json.dumps(safe_summary(args))
    assert 'PRIVATE_FILE_TICKET' not in summary and 'PRIVATE_ID' not in summary


@pytest.mark.parametrize('change', ['hash','size','limit','disconnect','type'])
def test_incoming_failure_never_publishes_partial(workspace,change):
    engine,project,root=workspace;data=b'payload';args=incoming(data=data)
    stream=[data]
    if change=='hash':args['expected_sha256']='0'*64
    elif change=='size':args['file']['size']=100
    elif change=='limit':engine.config['integrations']={'max_import_bytes':3};args['file'].pop('size')
    elif change=='type':stream=['not bytes']
    elif change=='disconnect':
        def broken():
            yield b'p'
            raise OSError('simulated disconnect')
        stream=broken()
    with pytest.raises((DevError,OSError)):import_artifact(engine,project,args,stream=stream)
    assert not (root/args['path']).exists()
    assert not list(root.rglob('*.part'))


@pytest.mark.parametrize('path',['../escape','/tmp/escape','.env','assets/../../escape'])
def test_import_respects_project_path_policy(workspace,path):
    with pytest.raises((DevError,ValueError)):import_artifact(workspace[0],workspace[1],incoming(path=path),stream=[b'\x00\xffimage'])


def test_import_rejects_symlink_ancestor(workspace,tmp_path):
    engine,project,root=workspace;outside=tmp_path/'outside';outside.mkdir()
    (root/'assets').symlink_to(outside,target_is_directory=True)
    with pytest.raises((DevError,OSError)):import_artifact(engine,project,incoming(),stream=[b'\x00\xffimage'])
    assert not list(outside.iterdir())


@pytest.mark.parametrize('url',[
    'http://files.oaiusercontent.com/file','https://files.oaiusercontent.com.evil.test/file',
    'https://user:password@files.oaiusercontent.com/file','https://files.oaiusercontent.com:444/file',
    'https://127.0.0.1/file','file:///private/file','https://files.oaiusercontent.com/file#fragment',
    'https://files.oaiusercontent.com/\nprivate'])
def test_file_urls_fail_before_network(url):
    with pytest.raises(DevError):validate_url(url,DEFAULT_HOSTS)


def test_download_dns_pins_public_addresses_and_rejects_loopback(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('No socket may be opened for private DNS'))
    with pytest.raises(DevError) as exc:PublicTLSConnection('files.oaiusercontent.com',443).connect()
    assert exc.value.code=='ARTIFACT_SOURCE_DENIED'


def test_records_bind_owner_mapping_device_and_workspace(workspace):
    engine,project,_=workspace;records=Records(engine.journal);project=bound(project);identifier=uuid.uuid4().hex
    records.save('validation',identifier,project,{'state':'passed'})
    for changed in ({'_integration_owner':'grant:other'},{'id':'other'},{'_coding_device':'other'},
                    {'root':project['root']+'/moved'},{'_workspace_id':'f'*32}):
        with pytest.raises(DevError):records.load('validation',identifier,{**project,**changed})
    admin={**project,'_integration_owner':'panel:test','_integration_admin':True}
    assert records.load('validation',identifier,admin)['state']=='passed'
    records.save('validation',identifier,admin,{'state':'rejected'},replace=True)
    assert records.load('validation',identifier,project)['state']=='rejected'
    assert records.list('validation',{**project,'_integration_owner':'other'})==[]


def test_source_fingerprint_tracks_content_and_mode_not_secrets(workspace):
    engine,project,root=workspace;(root/'source.py').write_text('answer=42\n');(root/'.env').write_text('secret=first')
    first=source_version(engine,project);assert first['complete']
    (root/'.env').write_text('secret=second');assert source_version(engine,project)['sha256']==first['sha256']
    (root/'source.py').write_text('answer=43\n');assert source_version(engine,project)['sha256']!=first['sha256']
    if os.name!='nt':
        second=source_version(engine,project);(root/'source.py').chmod(0o700)
        assert source_version(engine,project)['sha256']!=second['sha256']


def test_source_budget_is_not_a_valid_whole_project_pass(workspace,monkeypatch):
    import agent.source_versions as versions
    engine,project,root=workspace;(root/'one.py').write_text('a=1');(root/'two.py').write_text('b=2')
    monkeypatch.setattr(versions,'MAX_FILES',1)
    result=source_version(engine,project)
    assert not result['complete'] and result['skipped']


def test_validation_is_bound_to_exact_version_and_acceptance_requires_owner(workspace):
    engine,project,root=workspace;project=bound(project);(root/'source.py').write_text('answer=42\n')
    async def execute(*_):return {'exit_code':0,'output':'controlled validation result','duration_ms':1,'timed_out':False,'cancelled':False}
    agent=SimpleNamespace(engine=engine,execute=execute);service=Validations(agent,Records(engine.journal));identifier=uuid.uuid4().hex
    args={'project':'P','command':'fixture','cwd':'.','env':{},'timeout_seconds':5,'label':'Unit fixture'}
    result=asyncio.run(service.run(identifier,project,args));assert result['state']=='passed'
    decision={'validation_id':identifier,'confirm':identifier,'decision':'accept','note':'Reviewed'}
    with pytest.raises(DevError):service.accept(project,decision)
    assert service.accept({**project,'_integration_admin':True},decision)['accepted_current']
    (root/'source.py').write_text('answer=43\n')
    fresh=service.get(project,identifier)
    assert fresh['historical_state']=='passed' and fresh['state']=='stale' and not fresh['accepted_current']
    with pytest.raises(DevError):service.accept({**project,'_integration_admin':True},decision)
    assert service.list(project)['validations'][0]['freshness']=='not_checked'


def test_lsp_unicode_columns_are_not_byte_or_utf16_columns(workspace,tmp_path):
    engine,_,root=workspace;(root/'source.py').write_text('a😀b\n')
    projection=Projector(engine,root,20)
    assert projection.position('source.py',{'line':0,'character':3})=={'line':1,'column':3}
    with pytest.raises(ValueError):projection.position('source.py',{'line':0,'character':2})
    assert projection.path((tmp_path/'private.py').as_uri()) is None
    assert projection.path((root/'source.py').as_uri())=='source.py'


def test_expired_browser_receipts_drop_page_text_and_controls():
    data={'lease_id':'a'*32,'text':'PRIVATE PAGE','elements':[{'value':'PRIVATE VALUE'}],
          'url':'https://private.test/path','title':'PRIVATE TITLE','computer_expires_at':0}
    reply=mcp_result('browser_snapshot',data)['structuredContent']
    assert reply['media_expired'] and not {'text','elements','url','title'}&reply.keys()
    op={'tool':'browser_snapshot','state':'succeeded','result':{'ok':True,'data':data}}
    assert 'PRIVATE' not in json.dumps(mcp_result('operations_get',op))
