"""Source-only responsive layout regression contracts.

These checks are deliberately runnable without a Hub, GUI permissions or a
downloaded browser. They supplement, rather than replace, rendered screenshots.
"""
from pathlib import Path

from scripts.css_override_report import declarations

WEB = Path(__file__).resolve().parents[1] / "web"


def rules(filename):
    return list(declarations((WEB / filename).read_text(encoding="utf-8")))


def has_rule(filename, selector, property_name, value, *, condition=None):
    return any(
        selector in [s.strip() for s in entry_selector.split(",")]
        and prop == property_name
        and val == value
        and (condition is None or condition in context)
        for context, entry_selector, prop, val in rules(filename)
    )


def test_panel_update_does_not_redefine_mobile_shell_geometry():
    # A shared topbar unexpectedly wrapped on every route merely because the
    # panel-update stylesheet had been loaded (even when its button was hidden).
    assert not any(selector.strip() == ".topbar"
                   for _ctx, selector, _prop, _val in rules("panel-update.css"))
    assert has_rule(
        "panel-update.css", ".topbar .panel-update-entry [data-panel-update-status]",
        "display", "none", condition="@media (max-width: 640px)"
    )


def test_long_device_and_vps_details_can_shrink_inside_two_column_cards():
    assert has_rule("styles.css", ".card-meta", "grid-template-columns",
                    "repeat(2, minmax(0, 1fr))")
    assert has_rule("vps.css", ".vps-facts", "grid-template-columns",
                    "repeat(2, minmax(0, 1fr))")


def test_settings_cards_allow_long_copy_at_mobile_width():
    assert has_rule("settings.css", ".settings-toolbar",
                    "grid-template-columns", "repeat(2, minmax(0, 1fr))")
    assert has_rule("settings.css", ".settings-flow-card",
                    "grid-template-columns", "32px minmax(0, 1fr)",
                    condition="@media (max-width: 700px)")


def test_room_controls_and_permission_rows_fit_narrow_screens():
    assert has_rule("collaboration-room.css", ".cr-room-actions",
                    "grid-template-columns", "repeat(3, minmax(0, 1fr)) 44px",
                    condition="@media (max-width: 720px)")
    assert has_rule("collaboration-room.css", ".cr-permission-row-main",
                    "grid-template-columns", "minmax(0, 1fr)",
                    condition="@media (max-width: 460px)")


def test_phone_chat_has_accessible_compact_return_control():
    source = (WEB / "chat-chrome.js").read_text(encoding="utf-8")
    assert 'class="chat-back-button"' in source
    assert 'aria-label="返回面板"' in source
    assert has_rule("chat.css", ".chat-workspace .chat-back-button", "min-width", "44px",
                    condition="@media (max-width: 360px)")
    assert has_rule("chat.css", ".chat-workspace .chat-back-button span", "display", "none",
                    condition="@media (max-width: 360px)")


def test_keyboard_and_landscape_modals_preserve_reachable_actions():
    ui = (WEB / "ui.js").read_text(encoding="utf-8")
    assert "--ui-visual-viewport-bottom" in ui
    assert "ui-keyboard-open" in ui
    assert has_rule("chat.css", "body.ui-keyboard-open .chat-workspace", "min-height", "0",
                    condition="@media (max-width: 760px)")
    assert has_rule("styles.css", "body.ui-keyboard-open .main:has(> #page .cr-shell)",
                    "height", "max(0px, calc(100dvh - var(--ui-visual-viewport-bottom, 0px)))",
                    condition="@media (max-width: 900px)")
    assert has_rule("styles.css", ".modal:not(.modal-editor) .modal-footer",
                    "overflow-y", "auto", condition="@media (max-height: 520px)")
    assert has_rule("collaboration-room.css", "body.ui-keyboard-open .cr-panel",
                    "bottom", "var(--ui-visual-viewport-bottom, 0px)",
                    condition="@media (max-width: 720px)")


def test_all_first_party_css_stays_parseable():
    count = 0
    for stylesheet in sorted(WEB.rglob("*.css")):
        if "vendor" in stylesheet.parts:
            continue
        list(declarations(stylesheet.read_text(encoding="utf-8")))
        count += 1
    assert count >= 20
