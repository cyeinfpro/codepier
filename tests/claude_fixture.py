"""Deterministic Claude Code wire simulator. No SDK, credentials or network."""
FAKE_CLAUDE = r'''
import json, os, sys, time, uuid
from pathlib import Path
if '--version' in sys.argv:
 print('fixture-claude 1.0');sys.exit(0)
Path('claude-argv.json').write_text(json.dumps(sys.argv[1:]))
sid=sys.argv[sys.argv.index('--resume')+1] if '--resume' in sys.argv else str(uuid.uuid4())
model=sys.argv[sys.argv.index('--model')+1] if '--model' in sys.argv else 'sonnet'
effort=sys.argv[sys.argv.index('--effort')+1] if '--effort' in sys.argv else 'high'
turn=0;pending=None

def emit(message):
 raw=(json.dumps(message,ensure_ascii=False)+'\n').encode()
 cut=len(raw)//2
 os.write(1,raw[:cut]);os.write(1,raw[cut:])

def ack(message,data=None,error=None):
 value={'subtype':'error' if error else 'success','request_id':message['request_id']}
 value.update({'error':error} if error else {'response':data or {}})
 emit({'type':'control_response','response':value})

def result(text='hello 世界',failed=False):
 emit({'type':'result','subtype':'success','is_error':failed,'session_id':sid,'uuid':str(uuid.uuid4()),'result':text,
       'usage':{'input_tokens':12,'output_tokens':8,'cache_read_input_tokens':3,'cache_creation_input_tokens':2},'total_cost_usd':0.001})

def complete():
 global turn
 turn+=1;mid='msg-'+str(turn)
 def stream(event):emit({'type':'stream_event','event':event})
 def final(block):emit({'type':'assistant','uuid':str(uuid.uuid4()),'message':{'id':mid,'content':[block]}})
 stream({'type':'message_start','message':{'id':mid}})
 stream({'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}})
 stream({'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'fixture thought'}})
 final({'type':'thinking','thinking':'fixture thought'})
 stream({'type':'content_block_stop','index':0})
 stream({'type':'content_block_start','index':1,'content_block':{'type':'text','text':''}})
 for text in ('hello ','世界'):
  stream({'type':'content_block_delta','index':1,'delta':{'type':'text_delta','text':text}})
 final({'type':'text','text':'hello 世界'})
 stream({'type':'content_block_stop','index':1})
 tool={'type':'tool_use','id':'tool-'+str(turn),'name':'Read','input':{'file_path':'fixture-edit.txt'}}
 stream({'type':'content_block_start','index':2,'content_block':tool})
 final(tool)
 stream({'type':'content_block_stop','index':2})
 stream({'type':'message_stop'})
 Path('fixture-edit.txt').write_text('fixture turn '+str(turn)+'\n')
 emit({'type':'user','message':{'role':'user','content':[{'type':'tool_result','tool_use_id':'tool-'+str(turn),'content':[{'type':'text','text':'fixture tool output'}]}]}})
 result()

for line in sys.stdin:
 m=json.loads(line)
 with open('claude-wire.jsonl','a') as log:log.write(json.dumps(m)+'\n')
 if m.get('type')=='control_request':
  r=m['request'];kind=r['subtype']
  if kind=='initialize':
   ack(m,{'models':[{'value':'sonnet','displayName':'Fixture Sonnet','supportsEffort':True,'supportedEffortLevels':['low','high','max'],'apiKey':'must-not-leak'},
                    {'value':'haiku','displayName':'Fixture Haiku'}],
          'commands':[{'name':'compact','description':'Compact'},{'name':'fixture-skill','description':'Fixture'}],
          'session_state':{'model':model,'effort':effort,'apiKey':'must-not-leak'},'account':{'token':'must-not-leak'}})
  elif kind=='set_model':
   if r.get('model')=='invalid':ack(m,error='Invalid model')
   else:model=r.get('model') or 'sonnet';ack(m)
  elif kind=='interrupt':
   # Result-before-ACK exercises the queue's acknowledgement barrier.
   result('interrupted');ack(m)
  else:ack(m,error='Unsupported control request')
 elif m.get('type')=='user':
  content=m['message']['content'];text='\n'.join(x.get('text','') for x in content if x.get('type')=='text')
  emit({'type':'system','subtype':'init','session_id':sid,'model':model})
  if text=='hold':continue
  if text=='error':result('API Error: fixture failed',True);continue
  if text in ('approve','question'):
   pending=str(uuid.uuid4())
   data={'command':'printf fixture-only'} if text=='approve' else {'questions':[{'question':'Choose target','multiSelect':False,'options':[{'label':'A'},{'label':'B'}]},
          {'question':'Choose checks','multiSelect':True,'options':[{'label':'Unit'},{'label':'Browser'}]}]}
   emit({'type':'control_request','request_id':pending,'request':{'subtype':'can_use_tool','tool_name':'Bash' if text=='approve' else 'AskUserQuestion','input':data}})
   continue
  complete()
 elif m.get('type')=='control_response':
  if m['response']['request_id']==pending:
   pending=None;complete()
'''
