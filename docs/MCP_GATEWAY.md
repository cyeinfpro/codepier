> Integration update: this feature now uses the upstream 1.14.3 core. Gateway
> defaults to enabled, with no remote tools unless configured and explicitly
> authorized. New Role PAT/OAuth consent accepts `confirm_external_mcp: true`.
> The native public catalog is nine core tools plus two identity tools. Details:
> [Upstream integration](UPSTREAM_INTEGRATION.md). Earlier standalone-feature
> validation counts below are historical, not certification of the new tree.

# MCP Gateway — reviewed multi-backend tool routing

Status: first implementation on `9366b23` (1.13 + multi-user IAM), **disabled by
 default**. This is not an upstream 1.14 merge, a release or a deployment.

## Scope and architecture

The existing `/mcp` remains the frontend MCP server. `hub/gateway/` is a separate
client/router for remote MCP servers; native tools continue through the original
Runtime. No backend tool is added to `shared/contracts.py`, and no external call
is presented as an Agent operation or a guaranteed exactly-once execution.

```
MCP client -> existing Auth -> live IAM / Role / explicit grant consent
                                   |
                      approved, paginated tool catalog
                         /                    \
               native Runtime           gateway router
                                               |
                           immutable binding -> account -> connector
                                               |
                                    remote MCP HTTP client
```

`get_profile` keeps its stable identity and OpenAI profile metadata. External
profile metadata cannot replace it. When enabled, `get_access_context` also
includes a redacted `mcp_gateway` summary. External tools use `alias__name` and
retain their input/output JSON Schemas. Long names have a deterministic hash
suffix; namespace aliases are unique within a Space and cannot be retargeted.

### Implemented

- Multiple fixed HTTP MCP endpoints, private or explicitly shared backend accounts,
  reviewed tool bindings, and explicit `(binding_id, tool name)` Role rules.
- Manual backend bearer credentials, encrypted server-side. Blank credentials are
  allowed for a deliberately unauthenticated endpoint; they are not inferred from
  the frontend token.
- Modern `2026-07-28` requests and legacy Streamable HTTP initialization, with JSON
  and bounded SSE tool responses. Auto negotiation uses a read-only discovery
  probe, never a mutating call. Authentication, rate-limit and server failures do
  not cause protocol fallback/replay.
- Credential/grant-separated legacy sessions; limits on discovery, calls, responses
  and validation workers; current authorization before dispatch and result return.
- Tool metadata review, cursor-based frontend pagination, optional idempotency keys,
  encrypted private receipts, and a Panel page using existing styles and IAM.

### Not implemented / do not assume

Backend OAuth browser authorization/refresh, stdio process runners, sampling,
elicitation, roots, MCP Apps/resource URI proxying, MRTR input requests, resumable
SSE and Tasks are not advertised by this client. Unsupported results fail explicitly
without automatically executing again. Text, bounded PNG/JPEG/WebP images, selected
audio MIME types and structured tool results are supported.

There is **no generic repo/workspace/session resource sandbox** for an arbitrary
backend. A tool may access everything the selected backend credential permits.
Use backend-enforced resource permissions or a separately reviewed adapter. Two
users sharing one backend service account may see the same backend resources even
though their CodePier receipts and transport sessions are isolated.

No actual Kiln endpoint or credentials are included. Tool names in examples below
are illustrative, not assertions about Kiln's current interface.

## Enable and configure

Before upgrade, back up the application version, complete Hub state, database and
matching `master.key`. The optional tables use `meta.gateway_schema=1`, separate
from IAM `meta.schema=10`. Do not run old code against Role policies containing
`connector_rules`; rollback requires restoring the matching application and
pre-upgrade data together. Restoring Hub data does not undo external effects.

Set this in the managed Hub environment (`compose.yml` forwards it):

```dotenv
CODEPIER_MCP_GATEWAY=1
```

Restart the reviewed Hub build. Default `0` disables external routing and all
configuration writes; native MCP catalog/call behavior stays on its existing path.
No endpoint is contacted by upgrading or merely opening the tool catalog.

In **MCP 网关**:

1. The **instance administrator** selects the intended Space and registers the
   fixed endpoint. HTTPS is the default; no URL credentials, query tokens,
   redirects or environment proxy configuration are accepted. Host operators can
   deliberately approve specific private CIDRs, such as `10.0.100.30/32`.
   HTTP additionally requires explicit approval and all resolved addresses must be
   in approved private ranges. Never use a broad subnet when a host route suffices.
2. A human creates a **private backend account** using a token obtained for that
   backend. Only a Space administrator may create an explicitly shared service
   account. Personal accounts remain private even from other Space administrators.
3. Run **发现 / 审核工具**, inspect descriptions and schemas as untrusted data,
   select exact tools, and approve a namespace (for example `kiln`). Discovery
   saves a candidate catalog, not automatic publication.
4. A Space administrator assigns selected published tools to an existing Role.
   Existing project/device/other binding rules are retained. Use existing IAM to
   assign the Role with delegation eligibility and create a Profile/PAT/OAuth grant.
5. The human who owns that grant must select **查看政策并同意** under **我的连接委派**.
   This explicitly covers current/future reviewed connector rules of that Role.
   An older project-only consent is not silently reinterpreted. Fixed grants cannot
   become gateway grants through this operation.
6. Refresh/discover the client's tool catalog. A client which caches tools may need
   its own reconnect/catalog refresh; this version does not claim push invalidation
   of every client's local UI. Call authorization remains current even with a stale UI.

For example, a Role may contain:

```json
{
  "connector_rules": [
    {"binding_id": "gwb_0123456789abcdef0123456789abcdef", "tools": ["session_start", "session_get"]}
  ]
}
```

The tool names are **original backend names**, not public prefixed names. Rules
retain the binding/tool pairing; there is no wildcard or cross-product of all tools
and accounts. Private credentials are stored on accounts, never inside Role policy.

New tools must be reviewed, published and explicitly added to Role rules. When a
manual rediscovery observes changed schemas/descriptions or removed tools, stale
published tools are blocked until reviewed again. This is a check against the most
recent discovery snapshot, **not continuous attestation of backend code**.
Credential rotation invalidates the candidate catalog; rediscover before reuse.
Changes to an endpoint, account binding or namespace require a new identity, not
an in-place retargeting of an existing grant.

## Recovery and lifecycle

A gateway tool result includes a separate text receipt and `_meta.codepier/callId`.
Its `structuredContent` continues to obey the backend's advertised output schema.
Read the original result with:

```json
{"name":"gateway_call_get","arguments":{"call_id":"gwc_<original-id>"}}
```

`running`, `completed`, `tool_error`, `rejected` and `unknown` are distinct. A
connection failure after dispatch is **unknown**, not proof that no side effect
happened. Hub restart turns unfinished local receipts into `unknown`; it never
restarts the backend call. No mutating call is automatically retried, including
when an HTTP session expires. Disconnecting a client does not prove a backend task
was cancelled. There is no generic backend cancel or external rollback operation.

A custom client can opt into a bounded retry key in **tools/call params metadata**:

```json
{
  "name":"kiln__session_start",
  "arguments":{},
  "_meta":{"codepier/idempotencyKey":"one-deliberate-intent-001"}
}
```

This metadata is consumed locally and not forwarded or inserted into backend
arguments. Equal keys within the same grant resolve the original receipt; changing
arguments/tool/schema with the same key is rejected. JSON-RPC IDs are **not** used
as idempotency keys. Without an explicit key the gateway cannot identify two client
submissions as the same intention. Lost responses must not be repaired by inventing
new keys or assuming a tool was not executed.

Receipts remain restricted to their original user, Space and exact grant, plus
current Role/account/tool access. Revocation can intentionally make an old result
unreadable. Disabling a connector/account/binding or removing Role access does not
undo work already performed on the external server. Failed backend authentication
requires repairing that account, not logging out of CodePier.

## API surface

All writes require normal Panel authentication and CSRF; no management tools are
exported through external MCP. All paths below start with `/api/mcp-gateway`.

| Route | Purpose |
|---|---|
| `GET /` (without trailing slash) | Redacted configuration and own recent call metadata |
| `POST /connectors`, `PATCH /connectors/{id}` | Instance-admin endpoint registration and versioned enable/disable |
| `POST /accounts`, `PATCH /accounts/{id}` | Own private or admin-managed shared credentials; versioned rotation |
| `POST /accounts/{id}/discover` | Bounded remote discovery with post-await identity/config check |
| `POST /bindings` | Approve exact tools from an expected catalog hash |
| `POST /bindings/{id}/publish`, `PATCH /bindings/{id}` | Versioned review/update or enable/disable |
| `POST /grants/{id}/consent`, `DELETE /grants/{id}/consent` | Own role-grant delegation, expected Role version, explicit confirmation |

Existing Role endpoints accept `connector_rules`. Older Role editors omitting that
field preserve existing connector rules; the native editor also passes them back.
Sending `connector_rules: []` explicitly clears them. Account tokens are write-only
and never returned by management APIs or configuration audit records.

## Security and operational limits

All DNS answers are checked; an otherwise public hostname with an unapproved
private answer is rejected. The TCP destination is the verified IP, while the
configured Host and TLS SNI/certificate identity are preserved. Link-local, reserved,
multicast and IPv6 site-local addresses are rejected; private exceptions can only
be approved inside explicit private ranges. There is no caller-selected URL or
arbitrary process command. Endpoint registration belongs to the trusted instance
operator and is not an ordinary user's unrestricted network proxy.

Upstream descriptions and annotations are untrusted. External identity/UI/auth
metadata is removed; annotations conservatively describe possible side effects.
JSON Schema cannot fetch remote references. Input and output validation runs in
short-lived bounded subprocesses to keep backend-controlled regex/combinators off
the Hub event loop. The subprocess is a fixed-purpose validator, not a stdio MCP
runner or a sandbox for arbitrary backend programs.

| Limit | Initial implementation |
|---|---|
| Connectors / accounts / bindings | 64 / 128 / 128 per Space |
| Backend catalog | 128 tools, 16 pages; each schema 64 KiB and 4096 nodes |
| Role connector rules | 32, explicit tool names |
| Front catalog page | 100 tools, identity/catalog-bound cursor, 10-minute cursor expiry |
| Connection pool | 64 clients; idle 5 minutes; active clients not evicted |
| Active calls / discovery / validation | 32 / 4 / 4 per Hub |
| Network phase / validator deadline | 45 seconds / 8 seconds; platform limits additionally apply |
| Response / stored result / arguments | 2 MiB / 1 MiB / 256 KiB |
| Receipts | 1000 per Space; terminal records eligible for deletion after 7 days |

Capacity exhaustion fails before a new dispatch; it is not permission to increase
backend concurrency indefinitely. The initial quotas suit a limited rollout and
should be reviewed for a high-volume deployment. Expired/deleted idempotency records
cannot provide permanent exactly-once protection. Back up gateway tables and the
matching master key together. Do not interpret conservative size/schema/result
rejection as proof that the upstream operation did not run.

## Verification

```sh
python -m pytest -q tests/test_mcp_gateway.py tests/test_mcp_gateway_protocol.py
python -m pytest -q tests/test_roles.py tests/test_access_profiles.py tests/test_iam_integration.py tests/test_oidc_review_followups.py tests/test_iam_consent_cost.py
python -m pytest -q tests/test_mcp_gateway_ui.py
node --check web/mcp-gateway.js
```

Gateway tests run real Hub/IAM routes with deterministic network fixtures and one
real loopback TCP MCP endpoint. They cover two accounts with colliding names,
per-grant sessions, wrong user/Space/grant, policy combinations, consent, credential
separation, schema drift, token/Role/membership/account revocation during calls,
unknown results without replay, SSRF/DNS, pool limits and bounded validation.

The browser suite uses real Chromium/WebKit DOM and real Hub requests through an
ASGI adapter, not mocked API JSON; SSE is intentionally excluded from that DOM
suite. Full existing cross-platform and real Authentik CI remains required. Local
Python dependency versions may differ from repository pins. Passing fixtures do
not certify an actual Kiln, third-party OAuth or ChatGPT-host connection.
