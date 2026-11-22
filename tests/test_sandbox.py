import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sandbox', Path(__file__).parents[1] / 'sandbox.py')
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)


class PreparationTests(unittest.TestCase):
    def test_cli_requires_minimum_python(self):
        with patch.object(sandbox.sys, 'version_info', (3, 12, 9)), \
             patch.object(sandbox.sys, 'argv', ['sandbox.py', 'check', 'state.json']), \
             patch.object(sandbox, 'run_command') as run_command, \
             patch('sys.stderr'), self.assertRaises(SystemExit) as raised:
            sandbox.main()
        self.assertNotEqual(raised.exception.code, 0)
        run_command.assert_not_called()

    def test_examples_declare_minimum_runtimes(self):
        package = json.loads((sandbox.ROOT / 'examples/fixtures/node/package.json').read_text())
        self.assertEqual(package['engines']['node'], '>=24.22.1')
        self.assertIn('requires-python = ">=3.13"', (sandbox.ROOT / 'pyproject.toml').read_text())
        fixtures = sandbox.ROOT / 'examples/fixtures'
        self.assertIn('\ngo 1.26\n', '\n' + (fixtures / 'go/go.mod').read_text())
        cargo = tomllib.loads((fixtures / 'rust/Cargo.toml').read_text())
        self.assertEqual(cargo['package']['rust-version'], '1.88')

    def test_manifest_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'project.json'
            manifest.write_text(json.dumps({'privileged': True}))
            with self.assertRaises(ValueError):
                sandbox.load_project(manifest)

    def test_examples_resolve(self):
        for manifest in (sandbox.ROOT / 'examples').glob('*.json'):
            config, source = sandbox.load_project(manifest)
            self.assertTrue(source.is_dir())
            self.assertIn(config['profile'], sandbox.PROFILES)

    def test_snapshot_excludes_credentials_and_fingerprints_content(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / '.env').write_text('secret')
            (source / '.git').mkdir()
            (source / 'main.py').write_text('print(1)')
            snapshot = Path(directory) / 'snapshot'
            first = sandbox.snapshot(source, snapshot)
            self.assertEqual([p.name for p in snapshot.iterdir()], ['main.py'])
            self.assertEqual(first, sandbox.fingerprint(snapshot))
            (snapshot / 'main.py').write_text('print(2)')
            self.assertNotEqual(first, sandbox.fingerprint(snapshot))

    def test_snapshot_skips_the_same_names_as_run_transfer(self):
        from execution import transfer
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            for name in sorted(transfer.SKIP) + ['.env.local']:
                (source / name).mkdir()
                (source / name / 'file').write_text('x')
            (source / 'kept.py').write_text('x')
            sandbox.snapshot(source, Path(directory) / 'snapshot')
            self.assertEqual([p.name for p in (Path(directory) / 'snapshot').iterdir()], ['kept.py'])

    def test_snapshot_preserves_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'run.sh').write_text('#!/bin/sh\n')
            (source / 'run.sh').chmod(0o755)
            sandbox.snapshot(source, Path(directory) / 'snapshot')
            self.assertEqual((Path(directory) / 'snapshot' / 'run.sh').stat().st_mode & 0o777, 0o755)

    def test_fingerprint_distinguishes_file_boundaries(self):
        # v1 hashed path\0mode\0data\0 records, so one file's data could encode another record.
        with tempfile.TemporaryDirectory() as directory:
            one, two = Path(directory) / 'one', Path(directory) / 'two'
            one.mkdir()
            two.mkdir()
            (one / 'a').write_text('x')
            (one / 'b').write_text('y')
            mode = str((one / 'b').lstat().st_mode)
            (two / 'a').write_bytes(b'x\0b\0' + mode.encode() + b'\0y')
            (two / 'a').chmod((one / 'a').stat().st_mode & 0o777)
            self.assertNotEqual(sandbox.fingerprint(one), sandbox.fingerprint(two))

    def test_external_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'outside').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                sandbox.snapshot(source, Path(directory) / 'snapshot')

    def test_internal_symlinks_loops_and_special_files_rejected(self):
        # Stage 3 transfer cannot represent these, so preparation must not accept them.
        import os
        for make in (lambda s: (s / 'link').symlink_to('main.py'),
                     lambda s: (s / 'loop').symlink_to('loop'),
                     lambda s: os.mkfifo(s / 'pipe')):
            with self.subTest(make=make), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'source'
                source.mkdir()
                (source / 'main.py').write_text('x')
                make(source)
                with self.assertRaises(ValueError):
                    sandbox.snapshot(source, Path(directory) / 'snapshot')

    def test_verify_candidate_read_is_bounded(self):
        from execution import transfer
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'candidate.json'
            candidate.write_bytes(b' ' * (transfer.MAX_SERIALIZED + 1))
            args = type('Args', (), {'state': str(Path(directory) / 'state.json'), 'candidate': str(candidate)})
            (Path(directory) / 'state.json').write_text('{}')
            with patch('execution.verify') as verify, self.assertRaises(ValueError):
                sandbox.run_verify_command(args)
            verify.assert_not_called()

    def test_check_disables_network_and_uses_immutable_image(self):
        with patch.object(sandbox, 'docker') as docker, patch.object(sandbox.subprocess, 'run'):
            sandbox.check_image({'image': 'sha256:example', 'config': {'check': 'true'}})
        args = docker.call_args.args
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertIn('sha256:example', args)
        self.assertIn('no-new-privileges:true', args)

    def test_every_container_and_build_overrides_all_proxy_keys(self):
        import execution
        keys = {'HTTP_PROXY', 'HTTPS_PROXY', 'FTP_PROXY', 'ALL_PROXY', 'NO_PROXY'}
        keys |= {key.lower() for key in keys}
        flags = execution.container_isolation_args()
        self.assertEqual({flags[i + 1] for i, f in enumerate(flags) if f == '--env'}, {k + '=' for k in keys})
        self.assertEqual(flags[flags.index('--log-driver') + 1], 'none')
        args = sandbox.proxy_build_args()
        self.assertEqual({args[i + 1] for i, f in enumerate(args) if f == '--build-arg'}, {k + '=' for k in keys})
        with_proxy = sandbox.proxy_build_args('http://proxy.internal:3128')
        self.assertIn('HTTPS_PROXY=http://proxy.internal:3128', with_proxy)
        self.assertIn('FTP_PROXY=', with_proxy)
        with_credentials = 'http://' + 'user' + ':' + 'pw' + '@proxy.internal:3128'
        for bad in (with_credentials, 'socks5://proxy', 'proxy:3128'):
            with self.subTest(proxy=bad), self.assertRaises(ValueError):
                sandbox.proxy_build_args(bad)
        with patch.object(sandbox, 'docker') as docker, patch.object(sandbox.subprocess, 'run'):
            sandbox.check_image({'image': 'sha256:example', 'config': {'check': 'true'}})
        self.assertIn('FTP_PROXY=', docker.call_args.args)
        self.assertIn('--log-driver', docker.call_args.args)

    def test_preparation_build_is_bounded_and_publishes_nothing_on_timeout(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'project.json'
            (Path(directory) / 'source').mkdir()
            manifest.write_text(json.dumps(dict(name='test', profile='node', source='source',
                                                check='true', prepare=['npm ci'], prepare_timeout=5)))
            state = Path(directory) / 'state.json'

            def fake_docker(*args, capture=False, timeout=None):
                if args[0] == 'build':
                    self.assertEqual(timeout, 5)
                    self.assertIn('FTP_PROXY=', args)
                    raise subprocess.TimeoutExpired('docker build', timeout)
                return 'sha256:base'
            with patch.object(sandbox, 'docker', side_effect=fake_docker), \
                 self.assertRaises(subprocess.TimeoutExpired):
                sandbox.prepare(manifest, state)
            self.assertFalse(state.exists())
        for value in (0, -1, True, '60'):
            with self.subTest(prepare_timeout=value), self.assertRaises(ValueError):
                sandbox.validate_fields(dict(name='x', source='s', profile='node', check='true',
                                             prepare_timeout=value))

    def test_preparation_does_not_publish_state_on_failed_check(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'project.json'
            source = Path(directory) / 'source'
            source.mkdir()
            manifest.write_text(json.dumps(dict(name='test', profile='python313',
                                               source='source', check='false')))
            state = Path(directory) / 'state.json'
            with patch.object(sandbox, 'docker', return_value='sha256:base'), \
                 patch.object(sandbox, 'check_image', side_effect=ValueError('baseline failed')):
                with self.assertRaises(ValueError):
                    sandbox.prepare(manifest, state)
            self.assertFalse(state.exists())

    def test_check_timeout_removes_its_container(self):
        import subprocess
        with patch.object(sandbox, 'docker', side_effect=subprocess.TimeoutExpired('docker', 1)), \
             patch.object(sandbox.subprocess, 'run') as cleanup:
            with self.assertRaises(subprocess.TimeoutExpired):
                sandbox.check_image({'image': 'sha256:example', 'config': {'check': 'sleep 99', 'timeout': 1}})
        self.assertEqual(cleanup.call_args.args[0][:3], ['docker', 'rm', '-f'])
        self.assertTrue(cleanup.call_args.args[0][3].startswith('sandbox-check-'))

    def test_protected_baseline_failure_does_not_publish_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'source').mkdir()
            (root / 'checks').mkdir()
            manifest = root / 'project.json'
            manifest.write_text(json.dumps(dict(name='test', profile='python313',
                source='source', check='true', verification={'source':'checks', 'command':'false'})))
            with patch.object(sandbox, 'docker', return_value='sha256:base'), \
                 patch.object(sandbox, 'check_image'), \
                 patch('execution.verify', return_value=(1, 'protected check failed')):
                with self.assertRaises(ValueError):
                    sandbox.prepare(manifest, root / 'state.json')
            self.assertFalse((root / 'state.json').exists())
