# Multi-user / OIDC implementation and acceptance

The planned first-release application paths are implemented on PR #3. This is
not the earlier standalone policy prototype. Use the **current exact-head CI
results on the PR** as the acceptance record; older passing subsets are not a
substitute for the full workflow. No main merge, service deployment or production
IdP/ChatGPT configuration is implied by this document.

Base: `57f502d428472d8e7f6d71195a952b7157a8e5ea`.
Setup, migration, recovery and Authentik instructions: [MULTIUSER_OIDC.md](MULTIUSER_OIDC.md).

## Implementation coverage

| Requirement | Connected implementation |
| --- | --- |
| Human identity and storage | `iam_schema.py`, `iam.py`, `auth.py`: transactional schema 10, user security state, personal/team Spaces, memberships and assignment provenance, ownership and Space-scoped aliases. |
| OIDC login and account linking | `oidc.py`: discovery, confidential Authorization Code/S256 PKCE, state/nonce/browser binding, asymmetric ID-token/JWKS/audience/azp/time/at_hash checks, exact issuer/subject, explicit recent-auth linking and unlink tombstones. |
| Human administration | `iam_api.py` and existing routers: ordinary users, separate instance/Space administration, membership/invitation/assignment management, owner/recovery protection, personal session management and suspension. |
| Shared dynamic Roles | Existing Role/Profile/grant engine plus live IAM checks: private stable Profiles, shared Space Roles, explicit delegation eligibility and current action/resource policy. |
| MCP authorization | `oauth.py`, `mcp.py`, `auth.py`: CodePier remains the downstream issuer; consent binds the human, session and Space; IdP Tokens never become Agent or MCP credentials. |
| Resource and record isolation | Runtime and all existing panel/service routers enforce Space, current membership, project permissions and private record ownership; lists/counts/search/audit/downloads are scoped. |
| Queues and streams | Current policy is rechecked before delivery, on awaited results, per native cached export/event item, for approvals, and for each authorized SSE subscriber. |
| Devices and native execution | Per-Space enrollment/ownership; device-owner current status/membership gates; Hub and upgraded Agent both enforce native session/upload ownership. |
| Groups and offboarding | Provider-scoped mappings, provenance, refresh/UserInfo reconciliation, bounded freshness, validated back-channel logout, user/membership/assignment revocation. |
| User interfaces | `identity.js`, existing panel components: OIDC login/linking, per-tab Space selection, personal connections/sessions, members/invitations, assignments, providers/group maps and user suspension. |
| Compatibility | Existing IDs, Token hashes, Profile/grant ownership, device credentials and master.key are preserved; old fixed grants do not silently become dynamic role grants. |

Production authorization uses the persistent IAM checks and the existing Role
engine. The unused standalone multiuser_policy prototype and its in-memory-only
tests were removed during PR review; regressions now exercise these production
paths. See PR3_REVIEW_FIXES.md for the review-specific changes and validation.

## Secretary behaviour retained

A human owns a Profile within a Space. The Profile links to a shared Role which
that human is allowed to use/delegate. Existing role credentials may gain or lose
**both projects and capabilities** as the Role changes, without a new grant or
repeated OAuth. The first resource list is not a permanent ceiling.

All current/future projects means within that Role's Space. A shared Role does
not implicitly share private transcripts, approvals, browser/desktop leases or
other grants' histories. Assigning a different Role to a Profile is not the same
as editing the existing Role and does not silently retarget old credentials.

## Final hardening and regression coverage

- Operation/workflow/project-save replay keys are Space-scoped and survive reopen.
- Parent resource Space bindings cannot be changed underneath private histories.
- Sensitive operations recheck current policy after asynchronous waits and
  between individual native export/event frames, not just at initial admission.
- Device lifecycle and authenticated connections require an active device owner
  with current Space membership.
- Only an owner can suspend **or restore** an owner; last active Space owner and
  last local recovery administrator are protected.
- Unlink cancels pending link transactions. An already-consumed callback awaiting
  upstream data must recheck its transaction before provisioning and cannot undo
  an unlink from another local session. A genuinely new explicit link still works.
- OIDC communication has bounded total/read deadlines and response sizes. Stale
  UserInfo/refresh failures cannot disable a newer login or overwrite provider
  edits. Provider outages do not extend entitlement freshness.
- Authentik's isolated fixture explicitly enables `authorization_code` and
  `refresh_token`. Its API defaults to no grant types; discovery alone is not
  evidence that a provider accepts authorization requests.
- Browser failure evidence retains only safe navigation status/path and static
  input metadata, never passwords, cookies, authorization codes or Token values.

The new security regressions are in `tests/test_iam_final_regressions.py`.

## Reproducible acceptance

Run the normal workflow, not only the isolated policy tests:

```sh
python -m ruff check agent hub shared scripts tests
python scripts/check_full_regression.py --output dist/ci-results --workers 2 --timeout 900
python scripts/check_oidc_authentik.py --output dist/authentik-acceptance.json
```

Use the repository's locked dependencies. Build browser resources and install
Chromium/WebKit as specified in `.github/workflows/ci.yml`. That workflow includes
full Ubuntu/macOS regression, dependency/release checks, public source packaging,
real Windows scheduled-Agent recovery and a required isolated Authentik job.
Use one worker for the macOS native/browser regression, as the workflow does.

The isolated Authentik job starts a unique disposable Compose project, creates
two ordinary users and a provider, exercises real browser login/PKCE, private
Profiles, group assignment, separate downstream OAuth consent, existing-grant
project expansion, Token refresh and target-user group removal. It does not use
production credentials or change a deployed IdP.

During this continuation, the combined local IAM/OIDC/Role/Profile/migration
suite passed **251 tests** on the hardening source. A separate pinned GitHub
hardening run passed **82 tests**. They overlap, are checkpoints, and must not be
summed or substituted for the current full cross-platform report.

The source snapshot workflow archives only committed source with a commit ID and
SHA256 checksum. Temporary hash-verified transfer payloads used during development
are confined to a separate build branch, not this feature's source diff.

## Deployment and trust boundaries

Provider records are disabled and admission is closed by default. Upgrade does
not enable enrollment. Back up the old database and matching key before opening
it with new code; also retain a consistent full data-directory snapshot including
native caches. Roll back with the matching old code and complete old data, not by
pointing old code at schema 10. Follow the setup guide before enrolling users.

Project/Role/Space policy is not an OS sandbox. Shell/build tools, browser
profiles, SSH credentials and native CLI authentication retain the Agent OS-user
trust boundary. Use separate OS accounts/containers/VMs for mutually untrusted
execution. The instance operator remains a trusted recovery administrator.

Generic OIDC is not instantaneous directory provisioning: group changes take
effect after verified reconciliation/login or the configured freshness deadline.
An outage does not preserve stale privileges indefinitely. Panel logout and
OIDC back-channel logout do not automatically revoke every MCP grant; explicit
account/grant suspension is distinct. Already accepted commands are not rolled
back or guaranteed terminated merely by removing a permission.

Real production-provider configuration and the intended ChatGPT connection host
must still be checked after deployment. Repository acceptance does not mean a
live installation was changed. ChatGPT chats/Projects are not trusted role or
Space selectors and do not provide hard credential isolation.

Cross-Space Agent sharing, arbitrary external MCP/Kiln routing, Role inheritance,
SCIM provisioning and multi-Hub high availability are outside this release's
agreed scope; they are not prerequisites for the implemented OIDC/Spaces model.

## Earlier delivery correction

The earlier claimed OIDC ZIP/patch and 18/28/221 checkpoint logs were not
recoverable and are not evidence. The integrated implementation was subsequently
written and published on this PR. Only actual source and explicitly identified
validation reports should be used; the old three-file-only description and the
unsupported package links do not describe the current branch.
