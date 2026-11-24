"""Best-effort per-turn reviews, independent of browser lifetime or model output."""

from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from agent.filesystem import FileEngine
from agent.coding_reviews import (
    ReviewStore,
    binding,
    capture,
    freeze_review,
    freeze_review_from_snapshots,
)
from shared.native_flags import NativeFeatureFlags, resolve_native_flags
from shared.util import DevError


class TurnReviews:
    def __init__(
        self,
        directory,
        row,
        config_path,
        *,
        flags: NativeFeatureFlags | Mapping[str, Any] | None = None,
    ):
        self.directory, self.row, self.config_path = Path(directory), row, Path(config_path)
        self.flags = resolve_native_flags(flags)
        self.baselines = {}

    def _store(self, engine):
        return ReviewStore(engine)

    def prepare_turn_baseline(
        self,
        receipt: str,
        attempt_id: str,
        *,
        before_ref: str,
        require_recoverable: bool = False,
        paths=None,
    ) -> dict[str, Any]:
        """Capture and durably publish a pre-turn baseline under a pre-allocated ref."""
        try:
            engine, project = self.engine()
            expected_binding = binding(engine, project)
            snapshot = capture(engine, project, paths=paths)
            saved = self._store(engine).save_turn_snapshot(
                snapshot,
                ref=before_ref,
                receipt=receipt,
                attempt_id=attempt_id,
                role="before",
                expected_binding=expected_binding,
            )
            return {
                "status": "baseline_ready",
                "before_ref": before_ref,
                "binding": expected_binding,
                "coverage": saved["coverage"],
                "snapshot": saved,
            }
        except Exception as exc:
            if require_recoverable:
                raise
            return {
                "status": "baseline_unavailable",
                "before_ref": before_ref,
                "error_code": getattr(exc, "code", "REVIEW_UNAVAILABLE"),
            }

    def capture_turn_after(
        self,
        receipt: str,
        attempt_id: str,
        *,
        after_ref: str,
        paths=None,
    ) -> dict[str, Any]:
        """Load already-published immutable after snapshot or capture and save it once."""
        engine, project = self.engine()
        store = self._store(engine)
        expected_binding = binding(engine, project)
        try:
            return store.load_turn_snapshot(after_ref, project, kind="turn_after")
        except DevError as exc:
            if exc.code != "REVIEW_UNAVAILABLE" or exc.details.get("cause") != "REVIEW_NOT_FOUND":
                raise
        snapshot = capture(engine, project, paths=paths)
        return store.save_turn_snapshot(
            snapshot,
            ref=after_ref,
            receipt=receipt,
            attempt_id=attempt_id,
            role="after",
            expected_binding=expected_binding,
        )

    def build_turn_review(
        self,
        *,
        before_ref: str,
        after_ref: str,
        finalize: bool = True,
    ) -> dict[str, Any]:
        """Build and finalize a turn review purely from fixed before/after snapshots."""
        engine, project = self.engine()
        summary = freeze_review_from_snapshots(
            engine, project, before_ref=before_ref, after_ref=after_ref, finalize=finalize
        )
        coverage = summary["coverage"]
        return {
            **summary,
            "available": True,
            "coverage": {
                "complete": coverage["complete"],
                "skipped_files": len(coverage["before"]["skipped"])
                + len(coverage["after"]["skipped"]),
            },
            "text": "本轮期间改动 · " + str(summary["summary"]["files"]) + " 个文件",
        }

    def release_terminal_baseline(self, before_ref: str) -> bool:
        """Release a baseline only on explicit terminal unrecoverability or cleanup."""
        engine, project = self.engine()
        try:
            self._store(engine).release_baseline(before_ref, project)
            return True
        except DevError as exc:
            if exc.code in {"REVIEW_NOT_FOUND", "REVIEW_EXPIRED"}:
                return False
            raise

    def engine(self):
        # Never create a Journal here: its constructor performs Agent recovery.
        with self.config_path.open(encoding="utf-8") as source:
            config = json.load(source)
        engine = FileEngine(
            config, SimpleNamespace(directory=self.directory.parent), self.config_path
        )
        project = {
            "id": self.row["project_id"],
            "alias": self.row["project_id"],
            "root": self.row["root"],
            "device_id": self.row["device_id"],
            "mode": "write",
            "allow_tasks": True,
            "_coding_owner": "native:" + self.row["id"],
        }
        engine.root(project)  # Fresh local root authorization at BOTH ends of a turn.
        return engine, project

    def begin(self, receipt):
        try:
            engine, project = self.engine()
            document = ReviewStore(engine).save(capture(engine, project))
            self.baselines[receipt] = document["id"]
        except Exception as exc:  # Optional review failures must not interrupt native execution.
            self.baselines[receipt] = {"code": getattr(exc, "code", "REVIEW_UNAVAILABLE")}

    def finish(self, receipt):
        baseline = self.baselines.pop(receipt, None)
        if baseline is None:
            return None
        finalized = False
        try:
            if isinstance(baseline, dict):
                return {
                    "available": False,
                    "status": "unavailable",
                    "text": "未能记录修改前快照，不能核实本轮改动。",
                    "reason": baseline["code"],
                }
            engine, project = self.engine()
            summary = freeze_review(engine, project, baseline, finalize=True)
            finalized = True
            # The persistent transcript contains a compact summary; large private
            # source/diff data stays in the bounded review store and loads on demand.
            coverage = summary["coverage"]
            return {
                **summary,
                "available": True,
                "coverage": {
                    "complete": coverage["complete"],
                    "skipped_files": len(coverage["before"]["skipped"])
                    + len(coverage["after"]["skipped"]),
                },
                "text": "本轮期间改动 · " + str(summary["summary"]["files"]) + " 个文件",
            }
        except Exception as exc:  # Optional review failures must not interrupt native execution.
            return {
                "available": False,
                "status": "unavailable",
                "text": "改动审阅暂不可用；不会把未知结果显示为没有修改。",
                "reason": getattr(exc, "code", "REVIEW_UNAVAILABLE"),
            }
        finally:
            if isinstance(baseline, str) and not finalized:
                try:
                    engine, project = self.engine()
                    # A failed, settled turn cannot use this baseline again.
                    # A successful atomic finalization has changed its kind to
                    # review, so release_baseline deliberately refuses deletion.
                    ReviewStore(engine).release_baseline(baseline, project)
                except Exception:
                    pass
