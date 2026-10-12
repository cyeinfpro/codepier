"""Shared synthetic Weixin browser fixtures; never calls Tencent."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def full_styles(page):
    index = (ROOT / "web/index.html").read_text()
    names = re.findall(r'<link rel="stylesheet" href="/static/([^?"]+)', index)
    assert "collaboration-channels.css" in names and "collaboration.css" in names
    page.add_style_tag(content="\n".join((ROOT / "web" / name).read_text() for name in names))
    page.evaluate("""() => {
      const drawer=document.querySelector('#drawer');
      drawer.removeAttribute('style');drawer.className='cc-drawer-body';
      const dialog=document.createElement('dialog');dialog.id='cc-drawer';dialog.className='cc-drawer';
      const header=document.createElement('header');header.className='cc-drawer-header';
      header.innerHTML='<h2>消息连接 · 合成验收</h2>';
      drawer.before(dialog);dialog.append(header,drawer);dialog.showModal();
    }""")


@pytest.fixture(params=["chromium", "webkit"])
def qr_page(request):
    with sync_playwright() as playwright:
        kind = getattr(playwright, request.param)
        executable = os.environ.get("CODEPIER_TEST_CHROMIUM") if request.param == "chromium" else None
        if not executable and not Path(kind.executable_path).is_file():
            pytest.skip(request.param + " runtime is not installed; no browser result claimed")
        browser = kind.launch(headless=True, **({"executable_path": executable} if executable else {}))
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content("<style>body{font:16px sans-serif;margin:16px}*{box-sizing:border-box}"
                         ".cc-field{display:grid}#drawer{max-width:800px;margin:auto}"
                         "button{padding:8px}" +
                         (ROOT / "web/collaboration-channels.css").read_text() +
                         "</style><button id='open'>管理通道</button><main id='drawer'></main>")
        page.clock.install()
        page.add_script_tag(path=str(ROOT / "web/collaboration-channels.js"))
        page.evaluate(r"""() => {
          window.S={session:{id:'synthetic-session'},space_id:'synthetic-space'};
          window.state={generation:1,drawerEpoch:0,project:'projectA',environment:'test',
            conversation:'conversationA',snapshot:{can_manage:true,
              capabilities:{room_scope_v1:true,telegram_channel_v1:true},
              room:{id:'roomA',project_id:'projectA'},
              conversation:{id:'conversationA',projects:[
                {project_id:'projectA',project_label:'合成项目',environment_id:'test'}]}}};
          const channel=(name, enabled)=>({enabled,setting:{channel:name,enabled,version:0,scope:'owner',
            source:'default'},accounts:[],bindings:[],deliveries:[],reply_candidates:[],artifact_candidates:[]});
          window.data={scopes:{items:[],eligible_connections:[]},
            weixin:channel('weixin',true),telegram:channel('telegram',false)};
          window.writes=[];window.reads=[];window.statuses=[];window.statusResponses=[];
          window.startFail=false;window.statusFail=false;window.acceptFail=false;window.cancelFail=false;
          window.pauseStart=false;window.pauseStatus=false;window.pauseAccept=false;window.acceptError=null;
          window.confirmResult=true;window.confirmations=[];
          window.confirm=text=>{confirmations.push(text);return confirmResult};
          window.nextState='wait';window.counter=0;
          window.entry=null;
          window.makeEntry=()=>({
            enrollment_id:'enrollment-'+(++counter),state:nextState,version:1,
            expires_at:Date.now()/1000+300,scanner_id:nextState==='confirmed'?'scanner-123':'',
            qrcode_img_content:'https://attacker.invalid/must-not-request',
            qrcode_image:'data:image/svg+xml;base64,'+btoa('<svg xmlns="http://www.w3.org/2000/svg" width="29" height="29"><rect width="29" height="29" fill="white"/><path d="M4 4h7v7H4z" fill="black"/></svg>'),
            token:'MUST-NOT-RETAIN-SYNTHETIC-TOKEN'
          });
          const starts=new Map();const accepted=new Map();
          window.api=async(url,options={})=>{
            if(options.method==='POST'){
              if(url!=='/api/collaboration/weixin-qr-status')throw Error('unexpected route');
              const args=JSON.parse(options.body);statuses.push({args,retrySafe:options.retrySafe});
              if(pauseStatus)await new Promise(resolve=>window.releaseStatus=resolve);
              if(statusFail)throw Error('synthetic lost response');
              if(statusResponses.length)entry={...entry,...statusResponses.shift()};
              return structuredClone(entry);
            }
            reads.push(url);
            return structuredClone(url.includes('room-scopes')?data.scopes:
              url.includes('/telegram?')?data.telegram:data.weixin);
          };
          window.E=value=>String(value??'').replace(/[&<>"']/g,
            x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
          window.channels=CodePierCollaborationChannels.create({
            state,E,field:(label,input)=>'<label class="cc-field"><span>'+E(label)+'</span>'+input+'</label>',
            button:(action,label,item={},attrs='')=>'<button type="button" class="btn" data-cc-action="'+E(action)+
              '" data-id="'+E(item.id||'')+'" '+attrs+'>'+E(label)+'</button>',
            mentionSlots:()=>[{id:'dotA',label:'合成 dot',duplex:true,state:'registered'}],
            showDrawer:(title,body)=>{state.drawerEpoch++;document.querySelector('#drawer').innerHTML=body},
            mutation:async(operation,args)=>{
              writes.push({operation,args:structuredClone(args)});
              if(operation==='weixin-qr-start'){
                if(!starts.has(args.idempotency_key))starts.set(args.idempotency_key,makeEntry());
                entry=starts.get(args.idempotency_key);
                if(pauseStart)await new Promise(resolve=>window.releaseStart=resolve);
                if(startFail)throw Error('synthetic lost response');
                return structuredClone(entry);
              }
              if(operation==='weixin-qr-cancel'){
                if(cancelFail)throw Error('synthetic cancellation unconfirmed');
                entry={...entry,state:'cancelled',version:entry.version+1};
                return structuredClone(entry);
              }
              if(operation==='weixin-qr-accept'){
                if(acceptError)throw Object.assign(Error('synthetic rejection'),acceptError);
                if(!accepted.has(args.idempotency_key)){
                  const result={account_id:'accountA',bot_id:'botA',credential_version:1,room_bound:false};
                  accepted.set(args.idempotency_key,result);
                  data.weixin.accounts=[{id:'accountA',label:args.label,bot_id:'botA',state:'active'}];
                }
                if(pauseAccept)await new Promise(resolve=>window.releaseAccept=resolve);
                if(acceptFail)throw Error('synthetic saved but response lost');
                return structuredClone(accepted.get(args.idempotency_key));
              }
              if(operation==='channel-settings'){
                const ch=data[args.channel];ch.enabled=args.enabled;
                ch.setting={...ch.setting,enabled:args.enabled,version:ch.setting.version+1,source:'panel'};
              }
              return {ok:true};
            }
          });
          window.clickAction=action=>channels.act(action,document.querySelector('[data-cc-action="'+action+'"]'));
        }""")
        page.evaluate("channels.act('dot-channel-open',document.querySelector('#open'))")
        page.evaluate("clickAction('dot-channel-view-weixin')")
        yield page, request.param
        page.evaluate("channels.clear()")
        assert not errors
        browser.close()
