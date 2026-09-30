"""Central test tiers; infer browser requirements from imports, never filenames alone."""
from __future__ import annotations
import ast
from functools import lru_cache
from pathlib import Path

WINDOWS_CORE = {
    'test_native_windows.py', 'test_windows_service.py', 'test_optimization_store.py',
    'test_regression_plan.py', 'test_smoke.py', 'test_protocol_negotiation.py',
}

# These modules deliberately assert short local timing, subprocess startup,
# parser-worker failure semantics, or browser/service restart recovery. Running
# them beside other whole-module pytest processes measures host contention, not
# the contract under test, so the exhaustive runner gives each one the machine.
REGRESSION_EXCLUSIVE = {
    'test_audit_agent.py', 'test_audit_bridge_deploy.py', 'test_computer_lifecycle.py',
    'test_continuous_access_ui.py', 'test_native_cli.py', 'test_native_browser_hardening.py',
    'test_panel_update_autoreload.py', 'test_panel_update_ui.py', 'test_parser_recovery_integration.py',
    'test_reliability.py', 'test_symbol_parser_stability.py',
    'test_chat_backend_stability.py', 'test_background_browser.py', 'test_bridge.py',
    'test_chat_review_worker.py', 'test_chat_stability_async.py', 'test_chat_mobile_immersion.py',
    'test_chat_webkit_browser.py', 'test_chat_worker.py', 'test_chat_window_browser.py',
    'test_codepier_ui.py', 'test_agent_install_ui.py',
}

@lru_cache(maxsize=None)
def module_tiers(filename):
    path = Path(filename)
    tree = ast.parse(path.read_text(encoding='utf-8'))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
    browser = any(name.startswith('playwright') or name == 'tests.browser_support' for name in imports)
    integration = browser or bool(imports & {'subprocess', 'multiprocessing'})
    return browser, integration
