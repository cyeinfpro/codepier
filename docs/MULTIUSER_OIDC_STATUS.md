# Multi-user / OIDC implementation status

**Integration checkpoint; full release acceptance is in progress. No live deployment or IdP configuration was changed.**

Integrated source commit: `db36ddacebedca853c44b554373ea595e8d90ab1`.
Base: `57f502d428472d8e7f6d71195a952b7157a8e5ea` plus the original PR #3 prototype.

## Implemented and connected to the application

This is no longer just the standalone `multiuser_policy.py` prototype. The real
application integration extends the existing Role engine through `hub/iam.py`,
`iam_schema.py`, `iam_api.py`, `oidc.py` and the existing Auth/runtime/routers.
The prototype remains a separate policy contract; production requests use the
persistent live IAM checks and the existing per-project Role rules.

- Transactional schema 8 migration: users' security state, personal/team Spaces,
  source-tracked memberships and Role assignments, resource/history ownership,
  Space-scoped aliases, cross-Space reference guards, external identities and
  server-side OIDC state. Existing Profile/grant/resource IDs, Token hashes,
  device credentials and the master encryption key are retained.
- Ordinary panel users no longer automatically become administrators. Human
  requests select a Space per request/tab and must retain current membership;
  MCP credentials carry a fixed Space. Instance and Space administration differ.
- Shared dynamic Roles: a user's Profile remains private/stable while an assigned
  Role is shared within a Space. Both projects and capabilities can expand on
  existing explicitly delegated connections without new OAuth. Role assignment,
  delegation eligibility, membership, user suspension and current policy are
  rechecked. All present/future projects is bounded by the grant's Space.
- Generic OIDC Authorization Code + S256 PKCE with encrypted server-side state,
  exact issuer/subject identity, nonce, signature/JWKS, audience/azp, at_hash,
  expiry/issued-at and browser binding checks. Discovery/token endpoints are
  limited to explicitly configured HTTPS origins; redirects are not followed.
- Explicit account linking with recent authentication and tombstones preventing
  a delayed callback from reviving an unlinked identity. No email auto-linking.
  Existing local recovery login remains separate. JIT is opt-in, never first-login
  administrator, and creates a private personal Space, not access to Legacy.
- Provider-scoped group mappings with assignment provenance, administrator
  membership blocks, bounded entitlement freshness and refresh reconciliation.
  Validated back-channel logout and replay handling. Panel logout is distinct
  from revoking all MCP connections. Local user suspension revokes sessions and
  grants and fences queued work; already accepted external commands are not
  represented as automatically rolled back.
- Space-aware OAuth/PAT consent and refresh. Consent requests are bound to the
  signed-in human, browser session and Space. IdP Tokens are never forwarded to
  ChatGPT or Agents. Old fixed grants retain their fixed semantics.
- Scoped dashboard/list/count/audit/export paths, operations and awaited results,
  workflows, artifacts/downloads, VPS, approvals and filtered event streams.
  Private histories do not become shared merely through Role/project membership.
- Self-service device ownership within a Space. Native sessions/uploads are bound
  to their human and Space on both Hub and updated Agent. Older Agent capability
  versions cannot silently supply ordinary users with shared private sessions.
- OIDC login, identity/session management, per-tab Space selection, membership,
  invitations, Role assignment, provider/group mapping and user suspension UI,
  retaining existing layout/accessibility components.

## Checkpoint tests actually executed locally

The following are separate checkpoints, not a summed final-suite count:

- 12 new real HTTP/database multi-user integration tests passed.
- 39 new signed-provider OIDC HTTP tests passed. They cover PKCE, issuer/audience,
  nonce, state/browser binding, callback replay, linking, group removal, freshness
  expiry and logout-token replay. The provider is a deterministic RSA-signed test
  adapter, NOT a live Authentik installation.
- One new actual temporary Hub/Agent test passed: create/read a new project with
  the same role Token, then remove assignment and prove a filesystem write cannot
  occur; an unrelated Space is denied.
- 97 legacy Roles/Profiles/continuous-access tests passed after updating genuine
  old-schema fixtures to migrate through schema 8.
- 162 workflow/workspace/VPS tests passed; 11 native history/SSE fixture tests
  passed at subsequent checkpoints.
- Python compilation, JavaScript syntax and git diff whitespace checks passed.

Local browser navigation is blocked by the execution environment. No local UI
success is claimed. New Chromium/WebKit desktop/mobile tests have been added;
GitHub Actions must supply the real-browser evidence. Local dependencies differ
from repository pins and omit some optional parser/lint packages. Full pinned
Linux/macOS regression and Windows Agent acceptance remain required.

Exact-source checkpoint validation:
https://github.com/tsunheimat/codepier/actions/runs/36110869167

The earlier green run 36104514001 validated only the original prototype commit;
it does NOT validate this integrated implementation.

## Remaining acceptance/hardening before release

- Complete pinned-dependency full repository CI, fix failures, and inspect its
  per-platform reports rather than inferring success from a selected test suite.
- Exercise native ownership and revocation across streaming, downloads and
  concurrent requests; check cross-Space idempotency and UI edge cases.
- Validate against a real isolated Authentik provider and the intended ChatGPT
  connection host; protocol-unit tests alone are not provider/host acceptance.
- Finish operational setup/migration/recovery documentation and publish final
  source hashes and test results. Do not treat this checkpoint as a deployment.

## Important boundaries

Roles, Space IDs and project mappings are not OS sandboxes. Shell/build commands,
Codex login, SSH agents and browser profiles use the Agent OS-account environment.
Use separate OS accounts, containers or VMs for mutually untrusted execution.
The Hub operator remains a trusted administrative party.

Group changes are applied on verified reconciliation or login, with a configured
freshness deadline; an IdP outage does not grant perpetual cached access. Generic
OIDC alone is not instantaneous directory deprovisioning. Panel logout and OIDC
back-channel session logout do not automatically revoke every MCP grant.

No ChatGPT chat or Project is a trusted role selector or hard security boundary.
The OAuth/CodePier credential determines the identity and Space.

## Correction retained from earlier delivery

Earlier claimed OIDC ZIP/patch/checkpoint logs were not recoverable, and packaging
commands had failed with FileNotFoundError. The unsupported 18/28/221 checkpoint
claims are withdrawn. This integration was newly implemented, executed locally,
and published as actual source; those earlier claims are not acceptance evidence.

The source checkpoint transfer verified every preimage/result hash and performed
only a non-force fast-forward of the feature branch. Temporary transfer payloads
and workflow are NOT included in the feature source diff.
