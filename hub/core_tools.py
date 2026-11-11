"""Small facades over existing authorized APIs, not a generic tool dispatcher."""

from __future__ import annotations

import asyncio
import copy
import json
from pydantic import ValidationError
from shared.contracts import TOOLS
from shared.core_contracts import CORE_TOOLS, CORE_ACTIONS
from shared.computer_media import mcp_result
from shared.mcp_presentation import present
from shared.util import DevError

from hub.tool_discovery import (
    WORKSPACE,
    COMPUTER,
    catalogs,
    scope_allows,
    guarded_schema,
    project_hints,
)


def validate(name, raw):
    try:
        return TOOLS[name].model.model_validate(raw)
    except ValidationError as exc:
        issues = [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()
        ]
        raise DevError("INVALID_ARGUMENTS", json.dumps(issues, ensure_ascii=False)[:1500]) from exc


def help_result(tool="", action="", *, scopes=None):
    from shared.public_collaboration import PUBLIC_TOOLS, action_help

    if tool in PUBLIC_TOOLS:
        return action_help(tool, action, scopes=scopes)
    visible = catalogs(scopes)
    if not tool or not action:
        return {
            "tools": {
                name: {
                    "operations": sorted(actions),
                    "help": {
                        "tool": "project_query",
                        "arguments": {"operation": "help", "tool": name, "action": "<operation>"},
                    },
                }
                for name, actions in visible.items()
                if not tool or name == tool
            },
            "note": "Specialized operations use options. Common operations use top-level arguments, with additional filters in options as shown by each help schema. All scopes and node opt-ins are checked on every call.",
        }
    target = catalogs().get(tool, {}).get(action)
    if not target:
        raise DevError("UNKNOWN_OPERATION", "未找到工具操作，请先读取 workspace.help 目录")
    if not scope_allows(target, {}, scopes):
        raise DevError(
            "INSUFFICIENT_SCOPE",
            "当前凭据不提供此操作的发现入口",
            403,
            required_scope=TOOLS[target].scope,
        )
    from shared.contracts import _compact_input_schema

    schema = _compact_input_schema(TOOLS[target].model.model_json_schema())
    advanced = action in CORE_ACTIONS.get(tool, {})
    if advanced:
        for field in ("project", "workspace_id", "idempotency_key"):
            schema.get("properties", {}).pop(field, None)
            if field in schema.get("required", []):
                schema["required"].remove(field)
    elif tool != target:
        # Describe the actual public call, including batched operation IDs and
        # optional filters that are intentionally absent from the small catalog.
        backend = schema
        schema = _compact_input_schema(TOOLS[tool].model.model_json_schema())
        properties = schema["properties"]
        exposed = (backend.get("properties", {}).keys() & properties.keys()) | {"operation"}
        extra = {k: v for k, v in backend.get("properties", {}).items() if k not in properties}
        if tool == "process" and action in {"get", "wait", "cancel", "trace"}:
            exposed |= {"operation_ids"}
            extra.pop("operation_id", None)
            if action in {"get", "wait"}:
                exposed |= {"include_trace"}
        schema["properties"] = {k: v for k, v in properties.items() if k in exposed}
        schema["properties"]["operation"] = {"type": "string", "const": action}
        required = set(backend.get("required", [])) & exposed
        if "operation_id" in backend.get("required", []):
            required.add("operation_ids")
        if extra:
            required_extra = sorted(set(backend.get("required", [])) & extra.keys())
            schema["properties"]["options"] = {
                "type": "object",
                "properties": extra,
                "additionalProperties": False,
            }
            if required_extra:
                schema["properties"]["options"]["required"] = required_extra
                required.add("options")
        schema["required"] = sorted(required | {"operation"})
    if tool == "read" and action == "batch":
        from shared.core_contracts import ReadBatchOptions

        schema = _compact_input_schema(ReadBatchOptions.model_json_schema())
        advanced = True
    if tool == "write" and action in {"import", "source_check"}:
        # openai/fileParams resolves ONLY top-level fields. A nested file in
        # options is retained for legacy callers, never recommended/generated.
        options_schema = copy.deepcopy(schema)
        definitions = options_schema.pop("$defs", {})
        native = options_schema["properties"].pop("file")
        options_schema["required"].remove("file")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "$defs": definitions,
            "properties": {
                "operation": {"type": "string", "const": action},
                "project": {"type": "string", "minLength": 1, "maxLength": 100},
                "workspace_id": {"type": "string", "pattern": r"^(|[a-f0-9]{32})$"},
                "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
                "file": native,
                "options": options_schema,
            },
            "required": ["operation", "project", "idempotency_key", "file"]
            + (["options"] if options_schema.get("required") else []),
        }
        advanced = False
    if not advanced and "operation" in schema.get("properties", {}):
        # A single-operation help schema must not re-advertise facade siblings.
        schema["properties"]["operation"] = {"type": "string", "const": action}
    return {
        "tool": tool,
        "operation": action,
        "scope": TOOLS[target].scope,
        "arguments_location": "options" if advanced else "top-level",
        "requires_project": any(
            "project" in TOOLS[n].model.model_fields
            and TOOLS[n].model.model_fields["project"].is_required()
            for n in (target, tool)
        ),
        "requires_idempotency_key": "idempotency_key" in TOOLS[target].model.model_fields
        and TOOLS[target].model.model_fields["idempotency_key"].is_required(),
        "inputSchema": guarded_schema(tool, schema, scopes),
        "description": public_description(TOOLS[target].description),
    }


def public_call(target, arguments):
    name, args = _public_call(target, arguments)
    # A receipt/discovery hint must not send a read through a mixed mutation
    # tool. Validate against the query allowlist, including baseline rejection;
    # never relabel a mutating call or change its authorization requirements.
    query = {"workspace": "project_query", "process": "task_query"}.get(name)
    if query:
        try:
            TOOLS[query].model.model_validate(args)
        except ValidationError:
            pass
        else:
            return query, args
    return name, args


def _public_call(target, arguments):
    for name, actions in CORE_ACTIONS.items():
        for operation, backend in actions.items():
            if target == backend:
                args = dict(arguments)
                outer = {
                    key: args.pop(key)
                    for key in ("project", "workspace_id", "idempotency_key")
                    if key in args
                }
                if name == "write" and operation in {"import", "source_check"} and "file" in args:
                    outer["file"] = args.pop("file")
                return name, {**outer, "operation": operation, "options": args}
    for operation, backend in WORKSPACE.items():
        if target == backend:
            outer = {
                k: v for k, v in arguments.items() if k in TOOLS["workspace"].model.model_fields
            }
            options = {k: v for k, v in arguments.items() if k not in outer}
            return "workspace", {
                **outer,
                "operation": operation,
                **({"options": options} if options else {}),
            }
    for operation, backend in COMPUTER.items():
        if target == backend:
            return "computer", {**arguments, "operation": operation}
    if target.startswith("browser_"):
        return "browser", {**arguments, "operation": target.removeprefix("browser_")}
    if target in {"operations_get", "operations_wait", "operations_cancel", "operations_trace"}:
        args = dict(arguments)
        args["operation_ids"] = [args.pop("operation_id")]
        args["operation"] = target.removeprefix("operations_")
        return "process", args
    if target in {"operations_list", "diagnostics_get", "activity_list"}:
        action = {
            "operations_list": "list",
            "diagnostics_get": "diagnostics",
            "activity_list": "activity",
        }[target]
        outer = {k: v for k, v in arguments.items() if k in TOOLS["process"].model.model_fields}
        options = {k: v for k, v in arguments.items() if k not in outer}
        return "process", {
            **outer,
            "operation": action,
            **({"options": options} if options else {}),
        }
    if target in {"fs_read", "fs_write", "apply_patch", "shell_exec", "tasks_run"}:
        name = {
            "fs_read": "read",
            "fs_write": "write",
            "apply_patch": "edit",
            "shell_exec": "exec",
            "tasks_run": "exec",
        }[target]
        return name, {**arguments, **({"yield_seconds": 0} if name == "exec" else {})}
    return target, arguments


def public_description(description):
    import re
    from shared.core_contracts import REPLACED_MCP_TOOLS

    return re.sub(
        r"\b[a-z][a-z_]+\b", lambda match: REPLACED_MCP_TOOLS.get(match[0], match[0]), description
    )


async def invoke_query(runtime, name, raw, principal):
    model = validate(name, raw)
    target = "workspace" if name == "project_query" else "process"
    return await runtime.invoke(target, model.model_dump(exclude_unset=True), principal)


async def invoke(runtime, name, raw, principal):
    model = validate(name, raw)
    args = model.model_dump()
    action = args["operation"]
    if name == "workspace" and action == "help":
        from hub.principal import refresh_principal

        user = await runtime.store.run(refresh_principal, runtime.store, principal)
        if "read" not in user.scopes:
            raise DevError("INSUFFICIENT_SCOPE", "缺少 read 权限", 403, required_scope="read")
        return help_result(args["tool"], args["action"], scopes=user.scopes)
    target = CORE_ACTIONS.get(name, {}).get(action)
    if target:
        fields = TOOLS[target].model.model_fields
        outer = {"operation", "options", "project", "workspace_id", "idempotency_key"}
        if name == "write" and action in {"import", "source_check"}:
            outer.add("file")
        if set(model.model_fields_set) - outer:
            raise DevError("INVALID_ARGUMENTS", "专项操作参数必须放在 options 中")
        options = dict(args["options"])
        if (
            name == "write"
            and action in {"import", "source_check"}
            and args.get("file") is not None
        ):
            if "file" in options:
                raise DevError("INVALID_ARGUMENTS", "file 不能在顶层和 options 中重复提供")
            options["file"] = args["file"]
        if {"project", "workspace_id", "idempotency_key"} & options.keys():
            raise DevError(
                "INVALID_ARGUMENTS", "project、workspace_id、idempotency_key 必须放在顶层"
            )
        for field in ("project", "workspace_id", "idempotency_key"):
            if field in fields and args.get(field):
                options[field] = args[field]
        return await runtime.invoke(target, options, principal)
    if name == "process":
        options = dict(args["options"])
        if {
            "project",
            "workspace_id",
            "idempotency_key",
            "operation_id",
            "operation_ids",
        } & options.keys():
            raise DevError("INVALID_ARGUMENTS", "标识和授权范围参数必须放在顶层")
        if TOOLS[name].model.model_fields.keys() & options.keys():
            raise DevError("INVALID_ARGUMENTS", "options 中不能重复顶层参数")
        if action in {"diagnostics", "activity"}:
            target = "diagnostics_get" if action == "diagnostics" else "activity_list"
            call = {
                k: v
                for k, v in model.model_dump(exclude_unset=True).items()
                if k in TOOLS[target].model.model_fields
            }
            return await runtime.invoke(target, {**call, **options}, principal)
        if action == "list":
            return await runtime.invoke(
                "operations_list",
                {
                    k: args[k]
                    for k in ("project", "state", "idempotency_key", "limit", "before_created")
                }
                | options,
                principal,
            )
        # Authorization is checked by each real operation endpoint. Batch waits
        # share one wall-clock budget; one slow job never serializes the others.
        batch_wait = action == "wait" and len(args["operation_ids"]) > 1
        if batch_wait:
            await runtime.wait_operations(args["operation_ids"], principal, args["wait_seconds"])

        async def one(identifier):
            call = {**options, "operation_id": identifier}
            if action not in {"cancel", "trace"}:
                call.update(
                    {
                        k: args[k]
                        for k in (
                            "include_output",
                            "include_result",
                            "output_limit",
                            "output_offset",
                            "after_output_seq",
                        )
                    }
                )
            if action == "wait":
                call["wait_seconds"] = 0 if batch_wait else args["wait_seconds"]
            if action == "trace" and "limit" in model.model_fields_set:
                call["limit"] = args["limit"]
            try:
                value = await runtime.invoke("operations_" + action, call, principal)
                if args["include_trace"] and action in {"get", "wait"}:
                    value["execution"] = await runtime.invoke(
                        "operations_trace", {"operation_id": identifier}, principal
                    )
                return value
            except DevError as exc:
                return {
                    "operation_id": identifier,
                    "error": {"code": exc.code, "message": exc.message},
                }

        return {
            "operations": await asyncio.gather(
                *(one(identifier) for identifier in args["operation_ids"])
            )
        }
    target = (
        WORKSPACE[action]
        if name == "workspace"
        else COMPUTER[action]
        if name == "computer"
        else "browser_" + action
    )
    fields = TOOLS[target].model.model_fields
    supplied = model.model_dump(exclude_unset=True)
    supplied.pop("operation", None)
    options = supplied.pop("options", {})
    if {
        "project",
        "workspace_id",
        "idempotency_key",
    } & options.keys() or supplied.keys() & options.keys():
        raise DevError("INVALID_ARGUMENTS", "options 不能覆盖顶层参数或授权范围")
    supplied.update(options)
    # Do not silently ignore an action parameter belonging to another operation.
    unused = set(supplied) - set(fields)
    if unused:
        raise DevError(
            "INVALID_ARGUMENTS", f"{name}.{action} 不接受参数：" + ", ".join(sorted(unused))
        )
    return await runtime.invoke(target, supplied, principal)


def failed(value):
    if value.get("pending"):
        return False
    nested = value.get("result") or {}
    data = nested.get("data") or value
    return bool(
        value.get("error")
        or value.get("state") in {"failed", "cancelled", "needs_review", "interrupted"}
        or nested.get("ok") is False
        or data.get("command_ok") is False
        or data.get("success") is False
        or data.get("batch") is True
        and any(item.get("ok") is False for item in data.get("files", []))
    )


def compact_receipt(value):
    for key in ("next", "next_call", "output_next"):
        call = value.get(key)
        if isinstance(call, dict) and isinstance(call.get("arguments"), dict):
            field = "name" if "name" in call else "tool"
            if isinstance(call.get(field), str):
                name, args = public_call(call[field], call["arguments"])
                value[key] = {**call, field: name, "arguments": args}
    if isinstance(value.get("next"), str) and value["next"] in {
        "operations_get",
        "operations_wait",
    }:
        value["next"] = "task_query"
    nested = value.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("data"), dict):
        compact_receipt(nested["data"])
    return value


def _batch_reads(public):
    """Find only known read result locations, never inspect source text."""
    if not isinstance(public, dict):
        return []
    if (
        isinstance(public.get("content"), str)
        and isinstance(public.get("offset"), int)
        and "sha256" in public
        and "total_lines" in public
    ):
        return [{"files": [public]}]
    if public.get("batch") is True and isinstance(public.get("files"), list):
        return [public]
    if public.get("tool") in {"read", "fs_read", "fs_read_many"}:
        nested = public.get("result")
        data = nested.get("data") if isinstance(nested, dict) else None
        if isinstance(data, dict):
            return _batch_reads(data)
    return [
        batch
        for item in public.get("operations", [])
        if isinstance(item, dict)
        for batch in _batch_reads(item)
    ]


def _render_text(rendered, public, *, text_mode, body_key, mirror, tool_name="", fold=True):
    from shared.mcp_presentation import compact_output_text

    rendered["structuredContent"] = public
    if body_key:
        header = {key: value for key, value in public.items() if key != body_key}
        text = (
            json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n" + public[body_key]
        )
        if mirror == "meta":
            rendered["structuredContent"] = {**header, body_key + "_in": "text"}
    else:
        value = (
            compact_output_text(public, tool_name) if text_mode == "compact" and fold else public
        )
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    rendered["content"][0]["text"] = text


def _fit_prefix(count, measure, budget):
    low, high = 0, count
    while low < high:
        middle = (low + high + 1) // 2
        if measure(middle) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


def bound_batch_response(
    payload,
    *,
    output_budget=False,
    text_mode="json",
    body_key=None,
    mirror="full",
    tool_name="",
    project="",
    workspace_id="",
    unified=False,
    sources=(),
):
    """Bound final JSON-RPC bytes without changing the authorized stored result."""
    from shared.core_contracts import READ_BATCH_PAYLOAD_BYTES
    from shared.mcp_presentation import (
        output_records,
        record_output_original,
        normalize_output,
        original_output,
        log_receipts,
    )
    from shared.output_fidelity import add_output_loss, output_next, OUTPUT_PAGE_FIELDS

    rendered = payload.get("result", {})
    public = rendered.get("structuredContent")
    if not isinstance(public, dict) or any(
        block.get("type") != "text" for block in rendered.get("content", [])
    ):
        return payload

    # The HTTP boundary runs after protocol metadata is attached. Recover the
    # raw body from the first rendering so the final cap measures the same shape.
    if text_mode == "compact" and rendered.get("content") and not rendered.get("isError"):
        first, separator, body = rendered["content"][0]["text"].partition("\n")
        if separator:
            for key in ("content", "output"):
                if public.get(key + "_in") == "text":
                    public = {name: value for name, value in public.items() if name != key + "_in"}
                    public[key], body_key, mirror = body, key, "meta"
                    break
                if (
                    isinstance(public.get(key), str)
                    and public[key] == body
                    and first.startswith("{")
                ):
                    body_key = key
                    break
    if not _batch_reads(public) and not (output_budget and output_records(public, tool_name)):
        return payload

    payload = copy.deepcopy(payload)
    rendered = payload["result"]
    public = copy.deepcopy(public)
    batches = _batch_reads(public)
    budget = READ_BATCH_PAYLOAD_BYTES

    def size():
        return len(
            json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )

    def render():
        _render_text(
            rendered,
            public,
            text_mode=text_mode,
            body_key=body_key,
            mirror=mirror,
            tool_name=tool_name,
            fold=output_budget,
        )
        return size()

    def pressure():
        if size() > budget:
            return True
        if not output_budget:
            return False
        # Restoring a parent receipt replaces nested dictionaries; refresh
        # each slot so later candidates still belong to the rendered payload.
        for index in range(len(output_records(public, tool_name))):
            candidate = output_records(public, tool_name)[index]
            if not candidate.get("output") or candidate.get("output_budget_limited"):
                continue
            saved = copy.deepcopy(candidate)
            raw = original_output(candidate, sources)
            if raw is None:
                continue
            owner = next(
                (
                    item
                    for item in log_receipts(public, tool_name)
                    if item is candidate or (item.get("result") or {}).get("data") is candidate
                ),
                candidate,
            )
            span = candidate.get("output_range", [0, len(raw)])
            add_output_loss(candidate, "budget")
            candidate.update(
                output_budget_limited=True,
                output_range=span,
                output_chars=candidate.get("output_chars", span[1]),
            )
            output_next(
                candidate,
                operation_id=owner.get("operation_id") or owner.get("id"),
                project=project,
                workspace_id=workspace_id,
                unified=unified,
                pending=owner.get("pending", False),
                raw_page=candidate.get("output_normalized", False),
            )
            linked = (candidate.get("result") or {}).get("data")
            if isinstance(linked, dict) and linked.get("output_at") == "output":
                for key in OUTPUT_PAGE_FIELDS:
                    if key in candidate:
                        linked[key] = copy.deepcopy(candidate[key])
            exceeds = render() > budget
            candidate.clear()
            candidate.update(saved)
            render()
            if exceeds:
                return True
        return False

    while pressure():
        candidates = [
            (batch, item)
            for batch in batches
            for item in batch["files"]
            if item.get("ok", True) is True and item.get("content")
        ]
        outputs = (
            [row for row in output_records(public, tool_name) if row.get("output")]
            if output_budget
            else []
        )
        if outputs and (
            not candidates
            or max(len(row["output"].encode("utf-8")) for row in outputs)
            > max(len(pair[1]["content"].encode("utf-8")) for pair in candidates)
        ):
            row = max(outputs, key=lambda item: len(item["output"].encode("utf-8")))
            original = original_output(row, sources)
            if original is None:
                return payload  # Never invent offsets for unavailable normalized source.
            owner = next(
                (
                    item
                    for item in log_receipts(public, tool_name)
                    if item is row or (item.get("result") or {}).get("data") is row
                ),
                row,
            )
            span = row.get("output_range", [0, len(original)])
            total = row.get("output_chars", span[1])
            if span[1] < total and not original.endswith("\n"):
                boundary = original.rfind("\n") + 1
                if boundary:
                    original = original[:boundary]
                    span = [span[0], span[0] + boundary]
            # A budget window contains contiguous complete lines. A single very
            # long line still makes progress via a character window.
            ends = [0]
            for line in original.splitlines(keepends=True):
                ends.append(ends[-1] + len(line))
            normalized = row.get("output_normalized", False)
            if row.get("output_truncated") and not row.get("output_truncated_by"):
                add_output_loss(row, "upstream")
            add_output_loss(row, "budget")
            row["output_budget_limited"] = True
            row["output_chars"] = total

            def measure(
                count,
                *,
                ends=ends,
                original=original,
                row=row,
                normalized=normalized,
                span=span,
                owner=owner,
            ):
                start = ends[-1 - count] if len(ends) > 2 else len(original) - count
                raw = original[start:]
                row["output"] = normalize_output(raw) if normalized else raw
                row["output_range"] = [span[0] + start, span[1]]
                if normalized:
                    record_output_original(row, raw)
                output_next(
                    row,
                    operation_id=owner.get("operation_id") or owner.get("id"),
                    project=project,
                    workspace_id=workspace_id,
                    unified=unified,
                    pending=owner.get("pending", False),
                    raw_page=normalized,
                )
                linked = (row.get("result") or {}).get("data")
                if isinstance(linked, dict) and linked.get("output_at") == "output":
                    for key in OUTPUT_PAGE_FIELDS:
                        if key in row:
                            linked[key] = copy.deepcopy(row[key])
                        else:
                            linked.pop(key, None)
                return render()

            count = len(ends) - 1 if len(ends) > 2 else len(original)
            fit = _fit_prefix(max(0, count - 1), measure, budget)
            # If other bodies alone exceed the cap, share the reduction before
            # emptying one operation. Each pass strictly narrows a source window.
            measure(max(fit, count // 2) if len(outputs) > 1 else fit)
            continue
        if not candidates:
            if output_budget and not batches:
                return payload  # Required control/authorization metadata is irreducible.
            raise DevError(
                "READ_BATCH_RESPONSE_TOO_LARGE",
                "读取响应元数据超过配置预算；请减少操作数或单独读取文件",
            )
        batch, item = max(candidates, key=lambda pair: len(pair[1]["content"].encode("utf-8")))
        lines = item["content"].splitlines(keepends=True)
        batch.update(truncated=True, budget_limited=True, payload_limit_bytes=budget)

        def measure(count, *, item=item, lines=lines):
            item.update(
                content="".join(lines[:count]),
                end_line=item["offset"] - 1 + count,
                next_offset=item["offset"] + count,
                truncated=True,
                budget_limited=True,
            )
            return render()

        fit = _fit_prefix(len(lines) - 1, measure, budget)
        # Share pressure across several pages before sacrificing a single
        # file's usable prefix. Each pass strictly reduces one candidate.
        measure(max(fit, len(lines) // 2) if len(candidates) > 1 else fit)
    return payload


def _result(name, arguments, value, *, scopes=None):
    value = present(name, arguments, value)
    value = project_hints(copy.deepcopy(value), scopes)
    name = {"project_query": "workspace", "task_query": "process"}.get(name, name)
    if name in CORE_TOOLS:
        value = compact_receipt(copy.deepcopy(value))
    if name in {"browser", "computer"}:
        action = arguments.get("operation", "status")
        return mcp_result(COMPUTER[action] if name == "computer" else "browser_" + action, value)
    backend = CORE_ACTIONS.get(name, {}).get(arguments.get("operation"))
    if backend:
        rendered = mcp_result(backend, value)
        rendered["isError"] = rendered["isError"] or failed(value)
        return rendered
    if name == "process" and arguments.get("operation", "list") in {
        "get",
        "wait",
        "cancel",
        "trace",
    }:
        rendered = [
            mcp_result("operations_get", compact_receipt(item))
            for item in value.get("operations", [])
        ]
        public = {"operations": [item["structuredContent"] for item in rendered]}
        # Each operation applies the existing screenshot lifetime/size checks.
        # Limit aggregate media in a batch; omitted images can be fetched singly.
        images, size = [], 0
        for item in rendered:
            for block in item["content"][1:]:
                size += len(block.get("data", ""))
                if size <= 7 * 1024 * 1024:
                    images.append(block)
                else:
                    public["media_truncated"] = True
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(public, ensure_ascii=False, separators=(",", ":")),
                },
                *images,
            ],
            "structuredContent": public,
            "isError": any(
                item["isError"] or failed(item["structuredContent"]) for item in rendered
            ),
        }
        return result
    rendered = mcp_result(name, value)
    if name in CORE_TOOLS:
        rendered["isError"] = rendered["isError"] or failed(value)
    return rendered


def result(
    name,
    arguments,
    value,
    *,
    scopes=None,
    view=2,
    verbosity="lean",
    text_mode="json",
    output_mode="raw",
    unified=False,
    unified_views=None,
    mirror="full",
):
    from shared.mcp_presentation import token_view, raw_output_sources

    if view >= 3 and verbosity != "full" and unified and unified is not True and name == "process":
        from shared.unified_contracts import unwrap_waited

        unwrapped = unwrap_waited(value, unified)
        if unwrapped is not value and not failed(unwrapped):
            name, arguments, value = unified.tool, unified.args, unwrapped
    rendered = _result(name, arguments, value, scopes=scopes)
    sources = raw_output_sources(rendered["structuredContent"], name)
    # Stable range reads are a raw escape hatch available on this connection.
    if arguments.get("output_offset") is not None:
        output_mode = "raw"
    presentation_args = arguments
    if getattr(unified, "action", "") == "tree":
        options = {"format": "compact", **unified.args.get("options", {})}
        presentation_args = {**unified.args, "options": options}
    elif view >= 3 and getattr(unified, "action", "") == "search":
        presentation_args = {
            **arguments,
            "options": {**arguments.get("options", {}), "format": unified.format},
        }
    public = token_view(
        name,
        presentation_args,
        rendered["structuredContent"],
        view=view,
        verbosity=verbosity,
        output_mode=output_mode,
    )
    if getattr(unified, "action", "") == "tree" and name == "process":
        for row in public.get("operations", []):
            data = (row.get("result") or {}).get("data")
            if isinstance(data, dict):
                row["result"]["data"] = token_view(
                    "workspace",
                    presentation_args,
                    data,
                    view=view,
                    verbosity=verbosity,
                    output_mode=output_mode,
                )
    if unified:
        # Presentation only: unified next-call hints and no duplicated output.
        from shared.unified_contracts import present_unified
        from hub.unified_tools import convert_hint

        public = present_unified(
            public,
            verbosity=verbosity,
            plan=None if unified is True else unified,
            visible=unified_views,
            convert=convert_hint,
            view=view,
        )
    body_key = None
    if text_mode == "compact" and not rendered.get("isError") and not public.get("pending"):
        action = getattr(unified, "action", "")
        if (
            (name == "read" or action == "file")
            and isinstance(public.get("content"), str)
            and "sha256" in public
        ):
            body_key = "content"
        elif (
            (name == "exec" or action == "exec")
            and public.get("exit_code") == 0
            and isinstance(public.get("output"), str)
        ):
            body_key = "output"
    _render_text(
        rendered,
        public,
        text_mode=text_mode,
        body_key=body_key,
        mirror=mirror,
        tool_name=name,
        fold=verbosity != "full",
    )
    return bound_batch_response(
        {"jsonrpc": "2.0", "id": None, "result": rendered},
        output_budget=view >= 2 and verbosity != "full",
        text_mode=text_mode,
        body_key=body_key,
        mirror=mirror,
        tool_name=name,
        project=getattr(unified, "project", "") or arguments.get("project", ""),
        workspace_id=getattr(unified, "workspace_id", "") or arguments.get("workspace_id", ""),
        unified=bool(unified),
        sources=sources,
    )["result"]
