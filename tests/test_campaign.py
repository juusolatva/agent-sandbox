import json
import tempfile
import unittest
from pathlib import Path

import campaign


class CampaignTests(unittest.TestCase):
    def test_resume_retries_and_retains_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state').write_text('{}')
            (root / 'tasks').write_text(json.dumps(['one', 'one', 'two']))
            directory = root / 'campaign'
            campaign.create(directory, root / 'state', root / 'tasks')
            statuses = iter(['failed', 'completed', 'completed'])
            def runner(record, task, artifacts, *args, **kwargs):
                result = Path(artifacts) / 'run'
                result.mkdir()
                status = next(statuses)
                (result / 'result.json').write_text(json.dumps({'status': status}))
                return 0 if status == 'completed' else 1
            self.assertEqual(campaign.resume(directory, 3, 1, 1, runner=runner), 'attempt_failed')
            self.assertEqual(campaign.resume(directory, 3, 1, 1, runner=runner), 'all_completed')
            data = json.loads((directory / 'campaign.json').read_text())
            self.assertEqual([len(t['attempts']) for t in data['tasks']], [2, 1])
            self.assertTrue((directory / data['tasks'][0]['attempts'][0]['result']).exists())

    def test_findings_retain_observations_and_reject_external_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'runs' / '0').mkdir(parents=True)
            (root / 'runs' / '0' / 'events.jsonl').write_text('evidence')
            finding = {'title': 'Issue', 'location': 'src/a.c:1', 'root_cause': 'overflow',
                       'status': 'suspected', 'evidence': ['runs/0/events.jsonl']}
            first = campaign.record_finding(root, finding)
            finding['status'] = 'confirmed'
            self.assertEqual(first, campaign.record_finding(root, finding))
            data = json.loads((root / 'findings.json').read_text())
            self.assertEqual(len(data[0]['observations']), 2)
            finding['evidence'] = ['../outside']
            with self.assertRaises(ValueError):
                campaign.record_finding(root, finding)

    def test_replay_uses_distinct_workers_and_records_both_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'prepared.json').write_text('{}')
            run = root / 'runs' / '0' / 'attempt' / 'run'
            run.mkdir(parents=True)
            for name in ('original', 'candidate', 'result'):
                (run / (name + '.json')).write_text('{}')
            campaign.save(root / 'campaign.json', {'tasks': [{'attempts': [
                {'result': 'runs/0/attempt/run/result.json'}]}]})
            workers = []
            class FakeWorker:
                def __init__(self, *args, **kwargs):
                    workers.append(self)
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def restore(self, bundle):
                    self.bundle = bundle
                def execute(self, command, timeout):
                    return (1 if len(workers) == 1 else 0), 'diagnostic'
            path = campaign.replay(root, 'runs/0/attempt/run', 'test command', worker_class=FakeWorker)
            report = json.loads((root / path / 'replay.json').read_text())
            self.assertEqual(report['status'], 'failure_then_pass')
            self.assertEqual(len(workers), 2)
            self.assertIn('snapshot_sha256', report['results']['original'])
            with self.assertRaises(ValueError):
                campaign.replay(root, '../outside', 'test', worker_class=FakeWorker)

    def test_attempt_limit_prevents_unbounded_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state').write_text('{}')
            (root / 'tasks').write_text('["one"]')
            campaign.create(root / 'campaign', root / 'state', root / 'tasks')
            calls = []
            def runner(*args, **kwargs):
                calls.append(True)
                return 1
            for _ in range(4):
                campaign.resume(root / 'campaign', 1, 1, 1, runner=runner, max_attempts=2)
            self.assertEqual(len(calls), 2)
            data = json.loads((root / 'campaign' / 'campaign.json').read_text())
            self.assertEqual(data['stop_reason'], 'no_eligible_targets')

    def test_scheduler_tracks_proposals_and_stalls_without_starving_new_targets(self):
        from test_investigation import finding_report, report_bundle
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state').write_text('{}')
            (root / 'tasks').write_text(json.dumps([
                {'task': 'parser', 'scope': 'parse.c', 'priority': 9},
                {'task': 'decoder', 'scope': 'decode.c'}]))
            directory = root / 'campaign'
            campaign.create(directory, root / 'state', root / 'tasks')
            prompts = []
            def runner(record, task, artifacts, *args, **kwargs):
                prompts.append(task)
                run = Path(artifacts) / 'run'
                run.mkdir()
                (run / 'result.json').write_text('{"status":"exhausted"}')
                (run / 'candidate.json').write_text(json.dumps(report_bundle(finding_report())))
                return 2
            campaign.resume(directory, 1, 1, 1, runner=runner)
            campaign.resume(directory, 1, 1, 1, runner=runner)
            self.assertTrue(prompts[0].startswith('parser'))
            self.assertTrue(prompts[1].startswith('decoder'))
            campaign.resume(directory, 2, 1, 1, runner=runner, continue_on_failure=True)
            state = campaign.status(directory)
            self.assertTrue(all(t['stalled'] and t['attempted_at'] for t in state['campaign']['tasks']))
            self.assertEqual(len(state['proposed_findings']), 1)
            self.assertFalse((directory / 'findings.json').exists())
            campaign.review(directory, 0, 0, 0, 'rejected')
            ledger = json.loads((directory / 'findings.json').read_text())
            self.assertEqual(ledger[0]['observations'][0]['status'], 'rejected')

    def test_status_reports_finding_index_used_by_review(self):
        from test_investigation import finding_report, report_bundle
        report = finding_report()
        second = dict(report['findings'][0], title='Leak', location='decode.c:free', root_cause='double free')
        report['findings'].append(second)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state').write_text('{}')
            (root / 'tasks').write_text(json.dumps(['decoder']))
            directory = root / 'campaign'
            campaign.create(directory, root / 'state', root / 'tasks')

            def runner(record, task, artifacts, *args, **kwargs):
                run = Path(artifacts) / 'run'
                run.mkdir()
                (run / 'result.json').write_text('{"status":"exhausted"}')
                (run / 'candidate.json').write_text(json.dumps(report_bundle(report)))
                return 2
            campaign.resume(directory, 1, 1, 1, runner=runner)
            groups = campaign.status(directory)['proposed_findings'].values()
            leak = next(g for g in groups if g['title'] == 'Leak')['observations'][0]
            self.assertEqual((leak['target'], leak['attempt'], leak['finding']), (0, 0, 1))
            campaign.review(directory, leak['target'], leak['attempt'], leak['finding'], 'rejected')
            ledger = json.loads((directory / 'findings.json').read_text())
            self.assertEqual(ledger[0]['observations'][0]['title'], 'Leak')

    def test_state_identity_and_finite_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state').write_text('{}')
            (root / 'tasks').write_text('["target"]')
            directory = root / 'campaign'
            campaign.create(directory, root / 'state', root / 'tasks')
            with self.assertRaises(ValueError):
                campaign.resume(directory, 1, 1, 1, budget=float('nan'))
            (directory / 'prepared.json').write_text('{"changed":true}')
            with self.assertRaises(ValueError):
                campaign.resume(directory, 1, 1, 1)

    def test_deadline_stops_selection_and_interruption_is_durable(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state').write_text('{}')
            (root / 'tasks').write_text('["one", "two"]')
            directory = root / 'campaign'
            campaign.create(directory, root / 'state', root / 'tasks')
            calls = []
            def runner(record, task, artifacts, url, model, steps, timeout, **kwargs):
                calls.append(timeout)
                raise KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                campaign.resume(directory, 1, 10, 2, runner=runner, budget=3)
            data = json.loads((directory / 'campaign.json').read_text())
            self.assertEqual(data['stop_reason'], 'interrupted')
            self.assertEqual(data['tasks'][0]['attempts'][0]['status'], 'interrupted')
            self.assertLessEqual(calls[0], 3)
            with patch('campaign.time.monotonic', side_effect=[0, 2]):
                reason = campaign.resume(directory, 2, 10, 2, runner=runner, budget=1)
            self.assertEqual(reason, 'time_budget')
            self.assertEqual(len(calls), 1)
