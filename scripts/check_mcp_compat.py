"""Read-only interoperability probe using the official MCP SDK, not CodePier's client."""

from __future__ import annotations
import argparse
import asyncio
import json
from importlib.metadata import version
import os
import stat
from pathlib import Path
import httpx2
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


def read_token(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > 2048
            or os.name != "nt"
            and info.st_mode & 0o077
        ):
            raise ValueError("Use a private, small regular token file")
        return os.read(fd, 2049).decode("utf-8").strip()
    finally:
        os.close(fd)


async def probe(url, token_file, project, mode="legacy"):
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + read_token(token_file)},
        timeout=20,
        follow_redirects=False,
        trust_env=False,
    ) as http:
        transport = streamable_http_client(url, http_client=http, terminate_on_close=False)
        async with Client(transport, mode=mode, read_timeout_seconds=20) as client:
            catalog = await client.list_tools()
            definitions = {tool.name: tool.model_dump(by_alias=True) for tool in catalog.tools}
            # Read-only grants can omit act; discovery must not require write access.
            unified = "query" in definitions
            schemas = {name: tool.get("outputSchema") for name, tool in definitions.items()}
            for tool in definitions.values():
                Draft202012Validator.check_schema(tool["inputSchema"])
            # MCP outputSchema is optional. Never report an omitted schema as
            # validated, and still validate every schema actually advertised.
            for schema in schemas.values():
                if schema is not None:
                    Draft202012Validator.check_schema(schema)
            checks = []

            async def invoke(name, args, *, cached=False):
                assert cached or name in definitions, "Call was not advertised"
                if name in definitions:
                    Draft202012Validator(definitions[name]["inputSchema"]).validate(args)
                result = await client.call_tool(name, args)
                value = result.model_dump(by_alias=True, exclude_none=True)
                assert isinstance(value.get("structuredContent"), dict)
                schema = schemas.get(name)
                if schema is not None:
                    Draft202012Validator(schema).validate(value["structuredContent"])
                checks.append(
                    {
                        "tool": name,
                        "action": args.get("action", args.get("operation")),
                        "is_error": value.get("isError", False),
                        "advertised": name in definitions,
                        "schema_valid": True if schema is not None else None,
                    }
                )
                return value

            async def call(legacy_name, legacy_args, action, view="", **args):
                if unified:
                    return await invoke(
                        "query", {"action": action, **({"view": view} if view else {}), **args}
                    )
                return await invoke(legacy_name, legacy_args)

            resolved = await call(
                "workspace",
                {"operation": "resolve", "project": project},
                "project",
                "resolve",
                project=project,
            )
            assert not resolved.get("isError")
            diagnosis = await call(
                "process",
                {"operation": "diagnostics", "project": project},
                "job",
                "diagnostics",
                project=project,
            )
            assert not diagnosis.get("isError")
            assert diagnosis["structuredContent"]["client_catalog"]["matches"] is None

            async def read_content(value):
                assert not value.get("isError")
                data = value["structuredContent"]
                if data.get("pending"):
                    for _ in range(5):
                        receipt = await call(
                            "process",
                            {
                                "operation": "wait",
                                "operation_ids": [data["operation_id"]],
                                "wait_seconds": 2,
                            },
                            "job",
                            "wait",
                            ids=[data["operation_id"]],
                            wait=2,
                        )
                        assert not receipt.get("isError")
                        operation = receipt["structuredContent"]["operations"][0]
                        assert operation["operation_id"] == data["operation_id"]
                        if not operation["pending"]:
                            assert operation["state"] == "succeeded"
                            data = operation["result"]["data"]
                            break
                    else:
                        raise AssertionError("Read did not complete")
                assert isinstance(data.get("content"), str)
                return data["content"]

            read = await call(
                "read",
                {"project": project, "path": "README.md", "limit": 20},
                "file",
                project=project,
                path="README.md",
                limit=20,
            )
            content = await read_content(read)
            if unified:
                # Existing SDK clients may still call a cached legacy tool even
                # though default discovery now advertises only unified tools.
                cached = await invoke(
                    "read", {"project": project, "path": "README.md", "limit": 20}, cached=True
                )
                assert await read_content(cached) == content
            error = await call(
                "read", {"project": project, "path": ".env"}, "file", project=project, path=".env"
            )
            assert (
                error.get("isError")
                and error["structuredContent"]["error"]["code"] == "PROTECTED_PATH"
            )
            artifacts = await call(
                "read", {"operation": "artifacts", "project": project}, "artifacts", project=project
            )
            assert not artifacts.get("isError")
            resources = await client.list_resources()
            prompts = await client.list_prompts()
            assert resources.resources and prompts.prompts
            if client.protocol_version in {"2025-03-26", "2025-06-18", "2025-11-25"}:
                await client.send_ping()
            else:
                assert client.protocol_version == "2026-07-28", "Unverified MCP revision"
            return {
                "sdk": "official mcp " + version("mcp"),
                "mode": mode,
                "protocol_version": client.protocol_version,
                "server": client.server_info.model_dump(by_alias=True),
                "tool_count": len(schemas),
                "tool_names": sorted(definitions),
                "catalog": "unified" if unified else "legacy",
                "output_schema_count": sum(schema is not None for schema in schemas.values()),
                "checks": checks,
                "resources": len(resources.resources),
                "prompts": len(prompts.prompts),
                "scope": "Read-only real HTTP interoperability; not certification of every optional MCP extension.",
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--mode", choices=["legacy", "auto"], default="legacy")
    args = parser.parse_args()
    result = asyncio.run(probe(args.url, args.token_file, args.project, args.mode))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
