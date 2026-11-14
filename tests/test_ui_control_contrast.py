"""Editable field boundaries remain identifiable in both color schemes."""
import re
import pytest
from pathlib import Path


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
    return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


@pytest.mark.parametrize("asset,count", [("tokens.css", 3), ("browser-extension/popup.css", 2)])
def test_control_border_contrast_against_editable_field_backgrounds(asset, count):
    css = (Path(__file__).resolve().parents[1] / 'web' / asset).read_text()
    # Root, explicit dark and OS-dark fallback must all meet the same contract.
    blocks = re.findall(r'\{([^{}]*--ui-control-border:[^{}]*)\}', css)
    assert len(blocks) == count
    for block in blocks:
        values = dict(re.findall(r'(--ui-[\w-]+):\s*(#[\da-fA-F]{6})', block))
        border = luminance(values['--ui-control-border'])
        for token in ('--ui-panel', '--ui-raised', '--ui-side'):
            background = luminance(values.get(token, values['--ui-panel']))
            ratio = (max(border, background) + 0.05) / (min(border, background) + 0.05)
            assert ratio >= 3, (token, values['--ui-control-border'], values[token], ratio)


def test_extension_general_status_is_not_unconditionally_success_colored():
    css = (Path(__file__).resolve().parents[1] / "web/browser-extension/popup.css").read_text()
    indicator = re.search(r"#connection::before\s*\{([^}]+)\}", css).group(1)
    assert "background: var(--ui-muted)" in indicator
    assert "--ui-success" not in indicator
