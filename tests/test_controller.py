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
        diff = controller.patch_bytes(before, after)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source.txt').write_bytes(b'old')
            subprocess.run(['git', 'apply', '-'], cwd=root, input=diff, check=True)
            self.assertEqual((root / 'source.txt').read_bytes(), b'new')
            self.assertEqual((root / 'new.bin').read_bytes(), b'\0binary')
            self.assertTrue((root / 'source.txt').stat().st_mode & 0o111)

    def test_patch_preserves_non_utf8_text_bytes(self):
        before = bundle(b'caf\xe9 = 1\n', 'latin1.py')
        after = bundle(b'caf\xe9 = 2\n\xff\xfe tail\n', 'latin1.py')
        diff = controller.patch_bytes(before, after)
        self.assertNotIn(b'GIT binary patch', diff)  # Classified as text, so bytes must survive.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'latin1.py').write_bytes(b'caf\xe9 = 1\n')
            subprocess.run(['git', 'apply', '-'], cwd=root, input=diff, check=True)
            self.assertEqual((root / 'latin1.py').read_bytes(), b'caf\xe9 = 2\n\xff\xfe tail\n')

    def test_patch_ignores_host_git_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'gitconfig'
            config.write_text('[color]\n\tui = always\n\tdiff = always\n[core]\n\tautocrlf = true\n')
            with patch.dict(controller.os.environ, {'GIT_CONFIG_GLOBAL': str(config)}):
                diff = controller.patch_bytes(bundle(b'old\n'), bundle(b'new\n'))
        self.assertNotIn(b'\x1b[', diff)
        self.assertIn(b'-old\n+new\n', diff)

    def test_validate_rejects_oversized_field_before_decoding(self):
        oversized = {'big': {'data': 'A' * (controller.transfer.LIMIT // 3 * 4 + 8), 'executable': False}}
        with patch.object(controller.transfer.base64, 'b64decode') as decode, self.assertRaises(ValueError):
            controller.transfer.validate(oversized)
        decode.assert_not_called()
        with self.assertRaises(ValueError):
            controller.transfer.validate({'a': {'data': ['not', 'text'], 'executable': False}})

    def test_load_caps_serialized_input_before_parsing(self):
        import io
        stream = io.BytesIO(b' ' * (controller.transfer.MAX_SERIALIZED + 1))
        with patch.object(controller.transfer.json, 'loads') as loads, self.assertRaises(ValueError):
            controller.transfer.load(stream)
        loads.assert_not_called()
        self.assertEqual(controller.transfer.load(io.BytesIO(json.dumps(bundle()).encode())), bundle())

    def test_export_rejects_linked_root_special_files_and_unreadable_directories(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / 'real'
            real.mkdir()
            (real / 'plain.txt').write_text('ordinary')
            linked = Path(directory) / 'workspace'
            linked.symlink_to(real)
            with self.assertRaises(ValueError):
                controller.transfer.export(linked)
            os.mkfifo(real / 'pipe')
            with self.assertRaises(ValueError):
                controller.transfer.export(real)
            (real / 'pipe').unlink()
            if os.geteuid() != 0:
                hidden = real / 'hidden'
                hidden.mkdir()
                (hidden / 'source.py').write_text('x = 1\n')
                hidden.chmod(0)
                try:
                    with self.assertRaises(ValueError):
                        controller.transfer.export(real)
                finally:
                    hidden.chmod(0o755)

    def test_export_rejects_entry_swapped_to_symlink_after_classification(self):
        import os
        import stat as stat_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'swapped').symlink_to('/etc/hostname')
            real_stat = os.stat

            def stale_stat(name, *args, **kwargs):
                result = real_stat(name, *args, **kwargs)
                if name == 'swapped':  # Pretend the entry was a regular file when classified.
                    return os.stat_result((stat_module.S_IFREG | 0o644,) + tuple(result)[1:])
                return result
            with patch.object(controller.transfer.os, 'stat', side_effect=stale_stat), \
                 self.assertRaises(ValueError):
                controller.transfer.export(root)

    def test_stat_failure_during_export_is_a_named_value_error(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'vanishing.py').write_text('x')
            real_stat = os.stat

            def failing_stat(name, *args, **kwargs):
                if name == 'vanishing.py':
                    raise FileNotFoundError(2, 'No such file or directory')
                return real_stat(name, *args, **kwargs)
            with patch.object(controller.transfer.os, 'stat', side_effect=failing_stat), \
                 self.assertRaisesRegex(ValueError, 'Cannot stat vanishing.py'):
                controller.transfer.export(root)

    def test_patch_failure_reports_git_stderr(self):
        failed = subprocess.CompletedProcess([], 128, b'', b'fatal: example failure\n')
        with patch.object(controller.subprocess, 'run', return_value=failed), \
             self.assertRaisesRegex(RuntimeError, 'fatal: example failure'):
            controller.patch_bytes(bundle(), bundle(b'new'))

    def test_export_keeps_stderr_out_of_bundle_and_rejects_truncation(self):
        worker = execution.Worker({'image': 'sha256:fixture', 'config': {}})

        def fake(args, timeout, limit, payload, digest, details):
            details.update(errors='WARNING: swap limit not supported', truncated=False)
            return 0, json.dumps(bundle())
        with patch.object(execution, 'bounded_process', side_effect=fake), \
             patch.object(execution.subprocess, 'run'):
            self.assertEqual(worker.export(), bundle())

        def truncated(args, timeout, limit, payload, digest, details):
            details.update(errors='', truncated=True)
            return 0, '{"partial'
        with patch.object(execution, 'bounded_process', side_effect=truncated), \
             patch.object(execution.subprocess, 'run'), self.assertRaises(RuntimeError):
            worker.export()

    def test_bounded_process_can_separate_stderr(self):
        details = {}
        code, output = execution.bounded_process(
            ['python3', '-c', 'import sys; print("{}"); print("warning", file=sys.stderr)'], 5,
            details=details)
        self.assertEqual((code, output), (0, '{}\n'))
        self.assertEqual(details, {'errors': 'warning\n', 'truncated': False})

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
