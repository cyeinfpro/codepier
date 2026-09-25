"""Audit counterexamples against the real UI and isolated DOM action function.

HTTP/native responses are deterministic fixtures. No real account, external
model, authorized browser profile, SSH host or user file is touched.
"""
from pathlib import Path

from playwright.sync_api import expect
from tests.browser_support import chat_page, event  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_incremental_markdown_matches_every_two_part_split_and_single_char_stream(chat_page):
    examples = ['1. First\n2. Second\n3. Third',
                'Name | Value\n--- | ---\nAlpha | One\nBeta | Two\nGamma | Three',
                'same paragraph\n\nsame paragraph\n\n1. One\n2. Two',
                '# Heading\n\n```python\nprint(1)\n```\n\n- A\n- B']
    differences = chat_page.evaluate('''examples => {
      const differences=[];
      for(const text of examples){
        const full=document.createElement('div');chatRichMarkdown(full,text);
        for(let split=1;split<text.length;split++){
          const streamed=document.createElement('div');
          chatRichMarkdown(streamed,text.slice(0,split));chatRichMarkdown(streamed,text);
          if(streamed.innerHTML!==full.innerHTML)differences.push({text,split});
        }
        const streamed=document.createElement('div');
        for(let end=1;end<=text.length;end++)chatRichMarkdown(streamed,text.slice(0,end));
        if(streamed.innerHTML!==full.innerHTML)differences.push({text,split:'single characters'});
      }
      return differences;
    }''', examples)
    assert differences == []


def test_uncertain_send_retries_original_snapshot_despite_later_failed_attachment(chat_page):
    page = chat_page
    page.evaluate('''() => {
      const original=api;window.promptAttempts=0;
      window.api=async(path,options)=>{
        if(path.endsWith('/chat_prompt')&&++promptAttempts===1)
          throw Object.assign(new Error('fixture response was lost'),{code:'NETWORK_UNCERTAIN'});
        return original(path,options);
      };
    }''')
    page.fill('#chat-compose', 'original immutable request')
    page.press('#chat-compose', 'Enter')
    page.wait_for_function('chatView().pending && !chatView().busy')
    page.evaluate('''() => {
      chatView().files.push({file:'f'.repeat(32),name:'later-failed.png',size:0,
        ready:false,error:true,errorMessage:'fixture upload failed'});
      chatFiles();chatSyncChrome();
    }''')
    assert page.locator('.chat-file button:not(:disabled)').count() >= 1
    page.evaluate('chatSend(true)')
    assert page.evaluate('promptAttempts') == 2
    assert page.evaluate("requests.filter(request=>request.path.endsWith('/start')).length") == 1


def test_terminal_history_cannot_reactivate_lost_turn_or_approval(chat_page):
    page = chat_page
    page.evaluate("chatSwitch({id:'a'.repeat(32),project_id:'p1',provider:'pi',mode:'chat',status:'interrupted',title:'Stopped fixture',root:'/workspace/one',cwd:'/workspace/one'})")
    event(page, 'chat', {'type':'user','receipt':'lost-turn','text':'old request'}, 10)
    event(page, 'chat', {'type':'approval','receipt':'lost-turn','request_id':'old-confirmation',
                         'text':'Old approval','method':'confirm'}, 20)
    assert page.evaluate('chatView().active') is None
    assert page.locator('.chat-approval-actions button:not(:disabled)').count() == 0
    expect(page.locator('#chat-interrupt')).to_be_hidden()
    assert not page.evaluate("requests.some(request=>request.path.endsWith('/chat_answer'))")


def test_withdraw_pure_image_message_restores_attachment_not_empty_draft(chat_page):
    page = chat_page
    page.evaluate("chatSwitch({id:'a'.repeat(32),project_id:'p1',provider:'pi',mode:'chat',status:'running',title:'Queue fixture',root:'/workspace/one',cwd:'/workspace/one'})")
    page.evaluate('''async () => {
      const item={receipt:'withdraw-image',kind:'chat_prompt',state:'queued',text:'',
        attachments:[{file:'b'.repeat(32),name:'fixture.png',mime:'image/png',size:4,ready:true}]};
      chatView().queue=[item];
      await chatEditQueued(item);
    }''')
    files = page.evaluate('chatView().files.map(file=>({file:file.file,name:file.name,ready:file.ready}))')
    assert files == [{'file': 'b' * 32, 'name':'fixture.png', 'ready':True}]
    expect(page.locator('#chat-compose')).to_have_value('')
    expect(page.locator('.chat-file')).to_contain_text('fixture.png')


def dom_page(chat_browser_pool, html):
    browser = chat_browser_pool('chromium')
    page = browser.new_page(viewport={'width':800,'height':600})
    page.route('https://audit.fixture/**', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    page.goto('https://audit.fixture/')
    source = (ROOT / 'web/browser-extension/page.js').read_text(encoding='utf-8')
    page.add_script_tag(content=source.replace('export function pageCommand(', 'window.pageCommand = function(', 1))
    return page


def snapshot(page):
    result = page.evaluate("pageCommand({action:'snapshot'},['https://audit.fixture'])")
    assert result['ok'], result
    return result['data']


def action(page, observed, operation):
    return page.evaluate("request=>pageCommand(request,['https://audit.fixture'])", {
        'action':'action', 'document_id':observed['document_id'],
        'observation_token':observed['observation_token'], 'operation':operation})


def test_browser_snapshot_prioritizes_bottom_viewport_targets(chat_browser_pool):
    page = dom_page(chat_browser_pool, '<style>button{display:block;height:40px}</style>' +
                    ''.join(f'<button>Row {index}</button>' for index in range(250)))
    try:
        page.evaluate('scrollTo(0,document.body.scrollHeight)')
        observed = snapshot(page)
        assert any(item['label'] == 'Row 249' for item in observed['elements'])
        assert len(observed['elements']) <= 200 and observed['content_truncated']
    finally:
        page.context.close()


def test_browser_select_exposes_exact_options_and_key_targets_an_observed_element(chat_browser_pool):
    page = dom_page(chat_browser_pool, '''<label>Delivery<select>
      <option value="opaque_781">Ordinary</option><option value="opaque_946">Next day</option>
      <option value="">No preference</option><optgroup disabled label="Unavailable"><option value="off">Disabled</option></optgroup>
      </select></label><input aria-label="Keyboard target"><output>idle</output>
      <script>document.querySelector('input').addEventListener('keydown',event=>document.querySelector('output').textContent=event.key)</script>''')
    try:
        page.evaluate("() => {const option=new Option('Oversize opaque value','x'.repeat(1001));document.querySelector('select').append(option);}")
        observed = snapshot(page)
        target = next(item for item in observed['elements'] if item['tag'] == 'select')
        options = {item['label']:item for item in target['options']}
        assert options['Next day']['value'] == 'opaque_946'
        assert options['No preference']['value'] == ''
        assert options['Disabled']['disabled'] is True
        assert 'Oversize opaque value' not in options and target['options_truncated']
        result = action(page, observed, {'action':'select','element_id':target['id'],'value':options['Next day']['value']})
        assert result['ok'], result
        assert page.locator('select').input_value() == 'opaque_946'
        observed = snapshot(page)
        target = next(item for item in observed['elements'] if item['tag'] == 'input')
        result = action(page, observed, {'action':'key','element_id':target['id'],'value':'Enter'})
        assert result['ok'], result
        expect(page.locator('output')).to_have_text('Enter')
    finally:
        page.context.close()
