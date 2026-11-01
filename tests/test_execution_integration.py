"""Opt-in Docker tests against a Stage 3 prepared Python fixture."""
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import controller
import campaign
import shlex
from execution import Worker, verify


class Message(types.SimpleNamespace):
    def model_dump(self):
        return {'content': self.content}


class FakeClient:
    def __init__(self):
        self.responses = iter([
            Message(content='<bash>printf evidence > evidence.txt</bash>', tool_calls=None),
            Message(content='<done>Protected checks and evidence</done>', tool_calls=None)])
        self.chat = self
        self.completions = self

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=next(self.responses))])


@unittest.skipUnless(os.environ.get('SANDBOX_DOCKER_TESTS') == '1', 'Set SANDBOX_DOCKER_TESTS=1')
class DockerBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = json.loads(Path('artifacts/python313.json').read_text())

    def test_protected_verification_rejects_broken_source(self):
        self.assertEqual(verify(self.record)[0], 0)
        with Worker(self.record) as worker:
            code, _ = worker.execute("printf 'def add(a,b): return 0\\n' > calculator.py; printf 'pass\\n' > test_smoke.py")
            self.assertEqual(code, 0)
            candidate = worker.export()
            self.assertNotEqual(worker.execute('touch /opt/sandbox/verification/check.py')[0], 0)
            self.assertNotEqual(verify(self.record, candidate)[0], 0)
        self.assertEqual(verify(self.record)[0], 0)

    def test_detached_process_cannot_survive_command(self):
        with Worker(self.record) as worker:
            command = "python3 -c 'import os,time; os.setsid(); time.sleep(.8); open(\"survivor\",\"w\").close()' >/dev/null 2>&1 &"
            self.assertEqual(worker.execute(command)[0], 0)
            self.assertEqual(worker.execute('sleep 1; test ! -e survivor')[0], 0)

    def test_symlink_export_fails_closed(self):
        with Worker(self.record) as worker:
            self.assertEqual(worker.execute('ln -s /etc/passwd escape')[0], 0)
            with self.assertRaises(RuntimeError):
                worker.export()

    def test_controller_retains_evidence_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            code = controller.run(self.record, 'Create evidence and finish', directory,
                                  model='fake', steps=2, timeout=90, client=FakeClient())
            self.assertEqual(code, 0)
            run = next(Path(directory).iterdir())
            result = json.loads((run / 'result.json').read_text())
            self.assertTrue(result['baseline_verified'])
            self.assertTrue(result['candidate_verified'])
            self.assertIn('evidence.txt', (run / 'candidate.patch').read_text())
            self.assertTrue((run / 'events.jsonl').exists())

    def test_campaign_import_review_and_fresh_worker_replay(self):
        from test_investigation import finding_report
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'state.json').write_text(json.dumps(self.record))
            (root / 'tasks.json').write_text('["Investigate the evidence fixture"]')
            directory = root / 'campaign'
            campaign.create(directory, root / 'state.json', root / 'tasks.json')
            client = FakeClient()
            command = 'printf evidence > evidence.txt; printf %s ' + shlex.quote(json.dumps(finding_report())) + ' > investigation-findings.json'
            client.responses = iter([
                Message(content='<bash>' + command + '</bash>', tool_calls=None),
                Message(content='<done>Evidence retained</done>', tool_calls=None)])
            def runner(record, task, artifacts, base_url, model, steps, timeout, **kwargs):
                return controller.run(record, task, artifacts, model='fake', steps=steps,
                                      timeout=timeout, client=client, text_actions=True)
            self.assertEqual(campaign.resume(directory, 1, 90, 3, runner=runner), 'all_completed')
            state = campaign.status(directory)
            attempt = state['campaign']['tasks'][0]['attempts'][0]
            self.assertEqual(attempt['report_status'], 'imported')
            self.assertEqual(len(state['proposed_findings']), 1)
            self.assertFalse((directory / 'findings.json').exists())
            run = str(Path(attempt['result']).parent)
            command = 'python3 -c "from pathlib import Path; assert Path(\'evidence.txt\').read_text() == \'evidence\'"'
            replay_path = campaign.replay(directory, run, command, timeout=60)
            report = json.loads((directory / replay_path / 'replay.json').read_text())
            self.assertEqual(report['status'], 'failure_then_pass')
            self.assertIn('FileNotFoundError', report['results']['original']['output'])
            self.assertEqual(report['results']['candidate']['code'], 0)
            timeout_path = campaign.replay(directory, run, 'sleep 5', timeout=.5)
            timed = json.loads((directory / timeout_path / 'replay.json').read_text())
            self.assertEqual(timed['status'], 'no_failure_then_pass')
            self.assertEqual([r['code'] for r in timed['results'].values()], [124, 124])
            campaign.review(directory, 0, 0, 0, 'rejected')
            self.assertTrue((directory / 'findings.json').exists())

    def test_controller_stops_repeated_commands_with_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            client.responses = iter([Message(content='<bash>printf repeat</bash>', tool_calls=None)] * 4)
            code = controller.run(self.record, 'Repeated command fixture', directory,
                                  model='fake', steps=5, timeout=90, client=client, text_actions=True)
            self.assertNotEqual(code, 0)
            run = next(Path(directory).iterdir())
            result = json.loads((run / 'result.json').read_text())
            self.assertEqual(result['stop_reason'], 'repeated_command')
            self.assertTrue((run / 'candidate.json').exists())
            events = [json.loads(line) for line in (run / 'events.jsonl').read_text().splitlines()]
            self.assertEqual(len([e for e in events if e.get('command') == 'printf repeat']), 3)

    def test_all_language_protected_baselines(self):
        profiles = (os.environ['SANDBOX_TEST_PROFILE'],) if os.environ.get('SANDBOX_TEST_PROFILE') else ('cpp', 'go', 'rust', 'node', 'python314')
        for profile in profiles:
            with self.subTest(profile=profile):
                record = json.loads(Path(f'artifacts/{profile}.json').read_text())
                code, output = verify(record)
                self.assertEqual(code, 0, output)


@unittest.skipUnless(os.environ.get('SANDBOX_DOCKER_TESTS') == '1', 'Set SANDBOX_DOCKER_TESTS=1')
class CppSanitizerRegression(unittest.TestCase):
    @unittest.skipIf(os.environ.get('SANDBOX_TEST_PROFILE') not in (None, 'cpp'), 'C++ profile only')
    def test_repeated_sanitizer_startup(self):
        record = json.loads(Path('artifacts/cpp.json').read_text())
        command = record['config']['verification']['command'] + '; for i in $(seq 1 100); do /tmp/check-clang || exit $?; done'
        with Worker(record) as worker:
            code, output = worker.execute(command, readonly=True)
        self.assertEqual(code, 0, output)
