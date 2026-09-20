"""Protocol fixture only; never starts a native app or sends real input."""
import json
import sys
from tests.fake_computer_provider import schema, frame
from shared.computer_contracts import NATIVE_TOOLS

def main():
    for line in sys.stdin:
        request=json.loads(line)
        if 'id' not in request:continue
        method=request.get('method');params=request.get('params',{})
        if method=='initialize':result={'userAgent':'fixture'}
        elif method=='thread/start':result={'thread':{'id':'fixture-thread'}}
        elif method=='mcpServerStatus/list':result={'data':[{'name':'codepier_computer','tools':{n:{'name':n,'inputSchema':schema(n)} for n in NATIVE_TOOLS}}],'nextCursor':None}
        elif method=='mcpServer/tool/call':
            print(json.dumps({'id':'consent','method':'mcpServer/elicitation/request','params':{'threadId':'fixture-thread','serverName':'codepier_computer','mode':'form','message':'Allow Fixture? <img src=x onerror=alert(1)>','requestedSchema':{'type':'object','properties':{}}}}),flush=True)
            decision=json.loads(sys.stdin.readline())
            result=frame(0) if decision['result']['action']=='accept' else {'isError':True,'content':[{'type':'text','text':'Permission declined'}]}
        else:result={}
        print(json.dumps({'id':request['id'],'result':result}),flush=True)

if __name__=='__main__':main()
