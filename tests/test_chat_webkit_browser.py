"""Playwright WebKit parity; exercises WebKit, not a claimed real iOS device."""
import pytest
from tests.test_chat_browser import chat_page
from tests import test_chat_window_browser as window

@pytest.mark.parametrize('chat_page',['webkit'],indirect=True)
@pytest.mark.parametrize('scenario',[
 'test_switch_keeps_window_composer_nodes_and_tool_disclosure',
 'test_refresh_history_retains_focused_row_and_scroll',
 'test_rich_streaming_preserves_selection_and_existing_blocks',
 'test_streaming_does_not_take_reader_back_to_bottom',
 'test_slash_enter_completes_without_sending_a_prompt',
 'test_palette_and_model_picker_are_keyboard_operable',
 'test_rename_dialog_does_not_block_stream_and_requires_explicit_save',
 'test_switch_cancels_pending_confirmation_without_wrong_project_write',
 'test_session_settings_confirmation_recovers_after_switch',
 'test_resize_preserves_history_preference_and_focus',
 'test_theme_switch_is_atomic_for_composer_and_window',
])
def test_webkit_interaction_parity(chat_page,scenario):
    getattr(window,scenario)(chat_page)

@pytest.mark.parametrize('chat_page',['webkit'],indirect=True)
@pytest.mark.parametrize('width,height',[(1440,1000),(390,844),(320,568),(667,375)])
def test_webkit_viewport(chat_page,width,height):
    window.test_layers_follow_actual_composer_and_viewport(chat_page,width,height)

@pytest.mark.parametrize('chat_page',['webkit'],indirect=True)
@pytest.mark.parametrize('appearance',['light','dark'])
def test_webkit_appearance(chat_page,appearance):
    window.test_neutral_appearance_and_reduced_motion(chat_page,appearance)
