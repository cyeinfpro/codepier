"""Real Chromium/WebKit settings flow using synthetic account data only."""

from tests.browser_launch import launch_browser
import os
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1440, 390])
def test_telegram_settings_identity_consent_stale_and_duplicate_guards(
    tmp_path, engine, width
):
    with sync_playwright() as pw:
        browser_type = getattr(pw, engine)
        if not Path(browser_type.executable_path).is_file():
            pytest.skip("Compatible " + engine + " is not installed")
        browser = launch_browser(browser_type, headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            css = (ROOT / "web/collaboration-channels.css").read_text()
            page.set_content(
                "<style>body{margin:16px;font:15px sans-serif;--ui-line:#d8dde5;--ui-muted:#536176;--ui-panel:#fafbfd}.cc-field{display:grid;gap:4px}.btn{padding:10px}#drawer{max-width:850px;margin:auto}*{box-sizing:border-box}input,textarea,select{max-width:100%}</style><style>"
                + css
                + '</style><button id="open">通道与范围</button><div id="drawer"></div><div id="files"></div>'
            )
            page.add_script_tag(path=str(ROOT / "web/collaboration-channels.js"))
            page.evaluate("""() => {
                window.S={session:{id:'fixture'},space_id:'space'};
                window.state={generation:1,drawerEpoch:0,project:'projectA',environment:'production',conversation:'conversationA',
                    snapshot:{can_manage:true,capabilities:{room_scope_v1:true,telegram_channel_v1:true},room:{project_id:'projectA'}}};
                window.data={scopes:{items:[],eligible_connections:[]},
                    weixin:{enabled:true,accounts:[],bindings:[{id:'wxA',state:'active',version:3,generation:1,expires_at:1900000000,peer_id:'synthetic-weixin'}]},
                    telegram:{enabled:true,accounts:[{id:'tgAccount',label:'测试机器人',bot_id:'900001',state:'active',credential_version:1}],
                        bindings:[{id:'tgBinding',state:'awaiting_owner',version:2,generation:1,pending_user_id:'101',pending_chat_id:'101',expires_at:1900000000}],deliveries:[]}};
                window.reads=[];window.previewReads=[];window.writes=[];window.delayed=[];window.delayReads=false;window.delayWrites=false;
                window.api=async (url,options={})=>{reads.push(url);if(delayReads)await new Promise(resolve=>delayed.push(resolve));
                    if(url==='/api/collaboration/channel-enrollment-preview'){
                        previewReads.push({method:options.method,args:JSON.parse(options.body),retrySafe:options.retrySafe});
                        throw new Error('Synthetic legacy binding has no new owner-task preapproval');
                    }
                    return structuredClone(url.includes('room-scopes')?data.scopes:url.includes('/telegram?')?data.telegram:data.weixin)};
                window.E=v=>String(v??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
                window.channels=CodePierCollaborationChannels.create({state,E,
                    field:(label,input)=>'<label class="cc-field"><span>'+E(label)+'</span>'+input+'</label>',
                    button:(action,label,row={},attrs='')=>'<button type="button" class="btn" data-cc-action="'+E(action)+'" data-id="'+E(row.id||'')+'" '+attrs+'>'+E(label)+'</button>',
                    mentionSlots:()=>[{id:'dotA',label:'当前dot',duplex:true,state:'registered'}],
                    showDrawer:(title,body)=>{state.drawerEpoch++;document.querySelector('#drawer').innerHTML=body},
                    mutation:async(operation,args)=>{writes.push({operation,args});if(operation==='telegram-files-preview')return {preview_digest:'f'.repeat(64),recipient:{user_id:'101',chat_id:'101'},message_id:'replyA',message_version:1,total_bytes:23,files:[{name:'result.png',bytes:23,sha256:'a'.repeat(64)}]};if(delayWrites)await new Promise(resolve=>window.resolveWrite=resolve);}
                });
            }""")
            page.evaluate(
                "channels.act('dot-channel-open',document.querySelector('#open'))"
            )
            page.evaluate("channels.act('dot-channel-view-telegram',document.querySelector('[data-cc-action=dot-channel-view-telegram]'))")
            # Three existing settings reads plus one explicit read-only scope
            # preview. A legacy binding still cannot inherit owner-task consent.
            assert page.evaluate("reads.length") == 4
            assert page.evaluate("previewReads") == [{
                "method": "POST", "retrySafe": True, "args": {
                    "project": "projectA", "environment_id": "production",
                    "conversation_id": "conversationA", "channel": "telegram",
                    "account_id": "tgAccount", "dot_id": "dotA",
                },
            }]
            assert page.locator('[data-channel-form="enrollment"] [name="enrollment_agreement"]').is_disabled()
            assert page.evaluate("writes.length") == 0
            assert "每个私聊明确绑定一个协作室" in page.locator(".cc-tg-settings").inner_text()
            assert "仍可能显示通知" not in page.locator(".cc-tg-binding").inner_text()
            page.evaluate(
                "channels.act('dot-channel-tg-activate',document.querySelector('[data-cc-action=dot-channel-tg-activate]'))"
            )
            assert page.evaluate("writes.length") == 0
            page.locator("[data-channel-form=tg-activate] input").check()
            page.evaluate("""() => {
                delayWrites=true;
                window.first=channels.act('dot-channel-tg-activate',document.querySelector('[data-cc-action=dot-channel-tg-activate]'));
                window.second=channels.act('dot-channel-tg-activate',document.querySelector('[data-cc-action=dot-channel-tg-activate]'));
            }""")
            assert page.evaluate("writes.length") == 1
            args = page.evaluate("writes[0].args")
            assert (
                args["confirm_same_owner"] is True
                and args["confirm_user_id"] == "101"
                and args["confirm_chat_id"] == "101"
            )
            assert (
                args["conversation_id"] == "conversationA"
                and args["expected_version"] == 2
            )
            page.evaluate("resolveWrite(); delayWrites=false")
            page.evaluate("first")
            page.evaluate("second")
            page.locator('[data-channel-form="tg-bind"]').locator("..").locator("summary").click()
            group = page.locator("[data-channel-form=tg-bind]")
            group.locator("[name=account_id]").select_option("tgAccount")
            group.locator("[name=dot_id]").select_option("dotA")
            group.locator("[name=ingress]").check()
            page.evaluate(
                "channels.act('dot-channel-tg-bind',document.querySelector('[data-cc-action=dot-channel-tg-bind]'))"
            )
            assert page.evaluate("writes.length") == 1
            group.locator("[name=mirror]").check()
            group.locator("[name=silent]").check()
            page.evaluate(
                "channels.act('dot-channel-tg-bind',document.querySelector('[data-cc-action=dot-channel-tg-bind]'))"
            )
            assert page.evaluate("writes.length") == 2
            assert page.evaluate("writes[1].args.disable_notification") is True
            assert page.evaluate("writes[1].args.confirm_full_mirror") is True
            page.evaluate("""() => {
                document.querySelector('#files').innerHTML=CodePierCollaborationChannels.attachments({
                    channel_deliveries:[{channel:'telegram',state:'needs_attention',parts:[{ordinal:0,kind:'text',state:'unknown',reason_code:'safe_error'}]},
                      {channel:'weixin',state:'accepted',parts:[{ordinal:0,kind:'text',state:'accepted'}]}]},E)
            }""")
            assert (
                "Telegram" in page.locator("#files").inner_text()
                and "微信" in page.locator("#files").inner_text()
            )
            assert "禁止自动重发" in page.locator("#files").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            evidence = Path(os.environ.get("CODEPIER_TEST_EVIDENCE_DIR", str(tmp_path)))
            evidence.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(evidence / f"telegram-settings-{engine}-{width}.png"),
                full_page=True,
            )

            # Independent file preview is read-only; only exact confirmed selection queues egress.
            page.evaluate("""() => {
                data.telegram.bindings[0]={...data.telegram.bindings[0],state:'active',version:3,user_id:'101',chat_id:'101'};
                data.telegram.reply_candidates=[{id:'replyA',version:1,body_text:'已生成图片与说明'}];
                data.telegram.artifact_candidates=[{artifact_id:'artifactA',name:'result.png',bytes:23},{artifact_id:'artifactB',name:'notes.txt',bytes:12},{artifact_id:'oversize',name:'large.bin',bytes:60000000}];
                data.telegram.upload_limit=52428800;
            }""")
            page.evaluate(
                "channels.act('dot-channel-open',document.querySelector('#open'))"
            )
            page.locator(".cc-connection-maintenance > summary").click()
            page.get_by_text("向 Telegram 发送已有图片或文件", exact=True).click()
            files = page.locator("[data-channel-form=tg-files]")
            files.locator("[name=tg_binding]").select_option("tgBinding")
            files.locator("[name=tg_message]").select_option("replyA")
            assert files.locator("[value=oversize]").is_disabled()
            files.locator("[value=artifactA]").check()
            page.evaluate(
                "channels.act('dot-channel-tg-file-preview',document.querySelector('[data-cc-action=dot-channel-tg-file-preview]'))"
            )
            assert page.evaluate("writes.length") == 3
            assert page.evaluate("writes[2].operation") == "telegram-files-preview"
            assert "101" in files.locator(".tg-file-preview").inner_text()
            page.evaluate(
                "channels.act('dot-channel-tg-files',document.querySelector('[data-cc-action=dot-channel-tg-files]'))"
            )
            assert page.evaluate("writes.length") == 3
            files.locator("[name=file_confirm]").check()
            files.locator("[value=artifactB]").check()
            assert page.evaluate(
                "channels.act('dot-channel-tg-files',document.querySelector('[data-cc-action=dot-channel-tg-files]')).then(()=>false,()=>true)"
            )
            assert page.evaluate("writes.length") == 3
            files.locator("[value=artifactB]").uncheck()
            page.screenshot(
                path=str(evidence / f"telegram-file-preview-{engine}-{width}.png"),
                full_page=True,
            )
            page.evaluate(
                "channels.act('dot-channel-tg-files',document.querySelector('[data-cc-action=dot-channel-tg-files]'))"
            )
            assert page.evaluate("writes.length") == 4
            assert page.evaluate("writes[3].args.artifact_ids") == ["artifactA"]
            assert page.evaluate("writes[3].args.confirm_file_egress") is True

            page.evaluate("""() => {
                delayReads=true;window.opening=channels.act('dot-channel-open',document.querySelector('#open'));
                state.generation++;state.conversation='conversationB';document.querySelector('#drawer').textContent='新协作室';
                delayed.forEach(resolve=>resolve());
            }""")
            page.evaluate("opening")
            assert page.locator("#drawer").inner_text() == "新协作室"
            assert page.evaluate("writes.length") == 4
            assert not errors
        finally:
            browser.close()
