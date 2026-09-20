import {AppBridge,PostMessageTransport} from '../../web/mcp-apps/node_modules/@modelcontextprotocol/ext-apps/dist/src/app-bridge.js';
window.codepierHostErrors=[];
window.codepierOpenedLinks=[];
window.codepierMount=async(html)=>{
 window.codepierHostReady=false;
 if(window.codepierBridge)await window.codepierBridge.close();
 const previous=document.getElementById('app-frame');if(previous)previous.remove();
 const frame=document.createElement('iframe');frame.id='app-frame';frame.title='CodePier app acceptance';
 frame.setAttribute('sandbox','allow-scripts');frame.style.cssText='width:100%;height:850px;border:0';document.body.append(frame);
 const bridge=new AppBridge(null,{name:'CodePier isolated SDK acceptance host',version:'1.0.0'},
   {serverTools:{},serverResources:{},openLinks:{}},{hostContext:{theme:window.codepierHostTheme||'light',displayMode:'inline'}});
 bridge.oninitialized=()=>{window.codepierHostReady=true;};
 bridge.onerror=error=>window.codepierHostErrors.push(String(error));
 bridge.oncalltool=params=>window.codepierHostTool(params);
 bridge.onopenlink=async(params)=>{window.codepierOpenedLinks.push(params.url);return {};};
 await bridge.connect(new PostMessageTransport(frame.contentWindow,frame.contentWindow));
 window.codepierBridge=bridge;frame.srcdoc=html;
};
window.codepierDeliver=async({args,result})=>{
 await window.codepierBridge.sendToolInput({arguments:args});
 await window.codepierBridge.sendToolResult(result);
};
