'use strict';
// Render untrusted CLI text with DOM nodes only. No raw HTML, scripts or remote images.
function chatRichInline(parent, text) {
  const re=/(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let end=0;
  for(const match of text.matchAll(re)) {
    parent.append(document.createTextNode(text.slice(end,match.index)));
    const token=match[0];let node;
    if(token.startsWith('`')){node=document.createElement('code');node.textContent=token.slice(1,-1);}
    else if(token.startsWith('**')||token.startsWith('__')){node=document.createElement('strong');node.textContent=token.slice(2,-2);}
    else if(token.startsWith('*')){node=document.createElement('em');node.textContent=token.slice(1,-1);}
    else {const m=token.match(/^\[([^\]]+)\]\((.*)\)$/);let url;try{url=new URL(m[2]);}catch{}if(url&&['http:','https:'].includes(url.protocol)){node=document.createElement('a');node.href=url.href;node.target='_blank';node.rel='noopener noreferrer';node.textContent=m[1];}else{node=document.createTextNode(token);}}
    parent.append(node);end=match.index+token.length;
  }
  parent.append(document.createTextNode(text.slice(end)));
}
function chatRichBlocks(text) {
  const lines=text.split('\n'),out=[],offsets=[];let position=0;for(const line of lines){offsets.push(position);position+=line.length+1;}let i=0;
  while(i<lines.length){
    if(!lines[i].trim()){i++;continue;}
    const blockStart=i;
    const fence=lines[i].match(/^\s*(`{3,}|~{3,})(.*)$/);
    if(fence){const marker=fence[1],start=i++,body=[];while(i<lines.length&&!lines[i].trim().startsWith(marker))body.push(lines[i++]);const closed=i<lines.length;if(closed)i++;out.push({start:offsets[blockStart],type:'code',text:body.join('\n'),language:fence[2].trim().slice(0,40),raw:lines.slice(start,i).join('\n'),closed});continue;}
    const heading=lines[i].match(/^(#{1,6})\s+(.+)$/);
    if(heading){out.push({start:offsets[blockStart],type:'h'+Math.min(heading[1].length+1,6),text:heading[2],raw:lines[i++]});continue;}
    if(/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(lines[i])){out.push({start:offsets[blockStart],type:'hr',text:'',raw:lines[i++]});continue;}
    if(i+1<lines.length&&lines[i].includes('|')&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])){const start=i,rows=[lines[i]];i+=2;while(i<lines.length&&lines[i].includes('|')&&lines[i].trim())rows.push(lines[i++]);out.push({start:offsets[blockStart],type:'table',rows,raw:lines.slice(start,i).join('\n')});continue;}
    const list=lines[i].match(/^\s*(?:([-+*])|\d+[.)])\s+(.+)$/);
    if(list){const ordered=!list[1],start=i,rows=[];while(i<lines.length){const m=lines[i].match(/^\s*(?:([-+*])|\d+[.)])\s+(.+)$/);if(!m||(!m[1])!==ordered)break;rows.push(m[2]);i++;}out.push({start:offsets[blockStart],type:ordered?'ol':'ul',rows,raw:lines.slice(start,i).join('\n')});continue;}
    if(/^>\s?/.test(lines[i])){const start=i,rows=[];while(i<lines.length&&/^>\s?/.test(lines[i]))rows.push(lines[i++].replace(/^>\s?/,''));out.push({start:offsets[blockStart],type:'blockquote',text:rows.join('\n'),raw:lines.slice(start,i).join('\n')});continue;}
    const start=i++;while(i<lines.length&&lines[i].trim()&&!/^\s*(#{1,6}\s|`{3,}|~{3,}|>\s?|[-+*]\s|\d+[.)]\s)/.test(lines[i])&&!(i+1<lines.length&&lines[i].includes('|')&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])))i++;
    out.push({start:offsets[blockStart],type:'p',text:lines.slice(start,i).join('\n'),raw:lines.slice(start,i).join('\n')});
  }
  return out;
}
// Reconcile generated safe nodes rather than replacing a rich paragraph on every delta.
function chatRichReconcile(node,next) {
  if(node.nodeType===3){if(next.data.startsWith(node.data))node.appendData(next.data.slice(node.data.length));else node.data=next.data;return;}
  for(const attr of [...node.attributes])if(!next.hasAttribute(attr.name))node.removeAttribute(attr.name);
  for(const attr of [...next.attributes])if(node.getAttribute(attr.name)!==attr.value)node.setAttribute(attr.name,attr.value);
  const desired=[...next.childNodes];desired.forEach((child,index)=>{
    const current=node.childNodes[index];
    if(!current)node.append(child);
    else if(current.nodeType===child.nodeType&&current.nodeName===child.nodeName)chatRichReconcile(current,child);
    else current.replaceWith(child);
  });
  while(node.childNodes.length>desired.length)node.lastChild.remove();
}
function chatRichMarkdown(holder,text) {
  let blocks;const previous=holder._chatBlocks,source=holder._chatSource;
  if(previous?.length&&source&&text.startsWith(source)){
    // A partial next list marker/table separator can merge the preceding block.
    const keep=Math.max(0,previous.length-2),tail=previous[keep].start??0;
    blocks=[...previous.slice(0,keep),...chatRichBlocks(text.slice(tail)).map(block=>({...block,start:block.start+tail}))];
  }else blocks=chatRichBlocks(text);
  holder._chatSource=text;holder._chatBlocks=blocks;
  blocks.forEach((block,index)=>{
    let node=holder.children[index];
    if(node&&node._chatRaw===block.raw)return;
    if(node&&node._chatType===block.type&&block.type==='code'){
      node.querySelector('.chat-code-head span').textContent=block.language||'代码';const code=node.querySelector('code');if(code.firstChild?.nodeType===3&&block.text.startsWith(code.textContent))code.firstChild.appendData(block.text.slice(code.textContent.length));else code.textContent=block.text;node.querySelector('button')._copyText=block.text;node._chatRaw=block.raw;return;
    }
    if(node&&node._chatType===block.type&&['p','blockquote'].includes(block.type)&&node.childNodes.length===1&&node.firstChild.nodeType===3&&block.text.startsWith(node.textContent)&&!/[`*_[\]]/.test(block.text)){node.firstChild.appendData(block.text.slice(node.textContent.length));node._chatRaw=block.raw;return;}
    const next=document.createElement(block.type==='code'?'section':block.type==='table'?'div':block.type);next._chatType=block.type;next._chatRaw=block.raw;
    if(block.type==='code'){
      next.className='chat-code';const header=document.createElement('div'),label=document.createElement('span'),copyButton=document.createElement('button'),pre=document.createElement('pre'),code=document.createElement('code');header.className='chat-code-head';label.textContent=block.language||'代码';copyButton.type='button';copyButton.textContent='复制';copyButton._copyText=block.text;copyButton.addEventListener('click',()=>chatCopyText(copyButton._copyText));header.append(label,copyButton);code.textContent=block.text;pre.append(code);next.append(header,pre);
    }else if(block.type==='table'){
      next.className='chat-table-scroll';const table=document.createElement('table');block.rows.forEach((row,n)=>{const tr=document.createElement('tr');row.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').forEach(cell=>{const td=document.createElement(n===0?'th':'td');chatRichInline(td,cell.trim());tr.append(td);});table.append(tr);});next.append(table);
    }else if(block.rows){block.rows.forEach(row=>{const li=document.createElement('li');chatRichInline(li,row);next.append(li);});}
    else chatRichInline(next,block.text||'');
    if(node&&node._chatType===block.type){chatRichReconcile(node,next);node._chatRaw=block.raw;}else if(node)node.replaceWith(next);else holder.append(next);
  });
  while(holder.children.length>blocks.length)holder.lastElementChild.remove();
}
