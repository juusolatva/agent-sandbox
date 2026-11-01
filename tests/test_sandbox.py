import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sandbox', Path(__file__).parents[1] / 'sandbox.py')
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)


class PreparationTests(unittest.TestCase):
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
            sandbox.snapshot(source, snapshot)
            self.assertEqual([p.name for p in snapshot.iterdir()], ['main.py'])
            first = sandbox.fingerprint(snapshot)
            (snapshot / 'main.py').write_text('print(2)')
            self.assertNotEqual(first, sandbox.fingerprint(snapshot))

    def test_external_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'outside').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                sandbox.snapshot(source, Path(directory) / 'snapshot')

    def test_check_disables_network_and_uses_immutable_image(self):
        with patch.object(sandbox, 'docker') as docker, patch.object(sandbox.subprocess, 'run'):
            sandbox.check_image({'image': 'sha256:example', 'config': {'check': 'true'}})
        args = docker.call_args.args
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertIn('sha256:example', args)
        self.assertIn('no-new-privileges:true', args)

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
