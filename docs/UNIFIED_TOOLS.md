# Unified tools: `query` and `act`

The default MCP catalog (`/mcp`, profile `unified`) publishes at most three tools, filtered by current scopes:

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
`get_access_context`, `project_knowledge_query`, `project_knowledge_build`, or one public collaboration tool) and then runs through the unchanged `runtime.invoke`
path (`hub/unified_tools.py`, `hub/mcp.py`). Authorization, scope checks, idempotency records, recovery, audit and
rendering are the same code. `tests/test_unified_tools.py` enforces, against the live registries:

* every legacy facade operation (`hub.tool_discovery.catalogs()`), both standalone knowledge tools, and every one of the 126 public collaboration
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
* **Lean presentation.** Unified responses de-duplicate identical log output only in known execution receipts
  (`result.data.output` points to `output`) and rewrite next-call hints to unified addresses. Recovery evidence,
  including `recovery`, `failure_context`, `continuation` and `log_ref`, remains available after success.
  `?verbosity=full` keeps expanded scheduling metadata and the complete receipt.

## Compatibility

* `?profile=core`, `full` or `coding` restore the legacy tool catalogs and response layouts.
* Every legacy tool name (including the 90 legacy collaboration names) remains callable; it is simply not listed
  in the unified catalog.
* Initialize instructions describe the unified tools when the profile is `unified`.
* Connection evidence compares the served catalog with the router default, which is now `unified`.

### Opt-in compact view

Use `?view=3` on the unified connection to try the smaller tool responses. Successful inline reads have
the same shape as direct reads. Project open returns compact document and directory lines and keeps the
full context ID, baseline reference and Git commit. Project lists group aliases by shared defaults.
Sync search groups matches by path. Pass `args.format="structured"` on tree, sync search or project
list for object rows. Catalog help lists collaboration action names; action-specific help lists their views.

Successful single-file reads and command runs default to `text=compact&mirror=meta` in view 3. The text block
contains a one-line JSON header and the raw body. `structuredContent` contains the header and a
`content_in` or `output_in` marker. Use `?view=3&text=json&mirror=full` for a full JSON mirror, or
`?verbosity=full` for expanded receipts. Reads keep their SHA, preserve complete lines and report
`next_offset` when a page is limited. Successful reads retain their original operation ID.

Lean responses omit default fields such as `truncated: false` and `next_offset: null`. A successful
command reports `exit_code: 0` and retains its operation ID and output sequence. Errors and pending
or uncertain operations retain their recovery information. Successful operations retain it too, so a text-only
client can inspect the original operation without replaying it. The default connection uses view 2 until
the text-only body has been verified in both Claude and ChatGPT.

### Output fidelity

File bodies remain exact, paginated slices with their original SHA and `next_offset`.
Native execution logs use the declared `log` policy (`exec`, `shell_exec`, `ssh_exec`,
`tasks_run`, `vps_exec`); all other built-in tools default to `exact`. In particular,
legacy `git_status` / `git_diff` / `git_log` receipts remain exact. External gateway
results bypass native presentation and budget rewriting. Unknown nested payloads are
never inferred to be logs from their field names.

Complete, confirmed successful receipts omit redundant success diagnostics and recovery
objects in lean mode. Failed, timed-out, cancelled and unfinished receipts keep their
diagnostics. `?verbosity=full` preserves the full receipt. Native output defaults to
`output=raw`; `text=compact` changes packaging, and `mirror=meta` avoids duplicating the
body in structured content.

Output is a bounded saved tail, not a complete stdout archive. Losses append to
`output_truncated_by` in order: `upstream` (earlier bytes were not retained), `limit`
(the requested page), and `budget` (the final JSON-RPC size cap). `output_chars` is the
saved tail length and `output_range=[start,end]` addresses its Unicode character offsets,
with an exclusive end. The response budget keeps a contiguous source window, without
inserting omission text or joining unrelated head and tail sections. Default tail reads
prefer complete lines; an oversized single line can use a character window to make progress.

On terminal jobs, `query(action="job", project=..., ids=[...],
args={"output_offset": 0, "output_limit": 20000})` reads an explicit saved range. The
requested range is exact unless the response budget narrows it; always check
`output_range`. Follow `output_next` to read older windows, prepending them until the
start reaches zero. This works on the same connection and never reruns the command.
Running/unknown jobs reject `output_offset`; use `after_output_seq` while they run.
Offsets cannot be combined with `after_output_seq`, omitted output, or a zero page size.

`?output=normalized` explicitly enables lossy ANSI/progress/repetition normalization.
Only changed text receives `output_original_chars`, `output_original_bytes` and
`output_original_sha256`, describing the raw source window in `output_range`. If the
budget narrows a normalized page, its fingerprint is recomputed from that narrower raw
window. `output_next` first requests that raw range; explicit `output_offset` reads
bypass normalization even on a normalized connection. Large raw windows can require
additional budget-limited pages. `output_original_scope` is retired.

New Agents also report `output_total_bytes` and `output_total_sha256`, accumulated from
received pipe bytes (stdout with stderr merged) before UTF-8 decoding, credential redaction
or tail limits. These are stream evidence, not hashes of the displayed string, and do not
recover discarded bytes. They also describe the bytes received before timeout/cancellation;
older Agents may omit them. No database migration is needed.

If `upstream` is present, earlier output was not saved. When a complete log is needed,
explicitly redirect a new execution to a file in `$CODEPIER_RUN_DIR`, then page that file
using the ordinary SHA-protected file reader. Full log files are opt-in and are not
silently persisted by the Agent. The connection option `?verbosity=full&output=raw` is
available to people configuring a client; it does not bypass authorization or storage limits.

Agents with tree options list Git-visible paths by default. `args.scope="all"` includes ignored files
that pass filesystem protection rules. `args.per_dir` increases the default 40 entries per directory.
The `omitted` map names directories with capped results. Narrow `path`, increase `per_dir` or use
`next_offset` to inspect more. Older Agents return the previous filesystem listing.

### Native attachments and help pointers

Native attachments use the top-level `file` field: `query(action="source_check", file=..., ...)`
previews the source without saving it; `act(action="import", file=..., ...)` imports it under
the existing write policy. Both catalogs advertise `openai/fileParams=["file"]`. Keep
`download_url` and `file_id` in that native object, never move it into `args` for host
attachment resolution. Existing nested callers remain compatible, but sending the same
field at both levels is rejected. Import destinations remain `path`.

Capability-index help pointers translate both the help call and its queried legacy
tool/operation to unified action/view addresses. Following project-open → capability
index → help preserves the original scope-filtered schema and does not authorize execution.

### Project knowledge

`query(action="knowledge", project="P", args={"query": "routing", "paths": ["src/app.py"]})`
reads existing knowledge (default view `query`), without building or refreshing it.
`act(action="knowledge", project="P", idempotency_key="knowledge-refresh-001",
args={"max_files": 2000})` builds/refreshes it (default view `build`).
Both route to the unchanged standalone tools and preserve their typed contracts, project
authorization, source-SHA checks, idempotency and recovery. Build requires read + write;
read-only credentials neither discover nor execute build.

Discover the exact schema with `query(action="help", args={"action": "knowledge",
"view": "query"})` (or `view: "build"` with write scope). Standalone help carries the original
input/output schema plus a `unified_call` address. Existing `project_knowledge_query` and
`project_knowledge_build` names remain callable and listed in explicit core/full/coding profiles.
See [Project knowledge](PROJECT_KNOWLEDGE.md) for limits, freshness and trust boundaries.
