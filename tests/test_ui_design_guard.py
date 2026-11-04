"""Design drift ratchet: compatibility exceptions are explicit and bounded."""
from collections import Counter
from pathlib import Path

from scripts.css_override_report import declarations, overridden, split_top
from scripts.ui_design_audit import compatibility_limits, key, metrics
from scripts.ui_token_codemod import rewrite

ROOT = Path(__file__).resolve().parents[1]
# Raw color totals remain visible. Required standalone fallbacks are separately
# frozen by full selector, condition, property, value and occurrence count.
BASELINE = {
    'hex_outside_tokens': 152,
    'unapproved_hex': 3,
    'compatibility_hex': 149,
    'important': 15,
    'raw_z_index': 10,
    'font_px_below_11': 0,
    'font_px_below_12': 0,
    'font_size_distinct': 9,
    'font_size_distinct_raw': 17,
    'radius_distinct': 8,
    'radius_distinct_raw': 22,
    'weight_distinct': 4,
    'native_confirm_calls': 12,
    'native_show_modal_calls': 1,
}


def test_design_drift_never_grows():
    current = metrics()
    grown = {key_: (current[key_], limit) for key_, limit in BASELINE.items() if current[key_] > limit}
    assert not grown, f'Design drift increased (actual, ceiling): {grown}'


def test_global_layer_tokens_and_local_bounds():
    assert all(0 <= value <= 9 for value in metrics()['raw_z_values'])
    values = {prop: value for context, selector, prop, value in
              declarations((ROOT / 'web/tokens.css').read_text()) if selector == ':root' and not context}
    layers = ['local', 'sticky', 'dropdown', 'dock', 'drawer-scrim', 'drawer',
              'popover', 'modal', 'critical', 'toast', 'skip']
    assert [int(values['--ui-z-' + layer]) for layer in layers] == [
        1, 100, 200, 300, 390, 400, 500, 600, 700, 800, 900]


def test_explicit_and_system_dark_tokens_are_identical():
    rules = list(declarations((ROOT / 'web/tokens.css').read_text()))
    explicit = {prop: value for _, selector, prop, value in rules
                if selector == "html[data-appearance='dark']"}
    system = {prop: value for _, selector, prop, value in rules
              if selector == 'html:not([data-appearance])'}
    assert explicit and explicit == system


def test_fallback_exceptions_are_exact_and_bounded():
    limits = compatibility_limits()
    assert limits and len(limits) <= 151
    assert all(item[0] in {'chat.css', 'computer.css'} and count == 1 for item, count in limits.items())
    observed = Counter(key(path.name, context, selector, prop, value)
                       for path in (ROOT / 'web').glob('*.css')
                       for context, selector, prop, value in declarations(path.read_text()))
    # Removed compatibility code must also remove the obsolete exception.
    assert not (limits - observed)


def test_css_parser_respects_conditions_functions_strings_and_comments():
    css = '''/* { ignored } */ @supports (display:grid) {
      @media (width < 400px) { :is(.a,.b), .c {
        content: "/* literal */ {;}";
        font: 600 10px/1.5 monospace;
        background: rgb(0 0 0 / 50%);
      } }
    }'''
    rows = list(declarations(css))
    assert len(rows) == 3
    assert rows[0][0] == ('@supports (display:grid)', '@media (width < 400px)')
    assert rows[0][3] == '"/* literal */ {;}"'
    assert split_top(rows[0][1]) == [':is(.a,.b)', '.c']
    assert not list(overridden('.a,.b{color:red}', '.a{color:blue}'))
    assert not list(overridden('@supports(display:grid){.a{color:red}}', '.a{color:blue}'))
    assert list(overridden('.a,.b{color:red}', '.a,.b{color:blue}'))


def test_audit_rejects_unlisted_and_duplicated_fallbacks(tmp_path):
    css = '.sample { color: var(--ui-text, #272727); font: 600 10px/1.5 monospace; }'
    path = tmp_path / 'chat.css'
    path.write_text(css)
    identity = key('chat.css', (), '.sample', 'color', 'var(--ui-text, #272727)')
    assert metrics(tmp_path, {})['unapproved_hex'] == 1
    allowed = {identity: 1}
    assert metrics(tmp_path, allowed)['unapproved_hex'] == 0
    assert metrics(tmp_path, allowed)['font_px_below_11'] == 1
    path.write_text(css + css)
    assert metrics(tmp_path, allowed)['unapproved_hex'] == 1


def test_codemod_preserves_comments_and_updates_standalone_fallbacks():
    original = '/* font-size: 8px; */ .x {font-size: 8px; font: 650 10px/1.5 sans-serif; border-radius: 12px;}'
    changed = rewrite(original, True)
    assert '/* font-size: 8px; */' in changed
    assert 'font-size: var(--ui-text-xs, 12px)' in changed
    assert 'font: var(--ui-weight-semibold) var(--ui-text-xs, 12px)/1.5' in changed
    assert 'border-radius: var(--ui-radius-md, 12px)' in changed
    assert rewrite(changed, True) == changed


def test_codemod_never_rewrites_css_string_contents():
    css = '.x {content: "font-size: 8px;"; font-size: 8px;}'
    changed = rewrite(css, False)
    assert 'content: "font-size: 8px;"' in changed
    assert 'font-size: var(--ui-text-xs);' in changed
