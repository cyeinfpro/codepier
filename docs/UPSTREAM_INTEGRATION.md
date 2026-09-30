# Upstream 1.14.3 + IAM + MCP Gateway integration

This is an **unreleased fork integration**, not an upstream release or deployment.
The repository was not deployed when this integration was requested. Public MCP
API changes are intentional; no live Hub data or external accounts were changed.

## Exact sources and history

- Fork main: `9366b23fed7f374cf744f1cd461c8192428c910d`.
- Gateway PR #4: `68aa9c8d2bf279b4579779f304b28410f23a3fe9`.
- Upstream: `84e24d53b33ca24e3d54609f65df8c14c347f29e` (1.14.3).
- Former common ancestor: `4ed71592ee3bcb4291cfc122f2a9f3f3492d52bc` (1.13.0).

The integration commit retains Gateway and upstream as parents. All 19 upstream
commits after the former common ancestor are ancestors of this branch. This is
not an `ours` merge: upstream API decomposition, resource queue, file-import
security fixes, protocol negotiation, checked Store, call log, build pipeline,
locked dependency sets and native fixes are incorporated as actual source.
Ancestry is not a claim that every functionality/platform has passed acceptance.

## Composition and execution boundaries

`hub/app.py` creates services and attaches routers. Project/device/settings/
account/activity/system HTTP logic is in `hub/api/`; IAM, Role and Profile routers
are attached alongside them. OIDC is the human relying party; `runtime.oauth` is
the separate downstream MCP issuer. A native Agent is not an external MCP server.

`hub/principal.py` owns the only Principal type. Credential expiry/revocation,
Space membership, user/identity epochs, Profile binding and effective Role policy
are recomputed through the same refresh pipeline. Neither a stale `admin` flag
nor a caller-supplied Space selects authority. IAM request-local read scopes do
not survive an await; completed DB work is revalidated at subsequent boundaries.

Main Store uses upstream's owned lock, checked connection/cursor and dedicated
single worker. Complete synchronous authorization/write phases use `Store.run`;
no transaction is held across network IO. Cancellation drains admitted DB work.
Nested failures make the enclosing transaction rollback-only. IAM/OIDC, Gateway
and native API paths use this model; native history retains its own database.
Worker notifications return to the owner event loop, after transaction commit.

## One public tool router

`hub/tool_router.py` explicitly combines the native public catalog and reviewed
external exports. Unknown native names do not fall through to a remote backend.
The default catalog has the nine upstream core tools plus `get_profile` and
`get_access_context`. `get_profile` remains the stable CodePier identity tool.
The core tool count is nine; the default public count is eleven. Once explicitly
authorized, external exports and their receipt helper add to the public count.

Legacy public names such as `fs_read`, `shell_exec`, `devices_list` and
`projects_create` are removed. Their internal domain implementations remain for
Agent/Panel reuse. Use `workspace(operation="devices")` and
`workspace(operation="project_create", options={...})` for delegated management.
Native `full`, `coding` and `core` catalog selectors all return the same catalog;
they no longer select different legacy APIs or different identities.

Catalog pagination binds the cursor to its catalog and credential identity.
Capabilities shown in `tools/list` do not replace dispatch-time authorization.
External tool schemas do not get registered into `shared/contracts.py`, and
external operations never enter the Agent delivery/journal queue.

## Gateway and consent

Gateway is enabled by default; no configured/authorized exports means native-only
behavior. `CODEPIER_MCP_GATEWAY=0` is an explicit instance kill switch, not an
alternate implementation of the MCP server. Private backend accounts, reviewed
bindings and matched Role `connector_rules` are retained. Incoming credentials are
not passed to upstream MCP servers. Endpoint registration is instance-admin-only;
private network/HTTP access requires explicit connector policy.

When creating a Role PAT or deciding OAuth consent, `confirm_external_mcp: true`
explicitly includes external MCP delegation in the same transaction as that grant.
The UI offers the choice in its Role selector. The default is false; non-boolean
values and fixed grants cannot acquire external delegation. Existing explicit
consent/revoke endpoints remain usable; a Role edit cannot fabricate consent.

Gateway discovery and dispatch recheck account/version/policy after waits;
mutations are never automatically replayed after uncertain results. Result
retrieval is exact-user/Space/grant scoped and rechecks current permission.
A saved encrypted routing secret keeps idempotency and cursor signatures stable
across offline key rotation. Account passwords/tokens and result content are never
part of plaintext call-list projections.

This version remains HTTP tools-first. Backend OAuth enrollment/refresh, stdio
process runners, generalized resources/Apps proxying, sampling, roots, elicitation
and a generic resource-level sandbox are not implemented. A real Kiln endpoint
and account have not been configured or tested. Existing backend-account/tool
restrictions are not a claim of repo/workspace-level isolation.

## Privacy and key rotation

Call-log pages, watch-ID updates, filters and details respect Space and private
record ownership. Instance admin recovery is still scoped to the selected Space;
Space administration alone does not reveal another person's private transcript.
Internal event audiences are not serialized to browser SSE payloads.

The key rotation inventory now includes OIDC client secrets, cached upstream
identity credentials and pending PKCE verifiers, Gateway backend credentials,
private results and its routing secret, alongside the upstream cipher columns.
Matching OIDC scheduler credential hashes move with their ciphertext in the same
transaction; re-encryption neither renews entitlement freshness nor changes the
logical attempt owner. Startup fails closed if the key is missing with any
persisted encrypted extension data.

## Build and data

Edit `web/index.source.html`, not generated `index.html`. IAM/Gateway styles and
scripts are in the same hashed asset pipeline. Build in this order:

```sh
cd web/mcp-apps
npm ci --ignore-scripts
npm run build
cd ../..
python scripts/build_web_assets.py
python scripts/build_web_assets.py --check
```

Hub dependencies retain the upstream hash-locked sets plus the universal
`PyJWT[crypto]==2.15.0` wheel needed by IAM. Python requirements are installed from
reviewed pins; the network-dependent universal lock regeneration must be checked
in CI before a release. No deployed environment is updated by these commands.

Fresh Store initialization retains IAM schema 10 and Gateway schema 2. Historical
migration tests remain when they are cheap and meaningful, but API compatibility
with the old public MCP catalog is deliberately not retained. No startup routine
automatically erases an unsupported database. Do not use an old binary against
new state. For disposable development data, explicitly choose a new data directory;
for valuable data, retain its matching key and complete state backup.

`RELEASE.json` identifies this distribution as an unpublished development build
based on 1.14.3. Numerical VERSION stays the upstream protocol baseline for now;
it is not a claim that this tree is the published upstream archive. Formal fork
release numbering is separate. The panel updater defaults to `tsunheimat/codepier`
and does not fall back to upstream or main when a reviewed fork release is absent.

## Verification boundaries

Local unit, service, real HTTP/Agent and deterministic external-MCP test commands,
exit statuses and JUnit records are retained with the development delivery.
Additional integration regressions in `tests/test_upstream_integration.py` cover
private call-log watch IDs/cursors, rejected public aliases, first PAT/OAuth
external consent, key rotation across all domains and off-loop OIDC entitlement
work. Passing a test subset does not certify real IdP, real external MCP, browser
engines, Windows or macOS. Current-head full CI and explicit platform acceptance
remain the merge/release gate. This document does not assert those runs are green.

### Local checkpoint, 2026-09-27

| Run | Result |
| --- | --- |
| `pytest tests -m 'not integration and not browser'` | 1,665 passed; 1,318 intentionally outside this run's tier |
| Real Hub/Agent, core tools, call-log fullstack, computer and bridge selection (`-m 'not browser'`) | 42 passed; 13 browser cases outside this run |
| Explicit protocol, file-import, native ownership, Gateway, OIDC and integration-boundary selection | 217 passed |

The three JUnit files contain **1,731 distinct passing test cases**, after
removing overlap by `(classname, name)`. Do not add the row counts and call that
an independent test total. Seven warnings in the first run and two in the last
were not suppressed as failures; their output is retained in the delivery.
Ruff, configured mypy (three typed source files), JS syntax, locked Prettier,
MCP/core frontend builds, integration assets and public source release-input
checks passed locally. A clean worktree reproduced the reviewed Git tree from
pinned upstream/Gateway sources and the locked build, byte for byte.

There is no local browser/platform/real-Authentik acceptance claim. The full
repository CI remains required; this checkpoint is not authorization to deploy.
