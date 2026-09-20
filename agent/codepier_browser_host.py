#!/usr/bin/env python3
"""Chrome native messaging host: fixed profile-bound RPC only, no shell/eval."""
from __future__ import annotations
import argparse,concurrent.futures,http.client,json,os,re,stat,struct,sys,threading,time
from pathlib import Path

MAX=1024*1024

def descriptor(path):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>4096 or info.st_nlink!=1 or os.name!='nt' and info.st_mode&0o077:raise ValueError('Unsafe local descriptor')
        result=json.loads(os.read(fd,4097))
    finally:os.close(fd)
    if type(result.get('port')) is not int or not 1<=result['port']<=65535 or not isinstance(result.get('token'),str):raise ValueError('Invalid descriptor')
    return result


def post(path,route,body):
    desc=descriptor(path);con=http.client.HTTPConnection('127.0.0.1',desc['port'],timeout=13)
    try:
        raw=json.dumps(body).encode()
        con.request('POST',route,raw,{'Content-Type':'application/json','Authorization':'Bearer '+desc['token'],'Connection':'close'})
        response=con.getresponse();data=response.read(MAX+1)
        if len(data)>MAX:raise ValueError('Local reply too large')
        result=json.loads(data)
        if response.status!=200:raise ValueError(result.get('error',{}).get('code','LOCAL_ERROR'))
        return result
    finally:con.close()


def read_exact(stream,count):
    data=bytearray()
    while len(data)<count:
        part=stream.read(count-len(data))
        if not part:raise EOFError()
        data.extend(part)
    return bytes(data)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--descriptor');parser.add_argument('--extension-id')
    parser.add_argument('--parent-window',type=int,default=0)
    parser.add_argument('origin');args=parser.parse_args()
    if args.parent_window<0:return 2
    if not args.descriptor or not args.extension_id:
        if not getattr(sys,'frozen',False):return 2
        config=Path(sys.executable).parent/'codepier-browser-host.json'
        if config.is_symlink() or not config.is_file() or config.stat().st_size>4096:return 2
        values=json.loads(config.read_text())
        args.descriptor=values.get('descriptor');args.extension_id=values.get('extension_id')
        if not isinstance(args.descriptor,str) or not isinstance(args.extension_id,str):return 2
    if not re.fullmatch('[a-p]{32}',args.extension_id) or args.origin!='chrome-extension://'+args.extension_id+'/':return 2
    stream_in,stream_out=sys.stdin.buffer,sys.stdout.buffer
    if os.name=='nt':
        import msvcrt
        msvcrt.setmode(stream_in.fileno(),os.O_BINARY);msvcrt.setmode(stream_out.fileno(),os.O_BINARY)
    lock=threading.Lock();stop=threading.Event();identity={};state={}
    def send(message):
        raw=json.dumps(message,ensure_ascii=False).encode()
        if len(raw)>MAX:raise ValueError('Native frame too large')
        with lock:stream_out.write(struct.pack('<I',len(raw))+raw);stream_out.flush()
    def reader():
        try:
            while not stop.is_set():
                size=struct.unpack('<I',read_exact(stream_in,4))[0]
                if not 0<size<=MAX:raise ValueError('Native size limit')
                msg=json.loads(read_exact(stream_in,size))
                if not isinstance(msg,dict):raise ValueError('Invalid message')
                if msg.get('type')=='hello':
                    profile=msg.get('profile_id')
                    if not isinstance(profile,str) or not re.fullmatch('[a-f0-9]{32}',profile):raise ValueError('Invalid profile')
                    if identity and identity['profile_id']!=profile:raise ValueError('Profile changed')
                    identity.update(extension_id=args.extension_id,profile_id=profile)
                    state.update(msg.get('state',{}) if isinstance(msg.get('state'),dict) else {})
                elif msg.get('type')=='result' and identity:
                    post(args.descriptor,'/native/reply',{**identity,'state':state,'request_id':msg.get('request_id'),'result':msg.get('result')})
                elif msg.get('type')=='state':
                    state.clear();state.update(msg.get('state',{}) if isinstance(msg.get('state'),dict) else {})
                else:raise ValueError('Unsupported message')
        except (EOFError,Exception):stop.set()
    thread=threading.Thread(target=reader,daemon=True);thread.start()
    while not stop.is_set():
        if not identity:stop.wait(.1);continue
        try:
            response=post(args.descriptor,'/native/poll',{**identity,'state':state})
            if response.get('request'):send({'type':'request',**response['request']})
        except Exception:
            with __import__('contextlib').suppress(Exception):send({'type':'status','state':'disconnected','message':'Agent 未连接或档案未授权'})
            stop.wait(2)
    return 0

if __name__=='__main__':raise SystemExit(main())
