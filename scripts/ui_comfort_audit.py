"""Isolated UI geometry and rendered text contrast audit; no real model submission."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.support import running_stack
from tests.test_ui_unification import _login, _navigate, _prepare_native_fixture, _set_scheme
from playwright.sync_api import sync_playwright

MEASURE = r"""() => {
 const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility==='visible' && !e.closest('[hidden],[inert]');
 const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom};};
 const cv=document.createElement('canvas');cv.width=cv.height=1;const ctx=cv.getContext('2d',{willReadFrequently:true});
 const colors=new Map();
 function rgba(value){if(colors.has(value))return colors.get(value);ctx.clearRect(0,0,1,1);ctx.fillStyle=value;ctx.fillRect(0,0,1,1);const c=[...ctx.getImageData(0,0,1,1).data];c[3]/=255;colors.set(value,c);return c;}
 function over(c,b){return c.slice(0,3).map((v,i)=>v*c[3]+b[i]*(1-c[3]));}
 const lum=c=>c.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0);
 const ratio=(a,b)=>{const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);};
 const rows=[],skipped=[];
 const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
 while(walker.nextNode()){
   const n=walker.currentNode,e=n.parentElement,t=n.textContent.trim();
   if(!t||!e||!visible(e)||e.closest('script,style,option,noscript,svg,button:disabled,[aria-hidden="true"]'))continue;
   const range=document.createRange();range.selectNode(n);const r=range.getBoundingClientRect();
   if(!r.width||!r.height||r.right<0||r.left>innerWidth||r.bottom<0||r.top>innerHeight)continue;
   const chain=[];for(let p=e;p;p=p.parentElement)chain.unshift(p);
   if(chain.some(p=>{const s=getComputedStyle(p),b=p.getBoundingClientRect();return /(hidden|clip|auto|scroll)/.test(s.overflow+s.overflowY)&&(r.top>=b.bottom||r.bottom<=b.top);} ))continue;
   let bg=[255,255,255],alpha=1,complex=false;
   for(const p of chain){const s=getComputedStyle(p);bg=over(rgba(s.backgroundColor),bg);alpha*=Number(s.opacity);if(s.backgroundImage!=='none')complex=true;}
   const s=getComputedStyle(e),fg=[...rgba(s.color)];fg[3]*=alpha;
   const value=ratio(over(fg,bg),bg),large=parseFloat(s.fontSize)>=24||(parseFloat(s.fontSize)>=18.66&&Number(s.fontWeight)>=700);
   const row={text:t.slice(0,85),element:e.id||e.className||e.tagName,color:s.color,background:bg.map(Math.round),ratio:Math.round(value*100)/100,minimum:large?3:4.5,size:s.fontSize};
   if(complex){skipped.push(row);continue;}
   rows.push(row);
 }
 const selectors=['.page-head','.stats','.codepier-focus-card','.codepier-visual','.workspace-operations','.workspace-project-row','.workspace-device','.workspace','.terminal','#chat-root','#chat-empty','.chat-starters','#chat-compose','.sidebar','.side-bottom'];
 return {viewport:{width:innerWidth,height:innerHeight},documentHeight:document.documentElement.scrollHeight,documentWidth:document.documentElement.scrollWidth,
   boxes:Object.fromEntries(selectors.map(sel=>[sel,[...document.querySelectorAll(sel)].filter(visible).map(rect)])),
   textCount:rows.length,failures:rows.filter(r=>r.ratio+0.01<r.minimum),text:rows,complexBackgroundSkipped:skipped};
}"""


def capture(output: Path, *, full: bool=False, engine: str='chromium') -> dict:
    output.mkdir(parents=True, exist_ok=True)
    results=[]
    routes=['overview','devices','projects','workbench','settings','native']
    viewports=[(1366,768)] if not full else [(1280,720),(1366,768),(1440,900),(1920,1080),(1024,768),(390,844),(320,568)]
    with TemporaryDirectory(prefix='codepier-ui-comfort-') as tmp, running_stack(Path(tmp)) as stack:
        _prepare_native_fixture(stack)
        with sync_playwright() as pw:
            browser=getattr(pw,engine).launch()
            try:
                for width,height in viewports:
                    for scheme in ['light','dark']:
                        page=browser.new_page(viewport={'width':width,'height':height},color_scheme=scheme,reduced_motion='reduce')
                        errors=[]
                        page.on('pageerror',lambda e, errors=errors:errors.append(str(e)))
                        _login(page,stack);_set_scheme(page,scheme)
                        for route in routes:
                            _navigate(page,route)
                            data=page.evaluate(MEASURE)
                            data.update(route=route,scheme=scheme,engine=engine)
                            name=f'{engine}-{scheme}-{width}-{height}-{route}.png'
                            page.screenshot(path=str(output/name),animations='disabled')
                            data['screenshot']=name;results.append(data)
                            print(json.dumps({'route':route,'scheme':scheme,'width':width,'height':height,'documentHeight':data['documentHeight'],'contrastFailures':len(data['failures'])},ensure_ascii=False),flush=True)
                        assert not errors, errors
                        page.close()
            finally:
                browser.close()
    report={'engine':engine,'scope':'isolated fixtures; no real model messages; measured visible solid-background text, not full WCAG certification','cases':results}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--full',action='store_true');p.add_argument('--engine',choices=['chromium','webkit'],default='chromium');a=p.parse_args()
    capture(a.output,full=a.full,engine=a.engine)
