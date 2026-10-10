"""Inspect synthetic source only; never invoke any of the process APIs below."""
from __future__ import annotations

import pytest

from shared.execution_policy import InvocationInspector, enforce_argv
from shared.util import DevError


FAMILIES = ("v", "vp", "ve", "vpe", "l", "le", "lp", "lpe")


def source(family, executable, arguments, *, spawn=True):
    method = ("spawn" if spawn else "exec") + family
    parts = ["os.P_WAIT"] if spawn else []
    parts.append(repr(executable))
    if family.startswith("v"):
        parts.append(repr(arguments))
    else:
        parts.extend(repr(value) for value in arguments)
    if family.endswith("e"):
        parts.append("{'NOTE': 'codex is environment data'}")
    return f"import os; os.{method}({', '.join(parts)})"


@pytest.mark.parametrize("spawn", [True, False], ids=["spawn", "exec"])
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("executable", ["codex", "/opt/tools/codex", r"C:\Tools\codex.exe"])
def test_executable_position_is_detected_for_every_process_family(family, executable, spawn):
    assert InvocationInspector().python(source(family, executable, ["display-name"], spawn=spawn))


@pytest.mark.parametrize("spawn", [True, False], ids=["spawn", "exec"])
@pytest.mark.parametrize("family", FAMILIES)
def test_interpreter_arguments_are_inspected_after_display_name(family, spawn):
    code = source(family, "/bin/sh", ["display-name", "-c", "codex exec synthetic"], spawn=spawn)
    assert InvocationInspector().python(code)


@pytest.mark.parametrize("spawn", [True, False], ids=["spawn", "exec"])
@pytest.mark.parametrize("family", FAMILIES)
def test_argument_and_environment_mentions_are_not_executables(family, spawn):
    assert not InvocationInspector().python(source(family, "/bin/echo", ["codex", "codex"], spawn=spawn))
    # A process path is not a shell fragment.
    assert not InvocationInspector().python(source(family, "echo codex", ["display-name"], spawn=spawn))


@pytest.mark.parametrize("code", [
    "import os as process; process.spawnv(process.P_WAIT, 'codex', ['display'])",
    "from os import spawnv as launch; launch(0, 'codex', ['display'])",
    "from os import execv as launch; launch('codex', ['display'])",
    "import os; os.spawnve(mode=0, path='codex', args=['display'], env={})",
    "import os; os.spawnvpe(mode=0, file='codex', args=['display'], env={})",
    "import os; os.execve(path='codex', args=['display'], env={})",
    "import os; os.execvpe(file='codex', args=['display'], env={})",
    "import os; target = 'co' + 'dex'; os.spawnv(0, target, ['display'])",
    "import os; os.spawnv(0, 'python3', ['display', '-c', \"import os; os.execv('codex', ['display'])\"])",
])
def test_static_alias_keyword_and_nested_interpreter_targets(code):
    assert InvocationInspector().python(code)


@pytest.mark.parametrize("code", [
    "import os; os.spawnv('codex', '/bin/echo', ['display'])",
    "import os; os.spawnv(0, '/bin/echo', ['codex'])",
    "import os; os.execv('/bin/echo', ['codex'])",
    "import os; os.spawnv(0, '/bin/echo', ['display', '-c', 'codex'])",
    "import os; os.spawnv(0, '/bin/sh', ['display', '-c', 'printf codex'])",
])
def test_mode_and_display_name_are_not_executable_positions(code):
    assert not InvocationInspector().python(code)


def test_agent_guard_rejects_synthetic_spawn_source_without_execution(tmp_path):
    code = source("v", "codex", ["display-name"])
    with pytest.raises(DevError) as caught:
        enforce_argv(
            {"mcp_policy": {"block_local_codex": True}},
            {},
            ["python3", "-c", code],
            tmp_path,
            {},
        )
    assert caught.value.code == "CODEX_REMOTE_DISABLED"
