#!/usr/bin/env python3
"""Windows structured-chat pipe/Job smoke verifier (honest non-Windows status)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.native_process import WindowsPipeTransport, structured_chat_platform_support
from shared.native_cli import process_exists


def _non_windows_report() -> dict[str, object]:
    support = structured_chat_platform_support(os_name="nt")
    return {
        "status": "UNVERIFIED_NON_WINDOWS_HOST",
        "os": os.name,
        "platform": sys.platform,
        "stage": support.stage,
        "structured_chat_available": support.available,
        "reason_code": support.reason_code,
        "native_cli_model_test": False,
    }


def _windows_smoke_report() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="codepier-win-chat-smoke-") as temporary:
        root = Path(temporary)
        child = root / "pipe_echo.py"
        child.write_text(
            (
                "import json, sys\n"
                "sys.stdout.buffer.write('{\"ready\":\"世界😀\"}\\r\\n'.encode('utf-8'))\n"
                "sys.stdout.buffer.flush()\n"
                "sys.stderr.buffer.write(b'diagnostic stderr line\\n')\n"
                "sys.stderr.buffer.flush()\n"
                "for line in sys.stdin.buffer:\n"
                "    msg = json.loads(line.decode('utf-8'))\n"
                "    out = json.dumps({'echo': msg}, ensure_ascii=False).encode('utf-8') + b'\\n'\n"
                "    sys.stdout.buffer.write(out)\n"
                "    sys.stdout.buffer.flush()\n"
                "    if msg.get('quit'):\n"
                "        break\n"
            ),
            encoding="utf-8",
        )
        transport = WindowsPipeTransport()
        transport.start([sys.executable, "-u", str(child)], root, dict(os.environ))
        events: list[dict] = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not any(
            ev.get("kind") == "stdout_line" for ev in events
        ):
            events.extend(transport.drain_events(time.monotonic() + 0.5))
        transport.write(
            (json.dumps({"hello": "世界😀", "quit": True}, ensure_ascii=False) + "\n").encode(
                "utf-8"
            ),
            "r" * 32,
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not any(ev.get("kind") == "eof" for ev in events):
            events.extend(transport.drain_events(time.monotonic() + 0.5))
        pid = transport._proc.pid if transport._proc else 0
        exit_state = transport.stop_and_drain(timeout=3.0)
        return {
            "status": "PASSED_WINDOWS_PIPE_SMOKE"
            if exit_state.output_complete
            else "FAILED_WINDOWS_PIPE_SMOKE",
            "os": os.name,
            "platform": sys.platform,
            "stage": "Win-1",
            "native_cli_model_test": False,
            "eof_seen": exit_state.eof_seen,
            "process_exited": exit_state.process_exited,
            "output_complete": exit_state.output_complete,
            "process_gone": not process_exists(pid) if pid else True,
            "stdout_lines": [ev.get("data") for ev in events if ev.get("kind") == "stdout_line"],
            "stderr_tail": exit_state.stderr_tail,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    report = _windows_smoke_report() if os.name == "nt" else _non_windows_report()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    if os.name == "nt" and report.get("status") != "PASSED_WINDOWS_PIPE_SMOKE":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
