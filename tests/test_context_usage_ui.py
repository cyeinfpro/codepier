"""New context contract renders compactly without replacing wire detail semantics."""
import json
from pathlib import Path
import subprocess

import pytest

from tests.context_usage_fixtures import synthetic_dashboard

ROOT = Path(__file__).resolve().parents[1]
NODE = """
const fs = require('fs'), vm = require('vm');
const context = {window:{}, URLSearchParams};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/token-usage.js', 'utf8'), context);
const input=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(context.window.CodePierTokenUsage[input.op](input.value)));
"""


def render(data, operation="dashboard"):
    result = subprocess.run(["node", "-e", NODE], cwd=ROOT,
                            input=json.dumps({"op": operation, "value": data}),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_default_context_headline_range_and_folded_wire_details():
    data = synthetic_dashboard()
    html = render(data)
    assert "含上下文 Token" in html
    assert "206.49K" in html
    assert "范围 131.85K–363.3K" in html
    assert "GPT-6 Astra · 输入缓存 90% 假设" in html
    assert "历史可能不完整" in html
    assert "<strong>工具文本 Token</strong>：约 11K" in html
    assert "模型输入（含上下文）" in html
    assert 'data-token-filters><summary>查看详情' in html
    assert "未包含完整对话上下文" not in html


def test_actual_usage_injection_is_never_rendered_as_context():
    data = synthetic_dashboard()
    data["summary"]["actual_usage"] = {"total_tokens": 987654321}
    data["summary"]["context_estimate"]["actual_usage"] = {"total_tokens": 987654321}
    html = render(data)
    assert "987654321" not in html
    assert "实际模型用量未提供" in html
    assert "不是统计置信区间" in html


@pytest.mark.parametrize("field,value", [
    ("kind", "actual"), ("version", "future-context"), ("scope", "whole-host-bill"),
])
def test_unrecognized_context_falls_back_to_explicit_wire_label(field, value):
    data = synthetic_dashboard()
    data["summary"]["context_estimate"][field] = value
    html = render(data)
    assert "<span>工具文本 Token</span>" in html
    assert "<span>含上下文 Token</span>" not in html


def test_missing_context_never_turns_into_zero_or_wire_context():
    data = synthetic_dashboard()
    value = data["summary"]["context_estimate"]
    value["state"] = "unavailable"
    for field in ("estimated_tokens", "low", "high"):
        value["total"][field] = None
    value["reference_cost"]["amount_nano_usd"] = None
    html = render(data)
    assert "<span>含上下文 Token</span>" in html
    assert "暂无法估算上下文" in html
    assert "约 0" not in html


def test_context_export_keeps_kind_scope_and_wire_breakdown_separate():
    data = synthetic_dashboard()
    export = json.loads(render(data, "exportJSON"))
    assert export["kind"] == "mcp_context_scenario_estimate"
    assert export["schema_version"] == 3
    assert export["summary"]["context_estimate"]["reference_cost"]["scope"] == "context_scenario_equivalent"
    assert export["summary"]["reference_cost"]["scope"] == "visible_tool_text_equivalent"
    assert "contributions" not in json.dumps(export)


def test_context_cost_cannot_borrow_incompatible_wire_cost_scope():
    data = synthetic_dashboard()
    data["summary"]["context_estimate"]["reference_cost"]["scope"] = "visible_tool_text_equivalent"
    html = render(data)
    assert '<span>参考费用 · USD（非账单）</span><strong>未记录</strong>' in html
