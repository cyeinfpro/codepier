# Multi-user OIDC: setup, operation and recovery

This guide describes the integrated multi-user branch. It is not a claim that
this branch is deployed. Check the PR's CI result for the exact commit being
installed, including the real Authentik job. Provider configuration is disabled
by default; an upgrade does not silently enable enrollment.

## Identity and authority

OIDC authenticates the human. CodePier continues to issue its own MCP OAuth/PAT
credentials; it never forwards an IdP Token to ChatGPT or an Agent.

```
OIDC (exact issuer + subject) -> CodePier human
  -> active Space membership -> current shared Role assignment
  -> user's stable Profile -> particular client grant -> MCP request
```

A **Space** is a personal or team security scope. A **Role** contains current
operation/resource rules in that Space. A **Profile** is a stable identity owned
by one human, and a **grant** represents one delegated connection. Roles may be
shared; credentials and private sessions are not implicitly shared.

A role connection is not limited to its first project's list: changing the Role
can add/remove both projects and capabilities without a new grant or OAuth.
“All current and future projects” always means within that Role's Space. Role
assignment, permission to delegate, user status and membership remain live gates.
Changing the Role attached to a Profile is different from editing that Role's
policy: an existing grant cannot silently inherit another Role.

There are three administration boundaries:

- Instance administration: providers, user suspension/promotion, global settings
  and upgrades. It is not granted by an arbitrary IdP group name.
- Space owner/admin: team membership, Roles and assignments, scoped resources.
  Only an owner may change ownership; the last active owner is protected.
- Member/guest: assigned resource capabilities, permitted personal connections
  and owned devices. Membership alone does not grant all project operations.

The trusted instance operator has recovery access to private records in Spaces
where they have membership. A Space administrator alone does not automatically
receive other humans' private transcripts. The Hub host operator is trusted;
this is not encryption against the machine administrator.

## Upgrade an existing single-owner Hub

1. Record the existing source/container version. Run its backup command **before**
   installing the new code; opening Store with new code performs migration.
2. Keep the backup of `hub.sqlite3` together with the matching `master.key` private.
   Also retain a consistent copy/snapshot of the full Hub data directory, including
   native-session caches, plus the Agent configuration/state needed for recovery.
3. Install the reviewed branch version and its pinned requirements. Restart the
   single Hub process normally; do not run two Hubs against the same data directory.
4. Sign in with the existing local administrator and verify the Legacy Space,
   projects, original Profiles and existing MCP grants before configuring OIDC.
5. Update Agents before ordinary users use native sessions. The new ownership
   capability is checked; an older Agent must not serve shared native transcripts
   as if it supported per-user ownership.

Example backup with the **old** installed code:

```sh
python -m hub --data-dir /srv/codepier/data backup \
  --output /secure-backups/codepier-before-multiuser.zip
```

Migration is transactional to schema **9**. It retains existing user, resource,
Profile, Role and grant IDs, Token hashes, device credentials and encryption key.
Legacy resources enter `legacy`; later OIDC users get a separate personal Space
and do not automatically become members of Legacy. Project aliases become unique
per Space. Operation/workflow request keys are also Space-scoped, including after
reopening the database. Cross-Space references and in-place resource Space changes
are rejected; renaming a resource is not a way to move it across a security boundary.

**Rollback:** stop the new Hub, restore the previous application version and its
matching complete pre-upgrade data backup. Do not point old code at schema 9 or
replace only the encryption key. Preserve newer data separately before rollback.
Already executed external commands cannot be undone by restoring the Hub database.

## Configure Authentik

The automated acceptance fixture pins Authentik 2026.8.3. Generic OIDC is supported,
not Authentik-specific identity parsing. Use the IdP's discovery document as the
source of truth and test your own deployment before broad enrollment.

Create an OAuth2/OpenID provider and application for CodePier:

- Confidential client with a generated client secret.
- Explicitly enable the Authorization Code grant and S256 PKCE. Enable the Refresh
  Token grant when requesting `offline_access`; no implicit flow is needed.
  When provisioning Authentik through its API, set `grant_types` to
  `["authorization_code", "refresh_token"]`: the API default is an empty list,
  which rejects authorization before the login form even though discovery works.
- An asymmetric signing key. CodePier accepts configured public RSA/EC signatures,
  not unsigned or symmetric ID Tokens.
- A strict, exact callback URI, copied from CodePier after the provider record is
  created. No wildcard, first-use callback registration or regular expression.
- The `openid` and `profile` mappings. Add `email` only when useful for display.
  Include/request `offline_access` for continued verified group reconciliation
  after the upstream access Token expires; it is not necessary to request email
  just to identify users.
- A group claim available in UserInfo, not only in a one-time ID Token, when using
  group mappings. Default CodePier claim name is `groups` and expects a list of
  strings. Use explicit Authentik property mappings for your deployment.
- Configure MFA/application admission in Authentik as appropriate, particularly
  for administrators. Group mappings in CodePier do not implement MFA themselves.

Authentik's usual per-provider issuer is:

```text
https://sso.example.com/application/o/codepier/
```

Its discovery URL is:

```text
https://sso.example.com/application/o/codepier/.well-known/openid-configuration
```

When using Authentik's global issuer mode, copy the exact root issuer and supply
its actual per-application discovery URL explicitly. Do not remove a trailing
slash or normalize one issuer into another. Email and username never establish
account ownership; identity is exactly `(issuer, sub)`.

### CodePier panel

Open **身份管理** (Identity administration) as the instance administrator, then
**添加 OIDC 提供者**. Initially leave enrollment closed/disabled.

| Field | Meaning |
|---|---|
| Issuer | Exact issuer from the discovery document; immutable after creation. |
| Client ID / secret | CodePier's confidential IdP client, not a ChatGPT Token. |
| Discovery URL | Optional override; defaults beneath the configured issuer. |
| OIDC scopes | For example `openid profile offline_access`. |
| Group claim | Defaults to `groups`; verified list of group strings. |
| Required group | Optional provider-wide login admission requirement. |
| Admission | `closed`: linked identities only; `jit`: admit new verified users. |
| Freshness seconds | Maximum entitlement age, 60–86400; default 900. |
| Extra endpoint origins | Only necessary for IdPs using other HTTPS origins. No wildcards. |

Use **测试发现** (Test discovery). It verifies discovery/JWKS and returns exact:

```text
https://YOUR-HUB/auth/oidc/PROVIDER-ID/callback
https://YOUR-HUB/auth/oidc/PROVIDER-ID/backchannel-logout
```

Register the callback in Authentik; optionally configure its back-channel logout
URL. The IdP and Hub must reach each other through normal TLS; server-to-server
metadata/JWKS/token/UserInfo endpoints cannot require interactive bot challenges.
CodePier does not follow upstream HTTP redirects or send client secrets to an
origin that was not explicitly configured. Restrict trusted reverse-proxy headers
with `FORWARDED_ALLOW_IPS`; keep the public Hub URL and proxy scheme consistent.

Link the existing recovery administrator through **我的账号 -> 关联提供者** while
recently signed in. Linking requires fresh IdP authentication and will not merge
an account simply because emails match. Test with a separate ordinary account.
Only then enable `jit` for general enrollment, preferably with a required group.
New accounts get a private personal Space, not instance administration or Legacy.

`CODEPIER_OIDC_INSECURE_TEST_LOOPBACK=1` exists solely for disposable loopback CI.
Do not enable it in the deployed Hub. It is not a production TLS workaround.

## Team Spaces, invitations and shared secretary Roles

1. Under **我的账号**, create a team Space. Select it in **当前空间**.
2. Under **空间成员**, issue a one-time invitation to an already signed-in human,
   or configure a verified provider group mapping. The invitation secret is shown
   only once; accepting it does not share the inviter's session or credentials.
3. Create the Space's `secretary` Role under **访问角色**. Keep operation/resource
   pairs explicit. An all-project read rule does not make a separate execute-on-A
   rule apply to every project.
4. Assign the Role to the human. Enable **允许通过 Profile 委派给 ChatGPT / MCP**
   (`may_delegate`) only when that human may create downstream role connections.
   Permission to use a Role is distinct from assigning or editing it.
5. Each human creates their own stable secretary Profile under **访问 Profiles**.
6. Connect ChatGPT to the role endpoint, select the intended Space/Profile in the
   authorization page, and explicitly accept current and future role policy.

```text
https://YOUR-HUB/mcp?authorization=role
https://YOUR-HUB/mcp?authorization=role&profile=coding
```

The coding parameter selects a tool catalog, not an identity. The authenticated
credential selects the human, grant and Space. Switching Space in the panel does
not retarget an existing MCP connection, and tool arguments cannot nominate a
more privileged user, Role or Space.

Use `get_profile()` to confirm stable identity and `get_access_context()` for
current role version, projects and **per-project** actions. The aggregate scopes
are a summary; they do not give every listed project all summarized capabilities.

Add another project or enable a capability in the secretary Role. Existing valid
role connections use the new policy on subsequent requests. Remove a project,
assignment or membership and new calls/results/queued delivery must be denied.
Old fixed-mode grants retain their fixed consent semantics and cannot silently
become dynamic grants.

## Groups and offboarding

Mappings explicitly associate one provider's exact group with a team Space,
member level and optional Role/delegation eligibility. Group names do not become
arbitrary administrator permissions. Group mappings cannot grant instance admin
or Space ownership. Manual, invitation and IdP-derived assignments keep separate
provenance; removing an IdP mapping does not erase an independent manual grant.

The worker checks eligible identities periodically, in bounded batches, using
verified UserInfo and upstream refresh where available. A provider-wide required
group removal or rejected credentials disables that identity. Ordinary group
removal removes that source's membership/Role assignment. **校验群组权限** starts
reconciliation of eligible identities (not a claim that every upstream service is
healthy or every account was just refreshed).

Local suspension acts immediately on current checks. Upstream directory changes
are observed at verified reconciliation/login, not magically at the time an IdP
administrator clicks Save. Cached entitlements are bounded by `freshness_seconds`;
network failures never extend that deadline. Without refresh support, the user
must reauthenticate after access-token/freshness expiry. For emergency offboarding,
use local account suspension or a Space membership block rather than waiting.

| Action | Effect |
|---|---|
| Pause Role | Policy operations denied; identity introspection and refresh remain usable. |
| Remove Role assignment | Human loses that Role; delegated connections cannot keep using it. |
| Suspend Space membership | Overrides all membership sources for that Space. |
| Suspend local user | Invalidates sessions/grants and fences owned devices and pending work. |
| Revoke one session | Ends that panel session, not unrelated MCP grants. |
| Revoke one MCP grant | Stops that connection; reenabling a Role cannot revive it. |
| OIDC back-channel logout | Validated signed session logout, with replay protection; not blanket grant revocation. |
| Unlink external identity | Tombstones the identity and revokes its associated sessions/grants. |

A response for obsolete upstream credentials is compare-and-set guarded: a slow
failed refresh cannot disable newer credentials established by a concurrent login.
Policy and ownership are rechecked after waits and while cached native output is
streamed, not only when a request begins. Errors preserve operation IDs when work
may already have been accepted; do not repeat an uncertain command with a new key.
Revocation is not rollback or a guarantee of terminating an already accepted OS
process. Use the original operation's stop/recovery path and inspect its outcome.

## Device and native-session ownership

A permitted human may enroll and manage their own device in the selected Space.
Enrollment is one-time; ongoing Agent authentication uses its machine credential,
not a browser cookie or IdP Token. Owner account/Space membership must still be
active. Losing membership prevents enrollment and authenticated work until restored.
A Space administrator may manage devices in that Space. Device lifecycle changes
are not implicitly delegated to ordinary MCP connections.

Creating a project mapping does not create disk directories or expand local
`allowed_roots`. The existing local canonical-path validation, read/write/task
restrictions, Shell allowlists, browser origins and Computer Use app/OS gates
remain. Secretaries can use explicit `projects.create` delegation for approved
nodes/paths; that does not confer permission to edit their own Role.

Native CLI sessions and uploads are bound to human, Space and originating grant
where applicable. Historical sessions without safe ownership metadata are not
silently imported as everyone’s private session. Artifact and stream links include
the per-tab Space selector; the server still verifies current ownership/membership.

Use separate Agent OS accounts, containers or VMs for mutually untrusted users.
A full Shell, build script or native Codex process can use the Agent OS account's
files, network, credentials and processes outside a project. A named Space or Role
is not an OS sandbox. Shared desktop/browser profiles imply shared execution trust.

## API integration and sharing

Human API requests send the opaque session cookie and selected `X-CodePier-Space`;
mutations also send `X-RD-CSRF` and must pass Origin checks. Download/SSE URLs use
`space_id` where a custom header is unavailable. Selection is checked against the
human's current memberships. The panel stores selection per tab, clears pending
private state when switching, and does not share a mutable active-Space session
between tabs.

New management route groups:

- `/api/iam/me`, `/spaces`, `/spaces/{id}/members`, `/assignments`, `/invites`.
- `/api/iam/users` for instance administrators; `/sessions` for the current human.
- `/api/iam/oidc/providers`, provider `/mappings`, `/oidc/reconcile`.
- `/api/iam/oidc/{provider}/link`, `/api/iam/identities/{identity}` for explicit linking.
- `/api/iam/share/{workflow|artifact}/{id}` for sharing one's own records with a Space.

Updates use returned `version`/`expected_version`; creates that support an
idempotency key require it. Handle a conflict by reading current state, not
blindly overwriting another administrator's changes. Last-owner and last-local-
recovery-admin protections apply inside the mutation transaction.

Private histories remain private even when humans share a Role. Workflow/artifact
sharing is explicit and still requires resource access. Interactive native,
browser and desktop sessions are not made public through that sharing endpoint.
Dashboard counts, audit/export, lists, search/downloads, approvals and SSE events
are scoped server-side, not merely hidden by the UI.

## Recovery and troubleshooting

Keep a tested local emergency administrator. On the Hub host, reset an existing
recovery account with:

```sh
python -m hub --data-dir /srv/codepier/data reset-password --username admin
```

The interactive password prompt avoids command-line secrets. Reset revokes that
account's sessions/grants and reenables local login. It does not transfer other
users' identities or change the IdP issuer. Do not run `init` against an already
initialized database or delete the encryption key to work around login errors.

Common errors:

- `ENTITLEMENTS_STALE`: upstream authority can no longer be freshly verified;
  reauthenticate the provider and check its UserInfo/refresh availability.
- `SPACE_FORBIDDEN`: removed/blocked membership, not necessarily an invalid Token.
- `ROLE_ASSIGNMENT_REQUIRED`: current human lacks Role-use/delegation eligibility.
- `ROLE_POLICY_DENIED`: adjust applicable Role/resource policy; do not repeatedly
  reconnect to bypass it.
- `OIDC_CONFIG_CHANGED` / `VERSION_CONFLICT`: reload and reconfirm current policy.
- Native ownership/capability errors: update Agent and use an owned session; do
  not remove ownership validation or import another user's cache.

## Reproducible validation

Protocol/HTTP/database and real temporary Agent tests:

```sh
python -m pytest -q tests/test_iam_integration.py tests/test_oidc_integration.py \
  tests/test_iam_agent.py tests/test_iam_hardening.py
python -m pytest -q tests/test_iam_ui.py
```

Actual isolated Authentik + Chromium, Docker Compose required:

```sh
python scripts/check_oidc_authentik.py --output dist/authentik-acceptance.json
```

The script provisions a uniquely named disposable Compose stack, two ordinary
users, a provider, a temporary Hub and a team Role. It exercises real login,
MCP consent, unchanged-token project expansion, Token refresh and group removal.
No deployment credentials are used. It cleans its own temporary stack and emits
non-secret check results. It is a separate test from the signed mock IdP suite.
The standard CI also retains full Linux/macOS regression, browser matrices,
Windows background recovery and source-package/dependency checks.

Generic provider acceptance is not a guarantee that your reverse proxy, MFA flow,
claim mapping or ChatGPT account-selection UI is configured correctly. Validate
those deployment-specific settings before inviting production users.

References: OIDC Core https://openid.net/specs/openid-connect-core-1_0.html ;
Back-channel logout https://openid.net/specs/openid-connect-backchannel-1_0.html ;
Authentik provider https://docs.goauthentik.io/add-secure-apps/providers/oauth2/ .
