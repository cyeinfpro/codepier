"""Capture the real chat DOM and production styles using synthetic native data.

This never contacts a Hub, reads real conversations, or starts a model. Full
HTTP/Hub/Agent tests live in test_chat_fullstack.py. Run as a module from root.
"""
from pathlib import Path
from types import SimpleNamespace
import argparse
import base64
import hashlib
import json
from tests.test_chat_browser import chat_page, event, ROOT
from tests.test_chat_complete_browser import send
from tests.conftest import chat_browser_pool


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'docs/evidence/cli-global-flow-20260918/screenshots')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    pool=chat_browser_pool.__wrapped__();generator=chat_page.__wrapped__(SimpleNamespace(param='chromium'),next(pool));p=next(generator)
    records=[]
    try:
        # Preserve production stylesheet order; the component fixture has chat.css last.
        for name in reversed(['tokens.css','styles.css','workspace.css']):
            p.evaluate("css=>{const style=document.createElement('style');style.textContent=css;document.head.prepend(style);}",(ROOT/'web'/name).read_text())
        p.emulate_media(reduced_motion='reduce')
        p.evaluate("S.projects[0].alias='MCP';S.projects[1].alias='Nexus';S.devices=[{id:'node-1',name:'开发节点（示例）',online:true}];for(const s of ['#chat-project','#chat-history-project']){document.querySelector(s+' option[value=p1]').textContent='MCP';document.querySelector(s+' option[value=p2]').textContent='Nexus';}")
        req=send(p,'继续优化 CLI 对话体验：直接浏览所有项目的会话，切换时保留草稿。')
        receipt=req['args']['receipt']
        event(p,'chat',{'type':'user','receipt':receipt,'text':req['args']['text']},10)
        event(p,'chat',{'type':'tool','receipt':receipt,'tool_id':'fixture-read','name':'读取项目文件','status':'end','text':'web/chat.js\nweb/chat-history.js'},20)
        event(p,'chat',{'type':'message','receipt':receipt,'text':'## 对话与项目，各归其位\n\n侧栏默认展示**所有项目的会话**。每条历史都带有项目标签，不用先切换项目再找对话。\n\n选择项目只是准备新草稿；发送首条消息时才会启动会话。\n\n```javascript\n// 始终使用所选会话自己的项目\nawait chatSwitch(conversation);\n```\n\n模型目录可以独立重试，不会丢失正在输入的内容。'},30)
        event(p,'chat',{'type':'done','receipt':receipt,'status':'completed'},40)
        p.evaluate("ChatUI.selected.title='CLI 会话 · 全流程体验优化';$('#chat-title').textContent=ChatUI.selected.title;window.rows=[{...ChatUI.selected,online:true,updated:Date.now()/1000},{id:'example-2',project_id:'p2',mode:'chat',provider:'pi',status:'running',online:true,title:'项目结构与实现计划',updated:Date.now()/1000-300},{id:'example-3',project_id:'p1',mode:'chat',provider:'codex',status:'exited',online:true,title:'上传队列的稳定性检查',updated:Date.now()/1000-90000}];chatList();chatStatus('示例数据 · 真实页面渲染');window.fixtureRow=ChatUI.selected;")
        def capture(name,width,height):
            p.wait_for_timeout(80)
            assert p.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            raw=p.screenshot(path=str(args.output/name),animations='disabled')
            records.append({'file':name,'width':width,'height':height,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
            # A reduced actual screenshot for remote visual inspection, not a mockup.
            encoded=p.evaluate("""async data=>{const image=new Image();image.src='data:image/png;base64,'+data;await image.decode();const canvas=document.createElement('canvas');canvas.width=Math.min(900,image.width);canvas.height=Math.round(image.height*canvas.width/image.width);canvas.getContext('2d').drawImage(image,0,0,canvas.width,canvas.height);return canvas.toDataURL('image/webp',.42).split(',')[1];}""",base64.b64encode(raw).decode())
            (args.output/Path(name).with_suffix('.webp')).write_bytes(base64.b64decode(encoded))
        for scheme in ['light','dark']:
            p.evaluate('scheme=>{document.documentElement.dataset.appearance=scheme;chatApplyAppearance(scheme);}',scheme)
            for width,height in [(1440,1000),(390,844),(320,568),(667,375)]:
                p.set_viewport_size({'width':width,'height':height});p.evaluate('chatSwitch(fixtureRow)')
                capture(f'conversation-{scheme}-{width}.png',width,height)
                if width<=760:p.click('#chat-history-toggle')
                capture(f'history-{scheme}-{width}.png',width,height)
                p.click('#chat-new');capture(f'project-{scheme}-{width}.png',width,height)
                p.press('#chat-project-search','Escape')
                if width<=760:p.keyboard.press('Escape')
        p.set_viewport_size({'width':1440,'height':1000});p.evaluate("chatSwitch(null,'p2',{cwd:'.'})")
        capture('draft-dark-1440.png',1440,1000)
        (args.output/'manifest.json').write_text(json.dumps({'synthetic':True,'production_styles':True,'browser':'Chromium','screenshots':records},ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'screenshots':len(records),'output':str(args.output),'synthetic':True,'production_styles':True},ensure_ascii=False))
    finally:
        try: next(generator)
        except StopIteration: pass
        try: next(pool)
        except StopIteration: pass


if __name__=='__main__':
    main()
