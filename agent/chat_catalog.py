"""Bounded, inference-free native discovery. Never return native connection config."""
import copy
import contextlib
import json
import os
import selectors
import signal
import subprocess
import threading
import time

CAPABILITIES = dict(steer=True, compact=True, commands=True, stats=True, auto_compaction=False, auto_retry=False)
COMMANDS = {'pi': {'refresh', 'compact', 'abort_retry', 'set_auto_compaction', 'set_auto_retry'},
            'codex': {'refresh', 'compact'}, 'claude': {'refresh', 'compact'}}


def public_model(model):
    if not isinstance(model, dict):
        return model if isinstance(model, str) else None
    strings = {'id', 'name', 'displayName', 'model', 'provider', 'defaultReasoningEffort'}
    out = {k: v[:2000] for k, v in model.items() if k in strings and isinstance(v, str)}
    for k in ('reasoning', 'contextWindow', 'maxTokens', 'configured', 'isDefault'):
        if type(model.get(k)) in (bool, int): out[k] = model[k]
    if isinstance(model.get('input'), list): out['input'] = [v for v in model['input'] if v in ('text', 'image')]
    if isinstance(model.get('supportedReasoningEfforts'), list):
        out['supportedReasoningEfforts'] = [{k: v[:1000] for k, v in x.items() if k in ('reasoningEffort', 'description') and isinstance(v, str)} for x in model['supportedReasoningEfforts'][:20] if isinstance(x, dict)]
    return out


def commands(data, cli):
    rows = data.get('commands', []) if cli != 'codex' else [s for group in data.get('data', []) for s in group.get('skills', [])]
    return [dict(name=str(x['name'])[:200], description=str(x.get('description', ''))[:2000],
                 source=str(x.get('source', 'native'))[:80] if cli != 'codex' else 'skill',
                 **({'invocation': '$' + str(x['name'])[:200]} if cli == 'codex' else {}))
            for x in rows[:500] if isinstance(x, dict) and isinstance(x.get('name'), str)]


def validate_settings(payload, cli):
    out = {}
    for key in ('model', 'effort'):
        if key not in payload: continue
        value = payload[key]
        if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid native setting')
        if key == 'effort' and value and value not in ({'off','minimal','low','medium','high','xhigh','max'} if cli == 'pi' else {'low','medium','high','xhigh','max'} if cli == 'claude' else {'none','minimal','low','medium','high','xhigh'}):
            raise ValueError('Unsupported native effort')
        if key == 'model' and value and cli == 'pi' and ('/' not in value or not all(value.split('/', 1))):
            raise ValueError('Pi model must be provider/id')
        out[key] = value
    return out


class Probe:
    def __init__(self, argv, cwd, env, timeout=8):
        self.deadline = time.monotonic() + timeout
        self.child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
        self.selector = selectors.DefaultSelector()
        os.set_blocking(self.child.stdout.fileno(), False)
        os.set_blocking(self.child.stdin.fileno(), False)
        self.selector.register(self.child.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.serial = 0
        self.total = 0
    def send(self, value):
        raw = (json.dumps(value) + '\n').encode()
        while raw:
            if time.monotonic() >= self.deadline: raise TimeoutError('Native catalog probe timed out')
            try: raw = raw[os.write(self.child.stdin.fileno(), raw):]
            except BlockingIOError: time.sleep(.005)
    def call(self, method, params=None, pi=False, timeout=None, claude=False):
        deadline=min(self.deadline,time.monotonic()+timeout) if timeout is not None else self.deadline
        self.serial += 1
        key = str(self.serial)
        if claude:
            self.send({'type': 'control_request', 'request_id': key, 'request': {'subtype': method, **(params or {})}})
        else:
            self.send({'id': key, **({'type': method, **(params or {})} if pi else {'method': method, 'params': params or {}})})
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n'); self.buffer[:] = rest
                m = json.loads(line)
                if claude:
                    response = m.get('response') or {}
                    if m.get('type') != 'control_response' or response.get('request_id') != key: continue
                    if response.get('subtype') != 'success': raise ValueError(method + ' unavailable in installed Claude version')
                    return response.get('response') or {}
                if m.get('id') != key: continue
                if m.get('error') or pi and not m.get('success'): raise ValueError(method + ' unavailable in installed native version')
                return m.get('data' if pi else 'result') or {}
            if not self.selector.select(min(.1, max(0, deadline-time.monotonic()))): continue
            raw = os.read(self.child.stdout.fileno(), 65536)
            if not raw: raise ValueError('Native CLI exited before catalog response; check local installation/login')
            self.buffer.extend(raw); self.total += len(raw)
            if len(self.buffer) > 2*1024*1024 or self.total > 8*1024*1024: raise ValueError('Native catalog exceeds safe limit')
        raise TimeoutError('Native catalog probe timed out')
    def close(self):
        if self.child.poll() is None:
            with contextlib.suppress(ProcessLookupError): os.killpg(self.child.pid, signal.SIGTERM)
            try: self.child.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError): os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=1)
        self.selector.close(); self.child.stdin.close(); self.child.stdout.close()


def probe(cli, executable, cwd, env, model='', timeout=8, include_commands=True):
    validate_settings({'model': model}, cli)
    if cli == 'claude':
        from agent.claude_cli import probe as claude_probe
        return claude_probe(executable, cwd, env, model, timeout, include_commands)
    result = dict(cli=cli, cwd=str(cwd), models=[], model=None, thinking_levels=[], commands=[], warnings=[], capabilities=dict(CAPABILITIES))
    if not include_commands: result.pop('commands')
    p = Probe([executable, '--mode', 'rpc', '--no-session'] if cli == 'pi' else [executable, 'app-server'], cwd, env, timeout)
    def call(method, params=None, optional=False):
        try: return p.call(method, params, cli == 'pi',timeout=1.5 if optional else None)
        except (ValueError,TimeoutError) as exc:
            if not optional: raise
            result['warnings'].append(str(exc)); return {}
    try:
        if cli == 'pi':
            result['warnings'].append('Installed Pi auto-compaction/retry toggles persist globally; session-only toggles unavailable')
            state = call('get_state')
            result.update({k: state[k] for k in ('thinkingLevel',) if k in state})
            result['model'] = public_model(state.get('model'))
            result['models'] = [public_model(x) for x in call('get_available_models').get('models', [])]
            if model:
                provider, mid = model.split('/', 1)
                result['model'] = public_model(call('set_model', {'provider': provider, 'modelId': mid}))
                selected_state=call('get_state')
                if isinstance(selected_state.get('thinkingLevel'),str): result['thinkingLevel']=selected_state['thinkingLevel']
            result['thinking_levels'] = [x for x in call('get_available_thinking_levels', optional=True).get('levels', []) if isinstance(x,str)][:20]
            if include_commands: result['commands'] = commands(call('get_commands', optional=True), cli)
        else:
            call('initialize', {'clientInfo': {'name': 'codepier-catalog', 'version': '1.0.0'}})
            p.send({'method': 'initialized', 'params': {}})
            cursor = None
            for _ in range(20):
                page = call('model/list', {'cursor': cursor} if cursor else {})
                result['models'].extend(public_model(x) for x in page.get('data', []))
                cursor = page.get('nextCursor')
                if not cursor: break
            else: result['warnings'].append('Native model catalog pagination limit reached')
            config = call('config/read', {'cwd': str(cwd), 'includeLayers': False}, optional=True).get('config', {})
            current = config.get('model')
            if isinstance(current, str) and not any(current in (x.get('id'), x.get('model')) for x in result['models']):
                result['models'].append(dict(id=current, model=current, displayName=current+' (configured)', configured=True))
            current = current or next((x.get('model') or x.get('id') for x in result['models'] if x.get('isDefault')),None)
            selected = model or current
            result['model'] = next((x for x in result['models'] if selected in (x.get('id'), x.get('model'))), selected)
            effort=config.get('model_reasoning_effort')
            result['reasoningEffort'] = effort if isinstance(effort,str) else None
            if isinstance(result['model'], dict): result['thinking_levels'] = [x['reasoningEffort'] for x in result['model'].get('supportedReasoningEfforts', []) if 'reasoningEffort' in x]
            if include_commands: result['commands'] = commands(call('skills/list', {'cwds': [str(cwd)]}, optional=True), cli)
        return result
    finally: p.close()


class CatalogCache:
    def __init__(self, ttl=300):
        self.ttl = ttl
        self.lock = threading.Lock(); self.entries = {}; self.pending = {}
    def get(self, key, loader, refresh=False):
        with self.lock:
            entry = self.entries.get(key)
            if not refresh and entry and entry[0] > time.monotonic(): return copy.deepcopy(entry[1])
            # Refresh bypasses a completed entry, but joins an existing probe.
            event = self.pending.get(key)
            if event is None:
                event = threading.Event(); self.pending[key] = event; owner = True
            else: owner = False
        if not owner:
            if not event.wait(12): raise TimeoutError('Native catalog coalesced probe timed out')
            if hasattr(event, 'error'): raise ValueError(event.error)
            return copy.deepcopy(event.result)
        try:
            value = loader(); event.result = value
            with self.lock:
                if len(self.entries) >= 64: self.entries.pop(next(iter(self.entries)))
                self.entries[key] = (time.monotonic()+self.ttl, value)
            return copy.deepcopy(value)
        except Exception as exc:
            event.error = str(exc); raise
        finally:
            with self.lock:
                if self.pending.get(key) is event: self.pending.pop(key)
                event.set()
