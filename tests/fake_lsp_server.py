"""Deterministic stdio language server for protocol tests; never a model CLI."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time


def send(value):
    data=json.dumps(value,ensure_ascii=False).encode('utf-8')
    sys.stdout.buffer.write(f'Content-Length: {len(data)}\r\n\r\n'.encode()+data)
    sys.stdout.buffer.flush()


def receive():
    headers={}
    while True:
        line=sys.stdin.buffer.readline()
        if not line:return None
        if line==b'\r\n':break
        key,value=line.decode('ascii').strip().split(':',1);headers[key.lower()]=value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers['content-length'])))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--marker');parser.add_argument('--mode',default='normal')
    args=parser.parse_args();root=None;documents={};requests=[];analyzed=False
    def marker(state):
        if args.marker:Path(args.marker).write_text(json.dumps({'pid':os.getpid(),'state':state,'requests':requests}))
    marker('started')
    zero={'start':{'line':0,'character':0},'end':{'line':0,'character':1}}
    while True:
        msg=receive()
        if msg is None:break
        method=msg.get('method');params=msg.get('params') or {}
        if method:requests.append(method)
        if method=='exit':break
        if method=='initialize':
            root=Path(__import__('urllib.parse',fromlist=['unquote']).unquote(__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(params['rootUri']).path))
            capabilities={'positionEncoding':'utf-16','definitionProvider':True,'referencesProvider':True,'hoverProvider':True,'documentSymbolProvider':True,'workspaceSymbolProvider':True,'callHierarchyProvider':True}
            if args.mode!='push':capabilities['diagnosticProvider']={'interFileDependencies':False,'workspaceDiagnostics':False}
            value={'capabilities':capabilities,'serverInfo':{'name':'CodePier deterministic LSP fixture','version':'1'}}
        elif method=='textDocument/didOpen':
            doc=params['textDocument'];documents[doc['uri']]=doc['text']
            if args.mode=='push':send({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics','params':{'uri':doc['uri'],'version':1,'diagnostics':[{'range':zero,'message':'Fixture diagnostic','severity':2}]}})
            continue
        elif method in {'initialized','workspace/didChangeConfiguration'}:continue
        elif method=='shutdown':value=None;marker('shutdown')
        elif 'id' not in msg:continue
        else:
            if args.mode=='timeout':time.sleep(120)
            if args.mode=='disconnect':return 2
            if args.mode=='oversized':sys.stdout.buffer.write(b'Content-Length: 99999999\r\n\r\n');sys.stdout.buffer.flush();time.sleep(120)
            uri=params.get('textDocument',{}).get('uri') or (root/'source.py').as_uri()
            location={'uri':uri,'range':zero}
            helper={'uri':(root/'helper.py').as_uri(),'range':zero}
            if args.mode=='cold' and not analyzed and method in {'workspace/symbol','textDocument/references','textDocument/prepareCallHierarchy'}:value=[]
            elif method=='textDocument/definition':value=[location,{'uri':'file:///outside-private.py','range':zero}]
            elif method=='textDocument/references':value=[location,location,helper]
            elif method=='textDocument/hover':value={'contents':{'kind':'plaintext','value':'fixture: str'},'range':zero}
            elif method=='textDocument/documentSymbol':value=[{'name':'fixture','kind':12,'range':zero,'selectionRange':zero,'children':[]}]
            elif method=='workspace/symbol':value=[{'name':params['query'],'kind':12,'location':location}]
            elif method=='textDocument/diagnostic':analyzed=True;value={'kind':'full','items':[{'range':zero,'message':'Fixture diagnostic','severity':2,'code':'F001'}]}
            elif method=='textDocument/prepareCallHierarchy':value=[{**location,'name':'fixture','kind':12,'selectionRange':zero}]
            elif method=='callHierarchy/incomingCalls':value=[{'from':{**helper,'name':'caller','kind':12,'selectionRange':zero},'fromRanges':[zero]}]
            elif method=='callHierarchy/outgoingCalls':value=[{'to':{**helper,'name':'callee','kind':12,'selectionRange':zero},'fromRanges':[zero]}]
            else:
                send({'jsonrpc':'2.0','id':msg['id'],'error':{'code':-32601,'message':'Unsupported fixture method'}});continue
        send({'jsonrpc':'2.0','id':msg['id'],'result':value})
        marker('running' if method!='shutdown' else 'shutdown')
    marker('exited');return 0

if __name__=='__main__':raise SystemExit(main())
