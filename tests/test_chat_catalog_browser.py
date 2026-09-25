"""Catalog reuse at the real browser boundary, without model inference."""
import pytest
from playwright.sync_api import expect
from tests.browser_support import chat_page, event
from tests.test_chat_complete_browser import send


def catalogs(page):
    return page.evaluate("requests.filter(r=>r.path.endsWith('/chat_catalog'))")


def choose(page, model):
    page.click('#chat-model-picker')
    page.locator(f'[data-model="{model}"]').click()


def test_project_session_and_directory_switches_reuse_model_catalog(chat_page):
    p = chat_page
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    assert len(catalogs(p)) == 1
    for project in ['p2', 'p1', 'p2']:
        p.select_option('#chat-project', project)
        expect(p.locator('#chat-effort-select')).to_be_enabled()
        assert not p.evaluate('chatView().catalogLoading')
    p.click('#chat-new');p.locator('#chat-project-results button:not(:disabled)').first.click()
    p.evaluate("chatSwitch(null, 'p2', {cwd:'src'})")
    p.click('#chat-model-picker')
    expect(p.locator('[data-model="remote/advanced-model"]')).to_be_visible()
    assert len(catalogs(p)) == 1
    # Shared choices must not claim that p1's defaults belong to p2/src.
    assert p.evaluate('chatView().catalog.model || null') is None
    assert p.evaluate('chatView().commands') is None


def test_model_thinking_levels_are_reused_and_drafts_keep_explicit_choices(chat_page):
    p = chat_page
    choose(p, 'remote/advanced-model')
    p.select_option('#chat-effort-select', 'high')
    assert len(catalogs(p)) == 2
    choose(p, 'local/native-model')
    choose(p, 'remote/advanced-model')
    p.select_option('#chat-effort-select', 'high')
    p.fill('#chat-compose', 'Keep this project draft')
    assert len(catalogs(p)) == 2
    p.select_option('#chat-project', 'p2')
    choose(p, 'remote/advanced-model')
    expect(p.locator('#chat-effort-select option[value="high"]')).to_have_count(1)
    p.select_option('#chat-project', 'p1')
    expect(p.locator('#chat-model-name')).to_have_text('Advanced Model')
    expect(p.locator('#chat-effort-select')).to_have_value('high')
    expect(p.locator('#chat-compose')).to_have_value('Keep this project draft')
    assert len(catalogs(p)) == 2


def test_codex_model_metadata_supplies_its_own_efforts_without_probe(chat_page):
    p = chat_page
    p.evaluate('''() => {
      const original=api;
      window.api=async(path,opts)=>{
        const data=await original(path,opts);
        if(path.endsWith('/chat_catalog')&&data.cli==='codex'){
          data.models.push({id:'fast-model',model:'fast-model',displayName:'Fast Model',
            defaultReasoningEffort:'minimal',supportedReasoningEfforts:[{reasoningEffort:'minimal'}]});
        }
        return data;
      };
    }''')
    p.select_option('#chat-provider', 'codex')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    count = len(catalogs(p))
    choose(p, 'fast-model')
    expect(p.locator('#chat-effort-select option[value="minimal"]')).to_have_count(1)
    expect(p.locator('#chat-effort-select option[value="high"]')).to_have_count(0)
    assert len(catalogs(p)) == count


def test_cache_is_isolated_by_node_cli_and_unknown_node(chat_page):
    p = chat_page
    p.evaluate("S.projects[1].device_id='node-2'")
    p.select_option('#chat-project', 'p2')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    assert len(catalogs(p)) == 2
    p.select_option('#chat-provider', 'codex')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    assert len(catalogs(p)) == 3
    p.select_option('#chat-provider', 'pi')
    p.select_option('#chat-project', 'p1')
    assert len(catalogs(p)) == 3
    p.evaluate('delete S.projects[1].device_id')
    p.select_option('#chat-project', 'p2')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    assert len(catalogs(p)) == 4


def test_switches_join_inflight_discovery_and_preserve_origin_defaults(chat_page):
    p = chat_page
    p.evaluate('ChatUI.catalogCache.clear();window.delayCatalog=true;void chatLoadCatalog()')
    p.wait_for_function('!!window.finishCatalog')
    p.select_option('#chat-project', 'p2')
    p.select_option('#chat-project', 'p1')
    assert len(catalogs(p)) == 2
    p.evaluate('window.delayCatalog=false;finishCatalog()')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    expect(p.locator('#chat-effort-select')).to_be_enabled()
    p.select_option('#chat-project', 'p2')
    expect(p.locator('#chat-model-name')).to_have_text('本机默认')
    assert len(catalogs(p)) == 2


def test_refresh_coalesces_and_keeps_cached_controls_usable(chat_page):
    p = chat_page
    p.evaluate('window.delayCatalog=true;void chatLoadCatalog(true);void chatLoadCatalog(true)')
    p.wait_for_function('!!window.finishCatalog')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    expect(p.locator('#chat-effort-select')).to_be_enabled()
    p.select_option('#chat-effort-select', 'high')
    p.select_option('#chat-project', 'p2')
    assert not p.evaluate('chatView().catalogLoading')
    assert len(catalogs(p)) == 2
    p.evaluate('window.delayCatalog=false;finishCatalog()')
    p.select_option('#chat-project', 'p1')
    expect(p.locator('#chat-effort-select')).to_have_value('high')
    assert len(catalogs(p)) == 2


def test_late_catalog_after_logout_cannot_repopulate_private_cache(chat_page):
    p = chat_page
    p.evaluate('window.delayCatalog=true;void chatLoadCatalog(true)')
    p.wait_for_function('!!window.finishCatalog')
    p.evaluate('window.oldCache=[...ChatUI.catalogCache.values()][0];chatDetach(true);S.session=null;S.page="login";finishCatalog()')
    p.wait_for_function('oldCache.pending.size===0')
    assert p.evaluate('ChatUI.catalogCache.size') == 0
    assert p.evaluate('ChatUI.views.size') == 0


def test_project_commands_load_on_demand_and_do_not_leak_across_directories(chat_page):
    p = chat_page
    p.evaluate('''() => {
      const original=api;
      window.api=async(path,opts)=>{
        const data=await original(path,opts);
        if(path.endsWith('/chat_catalog')&&JSON.parse(opts.body).args.include_commands){
          const {project,args}=JSON.parse(opts.body);
          data.commands=[{name:project+'-'+args.cwd,source:'skill'}];
        }
        return data;
      };
    }''')
    assert len(catalogs(p)) == 1 and catalogs(p)[0]['args']['include_commands'] is False
    p.click('#chat-commands')
    expect(p.locator('.chat-command-list')).to_contain_text('/p1-.')
    p.click('#chat-inspector-close')
    p.select_option('#chat-project', 'p2')
    assert len(catalogs(p)) == 2
    p.click('#chat-commands')
    expect(p.locator('.chat-command-list')).to_contain_text('/p2-.')
    expect(p.locator('.chat-command-list')).not_to_contain_text('/p1-.')
    p.evaluate("chatSwitch(null, 'p2', {cwd:'src'})")
    expect(p.locator('.chat-command-list')).to_contain_text('/p2-src')
    assert len(catalogs(p)) == 4
    assert all(r['args']['include_commands'] for r in catalogs(p)[1:])


def test_native_defaults_are_not_pinned_by_discovery_or_other_projects(chat_page):
    p = chat_page
    send(p, 'Use project one defaults')
    p.click('#chat-new');p.locator('#chat-project-results button:not(:disabled)').first.click()
    p.select_option('#chat-project', 'p2')
    send(p, 'Use project two defaults')
    starts = p.evaluate("requests.filter(r=>r.path.endsWith('/start'))")
    assert [r['project'] for r in starts] == ['p1', 'p2']
    assert all('model' not in r['args'] and 'effort' not in r['args'] for r in starts)
    assert len(catalogs(p)) == 1


def test_effort_interaction_fetches_unknown_project_defaults_only_when_needed(chat_page):
    p = chat_page
    p.select_option('#chat-project', 'p2')
    assert len(catalogs(p)) == 1
    p.focus('#chat-effort-select')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    p.select_option('#chat-effort-select', 'high')
    assert len(catalogs(p)) == 2
    assert catalogs(p)[-1]['project'] == 'p2'
    send(p)
    start = p.evaluate("requests.find(r=>r.path.endsWith('/start')).args")
    assert 'model' not in start and start['effort'] == 'high'


def test_live_native_efforts_and_commands_take_precedence_over_discovery(chat_page):
    p = chat_page
    p.click('#chat-commands')
    expect(p.locator('.chat-command-list')).to_contain_text('/skill:review')
    p.click('#chat-inspector-close')
    send(p)
    row = p.evaluate('ChatUI.selected')
    event(p, 'chat', {'type': 'settings', 'model': {'provider': 'local', 'id': 'native-model'},
                     'thinking_levels': ['off', 'xhigh'], 'thinkingLevel': 'xhigh'}, 10)
    event(p, 'chat', {'type': 'commands', 'commands': [{'name': 'session-only'}]}, 20)
    p.click('#chat-new');p.locator('#chat-project-results button:not(:disabled)').first.click()
    p.evaluate('row=>chatSwitch(row)', row)
    expect(p.locator('#chat-effort-select')).to_have_value('xhigh')
    expect(p.locator('#chat-effort-select option[value="high"]')).to_have_count(0)
    p.click('#chat-commands')
    expect(p.locator('.chat-command-list')).to_contain_text('/session-only')
    expect(p.locator('.chat-command-list')).not_to_contain_text('/skill:review')
    assert len(catalogs(p)) == 2


@pytest.mark.parametrize('resume_button', [False, True])
def test_explicit_reset_to_defaults_is_preserved_when_resuming(chat_page, resume_button):
    p = chat_page
    send(p)
    event(p, 'chat', {'type': 'settings', 'model': {'provider': 'remote', 'id': 'advanced-model'},
                     'thinkingLevel': 'high'}, 10)
    event(p, 'session', {'status': 'exited'}, 20)
    p.click('#chat-model-picker')
    p.get_by_text('恢复本机默认模型', exact=True).click()
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    p.select_option('#chat-effort-select', '')
    if resume_button:
        p.evaluate('chatResume()')
    else:
        send(p, 'Continue with defaults')
    start = p.evaluate("requests.filter(r=>r.path.endsWith('/start')).at(-1).args")
    assert start['continue_session']
    assert start['model'] == '' and start['effort'] == ''


def test_late_model_probe_does_not_overwrite_a_newer_choice(chat_page):
    p = chat_page
    p.evaluate('window.delayCatalog=true')
    choose(p, 'remote/advanced-model')
    p.wait_for_function('!!window.finishCatalog')
    choose(p, 'local/native-model')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    p.evaluate('window.delayCatalog=false;finishCatalog()')
    p.wait_for_function('[...ChatUI.catalogCache.values()][0].pending.size===0')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    choose(p, 'remote/advanced-model')
    expect(p.locator('#chat-effort-select option[value="high"]')).to_have_count(1)
    assert len(catalogs(p)) == 2


def test_switching_back_during_command_load_joins_and_finishes_the_same_request(chat_page):
    p = chat_page
    p.evaluate('window.delayCatalog=true')
    p.click('#chat-commands')
    p.wait_for_function('!!window.finishCatalog')
    p.click('#chat-inspector-close')
    p.select_option('#chat-project', 'p2')
    p.select_option('#chat-project', 'p1')
    p.click('#chat-commands')
    assert len(catalogs(p)) == 2
    p.evaluate('window.delayCatalog=false;finishCatalog()')
    expect(p.locator('.chat-command-list')).to_contain_text('/skill:review')
    p.wait_for_function('!chatView().commandsLoading')
    expect(p.locator('#chat-inspector-body')).not_to_contain_text('正在加载本机指令')


def test_switching_back_during_refresh_receives_new_models_without_a_second_request(chat_page):
    p = chat_page
    p.evaluate('''() => {
      const original=api;
      window.api=async(path,opts)=>{
        const data=await original(path,opts);
        if(path.endsWith('/chat_catalog'))data.models[0].name='Updated Model';
        return data;
      };
      window.delayCatalog=true;void chatLoadCatalog(true);
    }''')
    p.wait_for_function('!!window.finishCatalog')
    p.select_option('#chat-project', 'p2')
    p.select_option('#chat-project', 'p1')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    assert len(catalogs(p)) == 2
    p.evaluate('window.delayCatalog=false;finishCatalog()')
    expect(p.locator('#chat-model-name')).to_have_text('Updated Model')
    assert len(catalogs(p)) == 2
