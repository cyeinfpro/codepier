"""The new headline, trends and export share the calibrated scope."""
import json

import pytest

from tests.context_usage_fixtures import synthetic_dashboard
from tests.test_context_usage_ui import render


def dashboard(**kw):
    return synthetic_dashboard(calibrated=True, **kw)


def test_calibrated_headline_labels_rounds_and_preserves_separate_comparisons():
    data = dashboard()
    html = render(data)
    headline = html.split("<details data-token-filters", 1)[0]
    assert "按调用估算 Token" in headline
    assert "1.5M" in headline and "$2.43" in headline
    assert "10 次工具调用" in headline and "11 个模型轮次" in headline
    assert "95% 上限假设" in headline
    assert "工具文本 Token</strong>：约 11K" in html
    assert "上下文重放参考</strong>：约 206.49K" in html
    assert "推断模型轮次" in html
    assert "真实 API 次数" in html
    assert "训练样本 23,310" in html
    assert "本轮新消费的工具文本至少按普通输入计价" in html


def test_trend_and_export_use_same_estimate_without_discarding_old_fields():
    data = dashboard()
    trend = render(data, "trendHTML")
    assert "1.49M" in trend and "5.36K" in trend
    exported = json.loads(render(data, "exportJSON"))
    assert exported["kind"] == "mcp_call_calibration_estimate"
    assert exported["schema_version"] == 4
    for field in ("call_estimate", "context_estimate", "reference_cost", "input", "output"):
        assert field in exported["summary"]
    assert "contributions" not in json.dumps(exported)
    assert exported["summary"]["call_estimate"]["actual_usage"] is None


@pytest.mark.parametrize("field,value", [
    ("kind", "actual"), ("version", "future"), ("scope", "provider-bill")])
def test_unknown_call_contract_falls_back_to_compatible_context(field, value):
    data = dashboard()
    data["summary"]["call_estimate"][field] = value
    html = render(data).split("<details data-token-filters", 1)[0]
    assert "含上下文 Token" in html
    assert "206.49K" in html
    assert "按调用估算 Token" not in html


def test_incompatible_call_cost_and_injected_actual_usage_are_not_displayed():
    data = dashboard()
    data["summary"]["call_estimate"]["reference_cost"]["scope"] = "visible_tool_text_equivalent"
    data["summary"]["call_estimate"]["actual_usage"] = {"total_tokens": 987654321}
    headline = render(data).split("<details data-token-filters", 1)[0]
    assert "<strong>未记录</strong>" in headline
    assert "987654321" not in headline
