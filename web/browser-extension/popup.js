import {siteOf} from './workspace.js';
const $=id=>document.getElementById(id);let last;
async function call(action,body={}){const r=await chrome.runtime.sendMessage({action,...body});if(!r?.ok)throw new Error(r?.message||'扩展没有响应');return r.data;}
function render(s){last=s;$('extension').textContent=s.extension_id;$('profile').textContent=s.profile_id;$('pool').textContent=`${s.pool_size} 个标签页 · ${s.available} 个空闲 · ${s.leased} 个租约`;$('connection').textContent=s.connection||'设置已保存';$('origins').replaceChildren();for(const site of s.origins){const li=document.createElement('li');li.append(document.createTextNode(site+' '));const button=document.createElement('button');button.textContent='撤销';button.onclick=()=>run(button,()=>call('revoke',{origin:site}));li.append(button);$('origins').append(li);}}
async function run(button,fn){if(button.disabled)return;button.disabled=true;$('error').textContent='';try{render(await fn());}catch(e){$('error').textContent=e.message;}finally{button.disabled=false;}}
$('prepare').onclick=e=>run(e.currentTarget,()=>call('prepare',{size:Number($('size').value)}));
$('release').onclick=e=>run(e.currentTarget,()=>call('release-all'));
$('allow-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');await run(button,async()=>{const raw=$('origin').value,origin=siteOf(raw);const u=new URL(raw);if(u.pathname!=='/'||u.search||u.hash)throw new Error('仅填写站点 origin，不要包含页面路径');if(!await chrome.permissions.request({origins:[origin+'/*']}))throw new Error('尚未授权站点权限');return call('allow',{origin});});};
$('copy').onclick=async()=>{try{await navigator.clipboard.writeText(JSON.stringify({extension_id:last.extension_id,profile_id:last.profile_id},null,2));$('connection').textContent='绑定信息已复制，不含账号密码';}catch{$('error').textContent='复制失败，请手动复制上方两个编号';}};
call('status').then(render,e=>$('error').textContent=e.message);
