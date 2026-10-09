"""Token estimate presentation never turns missing telemetry into billing data."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = r"""
const fs = require('fs'), vm = require('vm');
const context = { window: {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/token-usage.js', 'utf8'), context);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const api = context.window.CodePierTokenUsage;
let result;
if (input.method === 'merge') result = api.mergeActivities(...input.args);
else result = api[input.method](...input.args);
process.stdout.write(JSON.stringify(result));
"""


def run_js(method, *args):
    result = subprocess.run(
        ['node', '-e', NODE], input=json.dumps({'method': method, 'args': args}),
        text=True, capture_output=True, cwd=ROOT, timeout=15, check=True,
    )
    return json.loads(result.stdout)


def metric(n=5, **changes):
    return {'state': 'available', 'estimated_tokens': n, 'low': n // 2,
            'high': n * 2, 'characters': n * 3, 'utf8_bytes': n * 5,
            'excluded_fields': 0, 'truncated': False, **changes}


def usage(**changes):
    return {'version': 'codepier-text-v1', 'kind': 'estimate',
            'scope': 'project_tool_payload', 'input': metric(5),
            'output': metric(10), 'actual_usage': None, **changes}


def row(identifier=1, **changes):
    return {'id': identifier, 'operation_id': 'same-server-operation',
            'token_usage': usage(), **changes}


@pytest.mark.parametrize('rows', [[], [row(token_usage=None)], [{'id': 1}]])
def test_missing_history_stays_unavailable(rows):
    result = run_js('summarize', rows)
    assert result['actual_usage'] is None
    for direction in ('input', 'output'):
        assert result[direction]['estimated_tokens'] is None
        assert result[direction]['characters'] is None
        assert result[direction]['measured_attempts'] == 0
    assert '未记录' in run_js('summary', rows)


def test_observed_zero_is_not_missing():
    zero = usage(input=metric(0), output=metric(0))
    result = run_js('summarize', [row(token_usage=zero)])
    assert result['input']['estimated_tokens'] == 0
    assert result['output']['estimated_tokens'] == 0
    assert '约 0' in run_js('cell', zero, 'input')


def test_input_output_and_included_unicode_byte_counts_remain_separate():
    value = usage(input=metric(3, characters=4, utf8_bytes=12),
                  output=metric(7, characters=10, utf8_bytes=24))
    result = run_js('summarize', [row(token_usage=value)])
    assert result['input']['estimated_tokens'] == 3
    assert result['input']['characters'] == 4
    assert result['input']['utf8_bytes'] == 12
    assert result['output']['estimated_tokens'] == 7
    assert result['output']['characters'] == 10
    assert result['output']['utf8_bytes'] == 24


@pytest.mark.parametrize('change', [{'state': 'partial'}, {'truncated': True}])
def test_partial_projection_is_visible(change):
    value = usage(input=metric(**change))
    result = run_js('summarize', [row(token_usage=value)])
    assert result['input']['partial_attempts'] == 1
    assert '部分文本' in run_js('cell', value, 'input')
    assert '部分文本' in run_js('summary', [row(token_usage=value)])


def test_source_tool_truncation_is_separate_from_estimator_budget():
    value = usage(output=metric(9, source_truncated=True))
    result = run_js('summarize', [row(token_usage=value)])
    assert result['output']['source_truncated_attempts'] == 1
    assert result['output']['partial_attempts'] == 0
    assert result['input']['source_truncated_attempts'] == 0
    assert '源工具已截断' in run_js('cell', value, 'output')
    assert '1 次源工具已截断' in run_js('summary', [row(token_usage=value)])


def test_distinct_attempts_for_same_operation_keep_each_returned_payload():
    result = run_js('summarize', [row(1), row(2)])
    assert result['wire_attempts'] == 2
    assert result['distinct_server_operation_ids'] == 1
    assert result['input']['estimated_tokens'] == 10
    assert result['output']['estimated_tokens'] == 20


def test_repeated_pagination_row_is_not_counted_twice_and_fresh_row_replaces_old():
    fresh = row(1, token_usage=usage(output=metric(20)))
    merged = run_js('merge', [row(1), row(2)], [fresh, row(3)])
    assert [item['id'] for item in merged] == [1, 2, 3]
    result = run_js('summarize', merged)
    assert result['wire_attempts'] == 3
    assert result['output']['estimated_tokens'] == 40


@pytest.mark.parametrize('changes', [
    {'version': 'unrecognized'},
    {'kind': 'actual'},
    {'scope': 'whole_host_conversation'},
])
def test_unrecognized_contract_cannot_be_presented_as_estimate(changes):
    value = usage(**changes, actual_usage={'total_tokens': 999999})
    result = run_js('summarize', [row(token_usage=value)])
    assert result['input']['estimated_tokens'] is None
    assert result['actual_usage'] is None
    assert '999999' not in run_js('summary', [row(token_usage=value)])


@pytest.mark.parametrize('invalid', [-1, 0.5, '123', '<img src=x onerror=alert(1)>', 2**53])
def test_invalid_counter_is_not_rendered(invalid):
    value = usage(input=metric(estimated_tokens=invalid))
    html = run_js('cell', value, 'input')
    assert html == '<span class="muted">未记录</span>'
    assert '<img' not in html


def test_invalid_interval_is_unavailable():
    value = usage(input=metric(5, low=9, high=10))
    assert '未记录' in run_js('cell', value, 'input')


def test_summary_bounds_and_overflow_fail_closed():
    result = run_js('summarize', [row(i) for i in range(1, 10002)])
    assert result['wire_attempts'] == 10000
    large = usage(input=metric(2**52, high=2**52, characters=1, utf8_bytes=1))
    overflow = run_js('summarize', [row(1, token_usage=large), row(2, token_usage=large)])
    assert overflow['input']['overflow'] is True
    assert overflow['input']['estimated_tokens'] is None


def test_summary_labels_scope_version_exclusions_and_non_billing():
    html = run_js('summary', [row()])
    for text in ('当前已载入', '非账单', '项目工具文本', '不是整段宿主对话',
                 '图片与文件字节', '历史缺失不会补算', '不是统计置信区间', 'codepier-text-v1'):
        assert text in html


def test_activity_view_checks_scope_before_render_and_releases_loading_guard():
    source = (ROOT / 'web/integrations.js').read_text()
    start = source.index('let before = null')
    end = source.index("section('#i-activity', activities)", start)
    body = source[start:end]
    assert body.index('if (!r || !valid()) return;') < body.index('usage.mergeActivities(')
    assert 'loadingActivities = true' in body
    assert 'finally {' in body and 'loadingActivities = false' in body
    assert "usage.cell(a.token_usage, 'input')" in body
    assert "usage.cell(a.token_usage, 'output')" in body
    assert 'colspan="7"' in body


def dashboard_payload(rows=None, **changes):
    value = run_js('summarize', [row(started=1000)] if rows is None else rows)
    return {'summary': value, 'period': {'key': 'today', 'start': 0, 'end': 3600,
            'timezone': 'Asia/Shanghai', 'bucket': 'hour'},
            'options': {'projects': [], 'connections': [], 'sessions': []},
            'filters': {}, 'coverage': {'activity_row_limit': 10000,
            'estimate_retention_days': 30}, 'trend': [], **changes}


def test_dashboard_in_workspace_card_labels_scope_missing_and_actual():
    html = run_js('dashboard', dashboard_payload(rows=[row(token_usage=None)]))
    assert '今日工具 Token' in html
    assert '<span class="token-estimate-badge">估算</span>' in html
    assert '<span>输入</span>' in html and '<span>输出</span>' in html
    assert '实际模型用量' in html and '未提供' in html
    assert '未记录' in html and '约 0' not in html
    assert 'Asia/Shanghai' in html and '当前保留的授权记录' in html
    assert '二进制文件/图片字节不计入' in html and '不是完整对话账单' in html
    assert '全部授权连接' in html and '全部匿名窗口' in html


def test_dashboard_escapes_labels_and_does_not_invent_missing_buckets():
    point = run_js('summarize', [row()])
    missing = run_js('summarize', [row(token_usage=None)])
    data = dashboard_payload(trend=[{'started': 0, **point}, {'started': 7200, **missing}],
        options={'projects': [{'id': '\" onclick=\"bad', 'label': '<img src=x onerror=bad>'}]})
    html = run_js('dashboard', data)
    assert '<img src=x' not in html and '&lt;img' in html
    assert html.count('<li>') == 2
    assert '未记录' in html and '缺失不补零' in html
    assert '实际模型用量未提供' in html


def test_loaded_details_explicitly_display_range_and_actual_unavailable():
    html = run_js('summary', [row(started=1700000000), row(2, started=1700003600)])
    assert 'UTC' in html and '只汇总已加载明细' in html
    assert '实际模型用量：未提供' in html


def test_dashboard_mounts_inside_existing_workspace_overview():
    source = (ROOT / 'web/app.js').read_text()
    assert 'CodePierTokenUsage?.dashboard(o.token_usage)' in source
    assert 'CodePierTokenUsage?.bindDashboard' in source
    css = (ROOT / 'web/token-usage.css').read_text()
    assert '@media (max-width: 600px)' in css


def test_dashboard_refresh_state_is_scoped_and_late_results_are_discarded():
    script = r"""
const fs = require('fs'), vm = require('vm');
const context = {window: {}, URLSearchParams};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/token-usage.js', 'utf8'), context);
const api = context.window.CodePierTokenUsage;
const original = JSON.parse(fs.readFileSync(0, 'utf8'));
const picked = {...original, period: {...original.period, key: '7d'}, filters: {project: 'p'}};
(async () => {
  const calls = [];
  await api.prepareDashboard(picked, async url => {calls.push(url); return picked;}, false, () => true);
  await api.prepareDashboard(original, async url => {calls.push(url); return picked;}, true, () => true);
  const preserved = calls.length === 2 && calls[1].includes('period=7d') && calls[1].includes('project=p');
  const different = {...original, authority_key: 'other-current-authority'};
  await api.prepareDashboard(different, async () => {throw new Error('must reset scope');}, true, () => true);
  api.resetDashboard();
  await api.prepareDashboard(original, async () => {throw new Error('must reset route');}, true, () => true);
  let release;
  const pending = api.prepareDashboard(picked, () => new Promise(resolve => {release = resolve;}), false, () => true);
  api.resetDashboard();
  release(picked);
  const late = await pending;
  process.stdout.write(JSON.stringify({preserved, late}));
})().catch(error => {console.error(error); process.exit(1);});
"""
    payload = dashboard_payload(authority_key='same-current-authority')
    result = subprocess.run(['node', '-e', script], input=json.dumps(payload),
        text=True, capture_output=True, cwd=ROOT, timeout=15, check=True)
    assert json.loads(result.stdout) == {'preserved': True, 'late': None}


def test_dashboard_compact_summary_keeps_details_and_missing_states():
    html = run_js('dashboard', dashboard_payload())
    visible = html.split('<details data-token-filters')[0]
    assert visible.count('class="token-dashboard-metric"') == 2
    assert '实际模型用量' not in visible
    assert '次有记录' not in visible and '次未记录' not in visible
    assert '统计说明' in html
    assert '1 次记录' in html
    assert 'codepier-text-v1' in html and '范围不是统计置信区间' in html
    partial = run_js('dashboard', dashboard_payload(rows=[
        row(token_usage=usage(input=metric(state='partial'))), row(2, token_usage=None)]))
    partial_visible = partial.split('<details data-token-filters')[0]
    assert '1 次未记录' in partial_visible and '部分文本' in partial_visible
    empty = run_js('dashboard', dashboard_payload(rows=[]))
    assert '未记录' in empty.split('<details data-token-filters')[0]
    assert '约 0' not in empty
