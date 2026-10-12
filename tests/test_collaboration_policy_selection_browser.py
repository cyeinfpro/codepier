"""Production delegation selection in isolated browsers, without external services."""
from pathlib import Path

import pytest

pytestmark = [pytest.mark.browser]
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_persistent_policy_uses_effective_authority_and_bounded_policy_uses_expiry(
        chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        page.add_script_tag(path=str(ROOT / 'web/collaboration-delegation.js'))
        actual = page.evaluate("""() => {
          const c = {mentions: [], delegationTargets: [], delegationCapabilities: []};
          const api = window.CodePierCollaborationDelegation.create({
            state: {delegationPolicies: []}, chat: () => c,
          });
          const past = Math.floor(Date.now() / 1000) - 60;
          const future = Math.floor(Date.now() / 1000) + 3600;
          return {
            persistentPast: api.policyAvailable({
              effective_active: true, lifetime_mode: 'until_revoked', expires_at: past,
            }),
            persistentRevoked: api.policyAvailable({
              effective_active: false, lifetime_mode: 'until_revoked', expires_at: future,
            }),
            persistentUnknown: api.policyAvailable({
              lifetime_mode: 'until_revoked', expires_at: future,
            }),
            boundedPast: api.policyAvailable({
              effective_active: true, lifetime_mode: 'fixed', expires_at: past,
            }),
            boundedFuture: api.policyAvailable({
              effective_active: true, lifetime_mode: 'fixed', expires_at: future,
            }),
            boundedRevoked: api.policyAvailable({
              effective_active: false, lifetime_mode: 'fixed', expires_at: future,
            }),
            absent: api.policyAvailable(null),
          };
        }""")
        assert actual == {
            'persistentPast': True, 'persistentRevoked': False, 'persistentUnknown': False,
            'boundedPast': False, 'boundedFuture': True, 'boundedRevoked': False, 'absent': False,
        }
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_policy_reselection_keeps_explicit_subset_without_inheriting_changed_authority(
        chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        page.add_script_tag(path=str(ROOT / 'web/collaboration-delegation.js'))
        actual = page.evaluate("""() => {
          const c = {
            mentions: ['dot-1'], reply: '', delegationPolicy: '', delegationVersion: 0,
            delegationTargets: [], delegationCapabilities: [],
          };
          let policy = {
            id: 'policy-1', slot_id: 'dot-1', version: 3, effective_active: true,
            lifetime_mode: 'until_revoked', expires_at: 1, automatic_delegation: true,
            capabilities: ['read', 'write', 'execute'],
            execution_targets: ['project_agent', 'vps:fixture-a'],
            execution_target_options: [
              {id: 'project_agent', available: true}, {id: 'vps:fixture-a', available: true},
            ],
          };
          const state = {delegationPolicies: [policy]};
          const api = window.CodePierCollaborationDelegation.create({
            state, chat: () => c, mentionSlots: () => [{id: 'dot-1'}],
          });
          const snapshot = () => ({
            targets: [...c.delegationTargets], capabilities: [...c.delegationCapabilities],
            version: c.delegationVersion, valid: api.selectedScopeValid(),
          });
          api.selectPolicy(policy);
          const initial = snapshot();
          c.delegationTargets = ['project_agent'];
          c.delegationCapabilities = ['read'];
          api.selectPolicy(policy);
          const repeated = snapshot();
          // A renewed server snapshot of the same authority must not broaden choices.
          policy = {...policy, execution_targets: [...policy.execution_targets, 'vps:fixture-b']};
          state.delegationPolicies = [policy];
          api.selectPolicy(policy);
          const expanded = snapshot();
          // A new version requires a new target choice, even when the old target remains.
          policy = {...policy, version: 4};
          state.delegationPolicies = [policy];
          api.selectPolicy(policy);
          const revised = snapshot();
          c.delegationTargets = ['vps:fixture-a'];
          c.delegationCapabilities = ['read', 'execute'];
          policy = {...policy, execution_target_options: [
            {id: 'project_agent', available: true}, {id: 'vps:fixture-a', available: false},
          ]};
          state.delegationPolicies = [policy];
          api.selectPolicy(policy);
          const unavailable = snapshot();
          c.delegationTargets = ['project_agent'];
          policy = {...policy, effective_active: false};
          state.delegationPolicies = [policy];
          const revokedBeforeReselection = api.selectedScopeValid();
          api.selectPolicy(policy);
          const revoked = snapshot();
          api.selectPolicy(null);
          const cleared = snapshot();
          return {initial, repeated, expanded, revised, unavailable,
            revokedBeforeReselection, revoked, cleared};
        }""")
        all_caps = ['read', 'write', 'execute']
        assert actual['initial'] == {
            'targets': [], 'capabilities': all_caps, 'version': 3, 'valid': False,
        }
        selected = {'targets': ['project_agent'], 'capabilities': ['read'], 'version': 3, 'valid': True}
        assert actual['repeated'] == selected
        assert actual['expanded'] == selected
        assert actual['revised'] == {
            'targets': [], 'capabilities': all_caps, 'version': 4, 'valid': False,
        }
        assert actual['unavailable'] == {
            'targets': [], 'capabilities': ['read', 'execute'], 'version': 4, 'valid': False,
        }
        assert actual['revokedBeforeReselection'] is False
        assert actual['revoked'] == {
            'targets': [], 'capabilities': all_caps, 'version': 4, 'valid': False,
        }
        assert actual['cleared'] == {
            'targets': [], 'capabilities': [], 'version': 0, 'valid': False,
        }
    finally:
        context.close()
