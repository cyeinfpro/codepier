"""Claude Code launch and inference-free, allowlisted initialization metadata.

The transport matches Anthropic's claude-agent-sdk-python control protocol;
no SDK dependency, API credentials or model requests are needed for discovery.
"""
from __future__ import annotations

import uuid

from agent.chat_catalog import CAPABILITIES, Probe, commands, public_model, validate_settings

CAPS = dict(CAPABILITIES, steer=False, effort_runtime=False)
EFFORTS = frozenset({'low', 'medium', 'high', 'xhigh', 'max'})


def launch(executable, settings=None, resume='', catalog=False):
    settings = validate_settings(settings or {}, 'claude')
    argv = [executable, '--print', '--verbose', '--input-format', 'stream-json',
            '--output-format', 'stream-json', '--include-partial-messages',
            '--permission-prompt-tool', 'stdio']
    # Keep the node's own auth/settings and permission policy. Never inject a
    # permission bypass, an allow-all rule, or a different native HOME.
    for name in ('model', 'effort'):
        if settings.get(name):
            argv.extend(['--' + name, settings[name]])
    if resume:
        argv.extend(['--resume', str(uuid.UUID(resume))])
    if catalog:
        argv.append('--no-session-persistence')
    return argv


def catalog(data, cwd, selected='', include_commands=True):
    """Do not copy account, environment, MCP connection or arbitrary config data."""
    rows = data.get('models', [])
    models = []
    if not isinstance(rows, list):
        rows = []
    for row in rows[:200]:
        if not isinstance(row, dict) or not isinstance(row.get('value'), str):
            continue
        mid = row['value']
        if not mid or len(mid) > 200 or any(ord(c) < 32 for c in mid):
            continue
        levels = row.get('supportedEffortLevels', [])
        model = {'id': mid, 'displayName': row.get('displayName', mid),
                 'supportedReasoningEfforts': [dict(reasoningEffort=x) for x in levels
                                               if isinstance(x, str) and x in EFFORTS]
                 if isinstance(levels, list) else [],
                 'reasoning': row.get('supportsEffort') is True,
                 'isDefault': mid == 'default'}
        models.append(public_model(model))
    state = data.get('session_state')
    state = state if isinstance(state, dict) else {}
    current = selected or state.get('model')
    if not isinstance(current, str) or len(current) > 200 or any(ord(c) < 32 for c in current):
        current = ''
    model = next((m for m in models if m['id'] == current), None)
    if current and model is None:
        # A custom configured model is legitimate even outside the picker list.
        model = dict(id=current, displayName=current, configured=True,
                     supportedReasoningEfforts=[])
        models.append(model)
    result = dict(cli='claude', cwd=str(cwd), models=models, model=model,
                  thinking_levels=[x['reasoningEffort'] for x in (model or {}).get('supportedReasoningEfforts', [])],
                  warnings=[], capabilities=dict(CAPS))
    effort = state.get('effort')
    if isinstance(effort, str) and effort in EFFORTS:
        result['effort'] = effort
    if include_commands:
        result['commands'] = commands(data, 'claude')
    return result


def probe(executable, cwd, env, model='', timeout=8, include_commands=True):
    p = Probe(launch(executable, {'model': model}, catalog=True), cwd, env, timeout)
    try:
        data = p.call('initialize', {'hooks': None}, claude=True)
        return catalog(data, cwd, model, include_commands)
    finally:
        p.close()
