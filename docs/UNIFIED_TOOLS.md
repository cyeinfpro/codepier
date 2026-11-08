# Unified tools: `query` and `act`

The default MCP catalog (`/mcp`, profile `unified`) publishes three tools, 5,368 characters in total
(the previous default `core` + collaboration catalog was 156,407):

| Tool | Annotation | Purpose |
| --- | --- | --- |
| `query` | `readOnlyHint: true` | Reads files, receipts, project context, GUI state and collaboration records. Never changes anything. |
| `act` | `destructiveHint: true` | Runs commands, edits files, manages worktrees/workflows, drives GUI sessions and writes collaboration state. |
| `get_profile` | `readOnlyHint: true` | Unchanged. Kept because hosts use its `openai/profile` marker as the identity protocol. |

Two action tools is the minimum that keeps every capability: MCP read-only hints exist per tool, so a single
tool would force hosts to either approve every read or auto-approve every write.

## How a call is shaped

```json
{"action": "file", "project": "MCP", "path": "README.md"}
{"action": "job", "view": "wait", "ids": ["<operation id>"]}
{"action": "exec", "project": "MCP", "idempotency_key": "build-001", "command": "make test"}
{"action": "collab.send", "view": "room_reply", "args": {"handle": "cph1....", "body_text": "Done"}}
```

* `action` picks the capability; `view` picks a sub-operation (the first view is the default).
* Frequent fields are top level (`project`, `workspace_id`, `path`, `offset`, `limit`, `ids`; for `act` also
  `idempotency_key`, `command`, `cwd`, `target`, `timeout_seconds`, `edits`, `content`, `expected_sha256`).
* Every other field goes in `args`, unchanged from the legacy schema.
* `query(action="help")` lists the visible actions and views; `query(action="help", args={"action": "...",
  "view": "..."})` returns the exact legacy schema plus a `unified_call` note.
* Collaboration views are the original public action names (for example `collab.work` view `execute` is the
  managed-lease step executor).

## Guarantees

Each unified call is translated (`shared/unified_contracts.py`) into exactly one legacy public call
(`workspace`, `read`, `write`, `edit`, `exec`, `process`, `vps`, `browser`, `computer`, `get_profile`,
`get_access_context`, or one public collaboration tool) and then runs through the unchanged `runtime.invoke`
path (`hub/unified_tools.py`, `hub/mcp.py`). Authorization, scope checks, idempotency records, recovery, audit and
rendering are the same code. `tests/test_unified_tools.py` enforces, against the live registries:

* every legacy facade operation (`hub.tool_discovery.catalogs()`) and every one of the 115 public collaboration
  actions has a unified address;
* every field of every internal target model stays expressible;
* `query` only reaches targets with `read`/`devices.read` scope or side-effect-free computer reads
  (`browser_snapshot`, `computer_observe`, `computer_apps`), and cannot capture a baseline;
* discovery follows scopes (for example a read-only credential sees no `act` core actions);
* the published catalog is at most 10,000 characters.

`tests/test_unified_tools_http.py` checks the same over real HTTP (both protocol eras), including that a unified
call returns the same structured result as the legacy call.

## Additions that do not widen authority

* **Inline waiting.** Unless `wait=0`, a pending receipt is waited on (same operation ID, `process wait`, at most
  `wait` seconds, default 30, max 60). Waiting never restarts or replays work.
* **Collaboration handle.** When a collaboration result issues consumer or connection credentials, the response
  also carries `handle` (`cph1.` + payload encrypted with the store cipher, bound to user and space, 7-day
  expiry). Passing `args.handle` fills `consumer_session_id`, `consumer_fence`, `consumer_proof`,
  `connection_proof`, `binding_generation`, `member_id`, `conversation_id` and `dot_id` where the action accepts
  them. Explicit fields win. Credentials are still returned once at begin/join (native host subscriptions need
  them); fences, proofs and version checks are still verified by the original handlers.
* **Lean presentation.** Unified responses drop duplicated output (`result.data.output` when equal to `output`),
  successful-terminal `recovery`, `failure_context` and `continuation`, and rewrite next-call hints to unified
  addresses. `?verbosity=full` keeps the complete receipt.

## Compatibility

* `?profile=core`, `full` or `coding` restore the previous catalogs exactly.
* Every legacy tool name (including the 90 legacy collaboration names) remains callable; it is simply not listed
  in the unified catalog.
* Initialize instructions describe the unified tools when the profile is `unified`.
* Connection evidence compares the served catalog with the router default, which is now `unified`.
