# PR #3: review fixes and regression evidence

This records the first review delta. See [PR3_SECOND_REVIEW_FIXES.md](PR3_SECOND_REVIEW_FIXES.md) for follow-up corrections to scheduling, capacity, batch consent, and key-refresh behaviour.

Reviewed baseline: `f8a34af4bca2370821ad4b11031cea37d3e351b7`.
Implementation commit: `c8c6c3742e749561a81b4c7d695d91bbb8989bc3`.
No deployment, merge, production identity-provider change or assertion relaxation.

## Findings and changes

1. **OIDC reconciliation starvation:** durable `oidc_sync_state` records every
   attempt, including early returns and failures, independently from verified
   `checked_at`/`fresh_until`. Eligible identities are ranked within each provider
   before taking the global 32-item batch, with four bounded concurrent requests.
   Retry delays grow from 60 seconds to at most 30 minutes. A finite lease survives
   cancellation/restart; configuration/login changes invalidate stale backoff.
   None of these attempt updates renew an entitlement.
2. **Outage audit volume:** failures are summarized once per provider/error/hour,
   with a durable suppressed-event counter; recovery is separately summarized.
   Audit state survives restart. Error text and Tokens are not logged.
3. **Group-claim consistency:** login and maintenance use the same UserInfo source.
   A required group or group mapping requires the configured claim in UserInfo.
   Missing claims reject new admission with OIDC_GROUPS_UNAVAILABLE; they do not
   masquerade as authoritative group removal for existing identities. Existing
   freshness still expires. An explicit empty group list still revokes authority.
   An ID-token-only group provider must configure UserInfo before group delegation.
4. **Login capacity:** server-derived socket-client buckets (32 pending requests),
   provider buckets (256 anonymous transactions), and an anonymous global pool
   (872 of 1000 slots) protect other clients/providers and reserve linking capacity.
   Linking is bound to an authenticated user (8 pending). Buckets are persisted;
   forwarded headers do not supply identity. Reservations precede metadata I/O
   and are removed on failure. No global oldest-first eviction cancels somebody
   else's legitimate login. Distributed flood protection still belongs at the
   deployment edge; these bounds are not a claim of unlimited DoS resilience.
5. **Key refresh amplification:** only an unknown signing kid or an invalid
   signature can request new keys. Semantic claim failures do not force refresh.
   Per-provider single-flight and a 60-second forced-refresh cooldown also bound
   invented-kid/signature floods. Failed metadata requests have a 30-second
   cooldown, preserve good cached keys and cannot overwrite newer configuration.
6. **Audit attribution:** a free-form target never selects a Space. Optional
   target_kind accepts only known resource kinds and performs one typed lookup.
   Verified caller context/credential scope takes precedence. Asynchronous device,
   operation and OAuth revocation events supply trusted target kinds explicitly.
7. **Permission query cost:** project IDs, assigned Role policies and created IDs
   are fetched in batches and evaluated in memory, not re-queried per project.
8. **Repeated checks:** nested synchronous read-only guards reuse one temporary
   authorization snapshot. It is not a session or whole-request cache, is closed
   before waits/stream yields, invalidates on SQL writes, and cannot survive in
   inherited task context. Each new call/event/post-await check reloads authority.
9. **Fixed-grant existence oracle:** the current administrator check runs at the
   top of the update transaction before grant/project lookup or validation.
10. **Unused policy model:** removed hub/multiuser_policy.py and its 72 tests of
    that unused model. Production IAM/Role/OIDC tests remain; the new tests target
    production routes, SQL scheduling and the existing policy engine. This is not
    the removal of failing production assertions to make CI pass.

## Migration and compatibility

Schema 10 adds retry/audit tables and a login client-hash column/indexes. Schema 9
upgrades preserve identities, memberships, credentials and master.key. Older
migrations still run in sequence. Existing migration assertions expect the new
version; a schema-9 reopen/recovery regression verifies retained data.

Existing dynamic secretary Roles still gain projects/capabilities without a new
OAuth grant. There are no broader resource permissions, cached stale entitlements,
CI skips, automatic test retries or production transport relaxations.

## Validation

The integrated local batch passed **465 tests**, with zero failures/errors/skips.
It includes both new modules: tests/test_oidc_review_regressions.py (40 cases)
and tests/test_iam_review_regressions.py (25 cases). Earlier 65- and 237-test
checkpoints overlap this result and must not be added as unique tests.

SQL trace measurements with four assigned Roles:

| Synchronous operation | 11 projects | 251 projects |
| --- | ---: | ---: |
| live_principal | 7 SELECTs | 7 SELECTs |
| actual GET /api/devices | 21 SELECTs | 21 SELECTs |

Query-count regressions persist measurements as JUnit properties. They test SQL
query count, not a claim of constant CPU complexity: evaluating policies against
all visible resources still requires bounded in-memory work.

Local dependencies differ from the repository pins. Run the complete pinned
CodePier CI, including real Authentik and Windows acceptance, on the published
exact head before merging. Local tests and a previously green baseline do not
certify these changes. The PR records the final workflow status separately.

Operational setup and trust boundaries remain in MULTIUSER_OIDC.md. Group claim
mapping and edge rate limiting are deployment responsibilities, not implicit
OIDC defaults. OIDC key rotation reference:
https://openid.net/specs/openid-connect-core-1_0.html#RotateSigKeys
