# SSH failure receipt interpretation

A completed SSH transport receipt can have result.ok=true and a nonzero exit_code. This means a structured transport result was received, not that the remote command succeeded. failure_context now retains the allowlisted data.ssh_error diagnostic and distinguishes SSH authentication, host verification, and ambiguous command-or-connection failures. It does not infer remote command execution from an authentication failure or change retry policy.

Keep these evidence boundaries separate:

- operation_id identifies a persistent CodePier operation. Query it before considering another request after a timeout or uncertain outcome.
- attempts is the Hub dispatch count, not a count of SSH authentications, successful connections, or caller-platform reviews. It must not be relabeled as any of those.
- failure_context.caller_platform_decision=not_observed means CodePier has no caller-platform decision evidence. A generic rejection text without a CodePier operation receipt cannot be assigned a CodePier review origin or counted as an actual SSH attempt.
- A result outside CodePier may add or transform metadata. Preserve both original content text and structuredContent when diagnosing discrepancies; do not attribute an adapter-added error_code to this repository without evidence.
- replay_recommended remains false. Classification is diagnostic, never authorization, and does not weaken authentication, host-key checks, or approval requirements.

The focused regression tests in tests/test_ssh_failure_receipt.py use synthetic receipts only. They verify diagnostics survive direct and polled MCP responses, preserve dispatch counters and original data, keep text and structured representations equal, ignore unknown diagnostics and output prose, and retain authorization/unknown-outcome precedence. No production SSH is needed.
