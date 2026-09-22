"""Keep native parser crashes in a child process so pytest reports a failure."""
import subprocess
import sys
from pathlib import Path


def test_large_coordinates_survive_gc_and_concurrent_analysis():
    # Values above CPython's small-integer cache expose the borrowed-reference
    # bug in Tree-sitter 0.26.0 Point.row/column getters. Tiny fixtures miss it.
    script = '''
import gc
from concurrent.futures import ThreadPoolExecutor
from agent.symbols import analyze

source = (" " * 300 + "function target(){return dependency;}\\n") * 400
def check(extension):
    result = analyze("fixture" + extension, source.encode())
    assert len(result["symbols"]) == 400
    assert result["symbols"][-1]["line"] == 400
    assert result["symbols"][-1]["column"] == 301
    assert len(result["references"]) == 400
    assert result["references"][-1]["line"] == 400
    gc.collect()

for extension in (".js", ".ts", ".tsx"):
    for _ in range(3):
        check(extension)
# Exercise both admitted worker slots, not intentional overload rejection.
# The separate saturation test below verifies bounded admission without retry.
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(check, [".js", ".ts", ".tsx"] * 4))
print("parser stability passed")
'''
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "parser stability passed" in result.stdout


def test_saturated_parser_rejects_without_starting_or_leaking_a_worker(monkeypatch):
    import pytest
    from agent import symbol_worker
    from shared.util import DevError

    assert symbol_worker._SLOTS.acquire(blocking=False)
    assert symbol_worker._SLOTS.acquire(blocking=False)
    try:
        def unexpected_start():
            raise AssertionError('a saturated request must not create a process')
        with monkeypatch.context() as patch:
            patch.setattr(symbol_worker, '_worker_command', unexpected_start)
            with pytest.raises(DevError) as error:
                symbol_worker.analyze_isolated('fixture.js', b'let x = 1;', timeout=.01)
            assert error.value.code == 'PARSER_BUSY'
    finally:
        symbol_worker._SLOTS.release()
        symbol_worker._SLOTS.release()
    result = symbol_worker.analyze_isolated('fixture.js', b'function restored(){}')
    assert result['symbols'][0]['name'] == 'restored'
