# Hub file ingress settings

The instance administrator can manage Hub byte ingress, native-file relay, exact source hosts and the reviewed `openai_sediment` provider from Settings → Files and artifacts. These controls do not alter Agent configuration, project mappings, grants, writable roots, credential headers or network validation.

## Precedence and migration

The four typed settings use one allowlisted JSON record in the existing Hub `meta` database. An owner-saved override takes precedence over an explicitly present deployment environment variable, which takes precedence over the source default. Startup adds the record only if missing. Existing values are never rewritten by a default upgrade.

New code defaults Hub byte ingress and native-file relay to enabled. An explicit environment or saved `false` remains disabled. The provider default remains empty. Exact source hosts retain the existing reviewed defaults; explicit empty host/provider arrays remain empty. Enabling a transport does not give a project write permission or turn on the execution node.

The native relay's effective value is false while byte ingress is disabled, even if its own configured value is true. A null patch explicitly removes a saved override and restores inheritance. The preview shows the resulting environment/default values before this reset can be confirmed.

Malformed explicit environment values fail closed and are shown as invalid without echoing their contents. A malformed saved record fails the entire policy closed rather than falling back to broader defaults. An explicit valid database override can replace an invalid inherited value, which remains visible as invalid inherited configuration.

## API and exact review

- GET `/api/settings/file-import` returns the revision, typed settings with configured/effective/inherited values and sources, current source policy, and reviewed provider options.
- POST `/api/settings/file-import/preview` accepts `expected_revision` and a typed `patch`. It returns before/after changes, dependent effective changes, the resulting snapshot and a confirmation digest.
- PUT `/api/settings/file-import` accepts the same revision/patch plus that exact `confirmation`.

All three endpoints require the current instance administrator. Preview and save require same-origin CSRF verification. Save rechecks instance-administrator authority and revision inside the database transaction. A stale revision, changed environment, changed patch or mismatched confirmation does not write anything. The browser must display the preview and require a separate explicit confirmation; editing the draft invalidates that preview.

Only `streaming_enabled`, `native_relay_enabled`, `native_file_hosts`, and `native_file_providers` are accepted. Booleans are strict, lists use the shared exact-host/provider normalizers, and unknown keys/providers are rejected. There is no arbitrary environment, file, shell, credentials or remote Agent configuration write API. Audit records contain only typed setting names/values and revision metadata.

## Activation boundary

New admission checks immediately read the saved policy. A finish already admitted to the Agent can still publish before a later check; disabling the Hub transport is not a cancellation or rollback of accepted work. A native source download uses its original immutable source-policy snapshot for every redirect/DNS/TLS/SSRF validation hop. Updated source settings apply to later downloads. Existing project, grant, device, size, checksum and atomic-publication checks remain in place.

Source preview and download use the same resolver. Neither a file ID nor a signed URL is authority. Previews do not fetch URLs, and settings/audit/preview output does not expose signed query strings, source paths, attachment IDs, credential headers or raw malformed environment values.

## Compatibility boundary

Unlike the old env-gated routing, disabling Hub native-file ingress now explicitly rejects the Hub download and source-preview entry points. It never silently falls back to the execution node's separate source policy. Existing independently invoked Agent protocols retain their own authorization and configuration checks. This makes saved Hub disable/deny-all choices reliable and keeps preview and transfer on the same policy.
