"""Browser regressions for hint reconciliation and one-entry team selection."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.browser


def boot_hint_editor(page):
    page.set_content("""<div class="collaboration cc-workspace cc-relay-focus">
      <div class="cc-feed" style="height:140px;overflow:auto">
        <div class="cc-message-list" style="min-height:600px"><article data-message-id="m">
          <div data-rich-message="m">A message kept while composing</div></article></div>
        <div id="cc-relay-outbox"></div>
      </div>
      <form id="cc-command"><textarea id="cc-message-input" name="request"
        style="width:300px;font-size:16px">中文输入中的草稿</textarea><button type="submit">Send</button></form>
      <div id="cc-relay-network" hidden></div></div>""")
    page.evaluate("""() => {
      window.S={session:'s',space_id:'space',page:'collaboration'};
      window.hintSources=[];
      window.EventSource=class {
        constructor(url){this.url=url;this.listeners={};this.closed=false;hintSources.push(this);}
        addEventListener(name,fn){this.listeners[name]=fn;}
        close(){this.closed=true;}
        emit(name,data){this.listeners[name]?.({data:JSON.stringify(data)});}
      };
      window.metrics={patches:0,workspace:0,api:0,writes:0};
      window.resolutions=[];
      window.api=(url,options={})=>{
        metrics.api++;if(options.method)metrics.writes++;
        return new Promise(resolve=>resolutions.push(resolve));
      };
      window.record={id:'m',body_text:'A message kept while composing'};
      window.slot={id:'dot',duplex:true,state:'registered'};
      window.c={messages:[record],mentions:['dot'],reply:'',draft:'中文输入中的草稿',
        relayOutbox:[],visibility:'v',after:'a'};
      window.state={session:'s',space:'space',project:'p',environment:'production',
        generation:1,refreshSequence:0,chats:new Map([['c',c]]),members:[],
        snapshot:{room:{state:'active'},can_manage:true,join_slots:[slot],
          connection_readiness:{},teams:[],
          capabilities:{native_dot_relay:true,relay_hints:true}}};
      window.reply=()=>({visibility_token:'v',server_now:100,after_cursor:'a',
        items:[],updates:[],task_states:[],join_slots:[{...slot}],connection_readiness:{},
        teams:[],room_state:'active',can_manage:true,has_more:false});
      window.chatRichMarkdown=(node,value)=>{node.textContent=value;node._chatSource=value;};
    }""")
    page.add_script_tag(path=str(ROOT / "web/collaboration-runtime.js"))
    page.add_script_tag(path=str(ROOT / "web/collaboration-relay.js"))
    page.evaluate("""() => {
      window.relay=CodePierCollaborationRelay.create({state,E:String,button:()=>'',chat:()=>c,
        chatScope:()=>({project:'p',environment_id:'production',conversation_id:'room'}),
        mentionSlots:()=>[slot],messageById:()=>record,messageText:m=>m.body_text,requestId:()=> 'id',
        dots:{isConversationMessage:()=>true,refreshCards:()=>{}},
        delegation:{messageCard:()=>'',selectPolicy:()=>{}},
        patchTimeline:()=>metrics.patches++,patchDraftContext:()=>{},
        patchDelegationComposer:()=>{},mergeMessages:items=>{c.messages=CodePierCollaborationRuntime.merge(c.messages,items);},
        recoverAccess:()=>{},onWorkspaceChange:()=>metrics.workspace++});
      relay.bind(document.querySelector('.collaboration'));
      window.editor=document.querySelector('textarea');
      window.messageNode=document.querySelector('[data-message-id]');
      editor.focus();editor.setSelectionRange(2,5);
      document.querySelector('.cc-feed').scrollTop=120;
      editor.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:'输入'}));
      state.composing=true;hintSources[0].onopen();
    }""")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_hints_during_ime_are_single_flight_and_preserve_editor(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
    )
    page = context.new_page()
    try:
        boot_hint_editor(page)
        assert page.evaluate("new URL(hintSources[0].url,'https://panel.example').searchParams.get('space_id')") == 'space'
        page.evaluate("hintSources[0].emit('change',{revision:'r1'})")
        page.wait_for_function("metrics.api === 1")
        page.evaluate("hintSources[0].emit('change',{revision:'r2'})")
        page.wait_for_function("c.relayRefreshPending === true")
        assert page.evaluate("metrics.api") == 1
        page.evaluate("resolutions.shift()(reply())")
        page.wait_for_function("metrics.api === 2")
        page.evaluate("resolutions.shift()(reply())")
        page.wait_for_function("c.relayRenderStats?.syncs === 2")
        assert page.evaluate("""() => ({
          sameEditor:editor===document.querySelector('textarea'),
          sameMessage:messageNode===document.querySelector('[data-message-id]'),
          focused:document.activeElement===editor,value:editor.value,
          selection:[editor.selectionStart,editor.selectionEnd],
          scroll:document.querySelector('.cc-feed').scrollTop,
          composing:state.composing,patches:metrics.patches,workspace:metrics.workspace,
          unchanged:c.relayRenderStats.unchanged,writes:metrics.writes
        })""") == {
            "sameEditor": True, "sameMessage": True, "focused": True,
            "value": "中文输入中的草稿", "selection": [2, 5], "scroll": 120,
            "composing": True, "patches": 0, "workspace": 0, "unchanged": 2, "writes": 0,
        }
        page.evaluate("""() => {
          state.composing=false;
          editor.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'输入'}));
          relay.detach();hintSources[0].emit('change',{revision:'stale'});
        }""")
        page.wait_for_timeout(100)
        assert page.evaluate("metrics.api") == 2
        assert page.evaluate("hintSources[0].closed")
    finally:
        context.close()


def boot_team_picker(page):
    page.set_content("""<main class="collaboration">
      <textarea id="cc-message-input" style="font-size:16px">保留我的中文需求</textarea>
      <div id="chip"></div><div id="drawer"></div></main>""")
    page.add_style_tag(path=str(ROOT / "web/collaboration-workspace.css"))
    page.add_style_tag(content="""
      body{margin:0;padding:12px;box-sizing:border-box} *{box-sizing:border-box}
      .cc-form{display:grid;gap:12px}.cc-field{display:grid;gap:5px}
      .cc-project-choices{display:grid;gap:8px;min-width:0}
      input,select,textarea{max-width:100%;font-size:16px}
      .cc-row{display:flex;justify-content:space-between;flex-wrap:wrap}
      .cc-two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
      button{min-height:44px}
    """)
    page.add_script_tag(path=str(ROOT / "web/collaboration-goals.js"))
    page.evaluate("""() => {
      const E=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.S={session:'s',space_id:'space',page:'collaboration'};
      window.calls=[];window.c={draft:'保留我的中文需求',mentions:[],reply:'',teamId:''};
      window.slots=['a','b'].map(id=>({id,label:id,task_dot:true,duplex:true,state:'registered'}));
      window.state={generation:1,project:'p',environment:'production',conversation:'room',
        session:'s',space:'space',snapshot:{can_manage:true,room:{id:'room',state:'active'},
          teams:[],capabilities:{multidot_teams:true}}};
      window.editor=document.querySelector('textarea');editor.focus();editor.setSelectionRange(1,4);
      const button=(action,label,row={})=>'<button type="button" data-cc-action="'+action+'" data-id="'+E(row.id)+'" data-version="'+E(row.version)+'">'+E(label)+'</button>';
      window.ui=CodePierCollaborationGoals.create({state,E,button,field:(label,control)=>'<label class="cc-field">'+E(label)+control+'</label>',
        empty:value=>'<p>'+E(value)+'</p>',getRecord:async()=>({}),
        mutation:async(op,args)=>{
          calls.push({op,args});
          const team=op==='team-create'?{id:'team',version:1,state:'active',expires_at:Date.now()/1000+86400,
            label:args.label,coordinator_dot_id:args.coordinator_dot_id,
            participants:args.dot_ids.map(dot_id=>({dot_id,label:dot_id})),
            project_ids:args.project_ids,capabilities:args.capabilities}:
            {...state.snapshot.teams[0],version:2,state:'paused'};
          return {team};
        },showDrawer:(title,markup)=>{document.querySelector('#drawer').innerHTML=markup;},
        closeDrawer:()=>{document.querySelector('#drawer').innerHTML='';},
        projectLabel:id=>id,roomProjects:()=>[{project_id:'p',environment_id:'production'}],
        when:value=>String(value),chat:()=>c,mentionSlots:()=>slots,selectPolicy:()=>{},
        patchDraftContext:()=>{document.querySelector('#chip').innerHTML=ui.teamChip();},
        patchDelegationComposer:()=>{}});
      document.addEventListener('change',event=>ui.change(event));
      document.addEventListener('submit',event=>{event.preventDefault();window.submission=ui.submit(event.target);});
      document.addEventListener('click',event=>{
        const button=event.target.closest('[data-cc-action]');
        if(button)window.action=ui.act(button.dataset.ccAction,button);
      });
      ui.act('team-new',document.querySelector('#chip'));
    }""")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_team_creation_requires_explicit_scope_and_keeps_one_coordinator(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        boot_team_picker(page)
        page.check("input[name=dot_ids][value=a]")
        page.check("input[name=dot_ids][value=b]")
        page.select_option("select[name=coordinator_dot_id]", "b")
        page.click("#cc-team-create button[type=submit]")
        assert page.evaluate("calls.length") == 0
        page.check("input[name=confirm_tasks]")
        page.click("#cc-team-create button[type=submit]")
        page.wait_for_function("calls.length === 1 && c.teamId === 'team'")
        assert page.evaluate("""() => ({
          operation:calls[0].op,confirm:calls[0].args.confirm_tasks,members:calls[0].args.dot_ids,
          coordinator:calls[0].args.coordinator_dot_id,capabilities:calls[0].args.capabilities,
          mentions:c.mentions,draft:c.draft,value:editor.value,
          same:editor===document.querySelector('textarea'),selection:[editor.selectionStart,editor.selectionEnd]
        })""") == {
            "operation": "team-create", "confirm": True, "members": ["a", "b"],
            "coordinator": "b", "capabilities": ["read"], "mentions": ["b"],
            "draft": "保留我的中文需求", "value": "保留我的中文需求",
            "same": True, "selection": [1, 4],
        }
        page.evaluate("document.querySelector('#drawer').innerHTML=ui.teamPicker()")
        page.click(".cc-team-controls summary")
        page.click("[data-cc-action=team-pause]")
        page.wait_for_function("state.snapshot.teams[0].state === 'paused'")
        assert page.evaluate("ui.teamAvailable('team','b')") is False
        assert page.evaluate("c.teamId") == "team"
        assert page.locator(".cc-team-context").inner_text().find("已暂停") >= 0
        assert page.evaluate("editor.value") == "保留我的中文需求"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()
