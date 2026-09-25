"""Exercise shipped core bytes in a real DOM, including disposal and identity."""
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def page(chat_browser_pool):
    browser = chat_browser_pool('chromium')
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_content('<main id="app"><button id="origin">Origin</button></main><div id="modal-root"></div><div id="toasts"></div><div id="fixture"></div>')
    page.add_script_tag(path=str(ROOT / 'web/core/bundle.js'))
    try:
        yield page
        assert not errors
    finally:
        page.context.close()


def test_text_primitives_never_turn_user_text_into_markup(page):
    result = page.evaluate('''() => {
      const unsafe='<img src=x onerror="globalThis.injected=true"> & \\' "';
      CP.ui.toast(unsafe,true);CP.ui.toast(unsafe,true);
      document.querySelector('#fixture').innerHTML=CP.ui.badge(unsafe)+CP.ui.empty(unsafe);
      return {injected:!!globalThis.injected, images:document.querySelectorAll('img').length,
        toasts:document.querySelectorAll('.toast').length,text:document.querySelector('.toast-message').textContent,
        expected:unsafe};
    }''')
    assert result['text'] == result['expected']
    assert not result['injected'] and result['images'] == 0 and result['toasts'] == 1


def test_busy_restores_original_nodes_handlers_and_previous_styles_after_error(page):
    result = page.evaluate('''async () => {
      const button=document.querySelector('#origin'),node=button.firstChild,errors=[];
      let events=0;button.addEventListener('custom',()=>events++);
      button.style.setProperty('min-width','47px','important');button.setAttribute('aria-busy','false');
      await CP.ui.busy(button,async()=>{
        if(!button.disabled||button.getAttribute('aria-busy')!=='true')throw Error('bad guard');
        throw Error('expected failure');
      },message=>errors.push(message));
      button.dispatchEvent(new Event('custom'));
      return {same:button.firstChild===node,disabled:button.disabled,busy:button.getAttribute('aria-busy'),
        width:button.style.minWidth,priority:button.style.getPropertyPriority('min-width'),events,errors};
    }''')
    assert result == {'same': True, 'disabled': False, 'busy': 'false', 'width': '47px', 'priority': 'important', 'events': 1, 'errors': ['expected failure']}


def test_actions_are_explicit_atomic_and_single_flight_per_button(page):
    result = page.evaluate('''async()=>{
      const actions=CP.actions.create(),button=document.querySelector('#origin');let calls=0,release;
      actions.register(['save','save-alias'],async()=>{calls++;await new Promise(resolve=>release=resolve);});
      const first=actions.dispatch('save',button),second=await actions.dispatch('save-alias',button);
      release();const completed=await first;let rejected=false;
      try{actions.register(['new-name','save'],()=>{});}catch{rejected=true;}
      actions.register(['broken'],()=>{throw Error('fixture');});
      let failures=0;for(let i=0;i<2;i++){try{await actions.dispatch('broken',button);}catch{failures++;}}
      return {completed,second,calls,rejected,names:actions.names(),failures,unknown:await actions.dispatch('unknown',button)};
    }''')
    assert result == {'completed': True, 'second': False, 'calls': 1, 'rejected': True, 'names': ['save', 'save-alias', 'broken'], 'failures': 2, 'unknown': False}


def test_view_owner_change_and_detach_cancel_stale_callbacks(page):
    result = page.evaluate('''async()=>{
      let owner={},events=0,timers=0;
      const panel=CP.createPanel({owner:()=>owner}),root=document.querySelector('#fixture');
      const old=panel.mount(root);old.listen(root,'probe',()=>events++);root.dispatchEvent(new Event('probe'));
      old.later(()=>timers++,0);owner={};root.dispatchEvent(new Event('probe'));
      await new Promise(resolve=>setTimeout(resolve,10));
      const fresh=panel.mount(root);fresh.listen(root,'probe',()=>events++);root.dispatchEvent(new Event('probe'));
      fresh.later(()=>timers++,0);panel.detach();root.dispatchEvent(new Event('probe'));
      await new Promise(resolve=>setTimeout(resolve,10));
      return {events,timers,oldAborted:old.signal.aborted,freshAborted:fresh.signal.aborted,current:fresh.current()};
    }''')
    assert result == {'events': 2, 'timers': 0, 'oldAborted': True, 'freshAborted': True, 'current': False}


def test_keyed_snapshot_preserves_reordered_nodes_and_local_control_state(page):
    result = page.evaluate('''()=>{
      const root=document.querySelector('#fixture');
      root.innerHTML='<article data-cp-key="a"><input id="filter" value="server"><details id="details"><summary>More</summary>Old</details><button id="working" aria-busy="true"><span>In flight</span></button></article><article data-cp-key="b">Before</article>';
      const a=root.firstChild,b=root.lastChild,input=root.querySelector('input'),details=root.querySelector('details'),button=root.querySelector('button'),child=button.firstChild;
      input.value='local draft';input.focus();input.setSelectionRange(2,5);details.open=true;
      CP.dom.replacePage(root,'<article data-cp-key="b">Updated</article><article data-cp-key="a"><input id="filter" value="fresh server"><details id="details"><summary>More</summary>New</details><button id="working">Replacement</button></article>');
      return {a:root.lastChild===a,b:root.firstChild===b,value:input.value,focused:document.activeElement===input,
        selection:[input.selectionStart,input.selectionEnd],open:details.open,content:details.textContent,
        busyChild:button.firstChild===child,busy:button.getAttribute('aria-busy'),updated:b.textContent};
    }''')
    assert result == {'a': True, 'b': True, 'value': 'local draft', 'focused': True, 'selection': [2, 5], 'open': True, 'content': 'MoreNew', 'busyChild': True, 'busy': 'true', 'updated': 'Updated'}


def test_clean_controls_update_but_dirty_selection_and_checkbox_are_preserved(page):
    result = page.evaluate('''()=>{
      const root=document.querySelector('#fixture');
      root.innerHTML='<input id="clean" value="before"><input id="checked" type="checkbox"><select id="selection"><option value="a">A</option><option value="b">B</option></select>';
      root.querySelector('#checked').checked=true;root.querySelector('select').value='b';
      CP.dom.replacePage(root,'<input id="clean" value="after"><input id="checked" type="checkbox"><select id="selection"><option value="a" selected>AA</option><option value="b">BB</option></select>');
      return {clean:root.querySelector('#clean').value,checked:root.querySelector('#checked').checked,selection:root.querySelector('select').value};
    }''')
    assert result == {'clean': 'after', 'checked': True, 'selection': 'b'}


def test_modal_expected_identity_prevents_old_completion_closing_new_dialog(page):
    result = page.evaluate('''()=>{
      const state={session:{}},origin=document.querySelector('#origin');origin.focus();
      const dialogs=CP.ui.createModal({state,labelFields:()=>{},trapTab:()=>{},focusable:root=>[...root.querySelectorAll('button')]});
      const old=dialogs.modal('First','<input id="first">');const fresh=dialogs.modal('Second','<input id="second">');
      dialogs.closeModal(old);const retained=document.querySelector('.modal')===fresh;
      document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}));
      return {retained,closed:!document.querySelector('.modal'),inert:document.querySelector('#app').inert,restored:document.activeElement===origin,cleanup:state.modalCleanup};
    }''')
    assert result == {'retained': True, 'closed': True, 'inert': False, 'restored': True, 'cleanup': None}
