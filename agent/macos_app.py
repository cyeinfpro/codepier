"""Stable macOS application identity for the managed Agent, outside its runtime.

Python/runtime upgrades retain the launcher bytes and its privacy identity.
The launcher stays alive to provide a stable application attribution candidate.
Actual macOS privacy authorization must still be granted and verified by the user.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import stat
import tempfile

IDENTIFIER = 'com.codepier.agent.app'
SOURCE = r'''
#include <CoreFoundation/CoreFoundation.h>
#include <spawn.h>
#include <signal.h>
#include <sys/wait.h>
#include <errno.h>
#include <stdio.h>
extern char **environ;
static volatile sig_atomic_t child = 0;
static void forward(int sig) { if (child > 0) kill(child, sig); }
int main(int argc, char **argv) {
    CFBundleGetMainBundle();
    if (argc < 2) { fprintf(stderr, "CodePier Agent: managed launch required\n"); return 64; }
    struct sigaction action = {0};
    action.sa_handler = forward;
    sigaction(SIGTERM, &action, NULL); sigaction(SIGINT, &action, NULL);
    sigaction(SIGHUP, &action, NULL);
    pid_t pid;
    int error = posix_spawn(&pid, argv[1], NULL, NULL, argv + 1, environ);
    if (error) { fprintf(stderr, "CodePier Agent: launch failed (%d)\n", error); return 70; }
    child = pid;
    int status;
    while (waitpid(pid, &status, 0) < 0) { if (errno != EINTR) return 71; }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
'''


def executable(base):
    return Path(base) / 'CodePier Agent.app/Contents/MacOS/CodePier Agent'


def safe_path(path, *, directory=False):
    """Reject redirects and files writable by another local account."""
    value = path.lstat()
    if (stat.S_ISLNK(value.st_mode)
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
            or value.st_uid != getattr(os, "getuid", lambda: value.st_uid)() or value.st_mode & 0o022):
        raise ValueError("Unsafe managed launcher path: " + path.name)


def validate(base):
    base = Path(base)
    safe_path(base, directory=True)
    app = base / 'CodePier Agent.app'
    for relative in ('', 'Contents', 'Contents/MacOS', 'Contents/Resources'):
        safe_path(app / relative, directory=True)
    for relative in ('Contents/MacOS/CodePier Agent', 'Contents/Info.plist',
                     'Contents/Resources/launcher-source.sha256'):
        safe_path(app / relative)
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if (info.get('CFBundleIdentifier') != IDENTIFIER
            or info.get('CFBundleExecutable') != 'CodePier Agent'
            or info.get('CFBundlePackageType') != 'APPL'):
        raise ValueError("Existing launcher application identity differs")
    signature = hashlib.sha256(SOURCE.encode()).hexdigest()
    if (app / 'Contents/Resources/launcher-source.sha256').read_text() != signature:
        raise ValueError("Existing launcher source differs; preserve its authorized identity")
    subprocess.run(['codesign', '--verify', '--strict', '-R',
                    '=identifier "' + IDENTIFIER + '"', str(app)], check=True)
    return executable(base)


def build(base):
    base = Path(base).absolute()
    # Validate every existing path component before resolving it.
    for parent in [*reversed(base.parents), base]:
        if parent.is_symlink():
            raise ValueError("Symlink installation directories are not supported")
    safe_path(base, directory=True)
    app = base / 'CodePier Agent.app'
    binary = executable(base)
    signature = hashlib.sha256(SOURCE.encode()).hexdigest()
    if app.exists() or app.is_symlink():
        return validate(base)
    # Build in the same parent for an atomic publication. Never modify the
    # existing authorized launcher as part of a Python/runtime upgrade.
    with tempfile.TemporaryDirectory(prefix='.codepier-app-', dir=base) as folder:
        staged = Path(folder) / app.name
        contents = staged / 'Contents'
        (contents / 'MacOS').mkdir(parents=True)
        (contents / 'Resources').mkdir()
        info = {'CFBundleIdentifier': IDENTIFIER, 'CFBundleName': 'CodePier Agent',
                'CFBundleDisplayName': 'CodePier Agent', 'CFBundleExecutable': 'CodePier Agent',
                'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
                'CFBundleShortVersionString': '1.0', 'LSBackgroundOnly': True,
                'NSDownloadsFolderUsageDescription': 'CodePier Agent 读取你在面板中授权的项目目录。'}
        (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
        source = Path(folder) / 'launcher.c'
        source.write_text(SOURCE)
        subprocess.run(['xcrun', 'clang', '-O2', '-framework', 'CoreFoundation',
                        str(source), '-o', str(contents / 'MacOS/CodePier Agent')], check=True)
        (contents / 'Resources/launcher-source.sha256').write_text(signature)
        subprocess.run(['codesign', '--force', '--sign', '-', '--identifier', IDENTIFIER,
                        str(staged)], check=True)
        validate(Path(folder))
        os.rename(staged, app)
    return validate(base)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Prepare the persistent CodePier Agent macOS app identity')
    parser.add_argument('--install-dir', type=Path, required=True)
    print(build(parser.parse_args().install_dir))
