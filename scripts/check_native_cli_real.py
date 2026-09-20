#!/usr/bin/env python3
"""Real native TUI acceptance, macOS Seatbelt restricted to loopback networking.
No personal configs/history are loaded; only owned fixture process groups are stopped.
"""
import argparse, base64, fcntl, hashlib, http.server, json, os, pathlib, pty, re
import select, signal, struct, subprocess, tempfile, termios, threading, time, zlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/evidence/native-cli-20260914'
BINS = {name: shutil.which(name) or str(pathlib.Path.home()/'.local/bin'/name) for name in ('pi', 'codex')}
REQUESTS = []
TOOL_IMAGE = None

def images(value):
    found = []
    if isinstance(value, dict):
        if value.get('type') in ('image_url', 'input_image', 'image'):
            data = value.get('image_url', value.get('data', ''))
            if isinstance(data, dict): data = data.get('url', '')
            if isinstance(data, str) and data.startswith('data:image/'):
                raw = base64.b64decode(data.split(',', 1)[1])
                found.append({'type': value['type'], 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        for v in value.values(): found += images(v)
    elif isinstance(value, list):
        for v in value: found += images(v)
    return found

class Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps({'object':'list','data':[{'id':'gpt-5.4','object':'model'}]}).encode())
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        REQUESTS.append({'path':self.path, 'model':body.get('model'), 'images':images(body),
                         'reasoning':body.get('reasoning',body.get('reasoning_effort'))})
        self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
        def event(kind, obj):
            self.wfile.write(((f'event: {kind}\n' if kind else '')+'data: '+json.dumps(obj)+'\n\n').encode())
            self.wfile.flush()
        try:
            if self.path.endswith('/chat/completions'):
                tool_needed = TOOL_IMAGE is not None and not any(m.get('role') == 'tool' for m in body.get('messages', []))
                deltas = [({'role':'assistant','tool_calls':[{'index':0,'id':'call_fixture','type':'function','function':{'name':'read','arguments':json.dumps({'path':TOOL_IMAGE})}}]},None),({},'tool_calls')] if tool_needed else [({'role':'assistant','content':'ACTUAL_NATIVE_FIXTURE_OK'},None),({},'stop')]
                for delta, finish in deltas:
                    event('',{'id':'chat-fixture','object':'chat.completion.chunk','created':1,'model':'gpt-5.4','choices':[{'index':0,'delta':delta,'finish_reason':finish}]})
                self.wfile.write(b'data: [DONE]\n\n')
            else:
                msg={'id':'msg_fixture','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':'ACTUAL_NATIVE_FIXTURE_OK','annotations':[]}]}
                response={'id':'resp_fixture','object':'response','created_at':1,'status':'in_progress','model':'gpt-5.4','output':[]}
                event('response.created',{'type':'response.created','response':response})
                event('response.output_item.added',{'type':'response.output_item.added','output_index':0,'item':{**msg,'status':'in_progress','content':[]}})
                event('response.content_part.added',{'type':'response.content_part.added','item_id':'msg_fixture','output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}})
                event('response.output_text.delta',{'type':'response.output_text.delta','item_id':'msg_fixture','output_index':0,'content_index':0,'delta':'ACTUAL_NATIVE_FIXTURE_OK'})
                event('response.output_text.done',{'type':'response.output_text.done','item_id':'msg_fixture','output_index':0,'content_index':0,'text':'ACTUAL_NATIVE_FIXTURE_OK'})
                event('response.output_item.done',{'type':'response.output_item.done','output_index':0,'item':msg})
                event('response.completed',{'type':'response.completed','response':{**response,'status':'completed','output':[msg],'usage':{'input_tokens':20,'output_tokens':5,'total_tokens':25}}})
        except (BrokenPipeError, ConnectionResetError): pass

def png():
    def chunk(t,d): return struct.pack('!I',len(d))+t+d+struct.pack('!I',zlib.crc32(t+d)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',16,16,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\0\0'*16)*16))+chunk(b'IEND',b'')

def clean(s, base):
    s=re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', s)
    s=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', s)
    s=re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', s)
    return s.replace(str(base), '<FIXTURE>')

class Terminal:
    def __init__(self, argv, cwd, env, profile):
        self.raw=b''; self.pid,self.fd=pty.fork()
        if self.pid==0:
            os.chdir(cwd)
            os.execve('/usr/bin/sandbox-exec', ['sandbox-exec','-f',str(profile),*argv], env)
        fcntl.ioctl(self.fd,termios.TIOCSWINSZ,struct.pack('HHHH',40,120,0,0))
    def send(self,s): os.write(self.fd,s.encode())
    def pump(self, seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if select.select([self.fd],[],[],min(.1,max(0,end-time.monotonic())))[0]:
                try: data=os.read(self.fd,65536)
                except OSError: break
                if not data: break
                self.raw+=data
                if b'\x1b[6n' in data: self.send('\x1b[1;1R')
                if b'\x1b]11;?' in data: self.send('\x1b]11;rgb:0000/0000/0000\x1b\\')
    def close(self):
        # pty.fork creates a distinct session/process group; never target other sessions.
        try: os.killpg(self.pid,signal.SIGTERM)
        except (ProcessLookupError, PermissionError): pass
        self.pump(.3)
        try: os.killpg(self.pid,signal.SIGKILL)
        except (ProcessLookupError, PermissionError): pass
        os.waitpid(self.pid,0); os.close(self.fd)

def main():
    global TOOL_IMAGE
    parser=argparse.ArgumentParser(); parser.add_argument('--cli',choices=['pi','codex','both'],default='both')
    parser.add_argument('--cases',default='startup,live_quoted,live_bare,live_quoted_spaces,live_tool_read'); args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    result={'scope':'real interactive native TUI + loopback mock protocol/image bridge; not model quality or browser acceptance',
            'mock':True,'no_paid':True,'network_policy':'macOS sandbox-exec denies all network except localhost; no external model services',
            'environment':'allowlist only; temporary HOME, PI_CODING_AGENT_DIR, CODEX_HOME, XDG dirs; no inherited API keys', 'cases':[]}
    with tempfile.TemporaryDirectory(prefix='actual-native-',dir=OUT) as tmp:
        base=pathlib.Path(tmp).resolve(); project=base/'project'; project.mkdir()
        profile=base/'network.sb'; profile.write_text('(version 1)\n(allow default)\n(deny network*)\n(allow network-outbound (remote ip "localhost:*"))\n(allow network-bind (local ip "localhost:*"))\n(allow network-inbound (local ip "localhost:*"))\n')
        probe=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),'/usr/bin/python3','-c',
            'import socket; s=socket.socket(); s.settimeout(1); print(s.connect_ex(("192.0.2.1",443)))'],
            env={'PATH':'/usr/bin:/bin','HOME':str(base)},capture_output=True,text=True,timeout=10)
        if probe.stdout.strip() != '1': raise RuntimeError('Network deny preflight failed: '+probe.stdout+probe.stderr)
        result['external_network_denied_preflight']='EPERM connecting to reserved TEST-NET address 192.0.2.1:443'
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Mock)
        threading.Thread(target=server.serve_forever,daemon=True).start(); url=f'http://127.0.0.1:{server.server_port}/v1'
        img=project/'fixture.png'; img.write_bytes(png()); spaced=project/'fixture image.png'; spaced.write_bytes(png())
        result['fixture_image_sha256']=hashlib.sha256(png()).hexdigest()
        try:
            for cli in (BINS if args.cli=='both' else [args.cli]):
                for case in args.cases.split(','):
                    if case == 'live_tool_read' and cli != 'pi': continue
                    TOOL_IMAGE = str(img) if case == 'live_tool_read' else None
                    h=base/(cli+'-'+case); h.mkdir(); pi=h/'pi'; pi.mkdir(); codex=h/'codex'; codex.mkdir()
                    env={'PATH':'/opt/homebrew/bin:/usr/bin:/bin','HOME':str(h),'PI_CODING_AGENT_DIR':str(pi),'CODEX_HOME':str(codex),
                         'XDG_CONFIG_HOME':str(h/'config'),'XDG_CACHE_HOME':str(h/'cache'),'XDG_DATA_HOME':str(h/'data'),
                         'TMPDIR':str(h),'TERM':'xterm-256color','LANG':'en_US.UTF-8','PI_OFFLINE':'1','PI_TELEMETRY':'0'}
                    pi.joinpath('models.json').write_text(json.dumps({'providers':{'fixture':{'baseUrl':url,'api':'openai-completions','apiKey':'local-dummy-not-secret','models':[{'id':'gpt-5.4','name':'Fixture Vision','input':['text','image'],'reasoning':True,'contextWindow':128000,'maxTokens':1024}]}}}))
                    pi.joinpath('settings.json').write_text(json.dumps({'quietStartup':True,'enableTerminalTitle':False}))
                    codex.joinpath('config.toml').write_text(f'''model = "gpt-5.4"
model_provider = "fixture"
model_reasoning_effort = "medium"
check_for_update_on_startup = false
web_search = "disabled"
[analytics]
enabled = false
[model_providers.fixture]
name = "Local Fixture"
base_url = "{url}"
wire_api = "responses"
requires_openai_auth = false
[projects.{json.dumps(str(project))}]
trust_level = "trusted"
''')
                    ver=subprocess.run(['/usr/bin/sandbox-exec','-f',str(profile),BINS[cli],'--version'],env=env,cwd=project,capture_output=True,text=True,timeout=15).stdout.strip()
                    result.setdefault('installed_versions',{})[cli]=ver
                    argv=[BINS[cli]]
                    if cli=='pi': argv+=['--offline','--no-extensions','--no-skills','--no-prompt-templates','--no-themes','--no-context-files','--no-approve','--no-tools','--provider','fixture','--model','gpt-5.4']
                    else: argv+=['--no-alt-screen','--sandbox','read-only','--ask-for-approval','never']
                    if case=='live_tool_read':
                        argv.remove('--no-tools'); argv += ['--tools','read']
                    if case=='startup': argv+=['@'+str(img)] if cli=='pi' else ['--image',str(img)]
                    start=len(REQUESTS); term=Terminal(argv,project,env,profile); controls={}
                    try:
                        term.pump(3)
                        for _ in range(22):
                            if (b'Ask Codex' in term.raw if cli=='codex' else b'gpt-5.4' in term.raw): break
                            term.pump(1)
                        term.pump(12 if cli=='codex' else 2)
                        startup=clean(term.raw.decode(errors='replace'),base)
                        if 'Press enter to continue' in startup or 'Press Enter to continue' in startup:
                            term.send('\r'); term.pump(3); startup=clean(term.raw.decode(errors='replace'),base)
                        if case=='live_quoted':
                            for control in (['/model','/thinking','/session'] if cli=='pi' else ['/model','/status']):
                                offset=len(term.raw); term.send(control); term.pump(.3); term.send('\r'); term.pump(1.5)
                                controls[control]=clean(term.raw[offset:].decode(errors='replace'),base)
                                term.send('\x1b'); term.pump(.3)
                        if case!='startup':
                            path=str(spaced if case=='live_quoted_spaces' else img)
                            text=json.dumps(path) if 'quoted' in case else path
                            term.send('\x1b[200~'+text+'\x1b[201~'); term.pump(1)
                            term.send(' Describe fixture only.')
                        else: term.send('Describe fixture only.')
                        term.pump(.5); term.send('\r'); term.pump(7)
                    except OSError as error:
                        controls["fixture_error"]=str(error)
                    finally: term.close()
                    screen=clean(term.raw.decode(errors='replace'),base)
                    name=f'actual-native-{cli}-{case}-screen.txt'; OUT.joinpath(name).write_text(screen)
                    calls=REQUESTS[start:]
                    histories=list((pi if cli=='pi' else codex).rglob('*.jsonl'))
                    history_cwd=False
                    for history in histories:
                        for line in history.open():
                            try: entry=json.loads(line)
                            except ValueError: continue
                            if entry.get('cwd') == str(project) or entry.get('payload',{}).get('cwd') == str(project): history_cwd=True
                    resume={}
                    if case=='live_quoted':
                        resume_start=len(REQUESTS)
                        resume_argv=argv+(['--continue'] if cli=='pi' else ['resume','--last'])
                        resumed=Terminal(resume_argv,project,env,profile)
                        try: resumed.pump(6)
                        finally: resumed.close()
                        resume_screen=clean(resumed.raw.decode(errors='replace'),base)
                        resume={'history_reply_visible':'ACTUAL_NATIVE_FIXTURE_OK' in resume_screen,'mock_requests':len(REQUESTS)-resume_start,'command':[clean(a,base) for a in resume_argv]}
                        OUT.joinpath(f'actual-native-{cli}-resume-screen.txt').write_text(resume_screen)
                    row={'cli':cli,'case':case,'version':ver,'command':[clean(a,base) for a in argv],
                         'startup_ready':(('gpt-5.4' in startup and 'fixture' in startup.lower()) if cli=='pi' else 'OpenAI Codex' in startup),
                         'directory': '<FIXTURE>/project','cwd_screen_observed':str(project) in term.raw.decode(errors='replace'),
                         'startup_screen':startup,'controls':controls,'requests':calls,'image_payload_observed':any(c['images'] for c in calls),
                         'deterministic_reply_observed':'ACTUAL_NATIVE_FIXTURE_OK' in screen,
                         'cwd_native_history_observed':history_cwd,'native_resume':resume,'native_history_files_created':len(histories),'screen_file':name,
                         'blocked':[] if calls else ['No request reached local mock endpoint; inspect sanitized fixture screen']}
                    result['cases'].append(row)
                    print(json.dumps({k:row[k] for k in ['cli','case','startup_ready','cwd_screen_observed','image_payload_observed','deterministic_reply_observed','blocked']}),flush=True)
        finally: server.shutdown(); server.server_close()
    for row in result['cases']:
        row['image_payload_count'] = sum(len(call['images']) for call in row['requests'])
        row['first_request_image_count'] = len(row['requests'][0]['images']) if row['requests'] else None
        row['feature_result'] = ('blocked' if not row['requests'] else 'native_image_payload' if row['image_payload_observed'] else 'submitted_as_text_without_image')
    OUT.joinpath('actual-native-results.json').write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__': main()
