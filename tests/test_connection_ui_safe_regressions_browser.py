"""Read-only render and test-harness regressions; no provider or grant mutations."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("usage,expected", [
    ({"budget_mode": "unlimited", "total_delegations": 23, "max_delegations": 0},
     "任务数量不限；累计 23 次"),
    ({"budget_mode": "unlimited", "total_delegations": 0, "delegations": 9},
     "任务数量不限；累计 0 次"),
    ({"budget_mode": "unlimited", "delegations": 12}, "任务数量不限；累计 12 次"),
    ({"budget_mode": "rolling_24h", "window_delegations": 2, "max_delegations": 50,
      "total_delegations": 101}, "滚动24小时已用 2 / 50；累计 101 次"),
    ({"budget_mode": "lifetime", "delegations": 3, "max_delegations": 50}, "已用 3 / 50 次"),
])
def test_budget_usage_renders_exact_units_without_mutating_authority(chat_browser_pool, engine, usage, expected):
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        page.add_script_tag(path=str(ROOT / "web/collaboration-delegation.js"))
        actual = page.evaluate("""usage => {
          const policy = {id:'policy-safe', slot_id:'dot-safe', version:3,
            effective_active:true, lifetime_mode:'until_revoked', expires_at:1,
            purpose:'Synthetic read-only display', execution_targets:['project_agent'],
            capabilities:['read'], usage};
          const state = {project:'project-safe', delegationPolicies:[policy],
            snapshot:{can_manage:true, capabilities:{direct_delegation:true}}};
          const before = JSON.stringify(state);
          let writes = 0;
          const api = window.CodePierCollaborationDelegation.create({
            state, E: value => String(value ?? ''), when: () => 'synthetic',
            projectLabel: () => 'Synthetic project',
            button: (_action,label) => '<button class="btn">' + label + '</button>',
            mutation: () => { writes++; throw Error('Unexpected write'); },
          });
          document.body.innerHTML = api.policyCard({id:'dot-safe',label:'Synthetic dot',state:'registered'});
          return {same: JSON.stringify(state) === before, writes};
        }""", usage)
        expect(page.locator(".cc-connection-settings")).to_contain_text(expected)
        if usage["budget_mode"] == "unlimited":
            expect(page.locator(".cc-connection-settings")).not_to_contain_text(" / 0")
        assert actual == {"same": True, "writes": 0}
    finally:
        context.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_deferred_evaluate_returns_before_release_and_keeps_late_result_inert(chat_browser_pool, engine):
    """Prove the safe callback shape independently of the blocked enrollment code."""
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        page.evaluate("""() => {
          window.view = 'form'; window.writes = []; window.applied = false;
          window.delayedRead = async () => {
            await new Promise(resolve => window.releasePreview = resolve);
            if (view === 'form') applied = true;
          };
          window.pendingPreview = delayedRead();
        }""")
        page.wait_for_function("typeof releasePreview === 'function'")
        assert page.evaluate("applied") is False
        page.evaluate("view = 'overview'")
        page.evaluate("releasePreview(); pendingPreview")
        assert page.evaluate("({view, applied, writes: writes.length})") == {
            "view": "overview", "applied": False, "writes": 0,
        }
    finally:
        context.close()
