"""Independent native JSON pipe owner; no browser RPC passthrough or PTY parsing."""
from __future__ import annotations
import base64
import contextlib
import hashlib
import json
import copy
import jsonschema
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.native_cli import database, identifier, WorkerLock, SESSION_QUOTA, TOTAL_QUOTA, retained_bytes, ATTACHMENT_CAP

MAX_LINE = 64 * 1024 * 1024  # Native Pi events may echo one bounded 20 MiB image as base64.
MAX_EVENT = 256 * 1024


from agent.chat_catalog import public_model, commands, CAPABILITIES, COMMANDS, validate_settings


def bounded_event(event):
    if event.get('type') == 'settings':
        if 'model' in event:
            event['model'] = public_model(event['model'])
        if 'models' in event:
            event['models'] = [public_model(m) for m in event['models'][:200]]
    data = (json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    if len(data) <= MAX_EVENT:
        return data
    # Tool snapshots and model inventories can be huge. Never silently present
    # truncated approvals as actionable; they require the node-local native CLI.
    if event.get('type') == 'approval':
        event = {'type': 'error', 'receipt': event.get('receipt'),
                 'text': 'Native approval exceeds display limit; interrupt and use the node-local CLI', 'truncated': True}
    else:
        event = {k: v for k, v in event.items() if k in {'type', 'receipt', 'tool_id', 'name', 'status', 'item_id'}}
        event['text'] = data[:60000].decode('utf-8', errors='replace')
        event['truncated'] = True
    return (json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n').encode()



def image_inputs(attachments, provider):
    """The manager has bound validated uploads; recheck identity before reading."""
    images, files = [], []
    for attachment in attachments:
        path = Path(attachment['path'])
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > ATTACHMENT_CAP:
            raise ValueError('Attachment is no longer a valid upload')
        if attachment.get('size') is not None and path.stat().st_size!=attachment['size']:
            raise ValueError('Attachment size changed after upload')
        if attachment.get('sha256'):
            with path.open('rb') as source:
                if hashlib.file_digest(source,'sha256').hexdigest()!=attachment['sha256']:
                    raise ValueError('Attachment checksum changed after upload')
        mime = attachment.get('mime', '')
        if mime in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}:
            if provider == 'pi':
                images.append({'type': 'image', 'data': base64.b64encode(path.read_bytes()).decode(), 'mimeType': mime})
            else:
                images.append({'type': 'localImage', 'path': str(path)})
        else:
            files.append(str(path))
    return images, files


class Protocol:
    """Allowlisted native protocol translator. Only replies to observed requests."""
    def __init__(self, provider, row, send, emit, finish, persist):
        self.provider, self.row = provider, row
        self.send, self.emit, self.finish, self.persist = send, emit, finish, persist
        self.active = None
        self.turn = None
        self.thread = row.get('native_thread', '')
        self.ready = False
        self.pending = {}
        self.calls = {}
        self.serial = 0
        self.interrupted = False
        self.failure = None
        saved=json.loads(row.get('chat_settings','{}'))
        self.settings = saved.get('actual', {})
        self.settings['capabilities']=dict(CAPABILITIES, auto_compaction=False, auto_retry=False)
        self.settings_busy = False
        self.settings_receipt = None
        self.settings_remaining = 0
        self.next_settings = saved.get('next', {})
        self.next_settings_receipt = saved.get('next_receipt')
        self.turn_settings_receipt = None
        self.selection = {}
        self.operations = {}
        self.refresh_calls = {}
        self.refresh_errors = set()
        self.refresh_receipt = None
        self.settings_deadline = 0
        self.compaction_receipt = None
        self.operation_deadlines = {}
        self.defaults = saved.get("defaults", {})
        self.turn_settings = {}
        self.message_serial = 0
        self.message_id = ""
        self.command_prompt = False

    def call(self, method, params=None, tag=None):
        self.serial += 1
        key = 'codepier-' + str(self.serial)
        self.calls[key] = tag or method
        if self.refresh_receipt:
            self.refresh_calls[key]=self.refresh_receipt
            self.operation_deadlines[key]=time.monotonic()+15
        if self.provider == 'pi':
            self.send({'id': key, 'type': method, **(params or {})})
        else:
            self.send({'id': key, 'method': method, 'params': params or {}})
        return key

    def start(self):
        if self.provider == 'pi':
            self.call('get_state')
            self.call('get_available_models')
            self.call('get_available_thinking_levels')
            self.call('get_commands')
        else:
            self.call('initialize', {'clientInfo': {'name': 'codepier', 'version': '1.0.0'}, 'capabilities': {'experimentalApi': True}})

    def prompt(self, receipt, payload):
        if self.active or not self.ready or self.settings_busy:
            raise ValueError('Native session is not ready')
        images, files = image_inputs(payload.get('attachments', []), self.provider)
        text = payload['text']
        if files:
            text += '\n\nAttached files:\n' + '\n'.join(files)
        self.active, self.interrupted, self.failure = receipt, False, None
        self.message_id = ''
        self.command_prompt = self.provider=='pi' and payload['text'].lstrip().startswith('/')
        self.emit('user', receipt=receipt, text=payload['text'], attachments=[{'name': a.get('name', ''), 'mime': a.get('mime', '')} for a in payload.get('attachments', [])])
        if self.provider == 'pi':
            self.call('prompt', {'message': text, **({'images': images} if images else {})}, 'prompt')
        else:
            self.turn_settings=dict(self.next_settings)
            self.turn_settings_receipt=self.next_settings_receipt
            params = {'threadId': self.thread, 'input': [{'type': 'text', 'text': text}] + images, **self.turn_settings}
            self.call('turn/start', params)

    def save_settings(self):
        self.row['chat_settings']=json.dumps({'actual': self.settings, 'next': self.next_settings, 'defaults': self.defaults, 'next_receipt': self.next_settings_receipt})
        self.persist(self.thread)

    def publish_settings(self):
        self.settings['model']=public_model(self.settings.get('model'))
        self.settings['models']=[public_model(m) for m in self.settings.get('models', [])]
        current=self.settings.get('configured_model')
        if self.provider=='codex' and current and not any(current in (m.get('id'),m.get('model')) for m in self.settings['models']):
            self.settings['models'].append(dict(id=current,model=current,displayName=current+' (configured)',configured=True))
        if self.provider=='codex':
            selected=self.next_settings.get('model') or self.settings.get('model')
            match=next((m for m in self.settings['models'] if selected in (m.get('id'),m.get('model'))),{})
            self.settings['thinking_levels']=[x['reasoningEffort'] for x in match.get('supportedReasoningEfforts',[]) if 'reasoningEffort' in x]
        self.save_settings()
        self.emit('settings', **self.settings)

    def change_settings(self, receipt, payload):
        if self.active or self.settings_busy or not self.ready:
            raise ValueError('Wait for the native turn to finish before changing settings')
        selected=validate_settings(payload,self.provider)
        if not selected: raise ValueError('No settings supplied')
        for key,value in list(selected.items()):
            if value=='':
                if key not in self.defaults: raise ValueError('Native default unavailable; refresh native configuration first')
                selected[key]=self.defaults[key]
        if self.provider=='codex':
            self.next_settings.update(selected)
            self.next_settings_receipt=receipt
            self.save_settings()
            self.finish(receipt,'completed')
            self.emit('settings_pending',receipt=receipt,**self.next_settings,text='Accepted for the next turn; native validation occurs at turn/start')
            return
        self.settings_busy, self.settings_receipt = True, receipt
        self.settings_deadline=time.monotonic()+15
        self.selection=selected
        self.emit('settings_pending',receipt=receipt,**selected)
        if selected.get('model'):
            provider, mid=selected['model'].split('/',1)
            self.call('set_model',{'provider':provider,'modelId':mid},'settings_model')
        else:
            self.call('get_available_thinking_levels',tag='settings_levels')

    def settings_response(self, tag, success, data=None, error='Native setting rejected'):
        receipt=self.settings_receipt
        if not self.settings_busy: return
        if not success:
            for key in [k for k,v in self.calls.items() if v.startswith('settings_')]: self.calls.pop(key,None)
            self.settings_busy=False; self.settings_receipt=None
            self.finish(receipt,'error')
            self.emit('error',receipt=receipt,text=error)
            self.emit('settings_pending',receipt=receipt,cleared=True)
            if receipt is None: raise RuntimeError('Initial native settings rejected: '+error)
            self.call('get_state')
            return
        data=data or {}
        if tag=='settings_model':
            self.call('get_available_thinking_levels',tag='settings_levels')
        elif tag=='settings_levels':
            levels=data.get('levels',[]) if isinstance(data,dict) else data
            self.settings['thinking_levels']=levels
            effort=self.selection.get('effort')
            if effort and effort not in levels:
                self.settings_response(tag,False,error='Unsupported thinking level for selected native model')
            elif effort:
                self.call('set_thinking_level',{'level':effort},'settings_effort')
            else: self.call('get_state',tag='settings_confirm')
        elif tag=='settings_effort':
            self.call('get_state',tag='settings_confirm')
        elif tag=='settings_confirm':
            self.settings.update({k:data[k] for k in ('model','thinkingLevel') if k in data})
            model=data.get('model') or {}
            if self.selection.get('model') and self.selection['model']!=model.get('provider','')+'/'+model.get('id',''):
                self.settings_response(tag,False,error='Native model acknowledgement mismatch')
                return
            if self.selection.get('effort') and data.get('thinkingLevel')!=self.selection['effort']:
                self.settings_response(tag,False,error='Native thinking level acknowledgement mismatch')
                return
            self.next_settings.update(self.selection)
            self.settings_busy=False; self.settings_receipt=None
            self.publish_settings()
            self.emit('settings',receipt=receipt,**self.settings)
            self.finish(receipt,'completed')
            self.emit('settings_pending',receipt=receipt,cleared=True)

    def operation(self, receipt, name, method, params=None):
        key=self.call(method,params)
        self.operations[key]=(receipt,name)
        self.operation_deadlines[key]=time.monotonic()+(600 if name=='compact' else 15)
        if name=='compact':
            self.compaction_receipt=receipt
            self.compaction_deadline=time.monotonic()+600
        self.emit('command_result',receipt=receipt,name=name,state='running')

    def operation_response(self,m):
        op=self.operations.pop(m.get('id'),None)
        if not op: return False
        self.operation_deadlines.pop(m.get('id'),None)
        receipt,name=op
        success=m.get('success',False) if self.provider=='pi' else 'error' not in m
        state='completed' if success else 'error'
        if name=='compact' and self.provider=='codex' and success:
            self.calls.pop(m.get('id'),None)
            self.emit('command_result',receipt=receipt,name=name,state='accepted')
            return True
        self.finish(receipt,state)
        if name=='compact' and (self.provider=='pi' or not success): self.compaction_receipt=None
        self.calls.pop(m.get('id'),None)
        self.emit('command_result',receipt=receipt,name=name,state=state)
        if not success: self.emit('error',receipt=receipt,text='Native '+name+' rejected: '+str(m.get('error','unavailable'))[:1000])
        if success and name=='compact':
            self.refresh_receipt=receipt
            try: self.refresh()
            finally: self.refresh_receipt=None
        return True

    def refresh(self):
        if self.provider=='pi':
            for method in ('get_state','get_available_models','get_available_thinking_levels','get_commands','get_session_stats'): self.call(method)
        else:
            self.settings['models']=[]
            self.call('model/list')
            self.call('skills/list',{'cwds':[self.row['cwd']]})
            self.call('config/read',{'cwd':self.row['cwd'],'includeLayers':False})
            self.emit('stats',stats=self.settings.get('stats',{}))

    def command(self,receipt,payload):
        name=payload['name']
        if name not in COMMANDS[self.provider]: raise ValueError('Unsupported native command')
        if name=='compact' and self.compaction_receipt: raise ValueError('Native compaction is already active')
        if self.provider=='pi' and name in ('set_auto_compaction','set_auto_retry'):
            raise ValueError('Installed Pi persists this option globally; session-only toggle unavailable')
        if name=='refresh':
            self.refresh_receipt=receipt
            try: self.refresh()
            finally: self.refresh_receipt=None
            self.emit('command_result',receipt=receipt,name=name,state='running')
        elif self.provider=='pi':
            params={'enabled':payload['enabled']} if name.startswith('set_auto_') else {}
            if name=='compact' and payload.get('instructions'): params['customInstructions']=payload['instructions']
            self.operation(receipt,name,name,params)
        else:
            if payload.get('instructions'): raise ValueError('Codex compact does not support instructions')
            self.operation(receipt,name,'thread/compact/start',{'threadId':self.thread})

    def steer(self,receipt,payload):
        if not self.active or self.provider=='codex' and not self.turn:
            raise ValueError('Steer requires an observed active native turn; send a prompt while idle')
        images,files=image_inputs(payload.get('attachments',[]),self.provider)
        text=payload['text'] + ('\n\nAttached files:\n'+'\n'.join(files) if files else '')
        self.emit('user',receipt=receipt,parent_receipt=self.active,steering=True,text=payload['text'],attachments=[{'name':a.get('name',''),'mime':a.get('mime','')} for a in payload.get('attachments',[])])
        if self.provider=='pi': self.operation(receipt,'steer','steer',{'message':text,**({'images':images} if images else {})})
        else: self.operation(receipt,'steer','turn/steer',{'threadId':self.thread,'expectedTurnId':self.turn,'input':[{'type':'text','text':text}]+images})

    def interrupt(self):
        if not self.active:
            raise ValueError('No active native turn')
        self.interrupted = True
        if self.provider == 'pi':
            self.call('abort')
        elif self.turn:
            self.call('turn/interrupt', {'threadId': self.thread, 'turnId': self.turn})
        else:
            # turn/start response can be in flight. Its response issues interrupt.
            return

    def answer(self, request_id, answer, guard=None):
        request = self.pending.get(str(request_id))
        if not request or request.get('_deadline', float('inf')) < time.monotonic():
            raise ValueError('Native request is not pending')
        method = request.get('method')
        if guard is not None:
            details={k:v for k,v in request.items() if k!='_deadline'} if self.provider=='pi' else request.get('params',{})
            fingerprint=hashlib.sha256(json.dumps({'method':method,'details':details},sort_keys=True).encode()).hexdigest()
            if guard.get('parent_receipt')!=self.active or guard.get('request_guard')!=fingerprint:
                raise ValueError('Native request changed after answer was queued')
        if self.provider == 'pi':
            result = {'type': 'extension_ui_response', 'id': request['id']}
            if answer is None:
                result['cancelled'] = True
            elif method == 'confirm' and isinstance(answer, bool):
                result['confirmed'] = answer
            elif method in {'select', 'input', 'editor'} and isinstance(answer, str):
                if method == 'select' and answer not in request.get('options', []):
                    raise ValueError('Invalid native choice')
                result['value'] = answer
            else:
                raise ValueError('Invalid native answer')
            self.send(result)
        else:
            if method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
                choices = request.get('params', {}).get('availableDecisions')
                if (choices is not None and answer not in choices) or (choices is None and answer not in ('accept','acceptForSession','decline','cancel')):
                    raise ValueError('Invalid native approval choice')
                result = {'decision': copy.deepcopy(answer)}
            elif method == 'item/permissions/requestApproval':
                requested=request.get('params',{}).get('permissions',{})
                if not isinstance(answer,dict) or set(answer)-{'permissions','scope','strictAutoReview'} or 'permissions' not in answer:
                    raise ValueError('Invalid native permission answer')
                def subset(value,observed):
                    if isinstance(value,dict): return isinstance(observed,dict) and all(k in observed and subset(v,observed[k]) for k,v in value.items())
                    if isinstance(value,list): return isinstance(observed,list) and all(v in observed for v in value)
                    return value==observed or value is None or value is False
                if not isinstance(answer['permissions'],dict) or not subset(answer['permissions'],requested): raise ValueError('Permissions exceed observed request')
                if answer.get('scope','turn') not in ('turn','session'): raise ValueError('Invalid permission scope')
                if 'strictAutoReview' in answer and answer['strictAutoReview'] is not True: raise ValueError('Cannot weaken native auto review')
                result=copy.deepcopy(answer)
            elif method == 'mcpServer/elicitation/request':
                if not isinstance(answer,dict) or set(answer)-{'action','content'} or answer.get('action') not in ('accept','decline','cancel'): raise ValueError('Invalid elicitation answer')
                schema=request.get('params',{}).get('requestedSchema')
                if answer['action']=='accept' and schema:
                    if '$ref' in json.dumps(schema): raise ValueError('Referenced elicitation schema requires native client')
                    try: jsonschema.validate(answer.get('content'),schema)
                    except jsonschema.ValidationError: raise ValueError('Answer does not match observed elicitation schema') from None
                elif answer.get('content') is not None: raise ValueError('This elicitation does not accept form content')
                result=copy.deepcopy(answer)
            elif method == 'item/tool/requestUserInput':
                questions = request.get('params', {}).get('questions', [])
                if not isinstance(answer, dict) or set(answer) != {q['id'] for q in questions}:
                    raise ValueError('Answer must match pending question IDs')
                if any(not isinstance(v, list) or any(not isinstance(x, str) for x in v) for v in answer.values()):
                    raise ValueError('Invalid question answers')
                result = {'answers': {k: {'answers': list(v)} for k, v in answer.items()}}
            else:
                raise ValueError('This native request requires the node-local CLI')
            self.send({'id': request['id'], 'result': result})
        del self.pending[str(request_id)]
        self.emit('approval', request_id=str(request_id), resolved=True)

    def settled(self, status='completed', error=''):
        if not self.active:
            return
        status = 'interrupted' if self.interrupted else ('error' if self.failure else status)
        self.finish(self.active, status)
        self.emit('done', status=status, text=error or self.failure or '')
        for key in list(self.pending):
            self.emit('approval', request_id=key, resolved=True, resolution='closed')
        self.active = self.turn = None
        self.pending.clear()
        if self.provider=='pi': self.call('get_session_stats')

    def receive(self, message):
        receipt=self.refresh_calls.pop(message.get('id'),None)
        if receipt: self.operation_deadlines.pop(message.get('id'),None)
        emit=self.emit
        if receipt:
            self.refresh_receipt=receipt
            self.emit=lambda kind,**values: emit(kind,**{'receipt':receipt,**values})
            if message.get('error') or self.provider=='pi' and not message.get('success',False): self.refresh_errors.add(receipt)
        try:
            if self.provider == 'pi': self.pi(message)
            else: self.codex(message)
        finally:
            self.emit=emit;self.refresh_receipt=None
            if receipt and receipt not in self.refresh_calls.values():
                state='error' if receipt in self.refresh_errors else 'completed'
                self.refresh_errors.discard(receipt)
                self.finish(receipt,state)
                self.emit('command_result',receipt=receipt,name='refresh',state=state)

    def pi(self, m):
        if self.operation_response(m): return
        kind = m.get('type')
        if kind == 'response':
            tag = self.calls.pop(m.get('id'), '')
            if not tag: return
            if tag and tag.startswith('settings_'):
                self.settings_response(tag,m.get('success',False),m.get('data'),m.get('error','Native setting rejected'))
                return
            if not m.get('success', False):
                if tag == 'prompt':
                    self.settled('error', m.get('error', 'Native prompt rejected'))
                else:
                    self.emit('error',receipt=self.refresh_receipt,text=m.get('error', 'Native command failed'))
                    if tag == 'get_state' and not self.ready:
                        raise RuntimeError('Native Pi state initialization failed')
                return
            data = m.get('data') or {}
            if tag == 'prompt' and self.command_prompt and self.active:
                self.call('get_state',tag='prompt_idle_check')
            elif tag == 'prompt_idle_check':
                if self.active and not self.pending and not data.get('isStreaming') and not data.get('isCompacting') and not data.get('pendingMessageCount'):
                    self.settled()
            elif tag == 'get_state':
                initial=not self.ready
                native=data.get('model') or {}
                if native: self.defaults.setdefault('model',native.get('provider','')+'/'+native.get('id',''))
                self.defaults.setdefault('effort',data.get('thinkingLevel','off'))
                self.ready = True
                self.settings.update({k: data[k] for k in ('model', 'thinkingLevel','autoCompactionEnabled') if k in data})
                self.publish_settings()
                if initial and any(self.next_settings.values()): self.change_settings(None,self.next_settings)
            elif tag == 'get_commands':
                self.emit('commands',commands=commands(data,'pi'))
            elif tag == 'get_session_stats':
                self.emit('stats',stats={k:data[k] for k in ('userMessages','assistantMessages','toolCalls','toolResults','totalMessages','tokens','cost','contextUsage') if k in data})
            elif tag == 'get_available_models':
                self.settings['models'] = data.get('models', []) if isinstance(data, dict) else data
                self.publish_settings()
            elif tag == 'get_available_thinking_levels':
                self.settings['thinking_levels'] = data.get('levels', []) if isinstance(data, dict) else data
                self.publish_settings()
        elif kind == 'message_start' and m.get('message',{}).get('role')=='assistant':
            self.message_serial+=1
            self.message_id='pi-message-'+str(self.message_serial)
        elif kind == 'message_update':
            event = m.get('assistantMessageEvent', {})
            if event.get('type') in ('text_delta', 'thinking_delta'):
                self.emit('delta' if event['type'] == 'text_delta' else 'reasoning', text=event.get('delta', ''),item_id=self.message_id)
        elif kind == 'message_end':
            message = m.get('message', {})
            if message.get('role')=='assistant':
                text=''.join(c.get('text','') for c in message.get('content',[]) if c.get('type')=='text')
                if text: self.emit('message',text=text,item_id=self.message_id)
            if message.get('stopReason') in ('error', 'aborted'):
                self.failure = message.get('errorMessage', 'Native response failed')
                self.emit('error', text=self.failure)
        elif kind and kind.startswith('tool_execution_'):
            self.emit('tool', tool_id=m.get('toolCallId', ''), name=m.get('toolName', ''), status=kind.removeprefix('tool_execution_'), text=json.dumps(m.get('result', m.get('partialResult', m.get('args', {}))), ensure_ascii=False))
        elif kind == 'extension_ui_request':
            if m.get('method') in ('select', 'confirm', 'input', 'editor'):
                self.pending[str(m['id'])] = json.loads(json.dumps(m))
                if m.get('timeout'): self.pending[str(m['id'])]['_deadline']=time.monotonic()+m['timeout']/1000
                self.emit('approval', request_id=str(m['id']), method=m['method'], text=m.get('title', ''), options=m.get('options', []), details=m)
            elif m.get('method') == 'notify':
                self.emit('tool', name='Notice', text=m.get('message', ''))
        elif kind in ('compaction_start','compaction_end'):
            self.emit('command_result',receipt=self.compaction_receipt,name='compact',state='running' if kind=='compaction_start' else ('error' if m.get('errorMessage') else 'completed'),text=m.get('errorMessage',''))
        elif kind == 'agent_settled':
            self.settled()

    def codex(self, m):
        if self.operation_response(m): return
        method, params = m.get('method', ''), m.get('params', {})
        if self.thread and params.get('threadId') and params['threadId'] != self.thread:
            return
        if 'id' in m and method:
            self.pending[str(m['id'])] = json.loads(json.dumps(m))
            self.emit('approval', request_id=str(m['id']), method=method, text=params.get('reason', method), details=params,
                      options=params.get('availableDecisions', ['accept', 'acceptForSession', 'decline', 'cancel']) if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval') else [])
            return
        if 'id' in m:
            tag = self.calls.pop(m['id'], '')
            if tag.startswith('settings_'):
                self.settings_response(tag, 'error' not in m)
                if 'error' in m:
                    self.emit('error', text=m['error'].get('message', 'Native setting rejected'))
                return
            if 'error' in m:
                text = m['error'].get('message', 'Native RPC failed')
                self.emit('error',receipt=self.active if tag=='turn/start' else self.refresh_receipt,text=text)
                if tag == 'turn/start':
                    if self.turn_settings_receipt:
                        self.emit('error',receipt=self.turn_settings_receipt,text='Next-turn settings were not accepted: '+text)
                        self.finish(self.turn_settings_receipt,'error')
                    self.settled('error', text)
                elif tag in ('initialize', 'thread/start', 'thread/resume'):
                    raise RuntimeError('Native session initialization failed: ' + text)
                return
            data = m.get('result') or {}
            if tag == 'initialize':
                self.send({'method': 'initialized', 'params': {}})
                self.call('thread/resume' if self.thread else 'thread/start', {'cwd': self.row['cwd'], **({'threadId': self.thread} if self.thread else {})})
                self.call('model/list', {})
                self.call('skills/list',{'cwds':[self.row['cwd']]})
                self.call('config/read',{'cwd':self.row['cwd'],'includeLayers':False})
            elif tag in ('thread/start', 'thread/resume'):
                self.thread = data['thread']['id']
                self.persist(self.thread)
                self.ready = True
                self.settings.update({k: data[k] for k in ('model', 'reasoningEffort') if k in data})
                self.publish_settings()
            elif tag=='skills/list':
                self.emit('commands',commands=commands(data,'codex'))
            elif tag=='config/read':
                config=data.get('config',{})
                current=config.get('model')
                if isinstance(current,str):
                    self.settings['configured_model']=current
                    self.defaults['model']=current
                self.defaults['effort']=config.get('model_reasoning_effort')
                self.publish_settings()
            elif tag == 'model/list':
                self.settings['models'] = self.settings.get('models', []) + data.get('data', [])
                self.publish_settings()
                if data.get('nextCursor'):
                    self.call('model/list', {'cursor': data['nextCursor']})
            elif tag == 'turn/start':
                self.turn = data['turn']['id']
                if self.turn_settings:
                    self.settings.update(self.turn_settings)
                    if 'effort' in self.turn_settings: self.settings['reasoningEffort']=self.turn_settings['effort']
                    self.publish_settings()
                    if self.turn_settings_receipt:
                        self.emit('settings',receipt=self.turn_settings_receipt,**self.settings)
                        self.emit('settings_pending',receipt=self.turn_settings_receipt,cleared=True)
                        self.next_settings_receipt=None
                        self.save_settings()
                    self.emit('settings_pending',cleared=True)
                if self.interrupted:
                    self.interrupt()
            return
        if method=='thread/tokenUsage/updated':
            self.settings['stats']=params.get('tokenUsage',{})
            self.emit('stats',stats=self.settings['stats'])
        elif method == 'thread/settings/updated':
            settings = params.get('threadSettings', {})
            self.settings.update({k: settings[k] for k in ('model', 'effort') if k in settings})
            self.publish_settings()
        elif method == 'item/agentMessage/delta':
            self.emit('delta', text=params.get('delta', ''), item_id=params.get('itemId', ''))
        elif method in ('item/reasoning/summaryTextDelta', 'item/reasoning/textDelta'):
            self.emit('reasoning', text=params.get('delta', ''))
        elif method in ('item/started', 'item/completed'):
            item = params.get('item', {})
            if item.get('type')=='contextCompaction':
                self.emit('command_result',receipt=self.compaction_receipt,name='compact',state='running' if method=='item/started' else 'completed')
                if method=='item/completed':
                    if self.compaction_receipt: self.finish(self.compaction_receipt,'completed')
                    self.compaction_receipt=None
            if item.get('type')=='agentMessage' and method=='item/completed' and item.get('text'):
                self.emit('message',text=item['text'],item_id=item.get('id',''))
            if item.get('type') not in ('agentMessage', 'userMessage', 'reasoning'):
                self.emit('tool', tool_id=item.get('id', ''), name=item.get('type', ''), status=method.split('/')[-1], text=json.dumps(item, ensure_ascii=False))
        elif method == 'item/commandExecution/outputDelta':
            self.emit('tool', tool_id=params.get('itemId', ''), name='commandExecution', status='delta', text=params.get('delta', ''))
        elif method == 'serverRequest/resolved':
            key=str(params.get('requestId'))
            if key in self.pending:
                del self.pending[key]
                self.emit('approval',request_id=key,resolved=True)
        elif method == 'turn/started':
            self.turn = params['turn']['id']
        elif method == 'turn/completed':
            turn = params.get('turn', {})
            self.settled({'failed': 'error', 'interrupted': 'interrupted'}.get(turn.get('status'), 'completed'), (turn.get('error') or {}).get('message', ''))
        elif method == 'error':
            self.emit('error', text=(params.get('error') or {}).get('message', 'Native error'))


def run(directory, sid, config_path=None):
    sid = identifier(sid)
    ownership = WorkerLock(directory, sid)
    try:
        db = database(directory)
    except BaseException:
        ownership.close()
        raise
    child = None
    protocol = None
    stop = False
    status, error = 'exited', ''
    selector = selectors.DefaultSelector()
    admitted = False
    buffers = {}
    outgoing = bytearray()
    try:
        raw = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        if not raw or raw['status'] != 'starting':
            return
        admitted = True
        row = dict(raw)
        if row.get('mode') != 'chat':
            raise ValueError('Not a structured chat session')
        if db.execute("SELECT 1 FROM commands WHERE session=? AND state='claimed'", (sid,)).fetchone():
            raise RuntimeError('Uncertain native prompt from previous worker; not replayed')
        reviews = None
        if config_path:
            from agent.chat_reviews import TurnReviews
            reviews = TurnReviews(directory, row, config_path)
        size = row['size']
        def emit(kind, **values):
            nonlocal size
            event = {'type': kind, 'receipt': protocol.active if protocol else None, **values}
            data = bounded_event(event)
            if len(data) > MAX_LINE or size + len(data) > SESSION_QUOTA:
                raise RuntimeError('TRANSCRIPT_QUOTA')
            for start in range(0, len(data), 65536):
                chunk = data[start:start + 65536]
                db.execute('INSERT INTO output VALUES (?,?,?)', (sid, size, chunk))
                size += len(chunk)
            db.execute('UPDATE sessions SET size=?,updated=? WHERE id=?', (size, time.time(), sid))
            db.commit()
        def finish(receipt, state):
            db.execute('UPDATE commands SET state=? WHERE id=?', (state, receipt))
            db.commit()
            if reviews:
                review = reviews.finish(receipt)
                if review:
                    emit('review', receipt=receipt, **review)
        def persist(thread):
            db.execute('UPDATE sessions SET native_thread=?,chat_settings=?,updated=? WHERE id=?', (thread,protocol.row.get('chat_settings','{}'), time.time(), sid))
            db.commit()
        def send(value):
            data = (json.dumps(value, separators=(',', ':')) + '\n').encode()
            if len(outgoing) + len(data) > 32 * 1024 * 1024:
                raise RuntimeError('Native input buffer quota exceeded')
            outgoing.extend(data)
        def stop_signal(*args):
            nonlocal stop
            stop = True
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, stop_signal)
        if os.name == 'nt':
            raise RuntimeError('Structured chat pipe worker is not supported on Windows; use node-local CLI')
        argv = json.loads(row['argv'])
        if row['provider'] == 'pi':
            from agent.native_pi_bridge import prepare_shell_guard
            argv += ['--extension', str(prepare_shell_guard(Path(directory)))]
        child = subprocess.Popen(argv, cwd=row['cwd'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != 'nt', bufsize=0)
        for pipe in (child.stdout, child.stderr):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ)
            buffers[pipe] = bytearray()
        os.set_blocking(child.stdin.fileno(), False)
        db.execute("UPDATE sessions SET status='running',worker_pid=?,child_pid=?,heartbeat=?,updated=? WHERE id=?", (os.getpid(), child.pid, time.time(), time.time(), sid))
        db.commit()
        protocol = Protocol(row['provider'], row, send, emit, finish, persist)
        protocol.start()
        heartbeat = 0
        initialize_deadline = time.monotonic() + 60
        last_history_check = 0
        while child.poll() is None and not stop:
            now = time.time()
            if not protocol.ready and time.monotonic() > initialize_deadline:
                raise RuntimeError('Native initialization timed out after 60 seconds; inspect node-local CLI')
            if now - last_history_check >= 5 and row['provider'] == 'pi':
                last_history_check = now
                native_history = Path(argv[argv.index('--session') + 1]) if '--session' in argv else None
                if native_history and native_history.exists() and native_history.stat().st_size > SESSION_QUOTA:
                    raise RuntimeError('NATIVE_HISTORY_QUOTA')
            if now - heartbeat >= 1:
                db.execute('UPDATE sessions SET heartbeat=? WHERE id=?', (now, sid))
                db.commit()
                heartbeat = now
                if retained_bytes(db) > TOTAL_QUOTA:
                    raise RuntimeError('TOTAL_QUOTA')
            kinds = ('stop', 'chat_interrupt', 'chat_answer', 'chat_steer', 'chat_command') if protocol.active or not protocol.ready or protocol.settings_busy or protocol.compaction_receipt else ('stop', 'chat_interrupt', 'chat_answer', 'chat_prompt', 'chat_settings', 'chat_steer', 'chat_command')
            command = db.execute('SELECT * FROM commands WHERE session=? AND state=\'queued\' AND kind IN (' + ','.join('?' for _ in kinds) + ') ORDER BY CASE WHEN kind=\'stop\' THEN 0 ELSE 1 END,rowid LIMIT 1', (sid, *kinds)).fetchone()
            if command:
                claimed=db.execute("UPDATE commands SET state='claimed' WHERE id=? AND session=? AND state='queued'", (command['id'],sid)).rowcount
                db.commit()  # durable before native write; crash cannot duplicate prompt
                if not claimed: continue
                try:
                    payload = json.loads(command['payload'])
                    if command['kind'] == 'stop':
                        stop = True
                    elif command['kind'] == 'chat_prompt':
                        if reviews:
                            reviews.begin(command['id'])
                        protocol.prompt(command['id'], payload)
                    elif command['kind'] == 'chat_steer':
                        protocol.steer(command['id'],payload)
                    elif command['kind'] == 'chat_command':
                        protocol.command(command['id'],payload)
                    elif command['kind'] == 'chat_settings':
                        protocol.change_settings(command['id'], payload)
                    elif command['kind'] == 'chat_interrupt':
                        protocol.interrupt()
                        finish(command['id'], 'completed')
                    elif command['kind'] == 'chat_answer':
                        protocol.answer(payload['request_id'], payload['answer'],payload if 'request_guard' in payload else None)
                        finish(command['id'], 'completed')
                except (ValueError, KeyError, OSError) as exc:
                    finish(command['id'], 'error')
                    emit('error', receipt=command['id'], text=str(exc))
            for key,deadline in list(protocol.operation_deadlines.items()):
                if time.monotonic()>deadline:
                    protocol.receive({'id':key,'type':'response','success':False,'error':{'message':'Native command acknowledgement timed out'}})
            if protocol.compaction_receipt and time.monotonic()>protocol.compaction_deadline:
                finish(protocol.compaction_receipt,'error')
                emit('error',receipt=protocol.compaction_receipt,text='Native compaction completion timed out')
                protocol.compaction_receipt=None
            if protocol.settings_busy and time.monotonic()>protocol.settings_deadline:
                protocol.settings_response('settings_timeout',False,error='Native settings acknowledgement timed out')
            if outgoing:
                try:
                    n = os.write(child.stdin.fileno(), outgoing[:65536])
                    del outgoing[:n]
                except BlockingIOError:
                    pass
            for key, _ in selector.select(.025):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is child.stderr:
                    # Runtime stderr can contain credentials/config. It is not chat.
                    continue
                buffer = buffers[key.fileobj]
                buffer.extend(data)
                while b'\n' in buffer:
                    line, _, remaining = buffer.partition(b'\n')
                    buffer[:] = remaining
                    if line.strip():
                        if len(line) > MAX_LINE:
                            raise RuntimeError('Native JSON line quota exceeded')
                        protocol.receive(json.loads(line))
                if len(buffer) > MAX_LINE:
                    raise RuntimeError('Native JSON line quota exceeded')
        if not protocol.ready and not stop:
            raise RuntimeError('Native CLI exited before initialization; check login/configuration in node-local CLI')
        if protocol.active:
            protocol.settled('interrupted' if stop else 'error', 'Native process stopped before completion')
    except BaseException as exc:
        if not admitted:
            raise
        status = 'quota_error' if 'QUOTA' in str(exc) else 'interrupted'
        error = str(exc)[:500] if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        if protocol is not None:
            with contextlib.suppress(Exception):
                emit('error', text=error)
    finally:
        try:
            if child:
                def signal_owned(number):
                    # Only signal our live unreaped direct child/group, never a
                    # persisted PID. It may exit between poll/getpgid/killpg.
                    if child.poll() is not None:
                        return
                    with contextlib.suppress(ProcessLookupError):
                        if os.name != 'nt' and os.getpgid(child.pid) == child.pid:
                            os.killpg(child.pid, number)
                        else:
                            child.send_signal(number)
                try:
                    signal_owned(signal.SIGTERM)
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        signal_owned(signal.SIGKILL)
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            status, error = 'orphaned', 'Owned native child has not exited; inspect Agent host'
                finally:
                    for pipe in (child.stdin, child.stdout, child.stderr):
                        with contextlib.suppress(OSError):
                            pipe.close()
            selector.close()
            if admitted:
                db.execute("UPDATE commands SET state='uncertain' WHERE session=? AND state='claimed' AND kind!='stop'", (sid,))
                db.execute("UPDATE commands SET state=? WHERE session=? AND state='claimed' AND kind='stop'", ('uncertain' if status == 'orphaned' else 'completed', sid))
                db.execute("UPDATE commands SET state='interrupted' WHERE session=? AND state='queued'", (sid,))
                db.execute('UPDATE sessions SET status=?,error=?,exit_code=?,updated=?,heartbeat=? WHERE id=?', (status, error, child.returncode if child else None, time.time(), time.time(), sid))
                db.commit()
        finally:
            try:
                db.close()
            finally:
                ownership.close()


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv)>3 else None)
