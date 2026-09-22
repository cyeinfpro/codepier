"""Protocol and installer failure recovery without real network/deployment changes."""
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from scripts import mcp_stdio_bridge as bridge

BASE = Path(__file__).resolve().parent.parent


def run_bridge(monkeypatch, tmp_path, raw, handler):
    token = tmp_path / "pat"
    token.write_text("rd_" + "x" * 43)
    token.chmod(0o600)
    monkeypatch.setenv("CODEPIER_TOKEN_FILE", str(token))
    monkeypatch.setenv("CODEPIER_HUB_URL", "http://fixture")
    monkeypatch.setenv("CODEPIER_RETRY_SECONDS", "1")
    monkeypatch.setattr(bridge.sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    client_factory = httpx.Client
    monkeypatch.setattr(bridge.httpx, "Client", lambda **kw: client_factory(
        transport=httpx.MockTransport(handler), **kw))
    return bridge.main()


@pytest.mark.parametrize("raw,code", [
    (b"broken-json", -32700), (b'{"n":NaN}', -32700),
    (b"[]", -32600), (b"null", -32600), (b"123", -32600),
    (b'{"id":1,"method":"ping"}', -32600),
    (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600),
    (b'{"jsonrpc":"2.0","id":[],"method":"ping"}', -32600),
    (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"n":1e999}}', -32600),
    (b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}', -32600),
    (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":[]}', -32602),
    (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":[],"arguments":{}}}', -32602),
])
def test_invalid_input_gets_protocol_error_and_next_call_continues(monkeypatch, tmp_path, capsys, raw, code):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {}})

    next_call = b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    assert run_bridge(monkeypatch, tmp_path, raw + b"\n" + next_call, handler) == 0
    replies = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert replies[0]["error"]["code"] == code
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [
    {"jsonrpc": "2.0", "id": 1},
    {"jsonrpc": "2.0", "id": True, "result": {}},
    {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
    {"jsonrpc": "2.0", "id": 1, "result": None},
    {"jsonrpc": "2.0", "id": 1, "error": {"code": True, "message": "bad"}},
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}},
    {"jsonrpc": "2.0", "id": 1, "result": {"text": "\ud800"}},
])
def test_corrupt_response_envelope_retries_identical_call(payload):
    sent = []
    request = bridge.prepare_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "fs_write", "arguments": {"path": "example.txt"}}})

    def handler(http_request):
        sent.append(json.loads(http_request.content))
        response = payload if len(sent) == 1 else {"jsonrpc": "2.0", "id": 1, "result": {}}
        return httpx.Response(200, content=json.dumps(response).encode())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert bridge.forward(client, "http://fixture", request, {}, sleep=lambda _: None)["result"] == {}
    assert len(sent) == 2 and sent[0] == sent[1]


@pytest.mark.parametrize("key", [False, 0, [], {}])
def test_invalid_idempotency_key_is_not_silently_replaced_with_valid_mutation_key(key):
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "fs_write", "arguments": {"idempotency_key": key}}}
    assert bridge.prepare_request(request) == request


@pytest.mark.parametrize("method,result", [
    ("initialize", {}), ("initialize", {"protocolVersion": []}),
    ("initialize", {"protocolVersion": "unknown"}),
    ("tools/list", {"tools": None}),
    ("tools/list", {"tools": [{"_meta": None}]}),
])
def test_invalid_session_result_is_rejected_before_state_update(method, result):
    with pytest.raises(ValueError):
        bridge.validate_response({"jsonrpc": "2.0", "id": 1, "result": result},
                                 {"id": 1, "method": method})


def test_202_response_cannot_silently_drop_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(202)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            bridge.forward(client, "http://fixture", {"id": 1}, {}, sleep=lambda _: None)
    assert len(calls) == 6


@pytest.mark.parametrize("status", [202, 204])
def test_notifications_have_no_response(status):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
        assert bridge.forward(client, "http://fixture", {"method": "notifications/initialized"}, {}) is None


def test_token_rotation_failure_is_local_and_bridge_can_recover(monkeypatch, tmp_path, capsys):
    token_reads, calls = [], []

    def rotating_token(_):
        token_reads.append(True)
        if len(token_reads) == 2:
            raise PermissionError("injected permission change")
        return "rd_" + "x" * 43

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {}})

    monkeypatch.setattr(bridge, "token_from_file", rotating_token)
    raw = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    assert run_bridge(monkeypatch, tmp_path, raw, handler) == 0
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert replies[0]["error"]["data"] == {"retryable": False, "forwarded": False}
    assert replies[1]["result"] == {}
    assert len(calls) == 1 and calls[0]["id"] == 2


def test_sleep_reaching_deadline_does_not_make_another_http_attempt():
    calls, now = [], [0.0]

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "5"})

    def sleep(seconds):
        now[0] += seconds

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            bridge.forward(client, "http://fixture", {"id": 1}, {}, retry_seconds=1,
                           sleep=sleep, clock=lambda: now[0])
    assert len(calls) == 1 and now[0] == 1


@pytest.mark.parametrize("value", ["NaN", "inf", "-inf"])
def test_nonfinite_retry_duration_fails_at_startup(monkeypatch, tmp_path, capsys, value):
    token = tmp_path / "pat"
    token.write_text("rd_" + "x" * 43)
    token.chmod(0o600)
    monkeypatch.setenv("CODEPIER_TOKEN_FILE", str(token))
    monkeypatch.setenv("CODEPIER_RETRY_SECONDS", value)
    assert bridge.main() == 2
    assert not capsys.readouterr().out


@pytest.mark.parametrize("suffix", ["\nInjected: value", "\x00", "\tfoo", "é"])
def test_token_content_cannot_poison_http_headers(tmp_path, suffix):
    token = tmp_path / "pat"
    token.write_text("rd_" + "x" * 43 + suffix)
    token.chmod(0o600)
    with pytest.raises(ValueError):
        bridge.token_from_file(token)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file descriptors and FIFOs")
def test_token_fifo_is_rejected_without_blocking(tmp_path):
    token = tmp_path / "pat"
    os.mkfifo(token, mode=0o600)
    with pytest.raises(ValueError):
        bridge.token_from_file(token)


@pytest.mark.skipif(os.name == "nt", reason="POSIX replacement while a descriptor is open")
def test_token_read_uses_the_descriptor_that_passed_permission_checks(tmp_path, monkeypatch):
    token = tmp_path / "pat"
    token.write_text("rd_" + "x" * 43)
    token.chmod(0o600)
    replacement = tmp_path / "replacement"
    replacement.write_text("rd_" + "y" * 43)
    replacement.chmod(0o644)
    real_fstat = os.fstat

    def replace_after_check(fd):
        info = real_fstat(fd)
        replacement.replace(token)
        return info

    monkeypatch.setattr(bridge.os, "fstat", replace_after_check)
    assert bridge.token_from_file(token) == "rd_" + "x" * 43


def fake_hub_installer(tmp_path, *, probe_status=0, start_status=0):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "bin").mkdir()
    shutil.copy(BASE / "deploy/install-hub.sh", tmp_path / "deploy/install-hub.sh")
    # These tests exercise the shell's init/error flow; migration internals have
    # separate real SQLite, Docker and proxy regression coverage.
    (tmp_path / "scripts").mkdir()
    for name in ("rename_checkout.py", "migrate_hub.py"):
        (tmp_path / "scripts" / name).write_text(
            "import os,sys\nfrom pathlib import Path\n"
            "with Path(os.environ['DOCKER_LOG']).open('a') as f: f.write(Path(__file__).name+' '+' '.join(sys.argv[1:])+'\\n')\n"
            "if len(sys.argv)>1 and sys.argv[1]=='probe-volume': print('codepier_hub_data')\n")
    (tmp_path / ".env").write_text("HUB_PUBLIC_URL=http://fixture:8765\n")
    fake = tmp_path / "bin/docker"
    fake.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case "$*" in
  'compose version'|'compose config --quiet'|'compose build hub') exit 0 ;;
  *'python -c '*)
    [[ "$*" == *'--volume codepier_hub_data:/app/data:ro'* ]] || exit 98
    exit "$PROBE_STATUS" ;;
  *'python -m hub init'*) exit 0 ;;
  'compose up '*) exit "$START_STATUS" ;;
esac
exit 99
""")
    fake.chmod(0o700)
    log = tmp_path / "docker.log"
    result = subprocess.run(["bash", str(tmp_path / "deploy/install-hub.sh")],
        input="admin\n", text=True, capture_output=True, timeout=5,
        env={**os.environ, "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"],
             "DOCKER_LOG": str(log), "PROBE_STATUS": str(probe_status), "START_STATUS": str(start_status),
             "CODEPIER_BOOTSTRAP_PYTHON": sys.executable})
    return result, log.read_text()


@pytest.mark.parametrize("status", [1, 2, 125])
def test_hub_probe_failure_never_attempts_reinitialization(tmp_path, status):
    result, log = fake_hub_installer(tmp_path, probe_status=status)
    assert result.returncode == status
    assert "python -m hub init" not in log and "compose up " not in log


def test_hub_new_volume_initializes_once_and_waits_for_health(tmp_path):
    result, log = fake_hub_installer(tmp_path, probe_status=3)
    assert result.returncode == 0
    assert log.count("python -m hub init") == 1
    assert "compose up -d --wait --wait-timeout 90" in log
    assert (log.index('--volume codepier_hub_data:/app/data:ro')
            < log.index('migrate_hub.py proxy-trust')
            < log.index('migrate_hub.py write-boundary')
            < log.index('python -m hub init') < log.index('compose up -d'))


def test_hub_health_failure_is_not_reported_as_started(tmp_path):
    result, log = fake_hub_installer(tmp_path, start_status=17)
    assert result.returncode == 17
    assert "Hub 已启动" not in result.stdout
    assert "python -m hub init" not in log


@pytest.mark.parametrize("pairing,root", [("pairing.json", "projects"), ("~/pairing.json", "~/projects")])
def test_agent_installer_resolves_inputs_from_callers_directory(tmp_path, pairing, root):
    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / ".venv/bin").mkdir(parents=True)
    shutil.copy(BASE / "deploy/install-agent.sh", checkout / "deploy/install-agent.sh")
    fake_python = checkout / ".venv/bin/python"
    fake_python.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >> \"$PYTHON_LOG\"\n")
    fake_python.chmod(0o700)
    caller = tmp_path / "caller"
    caller.mkdir()
    log = tmp_path / "python.log"
    result = subprocess.run(["bash", str(checkout / "deploy/install-agent.sh"), pairing, root],
        cwd=caller, text=True, capture_output=True, timeout=5,
        env={**os.environ, "PYTHON": str(fake_python), "PYTHON_LOG": str(log)})
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = log.read_text().splitlines()
    assert arguments[-4:] == ["--pairing-file", pairing if pairing.startswith("~") else str(caller / pairing),
                              "--allow", root if root.startswith("~") else str(caller / root)]
