"""Deterministic stdio MCP fixture; never opens or controls any desktop application."""
import base64,json,struct,sys,zlib,time
from pathlib import Path
from shared.computer_contracts import NATIVE_TOOLS


def png(width=100,height=60):
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    pixels=b''.join(b'\0'+b'\x28\x38\x48'*width for _ in range(height))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')


def schema(name):
    fields={'app':{'type':'string'}} if name!='list_apps' else {}
    definitions={
        'click':{'element_index':'string','x':'number','y':'number','mouse_button':'string','click_count':'integer'},
        'perform_secondary_action':{'element_index':'string','action':'string'},
        'set_value':{'element_index':'string','value':'string'},
        'select_text':{'element_index':'string','text':'string','prefix':'string','suffix':'string','selection':'string'},
        'scroll':{'element_index':'string','direction':'string','pages':'number'},
        'drag':{'from_x':'number','from_y':'number','to_x':'number','to_y':'number'},
        'press_key':{'key':'string'},'type_text':{'text':'string'}}
    fields.update({k:{'type':v} for k,v in definitions.get(name,{}).items()})
    return {'type':'object','properties':fields,'additionalProperties':False,'required':['app'] if name!='list_apps' else []}


def frame(value='0',error=False):
    return {'isError':error,'content':[{'type':'text','text':'Fixture state '+str(value)+' <img src=x onerror=window.desktopXss=1>'},
        {'type':'image','mimeType':'image/png','data':base64.b64encode(png()).decode()}]}


def main():
    root=Path.cwd();state=root/'state.txt';log=root/'calls.jsonl'
    if not state.exists():state.write_text('0')
    for line in sys.stdin:
        request=json.loads(line);id=request.get('id');method=request.get('method')
        if id is None:continue
        if method=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'fixture-only','version':'1'}}
        elif method=='tools/list':result={'tools':[{'name':n,'inputSchema':schema(n)} for n in sorted(NATIVE_TOOLS)]}
        elif method=='tools/call':
            name=request['params']['name'];args=request['params']['arguments']
            with log.open('a') as file:file.write(json.dumps({'name':name,'args':args})+'\n')
            if name=='list_apps':result={'content':[{'type':'text','text':'Fixture'}]}
            elif name=='get_app_state':result=frame(state.read_text(),(root/'read-error').exists())
            else:
                state.write_text(str(int(state.read_text())+1))
                if (root/'disconnect-action').exists():return
                if (root/'hang-action').exists():time.sleep(60)
                result={'isError':(root/'action-error').exists(),'content':[{'type':'text','text':'Fixture action receipt'}]}
        else:result={}
        print(json.dumps({'jsonrpc':'2.0','id':id,'result':result}),flush=True)

if __name__=='__main__':main()
