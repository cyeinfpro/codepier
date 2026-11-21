"""Pi-inspired file primitives, using CodePier's path, SHA and backup guards.

Design reference: badlogic/pi-mono coding-agent tools (MIT), commit d5629e2.
All replacements match the original snapshot; no fuzzy substitution guesses.
"""

from __future__ import annotations

import base64
from agent.filesystem import MAX_TEXT_EDIT
from shared.crypto import digest
from shared.util import DevError

OUTPUT_BYTES = 50 * 1024
# Same bound as shared.computer_media.MAX_IMAGE_BYTES (checked by tests). Kept
# local so importing file primitives never loads desktop media contracts.
MAX_INLINE_IMAGE_BYTES = 3 * 1024 * 1024


def edited_bytes(engine, before, edits):
    original = engine.text(before)
    text = original.replace("\r\n", "\n")
    line_ending = "\r\n" if "\r\n" in original else "\n"
    replacements = []
    for edit in edits:
        needle = edit["old_text"].replace("\r\n", "\n")
        start = text.find(needle)
        if start < 0:
            raise DevError("EDIT_NOT_FOUND", "old_text 未匹配原文件，未修改")
        if not edit.get("replace_all") and text.find(needle, start + 1) >= 0:
            raise DevError("EDIT_AMBIGUOUS", "old_text 在原文件中不唯一，请补充上下文，未修改")
        end = start + len(needle)
        replacement = edit["new_text"].replace("\r\n", "\n").replace("\n", line_ending)
        replacements.append((start, end, replacement))
        if edit.get("replace_all"):
            start = text.find(needle, end)
            while start >= 0:
                end = start + len(needle)
                replacements.append((start, end, replacement))
                if len(replacements) > 65536:
                    raise DevError("EDIT_LIMIT", "替换匹配超过 65536 处，请缩小修改范围")
                start = text.find(needle, end)
    replacements.sort()
    if any(left[1] > right[0] for left, right in zip(replacements, replacements[1:], strict=False)):
        raise DevError("EDIT_OVERLAP", "多处编辑在原文件中重叠，未修改")
    # Convert only replacement boundaries back to original byte-preserving
    # text positions. A CRLF contributes one extra original character.
    output_size = len(before)
    chunks = []
    previous = 0
    extra = 0
    crlf = original.find("\r\n")
    for start, end, replacement in replacements:
        while crlf != -1 and crlf < start + extra:
            extra += 1
            crlf = original.find("\r\n", crlf + 2)
        original_start = start + extra
        chunks.append(original[previous:original_start])
        chunks.append(replacement)
        while crlf != -1 and crlf < end + extra:
            extra += 1
            crlf = original.find("\r\n", crlf + 2)
        previous = end + extra
        output_size += len(replacement.encode("utf-8")) - len(
            original[original_start:previous].encode("utf-8")
        )
    if output_size > MAX_TEXT_EDIT:
        raise DevError("FILE_TOO_LARGE", "替换后的文件超过 16 MiB，未修改")
    chunks.append(original[previous:])
    return engine.encode_content("".join(chunks), MAX_TEXT_EDIT)


def _read_page_bytes(project, *, fallback):
    budget = project.get("_response_bytes") if isinstance(project, dict) else None
    return max(OUTPUT_BYTES, budget) if type(budget) is int else fallback


class CoreFiles:
    def __init__(self, engine):
        self.engine = engine
        self.output_bytes = OUTPUT_BYTES

    def _large_image_metadata(self, mime, data, path, args):
        if mime and len(data) > MAX_INLINE_IMAGE_BYTES:
            return self.asset_metadata(path, args, "IMAGE_TOO_LARGE")
        return None

    def _read_image(self, mime, data, path, args, sha, meta):
        # Oversized project images are ordinary assets, not desktop screenshots.
        metadata = self._large_image_metadata(mime, data, path, args)
        if metadata is not None:
            return metadata
        if args["offset"] != 1:
            raise DevError("INVALID_OFFSET", "图片不支持文本行分页")
        # Isolated text reads do not need desktop media or model schemas.
        # Keep the same validation when an actual PNG/JPEG is returned.
        from shared.computer_media import normalize_content

        return {
            **meta,
            **normalize_content(
                {
                    "content": [
                        {
                            "type": "image",
                            "mimeType": mime,
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    ]
                }
            ),
            "truncated": False,
            "next_offset": None,
        }

    def read_one(self, root, args, *, text_only=False):
        engine = self.engine
        path = engine.path(root, args["path"], False)
        try:
            data = engine.read_bytes(path, max_bytes=MAX_TEXT_EDIT)
        except DevError as exc:
            if exc.code != "FILE_TOO_LARGE" or text_only:
                raise
            return self.asset_metadata(path, args, exc.code)
        sha = digest(data)
        if args["expected_sha256"] and args["expected_sha256"] != sha:
            raise DevError("SHA_CONFLICT", "分页读取期间文件改变，请从头重新读取", 409)
        meta = {"path": path.relative_to(root).as_posix(), "sha256": sha, "bytes": len(data)}
        mime = (
            "image/png"
            if data.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
            if data.startswith(b"\xff\xd8\xff")
            else None
        )
        if mime and text_only:
            raise DevError("BINARY_FILE", "批量读取仅支持 UTF-8 文本；图片请单独读取")
        if mime:
            return self._read_image(mime, data, path, args, sha, meta)
        try:
            lines = engine.text(data).splitlines(keepends=True)
        except DevError as exc:
            if exc.code not in {"BINARY_FILE", "ENCODING"} or text_only:
                raise
            return self.asset_metadata(path, args, exc.code)
        start = args["offset"] - 1
        if start > 0 and start >= len(lines):
            raise DevError("INVALID_OFFSET", "offset 超出文件行数")
        selected, size = [], 0
        for line in lines[start : start + args["limit"]]:
            count = len(line.encode("utf-8"))
            if size + count > self.output_bytes:
                if not selected:
                    raise DevError(
                        "LINE_TOO_LARGE",
                        f"此行超过 {self.output_bytes // 1024} KiB 单次输出上限；未返回半行",
                    )
                break
            selected.append(line)
            size += count
        end = start + len(selected)
        more = end < len(lines)
        return {
            **meta,
            "content": "".join(selected),
            "offset": start + 1,
            "end_line": end,
            "total_lines": len(lines),
            "truncated": more,
            "next_offset": end + 1 if more else None,
        }

    @staticmethod
    def asset_metadata(path, args, reason):
        from agent.file_blobs import snapshot

        info = snapshot(path)
        if args["expected_sha256"] and args["expected_sha256"] != info["sha256"]:
            raise DevError("SHA_CONFLICT", "文件已改变，请重新读取", 409)
        return {
            "path": args["path"],
            "sha256": info["sha256"],
            "bytes": info["bytes"],
            "content": "",
            "content_available": False,
            "content_unavailable_reason": reason,
            "truncated": True,
            "next_offset": None,
            "hint": "仅返回摘要；单项 edit delete/move 可处理此文件，不表示已读取文本",
        }

    def read_batch(self, root, args):
        # One admitted operation, one root, no sub-dispatch or authorization retry.
        # FileEngine retains all traversal, symlink, hardlink and secret guards.
        files = []
        for index, item in enumerate(args["options"]["items"]):
            try:
                value = self.read_one(root, item, text_only=True)
                files.append({"index": index, "ok": True, **value})
            except DevError as exc:
                files.append(
                    {
                        "index": index,
                        "path": item["path"],
                        "ok": False,
                        "error": {"code": exc.code, "message": exc.message, "retryable": False},
                    }
                )
            except OSError:
                # Do not return OS exceptions containing absolute host paths.
                files.append(
                    {
                        "index": index,
                        "path": item["path"],
                        "ok": False,
                        "error": {
                            "code": "FILE_READ_FAILED",
                            "message": "无法读取此文件",
                            "retryable": False,
                        },
                    }
                )
        return {
            "batch": True,
            "files": files,
            "truncated": any(item.get("truncated", False) for item in files),
        }

    def call(self, tool, project, args):
        engine = self.engine
        # A larger Hub response budget raises the single-read page; never lowers it.
        self.output_bytes = _read_page_bytes(project, fallback=self.output_bytes)
        root, _ = engine.root(project, tool != "read")
        if tool == "read":
            snapshot = args.get("options", {}).get("read_snapshot")
            if snapshot:
                from agent.read_snapshots import read_snapshot

                result = read_snapshot(engine, root, project, args, snapshot)
            elif args.get("operation") == "batch":
                result = self.read_batch(root, args)
            else:
                result = self.read_one(root, args)
            if args.get("options", {}).get("include_knowledge") is False:
                return result
            from agent.project_knowledge import auto_context

            paths = (
                [item["path"] for item in args["options"]["items"]]
                if args.get("operation") == "batch"
                else [args["path"]]
            )
            knowledge = auto_context(engine, project, paths=paths, max_chars=2200)
            if knowledge is not None:
                result["project_knowledge"] = knowledge
            return result
        if tool == "edit" and args.get("changes"):
            return engine.call("apply_patch", project, args)
        path = engine.path(root, args["path"], False)
        # Retain the engine's short mutation critical section for compatibility
        # with legacy writes. Async resource claims cover the complete operation.
        with engine.mutation_lock:
            if tool == "write":
                after = engine.encode_content(args["content"])
                before = engine.read_bytes(path, True, max_bytes=MAX_TEXT_EDIT)
                if (digest(before) if before is not None else "new") != args["expected_sha256"]:
                    raise DevError("SHA_CONFLICT", "文件已改变，请重新读取后再修改", 409)
                if not path.parent.is_dir():
                    engine.call(
                        "fs_mkdir", project, {"path": path.parent.relative_to(root).as_posix()}
                    )
            else:
                before = engine.read_bytes(path, max_bytes=MAX_TEXT_EDIT)
                if digest(before) != args["expected_sha256"]:
                    raise DevError("SHA_CONFLICT", "文件已改变，请重新读取后再修改", 409)
                after = edited_bytes(engine, before, args["edits"])
            # Reuse the bytes already read and SHA-checked under the same lock.
            return engine.mutate(
                project, args["path"], args["expected_sha256"], after, verified_before=before
            )
