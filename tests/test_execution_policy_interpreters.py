"""Only static temporary-file inspection: no interpreter or model is launched."""
import pytest
from shared.execution_policy import InvocationInspector


@pytest.mark.parametrize('argv,source', [
    (['sh', 'payload'], 'codex exec task\n'),
    (['bash', '-eu', 'payload'], 'codex exec task\n'),
    (['sh', '-o', 'errexit', 'payload'], 'codex exec task\n'),
    (['source', 'payload'], 'codex exec task\n'),
    (['python3', 'payload'], "import subprocess\nsubprocess.run(['codex'])\n"),
    (['python3', '-u', 'payload'], "import subprocess\nsubprocess.run(['codex'])\n"),
    (['python3', '-W', 'ignore', 'payload'], "import subprocess\nsubprocess.run(['codex'])\n"),
    (['node', 'payload'], 'require("child_process").spawn("codex", []);\n'),
    (['node', '--trace-warnings', 'payload'], 'require("child_process").spawn("codex", []);\n'),
    (['pwsh', '-File', 'payload'], 'codex exec task\n'),
])
def test_explicit_interpreter_reads_scripts_without_extension(tmp_path, argv, source):
    (tmp_path/'payload').write_text(source)
    assert InvocationInspector(cwd=tmp_path).argv(argv)


@pytest.mark.parametrize('argv,source', [
    (['sh', 'safe', './codex'], 'printf ok\n'),
    (['sh', 'safe', '-c', 'codex exec task'], 'printf ok\n'),
    (['python3', 'safe', '-c', "import subprocess; subprocess.run(['codex'])"], 'print("ok")\n'),
    (['node', 'safe', '--eval', 'require("child_process").spawn("codex", [])'], 'console.log("ok");\n'),
    (['pwsh', '-File', 'safe', './codex'], 'Write-Output ok\n'),
])
def test_arguments_after_script_filename_are_data(tmp_path, argv, source):
    (tmp_path/'safe').write_text(source)
    (tmp_path/'codex').write_text('#!/bin/sh\nprintf never-run\n')
    assert not InvocationInspector(cwd=tmp_path).argv(argv)


def test_python_module_arguments_are_not_inferred_as_inline_code(tmp_path):
    assert not InvocationInspector(cwd=tmp_path).argv(['python3', '-m', 'pytest', '-c', 'codex'])


def test_node_explicit_preload_is_inspected(tmp_path):
    (tmp_path/'preload').write_text('require("child_process").spawn("codex", []);\n')
    (tmp_path/'main').write_text('console.log("ok");\n')
    assert InvocationInspector(cwd=tmp_path).argv(['node', '--require', './preload', 'main'])


@pytest.mark.parametrize('argv,blocked', [
    (['sudo', '-n', 'codex'], True),
    (['sudo', '-n', 'echo', 'codex'], False),
    (['doas', '-n', 'codex'], True),
    (['doas', '-n', 'echo', 'codex'], False),
    (['nice', '-n', '10', 'codex'], True),
    (['nice', '-n', '10', 'echo', 'codex'], False),
    (['xargs', '-n', '1', 'codex'], True),
    (['xargs', '-n', '1', 'echo', 'codex'], False),
])
def test_wrapper_options_use_their_own_argument_rules(argv, blocked):
    assert InvocationInspector().argv(argv) is blocked


def test_unrecognized_extension_cache_does_not_hide_explicit_interpreter(tmp_path):
    (tmp_path/'payload').write_text('codex exec task\n')
    inspect = InvocationInspector(cwd=tmp_path)
    assert not inspect.argv(['./payload'])
    assert inspect.argv(['sh', 'payload'])
