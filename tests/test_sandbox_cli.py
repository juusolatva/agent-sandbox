"""Command-line wiring for sandbox.py: each subcommand reaches the right function."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import advisories
import campaign
import controller
import execution

spec = importlib.util.spec_from_file_location('sandbox_cli', Path(__file__).parents[1] / 'sandbox.py')
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='sandbox-cli-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.directory, ignore_errors=True))
        self.state = self.directory / 'state.json'
        self.state.write_text(json.dumps({'image': 'sha256:fixture'}))

    def invoke(self, *argv):
        """Run main() with argv; return (exit code, stdout, stderr). Exit code is None without SystemExit."""
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {k: v for k, v in os.environ.items() if k not in ('OPENAI_BASE_URL', 'LLM_MODEL')}
        code = None
        with patch.object(sandbox.sys, 'argv', ['sandbox.py', *argv]), patch('signal.signal'), \
             patch.dict(os.environ, environment, clear=True), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                sandbox.main()
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_build_prepare_and_check(self):
        with patch.object(sandbox, 'build') as build:
            self.assertIsNone(self.invoke('build', 'python313', '--ca', 'ca.pem')[0])
        build.assert_called_once_with('python313', 'ca.pem', None)
        with patch.object(sandbox, 'prepare') as prepare:
            self.invoke('prepare', 'examples/go.json', '--state', 'artifacts/go.json',
                        '--proxy', 'http://proxy.internal:3128')
        prepare.assert_called_once_with('examples/go.json', 'artifacts/go.json', None,
                                        'http://proxy.internal:3128')
        with patch.object(sandbox, 'check_image') as check:
            self.invoke('check', str(self.state))
        check.assert_called_once_with({'image': 'sha256:fixture'})

    def test_unknown_profile_is_rejected_by_parser(self):
        with patch.object(sandbox, 'build') as build:
            code, _, stderr = self.invoke('build', 'cobol')
        self.assertEqual(code, 2)
        self.assertIn('invalid choice', stderr)
        build.assert_not_called()

    def test_errors_become_exit_codes(self):
        failure = subprocess.CalledProcessError(3, ['docker', 'build'])
        with patch.object(sandbox, 'build', side_effect=failure):
            code, _, stderr = self.invoke('build', 'go')
        self.assertEqual(code, 3)
        self.assertIn('docker', stderr)
        with patch.object(sandbox, 'check_image', side_effect=ValueError('baseline failed')):
            code, _, stderr = self.invoke('check', str(self.state))
        self.assertEqual((code, stderr), (1, 'baseline failed\n'))

    def test_verify_passes_validated_candidate_and_exit_code(self):
        candidate = self.directory / 'candidate.json'
        bundle = {'source.txt': {'data': base64.b64encode(b'hello\n').decode(), 'executable': False}}
        candidate.write_text(json.dumps(bundle))
        with patch.object(execution, 'verify', return_value=(4, 'checks failed')) as verify:
            code, stdout, _ = self.invoke('verify', str(self.state), '--candidate', str(candidate))
        self.assertEqual((code, stdout), (4, 'checks failed\n'))
        verify.assert_called_once_with({'image': 'sha256:fixture'}, bundle)
        with patch.object(execution, 'verify', return_value=(0, 'ok')) as verify:
            self.assertEqual(self.invoke('verify', str(self.state))[0], 0)
        verify.assert_called_once_with({'image': 'sha256:fixture'}, None)

    def test_verify_rejects_invalid_candidate_before_verification(self):
        candidate = self.directory / 'candidate.json'
        candidate.write_text(json.dumps({'../escape': {'data': '', 'executable': False}}))
        with patch.object(execution, 'verify') as verify:
            code, _, _ = self.invoke('verify', str(self.state), '--candidate', str(candidate))
        self.assertEqual(code, 1)
        verify.assert_not_called()

    def test_run_passes_options_and_outcome(self):
        with patch.object(controller, 'run', return_value=2) as run:
            code, _, _ = self.invoke('run', str(self.state), 'Find bugs', '--artifacts', 'out', '--model', 'm',
                                     '--base-url', 'http://127.0.0.1:1234/v1', '--steps', '5',
                                     '--timeout', '60', '--text-actions')
        self.assertEqual(code, 2)
        run.assert_called_once_with({'image': 'sha256:fixture'}, 'Find bugs', 'out', 'http://127.0.0.1:1234/v1',
                                    'm', 5, 60.0, text_actions=True)

    def test_run_defaults(self):
        with patch.object(controller, 'run', return_value=0) as run:
            self.invoke('run', str(self.state), 'task')
        run.assert_called_once_with({'image': 'sha256:fixture'}, 'task', 'artifacts/runs', None, None, 20, 1800.0,
                                    text_actions=False)

    def test_campaign_resume_passes_every_limit(self):
        with patch.object(campaign, 'resume', return_value='time_budget') as resume:
            code, stdout, _ = self.invoke('campaign', 'resume', 'camp', '--max-runs', '4', '--max-attempts', '2',
                                          '--budget', '90', '--continue-on-failure', '--stall-limit', '3',
                                          '--timeout', '30', '--steps', '7', '--model', 'm', '--text-actions')
        self.assertEqual(code, 1)
        self.assertIn('Campaign stopped: time_budget', stdout)
        resume.assert_called_once_with('camp', 4, 30.0, 7, None, 'm', True, max_attempts=2, budget=90.0,
                                       continue_on_failure=True, stall_limit=3)

    def test_campaign_resume_succeeds_only_when_all_completed(self):
        with patch.object(campaign, 'resume', return_value='all_completed') as resume:
            self.assertEqual(self.invoke('campaign', 'resume', 'camp', '--max-runs', '1')[0], 0)
        resume.assert_called_once_with('camp', 1, 1800.0, 20, None, None, False, max_attempts=3, budget=None,
                                       continue_on_failure=False, stall_limit=2)

    def test_campaign_create_status_review(self):
        with patch.object(campaign, 'create') as create:
            self.invoke('campaign', 'create', 'camp', 'state.json', 'tasks.json')
        create.assert_called_once_with('camp', 'state.json', 'tasks.json')
        with patch.object(campaign, 'status', return_value={'proposed_findings': {}}):
            _, stdout, _ = self.invoke('campaign', 'status', 'camp')
        self.assertEqual(json.loads(stdout), {'proposed_findings': {}})
        with patch.object(campaign, 'review', return_value='finding-id') as review:
            _, stdout, _ = self.invoke('campaign', 'review', 'camp', '0', '1', '2', '--status', 'rejected')
        self.assertEqual(stdout, 'finding-id\n')
        review.assert_called_once_with('camp', 0, 1, 2, 'rejected')

    def test_campaign_review_requires_known_status(self):
        with patch.object(campaign, 'review') as review:
            code, _, _ = self.invoke('campaign', 'review', 'camp', '0', '0', '0', '--status', 'approved')
        self.assertEqual(code, 2)
        review.assert_not_called()

    def test_campaign_finding_and_replay_read_operator_files(self):
        finding = self.directory / 'finding.json'
        finding.write_text('{"title": "Overflow"}')
        reproducer = self.directory / 'reproduce.sh'
        reproducer.write_text('python3 -m pytest -x\n')
        with patch.object(campaign, 'record_finding', return_value='id') as record:
            self.invoke('campaign', 'finding', 'camp', str(finding))
        record.assert_called_once_with('camp', {'title': 'Overflow'})
        with patch.object(campaign, 'replay', return_value='runs/replays/x') as replay:
            _, stdout, _ = self.invoke('campaign', 'replay', 'camp', 'runs/0/a', str(reproducer), '--timeout', '15')
        self.assertEqual(stdout, 'runs/replays/x\n')
        replay.assert_called_once_with('camp', 'runs/0/a', 'python3 -m pytest -x\n', 15.0)

    def test_advisory_ecosystems_match_module(self):
        self.assertEqual(sandbox.ADVISORY_ECOSYSTEMS, advisories.ECOSYSTEMS)

    def test_advisories_fetch_forwards_ecosystems(self):
        metadata = {'fetched_at': 'now', 'databases': {'Go': {}}}
        with patch.object(advisories, 'fetch', return_value=metadata) as fetch:
            self.invoke('advisories', 'fetch', 'snap', '--ecosystem', 'Go', '--ecosystem', 'npm')
            fetch.assert_called_once_with('snap', ('Go', 'npm'))
            fetch.reset_mock()
            self.invoke('advisories', 'fetch', 'snap')
            fetch.assert_called_once_with('snap', advisories.ECOSYSTEMS)
        code, _, stderr = self.invoke('advisories', 'fetch', 'snap', '--ecosystem', 'Hackage')
        self.assertEqual(code, 2)

    def test_advisories_scan_writes_report_and_maps_status(self):
        report = {'snapshot': {'fetched_at': 'x', 'age_days': 1, 'stale': False}, 'checks': [
            {'lockfile': 'go.mod', 'status': 'scanned', 'vulnerabilities': [
                {'id': 'GO-1', 'package': 'p', 'version': '1'}]}]}
        output = self.directory / 'out' / 'report.json'
        with patch.object(advisories, 'scan_image', return_value=report) as scan:
            code, stdout, _ = self.invoke('advisories', 'scan', str(self.state), 'snap',
                                          '--output', str(output), '--max-age-days', '3')
        scan.assert_called_once_with('sha256:fixture', 'snap', 3.0)
        self.assertEqual(code, 1)
        self.assertIn('GO-1', stdout)
        self.assertEqual(json.loads(output.read_text()), report)
        with patch.object(advisories, 'scan_image', return_value={**report, 'checks': []}):
            self.assertEqual(self.invoke('advisories', 'scan', str(self.state), 'snap')[0], 2)
        report['checks'][0]['vulnerabilities'] = []
        with patch.object(advisories, 'scan_image', return_value=report):
            self.assertEqual(self.invoke('advisories', 'scan', str(self.state), 'snap')[0], 0)


if __name__ == '__main__':
    unittest.main()
