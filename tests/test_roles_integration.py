"""Real Hub/outbound Agent: same role token, live policy and local filesystem floor."""
import uuid

from tests.test_roles import role, profile, credential, update_role, must
from tests.test_access_profiles import call, data


def test_secretary_creates_project_reads_with_same_token_and_obeys_local_floor(stack):
    label='Role'+uuid.uuid4().hex[:10]
    r=role(stack.client,label=label,project_rules=[{'actions':['read','write'],'created_projects':True}],
        device_rules=[{'actions':['devices.read','projects.create'],'devices':[stack.device],
                      'max_project_mode':'write'}])
    p=profile(stack.client,r,label);token=must(credential(stack.client,r,p))['token']
    directory=stack.root/label;directory.mkdir();(directory/'hello.txt').write_text('secretary fixture',encoding='utf-8')
    args={'alias':label,'device_id':stack.device,'root':str(directory),'mode':'write','idempotency_key':uuid.uuid4().hex}
    created=data(call(stack.client,token,'projects_create',args))
    assert created['role_access']==['read','write']
    assert data(call(stack.client,token,'projects_create',args))['id']==created['id']
    content=data(call(stack.client,token,'fs_read',{'project':created['id'],'path':'hello.txt'}))
    assert content['content']=='secretary fixture'
    must(update_role(stack.client,r,project_rules=[{'actions':['read'],'all_projects':True}]))
    view=data(call(stack.client,token,'get_access_context'))
    assert {project['id'] for project in view['projects']} >= {created['id'],stack.project['id']}
    assert 'write' not in next(item for item in view['project_permissions'] if item['id']==created['id'])['actions']
    denied=call(stack.client,token,'fs_write',{'project':created['id'],'path':'hello.txt','content':'must not write',
                  'expected_sha256':content['sha256'],'idempotency_key':uuid.uuid4().hex}).json()['result']
    assert denied['isError'] and denied['structuredContent']['error']['code']=='ROLE_POLICY_DENIED'
    assert (directory/'hello.txt').read_text()=='secretary fixture'
    outside=stack.directory/('outside'+label);outside.mkdir()
    denied=call(stack.client,token,'projects_create',{**args,'alias':label+'bad','root':str(outside),'idempotency_key':uuid.uuid4().hex}).json()['result']
    assert denied['isError'] and denied['structuredContent']['error']['code']=='ROOT_NOT_ALLOWED'
    assert not any(item['alias']==label+'bad' for item in stack.client.get('/api/projects').json()['projects'])
