import json

import pytest

from hub.call_log import argument_summary, public_row, trace_timing
from shared.audit_redaction import display_value, redact_command, redact_text


@pytest.mark.parametrize('value,secret', [
    ('curl -H "Authorization: Bearer hidden-value" https://example.test', 'hidden-value'),
    ('password="a b c" next=visible', 'a b c'),
    ("--api-key 'hello world' --verbose", 'hello world'),
    ('{"apiKey": "hidden-value", "ok": 1}', 'hidden-value'),
    ('https://user:p%40ss@example.test/path?token=hidden-value&safe=yes', 'hidden-value'),
    ('postgres://user:secret-pass@example.test/database', 'secret-pass'),
    ('Cookie: session=hidden-value; other=another-value', 'hidden-value'),
    ('-----BEGIN RSA ' + 'PRIVATE KEY-----\nsecret-material\n-----END RSA PRIVATE KEY-----', 'secret-material'),
    ('-----BEGIN ' + 'PRIVATE KEY-----\nsecret-material', 'secret-material'),
    ('Bearer abcdefghijklmnop', 'abcdefghijklmnop'),
    ('--client-secret=hidden-value', 'hidden-value'),
])
def test_text_redaction(value, secret):
    assert secret not in redact_text(value)


def test_env_and_unrelated_commands():
    assert 'some-value' not in redact_command('CUSTOM_ENV="some-value" pytest -q')
    assert redact_command('pytest -q tests/test_file.py') == 'pytest -q tests/test_file.py'


def test_nested_bounded_display_and_no_mutation():
    original = {'password': 'hidden-password', 'data': {'headers': {'X-Key': 'hidden-header'}, 'env': {'FOO': 'hidden-env'}, 'output': 'token=hidden-output'}}
    value, truncated, redacted = display_value(original)
    assert 'hidden-' not in json.dumps(value)
    assert redacted and not truncated
    assert original['password'] == 'hidden-password'
    value, truncated, _ = display_value({'output': 'a' * 10000}, budget=500, text_limit=100)
    assert truncated and len(json.dumps(value)) < 200


def test_scrub_before_truncating():
    value, _, _ = display_value({'command': '--password="' + 'x' * 1000 + '"'}, text_limit=20)
    assert 'xxx' not in json.dumps(value)


def test_controls_and_wide_or_deep_data():
    assert '\x1b' not in redact_text('\x1b[31mHello\x1b[0m\x00')
    value, truncated, _ = display_value(list(range(5000)))
    assert truncated and len(value) == 101
    nested = {}
    for _ in range(100):
        nested = {'next': nested}
    assert display_value(nested)[1]


def test_summary_and_legacy_counts():
    assert argument_summary({'path':'a.py','content':'<477 chars>'}) == 'path=a.py  inputChars=477'
    assert 'edits=2' in argument_summary({'edits':[{},{}]})
    assert 'commandChars' not in argument_summary({'command':'short or truncated…'})
    assert 'commandChars=2073' in argument_summary({'command':'short…','_log':{'command_chars':2073}})


def test_list_projection_does_not_mutate_or_leak():
    raw={'id':'op1','created':10.0,'updated':13.0,'state':'failed','args_summary':json.dumps({'command':'curl --token=hidden-value','env':{'CUSTOM':'hidden-env'}}),'error':'password=hidden-error','payload':'private-encrypted-payload','result':'private-file-body'}
    shown=public_row(raw,20)
    assert shown['elapsed_ms']==3000
    assert 'hidden-' not in json.dumps(shown)
    assert 'private-' not in json.dumps(shown)
    assert 'args_summary' not in shown
    assert 'hidden-value' in raw['args_summary']
    raw['state']='running'
    assert public_row(raw,20)['elapsed_ms']==10000


def event(stage,seq,ms,at=20,source='agent'):
    return {'stage':stage,'seq':seq,'elapsed_ms':ms,'source':source,'at':at}


def test_timing_uses_monotonic_agent_offsets_not_wall_clock():
    trace={'events':[event('accepted',1,0),event('executing',4,1000,21),event('persisting',5,1250,30)]}
    assert trace_timing(trace,10)=={'wait_ms':11000,'execution_ms':250}


def test_missing_and_retried_timings_are_not_fabricated():
    assert trace_timing({'events':[]},10)=={'wait_ms':None,'execution_ms':None}
    trace={'events':[event('executing',4,1000),event('persisting',5,1250)]}
    assert trace_timing(trace,10)['execution_ms'] is None
    trace={'events':[event('accepted',1,0),event('executing',4,1000),event('accepted',1,0),event('persisting',5,50)]}
    assert trace_timing(trace,10)['execution_ms'] is None
    trace={'events':[event('accepted',1,0),event('executing',4,1000),event('persisting',5,900)]}
    assert trace_timing(trace,10)['execution_ms'] is None
