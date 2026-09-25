# Multi-user / OIDC: verified development status

**DRAFT. Not a multi-user release. Do not enable general OIDC enrollment.**

Base: `57f502d428472d8e7f6d71195a952b7157a8e5ea` (existing dynamic Roles and CI fixes).

## What actually exists

This branch adds a new, independently tested authorization core in
`hub/multiuser_policy.py` and its tests in `tests/test_multiuser_policy.py`.
It is NOT connected to `Auth`, panel routes, MCP, the database or Agent execution.
It makes no production authorization, schema, deployment or IdP changes.

The core implements:

- Explicit User / Space / Membership / RoleAssignment / Profile / Grant /
  Role / Resource bindings, checked together with deny-by-default behaviour.
- Current-state reads through an AccessStore protocol on every decision; no
  initial project/capability snapshot is used as the role's permanent ceiling.
- Dynamic role expansion to new projects without replacing a grant.
- All-resource selectors bounded by the grant's Space, and action/resource
  pairing to prevent a union-of-actions x union-of-resources escalation.
- Current suspension, role assignment, delegation eligibility, grant expiry,
  revocation, audience and resource-ceiling checks.
- Separate user-private and grant-private record gates. A shared Role does not
  share private transcripts, operations or sessions.
- An exact OIDC `(issuer, subject)` identity-key value type. This is NOT ID-token
  verification, login, provisioning, or an account-linking implementation.

Read transactions, secret handling, database records and trusted resource
resolution must be supplied by future server adapters. Never construct these
objects directly from client-supplied arguments or unverified identity claims.
An Authorization result is not a durable lease: adapters must recheck at dispatch,
inside mutation transactions, after awaited results and during subscriptions.
Agent-local filesystem/command/desktop gates must remain independently enforced.

Rule exclusions are local to their rule, not a global deny. Roles do not get an
implicit administrator bypass. Resource ceilings default to an empty set.
The delegated evaluator is not a substitute for separate human administration
permissions or safe transactionally checked Role editing.

## Outstanding implementation (not merely unrun tests)

- [ ] **Schema and migrations:** users' active state/local credentials;
  external identities; Spaces; memberships and source-tracked assignments;
  resource/history ownership; composite foreign keys; Space-scoped aliases;
  migration preserving old IDs, grants, secrets and the encryption key.
- [ ] **OIDC client and routes:** configured providers/discovery, safe endpoint
  handling, Authorization Code + PKCE, one-time state, nonce, callback and ID-token
  verification, encrypted server-side secrets, replay prevention and JIT admission.
- [ ] **Identity lifecycle:** explicit account linking (never email auto-link),
  recovery administrator, active-session management, validated back-channel logout.
- [ ] **Persistence adapter:** AccessStore.read_access must load consistent,
  current server records. Implement grant/profile/user/Space validation without
  trusting a caller's selected role or resource Space.
- [ ] **Panel authentication:** replace automatic full admin from Auth.admin with
  ordinary-user identity and explicit instance / Space / resource capabilities.
- [ ] **Role assignments and delegated credentials:** shared Role ownership,
  permission to use versus assign versus edit, Space-aware OAuth consent/PAT
  issuance and token refresh, no self-elevation via Profile creation.
- [ ] **Runtime enforcement:** apply the central policy to EVERY read/write,
  queue admission/delivery/cancellation, waited result, search, workflow, artifact,
  VPS, device management and native-session operation.
- [ ] **Privacy and events:** filtered overview counts, lists, audit/export,
  downloads, SSE/WebSocket events, approval queues, native caches and session
  history. Shared projects must not implicitly share credentials/transcripts.
- [ ] **Devices and execution:** self-service enrollment/ownership, trusted
  canonical mapping, per-user/per-Space Agent registration, local enforcement,
  and explicit OS-account/container/VM trust boundaries for Shell/Computer Use.
- [ ] **User interfaces:** OIDC login/linking, per-tab Space selector, membership
  and assignment management, personal connections, scoped resources and errors,
  preserving current layout and accessibility.
- [ ] **Groups/offboarding:** explicit provider-scoped mappings with assignment
  provenance, reconciliation freshness, user/membership/assignment revocation
  across sessions, MCP tokens, queued work and live streams.
- [ ] **End-to-end acceptance:** cross-user/cross-Space HTTP and MCP tests,
  deterministic local OIDC provider, Authentik integration, replay/issuer/nonce
  failure tests, concurrency/last-owner protection, migration and rollback tests,
  Chromium/WebKit UI matrix and full Linux/macOS/Windows CI.

## Verified tests for THIS branch

Command executed locally:

```sh
python -m pytest -q tests/test_multiuser_policy.py \
  --junitxml=/mnt/data/codepier-multiuser-policy-junit.xml
```

Result: **72 passed**, no skips/failures/errors. These tests use a controlled
in-memory policy-record adapter, not real OIDC, SQL persistence, HTTP, browsers,
Agents, a production IdP or the complete repository test suite.
Python source compilation also passed. The existing full CI must be run against
the published branch; a successful core test cannot certify multi-user isolation.

## Correction to the earlier delivery report

The previously described multi-user/OIDC development ZIP, patch, manifest,
workspace and checkpoint logs were not recoverable from the active runtime or
file search. Packaging commands shown in the development conversation failed
with FileNotFoundError. No corresponding OIDC branch existed at verification.
The earlier claims of an available package and 18/28/221 passing OIDC/multi-user
checkpoints are unverified and must NOT be used as delivery/acceptance evidence.

The code and 72 tests in this branch were newly written and executed during the
recovery effort. They do not represent recovered full implementation. Existing
Access Profiles / Dynamic Roles and their merged CI fixes remain separate,
previously published work.

## Design constraints retained

1. A secretary's Role can gain or lose projects and capabilities dynamically;
   no repeated OAuth is required for a valid, explicitly role-delegated grant.
2. All present/future projects means within the explicitly bound Space.
3. Profile identity is stable, distinct from the human and the shared Role.
4. OIDC authenticates humans; CodePier issues its own downstream MCP credentials.
5. No ordinary-user enrollment until all exposed routes are isolated. Merely
   adding a login button to the current admin session flow is unsafe.
6. Incomplete isolation is a release blocker, not a reason to omit read-only,
   administrative, cached or streaming surfaces from the implementation.

Specification references:
- OIDC claim stability: https://openid.net/specs/openid-connect-core-1_0.html#ClaimStability
- Authorization principles: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
