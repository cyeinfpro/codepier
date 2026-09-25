"""Coding entry, SHA batch edits and private immutable reviews (temporary data)."""
from copy import deepcopy
import json
from pathlib import Path
import time
import pytest
from pydantic import ValidationError
from shared.contracts import TOOLS, tool_definitions
from shared.crypto import digest
from shared.util import DevError
from shared.computer_media import mcp_result
from agent.coding_reviews import ReviewStore, read_review
from tests.test_agentdock_context import workspace


def call(workspace, name, **kwargs):
    engine, project, _ = workspace
    project = {**project, '_coding_owner': 'grant:test', '_coding_device': 'test-device'}
    args = TOOLS[name].model.model_validate({'project': 'P', **kwargs}).model_dump()
    return engine.call(name, project, args)


def write(path, content, sha='new'):
    return dict(action='write', path=path, content=content, expected_sha256=sha)


def test_all_catalog_profiles_share_the_nine_core_tools():
    from shared.core_contracts import CORE_TOOLS, REPLACED_MCP_TOOLS
    for profile in ('core', 'full', 'coding'):
        tools = tool_definitions(profile)
        assert {definition['name'] for definition in tools} == CORE_TOOLS
        assert not CORE_TOOLS.intersection(REPLACED_MCP_TOOLS)
    assert next(item for item in tool_definitions() if item['name'] == 'process')['inputSchema']['properties']['output_limit']['default'] == 8000


def test_context_reuses_payload_but_detects_rule_policy_and_owner_change(workspace):
    engine, project, root = workspace
    (root/'AGENTS.md').write_text('First rules\n')
    first=call(workspace,'open_workspace')
    second=call(workspace,'open_workspace',context_id=first['context_id'])
    assert second['context_unchanged'] and second['context'] is None
    (root/'AGENTS.md').write_text('Changed rules\n')
    third=call(workspace,'open_workspace',context_id=first['context_id'])
    assert not third['context_unchanged'] and third['context']['documents'][0]['sha256']!=first['context']['documents'][0]['sha256']
    engine.config['allowed_roots'][0]['writable']=False
    assert not call(workspace,'open_workspace',context_id=third['context_id'])['context_unchanged']
    engine.config['allowed_roots']=[]
    with pytest.raises(DevError): call(workspace,'open_workspace',context_id=third['context_id'])


def test_non_git_preexisting_edits_are_baseline_and_review_never_drifts(workspace):
    _,_,root=workspace
    (root/'source.txt').write_text('Existing uncommitted work\n')
    baseline=call(workspace,'open_workspace',capture_baseline=True)
    (root/'source.txt').write_text('New result\n')
    (root/'added.txt').write_text('New file\n')
    review=call(workspace,'show_changes',baseline_ref=baseline['baseline_ref'])
    assert review['immutable'] and review['summary']['files']==2
    frozen=call(workspace,'show_changes',review_ref=review['review_ref'],path='source.txt')
    assert '-Existing uncommitted work' in frozen['diff'] and '+New result' in frozen['diff']
    (root/'source.txt').write_text('Later unrelated work\n')
    assert call(workspace,'show_changes',review_ref=review['review_ref'],path='source.txt')['diff']==frozen['diff']


def test_review_reauthorizes_project_device_owner_and_storage_integrity(workspace):
    engine, project, root=workspace
    (root/'a').write_text('one')
    base=call(workspace,'open_workspace',capture_baseline=True)
    (root/'a').write_text('two')
    review=call(workspace,'show_changes',baseline_ref=base['baseline_ref'])
    args={'review_ref':review['review_ref']}
    for override in ({'_coding_owner':'grant:other'}, {'_coding_device':'other'}, {'id':'other'}):
        bound={**project,'_coding_owner':'grant:test','_coding_device':'test-device',**override}
        with pytest.raises(DevError): read_review(engine,bound,args)
    path=engine.journal.directory/'coding-reviews'/(review['review_ref']+'.json')
    data=json.loads(path.read_text());data['payload']['summary']['files']=999;path.write_text(json.dumps(data))
    with pytest.raises(DevError) as exc:call(workspace,'show_changes',review_ref=review['review_ref'])
    assert exc.value.code=='REVIEW_INTEGRITY'


def test_reviews_report_limits_binary_and_exclusions(workspace,monkeypatch):
    import agent.coding_reviews as reviews
    _,_,root=workspace
    (root/'.env').write_text('PRIVATE_SECRET_DO_NOT_EXPORT')
    (root/'.venv-custom').mkdir();(root/'.venv-custom'/'package.py').write_text('DEPENDENCY_NOISE')
    (root/'binary.bin').write_bytes(b'\x00before')
    (root/'plain').write_text('before')
    base=call(workspace,'open_workspace',capture_baseline=True)
    (root/'binary.bin').write_bytes(b'\x00after')
    (root/'plain').write_text('after')
    review=call(workspace,'show_changes',baseline_ref=base['baseline_ref'])
    assert review['summary']['files']==2
    assert next(f for f in review['files'] if f['path']=='binary.bin')['text_diff_available'] is False
    stored=''.join(p.read_text() for p in (workspace[0].journal.directory/'coding-reviews').glob('*.json'))
    assert 'PRIVATE_SECRET_DO_NOT_EXPORT' not in stored and 'DEPENDENCY_NOISE' not in stored
    monkeypatch.setattr(reviews,'MAX_FILES',1)
    limited=call(workspace,'open_workspace',capture_baseline=True)
    assert not limited['baseline_coverage']['complete']


def test_review_pages_and_diff_pages_are_fixed(workspace):
    _,_,root=workspace
    base=call(workspace,'open_workspace',capture_baseline=True)
    for i in range(4):(root/f'{i}.txt').write_text('line\n'*100)
    review=call(workspace,'show_changes',baseline_ref=base['baseline_ref'],limit=2)
    assert len(review['files'])==2 and review['next_offset']==2
    page=call(workspace,'show_changes',review_ref=review['review_ref'],offset=2,limit=2)
    assert len(page['files'])==2 and page['next_offset'] is None
    first=call(workspace,'show_changes',review_ref=review['review_ref'],path='0.txt',max_chars=256)
    rest=call(workspace,'show_changes',review_ref=review['review_ref'],path='0.txt',offset=first['next_offset'])
    whole=call(workspace,'show_changes',review_ref=review['review_ref'],path='0.txt')
    assert first['diff']+rest['diff']==whole['diff']


def test_expiry_and_quota_never_fake_empty_review(workspace,monkeypatch):
    import agent.coding_reviews as reviews
    monkeypatch.setattr(reviews,'TTL',-1)
    base=call(workspace,'open_workspace',capture_baseline=True)
    with pytest.raises(DevError) as exc:call(workspace,'show_changes',baseline_ref=base['baseline_ref'])
    assert exc.value.code=='REVIEW_EXPIRED'
    monkeypatch.setattr(reviews,'TTL',100)
    monkeypatch.setattr(reviews,'MAX_STORAGE_BYTES',1)
    with pytest.raises(DevError) as exc:call(workspace,'open_workspace',capture_baseline=True)
    assert exc.value.code=='REVIEW_QUOTA'


def test_patch_preflights_whole_batch_and_preview_is_read_only(workspace):
    engine,_,root=workspace
    (root/'a').write_text('before\n')
    changes=[write('new.txt','created\n'),write('a','after\n','0'*64)]
    with pytest.raises(DevError):call(workspace,'apply_patch',changes=changes,idempotency_key='conflict-123')
    assert not (root/'new.txt').exists() and (root/'a').read_text()=='before\n'
    assert not engine.journal.history(str(root))
    changes[1]['expected_sha256']=digest(b'before\n')
    preview=call(workspace,'apply_patch',changes=changes,dry_run=True,idempotency_key='preview-123')
    assert preview['outcome']=='preview' and not (root/'new.txt').exists()
    result=call(workspace,'apply_patch',changes=changes,idempotency_key='apply-12345')
    assert result['success'] and not result['atomic']
    assert (root/'a').read_text()=='after\n' and (root/'new.txt').exists()
    assert all(f['backup_id'] for f in result['files'])


def test_patch_supports_move_delete_and_preserves_mode(workspace):
    _,_,root=workspace
    (root/'a').write_text('executable\n');(root/'a').chmod(0o755)
    (root/'b').write_text('remove me')
    result=call(workspace,'apply_patch',idempotency_key='move-delete-1',changes=[
        {'action':'move','path':'a','destination':'c','expected_sha256':digest(b'executable\n')},
        {'action':'delete','path':'b','expected_sha256':digest(b'remove me')}])
    assert result['success'] and not (root/'a').exists() and not (root/'b').exists()
    assert (root/'c').read_text()=='executable\n' and (root/'c').stat().st_mode&0o777==0o755


@pytest.mark.parametrize('path',['../escape','.env','/tmp/escape'])
def test_patch_keeps_path_security(workspace,path):
    with pytest.raises(DevError):call(workspace,'apply_patch',changes=[write('safe','yes'),write(path,'no')],idempotency_key='unsafe-12345')
    assert not (workspace[2]/'safe').exists()


def test_patch_duplicate_and_symlink_are_rejected_before_effects(workspace,tmp_path):
    root=workspace[2];outside=tmp_path/'outside';outside.write_text('private');(root/'link').symlink_to(outside)
    for changes in ([write('a','1'),write('./a','2')],[write('a','1'),write('link','2')]):
        with pytest.raises(DevError):call(workspace,'apply_patch',changes=changes,idempotency_key='duplicates-123')
    assert not (root/'a').exists() and outside.read_text()=='private'


def test_patch_rolls_back_without_overwriting_concurrent_edits(workspace,monkeypatch):
    engine,_,root=workspace
    (root/'a').write_text('original')
    original=engine.mutate
    def injected(project,path,expected,after,**kwargs):
        if path=='b':raise OSError('disk failure')
        return original(project,path,expected,after,**kwargs)
    monkeypatch.setattr(engine,'mutate',injected)
    changes=[write('a','changed',digest(b'original')),write('b','new')]
    result=call(workspace,'apply_patch',changes=changes,idempotency_key='rollback-123')
    assert not result['success'] and result['outcome']=='rolled_back'
    assert (root/'a').read_text()=='original'
    assert mcp_result('apply_patch',result)['isError']
    def concurrent(project,path,expected,after,**kwargs):
        if path=='b':
            (root/'a').write_text('human edit')
            raise OSError('second file failed')
        return original(project,path,expected,after,**kwargs)
    monkeypatch.setattr(engine,'mutate',concurrent)
    result=call(workspace,'apply_patch',changes=changes,idempotency_key='partial-1234')
    assert result['outcome']=='partial' and result['rollback_errors'][0]['path']=='a'
    assert (root/'a').read_text()=='human edit'
    polled={'tool':'apply_patch','state':'needs_review','result':{'ok':True,'data':result}}
    assert mcp_result('operations_wait',polled)['isError']


def test_native_turn_capture_does_not_reset_agent_journal(workspace):
    from agent.chat_reviews import TurnReviews
    engine,project,root=workspace
    engine.config_path.write_text(json.dumps(engine.config))
    engine.journal.start('running-operation',{'tool':'fs_write'})
    engine.journal.mark_running('running-operation')
    row={'id':'native-session','project_id':project['id'],'device_id':'node','root':str(root)}
    reviews=TurnReviews(engine.journal.directory/'native-cli',row,engine.config_path)
    reviews.begin('first')
    (root/'source.txt').write_text('native result')
    review=reviews.finish('first')
    assert review['available'] and review['summary']['files']==1
    assert engine.journal.status('running-operation')['status']=='running'
    bound={**project,'device_id':'node','_coding_owner':'native:native-session'}
    saved=read_review(engine,bound,{'review_ref':review['review_ref'],'path':'source.txt'})
    assert '+native result' in saved['diff']
    assert reviews.finish('settings-receipt') is None


from tests.test_native_cli import native


@pytest.mark.parametrize('action',['clear','delete'])
def test_native_history_cleanup_purges_only_its_review_snapshots(native,action):
    import uuid
    from contextlib import closing
    from shared.native_cli import database
    from agent.coding_reviews import capture,freeze_review
    obj,project=native;engine=obj.agent.engine;sid=uuid.uuid4().hex;now=time.time()
    bound={**project,'_coding_owner':'native:'+sid}
    with closing(database(obj.directory)) as db,db:
        db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,argv,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (sid,project['id'],project['device_id'],project['root'],project['root'],'codex','fixture','exited',now,now,'[]','chat'))
    store=ReviewStore(engine)
    baseline=store.save(capture(engine,bound))
    Path(project['root'],'change.txt').write_text('native')
    review=freeze_review(engine,bound,baseline['id'])
    other=store.save(capture(engine,{**project,'_coding_owner':'grant:unrelated'}))
    assert obj.action('chat_review',project,{'id':sid,'review_ref':review['review_ref']})['immutable']
    cleared=obj.action(action,project,{'id':sid,'confirm':sid})
    assert cleared['review_snapshots_removed']==2 and cleared['native_history']=='untouched'
    assert not (store.directory/(review['review_ref']+'.json')).exists()
    assert (store.directory/(other['id']+'.json')).exists()
    with pytest.raises(DevError):obj.action('chat_review',project,{'id':sid,'review_ref':review['review_ref']})


def test_compact_receipt_metadata_keeps_named_fields_and_constraints():
    from shared.contracts import _compact_input_schema, _OPERATION
    schema = {
        'type': 'object', 'title': 'Generated title', 'additionalProperties': False,
        'required': ['title', 'description', 'operation_id'],
        'properties': {
            'title': {'type': 'string', 'minLength': 1, 'default': 'literal'},
            'description': {'type': 'string', 'description': 'Keep this useful field help'},
            'operation_id': {**_OPERATION, 'minLength': 1},
        },
        'anyOf': [{'properties': {'pending': {'const': True}}, 'required': ['pending']}],
    }
    original = deepcopy(schema)
    compact = _compact_input_schema(schema, output=True)
    expected = deepcopy(schema)
    expected.pop('title')
    expected['properties']['operation_id'].pop('description')
    assert compact == expected
    assert schema == original
    assert _compact_input_schema(schema)['properties']['operation_id']['description'] == _OPERATION['description']


def test_compact_outputs_preserve_validation_and_authorization_contracts():
    from shared.contracts import OUTPUT_SCHEMAS

    def constraints(schema):
        result = {k: deepcopy(v) for k, v in schema.items() if k not in {'title', 'description'}}
        for key in ('properties', '$defs', 'patternProperties'):
            if key in result:
                result[key] = {name: constraints(value) for name, value in result[key].items()}
        for key in ('items', 'additionalProperties', 'not'):
            if isinstance(result.get(key), dict):
                result[key] = constraints(result[key])
        for key in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            if key in result:
                result[key] = [constraints(value) for value in result[key]]
        return result

    original = deepcopy(OUTPUT_SCHEMAS)
    full = {t['name']: t for t in tool_definitions()}
    for compact in tool_definitions('coding'):
        complete = full[compact['name']]
        assert constraints(compact['outputSchema']) == constraints(complete['outputSchema'])
        assert compact['annotations'] == complete['annotations']
        assert compact['_meta']['securitySchemes'] == complete['_meta']['securitySchemes']
    assert OUTPUT_SCHEMAS == original
