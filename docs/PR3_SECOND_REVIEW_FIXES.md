# PR #3 — second-review fixes

Reviewed baseline: `22d548767c0686bcb3b95a4c3a0bf13590972b06`.
This delta fixes four reproduced defects and makes the group-provider compatibility
choice and key-rotation tradeoff explicit. No deployment or production IdP change.

## Corrections

1. **Pending capacity only.** All client, linking, provider, anonymous and total
   capacity queries now count `used=0`. A consumed callback remains a short-lived
   replay/account-linking tombstone, not a reservation. Invalid/replayed state
   cannot release a different transaction. Actual successful and rejected ID-token
   callbacks free their slots; genuinely pending logins still hit the quotas.
   Socket-IP rate throttles remain separate, and trusted proxies must be configured.
2. **Contain per-identity exceptions; join the entire batch.** Unexpected sync
   exceptions, including Fernet InvalidToken, become `OIDC_RECORD_INVALID` without
   logging raw exception text. They reach the normal persistent backoff/audit path.
   Cancellation is not caught. The batch gathers all outcomes before propagating
   a claim/completion storage failure, so it cannot release its sync lock while
   sibling requests continue. Cancellation also waits for child cleanup; finite
   retry leases remain, and failed/cancelled work never renews entitlements.
3. **Refresh transfers attempt ownership.** After its token compare-and-swap wins,
   sync transfers the scheduler's credential hash in the same SQL transaction,
   retaining the attempt lease and accumulated failure count. Completion validates
   that specific persisted generation, not the now-obsolete pre-refresh ciphertext.
   A subsequent UserInfo failure therefore records backoff/audit; successful
   recovery after a refresh emits the recovery summary. A concurrent new login,
   provider edit, suspension or different attempt still invalidates old completion.
   Merely reading whatever ciphertext is current after an await is not safe proof
   that a particular attempt owns it, so this implementation does not do that.
4. **Batch consent policy evaluation.** Profile creation/editing, fixed PAT
   issuance and fixed OAuth consent read one permissions map for their selected
   projects rather than rebuilding the entire Space map for each project ID.
   There is no cache spanning requests, awaits or mutation transactions. An
   unauthorized selected project rejects the complete delegation; later role
   removal remains effective for the next request.

## Provider compatibility decision

Keep the explicit **UserInfo-only** group policy; do not silently reinstate
ID-token-only groups or refresh freshness from a stored ID token. This is a
**breaking compatibility change** for pre-review configurations using groups only
from an ID token. Microsoft Entra's UserInfo response is not customizable and does
not return groups; adding ID-token optional claims cannot fix that. This release
has no Microsoft Graph entitlement adapter. A trusted broker supplying verified
UserInfo groups or a separately reviewed adapter is needed for Entra group-based
admission. OIDC login without CodePier group policy and manual memberships is a
separate, explicitly admitted configuration, not a suggested bypass of a required
group restriction.

The setup guide and CHANGELOG now say this explicitly. Existing group policies
produce a non-secret warning on Hub startup, including schema-10 reopen. The
provider editor, mapping editor and discovery-check result warn before operators
mistake discovery success for group compatibility. Anonymous provider discovery
still returns only provider IDs and labels. The local recovery login is unchanged.

Reference: https://learn.microsoft.com/en-us/entra/identity-platform/userinfo

## Key-rotation follow-up

Known cached keys are validated before any forced-refresh limit. With an unexpired
verified discovery document, refresh uses only its approved JWKS URI. A failed
optional discovery fetch therefore does not block key rotation through that
still-trusted URI, nor does a key-only fetch extend the discovery cache lifetime.

There are at most two successful forced key-fetch probes per provider per 60-second
window, with single-flight serialization. A single garbage-kid probe no longer
uses all rotation capacity. Network failures use a separate 10-second retry
cooldown and do not consume successful-refresh slots. Arbitrary-kid floods remain
bounded; the existing 30-request regression still produces only two HTTP requests
(now key-only instead of one discovery/JWKS pair).

It is not possible to authenticate a genuinely unseen key using an unrelated
cached key. If both successful probes are consumed, a new key may require a retry
when the window resets. This is an explicit bounded anti-amplification tradeoff,
not a claim of zero-delay key rotation under attack. Publish overlapping keys in
advance. Signature, claims, freshness and per-request authorization checks remain.

## Tests and measurements

`tests/test_oidc_review_followups.py` and `tests/test_iam_consent_cost.py` exercise
the real OIDC router, SQL scheduler and consent routes. No new mock-only policy
engine, CI exclusion or automatic retry is introduced. Before applying the fixes,
22 new cases yielded 14 failures reproducing all four reported defects (with eight
control cases passing). Later cases add key rotation, storage failure draining,
provider warnings and failed callback coverage.

Measured policy evaluations with one Role on the actual HTTP endpoints:

| Endpoint | Baseline 11 projects | Baseline 251 projects | Fixed 11 | Fixed 251 |
| --- | ---: | ---: | ---: | ---: |
| Profile creation | 143 | 63,503 | 33 | 753 |
| PAT creation | 154 | 63,754 | 44 | 1,004 |
| OAuth consent | 165 | 64,005 | 55 | 1,255 |

These are evaluations, NOT SQL counts or latency claims. They show a constant
number of whole-Space maps per request rather than a per-project rebuild.

Local impacted IAM/OIDC/Role/Profile/transport regression passed at a checkpoint;
local dependencies differ from repository pins. The exact current-head full CI
and the real Authentik/Windows/browser jobs are the acceptance record on the PR.
Earlier green baseline CI does not certify this delta. Test suites overlap and
must not be summed as independent totals.

## Compatibility and scope

No schema change is required by this delta: the existing schema-10 retry fields
carry refresh ownership atomically, and consumed transaction records keep their
original replay semantics. No grants, keys or role policies are rewritten. The
secretary's dynamic project/capability expansion and cross-Space/private-history
boundaries remain unchanged. Review the current PR head, not the old baseline.
