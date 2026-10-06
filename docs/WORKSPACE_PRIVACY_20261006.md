# Workspace privacy review — 2026-10-06

This is a source-level review and regression guide. It does not assert that a live
Hub, Agent or ChatGPT client has been upgraded. The starting main commit is
`6ebf3bd178dc3f2b37aea34f9dfff6af86a0a166`; existing uncommitted ChatGPT
workbench retirement changes are preserved.

## Authority boundaries inspected

- Current user status, identity freshness and active Space membership gate panel
  requests. Delegated MCP credentials remain bound to their original Space and
  exact grant; Role/Profile policy and project permissions are evaluated live.
- A shared Role/project does not implicitly share private operation history,
  native transcripts, workflows or artifacts. Explicit Space sharing is separate.
- Durable operations and standard Tasks reauthorize receipt/result access and
  queued admission. Artifact downloads recheck before and after Agent round trips.
- Agent file tools apply project-root/path checks. Full Shell, external tools and
  the host OS account remain a different trust boundary, not a filesystem sandbox.

## Confirmed findings and changes

### Revoked authority left the old panel visible

The SSE endpoint previously stopped without a client invalidation signal after a
membership/session failure. Its error handler displayed a reconnect indicator;
loaded private text could remain on screen. Explicit logout also left project,
device, settings and grant snapshots in JavaScript state. This was a stale-client
privacy defect: the tested server still denied subsequent unauthorized reads.

SSE now emits only an empty `access_revoked` control event before closing when
current authentication fails or effective project capabilities contract. The
same decision runs for every event and idle heartbeat. Role policy edits wake the
stream; harmless renaming and capability expansion do not count as contraction.

The panel clears private local work and common Space snapshots on that signal.
A rejected reconnection verifies authority using an ordinary read; a confirmed
auth/Space denial clears the panel, while a transient network failure preserves
unsaved local work. Old stream callbacks cannot clear a newer session/Space.
Switching, logout and membership removal reuse the common snapshot cleanup.

Behavior change: a verified loss of authority, including session expiry reported
by the stream, clears unsaved local drafts and attachments. This is local UI
cleanup, not remote deletion or cancellation of running commands.

### Audit export selected the default Space

The download anchor omitted the selected Space because normal API headers cannot
be attached to a clicked link. For a user entitled to both Spaces, an export from
the team view therefore selected the server's default Space. Tests also verify
that a user without membership receives 403 without audit data; this is not a
cross-user authorization bypass.

The anchor now includes the current `space_id`, and fails closed when no Space is
selected. Server membership verification and no-store response policy remain.

### File privacy policy differed by entrypoint

Skill resources rejected exact credential filenames, while regular file reads,
tree/search/checkpoint and artifact registration could include the same synthetic
files. Known local assistant transcript directories were also traversable.

The shared path predicate now protects exact `auth.json`, `credentials.json`,
`.credentials.json`, and `secrets.json` names and the
`.codex/sessions`, `.codex/archived_sessions`, and `.claude/projects`
subtrees, case-insensitively and at nested locations. Skills use this same policy
instead of maintaining duplicate filename checks.

Saved search hits and previously registered artifact snapshots recheck this
policy before returning content. Backup listings omit newly protected paths.
Existing artifacts/backups are not deleted.

This is intentionally filename/path-based. Files named `auth.example.json`,
`credentials.sample.json`, `secrets.template.json`, and `auth.schema.json`
remain ordinary source files; these names do not make real secrets safe to share.
Project `.codex/skills` and `.claude/skills` remain available. A legitimate
source file named exactly like a credential file is now rejected by regular file
tools, matching the existing skill-resource privacy policy.

## Reproduction and verification

New tests use temporary projects, synthetic sentinels, disposable databases and
mock browser transport, plus the existing real Chromium/WebKit fixtures. They
never read personal credentials, provider histories or a production database.

Before the fixes, the initial eight panel/SSE/export cases and nine file-policy
cases all failed for the intended assertions. The expanded focused suites test:

- membership, session and Role revocation, capability reduction, expiry and idle
  heartbeat checks;
- empty invalidation signals, watcher cleanup, stale callbacks, rejected reconnects
  and ordinary network failures;
- selected-Space audit exports and unauthorized-Space failure;
- shared exclusion rules across reads, scans, checkpoints, artifact registration,
  retained search hits, and registered snapshot downloads;
- readable source examples and both supported project skill directories.

Run the new suites with the existing compatible environment:

```sh
python -m pytest -q tests/test_workspace_privacy.py \
  tests/test_workspace_privacy_ui.py tests/test_workspace_private_files.py
```

The existing audit, IAM, session/editor, filesystem, artifact, skill, context,
import and standard-Task regressions remain part of aggregate verification.
See the delivered review report for the final run's exact counts and fingerprint.

## Limits

This does not erase data already downloaded, copied, sent to a client, stored in
old Hub operation results/logs, or preserved in OS backups/SQLite pages.
An offline page cannot receive revocation until communication resumes.
File-name protection cannot recognize every arbitrary secret in ordinary code,
custom command output or an explicitly permitted Shell command. No production
access, ACL, OIDC, operating-system security settings, dependencies, commits,
pushes, releases or deployments were changed by this review.

## Final verification receipt

- Final complete run: **3390/3390 passed**, 203 modules and 211 isolated jobs;
  no failed, skipped, missing or duplicate stages, and no source changes during
  the run. Coverage aggregation completed without error; `verified=true`.
- Source inventory: `85f4d9b62a8b87c4c1e1efb9d87f4b39951c5911da67eed5c0c1baa6245084f8`.
- Final verification operation: `cdf1fe47b95c4a42b35405bff8d5dd88`.
- Post-run comparison checked 596 inventoried files against the working tree,
  with no differences; all nine retired ChatGPT workbench files remain absent.
  `git diff --check` passed. Main and origin/main still match the starting commit.
- The first complete run was explicitly unsuccessful: 3376 passed and 14 failed.
  Six new tests depended on an obsolete FastAPI internal route representation;
  eight older adapter tests lacked the real EventSource event-listener interface.
  Only those three test fixtures were corrected, with assertions preserved.
  Their 37-test suite passed before the fresh full run. No production behavior
  was weakened to obtain a passing result.
- Frontend build/format checks, panel-asset consistency, Ruff, the existing
  three-file mypy scope, and static release validation passed. The compatible
  environment and dependencies were reused from an existing installation with
  byte-identical lockfiles; nothing was installed.
- This dated report is supporting review documentation, outside the curated
  runtime regression source inventory. No runtime source changed after the
  successful full verification.
