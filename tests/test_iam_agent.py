"""Two human identities, one authorized role Token and a real disposable Agent."""
import json
import time
import uuid
from pathlib import Path
from hub.store import Store
from hub import iam
from shared.crypto import password_hash
from tests.test_iam_integration import Browser
from tests.test_roles import must
from tests.test_access_profiles import call,data


def test_real_agent_new_user_role_create_read_revoke_and_mapping_bounds(stack):
    uid='usr-'+uuid.uuid4().hex
    from contextlib import closing
    with closing(Store(stack.hubdir)) as store:
        with store.transaction():
            store.db.execute('INSERT INTO users VALUES(?,?,?,?)',(uid,uid,password_hash('temporary-user-password'),time.time()))
            store.db.execute("UPDATE memberships SET level='member' WHERE user_id=? AND space_id='legacy'",(uid,))
    # A distinct real password session; the owner client is unchanged.
    import httpx
    with httpx.Client(base_url=stack.url,trust_env=False,timeout=30) as client:
        session=must(client.post('/api/login',json={'username':uid,'password':'temporary-user-password'}))
        client.headers['X-RD-CSRF']=session['csrf'];client.headers['X-CodePier-Space']='legacy'
        role=must(stack.client.post('/api/access-roles',json={'label':'Secretary '+uid,'project_rules':[{'actions':['read','write'],'created_projects':True}],
          'device_rules':[{'actions':['devices.read','projects.create'],'devices':[stack.device],'root_prefixes':[str(stack.root)],'max_project_mode':'write','allow_tasks':False}],
          'idempotency_key':uuid.uuid4().hex}),201)
        assignment=must(stack.client.put('/api/iam/spaces/legacy/assignments/'+role['id']+'/'+uid,json={'active':True,'may_delegate':True,'expected_version':0}))
        profile=must(client.post('/api/access-profiles',json={'label':'My secretary','role_id':role['id'],'idempotency_key':uuid.uuid4().hex}),201)
        grant=must(client.post('/api/grants',json={'label':'secretary','scopes':['codepier.role_access'],'authorization_mode':'role','profile_id':profile['id'],'profile_version':profile['version'],'role_version':role['version'],'confirm_dynamic_role':True}))
        directory=stack.root/'new-private';directory.mkdir();(directory/'readme.txt').write_text('allowed-test-data')
        args={'alias':'New-'+uid,'device_id':stack.device,'root':str(directory),'mode':'write','allow_tasks':False,'idempotency_key':uuid.uuid4().hex}
        from tests.support import wait_for
        results=[]
        def create():
            result=data(call(client,grant['token'],'projects_create',args));results.append(result)
            return result if not result.get('pending') else None
        created=wait_for(create,timeout=20)
        pid=created.get('id') or created.get('project',{}).get('id')
        assert pid,created
        receipt=data(call(client,grant['token'],'fs_read',{'project':pid,'path':'readme.txt'}))
        if receipt.get('pending'):receipt=stack.poll(receipt['operation_id'])['result']['data']
        assert 'allowed-test-data' in json.dumps(receipt)
        must(stack.client.put('/api/iam/spaces/legacy/assignments/'+role['id']+'/'+uid,json={'active':False,'may_delegate':True,'expected_version':assignment['version']}))
        denied=call(client,grant['token'],'fs_write',{'project':pid,'path':'forbidden.txt','content':'must-not-write','expected_sha256':'new','idempotency_key':uuid.uuid4().hex})
        assert denied.status_code==403,denied.text
        assert not (directory/'forbidden.txt').exists()
        assert client.get('/api/projects',headers={'X-CodePier-Space':'unrelated-space'}).status_code in {403,404}
