"""CI APT timeouts reach root subprocesses without running real sudo or installs."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
APT_FILE = '/etc/apt/apt.conf.d/zzz-codepier-ci-network'
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
    expected = "runner.os == 'Linux'" + (" && matrix.shard != 4" if job == 'verify' else "")
    assert step['if'] == expected
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
@pytest.mark.parametrize('failure', [None, 'tee', 'apt-config', 'earlier-fragment', *APT_OPTIONS])
def test_ci_root_config_is_verified_with_scrubbed_environment(tmp_path, job, failure):
    script = named_step(workflow_steps(job), 'Bound Linux APT download waits')['run']
    apt_file = APT_FILE
    if failure == 'earlier-fragment':
        apt_file = '/etc/apt/apt.conf.d/99-codepier-ci-network'
        script = script.replace(APT_FILE, apt_file)
    binary = tmp_path / 'bin'
    binary.mkdir()
    config_dir = tmp_path / 'simulated-root-apt.conf.d'
    config_dir.mkdir()
    # The actual Ubuntu 24.04 runner (ubuntu24/20260927.320) writes this
    # late-loading preset in images/ubuntu/scripts/build/configure-apt.sh.
    (config_dir / 'zz-retries').write_text(
        'Acquire::Retries "1";\nAcquire::http::Timeout "15";\nAcquire::https::Timeout "15";\n')
    config = config_dir / Path(apt_file).name
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
if args == ['tee', {apt_file!r}]:
    if failure == 'tee':
        raise SystemExit(7)
    text = sys.stdin.read()
    config.write_text(text)
    sys.stdout.write(text)
elif args == ['apt-config', 'dump']:
    # APT loads fragments alphabetically; later values replace earlier ones.
    values = {{}}
    for fragment in sorted(config.parent.iterdir()):
        for line in fragment.read_text().splitlines():
            key, value = line.split(' ', 1)
            values[key] = value.strip().removesuffix(';').strip('"')
    if failure in {list(APT_OPTIONS)!r}:
        values[failure] = '999'
    text = ''.join(key + ' "' + value + '";\\n' for key, value in values.items())
    sys.stdout.write(text)
    if failure == 'apt-config':
        # Even valid-looking output must not hide apt-config's failing exit code.
        sys.stdout.flush()
        raise SystemExit(9)
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
    assert observed[0] == ['tee', apt_file]
    assert all(call == ['apt-config', 'dump'] for call in observed[1:])
    expected_calls = {'tee': 1, 'apt-config': 2, 'earlier-fragment': 2}
    expected_calls.update({key: index + 2 for index, key in enumerate(APT_OPTIONS)})
    assert len(observed) == expected_calls.get(failure, 4)


@pytest.mark.parametrize('shard', [0, 1, 2, 3, 4])
def test_dedicated_resource_job_does_not_install_browsers(tmp_path, shard):
    install = named_step(workflow_steps('verify'), INSTALL_STEPS['verify'])['run']
    boundary = "if [[ '${{ matrix.shard }}' != '4' ]]; then"
    assert install.count(boundary) == 1
    script = install[install.index(boundary):].replace('${{ matrix.shard }}', str(shard))
    # Execute the real conditional with an inert interpreter substitute.
    binary = tmp_path / '.venv/bin'
    binary.mkdir(parents=True)
    python = binary / 'python'
    python.write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    python.chmod(0o755)
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script],
                            cwd=tmp_path, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in result.stdout.splitlines()]
    expected = [['-m', 'playwright', 'install', '--with-deps', 'chromium', 'webkit']] if shard < 4 else []
    assert calls == expected
