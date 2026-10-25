"""Native saved-VPS readiness must not borrow the clean project's PATH."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.project_runtime import validate_runtimes
from agent.shell import execution_info, validate_shell
from agent.ssh import prepare_ssh
from shared.util import DevError


class SSHTransportReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.native = self.root / 'native'
        self.clean = self.root / 'clean'
        self.empty = self.root / 'empty'
        for directory in (self.native, self.clean, self.empty):
            directory.mkdir()
        self.project = {'id': 'fixture', 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True}
        self.spec = {'writable': True, 'allow_tasks': True}
        self.config = {
            'state_dir': str(self.root / 'state'),
            'shell': validate_shell({'enabled': True, 'projects': ['fixture'], 'command': ['/bin/sh', '-c']}),
            'project_runtimes': validate_runtimes({'fixture': {
                'mode': 'project_clean_v1', 'path': [str(self.clean)],
                'credentials': {'github_cli': False, 'git_config': False, 'ssh_agent': False},
            }}),
        }
        self.args = {'timeout_seconds': 30, 'known_hosts_file': '', 'host_key_policy': 'strict',
                     'password': 'synthetic-only', 'username': 'fixture', 'host': 'fixture.invalid',
                     'port': 22, 'command': 'true'}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'PATH': str(self.native), 'HOME': str(self.root)}, clear=True).start()
        patch.object(os, 'defpath', str(self.empty)).start()

    def tools(self, directory):
        for name in ('ssh', 'sshpass'):
            file = directory / name
            file.write_text('#!/bin/sh\nexit 99\n')
            file.chmod(0o700)

    def info(self):
        return execution_info(self.config, self.project, self.spec, self.root)

    def prepare(self):
        return prepare_ssh(self.config, self.project, self.spec, self.args)

    def test_project_tools_do_not_imply_native_ssh_availability(self):
        self.tools(self.clean)
        info = self.info()
        self.assertTrue(info['tools']['sshpass'])
        self.assertFalse(info['ssh']['password_auth_available'])
        with self.assertRaises(DevError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'SSH_DEPENDENCY_MISSING')

    def test_native_ssh_does_not_require_clean_project_tools(self):
        self.tools(self.native)
        info = self.info()
        self.assertFalse(info['tools']['sshpass'])
        self.assertTrue(info['ssh']['password_auth_available'])
        command, _ = self.prepare()
        self.assertEqual(command[0], str(self.native / 'sshpass'))
        self.assertEqual(command[2], str(self.native / 'ssh'))

    def test_shell_path_override_is_shared_with_preparation(self):
        self.tools(self.native)
        self.tools(self.clean)
        self.config['shell']['env']['PATH'] = str(self.empty)
        self.assertFalse(self.info()['ssh']['password_auth_available'])
        with self.assertRaises(DevError):
            self.prepare()
        self.config['shell']['env']['PATH'] = str(self.clean)
        os.environ['PATH'] = str(self.empty)
        self.assertTrue(self.info()['ssh']['password_auth_available'])
        command, environment = self.prepare()
        self.assertEqual(command[0], str(self.clean / 'sshpass'))
        self.assertEqual(environment['PATH'], str(self.clean))

    def test_missing_parent_path_uses_the_same_default(self):
        self.tools(self.native)
        del os.environ['PATH']
        with patch.object(os, 'defpath', str(self.native)):
            self.assertTrue(self.info()['ssh']['password_auth_available'])
            self.assertEqual(self.prepare()[0][0], str(self.native / 'sshpass'))

    def test_explicit_empty_path_does_not_fall_back(self):
        self.tools(self.native)
        self.tools(self.clean)
        self.config['shell']['env']['PATH'] = ''
        previous = Path.cwd()
        try:
            os.chdir(self.empty)
            self.assertFalse(self.info()['ssh']['password_auth_available'])
            with self.assertRaises(DevError):
                self.prepare()
        finally:
            os.chdir(previous)

    def test_runtime_requirement_does_not_claim_native_transport(self):
        self.tools(self.native)
        self.project['_runtime_requirement'] = {'mode': 'container_v1'}
        self.assertFalse(self.info()['ssh']['password_auth_available'])

    def test_container_runtime_does_not_claim_native_transport(self):
        self.tools(self.native)
        runtime = self.info()['runtime']
        with patch('agent.project_runtime.runtime_readiness', return_value={**runtime, 'mode': 'container_v1'}):
            self.assertFalse(self.info()['ssh']['password_auth_available'])

    def test_denial_still_precedes_dependency_lookup(self):
        self.config['shell']['enabled'] = False
        with patch('agent.ssh.shutil.which', side_effect=AssertionError('Unexpected lookup')):
            with self.assertRaises(DevError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, 'SHELL_DISABLED')

    def test_probe_has_no_process_or_configuration_side_effects(self):
        self.tools(self.native)
        before = copy.deepcopy(self.config)
        with patch('subprocess.Popen', side_effect=AssertionError('Unexpected process')):
            info = self.info()
        self.assertEqual(before, self.config)
        self.assertFalse((self.root / 'state').exists())
        self.assertNotIn('synthetic-only', json.dumps(info))


if __name__ == '__main__':
    unittest.main()
