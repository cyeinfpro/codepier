"""Deterministic delayed permission invalidation hint against real room actions."""
import pytest
from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_room_permission_bulk_browser import test_bulk_permissions_select_vps_and_constrain_custom_channels as bulk_journey

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_permission_save_with_delayed_access_hint(collaboration_stack, chat_browser_pool, engine):
    traces = []
    class Browser:
        def new_context(self, **kwargs):
            context = chat_browser_pool(engine).new_context(**kwargs)
            context.add_init_script('''(() => {
              const Native = window.EventSource;
              window.__roomHintTrace = [];
              window.EventSource = class extends Native {
                constructor(...args) { super(...args); this.addEventListener('open', () => __roomHintTrace.push(['open', performance.now()])); }
                addEventListener(type, listener, options) {
                  return super.addEventListener(type, type === 'access_changed' ? (event) => {
                    __roomHintTrace.push(['access_received', performance.now()]);
                    setTimeout(() => { __roomHintTrace.push(['access_applied', performance.now()]); listener.call(this, event); }, 1000);
                  } : listener, options);
                }
              };
            })();''')
            close = context.close
            def observed_close():
                traces.extend(page.evaluate("window.__roomHintTrace || []") for page in context.pages)
                close()
            context.close = observed_close
            return context
    bulk_journey(collaboration_stack, lambda _: Browser(), engine)
    print("HINT_TRACE", traces)
    assert any(event[0] == "access_received" for trace in traces for event in trace), traces
    assert sum(event[0] == "access_applied" for trace in traces for event in trace) >= 2, traces
