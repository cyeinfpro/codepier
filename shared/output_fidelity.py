"""Output evidence and executable cursors; never changes stored process output."""

from __future__ import annotations

OUTPUT_FIELDS = frozenset(
    {
        "output_truncated",
        "output_truncated_by",
        "output_chars",
        "output_range",
        "output_next",
        "output_normalized",
        "output_original_chars",
        "output_original_bytes",
        "output_original_sha256",
        "output_budget_limited",
        "output_recovery",
        "output_total_bytes",
        "output_total_sha256",
    }
)
OUTPUT_PAGE_FIELDS = OUTPUT_FIELDS - {"output_total_bytes", "output_total_sha256"}
LOSS_ORDER = ("upstream", "limit", "budget")


def add_output_loss(row, *causes):
    existing = set(row.get("output_truncated_by", ()))
    existing.update(causes)
    row["output_truncated_by"] = [cause for cause in LOSS_ORDER if cause in existing]
    row["output_truncated"] = bool(existing)


def output_page(row, text, total, *, start, upstream=False):
    row["output"] = text
    if upstream:
        add_output_loss(row, "upstream")
    if start > 0 or start + len(text) < total:
        add_output_loss(row, "limit")
    if row.get("output_truncated_by"):
        row["output_chars"] = total
        row["output_range"] = [start, start + len(text)]
    else:
        row["output_truncated"] = False
    if upstream:
        row["output_recovery"] = (
            "Earlier output was not saved. If a complete log is needed, run a new command "
            "with output redirected to a file in the run directory, then page that file."
        )


def output_next(
    row,
    *,
    operation_id=None,
    project="",
    workspace_id="",
    unified=False,
    pending=False,
    raw_page=False,
):
    """Walk backwards through a terminal tail, or re-read a normalized page raw."""
    span = row.get("output_range")
    identifier = operation_id or row.get("operation_id") or row.get("id")
    if pending or row.get("pending") or not identifier or not span:
        row.pop("output_next", None)
        return
    start, end = span
    if raw_page:
        limit = min(51200, end - start)
        offset = end - limit
    else:
        limit = min(start, 20000)
        offset = start - limit
    if limit <= 0:
        row.pop("output_next", None)
        return
    options = {"output_offset": offset, "output_limit": limit}
    if unified:
        args = {"action": "job", "ids": [identifier], "args": options}
        if project:
            args["project"] = project
        if workspace_id:
            args["workspace_id"] = workspace_id
        row["output_next"] = {"name": "query", "arguments": args}
    else:
        row["output_next"] = {
            "name": "task_query",
            "arguments": {
                "operation": "get",
                "operation_ids": [identifier],
                **options,
                **({"project": project} if project else {}),
                **({"workspace_id": workspace_id} if workspace_id else {}),
            },
        }


def success_restatements(row, execution):
    """Only confirmed terminal success permits folding redundant receipt fields."""
    if (
        row.get("pending")
        or row.get("error")
        or row.get("timed_out")
        or row.get("cancelled")
        or (row.get("result") or {}).get("ok") is False
        or execution.get("command_ok") is not True
        or execution.get("exit_code") != 0
        or execution.get("timed_out")
        or execution.get("cancelled")
        or row.get("state", "succeeded") != "succeeded"
    ):
        return ()
    keys = []
    if (execution.get("failure_context") or {}).get("category") in (None, "succeeded"):
        keys.append("failure_context")
    recovery = row.get("recovery")
    if (
        isinstance(recovery, dict)
        and recovery.get("task_outcome_confirmed")
        and recovery.get("task_state") == "succeeded"
    ):
        keys.append("recovery")
    incomplete = any(
        row.get(k) or execution.get(k)
        for k in ("output_truncated", "output_normalized", "output_truncated_by", "output_omitted")
    )
    if not incomplete or row.get("output_next") or execution.get("output_next"):
        keys.extend(("continuation", "log_ref"))
    return tuple(keys)
