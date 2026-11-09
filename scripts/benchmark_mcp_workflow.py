#!/usr/bin/env python3
"""Measure one deterministic MCP workflow on a fresh loopback Hub and Agent."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from hub.collaboration.event_contracts import definitions
from tests.support import running_stack

PARAMETER_ERRORS = {'INVALID_ARGUMENTS', 'INVALID_ARGUMENT', 'UNKNOWN_OPERATION', 'INVALID_CURSOR'}


def size(value):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return {'chars': len(text), 'bytes': len(text.encode())}


def body_chars(value):
    if not isinstance(value, dict):
        return 0
    count = 0
    for key in ('output', 'content', 'text'):
        if isinstance(value.get(key), str):
            count += len(value[key])
    if isinstance(value.get('context'), dict):
        count += size(value['context'])['chars']
    nested = value.get('result', {})
    data = nested.get('data', {}) if isinstance(nested, dict) else {}
    if isinstance(value.get('output'), str) and value['output'] == data.get('output'):
        data = {key: val for key, val in data.items() if key != 'output'}
    count += body_chars(data) if data else 0
    for key in ('operations', 'matches', 'results', 'files'):
        if isinstance(value.get(key), list):
            count += sum(body_chars(item.get('data', item)) for item in value[key] if isinstance(item, dict))
    return count


class Workflow:
    def __init__(self, stack):
        self.stack = stack
        self.turns = self.parameter_errors = self.help_before_first_success = 0
        self.valid = False
        self.polls = {}
        self.logs = {}
        self.repeated_log_chars = self.response_chars = self.response_bytes = self.body = 0

    def call(self, tool, **arguments):
        result = self.stack.mcp(tool, arguments)
        # Measure one business JSON projection. The protocol text mirror is
        # deliberately excluded. Never print or persist arguments/response data.
        value = result['structuredContent']
        measured = size(value)
        self.turns += 1
        self.response_chars += measured['chars']
        self.response_bytes += measured['bytes']
        self.body += body_chars(value)
        action = arguments['action']
        if action == 'help':
            self.help_before_first_success += int(not self.valid)
        elif not result.get('isError'):
            self.valid = True
        error = value.get('error')
        code = value.get('error_code') or (error.get('code') if isinstance(error, dict) else None)
        if code in PARAMETER_ERRORS and not value.get('operation_id'):
            self.parameter_errors += 1
        if action == 'job' and arguments.get('view', 'get') in {'get', 'wait'}:
            for identifier in arguments['ids']:
                self.polls[identifier] = self.polls.get(identifier, 0) + 1
        for row in value.get('operations', [value]):
            identifier, output = row.get('operation_id'), row.get('output')
            if identifier and isinstance(output, str):
                previous = self.logs.get(identifier, '')
                if output.startswith(previous):
                    self.repeated_log_chars += len(previous)
                self.logs[identifier] = output
        assert not result.get('isError'), f'{tool}/{action} returned {code or "tool error"}'
        return value

    def report(self):
        return {
            'tool_calls': self.turns,
            'help_before_first_success': self.help_before_first_success,
            'parameter_errors': self.parameter_errors,
            'parameter_error_rate': self.parameter_errors / self.turns,
            'polls_per_operation': sorted(self.polls.values()),
            'repeated_log_chars': self.repeated_log_chars,
            'response_chars': self.response_chars, 'response_bytes': self.response_bytes,
            'body_chars': self.body, 'non_body_chars': self.response_chars - self.body,
            'body_fraction': round(self.body / self.response_chars, 4),
        }


def scenario(stack, *, context_id=''):
    meter = Workflow(stack)
    project = 'ProjectAlpha'
    opened = meter.call('query', action='project', project=project,
                        args={'context_id': context_id} if context_id else {})
    found = meter.call('query', action='search', project=project, path='src', limit=4,
                       args={'query': 'def greeting', 'file_glob': 'src/*.py'})
    assert len(found['matches']) == 1
    read = meter.call('query', action='file', project=project,
                      args={'items': [{'path': found['matches'][0]['path']}, {'path': 'README.md'}]})
    assert len(read['files']) == 2
    executed = meter.call('act', action='exec', project=project, args={'task': 'smoke'},
                          idempotency_key='benchmark-' + uuid4().hex, wait=30)
    row = executed.get('operations', [executed])[0]
    if row.get('pending'):
        polled = meter.call('query', action='job', view='wait', ids=[row['operation_id']])
        row = polled['operations'][0]
    assert row['state'] == 'succeeded'
    assert row['output'] == 'PASS: local task completed\n'
    cursor = row['output_seq']
    incremental = meter.call('query', action='job', ids=[row['operation_id']], args={'after_output_seq': cursor})
    assert incremental['operations'][0]['output_unchanged']
    report = meter.report()
    report.update(project_open=size(opened), exec_receipt=size(executed), incremental_job=size(incremental))
    assert opened['context_unchanged'] is bool(context_id)
    return opened['context_id'], report


def directory_metrics(stack):
    init = stack.rpc('initialize', {'protocolVersion': '2025-11-25'}).json()['result']
    catalog = stack.rpc('tools/list').json()['result']
    tools = catalog['tools']
    assert not catalog.get('nextCursor')
    event_text = [item['description'] for item in definitions()]
    descriptions = [item.get('description', '') for item in tools]
    return {'tools': len(tools), 'tools_list': size(tools),
            'instructions_chars': len(init['instructions']),
            'tool_description_repeat_chars': sum(map(len, descriptions)) - sum(map(len, set(descriptions))),
            'event_description_chars': sum(map(len, event_text)),
            'event_repeat_chars_if_expanded_in_two_wrappers': sum(map(len, event_text))}


def main():
    work = Path.cwd() / '.work'
    work.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='mcp-workflow-', dir=work) as scratch, running_stack(Path(scratch)) as stack:
        catalog = directory_metrics(stack)
        context_id, cold = scenario(stack)
        _, warm = scenario(stack, context_id=context_id)
        projects = stack.mcp('query', {'action': 'project', 'view': 'list'})['structuredContent']
        print(json.dumps({'scope': 'loopback_fixture', 'measurement': 'compact_json_unicode_chars_and_utf8_bytes',
                          'actual_usage': None, 'directory': catalog, 'cold': cold, 'warm': warm,
                          'project_list': {'rows': len(projects['projects']), **size(projects)}},
                         ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
