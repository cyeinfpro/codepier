"""Stateful Claude Code stream-json adapter, owned by the existing pipe worker.

Only initialize, model selection and interrupt use the control channel. Browser
answers are bound to an observed tool request and never grant persistent rules.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from collections import OrderedDict, deque

from agent.chat_catalog import validate_settings
from agent.chat_worker import Protocol, image_inputs
from agent.claude_cli import CAPS, catalog


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(x['text'] for x in value if isinstance(x, dict)
                         and x.get('type') == 'text' and isinstance(x.get('text'), str))
    return ''


class ClaudeProtocol(Protocol):
    def __init__(self, provider, row, send, emit, finish, persist):
        super().__init__(provider, row, send, emit, finish, persist)
        self.settings['capabilities'] = dict(CAPS)
        self.defaults = {'model': '', 'effort': ''}
        self.streams = {}
        self.message_streams = OrderedDict()
        self.thoughts = {}
        self.tools = {}
        self.closed_results = deque(maxlen=128)
        self.closed_requests = deque(maxlen=256)
        self.stats = {}
        self.had_text = False
        self.active_command = None

    def call(self, method, params=None, tag=None):
        self.serial += 1
        key = 'codepier-claude-' + str(self.serial)
        self.calls[key] = (method, tag)
        self.operation_deadlines[key] = time.monotonic() + (55 if method == 'initialize' else 15)
        self.send({'type': 'control_request', 'request_id': key,
                   'request': {'subtype': method, **(params or {})}})
        return key

    def start(self):
        self.call('initialize', {'hooks': None})

    def publish_settings(self):
        self.settings['capabilities'] = dict(CAPS)
        model = self.settings.get('model')
        mid = model.get('id') if isinstance(model, dict) else model
        selected = next((m for m in self.settings.get('models', []) if m.get('id') == mid), None)
        if selected:
            self.settings['model'] = selected
        self.settings['thinking_levels'] = [x['reasoningEffort'] for x in (selected or {}).get('supportedReasoningEfforts', [])]
        self.save_settings()
        self.emit('settings', **self.settings)

    def prompt(self, receipt, payload):
        if self.active or not self.ready or self.settings_busy:
            raise ValueError('Native session is not ready')
        text = payload['text']
        if text.split() and text.split()[0] in {'/clear', '/reset', '/resume', '/new'}:
            raise ValueError('请使用面板的新对话或恢复会话，不在同一记录内切换原生会话')
        images, files = image_inputs(payload.get('attachments', []), 'claude')
        self.active, self.interrupted, self.failure = receipt, False, None
        self.streams.clear(); self.thoughts.clear(); self.tools.clear()
        self.message_streams.clear()
        self.had_text = False
        self.emit('user', receipt=receipt, text=text,
                  attachments=[{'name': a.get('name', ''), 'mime': a.get('mime', '')}
                               for a in payload.get('attachments', [])])
        if files:
            text += '\n\nAttached files:\n' + '\n'.join(files)
        content = ([{'type': 'text', 'text': text}] if text else []) + images
        self.send({'type': 'user', 'uuid': str(uuid.uuid4()),
                   'session_id': self.thread or '', 'parent_tool_use_id': None,
                   'message': {'role': 'user', 'content': content}})

    def change_settings(self, receipt, payload):
        if not self.ready or self.active or self.settings_busy:
            raise ValueError('Native settings require an idle session')
        patch = validate_settings(payload, 'claude')
        if 'effort' in patch and patch['effort'] != self.next_settings.get('effort', ''):
            raise ValueError('Claude 思考强度在启动时生效；请停止会话后选择强度并恢复')
        if 'model' not in patch:
            self.finish(receipt, 'completed')
            return
        self.settings_busy = True
        self.settings_deadline = time.monotonic() + 30
        self.selection = {'model': patch['model']}
        self.settings_receipt = receipt
        # None restores the native default; never rewrite ~/.claude/settings.json.
        self.call('set_model', {'model': patch['model'] or None}, receipt)

    def steer(self, receipt, payload):
        raise ValueError('Claude 暂不支持立即补充，请使用排队跟进或中断当前回复')

    def interrupt(self, receipt=None):
        if not self.active:
            raise ValueError('No active native turn')
        if any(method == 'interrupt' for method, _ in self.calls.values()):
            raise ValueError('Claude interrupt is already pending')
        self.interrupted = True
        self.settings_busy = True
        self.settings_deadline = time.monotonic() + 30
        self.call('interrupt', tag=receipt)

    def command(self, receipt, payload):
        name = payload['name']
        if name == 'refresh':
            self.publish_settings()
            self.emit('commands', commands=self.settings.get('commands', []))
            if self.stats:
                self.emit('stats', stats=self.stats)
            self.finish(receipt, 'completed')
            self.emit('command_result', receipt=receipt, name=name, state='completed',
                      text='已刷新本会话状态；模型和指令来自原生启动握手')
        elif name == 'compact':
            self.prompt(receipt, {'text': '/compact' + (' ' + payload['instructions'] if payload.get('instructions') else '')})
            self.active_command = name
        else:
            raise ValueError('Unsupported Claude command')

    def observe_session(self, message):
        sid = message.get('session_id')
        if not sid:
            return
        try:
            sid = str(uuid.UUID(sid))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError('Invalid native Claude session ID') from exc
        if self.thread and sid != self.thread:
            raise ValueError('Claude changed native session unexpectedly; original history preserved')
        if not self.thread:
            self.thread = sid
            self.save_settings()

    def control_reply(self, request_id, response=None, error=None):
        body = {'subtype': 'error' if error else 'success', 'request_id': request_id}
        body.update({'error': error} if error else {'response': response or {}})
        self.send({'type': 'control_response', 'response': body})

    def permission(self, message):
        key, request = message.get('request_id'), message.get('request')
        if not isinstance(key, str) or not key or len(key) > 256 or not isinstance(request, dict):
            raise ValueError('Malformed Claude control request')
        if key in self.pending or key in self.closed_requests:
            return  # A duplicate never replaces a question the user is answering.
        if request.get('subtype') != 'can_use_tool':
            self.control_reply(key, error='Unsupported CodePier control request')
            return
        if not self.active or self.interrupted:
            self.control_reply(key, {'behavior': 'deny', 'message': 'No active CodePier request'})
            return
        tool, inputs = request.get('tool_name'), request.get('input')
        if not isinstance(tool, str) or not isinstance(inputs, dict):
            self.control_reply(key, error='Invalid tool permission request')
            return
        if len(self.pending) >= 128 or len(json.dumps(inputs).encode()) > 100000:
            self.control_reply(key, {'behavior': 'deny', 'message': 'Approval exceeds safe display limit'})
            self.emit('error', text='Claude 审批内容过大，已拒绝；请在节点本机核查')
            return
        inputs = copy.deepcopy(inputs)
        details = {'tool_name': tool, 'input': inputs, 'command': json.dumps(inputs, ensure_ascii=False),
                   'cwd': self.row['cwd']}
        method = 'confirm'
        if tool == 'AskUserQuestion':
            questions = inputs.get('questions', [])
            if not isinstance(questions, list) or not 1 <= len(questions) <= 4 or any(
                not isinstance(q, dict) or not isinstance(q.get('question'), str) or not q['question'] or not isinstance(q.get('options', []), list)
                for q in questions
            ) or len({q['question'] for q in questions}) != len(questions):
                self.control_reply(key, error='Unsupported question schema')
                return
            method = 'claude/question'
            details['questions'] = [dict(id=q['question'], question=q['question'],
                                         options=[{'label': o['label'], 'description': o.get('description', '')}
                                                  for o in q.get('options', []) if isinstance(o, dict) and isinstance(o.get('label'), str)],
                                         multiSelect=q.get('multiSelect') is True) for q in questions]
        if len(json.dumps(details, ensure_ascii=False).encode()) > 180000:
            self.control_reply(key, {'behavior': 'deny', 'message': 'Approval exceeds safe display limit'})
            self.emit('error', text='Claude 审批内容过大，已拒绝；请在节点本机核查')
            return
        self.pending[key] = {'method': method, 'details': details, 'receipt': self.active, 'input': inputs}
        self.emit('approval', request_id=key, method=method, text=tool + ' · 需要确认', details=details)

    def answer(self, request_id, answer, guard=None):
        key = str(request_id)
        request = self.pending.get(key)
        if not request or not self.active or request['receipt'] != self.active or self.interrupted:
            raise ValueError('Native request no longer pending')
        fingerprint = hashlib.sha256(json.dumps({'method': request['method'], 'details': request['details']}, sort_keys=True).encode()).hexdigest()
        if guard is not None and (guard.get('parent_receipt') != self.active or guard.get('request_guard') != fingerprint):
            raise ValueError('Native request changed after answer was queued')
        inputs = copy.deepcopy(request['input'])
        if answer is None or answer is False:
            response = {'behavior': 'deny', 'message': 'User declined in CodePier'}
        elif request['method'] == 'confirm' and answer is True:
            response = {'behavior': 'allow', 'updatedInput': inputs}
        elif request['method'] == 'claude/question' and isinstance(answer, dict):
            questions = request['details']['questions']
            if set(answer) != {q['id'] for q in questions}:
                raise ValueError('Answer must cover exactly the observed questions')
            values = {}
            for q in questions:
                value = answer[q['id']]
                if not isinstance(value, list) or not 1 <= len(value) <= 20 or any(
                    not isinstance(x, str) or not x.strip() or len(x) > 10000 for x in value
                ) or not q['multiSelect'] and len(value) != 1:
                    raise ValueError('Invalid question answer')
                values[q['id']] = ', '.join(value)
            inputs['answers'] = values
            response = {'behavior': 'allow', 'updatedInput': inputs}
        else:
            raise ValueError('Invalid native answer')
        self.control_reply(key, response)
        del self.pending[key]
        self.closed_requests.append(key)
        self.emit('approval', request_id=key, method=request['method'], resolved=True, resolution='answered')

    def settled(self, status='completed', text=''):
        if not self.active:
            return
        if self.interrupted:
            status = 'interrupted'
        elif self.failure:
            status = 'error'
        receipt = self.active
        self.finish(receipt, status)
        for key, request in self.pending.items():
            self.emit('approval', request_id=key, method=request['method'], resolved=True, resolution='closed')
            self.closed_requests.append(key)
        self.pending.clear()
        if self.active_command:
            self.emit('command_result', receipt=receipt, name=self.active_command, state=status, text=text)
        self.emit('done', receipt=receipt, status=status, text=text)
        self.active = self.turn = self.active_command = None

    def tool(self, block, status='start'):
        key = block.get('id') or block.get('tool_use_id')
        if not isinstance(key, str):
            return
        name = block.get('name') or self.tools.get(key, '工具')
        self.tools[key] = name
        failed = block.get('is_error') is True
        self.emit('tool', tool_id=key, name=name, status='error' if failed else status,
                  is_error=failed, text=text_content(block.get('content')) if status == 'end'
                  else json.dumps(block.get('input', {}), ensure_ascii=False))

    def stream(self, frame):
        event = frame.get('event') or {}
        kind = event.get('type')
        parent = frame.get('parent_tool_use_id') or 'main'
        if kind == 'message_start':
            mid = (event.get('message') or {}).get('id') or str(uuid.uuid4())
            stream = {'id': str(mid), 'blocks': {}, 'finalized': set(), 'finals': {}}
            self.streams[parent] = stream
            self.message_streams[parent, str(mid)] = stream
            if len(self.message_streams) > 128:
                self.message_streams.popitem(last=False)
            return
        stream = self.streams.get(parent)
        if not stream:
            return
        index = event.get('index', 0)
        key = self.block_key(parent, stream['id'], index)
        if kind == 'content_block_start':
            block = copy.deepcopy(event.get('content_block') or {})
            stream['blocks'][index] = block
            if block.get('type') == 'tool_use':
                self.tool(block)
        elif kind == 'content_block_delta':
            delta = event.get('delta') or {}
            if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
                self.had_text = True
                self.emit('delta', item_id=key, text=delta['text'])
            elif delta.get('type') == 'thinking_delta' and isinstance(delta.get('thinking'), str):
                self.thoughts[key] = self.thoughts.get(key, '') + delta['thinking']
                self.emit('reasoning', item_id=key, text=delta['thinking'])
            elif delta.get('type') == 'input_json_delta':
                block = stream['blocks'].get(index, {})
                block['_json'] = block.get('_json', '') + str(delta.get('partial_json', ''))
        elif kind == 'content_block_stop':
            block = stream['blocks'].get(index, {})
            if block.get('type') == 'thinking' and key in self.thoughts:
                self.emit('reasoning', item_id=key, text='', status='end')
            if block.get('type') == 'tool_use' and block.get('_json'):
                try:
                    block['input'] = json.loads(block.pop('_json'))
                except ValueError:
                    return
                self.tool(block)

    @staticmethod
    def block_key(parent, mid, index):
        return (parent + ':' if parent != 'main' else '') + str(mid) + ':' + str(index)

    def assistant(self, frame):
        message = frame.get('message') or {}
        parent = frame.get('parent_tool_use_id') or 'main'
        mid = message.get('id') or self.streams.get(parent, {}).get('id') or frame.get('uuid') or str(uuid.uuid4())
        content = message.get('content', [])
        stream = self.message_streams.get((parent, str(mid)))
        identity = frame.get('uuid')
        known = stream['finals'].get(identity, {}) if stream and identity else {}
        resolved = {}
        for offset, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            index = known.get(offset)
            if stream and index is None:
                # Claude emits one assistant frame per completed block, before
                # content_block_stop. Its content[0] is not stream block zero.
                candidates = [i for i, value in stream['blocks'].items()
                              if value.get('type') == block.get('type')
                              and (block.get('type') != 'tool_use' or value.get('id') == block.get('id'))]
                if len(content) > 1 and offset in candidates:
                    index = offset  # Also accept accumulated message snapshots.
                else:
                    index = next((i for i in candidates if i not in stream['finalized']), None)
            if index is not None:
                key = self.block_key(parent, mid, index)
                stream['finalized'].add(index)
                resolved[offset] = index
            else:
                # Subagent/final-only frames have no partial stream. Their UUID
                # distinguishes separate blocks sharing the same API message ID.
                key = self.block_key(parent, identity or mid, offset)
            if block.get('type') == 'text' and isinstance(block.get('text'), str):
                self.had_text = True
                # Final content replaces the matching partial block, not appends.
                self.emit('message', item_id=key, block_index=index, text=block['text'])
            elif block.get('type') == 'thinking' and isinstance(block.get('thinking'), str):
                previous = self.thoughts.get(key, '')
                text = block['thinking']
                if text.startswith(previous) and len(text) > len(previous):
                    self.emit('reasoning', item_id=key, text=text[len(previous):])
                self.thoughts[key] = text
            elif block.get('type') == 'tool_use':
                self.tool(block)
        if stream and identity:
            stream['finals'][identity] = resolved
        if frame.get('error'):
            self.failure = str(frame['error'])
            self.emit('error', text=self.failure)

    def result(self, frame):
        identity = frame.get('uuid')
        if identity and identity in self.closed_results:
            return
        if identity:
            self.closed_results.append(identity)
        self.observe_session(frame)
        usage = frame.get('usage') or {}
        tokens = {name: usage.get(key, 0) for name, key in (
            ('input', 'input_tokens'), ('output', 'output_tokens'),
            ('cacheRead', 'cache_read_input_tokens'), ('cacheWrite', 'cache_creation_input_tokens'))
                  if type(usage.get(key, 0)) in (int, float)}
        tokens['total'] = sum(tokens.values())
        self.stats = {'tokens': tokens, 'toolCalls': len(self.tools)}
        if type(frame.get('total_cost_usd')) in (int, float):
            self.stats['cost'] = frame['total_cost_usd']
        self.emit('stats', stats=self.stats)
        failed = frame.get('is_error') is True or frame.get('subtype', 'success') != 'success'
        errors = frame.get('errors', [])
        text = '; '.join(x for x in errors if isinstance(x, str)) if isinstance(errors, list) else ''
        if failed:
            text = text or text_content(frame.get('result')) or str(frame.get('subtype', 'Claude request failed'))
            self.emit('error', text=text)
        elif self.active and not self.had_text and isinstance(frame.get('result'), str):
            self.emit('message', item_id='result', text=frame['result'])
        self.settled('error' if failed else 'completed', text)

    def receive(self, message):
        kind = message.get('type')
        if kind == 'control_response' or message.get('id') in self.calls:
            response = message.get('response', {}) if kind == 'control_response' else {'request_id': message.get('id'), 'subtype': 'error'}
            key = response.get('request_id')
            call = self.calls.pop(key, None)
            self.operation_deadlines.pop(key, None)
            if not call:
                return
            method, receipt = call
            ok = response.get('subtype') == 'success'
            data = response.get('response') or {}
            if method == 'initialize':
                if not ok:
                    raise ValueError('Claude 初始化失败，请检查节点上的 CLI 安装与登录')
                self.settings = catalog(data, self.row['cwd'], self.next_settings.get('model', ''))
                self.settings.update({k: v for k, v in self.next_settings.items() if k == 'effort'})
                self.ready = True
                self.publish_settings()
                self.emit('commands', commands=self.settings.get('commands', []))
            elif method == 'set_model':
                self.settings_busy = False
                if ok:
                    self.next_settings.update(self.selection)
                    self.settings['model'] = self.selection['model'] or None
                    self.publish_settings()
                else:
                    self.emit('error', receipt=receipt, text='Claude 未确认模型切换，请重试或在节点本机核查')
                if receipt:
                    self.finish(receipt, 'completed' if ok else 'error')
                self.settings_receipt = None
            elif method == 'interrupt':
                self.settings_busy = False
                if not ok: self.interrupted = False
                if receipt:
                    self.finish(receipt, 'completed' if ok else 'error')
                if not ok:
                    self.emit('error', text='Claude 未确认中断，可停止会话进程')
            return
        if kind == 'control_request':
            self.permission(message)
        elif kind == 'control_cancel_request':
            key = message.get('request_id')
            request = self.pending.pop(key, None)
            if request:
                self.closed_requests.append(key)
                self.emit('approval', request_id=key, method=request['method'], resolved=True, resolution='closed')
        elif kind == 'system' and message.get('subtype') == 'init':
            self.observe_session(message)
            model = message.get('model')
            if isinstance(model, str) and not self.next_settings.get('model'):
                self.settings['model'] = model
                self.publish_settings()
        elif kind == 'result':
            self.result(message)
        elif self.active and kind == 'stream_event':
            self.stream(message)
        elif self.active and kind == 'assistant':
            self.assistant(message)
        elif self.active and kind == 'user':
            # Native tool results are not a second user message or an image echo.
            content = (message.get('message') or {}).get('content', [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'tool_result':
                        self.tool(block, 'end')
        elif kind == 'error':
            self.failure = str(message.get('error') or message.get('message') or 'Claude error')
            self.emit('error', text=self.failure)
