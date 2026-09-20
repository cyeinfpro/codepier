"""Typed, app-scoped desktop contracts; never accept arbitrary provider commands."""
from __future__ import annotations
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.util import valid_json_value

NATIVE_ACTIONS = frozenset({'click', 'perform_secondary_action', 'set_value', 'select_text', 'scroll', 'drag', 'press_key', 'type_text'})
NATIVE_TOOLS = NATIVE_ACTIONS | {'list_apps', 'get_app_state'}
COMPUTER_TOOLS = frozenset({'computer_status', 'computer_apps', 'computer_session_open', 'computer_observe', 'computer_action', 'computer_session_close'})
COMPUTER_READ_TOOLS = frozenset({'computer_status', 'computer_apps', 'computer_observe'})

class ComputerArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='before')
    @classmethod
    def valid_json(cls, value):
        if not valid_json_value(value):
            raise ValueError('Expected finite, valid JSON')
        return value

class ComputerProject(ComputerArgs):
    project: str = Field(min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)

class ComputerStatus(ComputerProject):
    probe: bool = Field(default=False, description='Initialize the installed provider and read its tool catalog only; never capture a screen or list apps. Requires local Computer Use opt-in.')

class ComputerApps(ComputerProject):
    pass

class ComputerOpen(ComputerProject):
    app: str = Field(min_length=1, max_length=512, description='Exact locally allowed app name, bundle ID, or application path. The session cannot switch apps.')
    ttl_seconds: int = Field(default=300, ge=30, le=1800)
    idempotency_key: str = Field(min_length=8, max_length=128)

class ComputerObserve(ComputerProject):
    session_id: str = Field(pattern=r'^[a-f0-9]{32}$')

class Click(ComputerArgs):
    type: Literal['click']
    element_index: str | None = Field(default=None, min_length=1, max_length=128)
    x: float | None = Field(default=None, ge=0, le=65535)
    y: float | None = Field(default=None, ge=0, le=65535)
    mouse_button: Literal['left', 'right', 'middle'] = 'left'
    click_count: int = Field(default=1, ge=1, le=3)

    @model_validator(mode='after')
    def target(self):
        if (self.x is None) != (self.y is None) or bool(self.element_index) == (self.x is not None):
            raise ValueError('Supply either element_index OR both screenshot-pixel x/y')
        return self

class SecondaryAction(ComputerArgs):
    type: Literal['perform_secondary_action']
    element_index: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=256)

class SetValue(ComputerArgs):
    type: Literal['set_value']
    element_index: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=16384)

class SelectText(ComputerArgs):
    type: Literal['select_text']
    element_index: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=16384)
    prefix: str | None = Field(default=None, max_length=2048)
    suffix: str | None = Field(default=None, max_length=2048)
    selection: Literal['text', 'cursor_before', 'cursor_after'] = 'text'

class Scroll(ComputerArgs):
    type: Literal['scroll']
    element_index: str = Field(min_length=1, max_length=128)
    direction: Literal['up', 'down', 'left', 'right']
    pages: float = Field(default=1.0, gt=0, le=20)

class Drag(ComputerArgs):
    type: Literal['drag']
    from_x: float = Field(ge=0, le=65535)
    from_y: float = Field(ge=0, le=65535)
    to_x: float = Field(ge=0, le=65535)
    to_y: float = Field(ge=0, le=65535)

class PressKey(ComputerArgs):
    type: Literal['press_key']
    key: str = Field(min_length=1, max_length=256, description='Native xdotool key syntax, e.g. Return, Tab, super+c, Shift+Tab. Not a shell command.')

class TypeText(ComputerArgs):
    type: Literal['type_text']
    text: str = Field(min_length=1, max_length=16384)

DesktopAction = Annotated[Click | SecondaryAction | SetValue | SelectText | Scroll | Drag | PressKey | TypeText, Field(discriminator='type')]

class ComputerAction(ComputerObserve):
    observation_id: str = Field(pattern=r'^[a-f0-9]{32}$', description='Latest observation from this session. Each action consumes it, including ambiguous failures.')
    action: DesktopAction
    verify_unchanged: bool = Field(default=True, description='Re-read app state before input and reject changed accessibility state (or screenshot for image-only apps). False still enforces observation identity, TTL and app scope.')
    idempotency_key: str = Field(min_length=8, max_length=128, description='Reuse only for the identical request after a transport failure. Never replay uncertain input with a new key.')

class ComputerClose(ComputerProject):
    session_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')
    force: bool = Field(default=False, description='Panel administrator only: stop the active CodePier desktop session, including another grant. Does not undo input or close the user app.')
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode='after')
    def close_target(self):
        if not self.force and not self.session_id:
            raise ValueError('session_id is required unless panel administrator force-stops')
        return self
