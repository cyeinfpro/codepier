"""CI APT timeouts reach root subprocesses without running real sudo or installs."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
APT_FILE = '/etc/apt/apt.conf.d/99-codepier-ci-network'
APT_OPTIONS = {
    'Acquire::http::Timeout': '30',
    'Acquire::https::Timeout': '30',
    'Acquire::Retries': '2',
}
INSTALL_STEPS = {
    'verify': 'Fresh environments; cached downloads, never shared virtualenvs',
    'oidc-authentik': 'Install locked OIDC and browser test tools',
}


def workflow_steps(job):
    return yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs'][job]['steps']


def named_step(steps, name):
    return next(step for step in steps if step.get('name') == name)


@pytest.mark.parametrize('job', INSTALL_STEPS)
def test_ci_bounds_existing_linux_installations_without_changing_trust(job):
    steps = workflow_steps(job)
    step = named_step(steps, 'Bound Linux APT download waits')
    assert step['if'] == "runner.os == 'Linux'"
    assert step['timeout-minutes'] == 2
    assert step['shell'] == 'bash'
    script = step['run']
    assert APT_FILE in script
    assert script.count('sudo apt-config dump') == 3
    directives = script.split("<<'APT'\n", 1)[1].split('\nAPT', 1)[0]
    assert directives.splitlines() == [f'{key} "{value}";' for key, value in APT_OPTIONS.items()]
    for forbidden in ['APT_CONFIG=', 'sources.list', 'apt-mirrors', '--allow-unauthenticated',
                      'trusted=yes', 'Verify-Peer', 'Verify-Host', 'Check-Valid-Until',
                      'apt-get install', 'pip install', 'npm install']:
        assert forbidden not in script
    install = named_step(steps, INSTALL_STEPS[job])
    assert steps.index(step) < steps.index(install)
    assert install['timeout-minutes'] == 15
    assert not install.get('continue-on-error', False)
    assert '.venv/bin/python -m playwright install --with-deps chromium' in install['run']
    assert 'requirements-dev.txt' in install['run']
    if job == 'verify':
        assert '--with-deps chromium webkit' in install['run']
        assert install['run'].count('--require-hashes') == 2
        assert 'requirements-compat.txt' in install['run']


@pytest.mark.parametrize('job', INSTALL_STEPS)
@pytest.mark.parametrize('failure', [None, 'tee', 'apt-config', *APT_OPTIONS])
def test_ci_root_config_is_verified_with_scrubbed_environment(tmp_path, job, failure):
    script = named_step(workflow_steps(job), 'Bound Linux APT download waits')['run']
    binary = tmp_path / 'bin'
    binary.mkdir()
    config = tmp_path / 'simulated-root-apt.conf'
    calls = tmp_path / 'calls.jsonl'
    stub = binary / 'sudo'
    stub.write_text(f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

config = Path({str(config)!r})
calls = Path({str(calls)!r})
failure = {failure!r}
args = sys.argv[1:]
with calls.open('a') as output:
    output.write(json.dumps(args) + '\\n')
# Simulate sudo's environment reset, including APT_CONFIG.
os.environ.clear()
if args == ['tee', {APT_FILE!r}]:
    if failure == 'tee':
        raise SystemExit(7)
    text = sys.stdin.read()
    config.write_text(text)
    sys.stdout.write(text)
elif args == ['apt-config', 'dump']:
    if failure == 'apt-config':
        # Even valid-looking output must not hide apt-config's failing exit code.
        sys.stdout.write(config.read_text())
        sys.stdout.flush()
        raise SystemExit(9)
    text = config.read_text()
    if failure:
        text = '\\n'.join(failure + ' "999";' if line.startswith(failure + ' ')
                         else line for line in text.splitlines()) + '\\n'
    sys.stdout.write(text)
else:
    raise SystemExit('Refusing unexpected simulated root command: ' + repr(args))
""")
    stub.chmod(0o755)
    environment = {
        **os.environ,
        'PATH': str(binary) + os.pathsep + os.environ.get('PATH', ''),
        'APT_CONFIG': '/environment-only-setting-must-not-be-needed',
    }
    environment.pop('BASH_ENV', None)
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script],
                            cwd=tmp_path, env=environment, text=True,
                            capture_output=True, timeout=10)
    if failure is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
    if failure == 'tee':
        assert not config.exists()
    else:
        assert config.read_text() == ''.join(f'{key} "{value}";\n' for key, value in APT_OPTIONS.items())
    observed = [json.loads(line) for line in calls.read_text().splitlines()]
    assert observed[0] == ['tee', APT_FILE]
    assert all(call == ['apt-config', 'dump'] for call in observed[1:])
    expected_calls = {'tee': 1, 'apt-config': 2}
    expected_calls.update({key: index + 2 for index, key in enumerate(APT_OPTIONS)})
    assert len(observed) == expected_calls.get(failure, 4)
