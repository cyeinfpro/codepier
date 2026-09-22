/** This closed function runs only in Chrome's isolated extension world.
 * It accepts a small action vocabulary, never selectors/scripts from the model.
 * DOM events are not trusted OS input; outcomes describe dispatch, not business success. */
export function pageCommand(request,origins){
  const reject=(code,message)=>{throw Object.assign(new Error(message),{code});};
  const nonce=()=>crypto.randomUUID().replaceAll('-','');
  const allowed=url=>{const u=new URL(url,location.href);if(!['http:','https:'].includes(u.protocol)||u.username||u.password||!Array.isArray(origins)||!origins.includes(u.origin))reject('BROWSER_ORIGIN_DENIED','页面已离开授权站点');return u;};
  const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return el.isConnected&&r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&!el.closest('[inert],[aria-hidden="true"]');};
  const safe=el=>!el.matches('input[type="password"],input[type="hidden"],input[type="file"],[autocomplete*="password"],[autocomplete="one-time-code"],[autocomplete^="cc-"]');
  const label=el=>(el.getAttribute('aria-label')||el.labels?.[0]?.innerText||el.getAttribute('placeholder')||el.innerText||el.getAttribute('title')||'').trim().slice(0,500);
  const fingerprint=el=>JSON.stringify([el.tagName,el.getAttribute('type'),label(el),el.getAttribute('href'),el.getAttribute('formaction'),el.form?.action,el.disabled,el.getAttribute('name'),el instanceof HTMLSelectElement?[...el.options].map(o=>[o.label,o.value,o.disabled,!!o.closest('optgroup[disabled]')]):null]);
  try{
    allowed(location.href);
    if(!document.body)reject('BROWSER_DOCUMENT_UNAVAILABLE','页面尚未准备好');
    let state=globalThis.__codepierBrowserV1;
    if(!state||state.document!==document){state={document,document_id:nonce(),token:null,elements:new Map(),url:location.href};Object.defineProperty(globalThis,'__codepierBrowserV1',{value:state,writable:true,configurable:true});}
    if(request.action==='snapshot'){
      state.token=nonce();state.url=location.href;state.elements=new Map();const elements=[];
      const all=[...document.querySelectorAll('button,a[href],input,textarea,select,[role="button"],[role="link"],[contenteditable="true"],[tabindex]')].slice(0,5000);
      const inViewport=el=>{const r=el.getBoundingClientRect();return r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;};
      // Stable DOM order within each group, with actionable viewport targets first.
      all.sort((a,b)=>Number(inViewport(b))-Number(inViewport(a)));
      let considered=0,truncated=all.length===5000,optionBudget=32768;
      for(const el of all){
        if(++considered>5000){truncated=true;break;}
        if(!safe(el)||!visible(el))continue;
        if(elements.length>=200){truncated=true;break;}
        const id='e'+elements.length;state.elements.set(id,{el,fingerprint:fingerprint(el)});
        const type=el.getAttribute('type')||'',value=('value' in el?String(el.value):el.isContentEditable?el.innerText:'').slice(0,1000);
        const item={id,tag:el.tagName.toLowerCase(),role:el.getAttribute('role')||'',label:label(el),type,value,disabled:!!el.disabled};
        if(el instanceof HTMLSelectElement){
          item.options=[];item.options_truncated=el.options.length>200;
          for(let index=0;index<Math.min(200,el.options.length);index++){
            const option=el.options[index];
            // Opaque values must be exact. Never advertise a truncated value
            // that could accidentally select a different, shorter option.
            if(option.value.length>1000){item.options_truncated=true;continue;}
            const entry={label:option.label.slice(0,500),value:option.value,disabled:!!option.disabled||!!option.closest('optgroup[disabled]'),selected:option.selected};
            const size=JSON.stringify(entry).length;
            if(size>optionBudget){item.options_truncated=true;break;}
            optionBudget-=size;item.options.push(entry);
          }
          truncated=truncated||item.options_truncated;
        }
        elements.push(item);
      }
      const text=document.body.innerText||'';
      return {ok:true,data:{document_id:state.document_id,observation_token:state.token,url:location.href,title:document.title.slice(0,300),text:text.slice(0,24000),elements,content_truncated:truncated||text.length>24000}};
    }
    if(request.action!=='action')reject('BROWSER_ACTION_UNSUPPORTED','不支持的页面请求');
    if(state.document_id!==request.document_id||!state.token||state.token!==request.observation_token||state.url!==location.href)reject('BROWSER_STALE_OBSERVATION','页面或观察已改变，请重新读取');
    const operation=request.operation||{};
    // Consume BEFORE input. Failed/uncertain input never reuses the observation.
    state.token=null;
    if(operation.action==='navigate'){allowed(operation.value);return {ok:true,data:{navigation_validated:true}};}
    if(operation.action==='scroll'){
      if(!Number.isInteger(operation.delta_y)||Math.abs(operation.delta_y)>4000)reject('BROWSER_ACTION_UNSUPPORTED','滚动距离超限');
      window.scrollBy({top:operation.delta_y,behavior:'instant'});return {ok:true,data:{input_dispatched:true}};
    }
    const item=state.elements.get(operation.element_id),el=item?.el;
    if(!el||!visible(el)||!safe(el)||fingerprint(el)!==item.fingerprint||el.disabled)reject('BROWSER_ELEMENT_CHANGED','控件已经变化或不可用，请重新读取');
    if(operation.action==='click'){
      const anchor=el.closest('a[href]');
      if(anchor){allowed(anchor.href);if(anchor.target&&anchor.target!=='_self'||anchor.hasAttribute('download'))reject('BROWSER_ACTION_UNSUPPORTED','不自动打开新窗口或下载，请人工操作');}
      if(el.form){allowed(el.form.action||location.href);if(el.form.target&&el.form.target!=='_self')reject('BROWSER_ACTION_UNSUPPORTED','不自动提交到新窗口');}
      if(el.getAttribute('formaction'))allowed(el.getAttribute('formaction'));
      el.click();
    }else if(operation.action==='fill'){
      if(typeof operation.value!=='string'||operation.value.length>16000)reject('BROWSER_ACTION_UNSUPPORTED','输入内容超限');
      if(el instanceof HTMLInputElement||el instanceof HTMLTextAreaElement){
        if(el instanceof HTMLInputElement&&!['text','email','url','search','tel','number','date','time','datetime-local','month','week'].includes(el.type))reject('BROWSER_ACTION_UNSUPPORTED','该控件不支持文字输入');
        if(el.readOnly)reject('BROWSER_ELEMENT_CHANGED','控件为只读');
        const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto,'value').set.call(el,operation.value);
      }else if(el.isContentEditable)el.textContent=operation.value;
      else reject('BROWSER_ACTION_UNSUPPORTED','该控件不是可编辑文本');
      el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));
    }else if(operation.action==='select'){
      if(!(el instanceof HTMLSelectElement)||![...el.options].some(o=>o.value===operation.value&&!o.disabled&&!o.closest('optgroup[disabled]')))reject('BROWSER_ACTION_UNSUPPORTED','选项不存在或不可用');
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(el,operation.value);
      el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));
    }else if(operation.action==='key'){
      if(!['Enter','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Tab'].includes(operation.value))reject('BROWSER_ACTION_UNSUPPORTED','只允许单个导航按键');
      // This is explicitly a DOM key event. It cannot press browser/OS shortcuts.
      el.dispatchEvent(new KeyboardEvent('keydown',{key:operation.value,bubbles:true,cancelable:true}));
      el.dispatchEvent(new KeyboardEvent('keyup',{key:operation.value,bubbles:true,cancelable:true}));
    }else reject('BROWSER_ACTION_UNSUPPORTED','不支持该页面动作');
    return {ok:true,data:{input_dispatched:true,requires_fresh_snapshot:true,trusted_os_input:false}};
  }catch(error){return {ok:false,code:error.code||'BROWSER_DOCUMENT_UNAVAILABLE',message:error.code?error.message:'页面已变化，未取得确定结果；请重新读取'};}
}
