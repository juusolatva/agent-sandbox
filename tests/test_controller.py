import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import controller
import execution


def bundle(content=b'hello\n', name='source.txt', executable=False):
    return {name: {'data': base64.b64encode(content).decode(), 'executable': executable}}


class TransferTests(unittest.TestCase):
    def test_invalid_paths_are_rejected(self):
        for name in ('../escape', '/absolute', '.venv/attack', 'a/../escape', '.env', './a'):
            candidate = bundle(name=name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                controller.transfer.validate(candidate)

    def test_export_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bad').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                controller.transfer.export(root)

    def test_restore_preserves_dependencies_and_handles_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.venv').mkdir()
            (root / '.venv' / 'trusted').write_text('dependency')
            (root / 'old.txt').write_text('old')
            controller.transfer.restore(bundle(), root)
            self.assertFalse((root / 'old.txt').exists())
            self.assertEqual((root / '.venv' / 'trusted').read_text(), 'dependency')
            self.assertEqual((root / 'source.txt').read_bytes(), b'hello\n')

    def test_patch_applies_binary_modes_and_missing_newline(self):
        before = bundle(b'old', 'source.txt')
        after = bundle(b'new', 'source.txt', True) | bundle(b'\0binary', 'new.bin')
        diff, omitted = controller.patch_text(before, after)
        self.assertFalse(omitted)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.txt').write_bytes(b'old')
            subprocess.run(['git', 'apply', '-'], cwd=root, input=diff.encode(), check=True)
            self.assertEqual((root / 'source.txt').read_bytes(), b'new')
            self.assertEqual((root / 'new.bin').read_bytes(), b'\0binary')
            self.assertTrue((root / 'source.txt').stat().st_mode & 0o111)

    def test_worker_arguments_are_fixed_and_cleanup_always_runs(self):
        record = {'image': 'sha256:fixture', 'config': {}}
        worker = execution.Worker(record)
        with patch.object(execution, 'bounded_process', return_value=(124, 'timeout')) as run, \
             patch.object(execution.subprocess, 'run') as cleanup:
            code, _ = worker.execute('untrusted command')
        args = run.call_args.args[0]
        self.assertEqual(code, 124)
        self.assertEqual(args[args.index('--network')+1], 'none')
        self.assertIn('--read-only', args)
        self.assertNotIn('/var/run/docker.sock', ' '.join(args))
        self.assertEqual(cleanup.call_args.args[0][:3], ['docker', 'rm', '-f'])

    def test_run_refuses_unprotected_stage2_state(self):
        with self.assertRaises(ValueError):
            controller.run({'image': 'sha256:fixture', 'config': {}}, 'task', '/tmp/unused')

    def test_bounded_output_and_timeout(self):
        code, output = execution.bounded_process(
            ['python3', '-c', 'import os; os.write(1, b"x" * 1000000)'], 5, 100)
        self.assertEqual(code, 0)
        self.assertLess(len(output), 200)
        code, _ = execution.bounded_process(['sleep', '2'], .05)
        self.assertEqual(code, 124)

    def test_digest_covers_output_beyond_retention_limit(self):
        # Same retained prefix, different later failure: repetition must not treat them as equal.
        digests = []
        for tail in ('first failure', 'second failure'):
            digest = hashlib.sha256()
            _, output = execution.bounded_process(
                ['python3', '-c', f'print("x" * 20000); print({tail!r})'], 5, 100, digest=digest)
            digests.append((output, digest.hexdigest()))
        self.assertEqual(digests[0][0], digests[1][0])
        self.assertNotEqual(digests[0][1], digests[1][1])

    def test_connection_defaults_to_loopback_and_closes_on_failure(self):
        from unittest.mock import Mock
        client_ok = Mock()
        with patch.object(controller.agent, 'OpenAI', return_value=client_ok) as sdk:
            client, model, url = controller.connect(model='example/model')
        self.assertIs(client, client_ok)
        self.assertEqual(model, 'example/model')
        self.assertEqual(url, 'http://127.0.0.1:1234/v1')
        self.assertEqual(sdk.call_args.kwargs['base_url'], 'http://127.0.0.1:1234/v1')

        with patch.object(controller.agent, 'OpenAI', return_value=client_ok) as sdk:
            _, _, url = controller.connect(base_url='http://127.0.0.1:8080/v1', model='example/model')
        self.assertEqual(url, 'http://127.0.0.1:8080/v1')
        self.assertEqual(sdk.call_args.kwargs['base_url'], 'http://127.0.0.1:8080/v1')

        failing = Mock()
        failing.models.list.side_effect = ConnectionError('unavailable')
        with patch.object(controller.agent, 'OpenAI', return_value=failing), \
             self.assertRaises(ValueError):
            controller.connect(model='example/model')
        failing.close.assert_called_once()
