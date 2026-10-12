"""Synthetic UI capability/readiness/file review; no real channel traffic."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.weixin_browser_support import qr_page as qr_page, full_styles
from tests.test_dot_weixin_qr_browser import action


@pytest.mark.parametrize("width", [390, 1440])
def test_capabilities_pending_recovery_and_manifest_have_truthful_evidence(qr_page, tmp_path, width):
    page, engine = qr_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.evaluate("""() => {
        data.weixin.bindings = [{id:'synthetic-binding',state:'pairing',version:1,generation:1,
          account_id:'accountA',dot_id:'dotA',pairing_until:Date.now()/1000+300,
          pairing_challenge:'SYNTHETIC-PRIVATE-CODE',expires_at:Date.now()/1000+300,
          onboarding:{state:'pending',auto_complete:true,notice_state:'waiting_inbound',
            readiness:{same_pending_object:true,ready_for_private_proof:true,scope_unchanged:true}},
          effective_capabilities:{text:{limit:3000,unit:'utf8_bytes'},media:{upload_limit:33554432},
            features:{text:{implemented:true,provider_account_verified:null,authorized:true,
              session_available:false,effective:false,enabled:false,unknown_layers:['provider_account_verified']}}}}];
        data.weixin.accounts = [{id:'accountA',label:'合成账号',state:'active',bot_id:'synthetic-bot'}];
    }""")
    action(page, "open")
    expect(page.locator(".cc-onboarding-status")).to_contain_text("已恢复原接入对象")
    expect(page.locator(".cc-onboarding-status")).to_contain_text("等待本人完成私聊验证")
    page.locator(".cc-channel-capabilities > summary").click()
    expect(page.locator(".cc-channel-capabilities")).to_contain_text("账号实测：尚未核实")
    expect(page.locator(".cc-channel-capabilities")).to_contain_text("上传成功不等于消息已发送")
    assert page.evaluate("writes.length") == 0
    page.evaluate("""() => {
      const target=document.createElement('div');target.id='manifest';document.querySelector('#drawer').append(target);
      target.innerHTML=CodePierCollaborationChannels.attachments({attachments:[{
        id:'a'.repeat(32),name:'合成原音频.wav',kind:'voice',mime:'audio/wav',size:16,
        sha256:'b'.repeat(64),expires_at:Date.now()/1000+600,state:'ready',
        origin:{artifact_id:'synthetic-artifact'},shares:[{channel:'weixin',upload_state:'accepted',send_state:'unknown'}]
      }]}, E);
    }""")
    page.locator(".cc-file-manifest > summary").click()
    expect(page.locator(".cc-file-manifest")).to_contain_text("上传已接受")
    expect(page.locator(".cc-file-manifest")).to_contain_text("发送结果未知")
    expect(page.locator(".cc-file-manifest")).to_contain_text("尚无可验证转写")
    full_styles(page)
    page.screenshot(path=str(tmp_path / f"capabilities-{engine}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".cc-file-manifest").evaluate("(node) => node.scrollWidth <= node.clientWidth")
    action(page, "open")
    assert page.evaluate("writes.length") == 0
    assert page.locator('[aria-label="微信配对码"]').input_value() == "SYNTHETIC-PRIVATE-CODE"


def install_review(page):
    page.evaluate("""() => {
      channels.clear();
      data.weixin.enabled=true;
      data.weixin.reply_candidates=[{id:'replyA',version:1,body_text:'合成结果'}];
      data.weixin.artifact_candidates=[
        {artifact_id:'artifactA',name:'first.txt',bytes:4},
        {artifact_id:'artifactB',name:'second.txt',bytes:8}];
      window.reviewPaused=false;
      window.reviewPreview={preview_digest:'a'.repeat(64),binding_version:2,
        recipient:{peer_id:'original-synthetic-peer'},project_id:'projectA',conversation_id:'conversationA',
        files:[{name:'first.txt',bytes:4,sha256:'b'.repeat(64),expires_at:Date.now()/1000+600}]};
      window.channels=CodePierCollaborationChannels.create({
        state,E,field:(label,input)=>'<label class="cc-field"><span>'+E(label)+'</span>'+input+'</label>',
        button:(action,label,item={},attrs='')=>'<button type="button" class="btn" data-cc-action="'+E(action)+
          '" data-id="'+E(item.id||'')+'" '+attrs+'>'+E(label)+'</button>',
        mentionSlots:()=>[],
        showDrawer:(title,body)=>{state.drawerEpoch++;document.querySelector('#drawer').innerHTML=body},
        mutation:async(operation,args)=>{
          writes.push({operation,args:structuredClone(args)});
          if(operation==='weixin-files-preview' || operation==='telegram-files-preview'){
            if(reviewPaused)await new Promise(resolve=>window.finishReview=resolve);
            return structuredClone(reviewPreview);
          }
          return {queued:true};
        }
      });
    }""")
    page.evaluate("channels.act('dot-channel-open', document.querySelector('#open'))")
    action(page, "view-weixin")
    form = page.locator('[data-channel-form="files"]')
    page.locator(".cc-connection-maintenance > summary").click()
    form.locator("..").locator("summary").click()
    form.locator('[name="message_id"]').select_option("replyA")
    form.locator('[value="artifactA"]').check()
    return form


def test_weixin_review_binds_file_list_recipient_digest_and_double_submit(qr_page):
    page, _ = qr_page
    form = install_review(page)
    assert page.locator('[data-cc-action="dot-channel-files"]').count() == 0
    action(page, "file-preview")
    expect(form.locator(".wx-file-preview")).to_contain_text("original-synthetic-peer")
    expect(form.locator(".wx-file-preview")).to_contain_text("保留至")
    assert [r["operation"] for r in page.evaluate("writes")] == ["weixin-files-preview"]
    action(page, "files")
    assert len(page.evaluate("writes")) == 1
    form.locator('[name="confirm_files"]').check()
    form.locator('[value="artifactB"]').check()
    assert page.evaluate("clickAction('dot-channel-files').then(()=>false,()=>true)")
    assert len(page.evaluate("writes")) == 1
    form.locator('[value="artifactB"]').uncheck()
    page.evaluate("Promise.all([clickAction('dot-channel-files'),clickAction('dot-channel-files')])")
    values=page.evaluate("writes")
    assert len(values) == 2
    assert values[-1]["operation"] == "weixin-files-reviewed"
    assert values[-1]["args"]["artifact_ids"] == ["artifactA"]
    assert values[-1]["args"]["preview_digest"] == "a" * 64
    assert values[-1]["args"]["confirm_file_egress"] is True


def test_late_file_preview_cannot_restore_old_room_or_consent(qr_page):
    page, _ = qr_page
    install_review(page)
    page.evaluate("() => {reviewPaused=true; window.waitingReview=clickAction('dot-channel-file-preview');}")
    page.evaluate("""() => {state.generation++;state.conversation='new-room';
      document.querySelector('#drawer').textContent='新房间';finishReview();}""")
    page.evaluate("waitingReview")
    expect(page.locator("#drawer")).to_have_text("新房间")
    assert [r["operation"] for r in page.evaluate("writes")] == ["weixin-files-preview"]


def test_telegram_file_preview_after_close_never_reopens_or_reuses_consent(qr_page):
    page, _ = qr_page
    install_review(page)
    page.evaluate("""() => {
      data.telegram.enabled=true;
      data.telegram.bindings=[{id:'tgBinding',state:'active',version:2,generation:1,user_id:'101',chat_id:'101'}];
      data.telegram.reply_candidates=[{id:'replyA',version:1,body_text:'synthetic'}];
      data.telegram.artifact_candidates=[{artifact_id:'artifactA',name:'first.txt',bytes:4}];
    }""")
    action(page, "open")
    action(page, "view-overview")
    action(page, "view-telegram")
    page.evaluate("""() => {
      const form=document.querySelector('[data-channel-form="tg-files"]');
      form.querySelector('[name="tg_binding"]').value='tgBinding';
      form.querySelector('[name="tg_message"]').value='replyA';
      form.querySelector('[name="tg_artifact_ids"]').checked=true;
      reviewPaused=true;
      window.waitingReview=clickAction('dot-channel-tg-file-preview');
    }""")
    page.evaluate("""() => {channels.clear();document.querySelector('#drawer').textContent='已关闭';finishReview();}""")
    page.evaluate("waitingReview")
    expect(page.locator("#drawer")).to_have_text("已关闭")
    assert [r["operation"] for r in page.evaluate("writes")] == ["telegram-files-preview"]
