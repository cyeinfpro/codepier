import asyncio
import os
import sys

import pytest

from agent.operation_logs import OperationLog
from agent.journal import Journal
from agent.runner import Agent
from shared.secret_output import SecretOutputs


@pytest.fixture
def journal(tmp_path):
    value = Journal(tmp_path / "state")
    yield value
    value.db.close()


def test_log_preserves_prefix_with_explicit_cap(tmp_path):
    log = OperationLog(tmp_path, limit=8)
    log.append("first---")
    log.append("last")
    log.close()
    assert (tmp_path / "output.redacted.log").read_text() == "first---"
    assert log.receipt("op")["status"] == "truncated"
    assert log.receipt("op")["bytes"] == 8


def test_log_refuses_existing_file(tmp_path):
    path = tmp_path / "output.redacted.log"
    path.write_text("keep")
    log = OperationLog(tmp_path)
    log.append("replace")
    log.close()
    assert path.read_text() == "keep"
    assert log.receipt("op")["status"] == "write_failed"


def test_log_does_not_follow_file_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    (tmp_path / "output.redacted.log").symlink_to(target)
    log = OperationLog(tmp_path)
    log.append("replace")
    log.close()
    assert target.read_text() == "keep"
    assert log.error


def test_log_disk_failure_does_not_disclose_error(tmp_path, monkeypatch):
    log = OperationLog(tmp_path)

    def fail(*args):
        raise OSError("secret value and private path")

    monkeypatch.setattr(os, "write", fail)
    log.append("safe")
    log.close()
    assert log.receipt("op")["status"] == "write_failed"
    assert "secret" not in str(log.receipt("op"))


def test_redaction_precedes_log_and_handles_split_secret(tmp_path):
    log = OperationLog(tmp_path)
    redact = SecretOutputs(["synthetic-secret"])
    log.append(redact.feed("first synthetic-"))
    log.append(redact.feed("secret last", final=True))
    log.close()
    assert (tmp_path / "output.redacted.log").read_text() == "first <redacted> last"


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failure", "timeout"])
async def test_runner_spools_redacted_full_output_beyond_tail(tmp_path, journal, ending):
    agent = Agent.__new__(Agent)
    agent.journal = journal
    agent.processes = {}
    agent.cancelled = set()
    agent.stop_event = asyncio.Event()
    agent.output_wake = asyncio.Event()
    agent.pending_output = {}
    log = OperationLog(tmp_path)
    agent._execution_logs = {"log": log}
    journal.start("log", {"tool": "exec"})
    journal.mark_running("log")
    code = "import sys,time; print('synthetic-secret FIRST'); print('x'*150000); print('LAST',flush=True); "
    code += {"success": "pass", "failure": "sys.exit(7)", "timeout": "time.sleep(5)"}[ending]
    result = await agent._run_process_account(
        "log",
        [sys.executable, "-c", code],
        tmp_path,
        0.5 if ending == "timeout" else 10,
        environment_override=dict(os.environ),
        redaction_value="synthetic-secret",
    )
    log.close()
    content = (tmp_path / "output.redacted.log").read_text()
    if ending == "timeout":
        assert result["timed_out"]
    else:
        assert result["exit_code"] == (7 if ending == "failure" else 0)
    assert "FIRST" not in result["output"]
    assert content.startswith("<redacted> FIRST")
    assert content.endswith("LAST\n")
    assert "synthetic-secret" not in content
    assert log.receipt("log")["status"] == "complete"
