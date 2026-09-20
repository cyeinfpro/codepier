"""DOM-action UI against an explicit fixture boundary, not a user's browser."""
from __future__ import annotations
import pytest
from playwright.sync_api import expect
from tests.test_devtools_flow import tools_page, tab, confirm
from tests.test_integrations_stack import integrated_stack


@pytest.fixture
def browser_page(tools_page):
    page=tools_page
    page.evaluate('''() => {
      window.fixtureLease='c'.repeat(32);window.browserLeases=[];window.observations=0;window.poolAvailable=2;
      toolHandlers.browser_status=async()=>({enabled:true,connected:true,profile_bound:true,origins:['https://example.test'],pool:{available:poolAvailable},active_leases:browserLeases});
      toolHandlers.browser_open=async()=>{browserLeases=[{lease_id:fixtureLease,state:'ready',expires_at:Date.now()/1000+900}];return {lease_id:fixtureLease,opened:true};};
      toolHandlers.browser_snapshot=async args=>({lease_id:args.lease_id,observation_id:(++observations).toString(16).padStart(32,'0'),expires_at:Date.now()/1000+900,title:'Synthetic verification form',url:'https://example.test/form',text:'Fixture text <script>not executable</script>',elements:[{id:'field-one',tag:'input',label:'Synthetic field'}]});
      toolHandlers.browser_action=async args=>({lease_id:args.lease_id,action_outcome:'confirmed',observation_consumed:true,business_outcome_verified:false});
      toolHandlers.browser_close=async args=>{browserLeases=[];return {lease_id:args.lease_id,released:true,tab_cleanup_confirmed:false};};
    }''')
    tab(page,'browser')
    expect(page.locator('[data-i-form=browser-open] button[type=submit]')).to_be_enabled()
    return page


def open_page(page):
    page.fill('[name=url]','https://example.test/form')
    page.click('[data-i-form=browser-open] button[type=submit]')
    expect(page.locator('[data-i-form=browser-action]')).to_be_visible()


@pytest.mark.parametrize('action,value',[('click',''),('fill','fixture text'),('select','option-one'),('scroll',''),('key','Tab'),('navigate','https://example.test/next')])
def test_every_page_action_confirms_once_and_observes_again(browser_page,action,value):
    page=browser_page
    open_page(page)
    first=page.evaluate('observations')
    assert first==1
    page.select_option('[data-i-form=browser-action] [name=action]',action)
    if action in {'click','fill','select'}:
        page.select_option('[name=element_id]','field-one')
    else:
        expect(page.locator('[name=element_id]')).to_be_hidden()
    if action in {'fill','select','key','navigate'}:
        page.fill('[data-i-form=browser-action] [name=value]',value)
    else:
        expect(page.locator('[data-i-form=browser-action] [name=value]')).to_be_hidden()
    page.click('[data-i-form=browser-action] button[type=submit]')
    expect(page.locator('#i-confirm-form')).to_contain_text(action)
    assert page.evaluate("toolCalls.filter(r=>r.tool==='browser_action').length")==0
    confirm(page)
    page.wait_for_function('() => observations===2')
    expect(page.locator('#i-result')).to_contain_text('网页动作回执')
    args=page.evaluate("toolCalls.find(r=>r.tool==='browser_action').arguments")
    assert args['action']==action and args['observation_id']=='0'*31+'1'
    assert args['value']==value and args['element_id']==('field-one' if action in {'click','fill','select'} else '')
    assert page.evaluate("toolCalls.filter(r=>r.tool==='browser_action').length")==1
    assert page.locator('#i-browser-page script').count()==0
    expect(page.locator('.integration-page-text').first).to_contain_text('<script>not executable</script>')


def test_uncertain_action_does_not_repeat_or_reuse_observation(browser_page):
    page=browser_page
    open_page(page)
    page.evaluate("toolHandlers.browser_action=async()=>({error:{code:'BROWSER_ACTION_UNCERTAIN',message:'fixture unconfirmed input; observe before doing anything else'}})")
    page.select_option('[name=element_id]','field-one')
    page.click('[data-i-form=browser-action] button[type=submit]')
    confirm(page)
    expect(page.locator('[data-i-form=browser-action] .integration-form-error')).to_contain_text('unconfirmed input')
    expect(page.locator('[data-i-form=browser-action] button[type=submit]')).to_be_disabled()
    assert page.evaluate('observations')==1
    page.click('[data-i-action=snapshot]')
    page.wait_for_function('() => observations===2')
    expect(page.locator('[data-i-form=browser-action] button[type=submit]')).to_be_enabled()
    assert page.evaluate("toolCalls.filter(r=>r.tool==='browser_action').length")==1


def test_pool_preparation_retry_and_release_distinguish_business_result(browser_page):
    page=browser_page
    page.evaluate('poolAvailable=0')
    page.click('[data-i-action=browser-refresh]')
    expect(page.locator('[data-i-form=browser-open] button[type=submit]')).to_be_disabled()
    expect(page.locator('#i-browser-status')).to_contain_text('准备标签页')
    page.evaluate('poolAvailable=2')
    page.click('[data-i-action=browser-refresh]')
    expect(page.locator('[data-i-form=browser-open] button[type=submit]')).to_be_enabled()
    open_page(page)
    page.click('[data-i-action=browser-close]')
    expect(page.locator('#i-result')).to_contain_text('标签页清理尚未确认')
    expect(page.locator('#i-browser-page')).to_be_empty()
    assert page.evaluate("toolCalls.filter(r=>r.tool==='browser_close').length")==1


@pytest.mark.parametrize('url',['https://unapproved.test/','https://name:password@example.test/'])
def test_wrong_site_and_url_credentials_are_not_sent(browser_page,url):
    page=browser_page
    page.fill('[name=url]',url)
    page.click('[data-i-form=browser-open] button[type=submit]')
    expect(page.locator('[data-i-form=browser-open] .integration-form-error')).to_be_visible()
    assert not page.evaluate("toolCalls.some(r=>r.tool==='browser_open')")


def test_late_snapshot_does_not_replace_the_new_page(browser_page):
    page=browser_page
    page.evaluate('''() => {
      window.leaseA='a'.repeat(32);window.leaseB='b'.repeat(32);
      browserLeases=[{lease_id:leaseA,state:'ready'},{lease_id:leaseB,state:'ready'}];
      toolHandlers.browser_snapshot=async args=>args.lease_id===leaseA?await new Promise(resolve=>window.releaseSnapshot=()=>resolve({lease_id:leaseA,observation_id:'a'.repeat(32),title:'OLD PAGE',url:'https://example.test/old',text:'old',elements:[]})):{lease_id:leaseB,observation_id:'b'.repeat(32),title:'CURRENT PAGE',url:'https://example.test/current',text:'current',elements:[]};
    }''')
    page.click('[data-i-action=browser-refresh]')
    expect(page.locator('[data-i-action=snapshot]')).to_have_count(2)
    page.locator('[data-i-action=snapshot]').nth(0).click()
    page.wait_for_function('() => !!window.releaseSnapshot')
    page.locator('[data-i-action=snapshot]').nth(1).click()
    expect(page.locator('#i-browser-page')).to_contain_text('CURRENT PAGE')
    page.evaluate('releaseSnapshot()')
    expect(page.locator('#i-browser-page')).to_contain_text('CURRENT PAGE')
    expect(page.locator('#i-browser-page')).not_to_contain_text('OLD PAGE')
