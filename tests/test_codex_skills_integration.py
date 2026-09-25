import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from playwright.sync_api import expect, sync_playwright

from shared.contracts import OUTPUT_SCHEMAS
from tests.test_codex_skills import skill


@pytest.fixture(scope='module')
def prepared(stack):
    # All user-global folders for this Agent process point to a temporary HOME.
    home=stack.directory/'fake-home';home.mkdir()
    folder=skill(home/'.codex/skills/local-design',name='local-design',text='A local design workflow. <img src=x onerror=window.skillXss=1>')
    (folder/'references').mkdir();(folder/'references/layout.md').write_text('REFERENCE CONTENT')
    (folder/'scripts').mkdir();(folder/'scripts/check.py').write_text('from pathlib import Path\nPath("SHOULD_NOT_EXECUTE").touch()')
    disabled=skill(home/'.codex/skills/disabled',name='disabled-skill')
    (home/'.codex/config.toml').write_text('private_token = "HIDDEN-CONFIG-VALUE"\n[[skills.config]]\npath = '+json.dumps(str(disabled/'SKILL.md'))+'\nenabled = false\n')
    skill(home/'.agents/skills/user',name='user-skill')
    skill(stack.imago/'.codex/skills/project',name='project-skill')
    stack.stop_agent()
    config=json.loads(stack.config_path.read_text());config['skills']={'codex_enabled':True,'codex_projects':[stack.project['id']]}
    stack.config_path.write_text(json.dumps(config))
    stack.env['HOME']=str(home)
    stack.env.pop('CODEX_HOME',None)
    stack.start_agent()
    yield stack,folder


def mcp(stack,name,args):
    from hub.core_tools import public_call
    public_name, public_args = public_call(name, args)
    r=stack.mcp(public_name,public_args)
    assert not r.get('isError'),r
    value=r['structuredContent']
    Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(value)
    if value.get('pending'):
        op=stack.poll(value['operation_id'],timeout=20)
        assert op['state']=='succeeded',op
        value={'operation_id':op['id'],**op['result']['data']}
    return value


def test_real_mcp_reads_user_project_and_resource_without_executing(prepared):
    stack,folder=prepared
    data=mcp(stack,'skills_list',{'project':'Imago'})
    assert {s['name'] for s in data['skills']}=={'project-skill','user-skill','local-design'}
    assert 'HIDDEN-CONFIG-VALUE' not in json.dumps(data)
    selected=next(s for s in data['skills'] if s['name']=='local-design')
    resource=mcp(stack,'skills_read',{'project':'Imago','skill_id':selected['skill_id'],'resource_path':'references/layout.md'})
    assert resource['content']=='REFERENCE CONTENT'
    script=mcp(stack,'skills_read',{'project':'Imago','skill_id':selected['skill_id'],'resource_path':'scripts/check.py'})
    assert 'SHOULD_NOT_EXECUTE' in script['content']
    assert not (folder/'SHOULD_NOT_EXECUTE').exists()
    assert not mcp(stack,'skills_list',{'project':'Nexus'})['skills']
    denied=stack.mcp('workspace',{'project': 'Nexus', 'skill_id': selected['skill_id'], 'operation': 'skill'})
    assert denied['isError']
    disabled=mcp(stack,'skills_list',{'project':'Imago','include_disabled':True,'query':'disabled'})['skills'][0]
    denied=stack.mcp('workspace',{'project': 'Imago', 'skill_id': disabled['skill_id'], 'explicit': True, 'operation': 'skill'})
    assert denied['isError'] and denied['structuredContent']['error']['code']=='SKILL_DISABLED'
    preview=mcp(stack,'project_context',{'project':'Imago','max_files':1,'max_chars':1000})
    assert len(preview['codex_skills'])==2 and preview['skills_catalog']['codex_enabled_for_project']


def test_browser_lists_reads_resources_and_mobile_layout(prepared,tmp_path):
    stack,folder=prepared
    screenshots=Path(os.getenv('CODEX_SKILLS_SCREENSHOTS',str(tmp_path/'screenshots')));screenshots.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.goto(stack.url+'/#workbench');page.fill('#password',stack.password);page.click('#login-form button')
            expect(page.locator('[data-local-skills]')).to_be_visible();page.click('[data-local-skills]')
            expect(page.locator('#local-skills-results')).to_contain_text('Codex 用户技能已授权')
            expect(page.locator('#local-skills-results')).to_contain_text('disabled-skill')
            expect(page.locator('.local-skill-card').filter(has_text='disabled-skill').locator('button')).to_be_disabled()
            page.screenshot(path=str(screenshots/'skills-desktop-list.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(screenshots/'skills-mobile-list.png'),full_page=True)
            page.locator('#local-skills-form input').fill('local-design');page.locator('#local-skills-form button').click()
            expect(page.locator('[data-skill-open]')).to_have_count(1);page.locator('[data-skill-open]').click()
            expect(page.locator('#modal-title')).to_have_text('技能 · local-design')
            assert page.locator('.modal img').count()==0 and page.evaluate('window.skillXss') is None
            page.screenshot(path=str(screenshots/'skills-mobile-detail.png'),full_page=True)
            page.locator('.skill-resources summary').click()
            page.locator('[data-skill-resource="references/layout.md"]').click()
            expect(page.locator('.skill-document pre')).to_have_text('REFERENCE CONTENT')
            page.set_viewport_size({'width':1440,'height':1000})
            page.screenshot(path=str(screenshots/'skills-desktop-resource.png'),full_page=True)
            assert not errors,errors
            assert not (folder/'SHOULD_NOT_EXECUTE').exists()
        finally:browser.close()
