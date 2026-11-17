# Execution readiness and diagnostics

## Findings

Nexus's broad npm test command includes browser/staging suites that require explicit environment variables and fixture files. A missing requirement is not proof of an uninstalled dependency or a product regression. CodePier's clean environment intentionally does not inherit the user's shell variables or browser cache. Queue traces also demonstrated resource-conflict waits; those are distinct from connection failures. Long Python collection eventually completed with a larger budget, so a deadlock or storage root cause was not established.

## Changes

- Optional task `requirements` declares `env` variable names, `executables` (PATH basenames), and `files` (public project-relative files), with a limit of 32 entries per category. No commands/imports run during inspection. Task listing exposes readiness; execution checks the actual environment before spawning. Undeclared tasks remain compatible. Container requirements are explicitly unsupported rather than incorrectly validated on the host. No values, credentials, or private files are exposed.
- Queue cards distinguish Hub delivery, Agent resource waits, and running. Resource pressure, remote-target capacity, and project fairness have explanatory labels. If focus/selection prevents refreshing rows, the panel says it has received newer state, not that the old rows are synchronized.
- Local Agent executions retain an additional bounded prefix of already-redacted output in the existing run directory (`output.redacted.log`, exclusive creation, mode 0600). The receipt includes `output_log` status, byte limit, and local path. The limit is 8 MiB and retention follows existing seven-day run-directory cleanup. Status distinguishes complete stream, truncation, incomplete stream, and write failure. Existing receipt tails remain unchanged. Logs are not automatically uploaded or registered as downloadable artifacts, and no read permission is broadened. Redaction uses existing known-credential matching, not detection of all possible unknown secrets.

## Operational boundaries

These changes do not configure Nexus fixtures, install Playwright, inherit shell secrets, relax locks/isolation, or change production settings. Requirements must be declared for a named task; ad-hoc exec commands cannot safely infer all application requirements. The existing local browser cache can be selected explicitly for authorized tests rather than reinstalling browsers. Temporary pytest fixture roots must not be placed inside CodePier's protected private runtime directory; use an isolated public temporary root without weakening protection.

## Verification

Queue browser regression: 46 passed on Chromium and WebKit, using the existing browser cache explicitly. Integrated Python regression: 199 passed (preflight, runtime, reliability, output log, snapshots, and audit agent). Coverage includes missing dependencies, no-spawn enforcement, compatibility, confinement, stream completion, bounded output, split-secret redaction, failures, and timeout behavior. Ruff checks and generated-asset/source-inventory checks passed. The pre-existing queue browser test file is not Ruff-formatted; its unrelated formatting was left untouched. No commit, push, deployment, dependency installation, or credential/settings changes are included.
