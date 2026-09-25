"""Lazy review cards in real Chromium/WebKit, with isolated HTTP fixtures."""
import pytest
from playwright.sync_api import expect
from tests.browser_support import chat_page,event
from tests.test_chat_complete_browser import send


def seed(page):
    page.evaluate('''() => {
      window.originalReviewAPI=window.api;window.reviewRequests=[];
      window.api=async(path,opts={})=>{
        if(!path.endsWith('/chat_review'))return originalReviewAPI(path,opts);
        const args=JSON.parse(opts.body).args;reviewRequests.push(args);
        if(window.delayReview)await new Promise(resolve=>window.finishReview=resolve);
        if(window.reviewError)throw new Error('节点离线，固定快照仍保留');
        const common={review_ref:'a'.repeat(32),immutable:true,summary:{files:2,added_lines:4,removed_lines:1},coverage:{complete:true}};
        if(args.path)return {...common,path:args.path,diff:'--- a/source.py\\n+++ b/source.py\\n-previous\\n+<img src=x onerror=alert(1)>\\n',next_offset:null,diff_truncated:false};
        return {...common,files:[{path:'src/source.py',status:'modified',added_lines:3,removed_lines:1,text_diff_available:true},{path:'tests/<img src=x onerror=alert(1)>.py',status:'added',added_lines:1,removed_lines:0,text_diff_available:true}],next_offset:null};
      };
    }''')
    receipt=send(page)['args']['receipt']
    event(page,'chat',{'type':'user','receipt':receipt,'text':'优化这个项目'},10)
    event(page,'chat',{'type':'delta','receipt':receipt,'text':'已完成源码修改。'},20)
    event(page,'chat',{'type':'review','receipt':receipt,'available':True,'review_ref':'a'*32,'summary':{'files':2,'added_lines':4,'removed_lines':1},'coverage':{'complete':True},'text':'本轮期间改动 · 2 个文件'},30)
    event(page,'chat',{'type':'done','receipt':receipt,'status':'completed'},40)
    return receipt


@pytest.mark.parametrize('chat_page',['chromium','webkit'],indirect=True)
def test_review_is_collapsed_lazy_safe_and_stable(chat_page):
    p=chat_page;receipt=seed(p)
    expect(p.locator('.chat-review')).to_be_visible()
    expect(p.locator('.chat-review-counts')).to_contain_text('2 个文件 · +4 −1')
    assert p.evaluate('reviewRequests.length')==0
    p.locator('.chat-review>summary').click()
    expect(p.locator('.chat-review-file')).to_have_count(2)
    assert p.evaluate('reviewRequests.length')==1
    p.locator('.chat-review-file>summary').first.click()
    expect(p.locator('.chat-review-diff pre')).to_contain_text('<img src=x')
    assert p.locator('.chat-review img').count()==0
    p.evaluate('window.reviewNode=document.querySelector(".chat-review")')
    event(p,'chat',{'type':'delta','receipt':receipt,'text':'后续文字。'},50)
    assert p.evaluate('reviewNode===document.querySelector(".chat-review")')
    expect(p.locator('.chat-review-diff pre')).to_be_visible()
    assert p.evaluate('reviewRequests.length')==2


def test_stale_review_response_cannot_replace_new_conversation(chat_page):
    p=chat_page;seed(p);p.evaluate('window.delayReview=true')
    p.locator('.chat-review>summary').click()
    p.wait_for_function('!!window.finishReview')
    p.click('#chat-new');p.locator('#chat-project-results button:not(:disabled)').first.click();p.fill('#chat-compose','保留新会话输入')
    p.evaluate('window.delayReview=false;finishReview()')
    expect(p.locator('#chat-compose')).to_have_value('保留新会话输入')
    expect(p.locator('.chat-review')).to_have_count(0)


def test_offline_review_is_explicit_and_retries_the_same_snapshot(chat_page):
    p=chat_page;seed(p);p.evaluate('window.reviewError=true')
    p.locator('.chat-review>summary').click()
    expect(p.locator('.chat-review-loading.is-error')).to_contain_text('节点离线')
    p.evaluate('window.reviewError=false')
    p.get_by_role('button',name='重新读取快照').click()
    expect(p.locator('.chat-review-file')).to_have_count(2)
    expect(p.locator('.chat-review-loading.is-error')).to_have_count(0)
    assert p.evaluate('reviewRequests.every(x=>x.review_ref==="a".repeat(32))')


@pytest.mark.parametrize('width,height',[(320,568),(390,844),(768,1024),(1440,1000)])
def test_review_and_inspector_fit_without_hiding_composer(chat_page,width,height):
    p=chat_page;p.set_viewport_size({'width':width,'height':height});seed(p)
    p.locator('.chat-review>summary').click();p.locator('.chat-review-file>summary').first.click()
    expect(p.locator('.chat-review-diff pre')).to_be_visible()
    assert p.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    box=p.locator('#chat-send').bounding_box()
    assert box and box['y']+box['height']<=height+1
    if width<=760:
        p.locator('.chat-overflow summary').click()
        p.click('[data-chat-action="chat-inspector-toggle"]')
    else:
        p.click('#chat-inspector-toggle')
    p.click('[data-chat-tab="changes"]')
    expect(p.locator('.chat-review-jump')).to_be_visible()
    p.locator('.chat-review-jump').click()
    expect(p.locator('.chat-review')).to_have_attribute('open','')


def test_missing_baseline_is_not_rendered_as_zero_changes(chat_page):
    p=chat_page;receipt=send(p)['args']['receipt']
    event(p,'chat',{'type':'review','receipt':receipt,'available':False,'text':'未捕获修改前快照'},10)
    expect(p.locator('.chat-review-counts')).to_have_text('未能核实')
    p.locator('.chat-review>summary').click()
    expect(p.locator('.chat-review-note')).to_have_text('未捕获修改前快照')
